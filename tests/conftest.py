"""Shared pytest fixtures."""

import pytest

from models.schemas import (
    AnalysisResult,
    ComplianceFlag,
    ComplianceFramework,
    ContractClause,
    DocumentChunk,
    Entity,
    EntityType,
    FinalReport,
    RiskLevel,
)


@pytest.fixture
def sample_chunk() -> DocumentChunk:
    return DocumentChunk(
        chunk_id="chunk-001",
        index=0,
        text="The Vendor shall deliver services by January 15, 2025. "
             "Payment terms: Net 30. Limitation of liability capped at $1,000,000.",
        start_line=1,
        end_line=3,
    )


@pytest.fixture
def sample_analysis_result(sample_chunk: DocumentChunk) -> AnalysisResult:
    return AnalysisResult(
        chunk_id=sample_chunk.chunk_id,
        job_id="job-001",
        entities=[
            Entity(text="Vendor", type=EntityType.ORGANIZATION, start=4, end=10, confidence=0.95),
            Entity(text="January 15, 2025", type=EntityType.DATE, start=40, end=56, confidence=0.98),
            Entity(text="$1,000,000", type=EntityType.MONEY, start=120, end=130, confidence=0.97),
        ],
        clauses=[
            ContractClause(
                title="Delivery Deadline",
                text="The Vendor shall deliver services by January 15, 2025.",
                risk_level=RiskLevel.MEDIUM,
                recommendations=["Verify feasibility of deadline."],
            ),
            ContractClause(
                title="Liability Cap",
                text="Limitation of liability capped at $1,000,000.",
                risk_level=RiskLevel.HIGH,
                recommendations=["Consider increasing cap for high-value engagement."],
            ),
        ],
        flags=[
            ComplianceFlag(
                framework=ComplianceFramework.SOC2,
                control_id="CC7.2",
                description="Vendor liability cap may not cover breach costs.",
                severity=RiskLevel.HIGH,
                remediation="Negotiate higher liability limit or obtain cyber insurance.",
            ),
        ],
        summary="Vendor contract contains medium-risk delivery terms and a high-risk liability cap.",
        processing_time_ms=1200,
    )


@pytest.fixture
def sample_final_report(sample_analysis_result: AnalysisResult) -> FinalReport:
    return FinalReport(
        job_id="job-001",
        document_key="contracts/test.txt",
        framework=ComplianceFramework.SOC2,
        overall_risk=RiskLevel.HIGH,
        executive_summary="Test summary.",
        key_clauses=sample_analysis_result.clauses,
        compliance_flags=sample_analysis_result.flags,
        entity_registry={EntityType.ORGANIZATION: ["Vendor"]},
        generated_at=__import__("datetime").date(2025, 1, 1),
        raw_results_count=1,
    )
