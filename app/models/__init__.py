"""ORM models package.

Importing this package registers every table on ``Base.metadata`` so Alembic
autogenerate and the test-time ``create_all`` see the full schema.
"""

from __future__ import annotations

from app.db import Base
from app.models.chat import (
    ActiveSymptomState,
    ConversationMessage,
    ConversationSession,
    ConversationSummary,
    McpChunk,
    RagTurnReceipt,
    SymptomLog,
)
from app.models.core import (
    EXTERNAL_TABLES,
    PEDIGREE_SLOTS,
    ConsentLedger,
    PedigreeCondition,
    PedigreeMember,
    User,
)
from app.models.jobs import JobRun
from app.models.rules import InsightArtifact, InsightTemplate, RiskRule

__all__ = [
    "Base",
    "EXTERNAL_TABLES",
    "PEDIGREE_SLOTS",
    "User",
    "ConsentLedger",
    "PedigreeMember",
    "PedigreeCondition",
    "RiskRule",
    "InsightTemplate",
    "InsightArtifact",
    "SymptomLog",
    "ActiveSymptomState",
    "ConversationSession",
    "ConversationMessage",
    "ConversationSummary",
    "McpChunk",
    "RagTurnReceipt",
    "JobRun",
]
