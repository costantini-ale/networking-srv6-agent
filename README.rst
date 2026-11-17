=====================
networking-srv6-agent
=====================

SRv6 connectivity and traffic engineering for OpenStack Neutron (ML2/OVS).

.. contents::
   :local:
   :depth: 1


What it does
------------

The plugin gives a project's networks SRv6 connectivity and traffic
engineering in place of the Neutron router. Networks grouped into an **SRv6
domain** reach each other over the IPv6 underlay with Segment Routing over
IPv6 (SRv6), routed on each compute node with no network node in the path,
and an admin can steer that traffic along chosen paths.

- **Domains.** A domain is a routing context, like a VRF. Every node that
  hosts a port of the domain gets a Linux VRF, a local gateway for each
  attached network, and one SID that decapsulates traffic into that VRF
  (``End.DT46``). Domains are isolated from each other.
- **Distributed routing.** Each node routes the packets of its own VMs and
  encapsulates them directly toward the destination node's SID.
- **Two encodings.** ``usid`` (RFC 9800 microSID, default): a path of up to
  four waypoints fits in a 40-byte outer header with no SRH. ``encap``: full
  128-bit SIDs in an SRH.
- **Traffic engineering (admin).** A TE path forces a domain's traffic through
  chosen waypoints (other hosts or, with a topology file, fabric switches).
  It can match all traffic toward a host, only traffic toward one port,
  and/or only traffic entering at one host or from one VM. A path through a
  dead waypoint is marked ``DEGRADED`` and falls back to the direct route.
- **Automatic steering (optional).** With an underlay topology file and the
  telemetry agent running on the fabric switches, the server watches link
  load and moves heavy VM pairs off congested links on its own, choosing
  their new path with a built-in path computation (CSPF).
- **Tools.** An ``openstack srv6 ...`` CLI plugin and Horizon panels
  (project: domains and associations; admin: locators, TE paths, topology).

How it is built
~~~~~~~~~~~~~~~

The plugin has two halves. On the controller, the ``srv6`` **service
plugin** inside ``neutron-server`` owns the API and the database, allocates
SIDs, resolves TE paths and tells the agents what to install; it programs no
kernel state itself. On every node, the ``srv6`` **agent extension** inside
``neutron-openvswitch-agent`` installs the VRFs, SIDs, routes and edge filter
in the Linux kernel.

Repository layout:

``networking_srv6_agent/`` -- the Neutron plugin (Python package)
  - ``services/``: the service plugin. ``plugin.py`` is the entry point
    (API calls, SID allocation, validation); ``rpc.py`` talks to the agents;
    ``te_resolver.py`` decides which path each TE path really takes;
    ``te_liveness.py`` re-pushes paths when a waypoint dies or comes back;
    ``pce.py`` is the path computation (CSPF); ``workload.py`` finds the VM
    ports and their hosts.
  - ``services/underlay/``: the optional automatic steering. It stores the
    link load and heavy VM pairs sent by the telemetry agents and moves
    pairs off congested links.
  - ``agent/``: the agent extension. ``agent_extension.py`` reacts to port and
    domain events; ``dataplane.py`` turns them into kernel state.
  - ``privileged/``: the few operations that need root (``ip`` route and
    link commands, ``modprobe vrf``, ``nft`` rulesets), run through privsep.
  - ``api/definitions/`` and ``extensions/``: the REST resources (domains,
    associations, locators, TE paths, link state, aggregates).
  - ``db/``: database models and queries; ``db/migration/`` holds the
    Alembic migrations.
  - ``policies/``: who may call each API (oslo.policy rules).
  - ``cli/``: the ``openstack srv6 ...`` commands (OpenStackClient plugin).
  - ``common/``: configuration options and SID arithmetic.
  - ``tests/unit/``: unit tests (``stestr``).

``srv6_dashboard/`` -- the Horizon panels
  ``dashboards/project/`` (SRv6 domains, network topology with domains),
  ``dashboards/admin/`` (SRv6 overview, underlay topology), ``api/`` (calls to
  the Neutron API), ``enabled/`` (the files that register the panels in
  Horizon), ``test/`` (dashboard tests).

``devstack/`` -- the DevStack plugin
  ``plugin.sh`` installs and configures everything on a node; ``settings``
  holds the ``SRV6_*`` variables and their defaults.

``tools/``
  ``srv6-fabric-agent.py``, the optional telemetry agent for underlay switches
  (link load and heavy VM pairs); ``django-manage.py``, used to run the
  dashboard tests.

Each node announces its **locator** (an IPv6 prefix, e.g. ``fc00:0:1::/48``);
a domain's SID on that node is the locator plus the domain's function id.


Deploying
---------

What a cloud needs to run the plugin, and a worked three-node example.

Requirements
~~~~~~~~~~~~

- **ML2/OVS with VXLAN tunnels.** The agent side is an extension of the OVS
  L2 agent. ML2/OVN (no L2 agent) and linuxbridge are not supported.
- Linux kernel with ``CONFIG_IPV6_SEG6_LWTUNNEL`` and the ``vrf`` module
  (on Ubuntu cloud images: ``linux-modules-extra-$(uname -r)``). For
  ``usid``: kernel and iproute2 >= 6.1.
- An **IPv6-routable underlay** between all nodes: each node's locator must
  be reachable from every other node. With ``usid`` all locators are /48 in
  one /32 block. The underlay is yours to build; the plugin only uses it.
- Python >= 3.11; Neutron from the same OpenStack release as DevStack.

DevStack settings
~~~~~~~~~~~~~~~~~

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
~~~~~~~~~~~~~~~~~~~~

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
^^^^^^^^^^^

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
^^^^^^^^^^^

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
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

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
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

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
^^^^^^^^

Run ``./stack.sh`` on ``node1`` first, then on ``node2`` and ``node3``. Then,
on ``node1``, register the new computes::

    source ~/devstack/openrc admin admin
    nova-manage cell_v2 discover_hosts

6. Check it
^^^^^^^^^^^

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
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Everything above works without this part. It lets underlay switches be TE
waypoints and gives the server the load of each link, so that it can move
heavy VM pairs off congested links (see *Link state and automatic steering*
under *Using it*). It needs three things: switches that forward SRv6, a
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
^^^^^^^^

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
^^^^^^^^^^^^^

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
^^^^^^^^^^^^^^^

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
^^^^^^^^

::

    openstack srv6 link list --long        # every link reported, not stale
    openstack srv6 te path create --domain red --destination-host node2 \
        --via-host spine1                  # a switch as a waypoint: ACTIVE
    openstack srv6 te path list --long     # origin=auto: steered by the server


Using it
--------

Connect networks
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

::

    openstack network create net-a
    openstack subnet create --network net-a --subnet-range 10.0.1.0/24 sub-a
    openstack network create net-b
    openstack subnet create --network net-b --subnet-range 10.0.2.0/24 sub-b

    openstack srv6 domain create red
    openstack srv6 network association create red net-a
    openstack srv6 network association create red net-b

    # one VM per network, pinned to two different compute nodes (admin)
    openstack server create --image cirros --flavor m1.tiny \
        --network net-a --availability-zone nova:node1 vm-a
    openstack server create --image cirros --flavor m1.tiny \
        --network net-b --availability-zone nova:node2 vm-b

``--availability-zone nova:<host>`` places a VM on a given compute node
(``openstack compute service list`` shows the host names); without it the
scheduler picks the node. The VMs reach each other through their subnet
gateway, which now lives on every node that hosts one of them. Removing
an association or deleting the domain removes the state from every node.

Rules an association must follow (the API refuses otherwise):

- the network belongs to the domain's project and is not external;
- the network has **no router interface** (and a router interface cannot be
  added later): a network is routed either by a Neutron router or by a domain;
- a network belongs to at most one domain;
- subnets in one domain must not overlap.

A domain routes traffic *between* its networks only. Traffic between two VMs
of the same network stays on L2 (VXLAN) and never uses SRv6.

Inspect::

    openstack srv6 domain list --long
    openstack srv6 domain show red        # function id, SIDs
    openstack srv6 network association list red

Traffic engineering (admin)
~~~~~~~~~~~~~~~~~~~~~~~~~~~

::

    # all of red's traffic toward node2 goes through node3
    openstack srv6 te path create --domain red --destination-host node2 \
        --via-host node3

    # only the traffic entering the domain at node1
    openstack srv6 te path create --domain red --destination-host node2 \
        --source-host node1 --via-host node3

    # only toward one VM port, or only from one VM port
    openstack srv6 te path create --domain red --destination-port vm-b-port \
        --via-host node3 --via-host node4
    openstack srv6 te path create --domain red --destination-host node2 \
        --source-port vm-a-port --via-host node4

    openstack srv6 te path list --domain red --long
    openstack srv6 te path set <id> --via-host node4   # re-steer
    openstack srv6 te path set <id> --no-via-host      # explicitly direct
    openstack srv6 te path delete <id>

``--via-host`` can be repeated; waypoints are hosts, or fabric nodes named in
the topology file. When several paths match, the most specific wins:
source + port > source + host > port > host > direct. A path whose source and destination ports are on the same
network is refused (that traffic never enters the domain).

Link state and automatic steering (admin, optional)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Everything above works without this part. It adds a feedback loop on the
underlay: telemetry agents on the fabric switches report link load and the
heaviest VM pairs; every ``te_reopt_interval`` the server looks for links
loaded above ``te_reopt_high`` and gives the largest pairs on them a path of
their own (``origin=auto``). The path is chosen by the built-in **path
computation** (CSPF: fewest hops over links with room, then the widest
bottleneck) and released when the pair goes idle or the link has room
again. Manual TE paths never use it: their waypoints are the ones you give.

It needs a topology file on the controller (without it, link state is
disabled and fabric nodes cannot be waypoints) and the telemetry agent on
every fabric switch (without it there is nothing to measure, so the server
never steers anything). How to set both up: *Optional: a fabric for
link state and automatic steering* under *Deploying*. Watch it::

    openstack srv6 link list --long        # load per link, stale flag
    openstack srv6 aggregate list --long   # heavy VM pairs per link
    openstack srv6 te path list --long     # origin=auto: steered by server

Thresholds and limits are the ``te_reopt_*`` and ``te_auto_*`` options in
``[srv6]`` (``oslo-config-generator`` lists them all with help text).

Horizon
~~~~~~~

- *Project → Network → SRv6 Domains*: create domains, associate networks.
- *Project → Network → Network Topology*: replaced by a version that shows
  domains.
- *Admin → Network → SRv6*: all domains, locators, TE paths.
- *Admin → Network → SRv6 Topology*: the underlay graph with live link load.

``openstack command list --group openstack.srv6.v2`` lists every CLI command.


Configuration reference
-----------------------

Main options; see ``networking_srv6_agent/common/config.py`` for all of them.

``[srv6]`` in ``neutron.conf`` (server)
  ``dataplane_encoding`` (``usid``), ``function_id_ranges``
  (``0xe000:0xefff``), ``vrf_table_base`` (``0``), ``te_liveness_interval``
  (``15`` s; failover time ≈ Neutron's ``agent_down_time`` + this),
  ``topology_file``, ``te_reopt_interval`` (``10`` s, ``0`` = off),
  ``te_auto_steering`` (``true``).

``[srv6_agent]`` in the OVS agent config (every node)
  ``locator`` (required), ``underlay_interface`` (auto), ``resync_interval``
  (``300`` s), ``apply_sysctls`` (``true``; set ``false`` if config
  management owns ``seg6_enabled``, ``forwarding``, ``rp_filter``…).

``[quotas]``
  ``quota_srv6_domain`` (``10`` per project; ``-1`` unlimited). All projects
  share the 4096 function ids.

Changing ``dataplane_encoding`` requires a deployment with no domains:
restart the server, then the agents.


License: Apache 2.0.
