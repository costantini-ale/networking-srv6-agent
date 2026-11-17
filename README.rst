=====================
networking-srv6-agent
=====================

SRv6 connectivity and traffic engineering for OpenStack Neutron (ML2/OVS).

.. contents::
   :local:
   :depth: 1


What it does
------------

The plugin adds a new Neutron routing mode for the traffic between a
project's networks, based on Segment Routing over IPv6 (SRv6). It bridges
the Linux kernel, which already implements SRv6, with the Open vSwitch agent
of every node. It is a standalone package: Neutron is not modified, VMs need
no configuration and keep using their subnet's gateway, and networks outside
a domain behave as before.

Concepts
~~~~~~~~

.. image:: doc/images/resources.png
   :alt: Resources added to the Neutron API: a network has at most one
         network association, which belongs to one SRv6 domain; a domain
         has TE paths, whose waypoints are locators, one per compute node.
   :width: 460

=======================  =====================================================
Resource                 Meaning
=======================  =====================================================
SRv6 domain              A set of networks of one project, routed to each
                         other over SRv6. Gets a function id
                         (``0xe000``-``0xefff``) from which its SIDs and its
                         routing table on every node derive.
Network association      Joins a network to a domain, like a router interface
                         joins a network to a router.
Locator                  A node's /48 prefix (e.g. ``fc00:0:2::/48``), set in
                         the agent config. A domain's SID on a node is the
                         locator plus the domain's function id.
TE path (admin)          Waypoints (compute nodes or fabric switches) that
                         part of a domain's traffic must cross. Written by
                         the admin, or created by the server to relieve a
                         congested link.
=======================  =====================================================


Architecture
------------

.. image:: doc/images/architecture.png
   :alt: Components of the plugin: on the controller, neutron-server with
         the SRv6 API extensions, Srv6Plugin, the TE re-optimiser and the
         liveness monitor; a message bus; on each compute node, the OVS
         agent with the SRv6 extension programming the kernel via privsep.
   :width: 520

======================  ===============  =====================================
Component               Runs in          Role
======================  ===============  =====================================
API extensions          neutron-server   REST resources and policies.
                                         ``extensions/``, ``api/``,
                                         ``policies/``
``Srv6Plugin``          neutron-server   Validates requests, allocates
                                         function ids, builds each domain's
                                         payload, notifies the agents.
                                         ``services/plugin.py``
TE resolver             neutron-server   Turns TE paths into SID lists per
                                         route. ``services/te_resolver.py``
Liveness monitor        neutron-server   Marks TE paths ``DEGRADED`` when a
                                         waypoint dies, active when it is back.
                                         ``services/te_liveness.py``
TE re-optimiser         neutron-server   Optional: moves heavy VM pairs off
                                         congested links.
                                         ``services/underlay/``
Agent extension         OVS agent,       Applies the payloads to the kernel.
                        every node       ``agent/``
Privileged helpers      privsep daemon   ``ip``, ``modprobe``, ``nft`` as
                                         root. ``privileged/``
Fabric agent            fabric switches  Optional: posts link load and heavy
                                         VM pairs.
                                         ``tools/srv6-fabric-agent.py``
======================  ===============  =====================================

How a change reaches the nodes:

1. An API call (or a VM starting, stopping or moving) changes the state in
   the Neutron database.
2. ``Srv6Plugin`` builds the domain's **payload**: function id, networks
   with their subnets and MTU, every node's locator, and one route per VM
   address naming the node that hosts it, with the SID list of any TE path.
3. The server sends a fanout cast to every agent: ``domain_updated`` (the
   whole payload) or ``routes_updated`` (only the changed routes).
4. Each agent makes its kernel match the payload. Domains are built only on
   nodes that host one of their VMs.
5. Every agent also calls ``sync_state`` at start-up and every
   ``resync_interval`` (300 s) to repair lost or reordered messages.

What the agent installs
~~~~~~~~~~~~~~~~~~~~~~~

For domain ``red`` (function id ``0xe005``, table 57349) on node 2 (locator
``fc00:0:2::/48``), with ``vm-a2`` on ``net-a`` and a remote ``vm-b3``
(``10.50.2.13``) on node 3, the agent installs the equivalent of::

    # VRF, sealed by an unreachable default
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
    # route to a remote VM, and the rule that sends it to the underlay
    ip route add 10.50.2.13/32 encap seg6 mode encap.red \
        segs fc00:0:3:e005:: dev eth1 table 57349
    ip -6 rule add pref 999 iif sv6vrf-57349 \
        to fc00:0:3:e005::/128 lookup main

- **VRF** (``sv6vrf-<function id>``): the domain's routing table. The
  *seal* ends lookups that find nothing, so traffic never leaks into the
  host table.
- **Decapsulation SID** (``End.DT46``): strips the outer header of arriving
  packets and looks them up in the VRF.
- **Gateway port** (``svp-<function id>-<vlan>``): an OVS internal port
  holding the subnet gateway address. Every node hosting the network has
  its own.
- **Route per remote VM**: encapsulates toward the decapsulation SID on that
  VM's node. Per VM, not per subnet, so a TE path can steer one VM.
- **Rule 999**: sends the encapsulated packet to the main table, where the
  underlay route forwards it.

Waypoints and isolation
~~~~~~~~~~~~~~~~~~~~~~~

- **Waypoints.** Every node installs a uN SID (``End`` with ``next-csid``) on
  its locator, so any node or fabric switch can be a waypoint. A TE path
  only changes the route's destination, e.g. ``fc00:0:1:3:e005::`` goes
  through node 1 to node 3. Up to four waypoints fit in the 40-byte header;
  a longer path adds an SRH.
- **Isolation.** The VRF separates domains, even with overlapping addresses.
  The seal blocks unknown destinations, the ``local`` rule is moved after
  the VRF rules so VMs cannot reach host addresses, and an nftables table
  drops packets from a gateway port that are addressed to a node SID or
  carry an SRH (RFC 8754 edge filtering).


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


Development
-----------

::

    tox -e py3         # unit tests (stestr)
    tox -e pep8        # flake8
    tox -e dashboard   # Horizon panel tests (installs Horizon from git)


License
-------

Apache 2.0, see ``LICENSE``.
