#!/usr/bin/env python
# Copyright 2017 OVH SAS
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

import sys
from unittest import mock

from oslo_config import cfg

from neutron.agent.common import ovs_lib
from neutron.agent.common import polling
from neutron.agent.l2.extensions import qos as qos_extension
from neutron.common import config
from neutron.tests.common.agents import ovs_agent


def monkeypatch_qos():
    mock.patch.object(ovs_lib.OVSBridge, 'clear_bandwidth_qos').start()
    if "qos" in cfg.CONF.service_plugins:
        mock.patch.object(qos_extension.QosAgentExtension,
                          '_process_reset_port').start()


def monkeypatch_event_filtering():
    def filter_bridge_names(br_names):
        if 'trunk' in cfg.CONF.service_plugins:
            return []
        return br_names

    polling.filter_bridge_names = filter_bridge_names


def main():
    # TODO(slaweq): this monkepatch will not be necessary when
    # https://review.opendev.org/#/c/506722/ will be merged and ovsdb-server
    # ovs-vswitchd processes for each test will be isolated in separate
    # namespace
    config.register_common_config_options()
    monkeypatch_qos()
    monkeypatch_event_filtering()
    ovs_agent.main()


if __name__ == "__main__":
    sys.exit(main())
