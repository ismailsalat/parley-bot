"""Initial Parley schema.

Migrations are frozen snapshots: they use plain SQLAlchemy types rather than
importing the application's models, so later model changes never break them.

Revision ID: 0001
Revises:
Create Date: 2026-09-22 04:49:56.779368
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0001'
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('audit_logs',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('action', sa.String(length=64), nullable=False),
    sa.Column('actor_id', sa.BigInteger(), nullable=True),
    sa.Column('guild_id', sa.BigInteger(), nullable=True),
    sa.Column('details', sa.JSON(), nullable=False),
    sa.Column('timestamp', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_audit_logs'))
    )
    with op.batch_alter_table('audit_logs', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_audit_logs_action'), ['action'], unique=False)
        batch_op.create_index(batch_op.f('ix_audit_logs_guild_id'), ['guild_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_audit_logs_timestamp'), ['timestamp'], unique=False)

    op.create_table('cooldowns',
    sa.Column('scope', sa.String(length=40), nullable=False),
    sa.Column('subject', sa.String(length=100), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('scope', 'subject', name=op.f('pk_cooldowns'))
    )
    with op.batch_alter_table('cooldowns', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_cooldowns_expires_at'), ['expires_at'], unique=False)

    op.create_table('guilds',
    sa.Column('guild_id', sa.BigInteger(), autoincrement=False, nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('icon_url', sa.String(length=500), nullable=True),
    sa.Column('member_count', sa.Integer(), nullable=False),
    sa.Column('connected_by', sa.BigInteger(), nullable=True),
    sa.Column('connected_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.PrimaryKeyConstraint('guild_id', name=op.f('pk_guilds'))
    )
    op.create_table('moderation_bans',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('target_type', sa.String(length=10), nullable=False),
    sa.Column('target_id', sa.BigInteger(), nullable=False),
    sa.Column('reason', sa.Text(), nullable=True),
    sa.Column('moderator_id', sa.BigInteger(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("target_type IN ('guild', 'user')", name=op.f('ck_moderation_bans_target_type')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_moderation_bans')),
    sa.UniqueConstraint('target_type', 'target_id', name='uq_moderation_bans_target')
    )
    op.create_table('network_posts',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('source_guild_id', sa.BigInteger(), nullable=False),
    sa.Column('destination_guild_id', sa.BigInteger(), nullable=False),
    sa.Column('channel_id', sa.BigInteger(), nullable=False),
    sa.Column('message_id', sa.BigInteger(), nullable=True),
    sa.Column('posted_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_network_posts'))
    )
    with op.batch_alter_table('network_posts', schema=None) as batch_op:
        batch_op.create_index('ix_network_posts_destination_posted', ['destination_guild_id', 'posted_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_network_posts_source_guild_id'), ['source_guild_id'], unique=False)

    op.create_table('network_settings',
    sa.Column('guild_id', sa.BigInteger(), autoincrement=False, nullable=False),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('channel_id', sa.BigInteger(), nullable=True),
    sa.Column('categories', sa.JSON(), nullable=False),
    sa.Column('interval_minutes', sa.Integer(), nullable=False),
    sa.Column('last_post_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('configured_by', sa.BigInteger(), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('guild_id', name=op.f('pk_network_settings'))
    )
    op.create_table('panel_states',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('guild_id', sa.BigInteger(), nullable=False),
    sa.Column('channel_id', sa.BigInteger(), nullable=False),
    sa.Column('panel_type', sa.String(length=30), nullable=False),
    sa.Column('message_id', sa.BigInteger(), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_panel_states')),
    sa.UniqueConstraint('guild_id', 'panel_type', name='uq_panel_states_guild_type')
    )
    op.create_table('partnership_requests',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('source_guild_id', sa.BigInteger(), nullable=False),
    sa.Column('target_guild_id', sa.BigInteger(), nullable=False),
    sa.Column('requester_user_id', sa.BigInteger(), nullable=False),
    sa.Column('message', sa.Text(), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('responded_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('responded_by', sa.BigInteger(), nullable=True),
    sa.CheckConstraint('source_guild_id <> target_guild_id', name=op.f('ck_partnership_requests_not_self')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_partnership_requests'))
    )
    with op.batch_alter_table('partnership_requests', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_partnership_requests_source_guild_id'), ['source_guild_id'], unique=False)
        batch_op.create_index('ix_partnership_requests_target_status', ['target_guild_id', 'status'], unique=False)
        batch_op.create_index('uq_partnership_requests_pending_pair', ['source_guild_id', 'target_guild_id'], unique=True, postgresql_where=sa.text("status = 'pending'"), sqlite_where=sa.text("status = 'pending'"))

    op.create_table('runtime_settings',
    sa.Column('key', sa.String(length=100), nullable=False),
    sa.Column('value', sa.JSON(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_by', sa.BigInteger(), nullable=True),
    sa.PrimaryKeyConstraint('key', name=op.f('pk_runtime_settings'))
    )
    op.create_table('listing_contacts',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('guild_id', sa.BigInteger(), nullable=False),
    sa.Column('user_id', sa.BigInteger(), nullable=False),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.ForeignKeyConstraint(['guild_id'], ['guilds.guild_id'], name=op.f('fk_listing_contacts_guild_id_guilds'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_listing_contacts')),
    sa.UniqueConstraint('guild_id', 'user_id', name='uq_listing_contacts_guild_user')
    )
    with op.batch_alter_table('listing_contacts', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_listing_contacts_guild_id'), ['guild_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_listing_contacts_user_id'), ['user_id'], unique=False)

    op.create_table('listings',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('guild_id', sa.BigInteger(), nullable=False),
    sa.Column('advertisement_text', sa.Text(), nullable=False),
    sa.Column('category', sa.String(length=300), nullable=False),
    sa.Column('accepting_partnerships', sa.Boolean(), nullable=False),
    sa.Column('minimum_members', sa.Integer(), nullable=False),
    sa.Column('invite_url', sa.String(length=200), nullable=True),
    sa.Column('channel_id', sa.BigInteger(), nullable=True),
    sa.Column('message_id', sa.BigInteger(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('refreshed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.ForeignKeyConstraint(['guild_id'], ['guilds.guild_id'], name=op.f('fk_listings_guild_id_guilds'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_listings'))
    )
    with op.batch_alter_table('listings', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_listings_guild_id'), ['guild_id'], unique=True)
        batch_op.create_index(batch_op.f('ix_listings_status'), ['status'], unique=False)



def downgrade() -> None:
    with op.batch_alter_table('listings', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_listings_status'))
        batch_op.drop_index(batch_op.f('ix_listings_guild_id'))

    op.drop_table('listings')
    with op.batch_alter_table('listing_contacts', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_listing_contacts_user_id'))
        batch_op.drop_index(batch_op.f('ix_listing_contacts_guild_id'))

    op.drop_table('listing_contacts')
    op.drop_table('runtime_settings')
    with op.batch_alter_table('partnership_requests', schema=None) as batch_op:
        batch_op.drop_index('uq_partnership_requests_pending_pair', postgresql_where=sa.text("status = 'pending'"), sqlite_where=sa.text("status = 'pending'"))
        batch_op.drop_index('ix_partnership_requests_target_status')
        batch_op.drop_index(batch_op.f('ix_partnership_requests_source_guild_id'))

    op.drop_table('partnership_requests')
    op.drop_table('panel_states')
    op.drop_table('network_settings')
    with op.batch_alter_table('network_posts', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_network_posts_source_guild_id'))
        batch_op.drop_index('ix_network_posts_destination_posted')

    op.drop_table('network_posts')
    op.drop_table('moderation_bans')
    op.drop_table('guilds')
    with op.batch_alter_table('cooldowns', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_cooldowns_expires_at'))

    op.drop_table('cooldowns')
    with op.batch_alter_table('audit_logs', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_audit_logs_timestamp'))
        batch_op.drop_index(batch_op.f('ix_audit_logs_guild_id'))
        batch_op.drop_index(batch_op.f('ix_audit_logs_action'))

    op.drop_table('audit_logs')
