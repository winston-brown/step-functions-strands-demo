"""Unit tests for Lambda handlers.

Uses moto to mock AWS services and verifies handler input/output contracts.
"""

from __future__ import annotations

import json
import os
from typing import Any
from unittest.mock import MagicMock

import boto3
import pytest
from moto import mock_aws

from lambdas.aggregator.handler import handler as aggregator_handler
from lambdas.chunker.handler import (
    DEFAULT_MAX_CHUNK_SIZE,
    _create_chunks,
    _split_by_paragraphs,
    handler as chunker_handler,
)
from lambdas.reporter.handler import handler as reporter_handler
from models.schemas import AnalysisResult, DocumentChunk, EntityType, FinalReport


class TestChunker:
    """Tests for the document chunker Lambda."""

    def test_split_by_paragraphs(self) -> None:
        text = "Para one.\n\nPara two.\n\n\nPara three."
        result = _split_by_paragraphs(text)
        assert result == ["Para one.", "Para two.", "Para three."]

    def test_create_chunks_basic(self) -> None:
        paras = ["A" * 1000 for _ in range(20)]
        chunks = _create_chunks(paras, max_size=5000, overlap=100, job_id="job-1")
        assert len(chunks) >= 2
        for i, chunk in enumerate(chunks):
            assert chunk.index == i
            assert chunk.chunk_id
            assert len(chunk.text) <= 5000 + 200  # generous margin

    @mock_aws
    def test_handler_end_to_end(self) -> None:
        conn = boto3.client("s3", region_name="us-east-1")
        conn.create_bucket(Bucket="test-bucket")
        conn.put_object(
            Bucket="test-bucket",
            Key="contracts/test.txt",
            Body="This is a contract.\n\nIt has multiple paragraphs.\n\n" * 50,
        )

        event = {
            "document_key": "contracts/test.txt",
            "bucket": "test-bucket",
            "framework": "soc2",
            "job_id": "job-abc",
            "context": {},
        }
        context = MagicMock()
        context.aws_request_id = "req-123"

        result = chunker_handler(event, context)

        assert result["job_id"] == "job-abc"
        assert len(result["chunks"]) > 0
        assert result["chunks"][0]["chunk_id"]
        assert result["chunks"][0]["text"]


class TestAggregator:
    """Tests for the result aggregator Lambda."""

    def test_handler_basic(self, sample_analysis_result: AnalysisResult) -> None:
        event = {
            "results": [sample_analysis_result.model_dump(mode="json")],
            "job_id": "job-001",
            "document_key": "contracts/test.txt",
            "framework": "soc2",
        }
        context = MagicMock()
        context.aws_request_id = "req-456"

        result = aggregator_handler(event, context)

        assert result["job_id"] == "job-001"
        assert result["overall_risk"] == "high"
        assert len(result["key_clauses"]) == 2
        assert len(result["compliance_flags"]) == 1
        assert result["entity_registry"]["organization"] == ["Vendor"]
        assert result["raw_results_count"] == 1

    def test_handler_empty_results(self) -> None:
        event = {
            "results": [],
            "job_id": "job-empty",
            "document_key": "empty.txt",
            "framework": "gdpr",
        }
        context = MagicMock()
        context.aws_request_id = "req-789"

        result = aggregator_handler(event, context)
        assert result["overall_risk"] == "low"
        assert result["raw_results_count"] == 0


class TestReporter:
    """Tests for the report writer Lambda."""

    @mock_aws
    def test_handler_end_to_end(self, sample_final_report: FinalReport) -> None:
        os.environ["OUTPUT_BUCKET"] = "output-bucket"
        os.environ["METADATA_TABLE"] = ""

        conn = boto3.client("s3", region_name="us-east-1")
        conn.create_bucket(Bucket="output-bucket")

        event = sample_final_report.model_dump(mode="json")
        context = MagicMock()
        context.aws_request_id = "req-999"

        result = reporter_handler(event, context)

        assert result["status"] == "SUCCEEDED"
        assert result["job_id"] == sample_final_report.job_id
        assert result["report_s3_uri"].startswith("s3://output-bucket/reports/")
        assert result["markdown_s3_uri"].endswith(".md")

        # Verify S3 objects exist
        json_obj = conn.get_object(Bucket="output-bucket", Key=f"reports/{result['job_id']}.json")
        report_data = json.loads(json_obj["Body"].read())
        assert report_data["job_id"] == sample_final_report.job_id

        md_obj = conn.get_object(Bucket="output-bucket", Key=f"reports/{result['job_id']}.md")
        markdown = md_obj["Body"].read().decode("utf-8")
        assert "# Document Analysis Report" in markdown
        assert sample_final_report.executive_summary in markdown
