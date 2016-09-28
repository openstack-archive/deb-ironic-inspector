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

"""Gather and distinguish PCI devices from inventory."""

import collections
import json

from oslo_config import cfg

from ironic_inspector.common.i18n import _, _LI, _LW, _LE
from ironic_inspector.plugins import base
from ironic_inspector import utils

PCI_DEVICES_OPTS = [
    cfg.MultiStrOpt('alias',
                    default=[],
                    help=_('An alias for PCI device identified by '
                           '\'vendor_id\' and \'product_id\' fields. Format: '
                           '{"vendor_id": "1234", "product_id": "5678", '
                           '"name": "pci_dev1"}')),
]


def list_opts():
    return [
        ('pci_devices', PCI_DEVICES_OPTS)
    ]

CONF = cfg.CONF
CONF.register_opts(PCI_DEVICES_OPTS, group='pci_devices')

LOG = utils.getProcessingLogger(__name__)


def _parse_pci_alias_entry():
    parsed_pci_devices = []
    for pci_alias_entry in CONF.pci_devices.alias:
        try:
            parsed_entry = json.loads(pci_alias_entry)
            if set(parsed_entry) != {'vendor_id', 'product_id', 'name'}:
                raise KeyError(_LE("The 'alias' entry should contain "
                                   "exactly 'vendor_id', 'product_id' and "
                                   "'name' keys"))
            parsed_pci_devices.append(parsed_entry)
        except (ValueError, KeyError) as ex:
            LOG.error(_LE("Error parsing 'alias' option: %s"), ex)
    return {(dev['vendor_id'], dev['product_id']): dev['name']
            for dev in parsed_pci_devices}


class PciDevicesHook(base.ProcessingHook):
    """Processing hook for counting and distinguishing various PCI devices.

        That information can be later used by nova for node scheduling.
    """
    aliases = _parse_pci_alias_entry()

    def _found_pci_devices_count(self, found_pci_devices):
        return collections.Counter([(dev['vendor_id'], dev['product_id'])
                                    for dev in found_pci_devices
                                    if (dev['vendor_id'], dev['product_id'])
                                    in self.aliases])

    def before_update(self, introspection_data, node_info, **kwargs):
        if 'pci_devices' not in introspection_data:
            if CONF.pci_devices.alias:
                LOG.warning(_LW('No PCI devices information was received from '
                            'the ramdisk.'))
            return
        alias_count = {self.aliases[id_pair]: count for id_pair, count in
                       self._found_pci_devices_count(
                           introspection_data['pci_devices']).items()}
        if alias_count:
            node_info.update_capabilities(**alias_count)
            LOG.info(_LI('Found the following PCI devices: %s'),
                     alias_count)
