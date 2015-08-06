#!/usr/bin/env python

import os
import logging
from logging import debug, info, warning
import yaml
from subprocess import call, Popen, PIPE
from shutil import rmtree
from tempfile import mkstemp, mkdtemp
import netifaces

LOG_FILE = '/tmp/compute_post_deployment.log'
ASTUTE_PATH = '/etc/astute.yaml'
ACCESS_SECTION = 'xenserver-fuel-plugin'
XENAPI_URL = 'https://pypi.python.org/packages/source/X/XenAPI/XenAPI-1.2.tar.gz'

logging.basicConfig(filename=LOG_FILE,level=logging.DEBUG)

def get_astute(astute_path):
	if not os.path.exists(astute_path):
		warning('%s not found' % astute_path)
		return None

	astute = yaml.load(open(astute_path))
	return astute

def get_access(astute, access_section):
	if not access_section in astute:
		warning('%s not found' % access_section)
		return None

	access = astute[access_section]
	info('username: {username}'.format(**access))
	info('password: {password}'.format(**access))
	return access

def get_endpoints(astute):
	_endpoints = astute['network_scheme']['endpoints']
	endpoints = dict([(k,_endpoints[k]['IP'][0]) for k in _endpoints])

	info('br-storage: {br-storage}'.format(**access))
	info('br-mgmt: {br-mgmt}'.format(**access))
	return endpoints

def init_eth(dev_no):
	eth = 'eth%s' % dev_no
	if not eth in netifaces.interfaces():
		warning('%s not found' % eth)
		return

	info('%s found' % eth)
	call(['dhclient', eth])
	call(['ifconfig', eth])
	fname = '/etc/network/interfaces.d/ifcfg-' + eth
	s = \
"""auto {eth}
iface {eth} inet dhcp
""".format(eth = eth)
	with open(fname, 'w') as f:
		f.write(s)
	info('%s created' % fname)
	call(['ifdown', eth])
	call(['ifup', eth])
	addr = netifaces.ifaddresses(eth).get(2)
	if addr is not None:
		ip = addr[0]['addr']
		info('%s : %s' % (eth, ip))
		return ip
	else:
		warning('%s not found' % access_section)

def install_xenapi_sdk(xenapi_url):
	xenapi_zipball = mkstemp()[1]
	xenapi_sources = mkdtemp()

	call(['wget', '-qO', xenapi_zipball, xenapi_url])
	info('%s downloaded' % (xenapi_url))

	call(['tar', '-zxf', xenapi_zipball, '-C', xenapi_sources])
	subdirs = os.listdir(xenapi_sources)
	if (len(subdirs) != 1) or (not subdirs[0].startswith('XenAPI')):
		warning('fail to extract %s' % xenapi_url)
		return
	info('%s extracted' % (subdirs[0]))

	src = os.path.join(xenapi_sources, subdirs[0], 'XenAPI.py')
	dest = '/usr/lib/python2.7/dist-packages'
	call(['cp', src, dest])
	info('XenAPI.py deployed')

	os.remove(xenapi_zipball)
	rmtree(xenapi_sources)

def create_novacompute_conf(access, ip):
	template = """[DEFAULT]
compute_driver=xenapi.XenAPIDriver
[xenserver]
connection_url=http://%s
connection_username="%s"
connection_password="%s"
"""
	xs_ip = '.'.join(ip.split('.')[:-1] + ['1'])
	s = template % (xs_ip, access['username'], access['password'])
	with open('/etc/nova/nova-compute.conf','w') as f:
		f.write(s)
	info('nova-compute.conf created')

def restart_nova_services():
	_run('stop nova-compute')
	_run('start nova-compute')
	info('nova-compute restarted')
	_run('stop nova-network')
	_run('start nova-network')
	info('nova-network restarted')


def _ssh(host, access, cmd):
	ssh = Popen(['sshpass', '-p', access['password'], 'ssh', 
		'%s@%s' % (access['username'], host), cmd],
		stdout=PIPE, stderr=PIPE)
	s = ssh.stdout.readlines()
	return '\n'.join(s)

def _scp(host, access, path, filename):
	ssh = Popen(['sshpass', '-p', access['password'], 'scp', 
		'%s@%s:%s' % (access['username'], host, path), filename],
		stdout=PIPE, stderr=PIPE)
	s = ssh.stdout.readlines()
	return '\n'.join(s)

def _run(cmd):
	ssh = Popen(cmd, shell=True, stdout=PIPE, stderr=PIPE)
	s = ssh.stdout.readlines()
	return '\n'.join(s)

def route_to_himn(endpoints, himn_ip, access):
	#TODO check exists
	storage_ip = endpoints.get('br-storage')
	if storage_ip:
		_ssh(himn_ip, access, 'route add "%s" gw "%s"' % (storage_ip, himn_ip))
		info('storage network %s routed to %s' % (storage_ip, himn_ip))
	else:
		info('storage network ip is missing')

	mgmt_ip = endpoints.get('br-mgmt')
	if storage_ip:
		_ssh(himn_ip, access, 'route add "%s" gw "%s"' % (mgmt_ip, himn_ip))
		info('management network %s routed to %s' % (mgmt_ip, himn_ip))
	else:
		info('management network ip is missing')

def install_suppack(himn_ip, access):
	#TODO: check exists
	_scp(himn_ip, access, '/tmp/', 'novaplugins.iso')
	_ssh(himn_ip, access, 'xe-install-supplemental-pack /tmp/novaplugins.iso')
	_ssh(himn_ip, access, 'rm /tmp/novaplugins.iso')

def forward_from_himn(endpoints, himn_ip, eth_no):
	#TODO check exists
	_run('iptables -A FORWARD -i eth%s -j ACCEPT' % eth_no)
	_run("sed -i 's/#net.ipv4.ip_forward/net.ipv4.ip_forward/g' "\
		"/etc/sysctl.conf")
	_run('sysctl -p /etc/sysctl.conf')
	_run('iptables -t nat -A POSTROUTING -o eth%s -j MASQUERADE' % eth_no)

if __name__ == '__main__':
	eth_no = 2
	install_xenapi_sdk(XENAPI_URL)
	astute = get_astute(ASTUTE_PATH)
	if astute:
		access = get_access(astute, ACCESS_SECTION)
		endpoints = get_endpoints(astute)
		himn_ip = init_eth(eth_no)
		if access and endpoints and himn_ip:
			route_to_himn(endpoints, himn_ip, access)
			install_suppack(himn_ip, access)
			forward_from_himn(endpoints, himn_ip, eth_no)
			create_novacompute_conf(access, himn_ip)
			restart_nova_services()
