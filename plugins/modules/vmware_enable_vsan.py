from __future__ import absolute_import, division, print_function
__metaclass__ = type

DOCUMENTATION = r'''
---
module: vmware_enable_vsan
short_description: Enable vSAN for a given cluster in vCenter
description:
  - This module allows you to enable vSAN for a given cluster in vCenter. It supports both OSA and ESA architectures and provides options for automatic vSAN configuration.
'''

import atexit
import requests
import ssl
import time
from pyVim import connect
from pyVim.connect import SmartConnect, Disconnect
from pyVmomi import vim, vmodl
from ansible.module_utils.vsanmgmtObjects import *
from ansible.module_utils.vsanapiutils import *

   
def wait_for_tasks(tasks, SI):
    property_collector = SI.content.propertyCollector
    task_list = [str(task) for task in tasks]
    obj_specs = [vmodl.query.PropertyCollector.ObjectSpec(obj=task)
                 for task in tasks]
    property_spec = vmodl.query.PropertyCollector.PropertySpec(type=vim.Task,
                                                               pathSet=[],
                                                               all=True)
    filter_spec = vmodl.query.PropertyCollector.FilterSpec()
    filter_spec.objectSet = obj_specs
    filter_spec.propSet = [property_spec]
    pcfilter = property_collector.CreateFilter(filter_spec, True)
    try:
        version, state = None, None
        while len(task_list):
            update = property_collector.WaitForUpdates(version)
            for filter_set in update.filterSet:
                for obj_set in filter_set.objectSet:
                    task = obj_set.obj
                    for change in obj_set.changeSet:
                        if change.name == 'info':
                            state = change.val.state
                        elif change.name == 'info.state':
                            state = change.val
                        else:
                            continue
                        if not str(task) in task_list:
                            continue
                        if state == vim.TaskInfo.State.success:
                            task_list.remove(str(task))
                        elif state == vim.TaskInfo.State.error:
                            raise task.info.error
            version = update.version
    finally:
        if pcfilter:
            pcfilter.Destroy()


def wait_for_vsan_task(vsan_task):
    while vsan_task.info.state in [vim.TaskInfo.State.running, vim.TaskInfo.State.queued]:
        time.sleep(2)
    if vsan_task.info.state == vim.TaskInfo.State.success:
        return vsan_task.info.result
    raise Exception(f"vSAN Task failed: {vsan_task.info.error.msg}")


def get_obj(content, vimtype, name):
    obj = None
    container = content.viewManager.CreateContainerView( content.rootFolder, vimtype, True)
    for c in container.view:
        if name:
            if c.name == name:
                obj = c
                break
        else:
            obj = c
            break
    container.Destroy()
    return obj


def configure_vsan():
    vsan_config = vim.vsan.cluster.ConfigInfo()
    vsan_config.enabled = True
    vsan_config.defaultConfig = vim.vsan.cluster.ConfigInfo.HostDefaultInfo()
    vsan_config.defaultConfig.autoClaimStorage = False
    return vsan_config


def main():


    module = AnsibleModule(
        argument_spec=dict(
            vcenter=dict(required=True, type='str'),
            user=dict(required=True, type='str'),
            passwd=dict(required=True, type='str', no_log=True),
            cluster=dict(required=True, type='str'),
            architecture=dict(required=False, type='str', choices=['osa', 'esa'], default='osa'),
        ),
        supports_check_mode=True,
    )

    architecture = module.params['architecture'].lower()

    if architecture == 'esa' and not HAS_VSAN_SDK:
        module.fail_json(msg="VMware vSAN Management SDK (vsanapiutils) is required for vSAN ESA")

    global SI
    context = None
    requests.packages.urllib3.disable_warnings()
    try:
        SI = connect.SmartConnect(host=module.params['vcenter'],
                                            user=module.params['user'],
                                            pwd=module.params['passwd'])

    except (requests.ConnectionError, ssl.SSLError):
        try:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            SI = connect.SmartConnect(host=module.params['vcenter'],
                                            user=module.params['user'],
                                            pwd=module.params['passwd'],
                                            sslContext=context)

        except Exception as e:
            raise Exception(e)
    except vim.fault.InvalidLogin:
        module.fail_json(msg='exception while connecting to vCenter, login failure, check username and password')


    if not SI:
        module.fail_json(msg='exception while connecting to vCenter, no Service Instance')

    atexit.register(connect.Disconnect, SI)
    content = SI.RetrieveContent()
    cluster = get_obj(content, [vim.ClusterComputeResource], module.params['cluster'])
    if not cluster:
        module.fail_json(msg="Cluster '" + module.params['cluster'] + "' not found")

    if architecture == 'osa':
        current_vsan = getattr(cluster.configurationEx, 'vsanConfigInfo', None)
        current_enabled = getattr(current_vsan, 'enabled', False) if current_vsan else False

        if current_enabled:
            module.exit_json(changed=False, result="vSAN OSA is already enabled on cluster " + module.params['cluster'])

        if module.check_mode:
            module.exit_json(changed=True, result="vSAN OSA would be enabled on cluster " + module.params['cluster'])

        cluster_config_spec = vim.cluster.ConfigSpecEx()
        cluster_config_spec.vsanConfig = configure_vsan()
        task = cluster.ReconfigureComputeResource_Task(cluster_config_spec, True)
        wait_for_tasks([task], SI)
        module.exit_json(changed=True, result="vSAN OSA is enabled on cluster " + module.params['cluster'])
    else:
        try:
            vc_mos = vsanapiutils.GetVsanVcMos(SI._stub, context=context, version='vim.version.version14')
        except Exception:
            vc_mos = vsanapiutils.GetVsanVcMos(SI._stub, context=context)
        vccs = vc_mos['vsan-cluster-config-system']

        current_config = vccs.GetConfigInfoEx(cluster=cluster)
        vsan_enabled = getattr(current_config, 'enabled', False)
        esa_config = getattr(current_config, 'esaConfig', None)

        if esa_config is None:
            vc_vsan_info = getattr(cluster.configurationEx, 'vsanConfigInfo', None)
            esa_config = getattr(vc_vsan_info, 'esaConfig', None)

        esa_enabled = bool(getattr(esa_config, 'enabled', False)) if esa_config else False

        if vsan_enabled and esa_config is not None and not esa_enabled:
            module.fail_json(
                msg="Cluster '" + module.params['cluster'] + "' already has vSAN OSA enabled. In-place conversion to ESA is unsupported."
            )

        if vsan_enabled and (esa_enabled or esa_config is None):
            module.exit_json(changed=False, result="vSAN ESA is already enabled on cluster " + module.params['cluster'])

        if module.check_mode:
            module.exit_json(changed=True, result="vSAN ESA would be enabled on cluster " + module.params['cluster'])

        esa_spec = vim.vsan.VsanEsaConfigInfo(
            enabled=True,
            hclDiskClaimEnabled=False
        )
        vsan_spec = vim.vsan.ReconfigSpec(
            modify=True,
            vsanClusterConfig=vim.vsan.cluster.ConfigInfo(
                enabled=True,
                esaConfig=esa_spec
            )
        )
        vsan_task = vccs.ReconfigureEx(cluster=cluster, spec=vsan_spec)
        wait_for_vsan_task(vsan_task)
        module.exit_json(changed=True, result="vSAN ESA is enabled on cluster " + module.params['cluster'])


from ansible.module_utils.basic import AnsibleModule

# Start program
if __name__ == "__main__":
    main()
