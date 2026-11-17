=====================
networking-srv6-agent
=====================

SRv6 connectivity and traffic engineering for OpenStack Neutron (ML2/OVS).

.. contents::
   :local:
   :depth: 1


What it does
------------

Neutron routes the traffic between a project's networks with a router, in
one of three modes: *legacy* (a router namespace on a network node), *DVR*
(a copy of the router on every compute node) or *OVN* (logical flows in Open
vSwitch). All three carry packets between nodes in VXLAN or Geneve tunnels,
and none supports Segment Routing over IPv6 (SRv6).

This plugin adds SRv6 as a fourth routing mode for the traffic between a
project's networks. It builds no new data plane: the Linux kernel of every
node already implements SRv6, and the plugin connects it to the Open vSwitch
agent that already wires the VMs, so traffic leaving a tenant network is
routed by the kernel and carried over the IPv6 underlay in SRv6. Routing is
distributed: the node of the sender encapsulates every packet straight
toward the destination node, with no network node in the path. Because the
sender writes the path into the packet, the plugin can also steer traffic
through chosen waypoints, which no other mode allows. It is a standalone
package: Neutron is not modified, VMs need no configuration and keep using
their subnet's gateway, and networks outside a domain behave as before.

- **Domains.** Networks grouped into an SRv6 domain reach each other, as if
  attached to one router. Domains are isolated from each other, even with
  overlapping addresses.
- **Two encodings.** ``usid`` (RFC 9800 microSID, default): a direct route
  costs a 40-byte outer header, and up to four waypoints still fit in it.
  ``encap``: full 128-bit SIDs in a Segment Routing Header (SRH), 64 bytes.
- **Traffic engineering (admin).** A TE path forces part of a domain's
  traffic through chosen waypoints: other compute nodes or, with a topology
  file, fabric switches.
- **Automatic steering (optional).** With a topology file and the telemetry
  agent on the fabric switches, the server moves heavy VM pairs off
  congested links on its own.
- **Tools.** An ``openstack srv6 ...`` CLI plugin and Horizon panels.

Out of scope: north-south traffic (a domain network has no router, so no
floating IP) and traffic between domains.


How it works
------------

SRv6 in one paragraph
~~~~~~~~~~~~~~~~~~~~~

With Segment Routing the source node writes in the packet a list of
*segments*, instructions that the nodes along the path execute; the
per-flow state lives only at the source. In SRv6 each segment identifier
(*SID*) is an IPv6 address made of a **locator**, a prefix that routes to
the node owning the SID, and a **function**, which selects what that node
does with the packet. Transit routers only see an IPv6 destination address,
so they forward SRv6 packets as plain IPv6. In Linux, SRv6 is configured
with ordinary routes: ``encap seg6`` wraps matching packets in an outer IPv6
header toward a SID, and ``encap seg6local`` binds a SID to a behaviour,
such as ``End`` (go to the next segment) or ``End.DT46`` (remove the outer
header and look the inner packet up in a given routing table).

Resources
~~~~~~~~~

The plugin adds four resources to the Neutron API::

    Neutron network 1 --- 0..1 network association * --- 1 SRv6 domain
                                                              | 1
                                                              | *
    compute node 1 --- 0..1 locator * --- waypoints --- * TE path

**SRv6 domain** -- takes the place of a Neutron router: a set of networks of
one project between which traffic is routed over SRv6. At creation the
server gives it a **function id** from a pool (``0xe000``-``0xefff``); all
its SIDs and its routing table on every node derive from it.

**Network association** -- joins a network to a domain, as a router
interface joins a network to a router. Accepted only if the network belongs
to the domain's project, is not external, has no router interface, is in no
other domain and has no subnet overlapping the domain's (they all share one
routing table). Neutron callbacks keep these rules true afterwards.

**Locator** -- the /48 prefix of a node (e.g. ``fc00:0:2::/48``), set in its
agent's configuration and reported to the server. With ``usid`` all
locators share one /32 block and the third group names the node:
``0x0001``-``0xdfff`` are nodes, ``0xe000``-``0xefff`` function ids, so a
domain's function is never mistaken for the next node of a path. A domain's
SID on a node is the node's locator plus the domain's function id.

**TE path** (admin only) -- the waypoints, compute nodes or fabric switches,
that selected traffic must cross. It selects traffic by destination node or
VM, optionally restricted to a source node or VM; the most specific match
wins. It is written by the admin, or created by the server to relieve a
congested link. When a waypoint has no locator or is down, the path is
marked ``DEGRADED`` and its traffic falls back to the direct route.

Architecture
~~~~~~~~~~~~

Every component attaches to an existing Neutron extension point. The server
takes every routing decision; each compute node applies it::

         CLI, Horizon             fabric switches (telemetry agent)
               | REST                       | REST
    +----------v----------------------------v----------------------------+
    | controller: neutron-server                                         |
    |              SRv6 API extensions and policies                      |
    |                          |                                         |
    |   ML2, L3 --callbacks--> Srv6Plugin --+-- TE re-optimiser          |
    |                          |            +-- liveness monitor         |
    |                      Neutron DB                                    |
    +-----------------------------------+---------------^----------------+
                          fanout casts  |               | sync_state
    ====================================v== message bus =|================
                   |                                     |
    +--------------v---------------+      +--------------v---------------+
    | compute node                 |      | compute node                 |
    |  neutron-openvswitch-agent   |      |  neutron-openvswitch-agent   |
    |  + SRv6 extension -> br-int  |      |  + SRv6 extension -> br-int  |
    |          | privsep           |      |          | privsep           |
    |  kernel: VRFs, SRv6 routes,  |      |  kernel: VRFs, SRv6 routes,  |
    |  rules, nftables             |      |  rules, nftables             |
    +------------------------------+      +------------------------------+

**Server.** Five API extensions add the domains with their associations, the
locators, the TE paths, and two read-only collections of fabric
measurements (load per link, traffic per VM pair). Domains and associations
belong to the project's members, the rest to the admin. The state lives in
new tables of the Neutron database, created by ``neutron-db-manage``; unique
constraints enforce the main rules, so two concurrent requests cannot
bypass them. ``Srv6Plugin`` implements the API. For each domain it builds a
**payload** with the complete state the nodes need: the function id and
routing table, each network with its MTU and subnets, the locator of every
node, and one route per address of each active VM, naming the node that
hosts it. When a TE path applies to a route, its waypoints are attached to
that route as a list of SIDs. Two periodic tasks run beside it: the
**liveness monitor** marks TE paths ``DEGRADED`` when a waypoint goes down,
and the optional **TE re-optimiser** reads the link load posted by the
fabric switches and, when a link stays congested, moves the heaviest VM pair
onto a less loaded path (an automatic TE path, removed when the pair goes
idle or the link has room again).

**Messaging.** After every committed change the server sends a fanout cast
to all agents: the whole payload of a domain when the domain changes
(``domain_updated``), only the changed routes when a VM starts, stops or
moves (``routes_updated``). Messages can be lost or arrive out of order, so
each agent also calls ``sync_state`` at start-up and every
``resync_interval`` (300 s), which returns every payload: a node cannot
drift from the server for longer than one period.

**Node.** On each compute node, the SRv6 extension of the Open vSwitch agent
keeps the last payload of every domain and makes the kernel match it,
through a privsep daemon. A domain is built only on a node that hosts one of
its VMs.

Data plane
~~~~~~~~~~

Take domain ``red`` with function id ``0xe005`` (routing table 57349), on
node 2 (locator ``fc00:0:2::/48``), which hosts ``vm-a2`` (``10.50.1.12`` on
``net-a``) and talks to ``vm-b3`` (``10.50.2.13`` on ``net-b``) on node 3.
The agent installs the equivalent of::

    # VRF and its seal
    ip link add sv6vrf-57349 type vrf table 57349
    ip -4 route add unreachable default metric 4278198272 table 57349
    ip -6 route add unreachable default metric 4278198272 table 57349
    # decapsulation SID: locator + function id
    ip -6 route add fc00:0:2:e005::/64 encap seg6local \
        action End.DT46 vrftable 57349 dev sv6vrf-57349
    # gateway port of net-a (local VLAN 1) inside the VRF
    ovs-vsctl add-port br-int svp-57349-1 tag=1 \
        -- set interface svp-57349-1 type=internal
    ip link set svp-57349-1 master sv6vrf-57349
    ip addr add 10.50.1.1/24 dev svp-57349-1
    # route to a remote VM, and its rule
    ip route add 10.50.2.13/32 encap seg6 mode encap.red \
        segs fc00:0:3:e005:: dev eth1 table 57349
    ip -6 rule add pref 999 iif sv6vrf-57349 \
        to fc00:0:3:e005::/128 lookup main

These are five kinds of kernel objects per domain:

- The **VRF** is the domain's routing table on the node. It is closed by a
  **seal**, an unreachable default route, so a lookup that finds nothing in
  the VRF ends there instead of falling through to the host's main table.
- The **decapsulation SID** (``End.DT46``) removes the outer header of an
  arriving packet and looks the inner packet up in the VRF.
- A **gateway port** is an Open vSwitch internal port on ``br-int``, tagged
  with the network's local VLAN and placed in the VRF, holding the gateway
  address of each subnet. It plays the router interface: every node hosting
  ``net-a`` has its own ``10.50.1.1``, as in DVR.
- Each remote VM gets a /32 or /128 **route** in the VRF that wraps the
  packet in an outer IPv6 header addressed to the decapsulation SID on that
  VM's node. With a single segment no SRH is needed, so the encapsulation
  is just the 40-byte IPv6 header. Routes are per VM, not per subnet,
  because VMs of one subnet can sit on different nodes; it also lets a TE
  path steer a single VM.
- The **policy rule** at priority 999 sends the encapsulated packet to the
  main table, where the underlay route forwards it. Without it the kernel
  would look the outer packet up in the VRF again, hit the seal and drop it.

So ``vm-a2`` sends to its gateway ``10.50.1.1``; the VRF on node 2 matches
the route to ``10.50.2.13`` and encapsulates toward ``fc00:0:3:e005::``; the
underlay delivers it to node 3, where ``End.DT46`` puts the inner packet
into ``red``'s VRF, and the connected route of ``net-b`` hands it to
``vm-b3``. No router namespace or L3 agent is involved.

Steering through waypoints
~~~~~~~~~~~~~~~~~~~~~~~~~~

Every node that may be a waypoint installs a **uN SID** on its whole
locator (``End`` with the ``next-csid`` flavour). It never decapsulates, so
compute nodes without VMs and fabric switches can both serve. To send
``vm-a2``'s traffic to ``vm-b3`` through node 1, node 2 only changes the
route's destination to the container ``fc00:0:1:3:e005::``: the 32-bit
block, the micro-SIDs of node 1 and node 3, and the function. The underlay
routes it to node 1, whose uN shifts the micro-SIDs left by 16 bits and
forwards the packet to ``fc00:0:3:e005::``. The destination takes two of
the six micro-SIDs in a container, so up to four waypoints cost no extra
byte; a longer path moves the rest into an SRH. A TE path for a single
source VM uses a dedicated *steer table*, selected by a rule on the source
address and gateway port.

Isolation between domains
~~~~~~~~~~~~~~~~~~~~~~~~~

The VRF separates domains, even with overlapping addresses, and the agent
closes the three other ways out of a domain:

- A packet to an address the VRF does not know ends at the seal instead of
  continuing in the host's main table.
- A packet to an address of the host itself would be delivered by the
  ``local`` table, which Linux consults first; the agent moves that rule
  after the VRF rule, so the packet reaches the seal while host traffic
  still uses ``local``.
- A VM could craft an SRv6 packet addressed to another domain's SID. Like
  the edge filtering of RFC 8754, an nftables table drops, before routing,
  every packet from a gateway port that is addressed to a node SID or
  carries an SRH.


Repository layout
-----------------

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
  (link load and heavy VM pairs).


Deploying
---------

The plugin needs ML2/OVS with VXLAN, a Linux kernel with SRv6 and VRF
support (6.1 or newer for ``usid``), and an IPv6 underlay that routes every
node's locator. ``DEPLOYMENT.rst`` explains each requirement, lists the
DevStack settings and walks through a three-node example, plus the optional
fabric for link state and automatic steering.


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
source + port > source + host > port > host > direct. A path whose source
and destination ports are on the same network is refused (that traffic
never enters the domain).

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
link state and automatic steering* in ``DEPLOYMENT.rst``. Watch it::

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
