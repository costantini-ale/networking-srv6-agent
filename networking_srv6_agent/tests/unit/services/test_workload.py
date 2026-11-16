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

"""Workload endpoints: plain port dicts in, endpoints out, no mocks."""

from neutron.tests import base
from neutron_lib.api.definitions import portbindings
from neutron_lib import constants as n_const

from networking_srv6_agent.services import workload

DOWN = n_const.PORT_STATUS_DOWN


def _port(port_id='p1', **kwargs):
    port = {'id': port_id, 'network_id': 'net1',
            'device_owner': 'compute:nova',
            'status': n_const.PORT_STATUS_ACTIVE,
            'fixed_ips': [{'ip_address': '10.0.0.5'}],
            portbindings.HOST_ID: 'node1'}
    port.update(kwargs)
    return port


def _ips(*addresses):
    return [{'ip_address': a} for a in addresses]


class TestEndpoint(base.BaseTestCase):

    def test_which_ports_are_endpoints(self):
        # (case, port, migrating, expected host or None)
        cases = [
            ('active VM', _port(), False, 'node1'),
            ('dhcp', _port(device_owner='network:dhcp'), False, None),
            ('router', _port(device_owner='network:router_interface'),
             False, None),
            ('gateway', _port(device_owner='network:router_gateway'),
             False, None),
            ('no owner', _port(device_owner=None), False, 'node1'),
            ('unbound', _port(**{portbindings.HOST_ID: ''}), False, None),
            ('down', _port(status=DOWN), False, None),
            ('down, migrating', _port(status=DOWN), True, 'node1'),
            ('dhcp, migrating', _port(device_owner='network:dhcp'), True,
             None),
            ('host fallback', _port(host='node2',
                                    **{portbindings.HOST_ID: ''}),
             False, 'node2'),
            ('binding wins', _port(host='node2'), False, 'node1'),
        ]
        for case, port, migrating, host in cases:
            ep = workload.endpoint(port, migrating)
            self.assertEqual(host, ep and ep.host, case)

    def test_the_endpoint_carries_port_network_host_and_prefixes(self):
        self.assertEqual(
            workload.Endpoint('p1', 'net1', 'node1', ('10.0.0.5/32',)),
            workload.endpoint(_port()))

    def test_prefixes(self):
        # (case, fixed_ips, expected prefixes)
        cases = [
            ('ipv4', _ips('10.0.0.5'), ('10.0.0.5/32',)),
            ('ipv6', _ips('2001:db8::5'), ('2001:db8::5/128',)),
            ('several, in order', _ips('10.0.0.6', 'fd00::6', '10.0.0.5'),
             ('10.0.0.6/32', 'fd00::6/128', '10.0.0.5/32')),
            ('ipv6 normalised', _ips('2001:DB8:0:0::0005'),
             ('2001:db8::5/128',)),
            ('empty address skipped', [{'ip_address': ''}, {}], ()),
            ('none', [], ()),
        ]
        for case, fixed_ips, prefixes in cases:
            self.assertEqual(prefixes, workload.endpoint(
                _port(fixed_ips=fixed_ips)).prefixes, case)

    def test_workload_only_false_keeps_host_and_address_rules(self):
        ep = workload.endpoint(_port(status=DOWN, device_owner='network:dhcp',
                                     host='node2',
                                     **{portbindings.HOST_ID: ''}),
                               workload_only=False)
        self.assertEqual(('node2', ('10.0.0.5/32',)), (ep.host, ep.prefixes))
        self.assertIsNone(workload.endpoint(_port(**{portbindings.HOST_ID:
                                                     None}),
                                            workload_only=False))

    def test_no_port_is_no_endpoint(self):
        self.assertIsNone(workload.endpoint(None))
        self.assertIsNone(workload.endpoint({}, workload_only=False))

    def test_endpoints_takes_the_migrating_ids(self):
        ports = [_port('a'), _port('b', status=DOWN), _port('c', status=DOWN),
                 _port('d', device_owner='network:dhcp')]
        self.assertEqual(['a', 'b'], [ep.port_id for ep in
                                      workload.endpoints(ports, {'b'})])
        self.assertEqual(['a'], [ep.port_id for ep in
                                 workload.endpoints(ports)])

    def test_the_profile_marker_is_the_event_signal(self):
        marked = _port(**{portbindings.PROFILE: {'migrating_to': 'node2'}})
        self.assertTrue(workload.is_migrating(_port(), marked))
        self.assertFalse(workload.is_migrating(
            _port(), _port(**{portbindings.PROFILE: None})))


class TestClassify(base.BaseTestCase):

    def _deltas(self, original, port):
        deltas, first = workload.classify(original, port)
        return [(op, ep.host, ep.prefixes) for op, ep in deltas], first

    def test_cases(self):
        add, rm = workload.ADD, workload.REMOVE
        down = _port(status=DOWN)
        marked = {portbindings.PROFILE: {'migrating_to': 'node2'}}
        on2 = {portbindings.HOST_ID: 'node2'}
        p5 = ('10.0.0.5/32',)
        # (case, original, port, expected deltas, expected first node)
        cases = [
            ('activate', down, _port(), [(add, 'node1', p5)], None),
            ('activate, no original', None, _port(),
             [(add, 'node1', p5)], None),
            ('activate a dhcp port',
             _port(status=DOWN, device_owner='network:dhcp'),
             _port(device_owner='network:dhcp'), [], None),
            ('deactivate', _port(), down, [(rm, 'node1', p5)], None),
            ('deactivate while migrating', _port(**marked),
             _port(status=DOWN, **marked), [], None),
            ('deactivate, marker gone after', _port(**marked), down, [],
             None),
            ('handover', _port(status=DOWN, **marked),
             _port(status=DOWN, **dict(on2, **{portbindings.PROFILE: {}})),
             [(add, 'node2', p5)], 'node2'),
            ('down host change, no migration', down,
             _port(status=DOWN, **on2), [], None),
            ('active move', _port(), _port(**on2),
             [(rm, 'node1', p5), (add, 'node2', p5)], None),
            ('active move via host fallback',
             _port(host='node1', **{portbindings.HOST_ID: ''}),
             _port(host='node1', **on2),
             [(rm, 'node1', p5), (add, 'node2', p5)], None),
            ('address added', _port(),
             _port(fixed_ips=_ips('10.0.0.5', 'fd00::21')),
             [(add, 'node1', ('fd00::21/128',))], None),
            ('address replaced', _port(),
             _port(fixed_ips=_ips('10.0.0.6')),
             [(rm, 'node1', p5), (add, 'node1', ('10.0.0.6/32',))], None),
            ('address removed', _port(fixed_ips=_ips('10.0.0.5', '10.0.0.6')),
             _port(), [(rm, 'node1', ('10.0.0.6/32',))], None),
            ('address added to a dhcp port',
             _port(device_owner='network:dhcp'),
             _port(device_owner='network:dhcp',
                   fixed_ips=_ips('10.0.0.5', '10.0.0.6')), [], None),
            ('reordered', _port(fixed_ips=_ips('10.0.0.5', '10.0.0.6')),
             _port(fixed_ips=_ips('10.0.0.6', '10.0.0.5')), [], None),
            ('text only', _port(fixed_ips=_ips('FD00::21')),
             _port(fixed_ips=_ips('fd00::21')), [], None),
            ('no change', _port(), _port(), [], None),
            ('still down', down, _port(status=DOWN, name='x'), [], None),
        ]
        for case, original, port, deltas, first in cases:
            self.assertEqual((deltas, first), self._deltas(original, port),
                             case)
