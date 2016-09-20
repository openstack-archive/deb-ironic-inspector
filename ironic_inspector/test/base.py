# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.


import fixtures
import futurist
import mock
from oslo_concurrency import lockutils
from oslo_config import cfg
from oslo_config import fixture as config_fixture
from oslo_log import log
from oslo_utils import units
from oslo_utils import uuidutils

from ironic_inspector.common import i18n
# Import configuration options
from ironic_inspector import conf  # noqa
from ironic_inspector import db
from ironic_inspector import node_cache
from ironic_inspector.plugins import base as plugins_base
from ironic_inspector import utils

CONF = cfg.CONF


class BaseTest(fixtures.TestWithFixtures):

    IS_FUNCTIONAL = False

    def setUp(self):
        super(BaseTest, self).setUp()
        if not self.IS_FUNCTIONAL:
            self.init_test_conf()
        self.session = db.get_session()
        engine = db.get_engine()
        db.Base.metadata.create_all(engine)
        engine.connect()
        self.addCleanup(db.get_engine().dispose)
        plugins_base._HOOKS_MGR = None
        node_cache._SEMAPHORES = lockutils.Semaphores()
        for name in ('_', '_LI', '_LW', '_LE', '_LC'):
            patch = mock.patch.object(i18n, name, lambda s: s)
            patch.start()
            # 'p=patch' magic is due to how closures work
            self.addCleanup(lambda p=patch: p.stop())
        utils._EXECUTOR = futurist.SynchronousExecutor(green=True)

    def init_test_conf(self):
        CONF.reset()
        log.register_options(CONF)
        self.cfg = self.useFixture(config_fixture.Config(CONF))
        self.cfg.set_default('connection', "sqlite:///", group='database')
        self.cfg.set_default('slave_connection', False, group='database')
        self.cfg.set_default('max_retries', 10, group='database')

    def assertPatchEqual(self, expected, actual):
        expected = sorted(expected, key=lambda p: p['path'])
        actual = sorted(actual, key=lambda p: p['path'])
        self.assertEqual(expected, actual)

    def assertCalledWithPatch(self, expected, mock_call):
        def _get_patch_param(call):
            try:
                if isinstance(call[0][1], list):
                    return call[0][1]
            except IndexError:
                pass
            return call[0][0]

        actual = sum(map(_get_patch_param, mock_call.call_args_list), [])
        self.assertPatchEqual(actual, expected)


class InventoryTest(BaseTest):
    def setUp(self):
        super(InventoryTest, self).setUp()
        # Prepare some realistic inventory
        # https://github.com/openstack/ironic-inspector/blob/master/HTTP-API.rst  # noqa
        self.bmc_address = '1.2.3.4'
        self.macs = ['11:22:33:44:55:66', '66:55:44:33:22:11']
        self.ips = ['1.2.1.2', '1.2.1.1']
        self.inactive_mac = '12:12:21:12:21:12'
        self.pxe_mac = self.macs[0]
        self.all_macs = self.macs + [self.inactive_mac]
        self.pxe_iface_name = 'eth1'
        self.data = {
            'boot_interface': '01-' + self.pxe_mac.replace(':', '-'),
            'inventory': {
                'interfaces': [
                    {'name': 'eth1', 'mac_address': self.macs[0],
                     'ipv4_address': self.ips[0]},
                    {'name': 'eth2', 'mac_address': self.inactive_mac},
                    {'name': 'eth3', 'mac_address': self.macs[1],
                     'ipv4_address': self.ips[1]},
                ],
                'disks': [
                    {'name': '/dev/sda', 'model': 'Big Data Disk',
                     'size': 1000 * units.Gi},
                    {'name': '/dev/sdb', 'model': 'Small OS Disk',
                     'size': 20 * units.Gi},
                ],
                'cpu': {
                    'count': 4,
                    'architecture': 'x86_64'
                },
                'memory': {
                    'physical_mb': 12288
                },
                'bmc_address': self.bmc_address
            },
            'root_disk': {'name': '/dev/sda', 'model': 'Big Data Disk',
                          'size': 1000 * units.Gi,
                          'wwn': None},
        }
        self.inventory = self.data['inventory']
        self.all_interfaces = {
            'eth1': {'mac': self.macs[0], 'ip': self.ips[0]},
            'eth2': {'mac': self.inactive_mac, 'ip': None},
            'eth3': {'mac': self.macs[1], 'ip': self.ips[1]}
        }
        self.active_interfaces = {
            'eth1': {'mac': self.macs[0], 'ip': self.ips[0]},
            'eth3': {'mac': self.macs[1], 'ip': self.ips[1]}
        }
        self.pxe_interfaces = {
            self.pxe_iface_name: self.all_interfaces[self.pxe_iface_name]
        }


class NodeTest(InventoryTest):
    def setUp(self):
        super(NodeTest, self).setUp()
        self.uuid = uuidutils.generate_uuid()
        fake_node = {
            'driver': 'pxe_ipmitool',
            'driver_info': {'ipmi_address': self.bmc_address},
            'properties': {'cpu_arch': 'i386', 'local_gb': 40},
            'uuid': self.uuid,
            'power_state': 'power on',
            'provision_state': 'inspecting',
            'extra': {},
            'instance_uuid': None,
            'maintenance': False
        }
        mock_to_dict = mock.Mock(return_value=fake_node)

        self.node = mock.Mock(**fake_node)
        self.node.to_dict = mock_to_dict

        self.ports = []
        self.node_info = node_cache.NodeInfo(uuid=self.uuid, started_at=0,
                                             node=self.node, ports=self.ports)
        self.node_info.node = mock.Mock(return_value=self.node)
