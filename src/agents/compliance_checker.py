"""Compliance Checking Agent powered by Strands Agents SDK.

Evaluates extracted clauses against a compliance framework (SOC2, HIPAA,
GDPR, FISMA) and generates remediation guidance.
"""

from __future__ import annotations

import json
import time
from typing import Any

from strands import Agent
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from models.schemas import (
    AnalysisResult,
    ComplianceFlag,
    ComplianceFramework,
    DocumentChunk,
    RiskLevel,
)


def _framework_prompt(framework: ComplianceFramework) -> str:
    """Generate a compliance-specific system prompt."""
    base = (
        "You are a compliance auditor specializing in {framework} controls. "
        "Evaluate the provided document chunk against {framework} requirements. "
        "Identify control gaps, insufficient language, or missing provisions. "
        "Output ONLY valid JSON."
    )
    framework_details = {
        ComplianceFramework.SOC2: (
            "Focus on Trust Services Criteria: Security (CC6.1, CC7.2), "
            "Availability (A1.2), Processing Integrity (PI1.3), Confidentiality (C1.1), "
            "and Privacy (P1.1)."
        ),
        ComplianceFramework.HIPAA: (
            "Focus on Administrative Safeguards (§164.308), Physical Safeguards (§164.310), "
            "Technical Safeguards (§164.312), and Breach Notification (§164.404)."
        ),
        ComplianceFramework.GDPR: (
            "Focus on Article 32 (Security of processing), Article 33 (Breach notification), "
            "Article 35 (DPIA), and Article 28 (Processor obligations)."
        ),
        ComplianceFramework.FISMA: (
            "Focus on NIST SP 800-53 controls: AC-2 (Account Management), "
            "AU-6 (Audit Review), SC-28 (Protection at Rest), and RA-5 (Vulnerability Scanning)."
        ),
    }
    return base.format(framework=framework.value) + " " + framework_details.get(
        framework, ""
    )


@retry(
    retry=retry_if_exception_type(Exception),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
)
def _invoke_with_backoff(agent: Agent, prompt: str) -> str:
    return agent.invoke(prompt)


def _parse_json_safely(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```json"):
        text = text.removeprefix("```json").removesuffix("```").strip()
    elif text.startswith("```"):
        text = text.removeprefix("```").removesuffix("```").strip()
    return json.loads(text)


def create_compliance_checker(
    framework: ComplianceFramework,
    model_id: str | None = None,
) -> Agent:
    """Factory for a compliance-checking agent tuned to a specific framework.

    Args:
        framework: The compliance standard to enforce.
        model_id: Optional Bedrock model override.

    Returns:
        Configured Strands Agent.
    """
    return Agent(
        system_prompt=_framework_prompt(framework),
        # model_id=model_id or "us.amazon.nova-pro-v1:0",
    )


def run_compliance_check(
    agent: Agent,
    chunk: DocumentChunk,
    framework: ComplianceFramework,
    job_id: str,
) -> AnalysisResult:
    """Evaluate a document chunk for compliance gaps.

    Args:
        agent: Pre-configured compliance agent.
        chunk: Document slice.
        framework: Compliance standard being evaluated.
        job_id: Pipeline correlation ID.

    Returns:
        Analysis result containing only compliance flags and summary.
    """
    start = time.perf_counter_ns()

    prompt = f"""Evaluate the following document chunk for {framework.value.upper()} compliance.

CHUNK (lines {chunk.start_line}-{chunk.end_line}):
{chunk.text}

Return JSON with this structure:
{{
  "flags": [
    {{
      "control_id": "e.g., CC6.1",
      "description": "...",
      "severity": "low|medium|high|critical",
      "remediation": "..."
    }}
  ],
  "summary": "..."
}}
"""

    raw = _invoke_with_backoff(agent, prompt)
    data = _parse_json_safely(raw)

    flags = [ComplianceFlag(framework=framework, **f) for f in data.get("flags", [])]
    elapsed_ms = (time.perf_counter_ns() - start) // 1_000_000

    return AnalysisResult(
        chunk_id=chunk.chunk_id,
        job_id=job_id,
        flags=flags,
        summary=data.get("summary", ""),
        processing_time_ms=elapsed_ms,
    )
