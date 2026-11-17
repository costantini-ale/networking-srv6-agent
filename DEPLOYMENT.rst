==========================================
Deploying networking-srv6-agent (DevStack)
==========================================

A step-by-step build of a complete test cloud on one Linux machine: three
DevStack nodes and a spine-leaf (Clos) fabric of four SRv6 switches, all
libvirt VMs. Every command is given; run them in order. How the plugin
works and how to use it: `README.rst <README.rst>`__.

.. contents::
   :local:
   :depth: 1


What you build
--------------

The fabric of ``tools/fabric.topo``: two spines, two leaves, three hosts,
every link 1 Gbit/s::

         spine1      spine2
           |  \     /  |
           |    \ /    |
           |    / \    |
         leaf1       leaf2
         /   \          \
      node1  node2     node3

=========  =================  ================  ===========================
VM         Role               Management        Locator
=========  =================  ================  ===========================
node1      controller +       192.168.122.101    ``fc00:0:1::/48``
           compute
node2      compute            192.168.122.102    ``fc00:0:2::/48``
node3      compute            192.168.122.103    ``fc00:0:3::/48``
spine1/2   SRv6 switch        DHCP              ``fc00:0:101::/48``,
                                                ``fc00:0:102::/48``
leaf1/2    SRv6 switch        DHCP              ``fc00:0:103::/48``,
                                                ``fc00:0:104::/48``
=========  =================  ================  ===========================

Two networks carry the traffic:

- **Management** (libvirt's ``default`` NAT network, IPv4): the OpenStack
  APIs, SSH and VXLAN (traffic inside one tenant network).
- **Fabric** (one isolated libvirt network per link, IPv6, MTU 9000): SRv6,
  the traffic between the networks of a domain. Link *i* of the topology
  file (file order) is ``fd01:<i>::/64``; each NIC is named after the node
  at the other end.

The switches route every locator with static ECMP routes, own a SID (so
they can be TE waypoints) and run the telemetry agent, which reports link
load to Neutron (link state and automatic steering).

The steps:

1. `Prepare the hypervisor`_
2. `Create the three node VMs`_
3. `Install DevStack`_
4. `Build the fabric`_
5. `Check it`_

`Tear it down`_ removes everything. `Reference`_ lists the requirements and
settings for a cloud of your own.


Prepare the hypervisor
----------------------

This deployment was tested only on a hypervisor running **Ubuntu 24.04**;
other Linux distributions should work but are untested.

You need a Linux machine with:

- KVM with **nested virtualisation** (the nodes run Nova VMs);
- about **24 GB of RAM** (node1 8 GB, node2 and node3 4 GB each, four
  switches 1 GB each) and **8 vCPUs**;
- about **100 GB of disk** in libvirt's ``default`` storage pool;
- internet access (packages, DevStack, the cloud image).

Install libvirt::

    sudo apt-get install -y qemu-kvm libvirt-daemon-system virtinst git
    sudo usermod -aG libvirt $USER      # then log out and in again

Check it, as your user::

    cat /sys/module/kvm_*/parameters/nested     # Y or 1
    virt-install --version                      # 3.0 or newer
    export LIBVIRT_DEFAULT_URI=qemu:///system   # put it in your shell rc
    virsh pool-list                             # "default" active
    virsh net-list --all                        # "default" active

If the ``default`` network is missing or inactive::

    virsh net-define /usr/share/libvirt/networks/default.xml
    virsh net-start default && virsh net-autostart default

If the ``default`` pool is missing::

    virsh pool-define-as default dir --target /var/lib/libvirt/images
    virsh pool-build default
    virsh pool-start default && virsh pool-autostart default

Get the plugin (for the fabric script and topology file), the Ubuntu 24.04
cloud image, and an SSH key::

    mkdir -p ~/srv6-lab && cd ~/srv6-lab        # all later steps run here
    git clone https://github.com/costantini-ale/networking-srv6-agent.git
    wget https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img
    [ -f ~/.ssh/id_ed25519.pub ] || ssh-keygen -t ed25519 -N '' -f ~/.ssh/id_ed25519


Create the three node VMs
-------------------------

Each node is an Ubuntu 24.04 VM on the ``default`` network with a fixed
address, a ``stack`` user (passwordless sudo, your SSH key, home
``/opt/stack`` as DevStack expects) and the ``vrf`` kernel module.

Write the cloud-init template, ``node.yaml``::

    cat > node.yaml <<'EOF'
    #cloud-config
    hostname: @NAME@
    users:
      - name: stack
        homedir: /opt/stack
        shell: /bin/bash
        sudo: ALL=(ALL) NOPASSWD:ALL
        lock_passwd: true
        ssh_authorized_keys: ["@KEY@"]
    package_update: true
    packages: [git]
    runcmd:
      - chmod +x /opt/stack
      # the vrf module (one Linux VRF per SRv6 domain)
      - apt-get install -y linux-modules-extra-$(uname -r)
    # virt-install's first boot ends in a power off; started again below
    power_state: {mode: poweroff}
    EOF

Copy the image into the storage pool once, then create the three VMs (each
a 60 GB copy-on-write disk on top of it)::

    virsh vol-create-as default noble-base.qcow2 \
        $(stat -c %s noble-server-cloudimg-amd64.img) --format qcow2
    virsh vol-upload --pool default noble-base.qcow2 \
        noble-server-cloudimg-amd64.img

    for i in 1 2 3; do
        if [ $i = 1 ]; then mem=8192 cpu=4; else mem=4096 cpu=2; fi
        mac=52:54:00:00:00:1$i
        # a fixed address: 192.168.122.10<i>
        virsh net-update default add ip-dhcp-host \
            "<host mac='$mac' name='node$i' ip='192.168.122.10$i'/>" \
            --live --config
        virsh vol-create-as default node$i.qcow2 60G --format qcow2 \
            --backing-vol noble-base.qcow2 --backing-vol-format qcow2
        sed -e "s/@NAME@/node$i/" \
            -e "s|@KEY@|$(cat ~/.ssh/id_ed25519.pub)|" node.yaml > node$i.yaml
        virt-install -q --import --name node$i \
            --memory $mem --vcpus $cpu --cpu host-passthrough \
            --osinfo detect=on,require=off \
            --disk vol=default/node$i.qcow2,bus=virtio \
            --network network=default,model=virtio,mac=$mac \
            --cloud-init user-data=node$i.yaml \
            --graphics none --noautoconsole
    done

Wait for the first boot to finish (the VMs power off, a few minutes), then
start them for good::

    for i in 1 2 3; do
        until virsh domstate node$i | grep -q 'shut off'; do sleep 5; done
        virsh start node$i
    done

Check that you can log in (this also records the host keys, which the
fabric script needs) and that the kernel has what the plugin needs::

    for i in 1 2 3; do
        ssh -o StrictHostKeyChecking=accept-new stack@192.168.122.10$i \
            'hostname; modinfo -F name vrf; uname -r; ip -V'
    done
    # node<i>, vrf, kernel 6.1+, iproute2 6.1+ on each


Install DevStack
----------------

On each node, as ``stack`` (``ssh stack@192.168.122.10<i>``)::

    git clone https://opendev.org/openstack/devstack -b stable/2026.2
    cd devstack

Write ``~/devstack/local.conf``. On **node1** (controller and compute)::

    [[local|localrc]]
    HOST_IP=192.168.122.101
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

On **node2** (compute). On **node3** the same, with
``HOST_IP=192.168.122.103`` and ``SRV6_LOCATOR=fc00:0:3::/48``::

    [[local|localrc]]
    HOST_IP=192.168.122.102
    SERVICE_HOST=192.168.122.101
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

The locators must match ``tools/fabric.topo``. Stack **node1 first**, then
node2 and node3 (these two can run in parallel)::

    ./stack.sh                          # 20-40 minutes per node

Then, on node1, register the computes and check the agents::

    source ~/devstack/openrc admin admin
    nova-manage cell_v2 discover_hosts
    openstack network agent list --agent-type open-vswitch   # 3, alive
    openstack srv6 locator list                              # 3 locators

The nodes have no route to each other's locators yet: the next step adds
the fabric that carries them.


Build the fabric
----------------

``tools/srv6-fabric-lab.py`` reads the topology file and, on the
hypervisor, as your user:

- creates one isolated libvirt network per link (a cable, MTU 9000);
- creates one VM per switch, from the cloud image (1 vCPU, 1 GB). At first
  boot it addresses its links, adds static ECMP routes to every locator,
  installs its own SID and starts the telemetry agent; all of it is
  re-applied at every boot;
- hot-plugs one NIC per link into each node and, over SSH, sets the link
  address and a route to every locator through its leaf (re-applied at
  every boot);
- installs the topology file on node1 as ``[srv6] topology_file`` and
  restarts the Neutron server. The switches' agents use the admin
  credentials from node1's ``~/devstack/openrc``.

Run it::

    cd ~/srv6-lab/networking-srv6-agent
    tools/srv6-fabric-lab.py up tools/fabric.topo \
        --hosts node1=192.168.122.101,node2=192.168.122.102,node3=192.168.122.103 \
        --controller node1 \
        --image ~/srv6-lab/noble-server-cloudimg-amd64.img \
        --ssh-key ~/.ssh/id_ed25519.pub

It takes a few minutes: each switch boots once to configure itself, powers
off, and the script starts it for good. Running it again only adds what is
missing.

The agents pick up the new underlay at their next full resync (every 5
minutes); restart them to do it now::

    for i in 1 2 3; do
        ssh stack@192.168.122.10$i sudo systemctl restart devstack@q-agt
    done


Check it
--------

On node1, as ``stack``, with ``~/devstack/openrc admin admin`` sourced.

**Underlay.** Every locator is routed through the fabric::

    ip -6 route get fc00:0:3::        # via fd01:5::1 dev leaf1
    openstack srv6 link list --long    # 14 rows (7 links, both ways),
                                       # not stale

**A domain.** Two networks in one domain, one VM on each, on node1 and
node3 (different leaves, so the traffic crosses a spine)::

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
        --network net-b --availability-zone nova:node3 vm-b
    openstack server list        # wait for ACTIVE; note both addresses

**Ping across the domain.** A domain network has no router (so no
floating IP): reach ``vm-a`` from its DHCP namespace (cirros password
``gocubsgo``)::

    sudo ip netns exec qdhcp-$(openstack network show net-a -f value -c id) \
        ssh cirros@<vm-a address>
    $ ping <vm-b address>                       # inside vm-a: replies

**The SRv6 state** on node1:

=================================================  ===========================
Command                                            Expect
=================================================  ===========================
``ip -br link show type vrf``                      One VRF, for ``red``.
``ip -6 route show table all | grep seg6local``    ``End.DT46`` on red's SID
                                                   in ``fc00:0:1::/48``, plus
                                                   the node's own ``End``
                                                   routes (always there).
``ip route show table all | grep 'encap seg6 '``   ``vm-b``'s address, encap
                                                   toward a SID in
                                                   ``fc00:0:3::/48``.
=================================================  ===========================

**A TE path.** Send red's traffic toward node3 through ``spine1``; the
ping keeps working::

    openstack srv6 te path create --domain red --destination-host node3 \
        --via-host spine1
    openstack srv6 te path list --long     # ACTIVE
    ip route show table all | grep 'encap seg6 '   # now through spine1's ID

**Automatic steering.** With heavy traffic between VMs (e.g. ``iperf3``),
the server moves pairs off congested links on its own: those paths show
``origin=auto`` in ``openstack srv6 te path list --long``. See *Link state
and automatic steering* in `README.rst <README.rst>`__.

**On a switch** (``virsh domifaddr leaf1``, then ``ssh stack@<address>``):
``ip -6 route``, ``journalctl -u srv6-fabric-agent``.


Tear it down
------------

Remove the fabric (switch VMs, link networks, the nodes' fabric NICs and
boot script, ``topology_file``)::

    cd ~/srv6-lab/networking-srv6-agent
    tools/srv6-fabric-lab.py down tools/fabric.topo \
        --hosts node1=192.168.122.101,node2=192.168.122.102,node3=192.168.122.103 \
        --controller node1

Then the nodes::

    for i in 1 2 3; do
        virsh destroy node$i; virsh undefine node$i --remove-all-storage
        virsh net-update default delete ip-dhcp-host \
            "<host mac='52:54:00:00:00:1$i'/>" --live --config
    done
    virsh vol-delete --pool default noble-base.qcow2

To change the topology: ``down`` with the old file, edit it, ``up`` again.
After restacking node1, run ``up`` again: DevStack rewrites
``neutron.conf``.


Reference
---------

For a cloud other than the example above.

Requirements
~~~~~~~~~~~~

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
IPv6 underlay            Routes every node's locator to that node. The plugin
                         uses it, never configures it.
Underlay MTU             The outer header adds 40 to 64+ bytes to every VM
                         packet (see `MTU`_).
Python 3.11+             --
Neutron                  Same release as the rest of the cloud (the DevStack
                         branch).
=======================  =====================================================

Each node's locator (``SRV6_LOCATOR``) is a /48, unique per node, all in
one /32 with ``usid``. It is not an interface address: the agent installs
the routes for the SIDs inside it. The agent finds the underlay NIC from
the route to the peers' locators; set ``SRV6_UNDERLAY_INTERFACE`` only to
force one.

DevStack settings
~~~~~~~~~~~~~~~~~

``enable_plugin networking-srv6-agent ...`` installs the package, enables
the service plugin and the agent extension, writes ``[srv6]`` /
``[srv6_agent]``, runs the DB migrations and, if Horizon is enabled,
installs the SRv6 panels. Defaults live in ``devstack/settings``.

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

Topology file
~~~~~~~~~~~~~

Read by both the plugin and the fabric script::

    # node <name> <locator>      hosts: Neutron host name; /48s in one /32
    node spine1 fc00:0:101::/48
    node node1  fc00:0:1::/48
    # link <a> <b> <capacity, Mbit/s per direction>
    link spine1 leaf1 1000

Every node not passed in ``--hosts`` becomes a switch. Links are numbered
in file order, which sets their addresses: append new links at the end.

MTU
~~~

SRv6 wraps each VM packet in an outer IPv6 header, so it must fit:
**underlay MTU >= network MTU + 40** (``usid``; +16 per waypoint beyond
four), or **+ 64** (``encap``; +16 per waypoint). The example's fabric
links have MTU 9000, so nothing to do there. On a 1500-byte underlay it
breaks with ``encap``, VLAN or flat networks in a domain, five or more
waypoints with ``usid`` (fabric switches count), or a network MTU raised
by hand.

Symptom: ping works, large transfers stall; the agent logs a warning with
the largest network MTU that fits. Fix one side::

    sudo ip link set <nic> mtu 9000              # raise the underlay MTU
    openstack network set --mtu 1400 <net>       # or lower the network MTU

Physical switches
~~~~~~~~~~~~~~~~~

The script only builds VMs. For real switches, cable them as the topology
file says and run on each the script that ``tools/srv6-fabric-lab.py config
tools/fabric.topo --hosts ... <switch>`` prints (it finds NICs by MAC: edit
the ``nic`` lines), plus the telemetry agent as ``user_data`` in the script
installs it.


Deploy on physical nodes
------------------------

The same plugin runs on bare-metal servers; only the machines and the
underlay change. The steps above build everything as VMs; here you supply
the servers and the IPv6 network between them.

1. **Servers.** Ubuntu 24.04 (or another Linux with kernel 6.1+ and
   iproute2 6.1+), one management IPv4 address per server reachable from
   the others, a ``stack`` user with passwordless ``sudo``. The ``vrf``
   module must load (``sudo modprobe vrf``); on Ubuntu it needs the
   ``linux-modules-extra`` package of the running kernel.
2. **Locators.** Give each server a unique ``/48`` (see `Requirements`_).
   It is not configured on an interface; the agent installs the routes for
   the SIDs inside it.
3. **Underlay.** Make the network between the servers route every
   server's locator to that server over IPv6, on the NIC that faces the
   network. The plugin never configures it. Set the MTU as described in
   `MTU`_.
4. **DevStack.** Follow `Install DevStack`_ on each server, with its own
   ``HOST_IP`` and ``SRV6_LOCATOR`` in ``local.conf``: controller first,
   then the computes.
5. **Check.** Run the commands at the end of `Install DevStack`_ (agents
   alive, one locator per server), then the checks of `Check it`_. If the
   underlay has a topology file, set ``SRV6_TOPOLOGY_FILE`` on the
   controller to enable link state and automatic steering.
