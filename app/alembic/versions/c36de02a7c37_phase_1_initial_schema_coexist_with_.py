"""phase 1 initial schema (coexist with existing db)

Revision ID: c36de02a7c37
Revises: 
Create Date: 2026-08-14 14:25:34.647258

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy import Text
from sqlalchemy.dialects import postgresql

revision: str = 'c36de02a7c37'
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # pgvector extension is required for the mcp_chunks.embedding column.
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table('active_symptom_states',
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('symptom', sa.String(length=128), nullable=False),
    sa.Column('risk_level', sa.String(length=16), nullable=False),
    sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('user_id', 'symptom', name='uq_active_symptom')
    )
    op.create_index(op.f('ix_active_symptom_states_user_id'), 'active_symptom_states', ['user_id'], unique=False)
    op.create_table('consent_ledger',
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('purpose', sa.String(length=64), nullable=False),
    sa.Column('action', sa.String(length=16), nullable=False),
    sa.Column('scope', sa.JSON().with_variant(postgresql.JSONB(astext_type=Text()), 'postgresql'), nullable=True),
    sa.Column('source', sa.String(length=64), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_consent_ledger_user_id'), 'consent_ledger', ['user_id'], unique=False)
    op.create_table('conversation_sessions',
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_conversation_sessions_user_id'), 'conversation_sessions', ['user_id'], unique=False)
    op.create_table('insight_artifacts',
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('condition_code', sa.String(length=32), nullable=False),
    sa.Column('tier', sa.String(length=24), nullable=False),
    sa.Column('title', sa.String(length=200), nullable=False),
    sa.Column('body', sa.Text(), nullable=False),
    sa.Column('facts_used', sa.JSON().with_variant(postgresql.JSONB(astext_type=Text()), 'postgresql'), nullable=True),
    sa.Column('fired_rules', sa.JSON().with_variant(postgresql.JSONB(astext_type=Text()), 'postgresql'), nullable=True),
    sa.Column('template_key', sa.String(length=48), nullable=False),
    sa.Column('template_version', sa.Integer(), nullable=False),
    sa.Column('pipeline_version', sa.Integer(), nullable=False),
    sa.Column('content_hash', sa.String(length=64), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('superseded_by', sa.Uuid(), nullable=True),
    sa.Column('recompute_reason', sa.String(length=64), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['superseded_by'], ['insight_artifacts.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_insight_artifacts_content_hash'), 'insight_artifacts', ['content_hash'], unique=False)
    op.create_index(op.f('ix_insight_artifacts_user_id'), 'insight_artifacts', ['user_id'], unique=False)
    op.create_table('insight_templates',
    sa.Column('template_key', sa.String(length=48), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('locale', sa.String(length=16), nullable=False),
    sa.Column('title', sa.String(length=200), nullable=False),
    sa.Column('body', sa.Text(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('template_key', 'version', name='uq_insight_template_key_version')
    )
    op.create_table('job_runs',
    sa.Column('name', sa.String(length=64), nullable=False),
    sa.Column('trigger', sa.String(length=32), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('input_hash', sa.String(length=64), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_job_runs_name'), 'job_runs', ['name'], unique=False)
    op.create_table('mcp_chunks',
    sa.Column('condition_code', sa.String(length=32), nullable=False),
    sa.Column('chunk_type', sa.String(length=48), nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('embedding', Vector(1024), nullable=True),
    sa.Column('metadata', sa.JSON().with_variant(postgresql.JSONB(astext_type=Text()), 'postgresql'), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_mcp_chunks_condition_code'), 'mcp_chunks', ['condition_code'], unique=False)
    op.create_table('pedigree_members',
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('slot', sa.String(length=32), nullable=False),
    sa.Column('vital_status', sa.String(length=16), nullable=True),
    sa.Column('cause_of_death', sa.String(length=128), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('user_id', 'slot', name='uq_pedigree_member_slot')
    )
    op.create_index(op.f('ix_pedigree_members_user_id'), 'pedigree_members', ['user_id'], unique=False)
    op.create_table('rag_turn_receipts',
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('session_id', sa.Uuid(), nullable=True),
    sa.Column('query_hash', sa.String(length=64), nullable=False),
    sa.Column('model_name', sa.String(length=64), nullable=False),
    sa.Column('prompt_version', sa.String(length=32), nullable=False),
    sa.Column('retrieved', sa.JSON().with_variant(postgresql.JSONB(astext_type=Text()), 'postgresql'), nullable=True),
    sa.Column('grounding', sa.JSON().with_variant(postgresql.JSONB(astext_type=Text()), 'postgresql'), nullable=True),
    sa.Column('grounding_mode', sa.String(length=16), nullable=False),
    sa.Column('grounding_status', sa.String(length=24), nullable=False),
    sa.Column('used_rag', sa.Boolean(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_rag_turn_receipts_user_id'), 'rag_turn_receipts', ['user_id'], unique=False)
    op.create_table('risk_rules',
    sa.Column('rule_key', sa.String(length=32), nullable=False),
    sa.Column('pattern_key', sa.String(length=48), nullable=False),
    sa.Column('params', sa.JSON().with_variant(postgresql.JSONB(astext_type=Text()), 'postgresql'), nullable=True),
    sa.Column('condition_code', sa.String(length=32), nullable=False),
    sa.Column('tier', sa.String(length=24), nullable=False),
    sa.Column('modifier', sa.Integer(), nullable=False),
    sa.Column('template_key', sa.String(length=48), nullable=True),
    sa.Column('sensitive', sa.Boolean(), nullable=False),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('rationale', sa.Text(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_risk_rules_rule_key'), 'risk_rules', ['rule_key'], unique=False)
    op.create_table('symptom_logs',
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('symptom', sa.String(length=128), nullable=False),
    sa.Column('risk_level', sa.String(length=16), nullable=False),
    sa.Column('matched_terms', sa.JSON().with_variant(postgresql.JSONB(astext_type=Text()), 'postgresql'), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_symptom_logs_user_id'), 'symptom_logs', ['user_id'], unique=False)
    op.create_table('conversation_messages',
    sa.Column('session_id', sa.Uuid(), nullable=False),
    sa.Column('role', sa.String(length=16), nullable=False),
    sa.Column('message', sa.Text(), nullable=False),
    sa.Column('extracted_intent', sa.JSON().with_variant(postgresql.JSONB(astext_type=Text()), 'postgresql'), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['session_id'], ['conversation_sessions.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_conversation_messages_session_id'), 'conversation_messages', ['session_id'], unique=False)
    op.create_table('conversation_summaries',
    sa.Column('session_id', sa.Uuid(), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('summary', sa.JSON().with_variant(postgresql.JSONB(astext_type=Text()), 'postgresql'), nullable=False),
    sa.Column('covers_through_message_id', sa.Uuid(), nullable=True),
    sa.Column('token_estimate', sa.Integer(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['session_id'], ['conversation_sessions.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_conversation_summaries_session_id'), 'conversation_summaries', ['session_id'], unique=False)
    op.create_table('pedigree_conditions',
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('slot', sa.String(length=32), nullable=False),
    sa.Column('condition_code', sa.String(length=32), nullable=False),
    sa.Column('condition_display', sa.String(length=128), nullable=False),
    sa.Column('onset_band', sa.String(length=16), nullable=False),
    sa.Column('certainty', sa.String(length=24), nullable=False),
    sa.Column('provenance', sa.String(length=24), nullable=False),
    sa.Column('consent_grant_id', sa.Uuid(), nullable=True),
    sa.Column('soft_deleted', sa.Boolean(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['consent_grant_id'], ['consent_ledger.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_pedigree_conditions_user_id'), 'pedigree_conditions', ['user_id'], unique=False)
    # ### end Alembic commands ###


def downgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_index(op.f('ix_pedigree_conditions_user_id'), table_name='pedigree_conditions')
    op.drop_table('pedigree_conditions')
    op.drop_index(op.f('ix_conversation_summaries_session_id'), table_name='conversation_summaries')
    op.drop_table('conversation_summaries')
    op.drop_index(op.f('ix_conversation_messages_session_id'), table_name='conversation_messages')
    op.drop_table('conversation_messages')
    op.drop_index(op.f('ix_symptom_logs_user_id'), table_name='symptom_logs')
    op.drop_table('symptom_logs')
    op.drop_index(op.f('ix_risk_rules_rule_key'), table_name='risk_rules')
    op.drop_table('risk_rules')
    op.drop_index(op.f('ix_rag_turn_receipts_user_id'), table_name='rag_turn_receipts')
    op.drop_table('rag_turn_receipts')
    op.drop_index(op.f('ix_pedigree_members_user_id'), table_name='pedigree_members')
    op.drop_table('pedigree_members')
    op.drop_index(op.f('ix_mcp_chunks_condition_code'), table_name='mcp_chunks')
    op.drop_table('mcp_chunks')
    op.drop_index(op.f('ix_job_runs_name'), table_name='job_runs')
    op.drop_table('job_runs')
    op.drop_table('insight_templates')
    op.drop_index(op.f('ix_insight_artifacts_user_id'), table_name='insight_artifacts')
    op.drop_index(op.f('ix_insight_artifacts_content_hash'), table_name='insight_artifacts')
    op.drop_table('insight_artifacts')
    op.drop_index(op.f('ix_conversation_sessions_user_id'), table_name='conversation_sessions')
    op.drop_table('conversation_sessions')
    op.drop_index(op.f('ix_consent_ledger_user_id'), table_name='consent_ledger')
    op.drop_table('consent_ledger')
    op.drop_index(op.f('ix_active_symptom_states_user_id'), table_name='active_symptom_states')
    op.drop_table('active_symptom_states')
    # ### end Alembic commands ###
