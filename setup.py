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

# Required: the pbr.build backend in pyproject.toml hands off to setuptools,
# which runs this file, and pbr=True is what activates pbr. Without it the
# build still succeeds but the wheel ships no code (pbr's package discovery
# never runs, so only the top-level __init__.py files are packaged).

import setuptools

setuptools.setup(
    setup_requires=['pbr>=2.0.0'],
    pbr=True)
