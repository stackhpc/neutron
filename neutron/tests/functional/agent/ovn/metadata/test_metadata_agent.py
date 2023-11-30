# Copyright 2020 Red Hat, Inc.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import re
from unittest import mock

from oslo_config import fixture as fixture_config
from oslo_utils import uuidutils
from ovsdbapp.backend.ovs_idl import event
from ovsdbapp.backend.ovs_idl import idlutils
from ovsdbapp.tests.functional.schema.ovn_southbound import event as test_event

from neutron.agent.linux import iptables_manager
from neutron.agent.ovn.metadata import agent
from neutron.agent.ovn.metadata import ovsdb
from neutron.agent.ovn.metadata import server as metadata_server
from neutron.common.ovn import constants as ovn_const
from neutron.common import utils as n_utils
from neutron.conf.agent.metadata import config as meta_config
from neutron.conf.agent.ovn.metadata import config as meta_config_ovn
from neutron.tests.common import net_helpers
from neutron.tests.functional import base


class MetadataAgentHealthEvent(event.WaitEvent):
    event_name = 'MetadataAgentHealthEvent'

    def __init__(self, chassis, sb_cfg, table, timeout=5):
        self.chassis = chassis
        self.sb_cfg = sb_cfg
        super(MetadataAgentHealthEvent, self).__init__(
            (self.ROW_UPDATE,), table, (('name', '=', self.chassis),),
            timeout=timeout)

    def matches(self, event, row, old=None):
        if not super(MetadataAgentHealthEvent, self).matches(event, row, old):
            return False
        return int(row.external_ids.get(
            ovn_const.OVN_AGENT_METADATA_SB_CFG_KEY, 0)) >= self.sb_cfg


class TestMetadataAgent(base.TestOVNFunctionalBase):
    OVN_BRIDGE = 'br-int'
    FAKE_CHASSIS_HOST = 'ovn-host-fake'

    def setUp(self):
        super(TestMetadataAgent, self).setUp()
        self.handler = self.sb_api.idl.notify_handler
        # We only have OVN NB and OVN SB running for functional tests
        self.mock_ovsdb_idl = mock.Mock()
        mock_metadata_instance = mock.Mock()
        mock_metadata_instance.start.return_value = self.mock_ovsdb_idl
        mock_metadata = mock.patch.object(
            ovsdb, 'MetadataAgentOvsIdl').start()
        mock_metadata.return_value = mock_metadata_instance
        self._mock_get_ovn_br = mock.patch.object(
            agent.MetadataAgent,
            '_get_ovn_bridge',
            return_value=self.OVN_BRIDGE).start()
        self.agent = self._start_metadata_agent()

    @property
    def agent_chassis_table(self):
        if self.agent.has_chassis_private:
            return 'Chassis_Private'
        return 'Chassis'

    def _start_metadata_agent(self):
        conf = self.useFixture(fixture_config.Config()).conf
        conf.register_opts(meta_config.SHARED_OPTS)
        conf.register_opts(meta_config.UNIX_DOMAIN_METADATA_PROXY_OPTS)
        conf.register_opts(meta_config.METADATA_PROXY_HANDLER_OPTS)
        conf.register_opts(meta_config_ovn.OVS_OPTS, group='ovs')
        meta_config_ovn.setup_privsep()

        ovn_sb_db = self.ovsdb_server_mgr.get_ovsdb_connection_path('sb')
        conf.set_override('ovn_sb_connection', ovn_sb_db, group='ovn')
        conf.set_override('metadata_workers', '0')

        self.chassis_name = self.add_fake_chassis(self.FAKE_CHASSIS_HOST)
        mock.patch.object(agent.MetadataAgent,
                          '_get_own_chassis_name',
                          return_value=self.chassis_name).start()
        agt = agent.MetadataAgent(conf)
        with mock.patch.object(metadata_server.UnixDomainMetadataProxy,
                               'wait'):
            agt.start()

        # Metadata agent will open connections to OVS and SB databases.
        # Close connections to them when the test ends,
        self.addCleanup(agt.ovs_idl.ovsdb_connection.stop)
        self.addCleanup(agt.sb_idl.ovsdb_connection.stop)

        return agt

    def test_metadata_agent_healthcheck(self):
        chassis_row = self.sb_api.db_find(
            self.agent_chassis_table,
            ('name', '=', self.chassis_name)).execute(
            check_error=True)[0]

        # Assert that, prior to creating a resource the metadata agent
        # didn't populate the external_ids from the Chassis
        self.assertNotIn(ovn_const.OVN_AGENT_METADATA_SB_CFG_KEY,
                         chassis_row['external_ids'])

        # Let's list the agents to force the nb_cfg to be bumped on NB
        # db, which will automatically increment the nb_cfg counter on
        # NB_Global and make ovn-controller copy it over to SB_Global. Upon
        # this event, Metadata agent will update the external_ids on its
        # Chassis row to signal that it's healthy.

        row_event = MetadataAgentHealthEvent(self.chassis_name, 1,
                                             self.agent_chassis_table)
        self.handler.watch_event(row_event)
        self.new_list_request('agents').get_response(self.api)

        # If we do not time out waiting for the event, then we are assured
        # that the metadata agent has populated the external_ids from the
        # chassis with the nb_cfg, 1 revisions when listing the agents.
        self.assertTrue(row_event.wait())

    def _create_metadata_port(self, txn, lswitch_name):
        mdt_port_name = 'ovn-mdt-' + uuidutils.generate_uuid()
        txn.add(
            self.nb_api.lsp_add(
                lswitch_name,
                mdt_port_name,
                type=ovn_const.LSP_TYPE_LOCALPORT,
                addresses='AA:AA:AA:AA:AA:AA 192.168.122.123',
                external_ids={
                    ovn_const.OVN_CIDRS_EXT_ID_KEY: '192.168.122.123/24'}))

    def _create_logical_switch_port(self, type_=None, addresses=None):
        lswitch_name = 'ovn-' + uuidutils.generate_uuid()
        lswitchport_name = 'ovn-port-' + uuidutils.generate_uuid()
        # It may take some time to ovn-northd to translate from OVN NB DB to
        # the OVN SB DB. Wait for port binding event to happen before binding
        # the port to chassis.
        pb_event = test_event.WaitForPortBindingEvent(lswitchport_name)
        self.handler.watch_event(pb_event)

        lswitch_port_columns = {}
        if addresses:
            lswitch_port_columns['addresses'] = addresses
        if type_:
            lswitch_port_columns['type'] = type_

        with self.nb_api.transaction(check_error=True, log_errors=True) as txn:
            txn.add(
                self.nb_api.ls_add(lswitch_name))
            txn.add(
                self.nb_api.create_lswitch_port(
                    lswitchport_name, lswitch_name, **lswitch_port_columns))
            self._create_metadata_port(txn, lswitch_name)
        self.assertTrue(pb_event.wait())

        return lswitchport_name, lswitch_name

    def test_agent_resync_on_non_existing_bridge(self):
        BR_NEW = 'br-new'
        self._mock_get_ovn_br.return_value = BR_NEW
        self.agent.ovs_idl.list_br.return_value.execute.return_value = [BR_NEW]
        # The agent has initialized with br-int and above list_br doesn't
        # return it, hence the agent should trigger reconfiguration and store
        # new br-new value to its attribute.
        self.assertEqual(self.OVN_BRIDGE, self.agent.ovn_bridge)

        # NOTE: The IP address is specifically picked such that it fits the
        # metadata port external_ids: { neutron:cidrs }. This is because agent
        # will only trigger if the logical port is part of a neutron subnet
        lswitchport_name, _ = self._create_logical_switch_port(
            addresses='AA:AA:AA:AA:AA:AB 192.168.122.125'
        )

        # Trigger PortBindingChassisCreatedEvent
        self.sb_api.lsp_bind(lswitchport_name, self.chassis_name).execute(
            check_error=True, log_errors=True)

        exc = Exception("Agent bridge hasn't changed from %s to %s "
                        "in 10 seconds after Port_Binding event" %
                        (self.agent.ovn_bridge, BR_NEW))
        n_utils.wait_until_true(
            lambda: BR_NEW == self.agent.ovn_bridge,
            timeout=10,
            exception=exc)

    def _test_agent_events(self, delete, type_=None):
        m_pb_created = mock.patch.object(
            agent.PortBindingChassisCreatedEvent, 'run').start()
        m_pb_deleted = mock.patch.object(
            agent.PortBindingChassisDeletedEvent, 'run').start()

        lswitchport_name, lswitch_name = self._create_logical_switch_port(
            type_)
        self.sb_api.lsp_bind(lswitchport_name, self.chassis_name).execute(
            check_error=True, log_errors=True)

        def pb_created():
            if m_pb_created.call_count < 1:
                return False
            args = m_pb_created.call_args[0]
            self.assertEqual('update', args[0])
            self.assertEqual(self.chassis_name, args[1].chassis[0].name)
            self.assertFalse(args[2].chassis)
            return True

        n_utils.wait_until_true(
            pb_created,
            timeout=10,
            exception=Exception(
                "PortBindingChassisCreatedEvent didn't happen on port "
                "binding."))

        if delete:
            self.nb_api.delete_lswitch_port(
                lswitchport_name, lswitch_name).execute(
                    check_error=True, log_errors=True)
        else:
            self.sb_api.lsp_unbind(lswitchport_name).execute(
                check_error=True, log_errors=True)

        def pb_deleted():
            if m_pb_deleted.call_count < 1:
                return False
            args = m_pb_deleted.call_args[0]
            if delete:
                self.assertEqual('delete', args[0])
                self.assertTrue(args[1].chassis)
                self.assertEqual(self.chassis_name, args[1].chassis[0].name)
            else:
                self.assertEqual('update', args[0])
                self.assertFalse(args[1].chassis)
                self.assertEqual(self.chassis_name, args[2].chassis[0].name)
            return True

        n_utils.wait_until_true(
            pb_deleted,
            timeout=10,
            exception=Exception(
                "PortBindingChassisDeletedEvent didn't happen on port "
                "unbind or delete."))

        self.assertEqual(1, m_pb_deleted.call_count)

    def test_agent_unbind_port(self):
        self._test_agent_events(delete=False)

    def test_agent_delete_bound_external_port(self):
        self._test_agent_events(delete=True, type_='external')

    def test_agent_delete_bound_nonexternal_port(self):
        with mock.patch.object(agent.LOG, 'warning') as m_warn:
            self._test_agent_events(delete=True)
        self.assertTrue(m_warn.called)

    def test_agent_registration_at_chassis_create_event(self):
        def check_for_metadata():
            chassis = self.sb_api.lookup(
                self.agent_chassis_table, self.chassis_name)
            return ovn_const.OVN_AGENT_METADATA_ID_KEY in chassis.external_ids

        exc = Exception('Chassis not created, %s is not in chassis '
                        'external_ids' % ovn_const.OVN_AGENT_METADATA_ID_KEY)
        n_utils.wait_until_true(check_for_metadata, timeout=5, exception=exc)

        # Delete Chassis and assert
        chassis = self.sb_api.lookup('Chassis', self.chassis_name)
        self.del_fake_chassis(chassis.name)
        self.assertRaises(idlutils.RowNotFound, self.sb_api.lookup,
                          'Chassis', self.chassis_name)

        # Re-add the Chassis
        self.add_fake_chassis(self.FAKE_CHASSIS_HOST, name=self.chassis_name)
        exc = Exception('Agent metadata failed to re-register itself '
                        'after the Chassis %s was re-created' %
                        self.chassis_name)

        # Check if metadata agent was re-registered
        chassis = self.sb_api.lookup('Chassis', self.chassis_name)
        n_utils.wait_until_true(
            check_for_metadata,
            timeout=10,
            exception=exc)

    def test_metadata_agent_only_monitors_own_chassis(self):
        # We already have the fake chassis which we should be monitoring, so
        # create an event looking for a change to another chassis
        other_name = uuidutils.generate_uuid()
        other_chassis = self.add_fake_chassis(self.FAKE_CHASSIS_HOST,
                                              name=other_name)
        self.assertEqual(other_chassis, other_name)

        event = MetadataAgentHealthEvent(chassis=other_name, sb_cfg=-1,
                                         table=self.agent_chassis_table,
                                         timeout=0)
        # Use the agent's sb_idl to watch for the event since it has condition
        self.agent.sb_idl.idl.notify_handler.watch_event(event)
        # Use the test sb_api to set other_chassis values since shouldn't exist
        # on agent's sb_idl
        self.sb_api.db_set(
            self.agent_chassis_table, other_chassis,
            ('external_ids', {'test': 'value'})).execute(check_error=True)

        event2 = MetadataAgentHealthEvent(chassis=self.chassis_name, sb_cfg=-1,
                                          table=self.agent_chassis_table)
        self.agent.sb_idl.idl.notify_handler.watch_event(event2)
        # Use the test's sb_api again to send a command so we can see if it
        # completes and short-circuit the need to wait for a timeout to pass
        # the test. If we get the result to this, we would have gotten the
        # previous result as well.
        self.sb_api.db_set(
            self.agent_chassis_table, self.chassis_name,
            ('external_ids', {'test': 'value'})).execute(check_error=True)
        self.assertTrue(event2.wait())
        self.assertFalse(event.wait())

    def test__ensure_datapath_checksum_if_dpdk(self):
        self.mock_ovsdb_idl.db_get.return_value.execute.return_value = (
            ovn_const.CHASSIS_DATAPATH_NETDEV)
        regex = re.compile(r'-A POSTROUTING -p tcp -m tcp '
                           r'-j CHECKSUM --checksum-fill')
        namespace = self.useFixture(net_helpers.NamespaceFixture()).name
        self.agent._ensure_datapath_checksum(namespace)
        iptables_mgr = iptables_manager.IptablesManager(
            use_ipv6=True, nat=False, namespace=namespace, external_lock=False)
        for rule in iptables_mgr.get_rules_for_table('mangle'):
            if regex.match(rule):
                return
        else:
            self.fail('Rule not found in "mangle" table, in namespace %s' %
                      namespace)

    def test_metadata_proxy_handler_idl(self):
        # This test relies on the configuration option metadata_workers=0
        proxy_sb_idl = self.agent._proxy.server._server._application.sb_idl
        agent_sb_idl = self.agent.sb_idl
        self.assertEqual(agent_sb_idl, proxy_sb_idl)
