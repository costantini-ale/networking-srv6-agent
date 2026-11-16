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

"""The underlay stays an add-on: the dependency runs one way only.

Only the plugin imports the underlay package, and the package reaches the
plugin through its public methods, never a private attribute.
"""

import os
import re

from neutron.tests import base

import networking_srv6_agent
from networking_srv6_agent.services import underlay

ROOT = os.path.dirname(networking_srv6_agent.__file__)
PACKAGE = os.path.dirname(underlay.__file__)
PLUGIN = os.path.join(ROOT, 'services', 'plugin.py')
TESTS = os.path.join(ROOT, 'tests')

IMPORTS_UNDERLAY = re.compile(
    r'^\s*(from\s+networking_srv6_agent\.services(\.underlay\b|'
    r'\s+import\s+\(?\s*underlay\b)|'
    r'import\s+networking_srv6_agent\.services\.underlay\b)', re.M)
PRIVATE_PLUGIN = re.compile(r'plugin\._\w')


def _sources(top):
    for directory, _dirs, files in os.walk(top):
        for name in files:
            if name.endswith('.py'):
                path = os.path.join(directory, name)
                with open(path) as f:
                    yield path, f.read()


class TestUnderlayBoundary(base.BaseTestCase):

    def test_only_the_plugin_imports_the_underlay(self):
        importers = [
            os.path.relpath(path, ROOT) for path, src in _sources(ROOT)
            if path != PLUGIN and not path.startswith(PACKAGE + os.sep) and
            not path.startswith(TESTS + os.sep) and
            IMPORTS_UNDERLAY.search(src)]
        self.assertEqual([], importers)

    def test_the_underlay_uses_no_private_plugin_attribute(self):
        uses = ['%s: %s' % (os.path.relpath(path, ROOT), m.group(0))
                for path, src in _sources(PACKAGE)
                for m in PRIVATE_PLUGIN.finditer(src)]
        self.assertEqual([], uses)

    def test_the_checks_see_a_violation(self):
        self.assertTrue(IMPORTS_UNDERLAY.search(
            'from networking_srv6_agent.services.underlay import reopt'))
        self.assertTrue(IMPORTS_UNDERLAY.search(
            'from networking_srv6_agent.services import underlay'))
        self.assertTrue(PRIVATE_PLUGIN.search('self._plugin._dead_hosts(c)'))
        self.assertFalse(PRIVATE_PLUGIN.search('self._plugin.usid'))
