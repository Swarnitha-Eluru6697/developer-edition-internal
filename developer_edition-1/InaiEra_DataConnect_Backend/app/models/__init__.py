"""
ORM model registry.

Import every model here so Alembic's autogenerate and the repository
layer can find them all with a single  `from app.models import *`  or
`from app import models as m; m.Pipeline`.
"""

from app.models.alert import (
    Alert,
    AlertChannel,
    AlertChannelSecret,
    AlertEvent,
    AlertObservation,
    AlertRule,
    AlertState,
)
from app.models.audit import AuditLog
from app.models.catalog import ConnectorTemplate, ConnectorTemplateRevision
from app.models.connection import ConnectionProfile, ConnectionSecret, ConnectionVerification
from app.models.discovery import DiscoveryObject, DiscoveryRun
from app.models.failure_count import FailureCount, FailureCountOffset
from app.models.lineage import Asset, LineageEdge
from app.models.operations import Operation
from app.models.organization import Organization
from app.models.pipeline import Pipeline, PipelineRun
from app.models.pipeline_runtime_state import PipelineRuntimeState
from app.models.quality import (
    QualityExecutionJob,
    QualityExecutionJobRecords,
    QualityPolicy,
    QualityResult,
    QualityStreamOffset,
    QualityStreamStats,
)
from app.models.schema import SchemaDriftApproval, SchemaInferenceRun, SchemaVersion
from app.models.topic import TopicPolicy
from app.models.transform import TransformPolicy

__all__ = [
    "Alert",
    "AlertChannel",
    "AlertChannelSecret",
    "AlertEvent",
    "AlertObservation",
    "AlertRule",
    "AlertState",
    "AuditLog",
    "Asset",
    "ConnectorTemplate",
    "ConnectorTemplateRevision",
    "ConnectionProfile",
    "ConnectionSecret",
    "ConnectionVerification",
    "DiscoveryObject",
    "DiscoveryRun",
    "FailureCount",
    "FailureCountOffset",
    "LineageEdge",
    "Operation",
    "Organization",
    "Pipeline",
    "PipelineRun",
    "PipelineRuntimeState",
    "QualityExecutionJob",
    "QualityExecutionJobRecords",
    "QualityPolicy",
    "QualityResult",
    "QualityStreamOffset",
    "QualityStreamStats",
    "SchemaDriftApproval",
    "SchemaInferenceRun",
    "SchemaVersion",
    "TopicPolicy",
    "TransformPolicy",
]
