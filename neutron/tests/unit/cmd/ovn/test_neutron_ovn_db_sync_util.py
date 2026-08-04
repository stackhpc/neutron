# Copyright 2020 Canonical Ltd
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

import tempfile
from unittest import mock

from oslo_config import cfg
from stevedore import extension

from neutron.cmd.ovn import neutron_ovn_db_sync_util as util
from neutron.conf.plugins.ml2.drivers.ovn import ovn_db_sync as sync_conf
from neutron.tests import base
from neutron_lib.ovn import db_sync as db_sync_base


class TestNeutronOVNDBSyncUtil(base.BaseTestCase):

    def test_setup_conf(self):
        # the code under test will fail because of the cfg.conf already being
        # initialized by the BaseTestCase setUp method. Reset.
        cfg.CONF.reset()
        with mock.patch.object(
                sync_conf, 'register_sync_plugins_additional_cli_opts') as reg:
            util.setup_conf()
            reg.assert_called_once_with(cfg.CONF)
        # The sync tool will fail if these config options are at their default
        # value. Validate that the setup code overrides them. LP: #1882020
        self.assertFalse(cfg.CONF.notify_nova_on_port_status_changes)
        self.assertFalse(cfg.CONF.notify_nova_on_port_data_changes)

    def test_prepare_additional_configuration(self):
        conf = mock.Mock()
        plugin = mock.Mock()
        ext = mock.Mock(plugin=plugin)
        mgr = [ext]

        util.prepare_additional_configuration(conf, mgr)

        plugin.prepare_additional_configuration.assert_called_once_with(conf)

    def test_synchronize_ovn_dbs_passes_plugin_conf(self):
        sync_obj = mock.Mock()
        sync_plugin = mock.Mock(return_value=sync_obj)
        ext = extension.Extension('test_sync', None, sync_plugin, None)
        mgr = mock.Mock()
        mgr.__iter__ = mock.Mock(return_value=iter([ext]))

        plugin_configs = {'test_sync': mock.Mock()}
        util.synchronize_ovn_dbs(
            mgr, mock.Mock(), mock.Mock(), 'repair', plugin_configs)

        sync_plugin.assert_called_once_with(
            mock.ANY, mock.ANY, 'repair',
            plugin_conf=plugin_configs['test_sync'])
        sync_obj.do_sync.assert_called_once()

    def test_synchronize_ovn_dbs_without_plugin_conf(self):
        sync_obj = mock.Mock()
        sync_plugin = mock.Mock(return_value=sync_obj)
        ext = extension.Extension('test_sync', None, sync_plugin, None)
        mgr = mock.Mock()
        mgr.__iter__ = mock.Mock(return_value=iter([ext]))

        util.synchronize_ovn_dbs(mgr, mock.Mock(), mock.Mock(), 'repair')

        sync_plugin.assert_called_once_with(
            mock.ANY, mock.ANY, 'repair', plugin_conf=None)
        sync_obj.do_sync.assert_called_once()

    @mock.patch.object(util, 'LOG')
    def test_synchronize_ovn_dbs_legacy_plugin_without_plugin_conf(self,
                                                                   mock_log):
        sync_obj = mock.Mock()

        class LegacySynchronizer(db_sync_base.BaseOvnDbSynchronizer):
            def __init__(self, core_plugin, ovn_driver, mode,
                         is_maintenance=False):
                pass

            def do_sync(self):
                pass

        ext = extension.Extension(
            'legacy_sync', None, LegacySynchronizer, None)
        mgr = mock.Mock()
        mgr.__iter__ = mock.Mock(return_value=iter([ext]))
        plugin_configs = {'legacy_sync': mock.Mock()}

        with mock.patch.object(
                LegacySynchronizer, 'do_sync', sync_obj.do_sync):
            util.synchronize_ovn_dbs(
                mgr, mock.Mock(), mock.Mock(), 'repair', plugin_configs)

        mock_log.warning.assert_any_call(
            'Support for OVN DB sync plugins that do not accept the '
            'plugin_conf argument in __init__ is deprecated and will be '
            'removed in the 2027.2 (J) release. Sync plugin %(name)s '
            '(%(class)s) should be updated to accept plugin_conf and '
            'forward it to BaseOvnDbSynchronizer.__init__ for compatibility '
            'with plugin-specific configuration files.',
            {'name': 'legacy_sync', 'class': 'LegacySynchronizer'})
        mock_log.warning.assert_any_call(
            'Plugin-specific configuration for sync plugin %(name)s '
            'will not be loaded because the plugin does not support '
            'plugin_conf. This backward-compatible behavior is '
            'deprecated and will be removed in the 2027.2 (J) release.',
            {'name': 'legacy_sync'})
        sync_obj.do_sync.assert_called_once()

    def test_load_db_migration_drivers_calls_callable_plugin(self):
        migration_fn = mock.Mock()
        fake_ext = mock.Mock()
        fake_ext.name = 'neutron'
        fake_ext.plugin = migration_fn

        with mock.patch(
                'stevedore.enabled.EnabledExtensionManager') as mock_mgr_cls:
            mock_mgr = mock.Mock()
            mock_mgr_cls.return_value = mock_mgr
            mock_mgr.__iter__ = mock.Mock(return_value=iter([fake_ext]))

            util.load_db_migration_drivers()

        mock_mgr_cls.assert_called_once_with(
            'neutron.ovn.db_migration',
            check_func=mock.ANY,
            invoke_on_load=False)

        # Verify the check_func accepts a callable (non-class) plugin without
        # raising TypeError from issubclass()
        check_func = mock_mgr_cls.call_args[1]['check_func']
        self.assertTrue(check_func(fake_ext))

    def test_load_db_migration_drivers_filters_by_name(self):
        fake_ext_a = mock.Mock()
        fake_ext_a.name = 'neutron'
        fake_ext_b = mock.Mock()
        fake_ext_b.name = 'other'

        with mock.patch(
                'stevedore.enabled.EnabledExtensionManager') as mock_mgr_cls:
            util.load_db_migration_drivers(driver_name='neutron')

        check_func = mock_mgr_cls.call_args[1]['check_func']
        self.assertTrue(check_func(fake_ext_a))
        self.assertFalse(check_func(fake_ext_b))

    def test_migrate_neutron_dbs_to_ovn_calls_plugin(self):
        migration_fn = mock.Mock()
        fake_ext = mock.Mock()
        fake_ext.name = 'neutron'
        fake_ext.plugin = migration_fn

        util.migrate_neutron_dbs_to_ovn(fake_ext)

        migration_fn.assert_called_once_with()

    def test_synchronize_ovn_dbs_respects_sync_order(self):
        call_order = []

        class PluginOrder0:
            _sync_order = 0

            def __init__(self, *args):
                pass

            def do_sync(self):
                call_order.append('order0')

        class PluginOrder1:
            _sync_order = 1

            def __init__(self, *args):
                pass

            def do_sync(self):
                call_order.append('order1')

        class PluginNoOrder:
            def __init__(self, *args):
                pass

            def do_sync(self):
                call_order.append('no_order')

        ext_order1 = mock.Mock()
        ext_order1.name = 'late'
        ext_order1.plugin = PluginOrder1

        ext_order0 = mock.Mock()
        ext_order0.name = 'early'
        ext_order0.plugin = PluginOrder0

        ext_no_order = mock.Mock()
        ext_no_order.name = 'default'
        ext_no_order.plugin = PluginNoOrder

        mgr = [ext_order1, ext_no_order, ext_order0]
        util.synchronize_ovn_dbs(mgr, mock.ANY, mock.ANY, 'log')
        self.assertEqual(['no_order', 'order0', 'order1'], call_order)


class TestOvnDbSyncConf(base.BaseTestCase):

    _testplugin_config_file_opt = cfg.ListOpt(
        'testplugin_config_file', default=[])

    def test_register_sync_plugins_additional_cli_opts(self):
        conf = cfg.ConfigOpts()

        class TestSynchronizer(db_sync_base.BaseOvnDbSynchronizer):
            @classmethod
            def register_additional_cli_opts(cls, conf):
                conf.register_cli_opts(
                    [TestOvnDbSyncConf._testplugin_config_file_opt])

            def do_sync(self):
                pass

        ext = extension.Extension('test_sync', None, TestSynchronizer, None)
        with mock.patch.object(sync_conf, '_load_sync_entrypoints') as load:
            load.return_value = [ext]
            sync_conf.register_sync_plugins_additional_cli_opts(conf)

        conf(['--testplugin_config_file', '/tmp/test.conf'])
        self.assertEqual(['/tmp/test.conf'], conf.testplugin_config_file)

    def test_load_sync_plugins_configuration(self):
        with tempfile.NamedTemporaryFile(mode='w', delete=False) as f:
            f.write('[testgroup]\n')
            f.write('test_value = plugin_value\n')
            config_path = f.name

        conf = cfg.ConfigOpts()
        conf.register_cli_opts([self._testplugin_config_file_opt])
        conf(['--testplugin_config_file', config_path])

        class TestSynchronizer(db_sync_base.BaseOvnDbSynchronizer):
            @classmethod
            def register_plugin_config_opts(cls, conf):
                conf.register_opts(
                    [cfg.StrOpt('test_value', default='default')],
                    group='testgroup')

            @classmethod
            def get_plugin_config_files(cls, global_conf):
                return global_conf.testplugin_config_file

            def do_sync(self):
                pass

        ext = extension.Extension('test_sync', None, TestSynchronizer, None)
        mgr = mock.Mock()
        mgr.__iter__ = mock.Mock(return_value=iter([ext]))

        plugin_configs = sync_conf.load_sync_plugins_configuration(conf, mgr)
        self.assertIn('test_sync', plugin_configs)
        self.assertEqual(
            'plugin_value', plugin_configs['test_sync'].testgroup.test_value)
