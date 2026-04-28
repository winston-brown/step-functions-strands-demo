"""Lambda: Report Writer

Persists the FinalReport to S3 as JSON and optionally writes a human-readable
Markdown summary. Also writes pipeline metadata to DynamoDB for querying.

Input:
    FinalReport as dict + s3_output_bucket

Output:
    {
        "status": "SUCCEEDED",
        "report_s3_uri": "s3://bucket/reports/uuid.json",
        "markdown_s3_uri": "s3://bucket/reports/uuid.md",
        "job_id": "uuid"
    }
"""

from __future__ import annotations

import json
import os
from typing import Any

import boto3
import structlog

from models.schemas import FinalReport

logger = structlog.get_logger()


def _to_markdown(report: FinalReport) -> str:
    """Render a FinalReport as a professional Markdown document."""
    lines: list[str] = [
        f"# Document Analysis Report: {report.document_key}",
        "",
        f"**Job ID:** `{report.job_id}`  ",
        f"**Framework:** {report.framework.value.upper()}  ",
        f"**Overall Risk:** {report.overall_risk.value.upper()}  ",
        f"**Generated:** {report.generated_at.isoformat()}  ",
        "",
        "## Executive Summary",
        "",
        report.executive_summary,
        "",
        "## Key Clauses",
        "",
    ]

    for clause in report.key_clauses[:20]:  # Limit output length
        lines.append(f"### {clause.title}")
        lines.append(f"- **Risk Level:** {clause.risk_level.value.upper()}")
        lines.append(f"- **Text:** {clause.text[:500]}")
        if clause.recommendations:
            lines.append("- **Recommendations:**")
            for rec in clause.recommendations:
                lines.append(f"  - {rec}")
        lines.append("")

    lines.extend([
        "## Compliance Flags",
        "",
    ])

    for flag in report.compliance_flags[:20]:
        lines.append(f"### {flag.control_id} — {flag.severity.value.upper()}")
        lines.append(f"- {flag.description}")
        lines.append(f"- **Remediation:** {flag.remediation}")
        lines.append("")

    lines.extend([
        "## Entity Registry",
        "",
    ])

    for entity_type, texts in report.entity_registry.items():
        lines.append(f"### {entity_type.value}")
        for text in texts[:10]:
            lines.append(f"- {text}")
        lines.append("")

    return "\n".join(lines)


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """AWS Lambda entry point."""
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(
        aws_request_id=getattr(context, "aws_request_id", "local"),
        job_id=event.get("job_id"),
    )

    logger.info("reporter_start")

    try:
        report = FinalReport(**event)
        bucket = os.environ["OUTPUT_BUCKET"]
        table_name = os.environ.get("METADATA_TABLE", "")
        job_id = report.job_id

        s3 = boto3.client("s3")

        # Persist structured JSON
        json_key = f"reports/{job_id}.json"
        s3.put_object(
            Bucket=bucket,
            Key=json_key,
            Body=json.dumps(report.model_dump(mode="json"), indent=2),
            ContentType="application/json",
        )

        # Persist Markdown
        md_key = f"reports/{job_id}.md"
        s3.put_object(
            Bucket=bucket,
            Key=md_key,
            Body=_to_markdown(report),
            ContentType="text/markdown",
        )

        # Write metadata to DynamoDB if configured
        if table_name:
            dynamodb = boto3.resource("dynamodb")
            table = dynamodb.Table(table_name)
            table.put_item(Item={
                "job_id": job_id,
                "document_key": report.document_key,
                "framework": report.framework.value,
                "overall_risk": report.overall_risk.value,
                "generated_at": report.generated_at.isoformat(),
                "json_s3_uri": f"s3://{bucket}/{json_key}",
                "markdown_s3_uri": f"s3://{bucket}/{md_key}",
            })

        logger.info("reporter_complete", json_key=json_key, md_key=md_key)

        return {
            "status": "SUCCEEDED",
            "report_s3_uri": f"s3://{bucket}/{json_key}",
            "markdown_s3_uri": f"s3://{bucket}/{md_key}",
            "job_id": job_id,
        }

    except Exception as exc:
        logger.error("reporter_error", error=str(exc), exc_info=True)
        raise
