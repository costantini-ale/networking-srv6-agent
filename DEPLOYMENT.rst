==========================================
Deploying networking-srv6-agent (DevStack)
==========================================

What a cloud needs to run the plugin, and a worked three-node example. How
the plugin works and how to use it: `README.rst <README.rst>`__.

.. contents::
   :local:
   :depth: 1


Requirements
------------

=======================  =====================================================
Requirement              Why
=======================  =====================================================
ML2/OVS with VXLAN       The node side extends the OVS agent. OVN and
                         linuxbridge are not supported.
Kernel with SRv6         ``seg6`` / ``seg6local`` routes; stock on Ubuntu and
                         Debian.
``vrf`` kernel module    One Linux VRF per domain. Ubuntu cloud images need
                         ``linux-modules-extra``.
Kernel + iproute2 6.1+   Only for ``usid`` (the default): microSID routes.
                         ``encap`` works on older ones.
IPv6 underlay            Carries the SRv6 packets between nodes. The plugin
                         uses it, never configures it.
Underlay MTU             The outer header adds 40 to 64+ bytes to every VM
                         packet (see `MTU`_).
Python 3.11+             --
Neutron                  Same release as the rest of the cloud (the DevStack
                         branch).
=======================  =====================================================

Verify, on every node before stacking::

    grep SEG6_LWTUNNEL /boot/config-$(uname -r)  # CONFIG_IPV6_SEG6_LWTUNNEL=y
    modinfo vrf                                  # module info, no error
    uname -r                                     # 6.1 or newer (usid)
    ip -V                                        # iproute2 6.1 or newer (usid)
    python3 -V                                   # 3.11 or newer
    ping -6 -c1 <other node underlay address>    # a reply, from every peer

and after stacking, on the controller::

    openstack network agent list --agent-type open-vswitch  # all nodes, alive

VXLAN still carries the traffic between VMs of the *same* network; SRv6
carries only the traffic between networks of a domain. The agent loads
``vrf`` and sets the SRv6 sysctls itself; if the kernel lacks ``usid``
support, the server logs an error naming the node.

Underlay
~~~~~~~~

The *underlay* is the physical network between the nodes (it can be the
management or VXLAN NIC). Before stacking, every node needs:

============================  ===============================================
What                          Example
============================  ===============================================
IPv6 address on the NIC       ``fd00::1/64``, reachable from the other nodes.
Locator (``SRV6_LOCATOR``)    ``fc00:0:1::/48``, unique per node. Not an
                              interface address: the agent installs the
                              routes for the SIDs inside it. With ``usid``,
                              all locators are /48s in one /32
                              (``fc00:0:1::/48``, ``fc00:0:2::/48``, ...).
Route to each peer locator    ``ip -6 route add fc00:0:2::/48 via fd00::2``
                              (one per peer on a shared L2 segment; in a
                              routed fabric the switches route them).
============================  ===============================================

The agent finds the underlay NIC from the route to the peers' locators; set
``SRV6_UNDERLAY_INTERFACE`` only to force one.

MTU
~~~

SRv6 wraps each VM packet in an outer IPv6 header, so it must fit:
**underlay MTU >= network MTU + 40** (``usid``; +16 per waypoint beyond
four), or **+ 64** (``encap``; +16 per waypoint).

With DevStack defaults (VXLAN networks, 1500-byte underlay, ``usid``, at
most four waypoints) there is nothing to do: 1450 + 40 = 1490. It breaks
with:

- ``encap``: 1450 + 64 = 1514.
- VLAN or flat networks in a domain: 1500 + 40 = 1540.
- Five or more waypoints with ``usid`` (fabric switches count).
- A network MTU raised by hand.

Symptom: ping works, large transfers stall; the agent logs a warning with
the largest network MTU that fits. Fix one side::

    sudo ip link set <nic> mtu 9000              # raise the underlay MTU
    openstack network set --mtu 1400 <net>       # or lower the network MTU


DevStack settings
-----------------

In ``local.conf`` on **every** node (controller and computes)::

    enable_plugin networking-srv6-agent https://github.com/costantini-ale/networking-srv6-agent.git main
    SRV6_LOCATOR=fc00:0:1::/48        # unique per node

The plugin installs the package, enables the service plugin and the agent
extension, writes ``[srv6]`` / ``[srv6_agent]``, runs the DB migrations and,
if Horizon is enabled, installs the SRv6 panels.

=============================  ==================  ===========================
Variable                       Default             Meaning
=============================  ==================  ===========================
``SRV6_LOCATOR``               required on q-agt   This node's locator; stack
                                                   fails without it.
``SRV6_UNDERLAY_INTERFACE``    auto                Underlay NIC; unset = from
                                                   the route to the peers'
                                                   locators.
``SRV6_DATAPLANE_ENCODING``    ``usid``            ``usid`` or ``encap``
                                                   (server).
``SRV6_FUNCTION_ID_RANGES``    ``0xe000:0xefff``   Domain function ids
                                                   (server). Do not change.
``SRV6_TOPOLOGY_FILE``         unset               Underlay topology for link
                                                   state and automatic
                                                   steering (server).
``SRV6_VRF_TABLE_BASE``        ``0``               VRF table = base + function
                                                   id. Same on every node.
``SRV6_RESYNC_INTERVAL``       ``300``             Seconds between full agent
                                                   resyncs.
``SRV6_DASHBOARD_ENABLE``      all panels          Horizon panel files to
                                                   install.
=============================  ==================  ===========================

Defaults live in ``devstack/settings``.


Example: three nodes
--------------------

Three Ubuntu 24.04 hosts on one L2 segment, DevStack ``stable/2026.2``.
``node1`` is controller + compute (8 GB RAM, 4 vCPUs), ``node2`` and
``node3`` are computes (4 GB). Nested VMs need nested virtualisation or
``LIBVIRT_TYPE=qemu`` (slow).

=======  ===============  ===============  ==================
Host     Management       Underlay         Locator
=======  ===============  ===============  ==================
node1    10.0.0.11        fd00::1/64       ``fc00:0:1::/48``
node2    10.0.0.12        fd00::2/64       ``fc00:0:2::/48``
node3    10.0.0.13        fd00::3/64       ``fc00:0:3::/48``
=======  ===============  ===============  ==================

1. Underlay
~~~~~~~~~~~

On ``node1`` (``eth0`` = your NIC, e.g. ``enp1s0``)::

    sudo apt-get install -y linux-modules-extra-$(uname -r)   # vrf module
    sudo ip -6 addr add fd00::1/64 dev eth0                   # own address
    sudo ip -6 route add fc00:0:2::/48 via fd00::2            # node2 locator
    sudo ip -6 route add fc00:0:3::/48 via fd00::3            # node3 locator

On ``node2`` and ``node3``: same, with their own address and the two other
locators. Then, from every host: ``ping -6 fd00::2`` (and the others).

These commands do not survive a reboot. To persist them, write
``/etc/netplan/60-underlay.yaml`` (mode 600) and run ``sudo netplan apply``::

    network:
      version: 2
      ethernets:
        eth0:                     # same key as in your existing netplan file
          addresses: ["fd00::1/64"]
          routes:
            - {to: "fc00:0:2::/48", via: "fd00::2"}
            - {to: "fc00:0:3::/48", via: "fd00::3"}

2. DevStack
~~~~~~~~~~~

On each host::

    sudo useradd -s /bin/bash -d /opt/stack -m stack
    sudo chmod +x /opt/stack
    echo "stack ALL=(ALL) NOPASSWD: ALL" | sudo tee /etc/sudoers.d/stack
    sudo -u stack -i                                  # next steps as stack
    git clone https://opendev.org/openstack/devstack -b stable/2026.2
    cd devstack                                        # local.conf goes here

3. local.conf on node1 (controller)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

::

    [[local|localrc]]
    HOST_IP=10.0.0.11
    ADMIN_PASSWORD=secret
    DATABASE_PASSWORD=$ADMIN_PASSWORD
    RABBIT_PASSWORD=$ADMIN_PASSWORD
    SERVICE_PASSWORD=$ADMIN_PASSWORD
    MULTI_HOST=1

    # ML2/OVS with VXLAN: the plugin's agent side runs inside the OVS agent
    Q_AGENT=openvswitch
    Q_ML2_PLUGIN_MECHANISM_DRIVERS=openvswitch,l2population
    Q_ML2_PLUGIN_TYPE_DRIVERS=flat,vlan,vxlan
    Q_ML2_TENANT_NETWORK_TYPE=vxlan
    Q_TUNNEL_TYPES=vxlan
    enable_service q-svc q-agt q-dhcp q-l3 q-meta n-cpu
    disable_service ovn-controller ovn-northd q-ovn-metadata-agent

    enable_plugin networking-srv6-agent https://github.com/costantini-ale/networking-srv6-agent.git main
    SRV6_LOCATOR=fc00:0:1::/48

4. local.conf on node2 and node3 (computes)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

On ``node2``; on ``node3`` change ``HOST_IP=10.0.0.13`` and
``SRV6_LOCATOR=fc00:0:3::/48``::

    [[local|localrc]]
    HOST_IP=10.0.0.12
    SERVICE_HOST=10.0.0.11
    MYSQL_HOST=$SERVICE_HOST
    RABBIT_HOST=$SERVICE_HOST
    GLANCE_HOSTPORT=$SERVICE_HOST:9292
    ADMIN_PASSWORD=secret
    DATABASE_PASSWORD=$ADMIN_PASSWORD
    RABBIT_PASSWORD=$ADMIN_PASSWORD
    SERVICE_PASSWORD=$ADMIN_PASSWORD
    MULTI_HOST=1

    Q_AGENT=openvswitch
    Q_ML2_PLUGIN_MECHANISM_DRIVERS=openvswitch,l2population
    Q_ML2_PLUGIN_TYPE_DRIVERS=flat,vlan,vxlan
    Q_ML2_TENANT_NETWORK_TYPE=vxlan
    Q_TUNNEL_TYPES=vxlan
    disable_all_services
    enable_service n-cpu q-agt placement-client

    enable_plugin networking-srv6-agent https://github.com/costantini-ale/networking-srv6-agent.git main
    SRV6_LOCATOR=fc00:0:2::/48

5. Stack
~~~~~~~~

::

    ./stack.sh                          # node1 first, then node2, node3

    # then on node1: register the new computes
    source ~/devstack/openrc admin admin
    nova-manage cell_v2 discover_hosts

6. Check it
~~~~~~~~~~~

On ``node1``, as ``stack``, with ``~/devstack/openrc admin admin`` sourced.

Control plane::

    openstack network agent list --agent-type open-vswitch   # 3 hosts, alive
    openstack srv6 locator list                              # one per host

Two networks in one domain, one VM on each, on two hosts::

    openstack network create net-a
    openstack subnet create --network net-a --subnet-range 10.1.1.0/24 sub-a
    openstack network create net-b
    openstack subnet create --network net-b --subnet-range 10.1.2.0/24 sub-b
    openstack srv6 domain create red
    openstack srv6 network association create red net-a
    openstack srv6 network association create red net-b

    # allow ping and ssh in the default security group
    sg=$(openstack security group list --project admin -f value -c ID -c Name |
         awk '$2 == "default" {print $1}')
    openstack security group rule create --protocol icmp $sg
    openstack security group rule create --protocol tcp --dst-port 22 $sg

    image=$(openstack image list -f value -c Name | grep -m1 cirros)
    openstack server create --image $image --flavor m1.tiny \
        --network net-a --availability-zone nova:node1 vm-a
    openstack server create --image $image --flavor m1.tiny \
        --network net-b --availability-zone nova:node2 vm-b
    openstack server list        # wait for ACTIVE; note both addresses

Ping across the domain. A domain network has no router (so no floating IP):
reach ``vm-a`` from its DHCP namespace (cirros password ``gocubsgo``)::

    sudo ip netns exec qdhcp-$(openstack network show net-a -f value -c id) \
        ssh cirros@<vm-a address>
    $ ping <vm-b address>                       # inside vm-a

The SRv6 state on ``node1``:

=================================================  ===========================
Command                                            Expect
=================================================  ===========================
``ip -br link show type vrf``                      One VRF, for ``red``.
``ip -6 route show table all | grep seg6local``    ``End.DT46`` on
                                                   ``fc00:0:1:...`` (red's
                                                   SID), plus the node's own
                                                   ``End`` routes (one per
                                                   node, always there).
``ip route show table all | grep 'encap seg6 '``   ``vm-b``'s address, encap
                                                   toward a SID in
                                                   ``fc00:0:2::/48``
                                                   (``mode encap.red``).
=================================================  ===========================

A TE path through ``node3`` adds ``node3``'s SID to that route; the ping
still works::

    openstack srv6 te path create --domain red --destination-host node2 \
        --via-host node3


Optional: a fabric for link state and automatic steering
--------------------------------------------------------

Everything above works without this part. It lets underlay switches be TE
waypoints and lets the server move heavy VM pairs off congested links (see
*Link state and automatic steering* in `README.rst <README.rst>`__).

==================  ======================  ==================================
Piece               Where                   Without it
==================  ======================  ==================================
SRv6 switches       every fabric switch     Switches cannot be waypoints.
Topology file       controller              Link state is disabled.
Telemetry agent     every fabric switch     Nothing is measured, so the
                                            server never steers.
==================  ======================  ==================================

Example fabric: two spines, two leaves, the three hosts of the example
above, every link 1 Gbit/s::

         spine1      spine2
           |  \     /  |
           |    \ /    |
           |    / \    |
         leaf1       leaf2
         /   \          \
      node1  node2     node3

``tools/srv6-fabric-lab.py`` builds it as VMs: one small VM per switch
(plain Ubuntu, static routes, its own SID, the telemetry agent), one
isolated libvirt network per link, and a new NIC on each host.

Prerequisites
~~~~~~~~~~~~~

=====================  ======================================================
What                   Details
=====================  ======================================================
Hypervisor             Linux with KVM and libvirt, ideally Ubuntu 24.04,
                       with the packages below. Run the script with
                       ``sudo``.
The hosts as VMs       The DevStack nodes are libvirt VMs on the same
                       hypervisor, each named like its Neutron host
                       (``openstack compute service list``).
Switch image           Ubuntu 24.04 cloud image, about 600 MB (kernel 6.8,
                       iproute2 6.1: ``usid`` works).
Resources              Per switch: 1 vCPU, 1 GB RAM (``--memory``), about
                       2 GB of disk. The example: 4 GB RAM, 8 GB disk.
Management network     libvirt's ``default`` NAT network, active. The
                       switches reach the controller's Neutron through it.
Admin credentials      An openrc-style file for the telemetry agent:
                       ``OS_AUTH_URL``, ``OS_USERNAME``, ``OS_PASSWORD``,
                       ``OS_PROJECT_NAME``.
SSH key (optional)     To log in to the switches as ``stack``.
=====================  ======================================================

On the hypervisor::

    sudo apt-get install -y qemu-kvm libvirt-daemon-system virtinst \
        cloud-image-utils
    wget https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img
    virsh -c qemu:///system net-list --all   # "default" active

1. Topology file
~~~~~~~~~~~~~~~~

The plugin and the script read the same file, ``fabric.topo``. Hosts use
their Neutron host name; switches need a locator in the hosts' /32::

    # node <name> <locator>
    node spine1 fc00:0:101::/48
    node spine2 fc00:0:102::/48
    node leaf1  fc00:0:103::/48
    node leaf2  fc00:0:104::/48
    node node1  fc00:0:1::/48
    node node2  fc00:0:2::/48
    node node3  fc00:0:3::/48
    # link <a> <b> <capacity, Mbit/s per direction>
    link spine1 leaf1 1000
    link spine1 leaf2 1000
    link spine2 leaf1 1000
    link spine2 leaf2 1000
    link leaf1 node1 1000
    link leaf1 node2 1000
    link leaf2 node3 1000

2. Build the fabric
~~~~~~~~~~~~~~~~~~~

On the hypervisor::

    sudo tools/srv6-fabric-lab.py up fabric.topo --hosts node1,node2,node3 \
        --image noble-server-cloudimg-amd64.img \
        --ssh-key ~/.ssh/id_ed25519.pub \
        --neutron-url http://10.0.0.11/networking --credentials admin-openrc

Every node not in ``--hosts`` becomes a switch. Link *i* (file order) is
``fd01:<i>::/64``; each NIC is named after its peer. Routes are static ECMP
over every shortest path. Without ``--neutron-url`` the switches still
route but report nothing. Running ``up`` again only adds what is missing.

3. Configure the hosts
~~~~~~~~~~~~~~~~~~~~~~

The hosts now reach every locator through their leaf. On the hypervisor,
for each host::

    tools/srv6-fabric-lab.py config fabric.topo --hosts node1,node2,node3 \
        node1 | ssh stack@10.0.0.11 'cat > f.sh && sudo sh f.sh install'

It addresses the new NIC, replaces the ``fc00:0:N::/48`` routes of
`1. Underlay`_ (drop them from netplan too) and runs again at every boot.
The agent picks the new NIC by itself unless ``SRV6_UNDERLAY_INTERFACE`` is
set. VXLAN stays on the management network.

4. Point the plugin at the file
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Copy ``fabric.topo`` to the controller, e.g. ``/opt/stack/fabric.topo``.
Before stacking: ``SRV6_TOPOLOGY_FILE=/opt/stack/fabric.topo`` in its
``local.conf``. On a running cloud::

    # [srv6] in /etc/neutron/neutron.conf
    topology_file = /opt/stack/fabric.topo

    sudo systemctl restart devstack@neutron-api devstack@neutron-rpc-server \
        devstack@neutron-periodic-workers

5. Check it
~~~~~~~~~~~

::

    openstack srv6 link list --long        # every link reported, not stale
    openstack srv6 te path create --domain red --destination-host node2 \
        --via-host spine1                  # a switch as a waypoint: ACTIVE
    openstack srv6 te path list --long     # origin=auto: steered by the server

On a switch (``virsh domifaddr leaf1``, then ``ssh stack@<address>``):
``ip -6 route``, ``journalctl -u srv6-fabric-agent``.

6. Remove it
~~~~~~~~~~~~

::

    sudo tools/srv6-fabric-lab.py down fabric.topo --hosts node1,node2,node3

It deletes the switch VMs, their disks, the link networks and the hosts'
extra NICs. On each host, ``sudo systemctl disable srv6-fabric`` and
restore the routes of `1. Underlay`_.

Physical switches
~~~~~~~~~~~~~~~~~

Cable them as the topology file says, then run on each switch the script
from ``tools/srv6-fabric-lab.py config fabric.topo --hosts ... <switch>``
(it finds its NICs by MAC: edit the ``nic`` lines to your MACs). Install
the telemetry agent as the script does for a VM (``user_data`` in it):
``tools/srv6-fabric-agent.py``, a link map ``<interface> <this node>
<neighbour>``, the credentials, and a systemd unit with
``Restart=always``.
