from __future__ import absolute_import, division, print_function
__metaclass__ = type

DOCUMENTATION = r'''
---
module: vmware_claim_vsan_disks
short_description: Auto claim vSAN disks for a given cluster in vCenter
description:
  - This module allows you to auto claim vSAN disks for a given cluster in vCenter. It supports both OSA and ESA architectures and provides options for automatic disk claiming.
'''

import atexit
import requests
import ssl
import time
from pyVim import connect
from pyVim.connect import SmartConnect
from pyVmomi import vim, vmodl, vsanapiutils

def connect_to_api(vchost, vc_user, vc_pwd):
    global service_instance
    global context
    context = None
    try:
        service_instance = SmartConnect(host=vchost, user=vc_user, pwd=vc_pwd)
    except (requests.ConnectionError, ssl.SSLError):
        try:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            service_instance = SmartConnect(host=vchost, user=vc_user, pwd=vc_pwd, sslContext=context)
        except Exception as e:
            raise Exception(e)
    return service_instance.RetrieveContent()

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

  
def wait_for_tasks(tasks):
    property_collector = service_instance.content.propertyCollector
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


def configure_vsan():
    vsan_config = vim.vsan.cluster.ConfigInfo()
    vsan_config.enabled = True
    vsan_config.defaultConfig = vim.vsan.cluster.ConfigInfo.HostDefaultInfo()
    vsan_config.defaultConfig.autoClaimStorage = False
    return vsan_config


def claim_esa_disks(cluster, vc_mos, auto_claim=True, check_mode=False):
    changed = False
    vccs = vc_mos['vsan-cluster-config-system']
    vsan_dms = vc_mos['vsan-disk-management-system']

    current_config = vccs.GetConfigInfoEx(cluster=cluster)
    vsan_enabled = getattr(current_config, 'enabled', False)
    esa_config = getattr(current_config, 'esaConfig', None)

    if esa_config is None:
        vc_vsan_info = getattr(cluster.configurationEx, 'vsanConfigInfo', None)
        esa_config = getattr(vc_vsan_info, 'esaConfig', None)

    esa_enabled = bool(getattr(esa_config, 'enabled', False)) if esa_config else False

    if not vsan_enabled or (esa_config is not None and not esa_enabled):
        raise Exception(f"Cluster '{cluster.name}' does not have vSAN ESA enabled.")

    if auto_claim:
        current_hcl_claim = getattr(esa_config, 'hclDiskClaimEnabled', False)
        if not current_hcl_claim:
            if not check_mode:
                vsan_spec = vim.vsan.ReconfigSpec(
                    modify=True,
                    vsanClusterConfig=vim.vsan.cluster.ConfigInfo(
                        enabled=True,
                        esaConfig=vim.vsan.VsanEsaConfigInfo(
                            enabled=True,
                            hclDiskClaimEnabled=True
                        )
                    )
                )
                vsan_task = vccs.ReconfigureEx(cluster=cluster, spec=vsan_spec)
                wait_for_vsan_task(vsan_task)
            changed = True
    else:
        for host in cluster.host:
            disks = host.configManager.vsanSystem.QueryDisksForVsan()
            eligible_nvme = [
                d.disk for d in disks
                if d.state == 'eligible' and (d.disk.capacity.block * d.disk.capacity.blockSize) / (1024 ** 3) > 300
            ]
            if eligible_nvme:
                if not check_mode:
                    pool_spec = vim.vsan.host.AddStoragePoolDisksSpec(
                        host=host,
                        disks=eligible_nvme
                    )
                    vsan_task = vsan_dms.AddStoragePoolDisks(specs=[pool_spec])
                    wait_for_vsan_task(vsan_task)
                changed = True

    return changed


def main():

    module = AnsibleModule(
        argument_spec=dict(
            vcenter=dict(required=True, type='str'),
            user=dict(required=True, type='str'),
            passwd=dict(required=True, type='str', no_log=True),
	        cluster=dict(required=True, type='str'),
            datastorename=dict(required=True, type='str'),
            architecture=dict(required=False, type='str', choices=['osa', 'esa'], default='osa'),
            auto_claim_storage=dict(required=False, type='bool', default=True),
        ),
        supports_check_mode=True,
    )

    architecture = module.params['architecture'].lower()
    auto_claim = module.params['auto_claim_storage']

    try:
        content = connect_to_api(module.params['vcenter'], module.params['user'],
                                 module.params['passwd'])
    except vim.fault.InvalidLogin:
        module.fail_json(msg='exception while connecting to vCenter, login failure, check username and password')
    except requests.exceptions.ConnectionError:
        module.fail_json(msg='exception while connecting to vCenter, check hostname, FQDN or IP')

    atexit.register(connect.Disconnect, service_instance)
    content = service_instance.RetrieveContent()
    cluster = get_obj(content, [vim.ClusterComputeResource], module.params['cluster'])
    if not cluster:
        module.fail_json(msg="Cluster '" + module.params['cluster'] + "' not found")

    try:
        vcMos = vsanapiutils.GetVsanVcMos(service_instance._stub, context=context, version='vim.version.version14')
    except Exception:
        vcMos = vsanapiutils.GetVsanVcMos(service_instance._stub, context=context)
    changed = False

    if architecture == 'osa':
        current_vsan = getattr(cluster.configurationEx, 'vsanConfigInfo', None)
        current_default = getattr(current_vsan, 'defaultConfig', None) if current_vsan else None
        current_auto_claim = getattr(current_default, 'autoClaimStorage', True) if current_default else True

        if current_auto_claim:
            if not module.check_mode:
                cluster_config_spec = vim.cluster.ConfigSpecEx()
                cluster_config_spec.vsanConfig = configure_vsan()
                task = cluster.ReconfigureComputeResource_Task(cluster_config_spec, True)
                wait_for_tasks([task])
            changed = True

        for host in cluster.host:
            diskmap = {host: {'cache': [], 'capacity': []}}
            disks = host.configManager.vsanSystem.QueryDisksForVsan()
            ssds = []
            for ssd in disks:
                if ssd.state == 'eligible' and (ssd.disk.capacity.block) / 2 / 1024 / 1024 > 300:
                    ssds.append(ssd.disk)
            if not ssds:
                continue
            smallerSize = min([ssd.capacity.block * ssd.capacity.blockSize for ssd in ssds])
            for ssd in ssds:
                size = ssd.capacity.block * ssd.capacity.blockSize
                if size == smallerSize:
                    diskmap[host]['cache'].append(ssd)
                else:
                    diskmap[host]['capacity'].append(ssd)

            for h, h_disks in diskmap.items():
                if h_disks['cache'] and h_disks['capacity']:
                    if not module.check_mode:
                        dm = vim.VimVsanHostDiskMappingCreationSpec(
                            cacheDisks=h_disks['cache'], capacityDisks=h_disks['capacity'],
                            creationType='allFlash',
                            host=h)
                        vsanVcDiskManagementSystem = vcMos['vsan-disk-management-system']
                        task = vsanVcDiskManagementSystem.InitializeDiskMappings(dm)
                        wait_for_tasks([task])
                    changed = True
    else:
        try:
            changed = claim_esa_disks(cluster, vcMos, auto_claim=auto_claim, check_mode=module.check_mode)
        except Exception as e:
            module.fail_json(msg=f"Failed to claim vSAN ESA disks: {str(e)}")

    cl = get_obj(content, [vim.ClusterComputeResource], module.params['cluster'])
    for ds in cl.datastore:
        if ds.summary.name.startswith("vsanDatastore") and ds.summary.name != module.params['datastorename']:
            if not module.check_mode:
                ds.RenameDatastore(module.params['datastorename'])
            changed = True

    msg = "vSAN Disks would be claimed" if (module.check_mode and changed) else "vSAN Disks claimed !!!!"
    module.exit_json(changed=changed, result=msg)

    return 0

from ansible.module_utils.basic import AnsibleModule

# Start program
if __name__ == "__main__":
    main()

