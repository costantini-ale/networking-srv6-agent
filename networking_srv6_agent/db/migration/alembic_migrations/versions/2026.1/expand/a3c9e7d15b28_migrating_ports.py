# Copyright 2026 networking-srv6-agent contributors.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""expand migrating ports

The server's mark of a live-migration handover, kept until the port's next
status change: the only sign of the migration once nova has deleted the
source binding and before the port is ACTIVE on the destination.

    srv6_migrating_ports    new table; port_id primary key, a foreign key
                            onto ports (ON DELETE CASCADE), created_at

Revision ID: a3c9e7d15b28
Revises: e5a7c1b94d36
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'a3c9e7d15b28'
down_revision = 'e5a7c1b94d36'


def upgrade():
    op.create_table(
        'srv6_migrating_ports',
        sa.Column('port_id', sa.String(36), nullable=False),
        sa.Column('created_at', sa.DateTime, nullable=False),
        sa.PrimaryKeyConstraint('port_id'),
        sa.ForeignKeyConstraint(['port_id'], ['ports.id'],
                                ondelete='CASCADE'),
        mysql_engine='InnoDB',
    )
