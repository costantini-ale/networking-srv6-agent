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

"""Workload endpoints: which Neutron ports carry tenant traffic, and where.

Plain port dicts in, plain data out; no database, no RPC. Every reader on
the server (the full domain payload, the incremental port path, the
aggregate-address view, the TE source-VM bindings) takes its ports from
here, so a full sync and the sum of the incremental updates agree.

A workload endpoint is a port that is not an infrastructure port, is bound
to a node, and is ACTIVE or live-migrating. Live migration has two signals,
because the two callers see the port at different times:

- the full sync reads the database: nova's second, INACTIVE binding, or
  the server's own handover mark for the moment after nova deletes that
  binding and before the port is ACTIVE (srv6_db.get_migrating_port_ids);
  the caller passes those ids in;
- a port event reads the profile's migrating_to marker on the state before
  or after the update (is_migrating), because the second binding may not
  be in the database yet.

Which nodes have a locator is not decided here: routes skip a node without
one, the aggregate view keeps it.
"""

import collections
import ipaddress

from neutron_lib.api.definitions import portbindings
from neutron_lib import constants as n_const


Endpoint = collections.namedtuple('Endpoint',
                                  'port_id network_id host prefixes')


def host_of(port):
    """The node a port is bound to: the binding host, else `host`."""
    return port.get(portbindings.HOST_ID) or port.get('host') or None


def prefixes_of(port):
    """The port's addresses as normalised /32 and /128 prefixes, in order."""
    out = []
    for fixed_ip in port.get('fixed_ips') or []:
        if fixed_ip.get('ip_address'):
            address = ipaddress.ip_address(fixed_ip['ip_address'])
            out.append('%s/%d' % (address, address.max_prefixlen))
    return tuple(out)


def is_migrating(*ports):
    """Nova marks a live migration with migrating_to in the profile."""
    return any('migrating_to' in (p.get(portbindings.PROFILE) or {})
               for p in ports)


def is_workload(port, migrating=False):
    """Not infrastructure, and up: ACTIVE, or DOWN while live-migrating.

    A live-migrating port is up although its status says DOWN: the VM runs
    on its source until the move, then on the destination.
    """
    if (port.get('device_owner') or '').startswith(
            n_const.DEVICE_OWNER_NETWORK_PREFIX):
        # DHCP, router and other infrastructure ports are not workloads.
        return False
    return migrating or port.get('status') == n_const.PORT_STATUS_ACTIVE


def endpoint(port, migrating=False, workload_only=True):
    """The port's endpoint, or None when it is unbound or no workload.

    workload_only=False skips the workload rule but keeps the host and
    address rules: what a withdrawal or a TE binding needs.
    """
    if not port:
        return None
    host = host_of(port)
    if not host or (workload_only and not is_workload(port, migrating)):
        return None
    return Endpoint(port['id'], port.get('network_id'), host,
                    prefixes_of(port))


def endpoints(ports, migrating_ids=frozenset()):
    """The workload endpoints among ports, in their order."""
    return [ep for ep in (endpoint(p, p['id'] in migrating_ids)
                          for p in ports) if ep]


ADD, REMOVE = 'add', 'remove'


def classify(original, port):
    """What a port update changes in the routes.

    Returns (deltas, first_host): deltas is an ordered list of (ADD or
    REMOVE, Endpoint), removals before additions so that two nodes never
    claim one address; first_host, for a live-migration handover, is the
    node that must hold the new route before anybody else. A withdrawal
    skips the workload rule: removing a route nobody holds is harmless.
    """
    original = original or {}
    was_active = original.get('status') == n_const.PORT_STATUS_ACTIVE
    is_active = port.get('status') == n_const.PORT_STATUS_ACTIVE
    old_host, new_host = host_of(original), host_of(port)
    moved = bool(old_host and new_host and old_host != new_host)
    withdraw = (REMOVE, endpoint(original, workload_only=False))
    if moved and not is_active and is_migrating(original, port):
        # Nova activated the destination binding: the VM runs there now
        # and its port is wired (pre-plugged). The add moves the route at
        # once, new node first, instead of when the destination reports
        # the port up 1-2 s later, by which time the old node may have
        # dropped the network. The ACTIVE add later repeats it.
        return _only((ADD, endpoint(port, migrating=True))), new_host
    if is_active and not was_active:
        return _only((ADD, endpoint(port))), None
    if was_active and not is_active:
        if is_migrating(original, port):
            # DOWN while the VM still runs on its source: the add when it
            # is ACTIVE on the destination replaces the route in one step.
            return [], None
        return _only(withdraw), None
    if not is_active:
        return [], None
    if moved:
        return _only(withdraw, (ADD, endpoint(port))), None
    # Same node: only the addresses that changed, compared normalised.
    old, new = prefixes_of(original), prefixes_of(port)
    gone, added = withdraw[1], endpoint(port)
    return _only(
        (REMOVE, gone and gone._replace(
            prefixes=tuple(p for p in old if p not in new))),
        (ADD, added and added._replace(
            prefixes=tuple(p for p in new if p not in old)))), None


def _only(*deltas):
    """Drop the deltas with no endpoint or no address."""
    return [(op, ep) for op, ep in deltas if ep and ep.prefixes]
