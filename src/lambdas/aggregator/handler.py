"""Lambda: Result Aggregator

Collects parallel AnalysisResult outputs from the Map state, deduplicates
entities, ranks clauses by risk, and consolidates compliance flags.

Input:
    {
        "results": [AnalysisResult, ...],
        "job_id": "uuid",
        "document_key": "...",
        "framework": "soc2"
    }

Output:
    {
        "job_id": "uuid",
        "document_key": "...",
        "framework": "soc2",
        "overall_risk": "high",
        "key_clauses": [...],
        "compliance_flags": [...],
        "entity_registry": {...},
        "executive_summary": "...",
        "raw_results_count": 42
    }
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

import structlog

from models.schemas import (
    AnalysisResult,
    ComplianceFlag,
    ComplianceFramework,
    ContractClause,
    EntityType,
    FinalReport,
    RiskLevel,
)

logger = structlog.get_logger()

RISK_ORDER = {
    RiskLevel.CRITICAL: 4,
    RiskLevel.HIGH: 3,
    RiskLevel.MEDIUM: 2,
    RiskLevel.LOW: 1,
}


def _compute_overall_risk(flags: list[ComplianceFlag], clauses: list[ContractClause]) -> RiskLevel:
    """Derive overall risk from the highest severity present."""
    all_levels = [f.severity for f in flags] + [c.risk_level for c in clauses]
    if not all_levels:
        return RiskLevel.LOW
    return max(all_levels, key=lambda r: RISK_ORDER[r])


def _deduplicate_entities(results: list[AnalysisResult]) -> dict[EntityType, list[str]]:
    """Group unique entity texts by type across all chunks."""
    registry: dict[EntityType, set[str]] = defaultdict(set)
    for result in results:
        for entity in result.entities:
            registry[entity.type].add(entity.text)
    return {k: sorted(v) for k, v in registry.items()}


def _merge_clauses(results: list[AnalysisResult]) -> list[ContractClause]:
    """Flatten and sort clauses by risk, keeping all for transparency."""
    clauses: list[ContractClause] = []
    for result in results:
        clauses.extend(result.clauses)
    clauses.sort(key=lambda c: RISK_ORDER[c.risk_level], reverse=True)
    return clauses


def _merge_flags(results: list[AnalysisResult]) -> list[ComplianceFlag]:
    """Flatten and sort compliance flags by severity."""
    flags: list[ComplianceFlag] = []
    for result in results:
        flags.extend(result.flags)
    flags.sort(key=lambda f: RISK_ORDER[f.severity], reverse=True)
    return flags


def _generate_executive_summary(results: list[AnalysisResult]) -> str:
    """Synthesize a 2-3 sentence summary from all chunk summaries."""
    summaries = [r.summary for r in results if r.summary]
    if not summaries:
        return "No analysis data available."
    # Simple heuristic: concatenate first 3 summaries and truncate
    combined = " ".join(summaries[:3])
    if len(combined) > 400:
        combined = combined[:397] + "..."
    return combined


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """AWS Lambda entry point."""
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(
        aws_request_id=getattr(context, "aws_request_id", "local"),
        job_id=event.get("job_id"),
    )

    logger.info("aggregator_start", result_count=len(event.get("results", [])))

    try:
        raw_results = [AnalysisResult(**r) for r in event["results"]]
        job_id = event["job_id"]
        document_key = event["document_key"]
        framework = ComplianceFramework(event["framework"])

        clauses = _merge_clauses(raw_results)
        flags = _merge_flags(raw_results)
        entity_registry = _deduplicate_entities(raw_results)
        overall_risk = _compute_overall_risk(flags, clauses)
        summary = _generate_executive_summary(raw_results)

        report = FinalReport(
            job_id=job_id,
            document_key=document_key,
            framework=framework,
            overall_risk=overall_risk,
            executive_summary=summary,
            key_clauses=clauses,
            compliance_flags=flags,
            entity_registry=entity_registry,
            generated_at=__import__("datetime").date.today(),
            raw_results_count=len(raw_results),
        )

        logger.info(
            "aggregator_complete",
            overall_risk=overall_risk.value,
            clause_count=len(clauses),
            flag_count=len(flags),
        )

        return report.model_dump(mode="json")

    except Exception as exc:
        logger.error("aggregator_error", error=str(exc), exc_info=True)
        raise
