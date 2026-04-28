"""Strongly-typed Pydantic v2 models for the document analysis pipeline.

All cross-service communication uses these schemas to ensure type safety
and automatic validation at Lambda invocation boundaries.
"""

from datetime import date
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class RiskLevel(str, Enum):
    """Risk classification for contract clauses."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class EntityType(str, Enum):
    """Categories of entities extracted from documents."""

    PERSON = "person"
    ORGANIZATION = "organization"
    DATE = "date"
    MONEY = "money"
    DURATION = "duration"
    LEGAL_TERM = "legal_term"


class ComplianceFramework(str, Enum):
    """Supported compliance frameworks."""

    SOC2 = "soc2"
    HIPAA = "hipaa"
    GDPR = "gdpr"
    FISMA = "fisma"


class PipelineInput(BaseModel):
    """Trigger payload for the Step Functions state machine."""

    document_key: str = Field(..., description="S3 key of the uploaded document")
    bucket: str = Field(..., description="S3 bucket containing the document")
    framework: ComplianceFramework = Field(
        default=ComplianceFramework.SOC2,
        description="Compliance framework to evaluate against",
    )
    context: dict[str, Any] = Field(
        default_factory=dict,
        description="Optional metadata (uploader_id, project_id, etc.)",
    )


class DocumentChunk(BaseModel):
    """A semantically-meaningful slice of the source document."""

    chunk_id: str = Field(..., description="UUID for this chunk")
    index: int = Field(..., ge=0, description="Zero-based sequence number")
    text: str = Field(..., min_length=1, description="Chunk content")
    start_line: int = Field(..., ge=0)
    end_line: int = Field(..., ge=0)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("end_line")
    @classmethod
    def end_after_start(cls, v: int, info: Any) -> int:
        if "start_line" in info.data and v < info.data["start_line"]:
            raise ValueError("end_line must be >= start_line")
        return v


class ChunkPayload(BaseModel):
    """Payload passed to the Map state for parallel processing."""

    chunk: DocumentChunk
    framework: ComplianceFramework
    job_id: str = Field(..., description="Correlation ID for the entire pipeline run")


class Entity(BaseModel):
    """A named entity extracted by the analysis agent."""

    text: str
    type: EntityType
    start: int = Field(..., ge=0, description="Character offset in chunk")
    end: int = Field(..., ge=0, description="Character offset in chunk")
    confidence: float = Field(..., ge=0.0, le=1.0)


class ContractClause(BaseModel):
    """A specific clause identified within a chunk."""

    title: str = Field(..., description="Clause heading or auto-generated title")
    text: str = Field(..., description="Verbatim clause text")
    risk_level: RiskLevel
    entities: list[Entity] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)


class ComplianceFlag(BaseModel):
    """A compliance gap or finding."""

    framework: ComplianceFramework
    control_id: str = Field(..., description="e.g., SOC2 CC6.1")
    description: str
    severity: RiskLevel
    remediation: str


class AnalysisResult(BaseModel):
    """Output from a single parallel agent executor invocation."""

    chunk_id: str
    job_id: str
    entities: list[Entity] = Field(default_factory=list)
    clauses: list[ContractClause] = Field(default_factory=list)
    flags: list[ComplianceFlag] = Field(default_factory=list)
    summary: str = Field(..., description="1-2 sentence executive summary for this chunk")
    processing_time_ms: int = Field(..., ge=0)
    agent_version: str = Field(default="1.0.0")


class FinalReport(BaseModel):
    """Aggregated output delivered to the caller."""

    job_id: str
    document_key: str
    framework: ComplianceFramework
    overall_risk: RiskLevel
    executive_summary: str
    key_clauses: list[ContractClause] = Field(default_factory=list)
    compliance_flags: list[ComplianceFlag] = Field(default_factory=list)
    entity_registry: dict[EntityType, list[str]] = Field(default_factory=dict)
    generated_at: date
    raw_results_count: int = Field(..., ge=0)


class PipelineOutput(BaseModel):
    """Wrapper returned by the Step Functions state machine."""

    status: str = Field(..., pattern="^(SUCCEEDED|FAILED|PARTIAL)$")
    report: FinalReport | None = None
    error_message: str | None = None
    execution_arn: str | None = None
