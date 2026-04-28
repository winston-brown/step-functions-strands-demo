"""Unit tests for Strands Agents logic.

These tests mock the Strands Agent.invoke() method to avoid Bedrock calls
and verify prompt construction, JSON parsing, and result merging.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from agents.compliance_checker import (
    _framework_prompt,
    create_compliance_checker,
    run_compliance_check,
)
from agents.contract_analyzer import create_contract_analyzer, run_contract_analysis
from models.schemas import (
    AnalysisResult,
    ComplianceFramework,
    DocumentChunk,
    RiskLevel,
)


class TestContractAnalyzer:
    """Tests for the contract analysis agent."""

    def test_create_contract_analyzer(self) -> None:
        agent = create_contract_analyzer()
        assert agent is not None
        assert "senior contract analyst" in agent.system_prompt.lower()

    def test_run_contract_analysis(self, sample_chunk: DocumentChunk) -> None:
        mock_agent = MagicMock()
        mock_agent.invoke.return_value = json.dumps({
            "entities": [
                {"text": "Acme Corp", "type": "organization", "start": 0, "end": 9, "confidence": 0.95},
            ],
            "clauses": [
                {
                    "title": "Payment Terms",
                    "text": "Net 30",
                    "risk_level": "low",
                    "recommendations": ["Standard terms."],
                },
            ],
            "summary": "Low-risk payment terms.",
        })

        result = run_contract_analysis(mock_agent, sample_chunk, "job-001")

        assert isinstance(result, AnalysisResult)
        assert result.chunk_id == sample_chunk.chunk_id
        assert result.job_id == "job-001"
        assert len(result.entities) == 1
        assert result.entities[0].text == "Acme Corp"
        assert len(result.clauses) == 1
        assert result.clauses[0].risk_level == RiskLevel.LOW
        assert result.processing_time_ms >= 0

        # Verify the prompt contains the chunk text
        prompt = mock_agent.invoke.call_args[0][0]
        assert sample_chunk.text in prompt
        assert "Analyze the following document chunk" in prompt

    def test_run_contract_analysis_with_markdown_fences(self, sample_chunk: DocumentChunk) -> None:
        mock_agent = MagicMock()
        mock_agent.invoke.return_value = "```json\n{\"entities\": [], \"clauses\": [], \"summary\": \"ok\"}\n```"

        result = run_contract_analysis(mock_agent, sample_chunk, "job-002")
        assert result.summary == "ok"

    def test_run_contract_analysis_invalid_json(self, sample_chunk: DocumentChunk) -> None:
        mock_agent = MagicMock()
        mock_agent.invoke.return_value = "not json"

        with pytest.raises(json.JSONDecodeError):
            run_contract_analysis(mock_agent, sample_chunk, "job-003")


class TestComplianceChecker:
    """Tests for the compliance checking agent."""

    def test_framework_prompt_content(self) -> None:
        prompt = _framework_prompt(ComplianceFramework.HIPAA)
        assert "HIPAA" in prompt
        assert "164.308" in prompt

        prompt_gdpr = _framework_prompt(ComplianceFramework.GDPR)
        assert "GDPR" in prompt_gdpr
        assert "Article 32" in prompt_gdpr

    def test_create_compliance_checker(self) -> None:
        agent = create_compliance_checker(ComplianceFramework.SOC2)
        assert agent is not None
        assert "SOC2" in agent.system_prompt

    def test_run_compliance_check(self, sample_chunk: DocumentChunk) -> None:
        mock_agent = MagicMock()
        mock_agent.invoke.return_value = json.dumps({
            "flags": [
                {
                    "control_id": "CC6.1",
                    "description": "Missing access control clause.",
                    "severity": "high",
                    "remediation": "Add IAM policy reference.",
                },
            ],
            "summary": "High-risk access control gap.",
        })

        result = run_compliance_check(
            mock_agent, sample_chunk, ComplianceFramework.SOC2, "job-004"
        )

        assert isinstance(result, AnalysisResult)
        assert result.chunk_id == sample_chunk.chunk_id
        assert len(result.flags) == 1
        assert result.flags[0].control_id == "CC6.1"
        assert result.flags[0].severity == RiskLevel.HIGH
        assert "access control" in result.summary.lower()

    @patch("agents.compliance_checker._invoke_with_backoff")
    def test_retry_on_throttling(self, mock_invoke: MagicMock, sample_chunk: DocumentChunk) -> None:
        mock_invoke.side_effect = [Exception("ThrottledException"), json.dumps({"flags": [], "summary": "ok"})]
        agent = create_compliance_checker(ComplianceFramework.FISMA)
        result = run_compliance_check(agent, sample_chunk, ComplianceFramework.FISMA, "job-005")
        assert result.summary == "ok"
        assert mock_invoke.call_count == 2
