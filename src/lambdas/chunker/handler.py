"""Lambda: Document Chunker

Reads a document from S3, splits it into semantically-meaningful chunks,
and returns a list of ChunkPayload objects for the Step Functions Map state.

Input (from Step Functions):
    {
        "document_key": "contracts/acme-2024.pdf",
        "bucket": "my-doc-bucket",
        "framework": "soc2",
        "job_id": "uuid",
        "context": {}
    }

Output:
    {
        "chunks": [ChunkPayload, ...],
        "job_id": "uuid",
        "document_key": "...",
        "bucket": "...",
        "framework": "soc2"
    }
"""

from __future__ import annotations

import json
import os
import re
import uuid
from typing import Any

import boto3
import structlog

from models.schemas import ChunkPayload, ComplianceFramework, DocumentChunk

logger = structlog.get_logger()

# Tunable via environment variable; 4k tokens ~ 3k words ~ 15k chars
DEFAULT_MAX_CHUNK_SIZE = int(os.environ.get("MAX_CHUNK_SIZE", 15000))
DEFAULT_OVERLAP = int(os.environ.get("CHUNK_OVERLAP", 200))


def _split_by_paragraphs(text: str) -> list[str]:
    """Split text on double newlines, preserving structure."""
    return [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]


def _create_chunks(
    paragraphs: list[str],
    max_size: int,
    overlap: int,
    job_id: str,
) -> list[DocumentChunk]:
    """Greedy paragraph packing into chunks with overlap."""
    chunks: list[DocumentChunk] = []
    current_lines: list[str] = []
    current_size = 0
    start_line = 0
    global_line = 0

    for para in paragraphs:
        para_size = len(para)
        para_lines = para.count("\n") + 1

        if current_size + para_size > max_size and current_lines:
            chunk_text = "\n\n".join(current_lines)
            chunks.append(
                DocumentChunk(
                    chunk_id=str(uuid.uuid4()),
                    index=len(chunks),
                    text=chunk_text,
                    start_line=start_line,
                    end_line=global_line - 1,
                )
            )
            # Overlap: carry last paragraph(s) forward
            overlap_text = chunk_text[-overlap:] if len(chunk_text) > overlap else chunk_text
            current_lines = [overlap_text + "\n\n" + para]
            current_size = len(current_lines[0])
            start_line = global_line
        else:
            current_lines.append(para)
            current_size += para_size + 4  # "\n\n" approximation

        global_line += para_lines

    if current_lines:
        chunk_text = "\n\n".join(current_lines)
        chunks.append(
            DocumentChunk(
                chunk_id=str(uuid.uuid4()),
                index=len(chunks),
                text=chunk_text,
                start_line=start_line,
                end_line=global_line - 1,
            )
        )

    return chunks


def _read_from_s3(bucket: str, key: str) -> str:
    """Fetch object body from S3 as UTF-8 text.

    In production, this may call Textract for PDFs first.
    For this demo, we assume pre-extracted text files.
    """
    s3 = boto3.client("s3")
    response = s3.get_object(Bucket=bucket, Key=key)
    return response["Body"].read().decode("utf-8")


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """AWS Lambda entry point."""
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(
        aws_request_id=context.aws_request_id if context else "local",
        job_id=event.get("job_id"),
    )

    logger.info("chunker_start", document_key=event.get("document_key"))

    try:
        bucket = event["bucket"]
        key = event["document_key"]
        framework = ComplianceFramework(event.get("framework", "soc2"))
        job_id = event["job_id"]

        raw_text = _read_from_s3(bucket, key)
        paragraphs = _split_by_paragraphs(raw_text)
        chunks = _create_chunks(
            paragraphs,
            max_size=DEFAULT_MAX_CHUNK_SIZE,
            overlap=DEFAULT_OVERLAP,
            job_id=job_id,
        )

        payloads = [
            ChunkPayload(chunk=chunk, framework=framework, job_id=job_id).model_dump(
                mode="json"
            )
            for chunk in chunks
        ]

        logger.info(
            "chunker_complete",
            chunk_count=len(payloads),
            total_chars=len(raw_text),
        )

        return {
            "chunks": payloads,
            "job_id": job_id,
            "document_key": key,
            "bucket": bucket,
            "framework": framework.value,
        }

    except Exception as exc:
        logger.error("chunker_error", error=str(exc), exc_info=True)
        raise
