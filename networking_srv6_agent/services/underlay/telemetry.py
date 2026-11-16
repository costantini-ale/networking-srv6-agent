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

"""Underlay -- the measured link state and VM-pair loads of the topology.

Ingestion of the fabric's link-state and aggregate-load batches, their
history, the views the srv6_link_states and srv6_aggregate_loads APIs
show, what the re-optimisation reads from them (load series, dead links,
measured load, VM pairs) and the re-optimisation's worker. The plugin
forwards the APIs to it; this reaches the plugin only through the public
methods of the package docstring.
"""

import collections
import datetime

from neutron import worker as neutron_worker
from neutron_lib.db import utils as db_utils
from neutron_lib import exceptions as n_exc
from oslo_log import log as logging
from oslo_utils import timeutils

from networking_srv6_agent._i18n import _
from networking_srv6_agent.api.definitions import srv6_aggregate_loads \
    as ag_def
from networking_srv6_agent.api.definitions import srv6_link_states as ls_def
from networking_srv6_agent.db import aggregate_load_db
from networking_srv6_agent.db import domain_db
from networking_srv6_agent.db import link_state_db
from networking_srv6_agent.db import srv6_db
from networking_srv6_agent.extensions import srv6_aggregate_loads as \
    ag_ext
from networking_srv6_agent.extensions import srv6_link_states as ls_ext
from networking_srv6_agent.services.underlay import aggregates
from networking_srv6_agent.services.underlay import reopt

LOG = logging.getLogger(__name__)

# Seconds by which a VM-pair row may precede its reporter's newest link-state
# row and still belong to the same report (aggregate_view).
AGGREGATE_HEARTBEAT_SLACK = 2


class Underlay:
    """The link capacities of [srv6] topology_file and their measurements.

    conf: the [srv6] group; capacity: {(a, b): Mbit/s}, {} without a
    topology file (then every POST of link state or aggregate load is
    refused); plugin: the SRv6 plugin, used only through the public
    methods listed in this package's docstring.
    """

    def __init__(self, conf, capacity, plugin):
        if not 0 < conf.te_reopt_low < conf.te_reopt_high:
            raise n_exc.InvalidConfigurationOption(
                opt_name='[srv6] te_reopt_low/te_reopt_high',
                opt_value='%s/%s (need 0 < low < high)' % (
                    conf.te_reopt_low, conf.te_reopt_high))
        self.conf = conf
        self.capacity = capacity
        self.plugin = plugin
        # (reporter, link) already warned about by _add_link_states
        self._unknown_links = set()
        # (reporter, link) already warned about by _add_aggregate_loads
        self._unknown_aggregate_links = set()

    def create_srv6_link_state_bulk(self, context, srv6_link_states):
        return self._add_link_states(
            context, [item[ls_def.RESOURCE_NAME]
                      for item in srv6_link_states[ls_def.COLLECTION_NAME]])

    def create_srv6_link_state(self, context, srv6_link_state):
        return self._add_link_states(
            context, [srv6_link_state[ls_def.RESOURCE_NAME]])[0]

    def _add_link_states(self, context, items):
        """Validate and store one batch of measurements, whole or not at all.

        Every item must be measured by one of its link's two ends, at most
        once per batch; mbps and drops are null or a non-negative finite
        number (the collector's data moves traffic, so nothing else is
        trusted). The server's clock stamps the batch, and the batch deletes
        its reporters' rows older than two windows.

        An item for a link the topology file does not have is skipped, with
        one WARNING per reporter and link, and the rest is stored: refusing
        the batch would leave its reporter silent, which reads as a dead
        node and sends every TE path through it direct. A batch with
        no known link at all is refused.
        """
        if not self.capacity:
            raise ls_ext.Srv6LinkStateInvalid(
                reason=_("link state needs an underlay topology "
                         "([srv6] topology_file)"))
        rows, seen, unknown = [], set(), []
        for item in items:
            row = {k: item[k] for k in ('reporter', 'from_node', 'to_node',
                                        'up', 'mbps', 'drops')}
            link = (row['from_node'], row['to_node'])
            name = '%s>%s' % link
            if link not in self.capacity:
                unknown.append(name)
                if (row['reporter'], link) not in self._unknown_links:
                    self._unknown_links.add((row['reporter'], link))
                    LOG.warning("SRv6 link state: %(r)s reports %(l)s, which "
                                "is not a link of the underlay topology "
                                "(%(f)s); ignoring it, storing the rest",
                                {'r': row['reporter'], 'l': name,
                                 'f': self.conf.topology_file})
                continue
            if row['reporter'] not in link:
                raise ls_ext.Srv6LinkStateInvalid(
                    reason=_("reporter %(r)s is not an end of %(l)s")
                    % {'r': row['reporter'], 'l': name})
            if (row['reporter'], link) in seen:
                raise ls_ext.Srv6LinkStateInvalid(
                    reason=_("%(r)s reported %(l)s twice")
                    % {'r': row['reporter'], 'l': name})
            seen.add((row['reporter'], link))
            # 1e7 Mbit/s = 10 Tbit/s: no link carries more; also NaN/inf
            for attr, kinds, top in (('mbps', (int, float), 1e7),
                                     ('drops', (int,), 2 ** 63)):
                v = row[attr]
                if v is not None and (isinstance(v, bool) or
                                      not isinstance(v, kinds) or
                                      not 0 <= v < top):
                    raise ls_ext.Srv6LinkStateInvalid(
                        reason=_("%(a)s of %(l)s must be null or a number "
                                 "from 0 to %(t)g") %
                        {'a': attr, 'l': name, 't': top})
            rows.append(row)
        if not rows:
            raise ls_ext.Srv6LinkStateInvalid(
                reason=_("%s: not a link of the underlay topology")
                % ', '.join(unknown))
        # whole seconds: MySQL DATETIME rounds the fraction, which could
        # store a measurement up to half a second in the future
        now = timeutils.utcnow().replace(microsecond=0)
        link_state_db.add_link_states(
            context, rows, now, now - datetime.timedelta(
                seconds=2 * self.conf.link_state_window))
        return [dict(r, id='%s:%s' % (r['from_node'], r['to_node']),
                     measured_at=now.isoformat()) for r in rows]

    @staticmethod
    def history(context):
        """{(from, to, reporter): [row, ...]}, each oldest first."""
        hist = collections.defaultdict(list)
        for r in link_state_db.get_link_states(context):   # oldest first
            hist[(r.from_node, r.to_node, r.reporter)].append(r)
        return hist

    @staticmethod
    def _ends(hist, a, b, fresh):
        """(ends, live) of the directed link a -> b.

        ends: the histories of the ends that reported it, the sender's
        (a's) first; live: those with a measurement since fresh. live[0]
        is the end that counts -- the sender if fresh, else the receiver
        -- for the view (admission, the API) and the re-optimisation alike.
        """
        ends = [h for h in (hist.get((a, b, a)), hist.get((a, b, b))) if h]
        return ends, [h for h in ends if h[-1].measured_at >= fresh]

    def link_state_view(self, context, hist=None):
        """{(from, to): entry} of every link of the topology file.

        hist: a history already read (the re-optimisation reads it once
        per pass); None reads it now.

        capacity_mbps is the file's capacity, shown next to what is
        measured (U-a). A link nobody has reported yet has null
        measurements and stale=true.

        Per directed link, the sender's measurement is the load (what was
        offered to the link) and the receiver's is what got through
        (delivered_mbps); a link only one end reports (leaf-host) has just
        that one. mbps comes from the sender if it is fresh, else from the
        receiver if fresh, else from whichever is newer (then stale). A
        measurement is fresh for link_state_stale_after seconds;
        peak_mbps is the highest mbps of that end in link_state_window.
        up is False if a fresh report from either end says so (a silent
        node is silent_fabric's).
        """
        now = timeutils.utcnow()
        fresh = now - datetime.timedelta(
            seconds=self.conf.link_state_stale_after)
        start = now - datetime.timedelta(seconds=self.conf.link_state_window)
        hist = self.history(context) if hist is None else hist
        view = {}
        for a, b in sorted(self.capacity):
            entry = {'id': '%s:%s' % (a, b), 'from_node': a, 'to_node': b,
                     'capacity_mbps': self.capacity[(a, b)]}
            view[(a, b)] = entry
            ends, live = self._ends(hist, a, b, fresh)
            if not ends:
                entry.update(dict.fromkeys(
                    ('reporter', 'up', 'mbps', 'drops', 'delivered_mbps',
                     'peak_mbps', 'measured_at', 'age')), stale=True)
                continue
            main = live[0] if live else max(
                ends, key=lambda h: h[-1].measured_at)
            last = main[-1]
            rx = hist.get((a, b, b))
            entry.update({
                'reporter': last.reporter,
                'up': (all(h[-1].up for h in live) if live
                       else last.up),
                'mbps': last.mbps,
                'drops': last.drops,
                'delivered_mbps': (rx[-1].mbps if rx and
                                   rx[-1].measured_at >= fresh else None),
                'peak_mbps': max((r.mbps for r in main
                                  if r.measured_at > start and
                                  r.mbps is not None), default=None),
                'measured_at': last.measured_at.isoformat(),
                'age': int((now - last.measured_at).total_seconds()),
                'stale': not live,
            })
        return view

    def get_srv6_link_states(self, context, filters=None, fields=None):
        filters = filters or {}
        return [db_utils.resource_fields(entry, fields)
                for entry in self.link_state_view(context).values()
                if all(entry[k] in filters[k]
                       for k in ('id', 'from_node', 'to_node')
                       if filters.get(k))]

    def get_srv6_link_state(self, context, id, fields=None):
        entry = self.link_state_view(context).get(tuple(id.split(':', 1)))
        if entry is None:
            raise ls_ext.Srv6LinkStateNotFound(id=id)
        return db_utils.resource_fields(entry, fields)

    def dead_links(self, context, silent, view=None):
        """The directed topology links down now.

        Reported down by a fresh end (or last seen down), or touching a
        silent node. view: a link_state_view already read.
        """
        view = self.link_state_view(context) if view is None else view
        return frozenset(link for link, entry in view.items()
                         if entry['up'] is False or
                         not silent.isdisjoint(link))

    def measured_load(self, context, view=None):
        """{(a, b): Mbit/s}: each fresh link's peak in link_state_window.

        What a link holds when automatic steering looks for room. A
        stale link is not here: it counts as empty. view: a
        link_state_view already read.
        """
        view = self.link_state_view(context) if view is None else view
        # whole Mbit/s, the unit of the capacities: idle
        # links' control chatter (~0.03) counts as 0, so equal paths still
        # tie and the choice stays deterministic
        return {link: round(entry['peak_mbps'])
                for link, entry in view.items()
                if not entry['stale'] and entry['peak_mbps'] is not None}

    def load_series(self, context, hist=None):
        """{(a, b): [(measured_at, mbps), ...]} oldest first, per link.

        The same end as the view's mbps: the sender's (offered load) if
        fresh, else the receiver's if fresh; a link with neither is not
        here. What the re-optimisation judges congestion on. hist: as in
        link_state_view.
        """
        fresh = timeutils.utcnow() - datetime.timedelta(
            seconds=self.conf.link_state_stale_after)
        hist = self.history(context) if hist is None else hist
        series = {}
        for a, b in self.capacity:
            _ends, live = self._ends(hist, a, b, fresh)
            if live:
                series[(a, b)] = [(r.measured_at, r.mbps) for r in live[0]]
        return series

    def create_srv6_aggregate_load_bulk(self, context, srv6_aggregate_loads):
        return self._add_aggregate_loads(
            context, [item[ag_def.RESOURCE_NAME] for item in
                      srv6_aggregate_loads[ag_def.COLLECTION_NAME]])

    def create_srv6_aggregate_load(self, context, srv6_aggregate_load):
        return self._add_aggregate_loads(
            context, [srv6_aggregate_load[ag_def.RESOURCE_NAME]])[0]

    def _add_aggregate_loads(self, context, items):
        """Validate and store one batch of VM-pair loads, whole or not at all.

        aggregates.check_batch says what is refused. Items on a link the
        topology file does not have are skipped with one WARNING per
        reporter and link, as for link states; a batch with no known link
        is refused. Stamped with the server's clock; the batch deletes its
        reporter's rows older than two link_state_window.
        """
        if not self.capacity:
            raise ag_ext.Srv6AggregateLoadInvalid(
                reason=_("aggregate loads need an underlay topology "
                         "([srv6] topology_file)"))
        try:
            rows, unknown = aggregates.check_batch(items, self.capacity)
        except aggregates.Invalid as e:
            raise ag_ext.Srv6AggregateLoadInvalid(reason=str(e))
        for link in unknown:
            reporter = link[0]
            if (reporter, link) not in self._unknown_aggregate_links:
                self._unknown_aggregate_links.add((reporter, link))
                LOG.warning("SRv6 aggregate load: %(r)s reports %(l)s, "
                            "which is not a link of the underlay topology "
                            "(%(f)s); ignoring it, storing the rest",
                            {'r': reporter, 'l': '%s>%s' % link,
                             'f': self.conf.topology_file})
        if not rows:
            raise ag_ext.Srv6AggregateLoadInvalid(
                reason=_("%s: not a link of the underlay topology") %
                ', '.join(sorted({'%s>%s' % link for link in unknown})))
        # whole seconds, as the link states (MySQL rounds the fraction)
        now = timeutils.utcnow().replace(microsecond=0)
        aggregate_load_db.add_aggregate_loads(
            context, rows, now, now - datetime.timedelta(
                seconds=2 * self.conf.link_state_window))
        return [dict(r, id=aggregates.entry_id(r),
                     measured_at=now.isoformat()) for r in rows]

    def aggregate_view(self, context):
        """[entry] of every VM pair measured on a link and still fresh.

        Fresh = within link_state_stale_after, the link states' rule; the
        peak is over link_state_window. The domain, ports and hosts are
        resolved now, from today's ports: a pair whose VM has gone keeps
        its rate with null ports.
        """
        now = timeutils.utcnow()
        fresh = now - datetime.timedelta(
            seconds=self.conf.link_state_stale_after)
        start = now - datetime.timedelta(seconds=self.conf.link_state_window)
        rows = aggregate_load_db.get_aggregate_loads(context,
                                                     min(fresh, start))
        if not rows:
            return []
        admin_context = context.elevated()
        domains = []
        for domain in domain_db.get_domains(admin_context):
            function_id = srv6_db.get_function_id(admin_context,
                                                  domain['id'])
            if function_id is not None:
                domains.append((domain, function_id))
        resolve = aggregates.Resolver(
            domains, srv6_db.get_host_locators(admin_context),
            self.plugin.usid,
            lambda domain: self.plugin.domain_addresses(admin_context,
                                                        domain))
        # The link-state batch every agent posts each period is the
        # heartbeat: a pair row older than its reporter's last one has left
        # the link. AGGREGATE_HEARTBEAT_SLACK: both POSTs of one period are
        # stamped in whole seconds, the pairs' just after the links'.
        current = {reporter: seen - datetime.timedelta(
            seconds=AGGREGATE_HEARTBEAT_SLACK) for reporter, seen in
            link_state_db.get_last_seen(context).items()}
        return aggregates.view(rows, now, fresh, start, resolve, current)

    def get_srv6_aggregate_loads(self, context, filters=None, fields=None,
                                 sorts=None, limit=None, marker=None,
                                 page_reverse=False):
        filters = filters or {}
        return [db_utils.resource_fields(entry, fields)
                for entry in self.aggregate_view(context)
                if all(entry[k] in filters[k]
                       for k in ('id', 'from_node', 'to_node', 'domain_id')
                       if filters.get(k))]

    def get_srv6_aggregate_load(self, context, id, fields=None):
        for entry in self.aggregate_view(context):
            if entry['id'] == id:
                return db_utils.resource_fields(entry, fields)
        raise ag_ext.Srv6AggregateLoadNotFound(id=id)

    def workers(self):
        """[the re-optimisation's periodic worker], or [].

        Only with te_reopt_interval not 0, a topology_file and
        te_auto_steering; its first pass waits one interval.
        """
        interval = self.conf.te_reopt_interval
        if not (interval and self.capacity and self.conf.te_auto_steering):
            return []
        return [neutron_worker.PeriodicWorker(
            reopt.TeReoptimiser(self).check, interval, interval,
            desc='srv6-te-reopt')]
