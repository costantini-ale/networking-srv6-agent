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

"""The measured underlay: an add-on of the SRv6 plugin, never relied on.

Link state, VM-pair aggregate loads and the re-optimisation that steers on
them exist to show what SRv6 steering can do. The plugin builds the
Underlay (telemetry.py), forwards the srv6_link_states and
srv6_aggregate_loads APIs to it and asks it for its workers; no plugin
logic reads what it measures. Only the plugin imports this package.

The underlay reaches the plugin only through these public plugin methods:

- silent_fabric(ctx): (fabric nodes gone silent, collection outage).
- host_of_port(ctx, port_id): the host the port is bound to, or None.
- place_auto_path(ctx, pair, node_path, existing=None): create or move the
  auto TE path of one VM pair; None if a concurrent change won.
- release_auto_path(ctx, path): delete one auto path; None if already gone.
- domain_addresses(ctx, domain): {address: (port id, host)} of the
  domain's bound workload ports.
- usid: whether the dataplane encoding is uSID (a property).
"""
