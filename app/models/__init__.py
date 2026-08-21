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

# Core-app partial mappings (external tables; excluded from our migrations).
from app.models.coredata import (
    Bill,
    BodyMeasurement,
    Doctor,
    DoctorConnect,
    DoctorSpecialization,
    FamilyConnect,
    Insurance,
    LifestyleLog,
    ManualTracking,
    Prescription,
    Relation,
    Report,
    ScanImaging,
    TraditionalHealthParameter,
    UnclassifiedFile,
    Vaccination,
    VitalReading,
)
from app.models.jobs import JobRun
from app.models.knowledge import ConditionRegistry, DrugReference
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
    "ConditionRegistry",
    "DrugReference",
    "Report",
    "UnclassifiedFile",
    "Insurance",
    "Bill",
    "Doctor",
    "DoctorConnect",
    "DoctorSpecialization",
    "ScanImaging",
    "Prescription",
    "Vaccination",
    "VitalReading",
    "BodyMeasurement",
    "LifestyleLog",
    "ManualTracking",
    "FamilyConnect",
    "Relation",
    "TraditionalHealthParameter",
]
