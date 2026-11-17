from __future__ import absolute_import, division, print_function
__metaclass__ = type

DOCUMENTATION = r'''
---
module: vmware_migrate_vmk_vmnic
short_description: Migrate a VMK interface from VSS to VDS
description:
    - Migrate a VMK interface from VSS to VDS
requirements:
    - "python >= 3.9"
    - PyVmomi
options:
    esxi_hostname:
        description:
            - ESXi hostname to be managed
        required: True
        type: str
    device:
        description:
            - VMK interface name
        required: True
        type: str
    current_switch_name:
        description:
            - Switch VMK interface is currently on
        required: True
        type: str
    current_portgroup_name:
        description:
            - Portgroup name VMK interface is currently on
        required: True
        type: str
    migrate_switch_name:
        description:
            - Switch name to migrate VMK interface to
        required: True
        type: str
    migrate_portgroup_name:
        description:
            - Portgroup name to migrate VMK interface to
        required: True
        type: str
    migrate_vlan_id:
        description:
            - VLAN to use for the VMK interface when migrating from VDS to VSS
            - Will be ignored when migrating from VSS to VDS
        type: int
'''

EXAMPLES = r'''
- name: Migrate Management vmk
  vmware_migrate_vmk:
    hostname: "{{ vcenter_hostname }}"
    username: "{{ vcenter_username }}"
    password: "{{ vcenter_password }}"
    esxi_hostname: "{{ esxi_hostname }}"
    device: vmk1
    current_switch_name: temp_vswitch
    current_portgroup_name: esx-mgmt
    migrate_switch_name: dvSwitch
    migrate_portgroup_name: Management
  delegate_to: localhost
'''
try:
    from pyVmomi import vim, vmodl
    HAS_PYVMOMI = True
except ImportError:
    HAS_PYVMOMI = False

from ansible.module_utils.basic import AnsibleModule
from ansible_collections.community.vmware.plugins.module_utils.vmware import (
    vmware_argument_spec, find_dvs_by_name, find_hostsystem_by_name,
    PyVmomi, find_dvspg_by_name)


class VMwareMigrateVmkVmnic(PyVmomi):

    def __init__(self, module):
        super(VMwareMigrateVmkVmnic, self).__init__(module)

        self.desired_vmnics = {}
        self.migrate_switch_name = self.module.params['migrate_switch_name']
        self.migrate_portgroup_name = self.module.params['migrate_portgroup_name']
        self.migrate_vlan_id = self.module.params['migrate_vlan_id']
        self.device = self.module.params['device']
        self.esxi_hostname = self.module.params['esxi_hostname']
        self.current_portgroup_name = self.module.params['current_portgroup_name']
        self.current_switch_name = self.module.params['current_switch_name']
        self.vmnics = self.module.params['vmnics']
        self.lag_uplinks = self.module.params['lag_uplinks']
        self.dv_switch = find_dvs_by_name(self.content, self.migrate_switch_name)
        if self.dv_switch is None:
            self.module.fail_json(msg="A distributed virtual switch %s "
                                      "does not exist" % self.migrate_switch_name)
        self.lags = {}
        for lag in self.dv_switch.config.lacpGroupConfig:
            self.lags[lag.name] = lag

        for lag_uplink in self.lag_uplinks:
            if lag_uplink['lag'] not in self.lags:
                self.module.fail_json(msg="LAG %s not found" % lag_uplink['lag'])

        self.host_system = find_hostsystem_by_name(self.content, self.esxi_hostname)
        self.uplink_portgroup = self.dv_switch.config.uplinkPortgroup[0]

    def process_state(self):
        try:
            vmk_migration_states = {
                'migrate_vss_vds': self.state_migrate_vss_vds,
                'migrate_vds_vss': self.state_migrate_vds_vss,
                'migrated': self.state_exit_unchanged
            }

            vmk_migration_states[self.check_vmk_current_state()]()

        except vmodl.RuntimeFault as runtime_fault:
            self.module.fail_json(msg=runtime_fault.msg)
        except vmodl.MethodFault as method_fault:
            self.module.fail_json(msg=method_fault.msg)
        except Exception as e:
            self.module.fail_json(msg=str(e))

    def state_exit_unchanged(self):
        self.module.exit_json(changed=False)

    def create_host_vnic_config_vds_vss(self):
        host_vnic_config = vim.host.VirtualNic.Config()
        host_vnic_config.spec = vim.host.VirtualNic.Specification()
        host_vnic_config.changeOperation = "edit"
        host_vnic_config.device = self.device
        host_vnic_config.spec.portgroup = self.migrate_portgroup_name
        return host_vnic_config

    def create_port_group_config_vds_vss(self):
        port_group_config = vim.host.PortGroup.Config()
        port_group_config.spec = vim.host.PortGroup.Specification()
        port_group_config.changeOperation = "add"
        port_group_config.spec.name = self.migrate_portgroup_name
        port_group_config.spec.vlanId = self.migrate_vlan_id if self.migrate_vlan_id is not None else 0
        port_group_config.spec.vswitchName = self.migrate_switch_name
        port_group_config.spec.policy = vim.host.NetworkPolicy()
        return port_group_config

    def state_migrate_vds_vss(self):
        if self.module.check_mode:
            self.module.exit_json(changed=True)
        host_network_system = self.host_system.configManager.networkSystem
        config = vim.host.NetworkConfig()
        config.portgroup = [self.create_port_group_config_vds_vss()]
        host_network_system.UpdateNetworkConfig(config, "modify")
        config = vim.host.NetworkConfig()
        config.vnic = [self.create_host_vnic_config_vds_vss()]
        host_network_system.UpdateNetworkConfig(config, "modify")
        self.module.exit_json(changed=True)

    def create_host_vnic_config(self, dv_switch_uuid, portgroup_key):
        host_vnic_config = vim.host.VirtualNic.Config()
        host_vnic_config.spec = vim.host.VirtualNic.Specification()

        host_vnic_config.changeOperation = "edit"
        host_vnic_config.device = self.device
        host_vnic_config.portgroup = ""
        host_vnic_config.spec.distributedVirtualPort = vim.dvs.PortConnection()
        host_vnic_config.spec.distributedVirtualPort.switchUuid = dv_switch_uuid
        host_vnic_config.spec.distributedVirtualPort.portgroupKey = portgroup_key

        return host_vnic_config

    def create_port_group_config(self):
        port_group_config = vim.host.PortGroup.Config()
        port_group_config.spec = vim.host.PortGroup.Specification()

        port_group_config.changeOperation = "remove"
        port_group_config.spec.name = self.current_portgroup_name
        port_group_config.spec.vlanId = -1
        port_group_config.spec.vswitchName = self.current_switch_name
        port_group_config.spec.policy = vim.host.NetworkPolicy()

        return port_group_config

    def create_proxy_switch_config(self):
        config_proxySwitch_0 = vim.host.HostProxySwitch.Config()
        config_proxySwitch_0.uuid = self.dv_switch.uuid
        config_proxySwitch_0.changeOperation = 'edit'
        config_proxySwitch_0.spec = vim.host.HostProxySwitch.Specification()
        config_proxySwitch_0.spec.backing = vim.dvs.HostMember.PnicBacking()

        pnicSpecs = []
        for vmnic, uplinkPortKey in self.desired_vmnics.items():
            config_proxySwitch_0_spec_backing_pnicSpec = vim.dvs.HostMember.PnicSpec()
            config_proxySwitch_0_spec_backing_pnicSpec.pnicDevice = vmnic
            config_proxySwitch_0_spec_backing_pnicSpec.uplinkPortKey = uplinkPortKey
            config_proxySwitch_0_spec_backing_pnicSpec.uplinkPortgroupKey = self.uplink_portgroup.key
            pnicSpecs.append( config_proxySwitch_0_spec_backing_pnicSpec )

        config_proxySwitch_0.spec.backing.pnicSpec = pnicSpecs
        return config_proxySwitch_0


    def set_desired_vmnics(self):
        lag_uplinks = []
        switch_uplink_ports = {'non_lag': []}

        portCriteria = vim.dvs.PortCriteria()
        portCriteria.host = [self.host_system]
        portCriteria.portgroupKey = self.uplink_portgroup.key
        portCriteria.uplinkPort = True
        ports = self.dv_switch.FetchDVPorts(portCriteria)

        for name, lag in self.lags.items():
            switch_uplink_ports[name] = []
            for uplinkName in lag.uplinkName:
                for port in ports:
                    if port.config.name == uplinkName:
                        switch_uplink_ports[name].append(port.key)
                        lag_uplinks.append(port.key)

        for port in sorted(ports, key=lambda port: port.config.name):
            if port.key in self.uplink_portgroup.portKeys and port.key not in lag_uplinks:
                switch_uplink_ports['non_lag'].append(port.key)

        count = 0
        for vmnic in self.vmnics:
            self.desired_vmnics[vmnic] = switch_uplink_ports['non_lag'][count]
            count += 1

        for lag in self.lag_uplinks:
            count = 0
            for vmnic in lag['vmnics']:
                self.desired_vmnics[vmnic] = switch_uplink_ports[lag['lag']][count]
                count += 1

    def check_uplinks(self):
        pnic_device = []

        self.set_desired_vmnics()

        for dvs_host_member in self.dv_switch.config.host:
            if dvs_host_member.config.host.name == self.esxi_hostname:
                break

        for pnicSpec in dvs_host_member.config.backing.pnicSpec:
            pnic_device.append(pnicSpec.pnicDevice)
            if pnicSpec.pnicDevice not in self.desired_vmnics:
                return False
            if pnicSpec.uplinkPortKey != self.desired_vmnics[pnicSpec.pnicDevice]:
                return False

        for vmnic in self.desired_vmnics:
            if vmnic not in pnic_device:
                return False

        return True

    def state_migrate_vss_vds(self):
        pg = find_dvspg_by_name(self.dv_switch, self.migrate_portgroup_name)
        if pg is None:
            self.module.fail_json(msg="Distributed portgroup %s does not exist" % self.migrate_portgroup_name)
        self.check_uplinks()

        if self.module.check_mode:
            self.module.exit_json(changed=True)

        host_network_system = self.host_system.configManager.networkSystem
        config = vim.host.NetworkConfig()

        config_vswitch_0 = vim.host.VirtualSwitch.Config()
        config_vswitch_0.name = self.current_switch_name
        config_vswitch_0.changeOperation = 'edit'
        config_vswitch_0.spec = vim.host.VirtualSwitch.Specification()
        config_vswitch_0.spec.numPorts = 128
        config.vswitch = [config_vswitch_0]
        config.portgroup = [self.create_port_group_config()]
        config.vnic = [self.create_host_vnic_config(self.dv_switch.uuid, pg.key)]
        config.proxySwitch = [ self.create_proxy_switch_config() ]

        host_network_system.UpdateNetworkConfig(config, "modify")
        self.module.exit_json(changed=True)

    def check_vmk_current_state(self):

        for vnic in self.host_system.configManager.networkSystem.networkInfo.vnic:
            if vnic.device == self.device:
                if vnic.spec.distributedVirtualPort is None:
                    std_vswitches = [vswitch.name for vswitch in self.host_system.configManager.networkSystem.networkInfo.vswitch]
                    if self.current_switch_name not in std_vswitches:
                        return "migrated"
                    if vnic.portgroup == self.current_portgroup_name:
                        return "migrate_vss_vds"
                else:
                    dvs = find_dvs_by_name(self.content, self.current_switch_name)
                    if dvs is None:
                        return "migrated"
                    if vnic.spec.distributedVirtualPort.switchUuid == dvs.uuid:
                        return "migrate_vds_vss"


def main():

    argument_spec = vmware_argument_spec()
    argument_spec.update(dict(esxi_hostname=dict(required=True, type='str'),
                              device=dict(required=True, type='str'),
                              vmnics=dict(required=False, type='list', default=[], elements='str'),
                              lag_uplinks=dict(
                                type='list',
                                default=[],
                                required=False,
                                elements='dict',
                                options=dict(
                                    lag=dict(
                                        type='str',
                                        required=True,
                                    ),
                                    vmnics=dict(
                                        type='list',
                                        required=False,
                                        elements='str',
                                        default=[],
                                    ),
                                ),
                              ),
                              current_switch_name=dict(required=True, type='str'),
                              current_portgroup_name=dict(required=True, type='str'),
                              migrate_switch_name=dict(required=True, type='str'),
                              migrate_portgroup_name=dict(required=True, type='str'),
                              migrate_vlan_id=dict(required=False, type='int')))

    module = AnsibleModule(argument_spec=argument_spec, supports_check_mode=True)

    if not HAS_PYVMOMI:
        module.fail_json(msg='pyvmomi required for this module')

    vmware_migrate_vmk = VMwareMigrateVmkVmnic(module)
    vmware_migrate_vmk.process_state()


if __name__ == '__main__':
    main()
