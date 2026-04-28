"""Contract Analysis Agent powered by Strands Agents SDK.

This agent extracts entities, clauses, and risk indicators from
a document chunk using Amazon Bedrock (Nova Pro / Claude 3) via
the Strands Agents framework.
"""

from __future__ import annotations

import json
import time
from typing import Any

from strands import Agent
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from models.schemas import AnalysisResult, ContractClause, DocumentChunk, Entity, RiskLevel

SYSTEM_PROMPT = """You are a senior contract analyst and legal engineer.

Your job is to analyze a chunk of a legal or commercial document and extract:
1. Named entities (people, organizations, dates, monetary amounts, durations, legal terms)
2. Contract clauses with risk ratings
3. A 1-2 sentence summary of the chunk

RULES:
- Use exact text from the document; do not paraphrase clauses.
- Rate risk as: low, medium, high, critical.
- Provide specific, actionable recommendations for high/critical clauses.
- Output ONLY valid JSON conforming to the requested schema.
- Do not wrap JSON in markdown fences.
"""


@retry(
    retry=retry_if_exception_type(Exception),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
)
def _invoke_with_backoff(agent: Agent, prompt: str) -> str:
    """Invoke the Strands agent with exponential backoff for throttling."""
    # In production, Agent.invoke() handles Bedrock Converse API.
    # Here we wrap it for resiliency.
    return agent.invoke(prompt)


def _parse_json_safely(text: str) -> dict[str, Any]:
    """Strip markdown fences and parse JSON from agent output."""
    text = text.strip()
    if text.startswith("```json"):
        text = text.removeprefix("```json").removesuffix("```").strip()
    elif text.startswith("```"):
        text = text.removeprefix("```").removesuffix("```").strip()
    return json.loads(text)


def create_contract_analyzer(model_id: str | None = None) -> Agent:
    """Factory for the contract analysis agent.

    Args:
        model_id: Bedrock model ID. Defaults to Amazon Nova Pro.

    Returns:
        Configured Strands Agent ready for contract analysis.
    """
    return Agent(
        system_prompt=SYSTEM_PROMPT,
        # model_id=model_id or "us.amazon.nova-pro-v1:0",
    )


def run_contract_analysis(
    agent: Agent,
    chunk: DocumentChunk,
    job_id: str,
) -> AnalysisResult:
    """Execute contract analysis on a single document chunk.

    Args:
        agent: Pre-configured Strands Agent.
        chunk: Document slice to analyze.
        job_id: Pipeline correlation ID.

    Returns:
        Structured analysis result.
    """
    start = time.perf_counter_ns()

    prompt = f"""Analyze the following document chunk and return JSON.

CHUNK (lines {chunk.start_line}-{chunk.end_line}):
{chunk.text}

Return JSON with this structure:
{{
  "entities": [
    {{"text": "...", "type": "person|organization|date|money|duration|legal_term", "start": 0, "end": 5, "confidence": 0.95}}
  ],
  "clauses": [
    {{"title": "...", "text": "...", "risk_level": "low|medium|high|critical", "recommendations": ["..."]}}
  ],
  "summary": "..."
}}
"""

    raw = _invoke_with_backoff(agent, prompt)
    data = _parse_json_safely(raw)

    entities = [Entity(**e) for e in data.get("entities", [])]
    clauses = []
    for c in data.get("clauses", []):
        clause_entities = [Entity(**e) for e in c.pop("entities", [])]
        clauses.append(ContractClause(entities=clause_entities, **c))

    elapsed_ms = (time.perf_counter_ns() - start) // 1_000_000

    return AnalysisResult(
        chunk_id=chunk.chunk_id,
        job_id=job_id,
        entities=entities,
        clauses=clauses,
        summary=data.get("summary", ""),
        processing_time_ms=elapsed_ms,
    )
