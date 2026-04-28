"""Lambda: Agent Executor

Runs inside a Step Functions Map state. Each invocation receives a single
ChunkPayload and orchestrates two Strands Agents in parallel (via local
asyncio) to analyze the chunk and check compliance.

Input:
    ChunkPayload as dict

Output:
    AnalysisResult as dict
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import structlog

from agents.contract_analyzer import create_contract_analyzer, run_contract_analysis
from agents.compliance_checker import create_compliance_checker, run_compliance_check
from models.schemas import AnalysisResult, ChunkPayload, ComplianceFramework

logger = structlog.get_logger()


async def _analyze_chunk(payload: ChunkPayload) -> AnalysisResult:
    """Run contract analysis and compliance check concurrently."""
    contract_agent = create_contract_analyzer(
        model_id=os.environ.get("BEDROCK_MODEL_ID")
    )
    compliance_agent = create_compliance_checker(
        framework=payload.framework,
        model_id=os.environ.get("BEDROCK_MODEL_ID"),
    )

    contract_task = asyncio.to_thread(
        run_contract_analysis,
        contract_agent,
        payload.chunk,
        payload.job_id,
    )
    compliance_task = asyncio.to_thread(
        run_compliance_check,
        compliance_agent,
        payload.chunk,
        payload.framework,
        payload.job_id,
    )

    contract_result, compliance_result = await asyncio.gather(
        contract_task, compliance_task
    )

    # Merge: take entities/clauses/summary from contract, flags from compliance
    return AnalysisResult(
        chunk_id=payload.chunk.chunk_id,
        job_id=payload.job_id,
        entities=contract_result.entities,
        clauses=contract_result.clauses,
        flags=compliance_result.flags,
        summary=f"{contract_result.summary} | Compliance: {compliance_result.summary}",
        processing_time_ms=contract_result.processing_time_ms
        + compliance_result.processing_time_ms,
        agent_version="1.0.0",
    )


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """AWS Lambda entry point."""
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(
        aws_request_id=context.aws_request_id if context else "local",
        chunk_id=event.get("chunk", {}).get("chunk_id"),
    )

    logger.info("agent_executor_start")

    try:
        payload = ChunkPayload(**event)
        result = asyncio.run(_analyze_chunk(payload))

        logger.info(
            "agent_executor_complete",
            entity_count=len(result.entities),
            clause_count=len(result.clauses),
            flag_count=len(result.flags),
            processing_time_ms=result.processing_time_ms,
        )
        return result.model_dump(mode="json")
    except Exception as exc:
        logger.error("agent_executor_error", error=str(exc), exc_info=True)
        raise
