#!/usr/bin/env python3
"""srv6-fabric-lab: build an SRv6 spine-leaf test fabric of libvirt VMs.

Reads the plugin's topology file ([srv6] topology_file). Every node not in
--hosts becomes a switch VM: plain Ubuntu, static routes, its own SID and,
with --neutron-url, the telemetry agent. Every link becomes an isolated
libvirt network with one NIC at each end. The hosts (the DevStack nodes)
must be libvirt domains on this machine, named as in the file: they get
their NICs here, and `config` prints the script that addresses them.

  up      TOPO --hosts H,... --image IMG [--ssh-key F]
          [--neutron-url URL --credentials OPENRC]
  down    TOPO --hosts H,...
  config  TOPO --hosts H,... NODE   print the node's srv6-fabric.sh
  test                              self-check

Run `up` and `down` as root. Addressing: link i (file order, from 1) is
fd01:<i>::/64, first end ::1, second ::2. The NIC is named after the peer
and has MAC 52:54:00:f0:<i>:<end>. Routes: static, ECMP over every
shortest-path next hop to every other locator.
"""
import argparse
import ipaddress
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
IMAGES = '/var/lib/libvirt/images'
BASE = os.path.join(IMAGES, 'srv6-fabric-base.qcow2')
VIRSH = ['virsh', '-q', '-c', 'qemu:///system']


# --- the topology -----------------------------------------------------------

def load(path):
    """({node: locator}, [(i, a, b)]), same format as the plugin reads."""
    nodes, links = {}, []
    for line in open(path):
        w = line.split('#')[0].split()
        if w and w[0] == 'node':
            nodes[w[1]] = w[2]
        elif w and w[0] == 'link':
            links.append((len(links) + 1, w[1], w[2]))
    for _, a, b in links:
        assert a in nodes and b in nodes, 'link %s-%s: unknown node' % (a, b)
    assert len(links) < 256, 'one MAC byte per link'
    for n in nodes:
        # the NIC toward n is named n: an interface name has 15 characters
        assert len(n) <= 15, 'node name too long for an interface: %s' % n
    return nodes, links


def ends(link, node):
    """(my address, peer, peer's address, my NIC's MAC) on a link."""
    i, a, b = link
    me, peer = (1, 2) if node == a else (2, 1)
    return ('fd01:%x::%d' % (i, me), b if node == a else a,
            'fd01:%x::%d' % (i, peer), '52:54:00:f0:%02x:%02x' % (i, me))


def netname(link):
    return 'srv6f-%s-%s' % link[1:]


def routes(node, nodes, links):
    """[(locator, [(via, dev)])]: every other locator, all shortest hops."""
    adj = {n: {} for n in nodes}
    for lk in links:
        adj[lk[1]][lk[2]] = adj[lk[2]][lk[1]] = lk
    out = []
    for dest in nodes:
        if dest == node:
            continue
        dist, queue = {dest: 0}, [dest]
        for u in queue:                         # BFS from the destination
            for v in adj[u]:
                if v not in dist:
                    dist[v] = dist[u] + 1
                    queue.append(v)
        if node not in dist:
            sys.exit('%s cannot reach %s' % (node, dest))
        hops = [(ends(adj[node][p], node)[2], p) for p in sorted(adj[node])
                if dist.get(p) == dist[node] - 1]
        out.append((nodes[dest], hops))
    return out


def end_sid(locator):
    """The encap mode End SID of a /48 locator: <locator>:fff0::/128."""
    net = ipaddress.IPv6Network(locator)
    assert net.prefixlen == 48, 'locators must be /48: %s' % locator
    return '%s/128' % (net.network_address + (0xfff0 << 64))


# --- what runs on a node ----------------------------------------------------

# "sh srv6-fabric.sh install": copy the script in place and run it at boot
INSTALL = r"""if [ "${1:-}" = install ]; then
    f=/usr/local/sbin/srv6-fabric.sh
    [ "$0" = $f ] || cp "$0" $f
    cat > /etc/systemd/system/srv6-fabric.service <<EOF
[Unit]
After=network.target
Before=devstack@q-agt.service
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/sh $f
[Install]
WantedBy=multi-user.target
EOF
    # networkd must not delete these routes when it restarts
    mkdir -p /etc/systemd/networkd.conf.d
    printf '[Network]\nManageForeignRoutes=no\n' \
        > /etc/systemd/networkd.conf.d/10-srv6-fabric.conf
    systemctl daemon-reload && systemctl enable -q srv6-fabric.service
fi
"""

NIC = r"""for f in /proc/sys/net/ipv6/conf/*/seg6_enabled; do
    echo 1 > "$f"
done
nic() {  # <mac> <name> <address>: name the NIC with that MAC, address it
    cur=$(ip -br link | awk -v m="$1" '$3 == m {print $1; exit}')
    [ -n "$cur" ] || { echo "no NIC with MAC $1 (to $2)" >&2; exit 1; }
    if [ "$cur" != "$2" ]; then
        ip link set "$cur" down; ip link set "$cur" name "$2"
    fi
    echo 0 > /proc/sys/net/ipv6/conf/$2/accept_ra
    echo 1 > /proc/sys/net/ipv6/conf/$2/seg6_enabled
    ip link set "$2" mtu 9000 up
    ip -6 addr replace "$3/64" dev "$2" nodad
}
"""


def node_script(node, nodes, links, hosts):
    switch = node not in hosts
    s = ['#!/bin/sh',
         '# SRv6 fabric config of %s, by tools/srv6-fabric-lab.py' % node,
         'set -eu',
         INSTALL.rstrip()]
    if switch:
        s.append('sysctl -qw net.ipv6.conf.all.forwarding=1')
    s.append(NIC.rstrip())
    for lk in links:
        if node in lk[1:]:
            addr, peer, _, mac = ends(lk, node)
            s.append('nic %s %s %s' % (mac, peer, addr))
    for prefix, hops in routes(node, nodes, links):
        s.append('ip -6 route replace %s %s' % (prefix, ' '.join(
            'nexthop via %s dev %s' % h for h in hops)))
    if switch:
        # on a dummy, not a fabric NIC: a NIC going down would take the SID
        s += ['ip link show sr0 >/dev/null 2>&1 || '
              'ip link add sr0 type dummy',
              'ip link set sr0 up',
              '# usid: uN on the whole locator; encap: End on <loc>:fff0::',
              'ip -6 route replace %s encap seg6local action End '
              'flavors next-csid lblen 32 nflen 16 dev sr0' % nodes[node],
              'ip -6 route replace %s encap seg6local action End dev sr0'
              % end_sid(nodes[node])]
    return '\n'.join(s) + '\n'


AGENT_UNIT = '''[Unit]
After=srv6-fabric.service network-online.target
Wants=network-online.target
[Service]
ExecStart=/usr/bin/python3 /usr/local/sbin/srv6-fabric-agent.py \\
    --links /etc/srv6-fabric-agent.links --period 5 --aggregates \\
    --neutron-url %s --credentials /etc/srv6-fabric-agent.openrc
# the server reads a silent switch as a dead one: come back fast
Restart=always
RestartSec=2
[Install]
WantedBy=multi-user.target
'''


def user_data(node, nodes, links, hosts, args):
    """cloud-init user data, as JSON: valid YAML, nothing to escape."""
    files = [{'path': '/usr/local/sbin/srv6-fabric.sh', 'permissions': '0755',
              'content': node_script(node, nodes, links, hosts)}]
    run = [['sh', '/usr/local/sbin/srv6-fabric.sh', 'install']]
    if args.neutron_url:
        peers = [ends(lk, node)[1] for lk in links if node in lk[1:]]
        # <interface> <this node> <neighbour>; the NIC is named after the peer
        link_map = ''.join('%s %s %s\n' % (p, node, p) for p in peers)
        files += [
            {'path': '/usr/local/sbin/srv6-fabric-agent.py',
             'permissions': '0755',
             'content': open(os.path.join(HERE,
                                          'srv6-fabric-agent.py')).read()},
            {'path': '/etc/srv6-fabric-agent.links', 'content': link_map},
            {'path': '/etc/srv6-fabric-agent.openrc', 'permissions': '0600',
             'content': open(args.credentials).read()},
            {'path': '/etc/systemd/system/srv6-fabric-agent.service',
             'content': AGENT_UNIT % args.neutron_url}]
        run.append(['systemctl', 'enable', '--now', 'srv6-fabric-agent'])
    user = {'name': 'stack', 'shell': '/bin/bash', 'lock_passwd': True,
            'sudo': 'ALL=(ALL) NOPASSWD:ALL'}
    if args.ssh_key:
        user['ssh_authorized_keys'] = [open(args.ssh_key).read().strip()]
    return '#cloud-config\n' + json.dumps({
        'hostname': node, 'users': [user], 'ssh_pwauth': False,
        'write_files': files, 'runcmd': run}, indent=1)


# --- libvirt ----------------------------------------------------------------

def sh(*cmd, check=True):
    return subprocess.run(cmd, check=check, capture_output=True,
                          text=True).stdout


def virsh(*cmd, check=True):
    return sh(*VIRSH, *cmd, check=check)


def exists(kind, name):
    """kind: 'dom' or 'net-'."""
    return subprocess.run(VIRSH + [kind + 'info', name],
                          capture_output=True).returncode == 0


def running(dom):
    return virsh('domstate', dom).strip() == 'running'


def up(nodes, links, hosts, args):
    for h in hosts:
        if not exists('dom', h):
            sys.exit('host %s is not a libvirt domain here' % h)
    # the user data holds the credentials: gone when done
    with tempfile.TemporaryDirectory() as tmp:
        _up(nodes, links, hosts, args, tmp)
    print('next, on every host: %s config <topology> --hosts %s <host>'
          % (sys.argv[0], ','.join(hosts)))


def _up(nodes, links, hosts, args, tmp):
    for lk in links:
        net = netname(lk)
        if not exists('net-', net):
            xml = os.path.join(tmp, net + '.xml')
            # no <ip>, no <forward>: libvirt only makes a bridge
            open(xml, 'w').write("<network><name>%s</name>"
                                 "<mtu size='9000'/></network>" % net)
            virsh('net-define', xml)
        virsh('net-start', net, check=False)        # fails if already active
        virsh('net-autostart', net)
    print('links: %d networks' % len(links))

    if not os.path.exists(BASE):
        sh('cp', args.image, BASE)
    switches = [n for n in nodes if n not in hosts]
    for k, n in enumerate(switches, 1):
        if exists('dom', n):
            print('%s: exists, left alone' % n)
            continue
        disk = os.path.join(IMAGES, 'srv6-fabric-%s.qcow2' % n)
        seed = os.path.join(IMAGES, 'srv6-fabric-%s-seed.iso' % n)
        mgmt_mac = '52:54:00:f0:00:%02x' % k
        ud, nc = os.path.join(tmp, 'ud'), os.path.join(tmp, 'nc')
        open(ud, 'w').write(user_data(n, nodes, links, hosts, args))
        open(nc, 'w').write(json.dumps({'version': 2, 'ethernets': {'mgmt': {
            'match': {'macaddress': mgmt_mac}, 'set-name': 'mgmt',
            'dhcp4': True}}}))
        sh('qemu-img', 'create', '-q', '-f', 'qcow2', '-F', 'qcow2',
           '-b', BASE, disk, '8G')
        sh('cloud-localds', '--network-config=' + nc, seed, ud)
        os.chmod(seed, 0o600)                       # it holds the credentials
        nics = ['--network', 'network=default,model=virtio,mac=' + mgmt_mac]
        for lk in links:
            if n in lk[1:]:
                nics += ['--network', 'network=%s,model=virtio,mac=%s'
                         % (netname(lk), ends(lk, n)[3])]
        sh('virt-install', '-q', '--connect', 'qemu:///system', '--import',
           '--name', n, '--memory', str(args.memory), '--vcpus', '1',
           '--osinfo', 'detect=on,name=ubuntu24.04',
           '--disk', 'path=%s,bus=virtio' % disk,
           '--disk', 'path=%s,device=cdrom' % seed,
           *nics, '--graphics', 'none', '--noautoconsole')
        print('%s: created' % n)

    for h in hosts:
        have = virsh('domiflist', h).lower()
        live = ['--live'] if running(h) else []
        for lk in links:
            if h in lk[1:] and ends(lk, h)[3] not in have:
                virsh('attach-interface', '--domain', h, '--type', 'network',
                      '--source', netname(lk), '--model', 'virtio',
                      '--mac', ends(lk, h)[3], '--config', *live)
                print('%s: NIC toward %s' % (h, ends(lk, h)[1]))


def down(nodes, links, hosts):
    for n in nodes:
        if n in hosts:
            if not exists('dom', n):
                continue
            live = ['--live'] if running(n) else []
            have = virsh('domiflist', n).lower()
            for lk in links:
                if n in lk[1:] and ends(lk, n)[3] in have:
                    virsh('detach-interface', '--domain', n, '--type',
                          'network', '--mac', ends(lk, n)[3], '--config',
                          *live)
        elif exists('dom', n):
            virsh('destroy', n, check=False)
            virsh('undefine', n)
            for f in ('srv6-fabric-%s.qcow2', 'srv6-fabric-%s-seed.iso'):
                if os.path.exists(os.path.join(IMAGES, f % n)):
                    os.remove(os.path.join(IMAGES, f % n))
            print('%s: deleted' % n)
    for lk in links:
        if exists('net-', netname(lk)):
            virsh('net-destroy', netname(lk), check=False)
            virsh('net-undefine', netname(lk))
    print('links removed; the hosts keep their routes until rebooted '
          'without srv6-fabric.service')


# --- self-check -------------------------------------------------------------

SAMPLE = '''node spine1 fc00:0:101::/48
node spine2 fc00:0:102::/48
node leaf1  fc00:0:103::/48
node leaf2  fc00:0:104::/48
node node1  fc00:0:1::/48
node node2  fc00:0:2::/48
node node3  fc00:0:3::/48
link spine1 leaf1 1000
link spine1 leaf2 1000
link spine2 leaf1 1000
link spine2 leaf2 1000
link leaf1 node1 1000
link leaf1 node2 1000
link leaf2 node3 1000
'''


def selftest():
    with tempfile.NamedTemporaryFile('w', suffix='.topo') as f:
        f.write(SAMPLE)
        f.flush()
        nodes, links = load(f.name)
    hosts = ['node1', 'node2', 'node3']
    r = dict(routes('leaf1', nodes, links))
    assert r['fc00:0:3::/48'] == [('fd01:1::1', 'spine1'),
                                  ('fd01:3::1', 'spine2')], r
    assert r['fc00:0:1::/48'] == [('fd01:5::2', 'node1')], r
    assert r['fc00:0:101::/48'] == [('fd01:1::1', 'spine1')], r
    r = dict(routes('node1', nodes, links))
    assert r['fc00:0:3::/48'] == [('fd01:5::1', 'leaf1')], r
    assert dict(routes('spine1', nodes, links))['fc00:0:102::/48'] == [
        ('fd01:1::2', 'leaf1'), ('fd01:2::2', 'leaf2')]
    assert end_sid('fc00:0:103::/48') == 'fc00:0:103:fff0::/128'
    s = node_script('leaf1', nodes, links, hosts)
    assert 'nic 52:54:00:f0:01:02 spine1 fd01:1::2' in s, s
    assert 'next-csid' in s and 'forwarding=1' in s
    s = node_script('node1', nodes, links, hosts)
    assert 'seg6local' not in s and 'forwarding' not in s
    assert 'nic 52:54:00:f0:05:02 leaf1 fd01:5::2' in s, s

    with tempfile.NamedTemporaryFile('w') as cred:
        cred.write('OS_AUTH_URL=x\n')
        cred.flush()
        args = argparse.Namespace(neutron_url='http://ctl/networking',
                                  ssh_key=None, credentials=cred.name)
        ud = json.loads(user_data('leaf1', nodes, links, hosts, args)
                        .split('\n', 1)[1])
    link_map = [w['content'] for w in ud['write_files']
                if w['path'] == '/etc/srv6-fabric-agent.links'][0]
    assert link_map.splitlines() == [
        'spine1 leaf1 spine1', 'spine2 leaf1 spine2',
        'node1 leaf1 node1', 'node2 leaf1 node2']
    print('srv6-fabric-lab self-test ok')


def main():
    p = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    p.add_argument('action', choices=['up', 'down', 'config', 'test'])
    p.add_argument('topology', nargs='?')
    p.add_argument('node', nargs='?', help='config: the node')
    p.add_argument('--hosts', default='',
                   help='comma-separated: the DevStack nodes (no switch VM)')
    p.add_argument('--image', help='up: Ubuntu 24.04 cloud image')
    p.add_argument('--ssh-key', help='up: public key for user stack')
    p.add_argument('--memory', type=int, default=1024,
                   help='up: MB of RAM per switch (default 1024)')
    p.add_argument('--neutron-url', help='up: install the telemetry agent')
    p.add_argument('--credentials', help='up: openrc file for the agent')
    a = p.parse_intermixed_args()
    if a.action == 'test':
        return selftest()
    if not a.topology or not a.hosts:
        p.error('needs a topology file and --hosts')
    nodes, links = load(a.topology)
    hosts = a.hosts.split(',')
    for h in hosts:
        if h not in nodes:
            p.error('host %s is not in the topology' % h)
    if a.action == 'config':
        if a.node not in nodes:
            p.error('config needs a node of the topology')
        sys.stdout.write(node_script(a.node, nodes, links, hosts))
    elif a.action == 'up':
        if not a.image and not os.path.exists(BASE):
            p.error('up needs --image')
        if bool(a.neutron_url) != bool(a.credentials):
            p.error('--neutron-url and --credentials go together')
        up(nodes, links, hosts, a)
    else:
        down(nodes, links, hosts)


if __name__ == '__main__':
    main()
