# Copyright 2026 networking-srv6-agent contributors.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

"""Srv6Plugin -- the service plugin behind the srv6 API.

Enabled with `service_plugins = ...,srv6`. It owns three things: allocation
of each domain's SID function id, translation of Neutron state into the
payload agents consume -- admin TE paths included, resolved to SIDs here --
and the decision of *when* to tell agents anything. It programs no kernel
state itself -- that is entirely the agent's job.

This is the old networking-bgpvpn SRv6 driver with the driver/plugin split
removed. The precommit/postcommit discipline
survives without the hook framework: write inside the transaction, fan out
RPC after it commits, and read anything the commit destroys BEFORE it (a
postcommit hook that looked up the function id found it already
released, and every agent was silently left holding a stale VRF).

Never decorate the CRUD methods themselves with CONTEXT_WRITER: the fan-out
would then run inside the transaction and agents could be told about state
that later rolls back.
"""

import datetime
import ipaddress

from neutron import worker as neutron_worker
from neutron_lib.api.definitions import portbindings
from neutron_lib.callbacks import events
from neutron_lib.callbacks import registry
from neutron_lib.callbacks import resources
from neutron_lib import constants as n_const
from neutron_lib import context as n_context
from neutron_lib.db import api as db_api
from neutron_lib import exceptions as n_exc
from neutron_lib.plugins import directory
from neutron_lib import rpc as n_rpc
from oslo_config import cfg
from oslo_db import exception as db_exc
from oslo_log import log as logging
from oslo_utils import timeutils

from networking_srv6_agent._i18n import _
from networking_srv6_agent.api.definitions import srv6 as srv6_def
from networking_srv6_agent.api.definitions import srv6_aggregate_loads \
    as ag_def
from networking_srv6_agent.api.definitions import srv6_link_states as ls_def
from networking_srv6_agent.api.definitions import srv6_locators as loc_def
from networking_srv6_agent.api.definitions import srv6_te as te_def
from networking_srv6_agent.common import config
from networking_srv6_agent.common import constants
from networking_srv6_agent.common import sid
from networking_srv6_agent.db import domain_db
from networking_srv6_agent.db import link_state_db
from networking_srv6_agent.db import srv6_db
from networking_srv6_agent.db import te_db
from networking_srv6_agent.extensions import srv6 as srv6_ext
from networking_srv6_agent.extensions import srv6_te as te_ext
from networking_srv6_agent.services import pce
from networking_srv6_agent.services import rpc
from networking_srv6_agent.services import te_liveness
from networking_srv6_agent.services import te_resolver
from networking_srv6_agent.services.underlay import telemetry
from networking_srv6_agent.services import workload


LOG = logging.getLogger(__name__)


def _routes_of(endpoint):
    """An endpoint's payload routes, one per address.

    The full sync and the incremental path share it. network_id is for the
    agent's per-route MTU check; port_id is what per-VM TE precedence
    resolves on.
    """
    return [{'prefix': prefix,
             'host': endpoint.host,
             'port_id': endpoint.port_id,
             'network_id': endpoint.network_id}
            for prefix in endpoint.prefixes]


def _normalise_filters(filters):
    """Accept the legacy tenant_id filter as project_id."""
    filters = dict(filters or {})
    if 'tenant_id' in filters and 'project_id' not in filters:
        filters['project_id'] = filters.pop('tenant_id')
    return filters


def _only(res, fields):
    if fields:
        return {k: v for k, v in res.items() if k in fields}
    return res


@registry.has_registry_receivers
class Srv6Plugin(srv6_ext.Srv6PluginBase):

    supported_extension_aliases = [srv6_def.ALIAS, loc_def.ALIAS,
                                   te_def.ALIAS, ls_def.ALIAS, ag_def.ALIAS]
    # A POST of a list reaches create_<resource>_bulk in one call, so a
    # link-state batch is stored in one transaction. Only srv6_link_states
    # and srv6_aggregate_loads allow bulk at all (their get_resources); the
    # others refuse a list.
    __native_bulk_support = True

    def __init__(self):
        super().__init__()
        config.register_server_opts()
        self.conf = cfg.CONF[config.SRV6_SERVER_GROUP]
        self.agent_rpc = rpc.Srv6AgentNotifyAPI()
        self._pool_synced = False
        # port id -> the domains of its source-VM paths, between the
        # BEFORE_DELETE and AFTER_DELETE of that port
        self._deleting_source_ports = {}
        self.encoding = self.conf.dataplane_encoding
        self._validate_function_id_ranges()
        self._load_topology()
        LOG.info("SRv6 service plugin initialised (vrf_table_base=%(base)s, "
                 "function_id_ranges=%(ranges)s, dataplane_encoding=%(enc)s)",
                 {'base': self.conf.vrf_table_base,
                  'ranges': self.conf.function_id_ranges,
                  'enc': self.encoding})

    def _load_topology(self):
        """The topology file, read once.

        fabric_locators {node: locator} for fabric waypoints, empty
        without [srv6] topology_file; its link capacities go to the
        Underlay, the measured link state.
        """
        self.fabric_locators, capacity = {}, {}
        if self.conf.topology_file:
            try:
                self.fabric_locators, capacity = pce.load(
                    self.conf.topology_file)
            except (OSError, pce.TopologyError) as e:
                raise n_exc.InvalidConfigurationOption(
                    opt_name='[srv6] topology_file', opt_value=str(e))
        self.underlay = telemetry.Underlay(self.conf, capacity, self)

    @property
    def usid(self):
        return self.encoding == constants.ENCODING_USID

    def _validate_function_id_ranges(self):
        """Refuse to start with function ids outside the LIB.

        At startup rather than on the first API call. Ids already
        allocated outside the range are kept and warned about by
        sync_allocation_pool, as for any range change.
        """
        try:
            srv6_db.parse_function_id_ranges(self.conf.function_id_ranges)
        except ValueError as e:
            raise n_exc.InvalidConfigurationOption(
                opt_name='[srv6] function_id_ranges', opt_value=str(e))

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _ensure_pool(self, context):
        """Populate the function id pool once, lazily.

        Lazily rather than in __init__ because the plugin is constructed
        before the database is necessarily reachable, and a server that
        cannot start because of an unreachable DB is harder to diagnose
        than one that fails on the first API call.
        """
        if self._pool_synced:
            return
        ranges = srv6_db.parse_function_id_ranges(self.conf.function_id_ranges)
        try:
            srv6_db.sync_allocation_pool(context, ranges)
        except db_exc.DBDuplicateEntry:
            # Two sync_state calls landing together -- every agent's first
            # one after a server restart -- both find the newly configured
            # ids missing and both insert them; the second fails on the
            # primary key (seen on the testbed when function_id_ranges moved
            # to the microSID LIB). The rows are there now, so one more
            # pass inserts nothing and completes the sync.
            LOG.debug("SRv6 function id pool populated concurrently; "
                      "re-synchronising")
            srv6_db.sync_allocation_pool(context, ranges)
        self._pool_synced = True

    @staticmethod
    def _core_plugin():
        return directory.get_plugin()

    def _vrf_table(self, function_id):
        return sid.vrf_table(function_id, self.conf.vrf_table_base)

    @staticmethod
    def _owns(context, domain):
        return context.is_admin or domain['project_id'] == context.project_id

    def _get_domain_checked(self, context, domain_id):
        """The domain, if the caller may act on it; else NotFound.

        Association requests are authorised by neutron against the
        association's own project_id. That says nothing about the PARENT, so
        the parent's ownership is checked here. NotFound rather than
        NotAuthorized, so another project's domain id reveals nothing.

        (ADMIN_OR_PARENT_OWNER_* cannot do this: neutron resolves
        ext_parent_owner only for the parents listed in neutron-lib's
        EXT_PARENT_RESOURCE_MAPPING -- floatingip, router, local_ip, qos
        policy -- so for srv6_domain the rule could never pass.)
        """
        domain = domain_db.get_domain(context, domain_id)
        if domain is None or not self._owns(context, domain):
            raise srv6_ext.Srv6DomainNotFound(id=domain_id)
        return domain

    # ------------------------------------------------------------------
    # payload construction
    # ------------------------------------------------------------------
    def _network_info(self, context, network_id):
        """The subnets and MTU an agent needs to build a gateway port."""
        plugin = self._core_plugin()
        network = plugin.get_network(context, network_id)
        subnets = []
        for subnet_id in network.get('subnets', []):
            subnet = plugin.get_subnet(context, subnet_id)
            if subnet.get('ip_version') not in (4, 6):
                continue
            subnets.append({
                'id': subnet['id'],
                'cidr': subnet['cidr'],
                'ip_version': subnet['ip_version'],
                'gateway_ip': subnet.get('gateway_ip'),
            })
        return {
            'network_id': network_id,
            'mtu': network.get('mtu'),
            'subnets': subnets,
        }

    def _workload_endpoints(self, context, network_ids):
        """The workload endpoints on these networks, live-migrating included.

        A port DOWN in the middle of a live migration keeps its route: a
        resync in that window must not prune it everywhere.
        """
        if not network_ids:
            return []
        ports = self._core_plugin().get_ports(
            context, filters={'network_id': list(network_ids)})
        migrating = srv6_db.get_migrating_port_ids(
            context, [p['id'] for p in ports
                      if p.get('status') != n_const.PORT_STATUS_ACTIVE])
        return workload.endpoints(ports, migrating)

    def _routes_for_domain(self, context, network_ids, locators):
        """One route per port that is bound to a host with a known locator.

        A port on a host that has never called sync_state is skipped rather
        than advertised: guessing a SID for an unknown locator would install
        a blackhole on every other node. Each route keeps its port_id --
        per-VM TE precedence resolves on it -- and its network_id. TE
        segments are attached afterwards, by _attach_segments.
        """
        routes = []
        for ep in self._workload_endpoints(context, network_ids):
            if not locators.get(ep.host):
                LOG.debug("skipping port %(p)s: host %(h)s has not "
                          "registered an SRv6 locator yet",
                          {'p': ep.port_id, 'h': ep.host})
                continue
            routes.extend(_routes_of(ep))
        return routes

    def _build_domain_payload(self, context, domain, locators=None,
                              dead_hosts=None):
        """Full desired state of one domain, as agents consume it.

        Built with an ELEVATED context whoever triggered it. A shared network
        is allowed in a domain, and the instances on it may
        belong to other projects: under the owner's own context their ports
        are invisible, so their routes would silently be missing.

        dead_hosts only steers TE paths around dead waypoints; the payload's
        `locators` stay complete, because a dead node's VMs keep their
        routes and decap SIDs on every other node.
        """
        admin_context = context.elevated()
        if locators is None:
            locators = srv6_db.get_host_locators(admin_context)
        function_id = srv6_db.get_function_id(admin_context, domain['id'])
        if function_id is None:
            LOG.warning("SRv6 domain %s has no function id allocated; "
                        "skipping", domain['id'])
            return None
        network_ids = domain.get(srv6_def.NETWORKS) or []
        return {
            'id': domain['id'],
            'function_id': function_id,
            'behavior': (domain.get(srv6_def.SRV6_BEHAVIOR) or
                         constants.DEFAULT_BEHAVIOR),
            'vrf_table': self._vrf_table(function_id),
            'networks': [self._network_info(admin_context, net_id)
                         for net_id in network_ids],
            'routes': self._attach_segments(
                admin_context, domain['id'],
                self._routes_for_domain(admin_context, network_ids,
                                        locators),
                locators, dead_hosts),
            # Fabric nodes too (topology_file): their uN may be the first
            # segment of a computed path, and the agents' edge filter only
            # admits SIDs inside a locator it knows. Registered hosts win.
            'locators': {**self.fabric_locators, **locators},
            # Additive, so PAYLOAD_VERSION stays 1: an older agent ignores
            # it, and a missing key means full SIDs to a newer one.
            'encoding': self.encoding,
        }

    def _push_domain(self, context, domain):
        payload = self._build_domain_payload(context, domain)
        if payload is not None:
            self.agent_rpc.domain_updated(context, payload)

    def _push_domain_by_id(self, context, domain_id):
        domain = domain_db.get_domain(context.elevated(), domain_id)
        if domain is None:
            LOG.debug("SRv6 domain %s vanished before it could be pushed",
                      domain_id)
            return
        self._push_domain(context, domain)

    # ------------------------------------------------------------------
    # domains
    # ------------------------------------------------------------------
    def create_srv6_domain(self, context, srv6_domain):
        body = srv6_domain[srv6_def.RESOURCE_NAME]
        self._ensure_pool(context)
        try:
            # One transaction for both: the two DB helpers carry their own
            # CONTEXT_WRITER, which JOINS this one. Without the block they
            # would commit separately, and an exhausted pool would leave a
            # committed domain holding no function id.
            with db_api.CONTEXT_WRITER.using(context):
                domain = domain_db.create_domain(context, body)
                domain[srv6_def.SID_FUNCTION] = srv6_db.allocate_function_id(
                    context, domain['id'])
        except srv6_db.NoFunctionIdAvailable:
            raise srv6_ext.Srv6NoFunctionIdAvailable()
        LOG.info("SRv6 domain %(d)s allocated function id %(f)s (vrf table "
                 "%(t)s)", {'d': domain['id'],
                            'f': domain[srv6_def.SID_FUNCTION],
                            't': self._vrf_table(
                                domain[srv6_def.SID_FUNCTION])})
        # No RPC: a domain with no network has no presence on any node
        # (handle_sync_state and the agents agree).
        return domain

    def get_srv6_domain(self, context, id, fields=None):
        domain = domain_db.get_domain(context, id, fields)
        if domain is None:
            raise srv6_ext.Srv6DomainNotFound(id=id)
        return domain

    def get_srv6_domains(self, context, filters=None, fields=None,
                         sorts=None, limit=None, marker=None,
                         page_reverse=False):
        return domain_db.get_domains(context, _normalise_filters(filters),
                                     fields)

    def update_srv6_domain(self, context, id, srv6_domain):
        domain = domain_db.update_domain(
            context, id, srv6_domain[srv6_def.RESOURCE_NAME])
        if domain is None:
            raise srv6_ext.Srv6DomainNotFound(id=id)
        self._push_domain(context, domain)
        return domain

    def delete_srv6_domain(self, context, id):
        # BEFORE the delete: the allocation FK is ON DELETE SET NULL
        # and the TE paths' FK is ON DELETE CASCADE, so after the commit
        # there is nothing left to read either from.
        function_id = srv6_db.get_function_id(context, id)
        te_paths = te_db.get_te_paths(context.elevated(), id)
        with db_api.CONTEXT_WRITER.using(context):
            if domain_db.delete_domain(context, id) is None:
                raise srv6_ext.Srv6DomainNotFound(id=id)
            srv6_db.release_function_id(context, id)
        if te_paths:
            # A tenant action erasing admin configuration leaves a trace.
            LOG.warning("SRv6 domain %(d)s deleted by project %(p)s; its "
                        "%(n)d admin TE path(s) went with it: %(ids)s",
                        {'d': id, 'p': context.project_id,
                         'n': len(te_paths),
                         'ids': ', '.join(p['id'] for p in te_paths)})
        if function_id is None:
            LOG.warning("SRv6 domain %s had no function id; agents were not "
                        "told to tear down (nothing to name)", id)
            return
        LOG.info("SRv6 domain %(d)s deleted; telling agents to remove "
                 "function id %(f)s (vrf table %(t)s)",
                 {'d': id, 'f': function_id,
                  't': self._vrf_table(function_id)})
        self.agent_rpc.domain_deleted(context, id, function_id,
                                      self._vrf_table(function_id))

    # ------------------------------------------------------------------
    # network associations
    # ------------------------------------------------------------------
    def _validate_net_assoc(self, context, domain, network_id):
        plugin = self._core_plugin()
        # The caller's context: a network they cannot see is NotFound, so
        # the request cannot probe for other projects' networks.
        network = plugin.get_network(context, network_id)
        if network['project_id'] != domain['project_id']:
            raise srv6_ext.Srv6NetworkNotOwned(network_id=network_id,
                                               domain_id=domain['id'])
        if network.get('router:external'):
            raise srv6_ext.Srv6ExternalNetworkNotAllowed(network_id=network_id)
        # Elevated: a router interface is refused whoever owns the router.
        # ROUTER_INTERFACE_OWNERS, not the bare router_interface owner: the
        # tuple also covers the L3HA and DVR interface owners, and without
        # them the check passes on a network that is in fact routed.
        router_ports = plugin.get_ports(context.elevated(), filters={
            'network_id': [network_id],
            'device_owner': list(n_const.ROUTER_INTERFACE_OWNERS)})
        if router_ports:
            raise srv6_ext.Srv6RouteredNetworkNotAllowed(
                network_id=network_id,
                router_id=router_ports[0]['device_id'])
        self._check_one_domain(context, domain, network_id)
        self._check_no_overlap(
            context, domain, network_id,
            [s['cidr'] for s in self._subnets_of(context, network_id)])

    def _check_one_domain(self, context, domain, network_id):
        """A network belongs to one domain.

        Two domains holding one network would each put a gateway with the
        same address on the same L2 segment, and the network's instances
        would be routed into whichever domain answered ARP: one tenant's
        router could reach another's instance through such a network. The
        unique
        constraint on network_id closes the race this check leaves open;
        create_srv6_domain_network_association maps it back here.
        """
        for other in domain_db.get_domain_ids_for_network(
                context.elevated(), network_id):
            if other == domain['id']:
                raise srv6_ext.Srv6DomainNetAssocAlreadyExists(
                    network_id=network_id, domain_id=domain['id'])
            raise srv6_ext.Srv6NetworkInAnotherDomain(
                network_id=network_id, other_domain_id=other)

    def _subnets_of(self, context, network_id):
        """id, cidr and ip_version of every subnet; elevated, read only."""
        return self._core_plugin().get_subnets(
            context.elevated(), filters={'network_id': [network_id]},
            fields=['id', 'cidr', 'ip_version'])

    def _check_no_overlap(self, context, domain, network_id, cidrs):
        """No two networks of a domain may have overlapping subnets.

        They share one routing table: the second connected route would
        either be refused by the kernel or shadow the first, and the
        domain would route to whichever gateway port came up last. Checked
        per IP version; elevated, because a shared network's subnets may
        belong to the owner only in name.
        """
        new = []
        for cidr in cidrs:
            try:
                new.append(ipaddress.ip_network(cidr, strict=False))
            except ValueError:
                continue
        if not new:
            return
        for other_id in domain.get(srv6_def.NETWORKS) or []:
            if other_id == network_id:
                continue
            for subnet in self._subnets_of(context, other_id):
                try:
                    other = ipaddress.ip_network(subnet['cidr'],
                                                 strict=False)
                except ValueError:
                    continue
                for net in new:
                    if net.version == other.version and net.overlaps(other):
                        raise srv6_ext.Srv6OverlappingSubnets(
                            cidr=str(net), network_id=network_id,
                            other_cidr=str(other),
                            other_network_id=other_id,
                            domain_id=domain['id'])

    def create_srv6_domain_network_association(self, context, srv6_domain_id,
                                               network_association):
        body = network_association[srv6_def.NET_ASSOC_RESOURCE_NAME]
        domain = self._get_domain_checked(context, srv6_domain_id)
        self._validate_net_assoc(context, domain, body['network_id'])
        # Forced, never the caller's: an admin acting on a tenant's behalf
        # still produces the tenant's association.
        body = dict(body, project_id=domain['project_id'])
        try:
            assoc = domain_db.create_net_assoc(context, srv6_domain_id, body)
        except db_exc.DBDuplicateEntry:
            # A concurrent attach got there first: to this domain or, since
            # the network_id unique constraint, to another one.
            self._check_one_domain(context, domain, body['network_id'])
            raise srv6_ext.Srv6DomainNetAssocAlreadyExists(
                network_id=body['network_id'], domain_id=srv6_domain_id)
        self._push_domain_by_id(context, srv6_domain_id)
        return assoc

    def get_srv6_domain_network_association(self, context, id,
                                            srv6_domain_id, fields=None):
        self._get_domain_checked(context, srv6_domain_id)
        assoc = domain_db.get_net_assoc(context, id, srv6_domain_id, fields)
        if assoc is None:
            raise srv6_ext.Srv6DomainNetAssocNotFound(
                id=id, domain_id=srv6_domain_id)
        return assoc

    def get_srv6_domain_network_associations(self, context, srv6_domain_id,
                                             filters=None, fields=None,
                                             sorts=None, limit=None,
                                             marker=None, page_reverse=False):
        self._get_domain_checked(context, srv6_domain_id)
        return domain_db.get_net_assocs(context, srv6_domain_id,
                                        _normalise_filters(filters), fields)

    def delete_srv6_domain_network_association(self, context, id,
                                               srv6_domain_id):
        self._get_domain_checked(context, srv6_domain_id)
        if domain_db.delete_net_assoc(context, id, srv6_domain_id) is None:
            raise srv6_ext.Srv6DomainNetAssocNotFound(
                id=id, domain_id=srv6_domain_id)
        self._push_domain_by_id(context, srv6_domain_id)

    @registry.receives(resources.SUBNET, [events.BEFORE_CREATE])
    def _refuse_overlapping_subnet_in_domain(self, resource, event, trigger,
                                             payload=None):
        """No overlap again, through the other door: a subnet added after
        attach.

        Only a request that names its cidr can be checked here: Neutron
        publishes no precommit event for subnets, and a subnet allocated
        from a pool gets its cidr after this event.
        """
        subnet = payload.latest_state or {}
        network_id = subnet.get('network_id')
        cidr = subnet.get('cidr')
        if not network_id or not isinstance(cidr, str):
            return
        admin_context = payload.context.elevated()
        for domain_id in domain_db.get_domain_ids_for_network(
                admin_context, network_id):
            domain = domain_db.get_domain(admin_context, domain_id)
            if domain is not None:
                self._check_no_overlap(admin_context, domain, network_id,
                                       [cidr])

    @registry.receives(resources.SUBNET, [events.AFTER_CREATE,
                                          events.AFTER_UPDATE,
                                          events.AFTER_DELETE])
    def _push_domain_of_changed_subnet(self, resource, event, trigger,
                                       payload=None):
        """Tell the nodes: the payload carries each network's subnets.

        Without it a node that already holds the domain kept its cached
        subnets, and a new subnet had no gateway address until the next
        resync. Never raises: the subnet change has already committed.
        """
        network_id = (payload.latest_state or {}).get('network_id')
        if not network_id:
            return
        try:
            admin_context = payload.context.elevated()
            for domain_id in domain_db.get_domain_ids_for_network(
                    admin_context, network_id):
                self._push_domain_by_id(admin_context, domain_id)
        except Exception:
            LOG.exception("SRv6: could not push the domain of network %s "
                          "after a subnet change", network_id)

    @registry.receives(resources.ROUTER_INTERFACE, [events.BEFORE_CREATE])
    def _refuse_router_interface_on_domain_network(self, resource, event,
                                                   trigger, payload=None):
        """Close the race the association check alone leaves open.

        Without this, attaching the network first and adding the router
        interface second bypasses _validate_net_assoc entirely. The L3
        plugin turns the exception into RouterInterfaceAttachmentConflict.
        """
        network_id = (payload.metadata or {}).get('network_id')
        if not network_id:
            return
        domain_ids = domain_db.get_domain_ids_for_network(
            payload.context.elevated(), network_id)
        if domain_ids:
            raise srv6_ext.Srv6NetworkInDomain(network_id=network_id,
                                               domain_id=domain_ids[0])

    # ------------------------------------------------------------------
    # locators (read-only, admin-only)
    # ------------------------------------------------------------------
    @staticmethod
    def _make_locator_dict(row, fields=None):
        updated_at = row['updated_at']
        res = {
            'id': row['host'],
            'host': row['host'],
            'locator': row['locator'],
            'updated_at': updated_at.isoformat() if updated_at else None,
        }
        return _only(res, fields)

    def get_srv6_locators(self, context, filters=None, fields=None,
                          sorts=None, limit=None, marker=None,
                          page_reverse=False):
        hosts = set((filters or {}).get('host') or
                    (filters or {}).get('id') or [])
        return [self._make_locator_dict(row, fields)
                for row in srv6_db.get_host_locator_details(context)
                if not hosts or row['host'] in hosts]

    def get_srv6_locator(self, context, id, fields=None):
        for row in srv6_db.get_host_locator_details(context):
            if row['host'] == id:
                return self._make_locator_dict(row, fields)
        raise srv6_ext.Srv6LocatorNotFound(id=id)

    # ------------------------------------------------------------------
    # measured link state
    # ------------------------------------------------------------------
    def create_srv6_link_state_bulk(self, context, srv6_link_states):
        return self.underlay.create_srv6_link_state_bulk(context,
                                                         srv6_link_states)

    def create_srv6_link_state(self, context, srv6_link_state):
        return self.underlay.create_srv6_link_state(context, srv6_link_state)

    def get_srv6_link_states(self, context, filters=None, fields=None,
                             sorts=None, limit=None, marker=None,
                             page_reverse=False):
        return self.underlay.get_srv6_link_states(context, filters, fields)

    def get_srv6_link_state(self, context, id, fields=None):
        return self.underlay.get_srv6_link_state(context, id, fields)

    # ------------------------------------------------------------------
    # measured load per VM pair and link
    # ------------------------------------------------------------------
    def create_srv6_aggregate_load_bulk(self, context, srv6_aggregate_loads):
        return self.underlay.create_srv6_aggregate_load_bulk(
            context, srv6_aggregate_loads)

    def create_srv6_aggregate_load(self, context, srv6_aggregate_load):
        return self.underlay.create_srv6_aggregate_load(
            context, srv6_aggregate_load)

    def domain_addresses(self, admin_context, domain):
        """{address: (port id, host)} of a domain's bound workload ports.

        The same endpoints _routes_for_domain advertises, live-migrating
        ports included, whatever the host's locator: the measurement
        already shows where the traffic is.
        """
        return {prefix.split('/')[0]: (ep.port_id, ep.host)
                for ep in self._workload_endpoints(
                    admin_context, domain.get(srv6_def.NETWORKS) or [])
                for prefix in ep.prefixes}

    def get_srv6_aggregate_loads(self, context, filters=None, fields=None,
                                 sorts=None, limit=None, marker=None,
                                 page_reverse=False):
        return self.underlay.get_srv6_aggregate_loads(context, filters, fields)

    def get_srv6_aggregate_load(self, context, id, fields=None):
        return self.underlay.get_srv6_aggregate_load(context, id, fields)

    def _check_capabilities(self, host, capabilities):
        """Name a node that cannot honour dataplane_encoding. Logs only.

        Deployment-wide by design: the server does not
        route around such a node, but it does not let it fail in silence.
        """
        required = constants.ENCODING_REQUIRES[self.encoding]
        if not required:
            return
        if capabilities is None:
            LOG.warning("SRv6 agent on %(host)s did not report its "
                        "capabilities (an older agent?); it may not support "
                        "dataplane_encoding = %(enc)s",
                        {'host': host, 'enc': self.encoding})
            return
        missing = [c for c in required if c not in capabilities]
        if missing:
            LOG.error("SRv6 agent on %(host)s lacks %(missing)s, which "
                      "dataplane_encoding = %(enc)s needs (kernel >= 6.1 and "
                      "iproute2 >= 6.1). Traffic through and to that node "
                      "will fail.",
                      {'host': host, 'missing': ', '.join(missing),
                       'enc': self.encoding})

    @staticmethod
    def _check_usid_locator(admin_context, host, locator):
        """F3216, a node C-SID in the GIB, and the block everyone else uses.

        Every container begins with ONE block, so a path through a node
        in another block cannot be packed; refusing the locator is clearer
        than routes that silently fall back to full SIDs.
        """
        sid.check_usid_locator(locator)
        block = sid.usid_block(locator)
        for other_host, other in srv6_db.get_host_locators(
                admin_context).items():
            if other_host == host or not other:
                continue
            try:
                other_block = sid.usid_block(other)
            except sid.InvalidLocator:
                continue
            if other_block != block:
                raise sid.InvalidLocator(
                    _("it is in block %(b)s but %(h)s's locator %(o)s is in "
                      "%(ob)s; microSID needs one block for every node")
                    % {'b': block, 'h': other_host, 'o': other,
                       'ob': other_block})

    # ------------------------------------------------------------------
    # traffic engineering (admin-only)
    # ------------------------------------------------------------------
    def _dead_hosts(self, admin_context):
        """The hosts whose node is down, judged by Neutron's agent liveness.

        A node is dead when its Open vSwitch agent -- the process that hosts
        the SRv6 extension -- has missed its heartbeats for agent_down_time.
        The locator registry cannot say this: its rows are never expired.

        Computed from the database each time, never cached in the process:
        API workers resolve paths too, and one worker's memory would
        disagree with another's. A host with a registered locator but no
        agent row is dead too: only an OVS agent registers a locator, so
        that host is a removed node whose row an operator deleted. No host
        is dead when the check is off
        (te_liveness_interval = 0) or the question cannot be answered: an
        unanswerable query leaves the paths steered, as before this check
        existed, instead of rerouting every one of them on a database
        hiccup.

        Fabric nodes (spines, leaves) run no agent: one is dead when its
        link-state reports stopped (silent_fabric), so an operator
        path through a dead spine goes DEGRADED too. The two sources are
        read apart: either failing leaves the other's answer.
        """
        if not self.conf.te_liveness_interval:
            return frozenset()
        dead = set()
        get_agents = getattr(self._core_plugin(), 'get_agents', None)
        try:
            if get_agents is not None:
                agents = get_agents(
                    admin_context,
                    filters={'agent_type': [n_const.AGENT_TYPE_OVS]})
                dead.update(agent['host'] for agent in agents
                            if not agent['alive'])
                dead.update(set(srv6_db.get_host_locators(admin_context)) -
                            {agent['host'] for agent in agents})
        except Exception:
            LOG.exception("SRv6: cannot read agent liveness; treating "
                          "every TE waypoint as alive")
        try:
            dead.update(self.silent_fabric(admin_context)[0])
        except Exception:
            LOG.exception("SRv6: cannot read the fabric link state; "
                          "treating every fabric node as alive")
        return frozenset(dead)

    def _resolver(self, locators, dead_hosts=frozenset()):
        return te_resolver.Resolver(locators, self.fabric_locators,
                                    dead_hosts, self.usid)

    def _attach_segments(self, admin_context, domain_id, routes, locators,
                         dead_hosts=None):
        """Give each steered route its transit SIDs (Resolver.attach).

        Reads what the resolver needs: the domain's paths, and the binding
        of each source VM a path names. dead_hosts is what _dead_hosts
        returned; None asks it here, after the no-paths return, so a domain
        with no TE path costs no agent query.
        """
        paths = te_db.get_te_paths(admin_context, domain_id)
        if not paths:
            return routes
        if dead_hosts is None:
            dead_hosts = self._dead_hosts(admin_context)
        vms = {}
        for port_id in sorted({p[te_def.SOURCE_PORT_ID] for p in paths
                               if p.get(te_def.SOURCE_PORT_ID)}):
            try:
                port = self._core_plugin().get_port(admin_context, port_id)
            except n_exc.PortNotFound:
                continue
            # The host and address rules of its own route; a stopped or
            # migrating source VM keeps its binding.
            ep = workload.endpoint(port, workload_only=False)
            if ep:
                vms[port_id] = {'host': ep.host,
                                'network_id': ep.network_id,
                                'prefixes': list(ep.prefixes)}
        return self._resolver(locators, dead_hosts).attach(
            domain_id, routes, paths, vms)

    def host_of_port(self, admin_context, port_id):
        """The node a port is bound to (the routes' host rule), or None."""
        if not port_id:
            return None
        try:
            port = self._core_plugin().get_port(admin_context, port_id)
        except n_exc.PortNotFound:
            return None
        return workload.host_of(port)

    def _resolve_te_path(self, context, path, locators, function_id,
                         dead_hosts=()):
        """Resolver.resolve, with the bindings of the path's ports read."""
        bindings = {
            port_id: self.host_of_port(context.elevated(), port_id)
            for port_id in (path.get(te_def.DESTINATION_PORT_ID),
                            path.get(te_def.SOURCE_PORT_ID)) if port_id}
        return self._resolver(locators, dead_hosts).resolve(
            path, function_id, bindings)

    def _resolved(self, context, path, locators=None):
        admin_context = context.elevated()
        if locators is None:
            locators = srv6_db.get_host_locators(admin_context)
        return self._resolve_te_path(
            context, path, locators,
            srv6_db.get_function_id(admin_context, path['domain_id']),
            self._dead_hosts(admin_context))

    @staticmethod
    def _validate_via_hosts(via_hosts, locators):
        """A 400, never a silent `segments: []`."""
        if len(via_hosts) > constants.MAX_TE_VIA_HOSTS:
            raise te_ext.Srv6TePathInvalid(
                reason=_("at most %(max)d via_hosts are allowed, got %(n)d")
                % {'max': constants.MAX_TE_VIA_HOSTS, 'n': len(via_hosts)})
        unknown = [host for host in via_hosts if host not in locators]
        if unknown:
            raise te_ext.Srv6TePathInvalid(
                reason=_("via_hosts %s have no registered SRv6 locator (see "
                         "GET /v2.0/srv6_locators)") % ', '.join(unknown))

    def _validate_destination_port(self, admin_context, domain, port_id,
                                   role=te_def.DESTINATION_PORT_ID):
        """The port, if it may be a `role` of this domain's paths; else 400.

        role: the attribute that named it, for the message
        (destination_port_id or source_port_id).
        """
        try:
            port = self._core_plugin().get_port(admin_context, port_id)
        except n_exc.PortNotFound:
            raise te_ext.Srv6TePathInvalid(
                reason=_("%(r)s %(p)s does not exist")
                % {'r': role, 'p': port_id})
        if port['network_id'] not in (domain.get(srv6_def.NETWORKS) or []):
            raise te_ext.Srv6TePathInvalid(
                reason=_("port %(p)s is on network %(n)s, which is not "
                         "attached to SRv6 domain %(d)s")
                % {'p': port_id, 'n': port['network_id'],
                   'd': domain['id']})
        return port

    def _validate_source_port(self, admin_context, domain, body, locators,
                              host, port):
        """A 400 for a source VM that could never steer anything.

        One VM of the domain, bound to an SRv6 node (whose agent installs
        the rule), not the destination itself, nor on the destination's
        node. It replaces source_host (its node is its binding, which
        follows the VM).
        """
        source_port_id = body[te_def.SOURCE_PORT_ID]
        if body.get(te_def.SOURCE_HOST):
            raise te_ext.Srv6TePathInvalid(
                reason=_("source_port_id and source_host exclude each other: "
                         "a source VM's node is where it is bound"))
        if source_port_id == (port or {}).get('id'):
            raise te_ext.Srv6TePathInvalid(
                reason=_("port %s cannot be its own destination")
                % source_port_id)
        source_port = self._validate_destination_port(
            admin_context, domain, source_port_id, te_def.SOURCE_PORT_ID)
        if port and source_port['network_id'] == port['network_id']:
            # One L2 segment across nodes: the guest reaches its peer by
            # ARP over the tunnel, never through the gateway into the VRF.
            raise te_ext.Srv6TePathInvalid(
                reason=_("source_port_id %(s)s and destination_port_id %(d)s "
                         "are both on network %(n)s: traffic inside a "
                         "network is switched over the tunnel, not routed "
                         "through the domain, so the path would steer "
                         "nothing")
                % {'s': source_port_id, 'd': port['id'],
                   'n': port['network_id']})
        bound = source_port.get(portbindings.HOST_ID)
        if not bound:
            raise te_ext.Srv6TePathInvalid(
                reason=_("source_port_id %s is not bound to a node")
                % source_port_id)
        self._validate_source_host(bound, locators, host, port)
        return source_port

    @staticmethod
    def _validate_source_host(source, locators, host, port):
        """A 400 for a source that could never steer anything.

        The source must be an SRv6 node: its agent is the one that would
        push the path. And it must not be the destination: a node never
        encapsulates traffic toward its own VMs, so such a path would be
        accepted and do nothing. For a port path that is checked against
        the port's binding NOW; a later migration onto the source makes the
        path inert, and _resolve_te_path then reports it DEGRADED.
        """
        if source not in locators:
            raise te_ext.Srv6TePathInvalid(
                reason=_("source_host %s has no registered SRv6 locator "
                         "(see GET /v2.0/srv6_locators)") % source)
        if host and host == source:
            raise te_ext.Srv6TePathInvalid(
                reason=_("source_host and destination_host are both %s: a "
                         "node never encapsulates the traffic toward its own "
                         "VMs, so the path would steer nothing") % source)
        if port and port.get(portbindings.HOST_ID) == source:
            raise te_ext.Srv6TePathInvalid(
                reason=_("port %(p)s is bound on the source_host %(s)s: a "
                         "node never encapsulates the traffic toward its own "
                         "VMs, so the path would steer nothing")
                % {'p': port['id'], 's': source})

    # ------------------------------------------------------------------
    # failures of the underlay
    # ------------------------------------------------------------------
    def silent_fabric(self, context):
        """(fabric nodes down, outage) from the link-state reporters.

        A node that has posted link state and has posted nothing for
        link_state_stale_after seconds is down: its agent runs on the node
        itself, and a powered-off node's neighbours still see their NICs
        up, so silence is the only signal. A node that never posted is
        not down. Every reporter silent at once is an outage of the
        collection (expired credentials, the API unreachable), not a
        fabric failure: nothing is down, and outage is True.
        """
        last = link_state_db.get_last_seen(context)
        fresh = timeutils.utcnow() - datetime.timedelta(
            seconds=self.conf.link_state_stale_after)
        silent = frozenset(r for r, t in last.items() if t < fresh)
        if last and silent == frozenset(last):
            return frozenset(), True
        return silent, False

    def place_auto_path(self, context, pair, node_path, existing=None):
        """Create, or move, the server's own path for one VM pair.

        pair: one aggregate-view entry
        (domain_id, source_port_id, destination_port_id); node_path: the
        full computed path [source host, ..., destination host], every hop
        pinned, the leaf included, so the head-end's own ECMP cannot undo
        the choice. existing: the pair's auto path, moved in place
        (make-before-break: the head-end replaces the routes of its steer
        table). Returns the path, or None if a concurrent change created
        the same one first.
        """
        via_hosts = node_path[1:-1]
        try:
            with db_api.CONTEXT_WRITER.using(context):
                if existing:
                    path = te_db.update_te_path(
                        context, existing['id'], {te_def.VIA_HOSTS: via_hosts})
                else:
                    domain = domain_db.get_domain(context, pair['domain_id'])
                    if domain is None:
                        return None
                    path = te_db.create_te_path(context, pair['domain_id'], {
                        'project_id': domain['project_id'],
                        te_def.DESTINATION_PORT_ID:
                            pair['destination_port_id'],
                        te_def.SOURCE_PORT_ID: pair['source_port_id'],
                        te_def.VIA_HOSTS: via_hosts,
                        te_def.ORIGIN: te_def.ORIGIN_AUTO})
        except (db_exc.DBDuplicateEntry, db_exc.DBReferenceError):
            return None                     # raced, or a port just went
        if path is not None:
            self._push_domain_by_id(context, pair['domain_id'])
        return path

    def release_auto_path(self, context, path):
        """Delete one auto path: its pair's traffic goes back to ECMP."""
        gone = te_db.delete_te_path(context, path['id'])
        if gone is not None:
            self._push_domain_by_id(context, gone['domain_id'])
        return gone

    def create_srv6_te_path(self, context, srv6_te_path):
        body = srv6_te_path[te_def.RESOURCE_NAME]
        admin_context = context.elevated()
        domain_id = body[te_def.DOMAIN_ID]
        domain = domain_db.get_domain(admin_context, domain_id)
        if domain is None:
            raise srv6_ext.Srv6DomainNotFound(id=domain_id)

        host = body.get(te_def.DESTINATION_HOST)
        port_id = body.get(te_def.DESTINATION_PORT_ID)
        if bool(host) == bool(port_id):
            raise te_ext.Srv6TePathInvalid(
                reason=_("exactly one of destination_host and "
                         "destination_port_id must be set"))
        locators = srv6_db.get_host_locators(admin_context)
        if host and host not in locators:
            raise te_ext.Srv6TePathInvalid(
                reason=_("destination_host %s has no registered SRv6 "
                         "locator (see GET /v2.0/srv6_locators)") % host)
        port = None
        if port_id:
            port = self._validate_destination_port(admin_context, domain,
                                                   port_id)
        # '' is "any ingress" too: string_or_none lets it through, and it
        # is what the column stores for that.
        source = body.get(te_def.SOURCE_HOST) or None
        if source:
            self._validate_source_host(source, locators, host, port)
        source_port_id = body.get(te_def.SOURCE_PORT_ID)
        if source_port_id:
            self._validate_source_port(admin_context, domain, body, locators,
                                       host, port)
        # via == destination is accepted on purpose: a harmless detour, and
        # on two nodes the only depth-2 SRH there is (TE-DEPTH2).
        via_hosts = body.get(te_def.VIA_HOSTS) or []
        self._validate_via_hosts(via_hosts,
                                 {**self.fabric_locators, **locators})
        selector = ('port %s' % port_id) if port_id else ('host %s' % host)
        if source:
            selector += ' from %s' % source
        if source_port_id:
            selector += ' from port %s' % source_port_id
        try:
            with db_api.CONTEXT_WRITER.using(context):
                # Forced, never the caller's, as for associations: the path
                # belongs with the domain it steers.
                body = dict(body, project_id=domain['project_id'],
                            via_hosts=via_hosts, source_host=source,
                            origin=te_def.ORIGIN_USER)
                path = te_db.create_te_path(context, domain_id, body)
        except db_exc.DBDuplicateEntry:
            raise te_ext.Srv6TePathExists(domain_id=domain_id,
                                          selector=selector)
        except db_exc.DBReferenceError:
            # A port went between the check and the insert.
            raise te_ext.Srv6TePathInvalid(
                reason=_("port %s does not exist")
                % ' or '.join(p for p in (port_id, source_port_id) if p))
        LOG.info("SRv6 TE path %(id)s created in domain %(d)s: %(s)s via "
                 "%(v)s", {'id': path['id'], 'd': domain_id, 's': selector,
                           'v': via_hosts})
        self._push_domain(context, domain)
        return self._resolved(context, path, locators)

    def get_srv6_te_path(self, context, id, fields=None):
        path = te_db.get_te_path(context, id)
        if path is None:
            raise te_ext.Srv6TePathNotFound(id=id)
        return _only(self._resolved(context, path), fields)

    def get_srv6_te_paths(self, context, filters=None, fields=None,
                          sorts=None, limit=None, marker=None,
                          page_reverse=False):
        paths = te_db.get_all_te_paths(context, _normalise_filters(filters))
        if not paths:
            return []
        admin_context = context.elevated()
        locators = srv6_db.get_host_locators(admin_context)
        dead_hosts = self._dead_hosts(admin_context)
        function_ids = {}
        res = []
        for path in paths:
            domain_id = path['domain_id']
            if domain_id not in function_ids:
                function_ids[domain_id] = srv6_db.get_function_id(
                    admin_context, domain_id)
            res.append(_only(self._resolve_te_path(
                context, path, locators, function_ids[domain_id],
                dead_hosts), fields))
        return res

    def update_srv6_te_path(self, context, id, srv6_te_path):
        body = srv6_te_path[te_def.RESOURCE_NAME]
        locators = srv6_db.get_host_locators(context.elevated())
        if te_def.VIA_HOSTS in body:
            # PUT replaces the list: re-steering with no delete/create
            # window. [] un-steers.
            body = dict(body, via_hosts=body[te_def.VIA_HOSTS] or [])
            self._validate_via_hosts(body[te_def.VIA_HOSTS],
                                     {**self.fabric_locators, **locators})
            old = te_db.get_te_path(context, id)
            if old and old.get(te_def.ORIGIN) == te_def.ORIGIN_AUTO:
                # The re-optimisation computed it and moves it; a hand edit
                # would be undone or fought. Delete it to go back to ECMP.
                raise te_ext.Srv6TePathInvalid(
                    reason=_("via_hosts of a path the server created "
                             "(origin auto) are computed; delete it to send "
                             "its traffic back to the default routes"))
        path = te_db.update_te_path(context, id, body)
        if path is None:
            raise te_ext.Srv6TePathNotFound(id=id)
        LOG.info("SRv6 TE path %(id)s now via %(v)s",
                 {'id': id, 'v': path[te_def.VIA_HOSTS]})
        self._push_domain_by_id(context, path['domain_id'])
        return self._resolved(context, path, locators)

    def delete_srv6_te_path(self, context, id):
        path = te_db.delete_te_path(context, id)
        if path is None:
            raise te_ext.Srv6TePathNotFound(id=id)
        LOG.info("SRv6 TE path %(id)s deleted from domain %(d)s",
                 {'id': id, 'd': path['domain_id']})
        self._push_domain_by_id(context, path['domain_id'])

    # ------------------------------------------------------------------
    # port events -- the incremental path
    # ------------------------------------------------------------------
    def _notify_port(self, context, ep, removed=False, first_host=None):
        """Advertise or withdraw an endpoint's routes on every node.

        first_host gets the add before everybody else and is waited for
        (bounded): the live-migration handover, where the new node must
        stop routing the address back to the old one before any node sends
        it there, or the two loop.
        """
        if not ep or not ep.network_id or not ep.prefixes:
            return
        # Runs on every port update in the cloud: one indexed query, and an
        # early return for the common case of a network in no domain.
        domain_ids = domain_db.get_domain_ids_for_network(
            context.elevated(), ep.network_id)
        if not domain_ids:
            return
        locators = srv6_db.get_host_locators(context.elevated())
        if ep.host not in locators and not removed:
            LOG.debug("port %(p)s is on host %(h)s which has no SRv6 "
                      "locator; not advertising it",
                      {'p': ep.port_id, 'h': ep.host})
            return
        routes = _routes_of(ep)
        for domain_id in domain_ids:
            if srv6_db.get_function_id(context.elevated(), domain_id) is None:
                continue
            if removed:
                self.agent_rpc.routes_updated(context, domain_id,
                                              remove=routes)
            else:
                # Segments per domain, on copies: TE paths belong to a
                # domain. Without them a steered VM's route would arrive
                # at depth 1 and stay so until the next full sync.
                add = self._attach_segments(
                    context.elevated(), domain_id,
                    [dict(r) for r in routes], locators)
                if first_host:
                    self.agent_rpc.routes_updated_on_host(
                        context, first_host, domain_id, add=add)
                self.agent_rpc.routes_updated(context, domain_id, add=add)

    def _source_vm_moved(self, context, port):
        """A VM changed node: end its auto paths, re-push its source paths.

        Auto paths from or to it were computed for the old nodes, so they
        are deleted; the next re-optimisation pass judges the pair
        where it is now. A user's source-VM path follows the VM, but its
        rule must leave the old node and appear on the new one: every
        domain with a source-VM path of this port is pushed in full. Never
        raises into the port update.
        """
        try:
            gone = te_db.delete_auto_paths_of_port(context, port['id'])
            domains = ({p['domain_id'] for p in gone} |
                       set(te_db.get_domain_ids_for_source_port(
                           context, port['id'])))
        except Exception:
            LOG.exception("SRv6 TE: could not update the paths of port %s "
                          "after it moved", port['id'])
            return
        for path in gone:
            LOG.warning("SRv6 TE: automatic path %(id)s (port %(s)s to %(d)s) "
                        "deleted: port %(p)s moved to %(h)s",
                        {'id': path['id'], 's': path[te_def.SOURCE_PORT_ID],
                         'd': path[te_def.DESTINATION_PORT_ID],
                         'p': port['id'],
                         'h': port.get(portbindings.HOST_ID)})
        for domain_id in sorted(domains):
            try:
                self._push_domain_by_id(context, domain_id)
            except Exception:
                LOG.exception("SRv6: could not push domain %s after port %s "
                              "moved", domain_id, port['id'])

    def _track_handover(self, context, original, port, first_host):
        """Mark a handover before its RPC; drop the mark on a status change.

        Full syncs read the mark (srv6_db.get_migrating_port_ids) through
        the window where nova has deleted the source binding and the port
        is not ACTIVE yet. Never raises: the handover must go ahead.
        """
        try:
            if first_host:
                srv6_db.mark_migrating(context.elevated(), port['id'])
            elif original.get('status') != port.get('status'):
                srv6_db.clear_migrating(context.elevated(), port['id'])
        except Exception:
            LOG.exception("SRv6: could not update the handover mark of "
                          "port %s", port.get('id'))

    @registry.receives(resources.PORT, [events.AFTER_UPDATE])
    def registry_port_updated(self, resource, event, trigger, payload=None):
        context = payload.context
        port = payload.latest_state
        original = payload.states[0] if payload.states else {}
        if ((original.get(portbindings.HOST_ID) or None) !=
                (port.get(portbindings.HOST_ID) or None)):
            self._source_vm_moved(context.elevated(), port)
        try:
            deltas, first_host = workload.classify(original, port)
            self._track_handover(context, original, port, first_host)
            for op, ep in deltas:
                self._notify_port(context, ep, removed=op == workload.REMOVE,
                                  first_host=first_host)
        except Exception:
            LOG.exception("SRv6: failed to process port update for %s",
                          port.get('id'))

    @registry.receives(resources.PORT, [events.BEFORE_DELETE])
    def registry_port_deleting(self, resource, event, trigger, payload=None):
        """Remember the domains a VM's source-VM paths are in.

        The paths go with the port (ON DELETE CASCADE), so after the delete
        nothing says which domains still carry its rule; the AFTER_DELETE
        handler pushes these. Never raises: it must not block a delete.
        """
        try:
            domains = te_db.get_domain_ids_for_source_port(
                payload.context.elevated(), payload.resource_id)
            if domains:
                self._deleting_source_ports[payload.resource_id] = domains
        except Exception:
            LOG.exception("SRv6: could not read the source-VM paths of "
                          "port %s before its delete", payload.resource_id)

    @registry.receives(resources.PORT, [events.AFTER_DELETE])
    def registry_port_deleted(self, resource, event, trigger, payload=None):
        context = payload.context
        port = payload.latest_state
        try:
            self._notify_port(
                context, workload.endpoint(port, workload_only=False),
                removed=True)
        except Exception:
            LOG.exception("SRv6: failed to process port delete for %s",
                          port.get('id'))
        for domain_id in self._deleting_source_ports.pop(port.get('id'), []):
            try:
                self._push_domain_by_id(context.elevated(), domain_id)
            except Exception:
                LOG.exception("SRv6: could not push domain %s after its "
                              "source VM %s was deleted", domain_id,
                              port.get('id'))

    # ------------------------------------------------------------------
    # agent -> server
    # ------------------------------------------------------------------
    def get_workers(self):
        """The TE liveness monitor and the underlay's workers, one each.

        Neutron starts every worker with worker_process_count 0 as a thread
        of one extra process, so a node that dies is noticed and pushed once,
        and a congested link handled once, not once per API worker. The
        monitor starts only when its interval is not 0; Underlay.workers
        says when the re-optimisation starts.

        The liveness monitor's first pass waits half of agent_down_time,
        like Neutron's own agent status check: right after a server restart
        the agents have not yet reported, and a late heartbeat must not read
        as a death.
        """
        workers = []
        interval = self.conf.te_liveness_interval
        if interval:
            monitor = te_liveness.TeLivenessMonitor(self)
            workers.append(neutron_worker.PeriodicWorker(
                monitor.check, interval,
                max(cfg.CONF.agent_down_time // 2, 1),
                desc='srv6-te-liveness'))
        return workers + self.underlay.workers()

    def start_rpc_listeners(self):
        """Serve sync_state. Called by neutron's RPC worker at startup.

        On TOPIC_SRV6_PLUGIN, never topics.PLUGIN: a consumer there joins
        the round-robin for the core plugin's own RPC (see constants.py).
        """
        self.endpoints = [rpc.Srv6ServerRpcCallback(self)]
        self.conn = n_rpc.Connection()
        self.conn.create_consumer(constants.TOPIC_SRV6_PLUGIN,
                                  self.endpoints, fanout=False)
        LOG.info("SRv6 plugin serving RPC on topic %s",
                 constants.TOPIC_SRV6_PLUGIN)
        return self.conn.consume_in_threads()

    def _agent_alive(self, admin_context, host):
        """Whether host's Open vSwitch agent is alive; True if unknowable.

        Unknowable (no get_agents, or it fails) counts as alive, so the
        caller refuses rather than takes a locator away from a live node.
        """
        try:
            agents = self._core_plugin().get_agents(
                admin_context, filters={'agent_type': [n_const.AGENT_TYPE_OVS],
                                        'host': [host]})
        except Exception:
            LOG.exception("SRv6: cannot read the agent liveness of %s", host)
            return True
        return any(agent['alive'] for agent in agents)

    def _claim_locator(self, admin_context, host, locator):
        """Refuse a locator that overlaps another node's.

        Two nodes with one locator get one SID space: every route to the
        second node's VMs leads into the first node's, the underlay
        delivers it there, and nothing complains. So a locator overlapping
        a fabric node's (topology_file) or a registered host's whose agent
        is alive is refused: the agent logs the failed sync and retries.
        A registered host whose agent is down (or gone) loses its row
        instead: the rows never expire, and a node reinstalled under a new
        name with its old locator must be able to come back.
        """
        mine = ipaddress.IPv6Network(locator, strict=False)
        registered = srv6_db.get_host_locators(admin_context)
        others = {**self.fabric_locators, **registered}
        for other, theirs in sorted(others.items()):
            if other == host or not theirs:
                continue
            try:
                overlap = mine.overlaps(
                    ipaddress.IPv6Network(theirs, strict=False))
            except ValueError:
                continue
            if not overlap:
                continue
            if other in registered and not self._agent_alive(admin_context,
                                                             other):
                LOG.warning("SRv6: %(h)s reports locator %(l)s, which "
                            "overlaps %(o)s's %(t)s; %(o)s's agent is down, "
                            "so its locator row is removed and %(h)s takes "
                            "it", {'h': host, 'l': locator, 'o': other,
                                   't': theirs})
                srv6_db.delete_host_locator(admin_context, other)
                continue
            LOG.error("SRv6 agent on %(h)s reported locator %(l)s, which "
                      "overlaps the locator %(t)s of %(o)s; refusing its "
                      "sync. Give every node its own locator.",
                      {'h': host, 'l': locator, 'o': other, 't': theirs})
            raise rpc.Srv6SyncRefused(
                "locator %s overlaps %s of %s" % (locator, theirs, other))

    def handle_sync_state(self, context, host, locator, capabilities=None):
        """Register a locator and return every domain's full state.

        Also re-broadcasts each domain when the node's locator is new or
        changed, because that alters what every *other* node must
        encapsulate to. Doing it here rather than making agents poll keeps
        convergence bounded by one RPC round trip instead of one resync
        interval.

        Only then. It used to re-broadcast on every call, and every
        agent calls this at start, on every resync, and on every port of a
        network in no domain: domains x nodes casts per call, each one a
        full re-apply on every agent. A re-broadcast built before a
        concurrent domain delete could also land after the delete and
        bring the domain back. The caller itself always gets the
        payloads as the return value, so it converges whatever is sent.
        """
        admin_context = n_context.get_admin_context()
        self._ensure_pool(admin_context)
        self._check_capabilities(host, capabilities)
        if locator:
            try:
                sid.parse_locator(locator)
                if self.usid:
                    self._check_usid_locator(admin_context, host, locator)
            except sid.InvalidLocator as e:
                LOG.error("SRv6 agent on %(host)s reported an unusable "
                          "locator %(loc)s: %(err)s",
                          {'host': host, 'loc': locator, 'err': e})
                # Not []: the agent would remove every domain it holds.
                raise rpc.Srv6SyncRefused(
                    "unusable locator %s: %s" % (locator, e))
            self._claim_locator(admin_context, host, locator)
            locator_changed = srv6_db.set_host_locator(
                admin_context, host, locator,
                datetime.datetime.now(datetime.timezone.utc))
        else:
            locator_changed = False
        locators = srv6_db.get_host_locators(admin_context)
        # Once per sync, not once per domain. The node that is syncing may
        # still count as dead for a moment (its first heartbeat can follow
        # this call); the liveness monitor re-pushes when it turns alive.
        dead_hosts = self._dead_hosts(admin_context)
        payloads = []
        for domain in domain_db.get_domains(admin_context):
            # A domain with no network has no state on any node. It
            # used to be left out of the RPCs (create sends none) but put in
            # here, so every node built its VRF at the first sync after the
            # create. Left out, the agent's stale-domain pass and its kernel
            # garbage collector remove what an older build made.
            if not domain.get(srv6_def.NETWORKS):
                continue
            payload = self._build_domain_payload(admin_context, domain,
                                                 locators, dead_hosts)
            if payload is not None:
                payloads.append(payload)
        if locator_changed:
            # Tell everyone else about the new locator.
            LOG.info("SRv6: locator of %(h)s is new or changed; "
                     "re-broadcasting %(n)d domain(s)",
                     {'h': host, 'n': len(payloads)})
            for payload in payloads:
                self.agent_rpc.domain_updated(admin_context, payload)
        return payloads
