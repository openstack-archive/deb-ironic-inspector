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

"""Handling introspection data from the ramdisk."""

import base64
import copy
import datetime
import os

import eventlet
import json

from oslo_config import cfg
from oslo_utils import excutils

from ironic_inspector.common.i18n import _, _LE, _LI, _LW
from ironic_inspector.common import ironic as ir_utils
from ironic_inspector.common import swift
from ironic_inspector import firewall
from ironic_inspector import node_cache
from ironic_inspector.plugins import base as plugins_base
from ironic_inspector import rules
from ironic_inspector import utils

CONF = cfg.CONF

LOG = utils.getProcessingLogger(__name__)

_CREDENTIALS_WAIT_RETRIES = 10
_CREDENTIALS_WAIT_PERIOD = 3
_STORAGE_EXCLUDED_KEYS = {'logs'}
_UNPROCESSED_DATA_STORE_SUFFIX = 'UNPROCESSED'


def _store_logs(introspection_data, node_info):
    logs = introspection_data.get('logs')
    if not logs:
        LOG.warning(_LW('No logs were passed by the ramdisk'),
                    data=introspection_data, node_info=node_info)
        return

    if not CONF.processing.ramdisk_logs_dir:
        LOG.warning(_LW('Failed to store logs received from the ramdisk '
                        'because ramdisk_logs_dir configuration option '
                        'is not set'),
                    data=introspection_data, node_info=node_info)
        return

    fmt_args = {
        'uuid': node_info.uuid if node_info is not None else 'unknown',
        'mac': (utils.get_pxe_mac(introspection_data) or
                'unknown').replace(':', ''),
        'dt': datetime.datetime.utcnow(),
        'bmc': (utils.get_ipmi_address_from_data(introspection_data) or
                'unknown')
    }

    file_name = CONF.processing.ramdisk_logs_filename_format.format(**fmt_args)

    try:
        if not os.path.exists(CONF.processing.ramdisk_logs_dir):
            os.makedirs(CONF.processing.ramdisk_logs_dir)
        with open(os.path.join(CONF.processing.ramdisk_logs_dir, file_name),
                  'wb') as fp:
            fp.write(base64.b64decode(logs))
    except EnvironmentError:
        LOG.exception(_LE('Could not store the ramdisk logs'),
                      data=introspection_data, node_info=node_info)
    else:
        LOG.info(_LI('Ramdisk logs were stored in file %s'), file_name,
                 data=introspection_data, node_info=node_info)


def _find_node_info(introspection_data, failures):
    try:
        return node_cache.find_node(
            bmc_address=introspection_data.get('ipmi_address'),
            mac=utils.get_valid_macs(introspection_data))
    except utils.NotFoundInCacheError as exc:
        not_found_hook = plugins_base.node_not_found_hook_manager()
        if not_found_hook is None:
            failures.append(_('Look up error: %s') % exc)
            return
        # NOTE(sambetts): If not_found_hook is not none it means that we were
        # unable to find the node in the node cache and there is a node not
        # found hook defined so we should try to send the introspection data
        # to that hook to generate the node info before bubbling up the error.
        try:
            node_info = not_found_hook.driver(introspection_data)
            if node_info:
                return node_info
            failures.append(_("Node not found hook returned nothing"))
        except Exception as exc:
            failures.append(_("Node not found hook failed: %s") % exc)
    except utils.Error as exc:
        failures.append(_('Look up error: %s') % exc)


def _run_pre_hooks(introspection_data, failures):
    hooks = plugins_base.processing_hooks_manager()
    for hook_ext in hooks:
        # NOTE(dtantsur): catch exceptions, so that we have changes to update
        # node introspection status after look up
        try:
            hook_ext.obj.before_processing(introspection_data)
        except utils.Error as exc:
            LOG.error(_LE('Hook %(hook)s failed, delaying error report '
                          'until node look up: %(error)s'),
                      {'hook': hook_ext.name, 'error': exc},
                      data=introspection_data)
            failures.append('Preprocessing hook %(hook)s: %(error)s' %
                            {'hook': hook_ext.name, 'error': exc})
        except Exception as exc:
            LOG.exception(_LE('Hook %(hook)s failed, delaying error report '
                              'until node look up: %(error)s'),
                          {'hook': hook_ext.name, 'error': exc},
                          data=introspection_data)
            failures.append(_('Unexpected exception %(exc_class)s during '
                              'preprocessing in hook %(hook)s: %(error)s') %
                            {'hook': hook_ext.name,
                             'exc_class': exc.__class__.__name__,
                             'error': exc})


def _filter_data_excluded_keys(data):
    return {k: v for k, v in data.items()
            if k not in _STORAGE_EXCLUDED_KEYS}


def _store_data(node_info, data, suffix=None):
    if CONF.processing.store_data != 'swift':
        LOG.debug("Swift support is disabled, introspection data "
                  "won't be stored", node_info=node_info)
        return

    swift_object_name = swift.store_introspection_data(
        _filter_data_excluded_keys(data),
        node_info.uuid,
        suffix=suffix
    )
    LOG.info(_LI('Introspection data was stored in Swift in object '
                 '%s'), swift_object_name, node_info=node_info)
    if CONF.processing.store_data_location:
        node_info.patch([{'op': 'add', 'path': '/extra/%s' %
                          CONF.processing.store_data_location,
                          'value': swift_object_name}])


def _store_unprocessed_data(node_info, data):
    # runs in background
    try:
        _store_data(node_info, data,
                    suffix=_UNPROCESSED_DATA_STORE_SUFFIX)
    except Exception:
        LOG.exception(_LE('Encountered exception saving unprocessed '
                          'introspection data'), node_info=node_info,
                      data=data)


def _get_unprocessed_data(uuid):
    if CONF.processing.store_data == 'swift':
        LOG.debug('Fetching unprocessed introspection data from '
                  'Swift for %s', uuid)
        return json.loads(
            swift.get_introspection_data(
                uuid,
                suffix=_UNPROCESSED_DATA_STORE_SUFFIX
            )
        )
    else:
        raise utils.Error(_('Swift support is disabled'), code=400)


def process(introspection_data):
    """Process data from the ramdisk.

    This function heavily relies on the hooks to do the actual data processing.
    """
    unprocessed_data = copy.deepcopy(introspection_data)
    failures = []
    _run_pre_hooks(introspection_data, failures)
    node_info = _find_node_info(introspection_data, failures)
    if node_info:
        # Locking is already done in find_node() but may be not done in a
        # node_not_found hook
        node_info.acquire_lock()

    if failures or node_info is None:
        msg = _('The following failures happened during running '
                'pre-processing hooks:\n%s') % '\n'.join(failures)
        if node_info is not None:
            node_info.finished(error='\n'.join(failures))
        _store_logs(introspection_data, node_info)
        raise utils.Error(msg, node_info=node_info, data=introspection_data)

    LOG.info(_LI('Matching node is %s'), node_info.uuid,
             node_info=node_info, data=introspection_data)

    if node_info.finished_at is not None:
        # race condition or introspection canceled
        raise utils.Error(_('Node processing already finished with '
                            'error: %s') % node_info.error,
                          node_info=node_info, code=400)

    # Note(mkovacik): store data now when we're sure that a background
    # thread won't race with other process() or introspect.abort()
    # call
    utils.executor().submit(_store_unprocessed_data, node_info,
                            unprocessed_data)

    try:
        node = node_info.node()
    except ir_utils.NotFound as exc:
        with excutils.save_and_reraise_exception():
            node_info.finished(error=str(exc))
            _store_logs(introspection_data, node_info)

    try:
        result = _process_node(node, introspection_data, node_info)
    except utils.Error as exc:
        node_info.finished(error=str(exc))
        with excutils.save_and_reraise_exception():
            _store_logs(introspection_data, node_info)
    except Exception as exc:
        LOG.exception(_LE('Unexpected exception during processing'))
        msg = _('Unexpected exception %(exc_class)s during processing: '
                '%(error)s') % {'exc_class': exc.__class__.__name__,
                                'error': exc}
        node_info.finished(error=msg)
        _store_logs(introspection_data, node_info)
        raise utils.Error(msg, node_info=node_info, data=introspection_data,
                          code=500)

    if CONF.processing.always_store_ramdisk_logs:
        _store_logs(introspection_data, node_info)
    return result


def _run_post_hooks(node_info, introspection_data):
    hooks = plugins_base.processing_hooks_manager()

    for hook_ext in hooks:
        hook_ext.obj.before_update(introspection_data, node_info)


def _process_node(node, introspection_data, node_info):
    # NOTE(dtantsur): repeat the check in case something changed
    ir_utils.check_provision_state(node)

    node_info.create_ports(introspection_data.get('macs') or ())

    _run_post_hooks(node_info, introspection_data)
    _store_data(node_info, introspection_data)

    ironic = ir_utils.get_client()
    firewall.update_filters(ironic)

    node_info.invalidate_cache()
    rules.apply(node_info, introspection_data)

    resp = {'uuid': node.uuid}

    if node_info.options.get('new_ipmi_credentials'):
        new_username, new_password = (
            node_info.options.get('new_ipmi_credentials'))
        utils.executor().submit(_finish_set_ipmi_credentials,
                                ironic, node, node_info, introspection_data,
                                new_username, new_password)
        resp['ipmi_setup_credentials'] = True
        resp['ipmi_username'] = new_username
        resp['ipmi_password'] = new_password
    else:
        utils.executor().submit(_finish, ironic, node_info, introspection_data,
                                power_off=CONF.processing.power_off)

    return resp


def _finish_set_ipmi_credentials(ironic, node, node_info, introspection_data,
                                 new_username, new_password):
    patch = [{'op': 'add', 'path': '/driver_info/ipmi_username',
              'value': new_username},
             {'op': 'add', 'path': '/driver_info/ipmi_password',
              'value': new_password}]
    new_ipmi_address = utils.get_ipmi_address_from_data(introspection_data)
    if not ir_utils.get_ipmi_address(node) and new_ipmi_address:
        patch.append({'op': 'add', 'path': '/driver_info/ipmi_address',
                      'value': new_ipmi_address})
    node_info.patch(patch)

    for attempt in range(_CREDENTIALS_WAIT_RETRIES):
        try:
            # We use this call because it requires valid credentials.
            # We don't care about boot device, obviously.
            ironic.node.get_boot_device(node_info.uuid)
        except Exception as exc:
            LOG.info(_LI('Waiting for credentials update, attempt %(attempt)d '
                         'current error is %(exc)s') %
                     {'attempt': attempt, 'exc': exc},
                     node_info=node_info, data=introspection_data)
            eventlet.greenthread.sleep(_CREDENTIALS_WAIT_PERIOD)
        else:
            _finish(ironic, node_info, introspection_data)
            return

    msg = (_('Failed to validate updated IPMI credentials for node '
             '%s, node might require maintenance') % node_info.uuid)
    node_info.finished(error=msg)
    raise utils.Error(msg, node_info=node_info, data=introspection_data)


def _finish(ironic, node_info, introspection_data, power_off=True):
    if power_off:
        LOG.debug('Forcing power off of node %s', node_info.uuid)
        try:
            ironic.node.set_power_state(node_info.uuid, 'off')
        except Exception as exc:
            if node_info.node().provision_state == 'enroll':
                LOG.info(_LI("Failed to power off the node in"
                             "'enroll' state, ignoring; error was "
                             "%s") % exc, node_info=node_info,
                         data=introspection_data)
            else:
                msg = (_('Failed to power off node %(node)s, check '
                         'its power management configuration: '
                         '%(exc)s') % {'node': node_info.uuid, 'exc':
                                       exc})
                node_info.finished(error=msg)
                raise utils.Error(msg, node_info=node_info,
                                  data=introspection_data)
        LOG.info(_LI('Node powered-off'), node_info=node_info,
                 data=introspection_data)

    node_info.finished()
    LOG.info(_LI('Introspection finished successfully'),
             node_info=node_info, data=introspection_data)


def reapply(node_ident):
    """Re-apply introspection steps.

    Re-apply preprocessing, postprocessing and introspection rules on
    stored data.

    :param node_ident: node UUID or name
    :raises: utils.Error

    """

    LOG.debug('Processing re-apply introspection request for node '
              'UUID: %s', node_ident)
    node_info = node_cache.get_node(node_ident, locked=False)
    if not node_info.acquire_lock(blocking=False):
        # Note (mkovacik): it should be sufficient to check data
        # presence & locking. If either introspection didn't start
        # yet, was in waiting state or didn't finish yet, either data
        # won't be available or locking would fail
        raise utils.Error(_('Node locked, please, try again later'),
                          node_info=node_info, code=409)

    utils.executor().submit(_reapply, node_info)


def _reapply(node_info):
    # runs in background
    try:
        introspection_data = _get_unprocessed_data(node_info.uuid)
    except Exception:
        LOG.exception(_LE('Encountered exception while fetching '
                          'stored introspection data'),
                      node_info=node_info)
        node_info.release_lock()
        return

    failures = []
    _run_pre_hooks(introspection_data, failures)
    if failures:
        LOG.error(_LE('Pre-processing failures detected reapplying '
                      'introspection on stored data:\n%s'),
                  '\n'.join(failures), node_info=node_info)
        node_info.finished(error='\n'.join(failures))
        return

    try:
        ironic = ir_utils.get_client()
        node_info.create_ports(introspection_data.get('macs') or ())
        _run_post_hooks(node_info, introspection_data)
        _store_data(node_info, introspection_data)
        node_info.invalidate_cache()
        rules.apply(node_info, introspection_data)
        _finish(ironic, node_info, introspection_data,
                power_off=False)
    except Exception as exc:
        LOG.exception(_LE('Encountered exception reapplying '
                          'introspection on stored data'),
                      node_info=node_info,
                      data=introspection_data)
        node_info.finished(error=str(exc))
    else:
        LOG.info(_LI('Successfully reapplied introspection on stored '
                     'data'), node_info=node_info, data=introspection_data)
