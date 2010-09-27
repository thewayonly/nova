# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright 2010 United States Government as represented by the
# Administrator of the National Aeronautics and Space Administration.
# All Rights Reserved.
# Copyright (c) 2010 Citrix Systems, Inc.
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

"""
A connection to a hypervisor (e.g. KVM) through libvirt.
"""

import logging
import os
import shutil

from twisted.internet import defer
from twisted.internet import task
from twisted.internet import threads

from nova import db
from nova import exception
from nova import flags
from nova import process
from nova import utils
from nova.auth import manager
from nova.compute import disk
from nova.compute import instance_types
from nova.compute import power_state
from nova.virt import images

libvirt = None
libxml2 = None


FLAGS = flags.FLAGS
flags.DEFINE_string('libvirt_xml_template',
                    utils.abspath('virt/libvirt.qemu.xml.template'),
                    'Libvirt XML Template for QEmu/KVM')
flags.DEFINE_string('libvirt_uml_xml_template',
                    utils.abspath('virt/libvirt.uml.xml.template'),
                    'Libvirt XML Template for user-mode-linux')
flags.DEFINE_string('injected_network_template',
                    utils.abspath('virt/interfaces.template'),
                    'Template file for injected network')
flags.DEFINE_string('libvirt_type',
                    'kvm',
                    'Libvirt domain type (valid options are: kvm, qemu, uml)')
flags.DEFINE_string('libvirt_uri',
                    '',
                    'Override the default libvirt URI (which is dependent'
                    ' on libvirt_type)')


def get_connection(read_only):
    # These are loaded late so that there's no need to install these
    # libraries when not using libvirt.
    global libvirt
    global libxml2
    if libvirt is None:
        libvirt = __import__('libvirt')
    if libxml2 is None:
        libxml2 = __import__('libxml2')
    return LibvirtConnection(read_only)


class LibvirtConnection(object):
    def __init__(self, read_only):
        self.libvirt_uri, template_file = self.get_uri_and_template()

        self.libvirt_xml = open(template_file).read()
        self._wrapped_conn = None
        self.read_only = read_only

    @property
    def _conn(self):
        if not self._wrapped_conn or not self._test_connection():
            logging.debug('Connecting to libvirt: %s' % self.libvirt_uri)
            self._wrapped_conn = self._connect(self.libvirt_uri, self.read_only)
        return self._wrapped_conn

    def _test_connection(self):
        try:
            self._wrapped_conn.getInfo()
            return True
        except libvirt.libvirtError as e:
            if e.get_error_code() == libvirt.VIR_ERR_SYSTEM_ERROR and \
               e.get_error_domain() == libvirt.VIR_FROM_REMOTE:
                logging.debug('Connection to libvirt broke')
                return False
            raise

    def get_uri_and_template(self):
        if FLAGS.libvirt_type == 'uml':
            uri = FLAGS.libvirt_uri or 'uml:///system'
            template_file = FLAGS.libvirt_uml_xml_template
        else:
            uri = FLAGS.libvirt_uri or 'qemu:///system'
            template_file = FLAGS.libvirt_xml_template
        return uri, template_file

    def _connect(self, uri, read_only):
        auth = [[libvirt.VIR_CRED_AUTHNAME, libvirt.VIR_CRED_NOECHOPROMPT],
                'root',
                None]

        if read_only:
            return libvirt.openReadOnly(uri)
        else:
            return libvirt.openAuth(uri, auth, 0)

    def list_instances(self):
        return [self._conn.lookupByID(x).name()
                for x in self._conn.listDomainsID()]

    def destroy(self, instance):
        try:
            virt_dom = self._conn.lookupByName(instance['name'])
            virt_dom.destroy()
        except Exception as _err:
            pass
            # If the instance is already terminated, we're still happy
        d = defer.Deferred()
        d.addCallback(lambda _: self._cleanup(instance))
        # FIXME: What does this comment mean?
        # TODO(termie): short-circuit me for tests
        # WE'LL save this for when we do shutdown,
        # instead of destroy - but destroy returns immediately
        timer = task.LoopingCall(f=None)
        def _wait_for_shutdown():
            try:
                state = self.get_info(instance['name'])['state']
                db.instance_set_state(None, instance['id'], state)
                if state == power_state.SHUTDOWN:
                    timer.stop()
                    d.callback(None)
            except Exception:
                db.instance_set_state(None,
                                      instance['id'],
                                      power_state.SHUTDOWN)
                timer.stop()
                d.callback(None)
        timer.f = _wait_for_shutdown
        timer.start(interval=0.5, now=True)
        return d

    def _cleanup(self, instance):
        target = os.path.join(FLAGS.instances_path, instance['name'])
        logging.info('instance %s: deleting instance files %s',
            instance['name'], target)
        if os.path.exists(target):
            shutil.rmtree(target)

    @defer.inlineCallbacks
    @exception.wrap_exception
    def attach_volume(self, instance_name, device_path, mountpoint):
        yield process.simple_execute("sudo virsh attach-disk %s %s %s" %
                                     (instance_name,
                                      device_path,
                                      mountpoint.rpartition('/dev/')[2]))

    @defer.inlineCallbacks
    @exception.wrap_exception
    def detach_volume(self, instance_name, mountpoint):
        # NOTE(vish): despite the documentation, virsh detach-disk just
        # wants the device name without the leading /dev/
        yield process.simple_execute("sudo virsh detach-disk %s %s" %
                                     (instance_name,
                                      mountpoint.rpartition('/dev/')[2]))

    @defer.inlineCallbacks
    @exception.wrap_exception
    def reboot(self, instance):
        xml = self.to_xml(instance)
        yield self._conn.lookupByName(instance['name']).destroy()
        yield self._conn.createXML(xml, 0)

        d = defer.Deferred()
        timer = task.LoopingCall(f=None)
        def _wait_for_reboot():
            try:
                state = self.get_info(instance['name'])['state']
                db.instance_set_state(None, instance['id'], state)
                if state == power_state.RUNNING:
                    logging.debug('instance %s: rebooted', instance['name'])
                    timer.stop()
                    d.callback(None)
            except Exception, exn:
                logging.error('_wait_for_reboot failed: %s', exn)
                db.instance_set_state(None,
                                      instance['id'],
                                      power_state.SHUTDOWN)
                timer.stop()
                d.callback(None)
        timer.f = _wait_for_reboot
        timer.start(interval=0.5, now=True)
        yield d

    @defer.inlineCallbacks
    @exception.wrap_exception
    def spawn(self, instance):
        xml = self.to_xml(instance)
        db.instance_set_state(None,
                              instance['id'],
                              power_state.NOSTATE,
                              'launching')
        yield NWFilterFirewall(self._conn).setup_nwfilters_for_instance(instance)
        yield self._create_image(instance, xml)
        yield self._conn.createXML(xml, 0)
        # TODO(termie): this should actually register
        # a callback to check for successful boot
        logging.debug("instance %s: is running", instance['name'])

        local_d = defer.Deferred()
        timer = task.LoopingCall(f=None)
        def _wait_for_boot():
            try:
                state = self.get_info(instance['name'])['state']
                db.instance_set_state(None, instance['id'], state)
                if state == power_state.RUNNING:
                    logging.debug('instance %s: booted', instance['name'])
                    timer.stop()
                    local_d.callback(None)
            except:
                logging.exception('instance %s: failed to boot',
                                  instance['name'])
                db.instance_set_state(None,
                                      instance['id'],
                                      power_state.SHUTDOWN)
                timer.stop()
                local_d.callback(None)
        timer.f = _wait_for_boot
        timer.start(interval=0.5, now=True)
        yield local_d

    @defer.inlineCallbacks
    def _create_image(self, inst, libvirt_xml):
        # syntactic nicety
        basepath = lambda fname='': os.path.join(FLAGS.instances_path,
                                                 inst['name'],
                                                 fname)

        # ensure directories exist and are writable
        yield process.simple_execute('mkdir -p %s' % basepath())
        yield process.simple_execute('chmod 0777 %s' % basepath())


        # TODO(termie): these are blocking calls, it would be great
        #               if they weren't.
        logging.info('instance %s: Creating image', inst['name'])
        f = open(basepath('libvirt.xml'), 'w')
        f.write(libvirt_xml)
        f.close()

        os.close(os.open(basepath('console.log'), os.O_CREAT | os.O_WRONLY, 0660))

        user = manager.AuthManager().get_user(inst['user_id'])
        project = manager.AuthManager().get_project(inst['project_id'])

        if not os.path.exists(basepath('disk')):
           yield images.fetch(inst.image_id, basepath('disk-raw'), user, project)
        if not os.path.exists(basepath('kernel')):
           yield images.fetch(inst.kernel_id, basepath('kernel'), user, project)
        if not os.path.exists(basepath('ramdisk')):
           yield images.fetch(inst.ramdisk_id, basepath('ramdisk'), user, project)

        execute = lambda cmd, process_input=None: \
                  process.simple_execute(cmd=cmd,
                                         process_input=process_input,
                                         check_exit_code=True)

        key = str(inst['key_data'])
        net = None
        network_ref = db.project_get_network(None, project.id)
        if network_ref['injected']:
            address = db.instance_get_fixed_address(None, inst['id'])
            with open(FLAGS.injected_network_template) as f:
                net = f.read() % {'address': address,
                                  'netmask': network_ref['netmask'],
                                  'gateway': network_ref['gateway'],
                                  'broadcast': network_ref['broadcast'],
                                  'dns': network_ref['dns']}
        if key or net:
            if key:
                logging.info('instance %s: injecting key into image %s',
                    inst['name'], inst.image_id)
            if net:
                logging.info('instance %s: injecting net into image %s',
                    inst['name'], inst.image_id)
            yield disk.inject_data(basepath('disk-raw'), key, net, execute=execute)

        if os.path.exists(basepath('disk')):
            yield process.simple_execute('rm -f %s' % basepath('disk'))

        bytes = (instance_types.INSTANCE_TYPES[inst.instance_type]['local_gb']
                 * 1024 * 1024 * 1024)
        yield disk.partition(
                basepath('disk-raw'), basepath('disk'), bytes, execute=execute)

        if FLAGS.libvirt_type == 'uml':
            yield process.simple_execute('sudo chown root %s' %
                                         basepath('disk'))

    def to_xml(self, instance):
        # TODO(termie): cache?
        logging.debug('instance %s: starting toXML method', instance['name'])
        network = db.project_get_network(None, instance['project_id'])
        # FIXME(vish): stick this in db
        instance_type = instance_types.INSTANCE_TYPES[instance['instance_type']]
        ip_address = db.instance_get_fixed_address({}, instance['id'])
        # Assume that the gateway also acts as the dhcp server.
        dhcp_server = network['gateway']
        xml_info = {'type': FLAGS.libvirt_type,
                    'name': instance['name'],
                    'basepath': os.path.join(FLAGS.instances_path,
                                             instance['name']),
                    'memory_kb': instance_type['memory_mb'] * 1024,
                    'vcpus': instance_type['vcpus'],
                    'bridge_name': network['bridge'],
                    'mac_address': instance['mac_address'],
                    'ip_address': ip_address,
                    'dhcp_server': dhcp_server }
        libvirt_xml = self.libvirt_xml % xml_info
        logging.debug('instance %s: finished toXML method', instance['name'])

        return libvirt_xml

    def get_info(self, instance_name):
        virt_dom = self._conn.lookupByName(instance_name)
        (state, max_mem, mem, num_cpu, cpu_time) = virt_dom.info()
        return {'state': state,
                'max_mem': max_mem,
                'mem': mem,
                'num_cpu': num_cpu,
                'cpu_time': cpu_time}

    def get_disks(self, instance_name):
        """
        Note that this function takes an instance name, not an Instance, so
        that it can be called by monitor.

        Returns a list of all block devices for this domain.
        """
        domain = self._conn.lookupByName(instance_name)
        # TODO(devcamcar): Replace libxml2 with etree.
        xml = domain.XMLDesc(0)
        doc = None

        try:
            doc = libxml2.parseDoc(xml)
        except:
            return []

        ctx = doc.xpathNewContext()
        disks = []

        try:
            ret = ctx.xpathEval('/domain/devices/disk')

            for node in ret:
                devdst = None

                for child in node.children:
                    if child.name == 'target':
                        devdst = child.prop('dev')

                if devdst == None:
                    continue

                disks.append(devdst)
        finally:
            if ctx != None:
                ctx.xpathFreeContext()
            if doc != None:
                doc.freeDoc()

        return disks

    def get_interfaces(self, instance_name):
        """
        Note that this function takes an instance name, not an Instance, so
        that it can be called by monitor.

        Returns a list of all network interfaces for this instance.
        """
        domain = self._conn.lookupByName(instance_name)
        # TODO(devcamcar): Replace libxml2 with etree.
        xml = domain.XMLDesc(0)
        doc = None

        try:
            doc = libxml2.parseDoc(xml)
        except:
            return []

        ctx = doc.xpathNewContext()
        interfaces = []

        try:
            ret = ctx.xpathEval('/domain/devices/interface')

            for node in ret:
                devdst = None

                for child in node.children:
                    if child.name == 'target':
                        devdst = child.prop('dev')

                if devdst == None:
                    continue

                interfaces.append(devdst)
        finally:
            if ctx != None:
                ctx.xpathFreeContext()
            if doc != None:
                doc.freeDoc()

        return interfaces

    def block_stats(self, instance_name, disk):
        """
        Note that this function takes an instance name, not an Instance, so
        that it can be called by monitor.
        """
        domain = self._conn.lookupByName(instance_name)
        return domain.blockStats(disk)

    def interface_stats(self, instance_name, interface):
        """
        Note that this function takes an instance name, not an Instance, so
        that it can be called by monitor.
        """
        domain = self._conn.lookupByName(instance_name)
        return domain.interfaceStats(interface)


    def refresh_security_group(self, security_group_id):
        fw = NWFilterFirewall(self._conn)
        fw.ensure_security_group_filter(security_group_id)


class NWFilterFirewall(object):
    """
    This class implements a network filtering mechanism versatile
    enough for EC2 style Security Group filtering by leveraging
    libvirt's nwfilter.

    First, all instances get a filter ("nova-base-filter") applied.
    This filter drops all incoming ipv4 and ipv6 connections.
    Outgoing connections are never blocked.

    Second, every security group maps to a nwfilter filter(*).
    NWFilters can be updated at runtime and changes are applied
    immediately, so changes to security groups can be applied at
    runtime (as mandated by the spec).

    Security group rules are named "nova-secgroup-<id>" where <id>
    is the internal id of the security group. They're applied only on
    hosts that have instances in the security group in question.

    Updates to security groups are done by updating the data model
    (in response to API calls) followed by a request sent to all
    the nodes with instances in the security group to refresh the
    security group.

    Each instance has its own NWFilter, which references the above
    mentioned security group NWFilters. This was done because
    interfaces can only reference one filter while filters can
    reference multiple other filters. This has the added benefit of
    actually being able to add and remove security groups from an
    instance at run time. This functionality is not exposed anywhere,
    though.

    Outstanding questions:

    The name is unique, so would there be any good reason to sync
    the uuid across the nodes (by assigning it from the datamodel)?


    (*) This sentence brought to you by the redundancy department of
        redundancy.
    """

    def __init__(self, get_connection):
        self._conn = get_connection


    nova_base_filter = '''<filter name='nova-base' chain='root'>
                            <uuid>26717364-50cf-42d1-8185-29bf893ab110</uuid>
                            <filterref filter='no-mac-spoofing'/>
                            <filterref filter='no-ip-spoofing'/>
                            <filterref filter='no-arp-spoofing'/>
                            <filterref filter='allow-dhcp-server'/>
                            <filterref filter='nova-base-ipv4'/>
                            <filterref filter='nova-base-ipv6'/>
                          </filter>'''

    nova_base_ipv4_filter = '''<filter name='nova-base-ipv4' chain='ipv4'>
                                 <rule action='drop' direction='in'
                                       priority='400' />
                               </filter>'''


    nova_base_ipv6_filter = '''<filter name='nova-base-ipv6' chain='ipv6'>
                                 <rule action='drop' direction='in'
                                       priority='400' />
                               </filter>'''


    def _define_filter(self, xml):
        if callable(xml):
            xml = xml()
        d = threads.deferToThread(self._conn.nwfilterDefineXML, xml)
        return d


    @defer.inlineCallbacks
    def setup_nwfilters_for_instance(self, instance):
        """
        Creates an NWFilter for the given instance. In the process,
        it makes sure the filters for the security groups as well as
        the base filter are all in place.
        """

        yield self._define_filter(self.nova_base_ipv4_filter)
        yield self._define_filter(self.nova_base_ipv6_filter)
        yield self._define_filter(self.nova_base_filter)

        nwfilter_xml  = ("<filter name='nova-instance-%s' chain='root'>\n" +
                         "  <filterref filter='nova-base' />\n"
                        ) % instance['name']

        for security_group in instance.security_groups:
            yield self.ensure_security_group_filter(security_group['id'])

            nwfilter_xml += ("  <filterref filter='nova-secgroup-%d' />\n"
                            ) % security_group['id']
        nwfilter_xml += "</filter>"

        yield self._define_filter(nwfilter_xml)
        return

    def ensure_security_group_filter(self, security_group_id):
        return self._define_filter(
                   self.security_group_to_nwfilter_xml(security_group_id))


    def security_group_to_nwfilter_xml(self, security_group_id):
        security_group = db.security_group_get({}, security_group_id)
        rule_xml = ""
        for rule in security_group.rules:
            rule_xml += "<rule action='accept' direction='in' priority='900'>"
            if rule.cidr:
                rule_xml += "<%s srcipaddr='%s' " % (rule.protocol, rule.cidr)
                if rule.protocol in ['tcp', 'udp']:
                    rule_xml += "dstportstart='%s' dstportend='%s' " % \
                                (rule.from_port, rule.to_port)
                elif rule.protocol == 'icmp':
                    logging.info('rule.protocol: %r, rule.from_port: %r, rule.to_port: %r' % (rule.protocol, rule.from_port, rule.to_port))
                    if rule.from_port != -1:
                        rule_xml += "type='%s' " % rule.from_port
                    if rule.to_port != -1:
                        rule_xml += "code='%s' " % rule.to_port

                rule_xml += '/>\n'
            rule_xml += "</rule>\n"
        xml = '''<filter name='nova-secgroup-%s' chain='ipv4'>%s</filter>''' % (security_group_id, rule_xml,)
        return xml
