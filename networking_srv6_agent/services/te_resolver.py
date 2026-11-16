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

"""TE path resolution: one decision per path, read by the API and the agents.

The API shows a path's `segments` and `status`; the domain payload carries,
per route, the transit SIDs each ingress pushes. Both come from
Resolver.transit, so the list shown is the list pushed.

Pure: the plugin reads the database, the agents' liveness and the port
bindings, and hands the snapshot in. Nothing here does I/O.
"""

from oslo_log import log as logging

from networking_srv6_agent.api.definitions import srv6_te as te_def
from networking_srv6_agent.common import constants
from networking_srv6_agent.common import sid

LOG = logging.getLogger(__name__)


def rank(paths):
    """A domain's paths (te_db rows) as the maps precedence reads.

        {'by_port': {port_id: [via_host, ...]},
         'by_host': {destination_host: [via_host, ...]},
         'by_source': {source_host: {'by_port': {...}, 'by_host': {...}}},
         'by_source_port': {source_port_id: {'by_port': {...},
                                             'by_host': {...}}}}

    by_port and by_host hold the any-source paths, by_source the paths
    limited to one ingress node, by_source_port the paths limited to one
    source VM. The source is read FIRST: a source path filed under the
    any-source maps would overwrite (or be overwritten by) the any-source
    path to the same destination.
    """
    res = {'by_port': {}, 'by_host': {}, 'by_source': {},
           'by_source_port': {}}
    for path in paths:
        maps = res
        if path.get(te_def.SOURCE_PORT_ID):
            maps = res['by_source_port'].setdefault(
                path[te_def.SOURCE_PORT_ID], {'by_port': {}, 'by_host': {}})
        elif path.get(te_def.SOURCE_HOST):
            maps = res['by_source'].setdefault(
                path[te_def.SOURCE_HOST], {'by_port': {}, 'by_host': {}})
        if path.get(te_def.DESTINATION_PORT_ID):
            maps['by_port'][path[te_def.DESTINATION_PORT_ID]] = \
                path[te_def.VIA_HOSTS]
        elif path.get(te_def.DESTINATION_HOST):
            maps['by_host'][path[te_def.DESTINATION_HOST]] = \
                path[te_def.VIA_HOSTS]
    return res


class Resolver:
    """Resolves TE paths against one snapshot of the registry.

    locators        {host: locator} of the registered hosts (agents)
    fabric_locators {node: locator} from the topology file (spines, leaves:
                    usable as waypoints, never as source or destination);
                    a registered host wins over a fabric entry
    dead_hosts      the nodes that are down (plugin._dead_hosts)
    usid            the encoding: uN SIDs and C-SID containers, or full SIDs
    """

    def __init__(self, locators, fabric_locators=None,
                 dead_hosts=frozenset(), usid=True):
        self.locators = locators
        self.waypoints = {**(fabric_locators or {}), **locators}
        self.dead_hosts = dead_hosts
        self.usid = usid

    def _node_sid(self, host):
        """A waypoint's End SID, <locator>:fff0::, or in usid mode its uN."""
        locator = self.waypoints[host]
        if not self.usid:
            return sid.format_sid(locator, constants.NODE_END_FUNCTION_ID)
        try:
            return sid.node_sid(locator)
        except sid.InvalidLocator as e:
            LOG.error("SRv6: a via host's locator is not usable for "
                      "microSID: %s", e)
            return None

    def transit(self, via_hosts, source=None):
        """The transit SIDs of via_hosts in order, or None: DEGRADED.

        The one validity rule both readers use. Unusable is a via host with
        no locator (registered or fabric), a dead via host, a waypoint whose
        locator cannot be encoded, or a source with no registered locator.

        None, not a shorter list: a path missing a waypoint is not the path
        the admin asked for, and quietly dropping a hop is the silent
        partial steering the locator API exists to prevent. The routes go
        direct instead.
        """
        if source is not None and not self.locators.get(source):
            return None
        if any(not self.waypoints.get(host) or host in self.dead_hosts
               for host in via_hosts):
            return None
        sids = [self._node_sid(host) for host in via_hosts]
        return None if None in sids else sids

    # --- the agents' payload ------------------------------------------------
    def _route_transit(self, domain_id, route, by_port, by_host,
                       source=None):
        """The transit SIDs one set of paths gives a route, or None.

        by_port and by_host are the paths of one rank pair: the any-source
        paths, or the paths of one source. Port path > host path, and a
        port path with no vias is an explicit "direct" that still wins
        over a host path.

            None   no path of this set applies to the route
            []     a path applies and says direct: no vias, or DEGRADED.
                   Degraded is direct, not "fall through to the next path
                   down", which the admin did not ask for either.
            [...]  the transit SIDs, in traversal order
        """
        via_hosts = by_port.get(route.get('port_id'))
        if via_hosts is None:
            via_hosts = by_host.get(route.get('host'))
        if via_hosts is None:
            return None
        if not via_hosts:
            return []
        transit = self.transit(via_hosts, source)
        if transit is None:
            LOG.warning("SRv6 domain %(d)s: the TE path toward %(p)s%(s)s "
                        "names a host with no registered locator or a dead "
                        "node (via %(v)s, dead: %(x)s); routing it directly",
                        {'d': domain_id, 'p': route['prefix'],
                         's': ' from %s' % source if source else '',
                         'v': ', '.join(via_hosts),
                         'x': ', '.join(sorted(
                             set(via_hosts) & set(self.dead_hosts))) or
                         'none'})
            return []
        return transit

    def attach(self, domain_id, routes, paths, vms=None):
        """Give each steered route its transit SIDs. Mutates `routes`.

        paths are the domain's te_db rows. One payload goes to every node,
        so a route carries the answer for every ingress at once:

            segments             the any-source answer: what every ingress
                                 pushes unless it has an entry below
            segments_by_source   {source host: transit SIDs}, one entry per
                                 source whose paths apply to the route;
                                 the agent of that host uses it instead
            segments_by_source_port
                                 {source port id: {host, network_id,
                                 prefixes, segments}}, see below

        Precedence per route and ingress, highest first:

            (port, source VM) > (host, source VM) > (port, source)
            > (host, source) > (port, any) > (host, any) > direct

        The agent's choice of its own entry over `segments` is what puts
        every source path above every any-source path. An entry may be []
        -- an explicit or degraded "direct from this source" -- and must
        still be sent, or that ingress would fall back to `segments`.

        A source that is the route's own host gets no entry: that node never
        encapsulates its own VMs' traffic (the agent skips the route). The
        keys are added only when non-empty, so a route nothing steers stays
        byte-for-byte the plain route.

        Transit SIDs only, as resolved: the agent drops its own leading SID
        and appends the decap SID of the route's host.

        vms is {source port id: {'host', 'network_id', 'prefixes'}} for the
        source VMs the plugin found; one that is unbound, gone (absent) or on
        a node without a locator gets no entry, so its traffic follows the
        other ranks.
        """
        ranked = rank(paths)
        by_port, by_host = ranked['by_port'], ranked['by_host']
        by_source = ranked['by_source']
        for route in routes:
            transit = self._route_transit(domain_id, route, by_port, by_host)
            if transit:
                route['segments'] = transit
            per_source = {}
            for source in sorted(by_source):
                if source == route.get('host'):
                    continue
                transit = self._route_transit(
                    domain_id, route, by_source[source]['by_port'],
                    by_source[source]['by_host'], source=source)
                if transit is not None:
                    per_source[source] = transit
            if per_source:
                route['segments_by_source'] = per_source
        vms = {port_id: vm for port_id, vm in sorted((vms or {}).items())
               if port_id in ranked['by_source_port'] and vm.get('host') and
               self.locators.get(vm['host'])}
        for route in routes:
            per_vm = {}
            for port_id, vm in vms.items():
                if vm['host'] == route.get('host'):
                    continue
                maps = ranked['by_source_port'][port_id]
                transit = self._route_transit(
                    domain_id, route, maps['by_port'], maps['by_host'],
                    source=vm['host'])
                if transit is not None:
                    per_vm[port_id] = dict(vm, segments=transit)
            if per_vm:
                route['segments_by_source_port'] = per_vm
        return routes

    # --- the API ------------------------------------------------------------
    def resolve(self, path, function_id, bindings=None):
        """A copy of path with `segments`, `encoded_segments` and `status`.

        bindings is {port id: host it is bound to, or None} for the path's
        destination and source ports.

        ACTIVE:   [<via SIDs>..., <destination decap SID>]
        DEGRADED: [], because transit says so (a via host is unusable or
                  the source has no locator), the destination has no
                  locator, its port is unbound, or the domain has no
                  function id; the routes go direct meanwhile.
                  For a path with a source, also when the source VM is
                  unbound, or the destination is the source itself (a port
                  path whose VM has moved onto it): that agent never
                  encapsulates traffic to its own VMs, so the path steers
                  nothing.

        For a path with a source the source is the only ingress, so
        `segments` leaves out leading SIDs of that node, exactly as its
        agent drops them. `encoded_segments` is what the ingress pushes: the
        same list, C-SID containers in usid mode, by the sid.encode the
        agent calls.

        Server resolution, not an agent acknowledgement: ACTIVE says the
        payload carries this list, not that any kernel holds it.
        """
        bindings = bindings or {}
        path = dict(path)
        source = path.get(te_def.SOURCE_HOST)
        source_port_id = path.get(te_def.SOURCE_PORT_ID)
        if source_port_id:
            # the node the VM is bound to now: the path follows it
            source = bindings.get(source_port_id)
        port_id = path.get(te_def.DESTINATION_PORT_ID)
        destination = (bindings.get(port_id) if port_id
                       else path.get(te_def.DESTINATION_HOST))
        transit = None
        if not (source_port_id and not source) and destination != source:
            transit = self.transit(path[te_def.VIA_HOSTS], source)
        if (transit is not None and destination and
                self.locators.get(destination) and function_id is not None):
            if source:
                transit = sid.drop_leading(transit, self._node_sid(source))
            segments = transit + [
                sid.format_sid(self.locators[destination], function_id)]
            path[te_def.SEGMENTS] = segments
            path[te_def.ENCODED_SEGMENTS] = sid.encode(segments, self.usid)
            path[te_def.STATUS] = te_def.STATUS_ACTIVE
            return path
        LOG.warning("SRv6 TE path %(id)s is DEGRADED: destination %(dst)s, "
                    "source %(src)s, via %(via)s; not every host on it has "
                    "a registered locator or is dead, the port is unbound, "
                    "or the destination is the source itself, so its "
                    "routes go direct", {'id': path['id'], 'dst': destination,
                                         'src': source or 'any',
                                         'via': path[te_def.VIA_HOSTS]})
        path[te_def.SEGMENTS] = []
        path[te_def.ENCODED_SEGMENTS] = []
        path[te_def.STATUS] = te_def.STATUS_DEGRADED
        return path
