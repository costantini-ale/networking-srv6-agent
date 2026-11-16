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

"""The TE resolver: plain data in, data out.

Precedence, the DEGRADED fallback, dead waypoints, microSID transit lists
and path status, with no plugin and no mocks. The plugin's own reads (the
paths, the port bindings, the agents' liveness) are in test_te.py.
"""

from unittest import mock

from neutron.tests import base

from networking_srv6_agent.agent import dataplane
from networking_srv6_agent.common import constants
from networking_srv6_agent.common import sid
from networking_srv6_agent.services import te_resolver
from networking_srv6_agent.tests.unit.services import test_te

LOCATORS = {'node1': test_te.LOC1, 'node2': test_te.LOC2,
            'node3': test_te.LOC3}
E1, E2, E3 = test_te.E1, 'fc00:0:2:fff0::', test_te.E3
_route, _db_path, _rows = test_te._route, test_te._db_path, test_te._rows


def _resolver(dead=frozenset(), usid=False, fabric=None):
    return te_resolver.Resolver(dict(LOCATORS), fabric, dead, usid)


class ResolverTestCase(base.BaseTestCase):

    usid = False
    dead = frozenset()

    def _attach(self, *routes, vms=None, **maps):
        return _resolver(self.dead, self.usid).attach(
            'd1', [dict(r) for r in routes], _rows(**maps), vms)

    def _resolve(self, path, function_id=7, bindings=None):
        return _resolver(self.dead, self.usid).resolve(path, function_id,
                                                       bindings)


class TestPrecedence(ResolverTestCase):
    """Port path > host path > direct."""

    def test_a_port_path_beats_the_host_path(self):
        routes = self._attach(_route('p1'), _route('p2', prefix='10.0.0.6/32'),
                              by_port={'p1': ['node3']},
                              by_host={'node2': ['node1']})
        self.assertEqual([E3], routes[0]['segments'])
        # G3: the neighbour follows the host path.
        self.assertEqual([E1], routes[1]['segments'])

    def test_a_host_path_leaves_other_hosts_alone(self):
        route = self._attach(_route('p1', host='node3'),
                             by_host={'node2': ['node1']})[0]
        self.assertNotIn('segments', route)

    def test_an_empty_port_path_is_direct_even_under_a_host_path(self):
        self.assertNotIn('segments', self._attach(
            _route('p1'), by_port={'p1': []},
            by_host={'node2': ['node1']})[0])

    def test_no_path_leaves_the_route_untouched(self):
        self.assertEqual([_route('p1')], self._attach(_route('p1')))

    def test_transit_sids_only_in_traversal_order(self):
        # The agent appends the decap SID itself.
        self.assertEqual([E3, E1], self._attach(
            _route('p1'), by_port={'p1': ['node3', 'node1']})[0]['segments'])

    def test_a_degraded_port_path_goes_direct_not_to_the_host_path(self):
        with mock.patch.object(te_resolver.LOG, 'warning') as warning:
            route = self._attach(_route('p1'), by_port={'p1': ['node9']},
                                 by_host={'node2': ['node1']})[0]
        self.assertNotIn('segments', route)
        self.assertTrue(warning.called)

    def test_a_degraded_host_path_goes_direct(self):
        self.assertNotIn('segments', self._attach(
            _route('p1'), by_host={'node2': ['node3', 'node9']})[0])


class TestSourcePrecedence(ResolverTestCase):
    """(port, src) > (host, src) > (port, any) > (host, any) > direct.

    The routes go to every node. `segments` is what every ingress pushes;
    `segments_by_source` holds the answer of each source whose paths
    apply, and that node's agent uses it instead -- even when it is [].
    """

    def test_only_source_paths_still_steer(self):
        route = self._attach(_route('p1'),
                             by_source={'node1': (None, {'node2': ['node3']})}
                             )[0]
        self.assertNotIn('segments', route)
        self.assertEqual({'node1': [E3]}, route['segments_by_source'])

    def test_a_source_port_path_beats_a_source_host_path(self):
        steered, neighbour = self._attach(
            _route('p1'), _route('p2', prefix='10.0.0.6/32'),
            by_source={'node1': ({'p1': []}, {'node2': ['node3']})})
        self.assertEqual({'node1': []}, steered['segments_by_source'])
        self.assertEqual({'node1': [E3]}, neighbour['segments_by_source'])

    def test_a_source_host_path_beats_an_any_source_port_path(self):
        # The source rank wins, at that ingress only.
        route = self._attach(
            _route('p1'), by_port={'p1': ['node1']},
            by_source={'node1': (None, {'node2': ['node3']})})[0]
        self.assertEqual([E1], route['segments'])
        self.assertEqual({'node1': [E3]}, route['segments_by_source'])

    def test_an_explicitly_direct_source_path_is_sent_empty(self):
        # Left out, node1 would fall back to `segments`.
        route = self._attach(_route('p1'), by_host={'node2': ['node3']},
                             by_source={'node1': (None, {'node2': []})})[0]
        self.assertEqual([E3], route['segments'])
        self.assertEqual({'node1': []}, route['segments_by_source'])

    def test_a_degraded_source_path_is_direct_for_its_source(self):
        # Not "fall back to the any-source path".
        with mock.patch.object(te_resolver.LOG, 'warning') as warning:
            route = self._attach(
                _route('p1'), by_host={'node2': ['node3']},
                by_source={'node1': (None, {'node2': ['node9']})})[0]
        self.assertEqual([E3], route['segments'])
        self.assertEqual({'node1': []}, route['segments_by_source'])
        self.assertTrue(warning.called)

    def test_a_source_without_a_locator_is_direct(self):
        with mock.patch.object(te_resolver.LOG, 'warning'):
            route = self._attach(
                _route('p1'),
                by_source={'node9': (None, {'node2': ['node3']})})[0]
        self.assertEqual({'node9': []}, route['segments_by_source'])

    def test_a_direct_any_source_port_path_keeps_the_source_entry(self):
        route = self._attach(
            _route('p1'), by_port={'p1': []},
            by_source={'node1': (None, {'node2': ['node3']})})[0]
        self.assertNotIn('segments', route)
        self.assertEqual({'node1': [E3]}, route['segments_by_source'])

    def test_no_applicable_source_path_adds_no_key(self):
        # node1's path goes to node3; this route is on node2.
        route = self._attach(
            _route('p1'), by_host={'node2': ['node3']},
            by_source={'node1': (None, {'node3': ['node2']})})[0]
        self.assertEqual([E3], route['segments'])
        self.assertNotIn('segments_by_source', route)

    def test_the_routes_own_host_gets_no_entry(self):
        # A port path whose VM has moved onto its source: that node never
        # encapsulates traffic toward its own VMs.
        self.assertNotIn('segments_by_source', self._attach(
            _route('p1'), by_source={'node2': ({'p1': ['node3']}, None)})[0])

    def test_each_source_gets_its_own_entry(self):
        route = self._attach(
            _route('p1'), by_host={'node2': ['node1']},
            by_source={'node1': (None, {'node2': ['node3']}),
                       'node3': (None, {'node2': []})})[0]
        self.assertEqual([E1], route['segments'])
        self.assertEqual({'node1': [E3], 'node3': []},
                         route['segments_by_source'])

    def test_a_source_entry_keeps_the_sources_own_sid(self):
        # The server sends the list as resolved; the source's agent drops
        # its own leading SID, as it does for `segments`.
        self.assertEqual({'node1': [E1, E3]}, self._attach(
            _route('p1'),
            by_source={'node1': (None, {'node2': ['node1', 'node3']})})[0][
                'segments_by_source'])


VM, DST = 'vm-port', 'dst-port'
VM_ON_1 = {'host': 'node1', 'network_id': 'net1',
           'prefixes': ['10.0.0.9/32']}


class TestSourceVmPrecedence(ResolverTestCase):
    """segments_by_source_port: the paths of one source VM."""

    def test_the_entry_names_the_node_network_prefixes_and_segments(self):
        [r] = self._attach(_route(DST), vms={VM: VM_ON_1},
                           by_source_port={VM: ({DST: ['node3']}, None)})
        self.assertEqual({VM: dict(VM_ON_1, segments=[E3])},
                         r['segments_by_source_port'])
        self.assertNotIn('segments', r)
        self.assertNotIn('segments_by_source', r)

    def test_port_path_over_host_path_and_an_explicit_direct(self):
        to_dst, to_host = self._attach(
            _route(DST), _route('another', prefix='10.0.0.6/32'),
            vms={VM: VM_ON_1},
            by_source_port={VM: ({DST: []}, {'node2': ['node3']})})
        self.assertEqual([], to_dst['segments_by_source_port'][VM][
            'segments'])
        self.assertEqual([E3],
                         to_host['segments_by_source_port'][VM]['segments'])

    def test_a_route_without_a_vm_path_gets_no_key(self):
        [r] = self._attach(_route(DST), vms={VM: VM_ON_1},
                           by_source_port={VM: (None, {'node3': []})})
        self.assertNotIn('segments_by_source_port', r)

    def test_a_route_to_the_vms_own_node_gets_no_entry(self):
        [r] = self._attach(_route('local', host='node1'), vms={VM: VM_ON_1},
                           by_source_port={VM: (None, {'node1': []})})
        self.assertNotIn('segments_by_source_port', r)

    def test_an_unbound_absent_or_unregistered_vm_gets_no_entry(self):
        maps = ({DST: ['node3']}, None)
        [r] = self._attach(
            _route(DST),
            vms={VM: dict(VM_ON_1, host=None),
                 'elsewhere': dict(VM_ON_1, host='node9')},
            by_source_port={VM: maps, 'gone': maps, 'elsewhere': maps})
        self.assertNotIn('segments_by_source_port', r)


class TestDeadWaypoint(ResolverTestCase):
    """A path through a node that is down goes direct.

    Direct, not "skip the dead hop" and not "fall to the host path": the
    same answer as an unknown locator.
    """

    dead = frozenset({'node1'})

    def test_a_host_path_through_a_dead_node_goes_direct(self):
        with mock.patch.object(te_resolver.LOG, 'warning') as warning:
            route = self._attach(_route('p1'),
                                 by_host={'node2': ['node1']})[0]
        self.assertNotIn('segments', route)
        self.assertTrue(warning.called)

    def test_the_same_path_is_steered_while_the_node_lives(self):
        self.dead = frozenset()
        self.assertEqual([E1], self._attach(
            _route('p1'), by_host={'node2': ['node1']})[0]['segments'])

    def test_a_dead_node_off_the_path_changes_nothing(self):
        self.assertEqual([E3], self._attach(
            _route('p1'), by_host={'node2': ['node3']})[0]['segments'])

    def test_one_dead_hop_sends_the_whole_path_direct(self):
        # Not a shorter list: that is the partial steering the plugin refuses.
        self.assertNotIn('segments', self._attach(
            _route('p1'), by_host={'node2': ['node3', 'node1']})[0])

    def test_a_dead_port_path_goes_direct_not_to_the_host_path(self):
        route = self._attach(_route('p1'), _route('p2', prefix='10.0.0.6/32'),
                             by_port={'p1': ['node1']},
                             by_host={'node2': ['node3']})
        self.assertNotIn('segments', route[0])
        self.assertEqual([E3], route[1]['segments'])

    def test_a_dead_node_off_a_source_path_leaves_it(self):
        self.dead = frozenset({'node2'})
        route = self._attach(
            _route('p1'), by_source={'node1': (None, {'node2': ['node3']})})[0]
        self.assertEqual({'node1': [E3]}, route['segments_by_source'])

    def test_a_source_path_through_a_dead_node_is_direct_for_its_source(self):
        route = self._attach(
            _route('p1'), by_host={'node2': ['node3']},
            by_source={'node3': (None, {'node2': ['node1']})})[0]
        self.assertEqual([E3], route['segments'])
        self.assertEqual({'node3': []}, route['segments_by_source'])

    def test_a_dead_destination_does_not_unsteer_its_path(self):
        # Its VMs are gone with it; nothing to reroute.
        self.dead = frozenset({'node2'})
        self.assertEqual([E3], self._attach(
            _route('p1'), by_host={'node2': ['node3']})[0]['segments'])

    def test_the_path_reports_degraded(self):
        path = self._resolve(_db_path(destination_host='node2',
                                      via_hosts=['node1']))
        self.assertEqual(('DEGRADED', [], []),
                         (path['status'], path['segments'],
                          path['encoded_segments']))

    def test_a_dead_destination_or_source_does_not_degrade_the_path(self):
        self.dead = frozenset({'node2', 'node3'})
        path = self._resolve(_db_path(destination_host='node2',
                                      source_host='node3',
                                      via_hosts=['node1']))
        self.assertEqual('ACTIVE', path['status'])


class TestResolve(ResolverTestCase):

    def test_shape_and_order(self):
        path = self._resolve(_db_path(destination_host='node2',
                                      via_hosts=['node3', 'node1']))
        self.assertEqual([E3, E1, 'fc00:0:2:7::'], path['segments'])
        self.assertEqual('ACTIVE', path['status'])

    def test_the_function_id_is_hex_in_the_sid(self):
        path = self._resolve(_db_path(destination_host='node2'),
                             function_id=17)
        self.assertEqual(['fc00:0:2:11::'], path['segments'])

    def test_a_port_path_resolves_through_its_binding(self):
        path = self._resolve(_db_path(destination_port_id='p1',
                                      via_hosts=['node2']),
                             bindings={'p1': 'node2'})
        self.assertEqual([E2, 'fc00:0:2:7::'], path['segments'])

    def test_an_unbound_or_absent_port_is_degraded(self):
        for bindings in ({'p1': None}, {}):
            path = self._resolve(_db_path(destination_port_id='p1'),
                                 bindings=bindings)
            self.assertEqual(([], 'DEGRADED'), (path['segments'],
                                                path['status']))

    def test_a_lost_waypoint_is_degraded(self):
        # G6: the via host's locator row is gone.
        path = self._resolve(_db_path(destination_host='node2',
                                      via_hosts=['node9']))
        self.assertEqual(([], 'DEGRADED'), (path['segments'],
                                            path['status']))

    def test_a_destination_without_a_locator_is_degraded(self):
        self.assertEqual('DEGRADED', self._resolve(
            _db_path(destination_host='node9'))['status'])

    def test_a_domain_without_a_function_id_is_degraded(self):
        self.assertEqual('DEGRADED', self._resolve(
            _db_path(destination_host='node2'), function_id=None)['status'])

    def test_a_source_path_drops_its_own_leading_sid(self):
        # The source is the only ingress, and its agent drops the SID.
        path = self._resolve(_db_path(destination_host='node2',
                                      source_host='node1',
                                      via_hosts=['node1', 'node3']))
        self.assertEqual([E3, 'fc00:0:2:7::'], path['segments'])
        self.assertEqual(path['segments'], path['encoded_segments'])
        self.assertEqual('ACTIVE', path['status'])

    def test_only_leading_source_sids_are_dropped(self):
        path = self._resolve(_db_path(destination_host='node2',
                                      source_host='node1',
                                      via_hosts=['node3', 'node1']))
        self.assertEqual([E3, E1, 'fc00:0:2:7::'], path['segments'])

    def test_an_any_source_path_keeps_every_sid(self):
        # Any node may be the ingress; each strips its own SID itself.
        path = self._resolve(_db_path(destination_host='node2',
                                      via_hosts=['node1', 'node3']))
        self.assertEqual([E1, E3, 'fc00:0:2:7::'], path['segments'])

    def test_a_source_without_a_locator_is_degraded(self):
        self.assertEqual('DEGRADED', self._resolve(
            _db_path(destination_host='node2', source_host='node9'))[
                'status'])

    def test_a_port_moved_onto_its_source_is_degraded(self):
        path = self._resolve(_db_path(destination_port_id='p1',
                                      source_host='node1',
                                      via_hosts=['node3']),
                             bindings={'p1': 'node1'})
        self.assertEqual(([], 'DEGRADED'), (path['segments'],
                                            path['status']))

    def test_a_source_vm_follows_its_binding(self):
        path = _db_path(source_port_id=VM, destination_port_id=DST,
                        via_hosts=['node1', 'node3'])
        self.assertEqual([E3, 'fc00:0:2:7::'], self._resolve(
            path, bindings={VM: 'node1', DST: 'node2'})['segments'])
        self.assertEqual([E1, E3, 'fc00:0:2:7::'], self._resolve(
            path, bindings={VM: 'node3', DST: 'node2'})['segments'])
        self.assertEqual('DEGRADED', self._resolve(
            path, bindings={VM: None, DST: 'node2'})['status'])

    def test_the_db_dict_is_not_mutated(self):
        original = _db_path(destination_host='node2')
        self._resolve(original)
        self.assertEqual(_db_path(destination_host='node2'), original)

    def test_a_fabric_node_is_a_usable_waypoint(self):
        # registered hosts win over the topology file
        resolver = _resolver(fabric={'s1': 'fc00:0:c1::/48',
                                     'node1': 'fc00:0:99::/48'})
        self.assertEqual(['fc00:0:c1:fff0::', E1],
                         resolver.transit(['s1', 'node1']))


class TestUsid(ResolverTestCase):

    usid = True

    def test_transit_sids_are_un(self):
        self.assertEqual(['fc00:0:3::', 'fc00:0:1::'],
                         _resolver(usid=True).transit(['node3', 'node1']))

    def test_a_path_shows_its_container(self):
        path = self._resolve(_db_path(destination_host='node2',
                                      via_hosts=['node3', 'node1']),
                             function_id=0xe005)
        self.assertEqual(['fc00:0:3::', 'fc00:0:1::', 'fc00:0:2:e005::'],
                         path['segments'])
        self.assertEqual(['fc00:0:3:1:2:e005::'], path['encoded_segments'])

    def test_a_source_entry_is_un(self):
        route = self._attach(_route('p1'),
                             by_source={'node1': (None, {'node2': ['node3']})}
                             )[0]
        self.assertEqual({'node1': ['fc00:0:3::']},
                         route['segments_by_source'])

    def test_a_source_path_container_leaves_out_the_source(self):
        # Ingress node1, first waypoint node1: its uN is dropped, so the
        # container starts at node3.
        path = self._resolve(_db_path(destination_host='node2',
                                      source_host='node1',
                                      via_hosts=['node1', 'node3']),
                             function_id=0xe005)
        self.assertEqual(['fc00:0:3:2:e005::'], path['encoded_segments'])

    def test_a_degraded_path_pushes_nothing(self):
        path = self._resolve(_db_path(destination_host='nowhere'),
                             function_id=0xe005)
        self.assertEqual(([], []), (path['segments'],
                                    path['encoded_segments']))

    def test_full_sids_encode_to_the_same_list(self):
        path = _resolver(usid=False).resolve(
            _db_path(destination_host='node2', via_hosts=['node1']), 0xe007)
        self.assertEqual([E1, 'fc00:0:2:e007::'], path['encoded_segments'])


class TestShownIsPushed(base.BaseTestCase):
    """The list GET shows is the list the path's ingress pushes.

    Each case is a path, the route it steers, and the node that
    encapsulates (None: any ingress). The pushed list is read with the
    agent's own lookup (_segments_for, or the source VM's entry), then
    trimmed and completed as the agent does: its own leading SID dropped,
    the decap SID of the route's host appended.
    """

    FID = 0xe005
    BINDINGS = {'p1': 'node2', 'p2': 'node3', 'p3': 'node2', 'p4': 'node3',
                'p5': 'node1', 'p6': 'node2', VM: 'node1'}
    CASES = [
        # any source, port path
        (_db_path(id='a', destination_port_id='p1',
                  via_hosts=['node1', 'node3']), 'p1', None),
        # source node1, host path whose first waypoint is node1 itself
        (_db_path(id='b', destination_host='node3', source_host='node1',
                  via_hosts=['node1', 'node2']), 'p2', 'node1'),
        # source VM on node1
        (_db_path(id='c', destination_port_id='p3', source_port_id=VM,
                  via_hosts=['node3']), 'p3', VM),
        # a waypoint with no locator: DEGRADED, direct
        (_db_path(id='d', destination_port_id='p4', via_hosts=['node9']),
         'p4', None),
        # a dead waypoint, from one source: DEGRADED, direct from there
        (_db_path(id='e', destination_port_id='p5', source_host='node2',
                  via_hosts=['node3']), 'p5', 'node2'),
        # a source with no locator: DEGRADED, direct from there
        (_db_path(id='f', destination_port_id='p6', source_host='node9',
                  via_hosts=['node1']), 'p6', 'node9'),
    ]

    def _check(self, usid):
        resolver = te_resolver.Resolver(dict(LOCATORS), None, frozenset(),
                                        usid)
        dead = te_resolver.Resolver(dict(LOCATORS), None,
                                    frozenset({'node3'}), usid)
        vms = {VM: VM_ON_1}
        for path, port_id, ingress in self.CASES:
            r = dead if path['id'] == 'e' else resolver
            host = self.BINDINGS[port_id]
            [route] = r.attach('d1', [_route(port_id, host=host)], [path],
                               vms)
            shown = r.resolve(path, self.FID, self.BINDINGS)
            if ingress == VM:
                node = self.BINDINGS[VM]
                entry = route.get('segments_by_source_port', {}).get(VM)
                pushed = entry['segments'] if entry else []
            else:
                node = ingress
                pushed = dataplane.Srv6LinuxDataplane._segments_for(
                    route, ingress) or []
            if shown['status'] == 'DEGRADED':
                self.assertEqual([], pushed, path['id'])
                continue
            if node:
                own = (sid.node_sid(LOCATORS[node]) if usid else
                       sid.format_sid(LOCATORS[node],
                                      constants.NODE_END_FUNCTION_ID))
                pushed = sid.drop_leading(pushed, own)
            decap = sid.format_sid(LOCATORS[host], self.FID)
            self.assertEqual(shown['segments'], pushed + [decap], path['id'])
            self.assertEqual(shown['encoded_segments'],
                             sid.encode(pushed + [decap], usid), path['id'])

    def test_full_sids(self):
        self._check(usid=False)

    def test_usid(self):
        self._check(usid=True)
