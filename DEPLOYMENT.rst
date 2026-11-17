==========================================
Deploying networking-srv6-agent (DevStack)
==========================================

What a cloud needs to run the plugin, and a worked three-node example. How
the plugin works and how to use it once it runs: ``README.rst``.

.. contents::
   :local:
   :depth: 1


Requirements
------------

**Neutron with ML2/OVS and VXLAN tunnels.** The node side of the plugin is
an extension of the Open vSwitch L2 agent (``neutron-openvswitch-agent``), so
every node that hosts VMs must run that agent. ML2/OVN has no L2 agent and
linuxbridge has a different one: neither is supported. VXLAN still carries
the traffic between VMs of the *same* network; SRv6 only carries the traffic
between networks of a domain.

**A Linux kernel with SRv6 and VRF support, on every node.** The plugin
programs the kernel directly, with no software router in between:

- SRv6 routes (``seg6`` encapsulation and ``seg6local`` SIDs) need
  ``CONFIG_IPV6_SEG6_LWTUNNEL``. Stock Ubuntu and Debian kernels have it.
- Each domain is a Linux VRF, which needs the ``vrf`` kernel module. Ubuntu
  cloud images ship it in a separate package, so install it once on each
  node (the hosts, not the VMs) before stacking:
  ``sudo apt-get install linux-modules-extra-$(uname -r)``. The agent loads
  the module itself.
- The ``usid`` encoding (the default) uses microSID routes that only
  **kernel and iproute2 6.1 or newer** understand (Ubuntu 24.04 or later).
  The agent probes this at start-up; the server logs an error naming any
  node that lacks it. ``encap`` works on older kernels.

**An IPv6 underlay between the nodes.** The *underlay* is the physical
network that connects the OpenStack nodes; SRv6 packets travel over it as
plain IPv6. It can be the same NIC and network that already carries
management or VXLAN traffic. The plugin uses it but never configures it, so
before stacking every node needs:

- **An IPv6 address on the underlay NIC**, any address that the other nodes
  can reach (e.g. ``fd00::1/64``).
- **A locator**: a unique IPv6 prefix for this node, e.g.
  ``fc00:0:1::/48``, set with ``SRV6_LOCATOR``. It is not an interface
  address: the node's SIDs are addresses inside it, and the agent installs
  the routes that answer them. With ``usid`` (the default), every locator
  must be a /48 inside the same /32 block, so the nodes differ only in the
  third group (``fc00:0:1::/48``, ``fc00:0:2::/48``, ...).
- **A route to every other node's locator**, via that node's underlay
  address. When the nodes share one L2 segment, one static route per peer
  is enough (``ip -6 route add fc00:0:2::/48 via fd00::2``, see the example
  below). In a routed fabric, the switches must route the locators instead
  (an IGP or static routes).

The agent finds the underlay NIC from the route to each peer's locator; set
``SRV6_UNDERLAY_INTERFACE`` only to force one. Check the underlay before
stacking: from every node, ``ping -6`` every other node's underlay address.

**An underlay MTU large enough for the extra header.** To carry a VM's
packet to another node, SRv6 puts it inside a new outer IPv6 header, so the
packet on the underlay is bigger than the one the VM sent. The extra bytes
are:

=============  ===============================  ==========================
Encoding       Direct route                     Each TE waypoint adds
=============  ===============================  ==========================
``usid``       40 bytes                         nothing for the first
                                                four, then 16 bytes each
``encap``      64 bytes                         16 bytes
=============  ===============================  ==========================

The rule: **underlay MTU >= network MTU + extra bytes**. A VM sends packets up
to its network's MTU (``openstack network show <net> -c mtu``); the underlay
MTU is the MTU of the underlay NIC (``ip link show <nic>``). Example: on a
1500-byte underlay, Neutron gives VXLAN networks an MTU of 1450, which leaves
50 bytes. That fits ``usid`` (40) but not ``encap`` (64 or more).

When it does not fit, small packets still pass but full-size ones are
dropped, so ping works while large transfers stall. The agent logs a warning
with the largest network MTU that would fit. Fix it either way: raise the
underlay MTU (e.g. jumbo frames, 9000), or lower the network MTU
(``openstack network set --mtu 1400 <net>``).

**Software.** Python 3.11 or newer, and Neutron from the same OpenStack
release as the rest of the cloud (with DevStack: the branch you stack).

DevStack settings
-----------------

Enable the plugin in ``local.conf`` on **every** node (controller and
computes). It installs the package, enables the service plugin and the agent
extension, writes the options into ``neutron.conf`` / ``ml2_conf.ini``, runs
the DB migrations and, if Horizon is enabled, installs the SRv6 panels::

    enable_plugin networking-srv6-agent https://github.com/costantini-ale/networking-srv6-agent.git main
    SRV6_LOCATOR=fc00:0:1::/48        # unique per node

Variables (defaults in ``devstack/settings``):

=============================  ==========================  ================================
Variable                       Default                     Meaning
=============================  ==========================  ================================
``SRV6_LOCATOR``               *(required on q-agt)*       This node's locator; unique per
                                                           node. Stack fails without it.
``SRV6_UNDERLAY_INTERFACE``    auto                        NIC of the IPv6 underlay. Unset:
                                                           found from the route to each
                                                           peer's locator.
``SRV6_DATAPLANE_ENCODING``    ``usid``                    ``usid`` or ``encap`` (server).
``SRV6_FUNCTION_ID_RANGES``    ``0xe000:0xefff``           Pool of domain function ids
                                                           (server); must stay in this
                                                           range.
``SRV6_TOPOLOGY_FILE``         *(unset)*                   Underlay topology for link state
                                                           and automatic steering (server).
``SRV6_VRF_TABLE_BASE``        ``0``                       VRF table = base + function id.
                                                           Same value on every node.
``SRV6_RESYNC_INTERVAL``       ``300``                     Seconds between full agent
                                                           resyncs.
``SRV6_DASHBOARD_ENABLE``      all panels                  Horizon panel files to install.
=============================  ==========================  ================================


Example: three nodes
--------------------

Three Ubuntu 24.04 hosts on one L2 segment. ``node1`` is the controller and
also a compute; ``node2`` and ``node3`` are computes. The host names are the
Neutron host names. Tested with DevStack ``stable/2026.2``, 8 GB RAM and 4
vCPUs for ``node1``, 4 GB for the others. VMs inside VMs need nested
virtualisation, or ``LIBVIRT_TYPE=qemu`` (slow but works).

=======  ===============  ===============  ==================
Host     Management       Underlay         Locator
=======  ===============  ===============  ==================
node1    10.0.0.11        fd00::1/64       ``fc00:0:1::/48``
node2    10.0.0.12        fd00::2/64       ``fc00:0:2::/48``
node3    10.0.0.13        fd00::3/64       ``fc00:0:3::/48``
=======  ===============  ===============  ==================

1. Underlay
~~~~~~~~~~~

On each host, the ``vrf`` kernel module, an underlay address on the shared
NIC (``eth0`` here; use your NIC's name, e.g. ``enp1s0``) and a route to
every other host's locator. On ``node1``::

    sudo apt-get install -y linux-modules-extra-$(uname -r)   # vrf module
    sudo ip -6 addr add fd00::1/64 dev eth0
    sudo ip -6 route add fc00:0:2::/48 via fd00::2
    sudo ip -6 route add fc00:0:3::/48 via fd00::3

and the same on ``node2`` and ``node3`` with their own address and the two
other locators. These commands do not survive a reboot. On Ubuntu, make them
persistent with a netplan file, ``/etc/netplan/60-underlay.yaml`` (mode
600, then ``sudo netplan apply``)::

    network:
      version: 2
      ethernets:
        eth0:
          addresses: ["fd00::1/64"]
          routes:
            - {to: "fc00:0:2::/48", via: "fd00::2"}
            - {to: "fc00:0:3::/48", via: "fd00::3"}

The key under ``ethernets`` must match the one in your existing netplan
file (cloud images often use another id than the NIC name). Check from every
host before stacking: ``ping -6 fd00::2`` and so on. The agent loads ``vrf``
and sets the SRv6 sysctls (``seg6_enabled``, forwarding) itself.

2. DevStack
~~~~~~~~~~~

On each host, a ``stack`` user with passwordless sudo, and DevStack cloned
into its home::

    sudo useradd -s /bin/bash -d /opt/stack -m stack
    sudo chmod +x /opt/stack
    echo "stack ALL=(ALL) NOPASSWD: ALL" | sudo tee /etc/sudoers.d/stack
    sudo -u stack -i
    git clone https://opendev.org/openstack/devstack -b stable/2026.2
    cd devstack

The ``local.conf`` below goes in this ``devstack`` directory, and every
following command runs as ``stack``.

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

On ``node2`` (``node3``: ``HOST_IP=10.0.0.13``,
``SRV6_LOCATOR=fc00:0:3::/48``)::

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

Run ``./stack.sh`` on ``node1`` first, then on ``node2`` and ``node3``. Then,
on ``node1``, register the new computes::

    source ~/devstack/openrc admin admin
    nova-manage cell_v2 discover_hosts

6. Check it
~~~~~~~~~~~

Everything below runs on ``node1`` as ``stack``, in a shell where
``~/devstack/openrc admin admin`` is sourced. The control plane::

    openstack network agent list --agent-type open-vswitch   # 3 hosts, alive
    openstack srv6 locator list                             # one per host

Two networks in one domain, with a VM on each, on two different hosts::

    openstack network create net-a
    openstack subnet create --network net-a --subnet-range 10.1.1.0/24 sub-a
    openstack network create net-b
    openstack subnet create --network net-b --subnet-range 10.1.2.0/24 sub-b
    openstack srv6 domain create red
    openstack srv6 network association create red net-a
    openstack srv6 network association create red net-b

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

A domain network has no router, so no floating IP. To reach ``vm-a``, use
SSH from its network's DHCP namespace on ``node1`` (cirros password:
``gocubsgo``), then ping ``vm-b``::

    sudo ip netns exec qdhcp-$(openstack network show net-a -f value -c id) \
        ssh cirros@<vm-a address>
    $ ping <vm-b address>

The SRv6 state behind it, on ``node1``::

    ip -br link show type vrf                     # one VRF for domain red
    ip -6 route show table all | grep seg6local   # red's SID: decap into it
    ip route show table all | grep 'encap seg6 '  # vm-b's route: encap to node2

Expect one VRF, one ``End.DT46`` route on ``fc00:0:1:...``, and a route to
``vm-b``'s address that is encapsulated toward a SID in ``fc00:0:2::/48``
(``mode encap.red``). The ``seg6local`` list also shows the node's own
``End`` routes, on the locator and on ``fc00:0:1:fff0::``: they are always
there, one per node, not per domain.
A TE path through ``node3`` (``openstack srv6 te path create --domain red
--destination-host node2 --via-host node3``) adds ``node3``'s SID to that
route, and the ping still works.


Optional: a fabric for link state and automatic steering
--------------------------------------------------------

Everything above works without this part. It lets underlay switches be TE
waypoints and gives the server the load of each link, so that it can move
heavy VM pairs off congested links (see *Link state and automatic steering*
in ``README.rst``). It needs three things: switches that forward SRv6, a
topology file on the controller, and the telemetry agent on every switch.

The example fabric: two spines, two leaves, and the three hosts behind the
leaves, each link 1 Gbit/s::

         spine1      spine2
           |  \     /  |
           |    \ /    |
           |    / \    |
         leaf1       leaf2
         /   \          \
      node1  node2     node3

Switches
~~~~~~~~

Any Linux router works (SONiC, or Linux VMs). Each switch needs a locator in
the same /32 block, IPv6 forwarding, and its own SID; every locator must be
reachable from every node (an IGP, or static ECMP routes). The hosts reach
the locators through their leaf instead of the shared segment. On ``leaf1``
(locator ``fc00:0:103::/48``)::

    sudo sysctl -w net.ipv6.conf.all.forwarding=1
    sudo sysctl -w net.ipv6.conf.all.seg6_enabled=1   # and on every fabric NIC
    # the SIDs sit on a dummy interface: it never goes down
    sudo ip link add sr0 type dummy && sudo ip link set sr0 up
    # usid: the switch's uN
    sudo ip -6 route add fc00:0:103::/48 encap seg6local action End \
        flavors next-csid lblen 32 nflen 16 dev sr0
    # encap: the switch's End SID, <locator>:fff0::
    sudo ip -6 route add fc00:0:103:fff0::/128 encap seg6local action End \
        dev sr0

Topology file
~~~~~~~~~~~~~

On ``node1``, with ``SRV6_TOPOLOGY_FILE=/opt/stack/srv6-topology`` in its
``local.conf``. Hosts use their Neutron host name; capacity is Mbit/s per
direction::

    node spine1 fc00:0:101::/48
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

Telemetry agent
~~~~~~~~~~~~~~~

Copy ``tools/srv6-fabric-agent.py`` to every switch (plain Python 3, no
dependencies) and run it as a service with ``CAP_NET_ADMIN``. It needs a
link map, one line per neighbour (``<interface> <this node> <neighbour>``,
names as in the topology file). For ``leaf1``::

    eth1 leaf1 spine1
    eth2 leaf1 spine2
    eth3 leaf1 node1
    eth4 leaf1 node2

and admin credentials in an openrc-style file (``OS_AUTH_URL``,
``OS_USERNAME``, ``OS_PASSWORD``, ``OS_PROJECT_NAME``)::

    srv6-fabric-agent.py --links links.txt --period 5 --aggregates \
        --neutron-url http://10.0.0.11/networking --credentials admin-openrc

``--neutron-url`` is the network endpoint (``openstack endpoint list
--service network``); ``srv6-fabric-agent.py test`` runs a self-check.

Check it
~~~~~~~~

::

    openstack srv6 link list --long        # every link reported, not stale
    openstack srv6 te path create --domain red --destination-host node2 \
        --via-host spine1                  # a switch as a waypoint: ACTIVE
    openstack srv6 te path list --long     # origin=auto: steered by the server
