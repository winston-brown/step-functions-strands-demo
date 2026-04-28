# Orchestrating LLM Agents with AWS Step Functions: A Production-Grade Document Analysis Pipeline

> **TL;DR** — I built a serverless pipeline that uses AWS Step Functions to orchestrate Strands Agents (LLM-powered AI agents) for analyzing legal and compliance documents. The system chunks documents, runs parallel agent analysis, aggregates results, and generates structured reports. All infrastructure is defined as code with AWS CDK, and every cross-service boundary is type-safe via Pydantic v2.

---

## The Problem

Enterprise documents—contracts, policies, compliance attestations—are high-stakes, high-volume, and notoriously tedious to review manually. A single MSA (Master Service Agreement) can be 50+ pages, and verifying it against SOC2, HIPAA, or GDPR controls requires both legal expertise and domain-specific compliance knowledge.

Large Language Models (LLMs) can help, but throwing a 50-page PDF at a single prompt is brittle:

- **Context window limits** force truncation
- **Single-pass analysis** misses nuanced clause interactions
- **Unstructured output** makes downstream processing unreliable
- **No observability** means you can't audit what the model did

What we need is an **orchestrated, multi-agent pipeline** that:

1. Breaks documents into manageable chunks
2. Applies specialized AI agents in parallel
3. Aggregates and deduplicates findings
4. Produces structured, auditable reports
5. Runs on serverless infrastructure with built-in error handling

---

## The Solution: Step Functions + Strands Agents

I built exactly that. The architecture uses **AWS Step Functions** as the orchestration backbone and **Strands Agents** as the LLM execution framework. Here's why this pairing works:

### Why Step Functions?

AWS Step Functions is often pigeonholed as "Lambda choreography," but it's genuinely powerful for AI pipelines:

- **Visual state machines** make complex workflows explicit and auditable—critical for compliance scenarios
- **Map states** provide controlled parallelism (with `max_concurrency` to avoid throttling LLM APIs)
- **Built-in retries and error handling** per task, with dead-letter queues for failed chunks
- **Distributed tracing** via X-Ray and CloudWatch Logs integration
- **Event-driven triggers** via EventBridge when documents land in S3

### Why Strands Agents?

Strands Agents is a Python SDK for building structured, tool-using agents on top of Amazon Bedrock:

- **Pydantic-native output**: Agents return structured data that validates against your schemas
- **Tool composability**: Agents can call external validators, APIs, or other agents
- **Bedrock integration**: Direct use of the Converse API with Nova Pro / Claude 3
- **Resilience**: Built-in retry with exponential backoff for throttling

---

## Architecture Deep Dive

### The Pipeline (4 Stages)

```text
┌────────────────────────────────────────────────────────────────────────────┐
│  S3 Input Bucket                                                    │
└────────────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌────────────────────────────────────────────────────────────────────────────┐
│  AWS Step Functions                                                 │
│                                                                     │
│   ┌────────────┐    ┌─────────────────────────┐    ┌────────────┐    ┌────────────┐ │
│   │  Chunker    │───│   Map (Parallel)   │───│ Aggregator │───│  Reporter  │ │
│   │   Lambda    │    │   Agent Executor   │    │   Lambda   │    │   Lambda  │ │
│   └────────────┘    │      Lambda       │    └────────────┘    └────────────┘ │
│                      │  × N chunks        │                        │
│                      └─────────────────────────┘                        │
└────────────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌────────────────────────────────────────────────────────────────────────────┐
│  S3 Output Bucket + DynamoDB Metadata Table                         └────────────────────────────────────────────────────────────────────────────┘
• reports/{job_id}.json  (structured data)
• reports/{job_id}.md    (human-readable Markdown)
• DynamoDB: queryable metadata index
```

### Stage 1: Chunker

Documents are split into semantically-meaningful chunks (paragraph boundaries) with configurable overlap. This preserves context at chunk boundaries while keeping each unit small enough for reliable LLM processing.

```python
def _create_chunks(paragraphs: list[str], max_size: int, overlap: int) -> list[DocumentChunk]:
    """Greedy paragraph packing into chunks with overlap."""
    # ... packs paragraphs until max_size, then carries overlap forward
```

### Stage 2: Parallel Agent Execution (Map State)

The Step Functions `Map` state fans out each chunk to a Lambda running two Strands Agents **concurrently** via `asyncio.gather`:

#### Agent A: Contract Analyzer

Extracts named entities (people, orgs, dates, monetary amounts) and identifies contract clauses with risk ratings.

```python
agent = Agent(
    system_prompt="You are a senior contract analyst...",
    tools=[],
)
```

The agent is prompted to return strict JSON, which we parse into Pydantic `Entity` and `ContractClause` objects.

#### Agent B: Compliance Checker

Framework-specific evaluation against SOC2, HIPAA, GDPR, or FISMA controls. The system prompt is dynamically generated based on the target framework:

```python
def _framework_prompt(framework: ComplianceFramework) -> str:
    if framework == ComplianceFramework.HIPAA:
        return "Focus on §164.308 (Administrative Safeguards)..."
    if framework == ComplianceFramework.SOC2:
        return "Focus on Trust Services Criteria CC6.1, CC7.2..."
    # ...
```

Both agents run in the same Lambda invocation to minimize cold starts and Step Functions state transitions.

### Stage 3: Aggregator

After all chunks complete, the `Aggregator` Lambda:

- **Deduplicates entities** across chunks (e.g., "Acme Corp" mentioned in 3 chunks is listed once)
- **Ranks clauses and flags** by severity (CRITICAL → HIGH → MEDIUM → LOW)
- **Generates an executive summary** from chunk-level summaries
- **Computes overall risk** as the maximum severity found

```python
def _compute_overall_risk(flags, clauses) -> RiskLevel:
    all_levels = [f.severity for f in flags] + [c.risk_level for c in clauses]
    return max(all_levels, key=lambda r: RISK_ORDER[r])
```

### Stage 4: Reporter

Persists two artifacts to S3:

1. **`{job_id}.json`** — Structured `FinalReport` for downstream systems
2. **`{job_id}.md`** — Human-readable Markdown for legal/compliance teams

Also writes a metadata record to DynamoDB for querying by job ID, framework, or risk level.

---

## Code Walkthrough: The Strands Agent

Here's the core of the contract analysis agent:

```python
from strands import Agent
from tenacity import retry, stop_after_attempt, wait_exponential

SYSTEM_PROMPT = """You are a senior contract analyst...

RULES:
- Use exact text from the document; do not paraphrase clauses.
- Rate risk as: low, medium, high, critical.
- Output ONLY valid JSON conforming to the requested schema.
"""

def create_contract_analyzer() -> Agent:
    return Agent(system_prompt=SYSTEM_PROMPT)

def run_contract_analysis(agent: Agent, chunk: DocumentChunk, job_id: str) -> AnalysisResult:
    prompt = f"""Analyze the following document chunk and return JSON.

CHUNK (lines {chunk.start_line}-{chunk.end_line}):
{chunk.text}

Return JSON with this structure:
{{"entities": [...], "clauses": [...], "summary": "..."}}
"""
    raw = agent.invoke(prompt)
    data = _parse_json_safely(raw)  # Strips markdown fences, validates JSON

    return AnalysisResult(
        chunk_id=chunk.chunk_id,
        job_id=job_id,
        entities=[Entity(**e) for e in data["entities"]],
        clauses=[ContractClause(**c) for c in data["clauses"]],
        summary=data["summary"],
        processing_time_ms=elapsed_ms,
    )
```

Key patterns here:

- **Structured prompts** with explicit JSON schemas reduce hallucination
- **`_parse_json_safely`** handles models that wrap JSON in markdown fences
- **Pydantic v2** models validate field types and constraints automatically
- **`tenacity` retries** with exponential backoff protect against Bedrock throttling

---

## Infrastructure as Code (CDK)

The entire stack is defined in Python CDK:

```python
# Map state: process chunks in parallel with concurrency control
map_state = sfn.Map(
    self, "ParallelAnalysis",
    items_path="$.chunks",
    max_concurrency=10,  # Prevents Bedrock throttling
)
map_state.iterator(agent_task)

# Chain: Chunk → Map → Aggregate → Report → Success
definition = (
    chunk_task
    .next(map_state)
    .next(prepare_aggregator)
    .next(aggregate_task)
    .next(report_task)
    .next(success_state)
)
```

Notable infrastructure choices:

- **ARM64 Lambdas** for better price-performance
- **Lambda tracing** (X-Ray) for distributed debugging
- **CloudWatch alarms** on failed Step Functions executions
- **SNS email alerts** for operational visibility

---

## Type Safety Across Service Boundaries

Every Lambda input and output uses Pydantic v2 models:

```python
class AnalysisResult(BaseModel):
    chunk_id: str
    job_id: str
    entities: list[Entity]
    clauses: list[ContractClause]
    flags: list[ComplianceFlag]
    summary: str
    processing_time_ms: int = Field(..., ge=0)
```

This means:
- **Invalid payloads fail fast** at the Lambda entry point
- **Auto-generated JSON schemas** document the API contract
- **IDE autocomplete** works across the entire pipeline

---

## Testing Strategy

| Layer | Approach |
|-------|----------|
| **Agent Unit Tests** | Mock `Agent.invoke()` with synthetic JSON responses; verify prompt construction and result parsing |
| **Lambda Unit Tests** | `moto` mocks for S3 and DynamoDB; verify handler input/output contracts |
| **Integration** | Local Step Functions with LocalStack (manual) |
| **E2E** | Deploy to dev account, trigger with sample documents |

Example agent test:

```python
def test_run_contract_analysis(sample_chunk):
    mock_agent = MagicMock()
    mock_agent.invoke.return_value = json.dumps({
        "entities": [{"text": "Acme Corp", "type": "organization", ...}],
        "clauses": [{"title": "Payment Terms", "risk_level": "low", ...}],
        "summary": "Low-risk payment terms.",
    })

    result = run_contract_analysis(mock_agent, sample_chunk, "job-001")
    assert result.entities[0].text == "Acme Corp"
    assert result.clauses[0].risk_level == RiskLevel.LOW
```

---

## Lessons Learned

### 1. Chunk Overlap Matters

Without overlap, clauses split across chunk boundaries get garbled. We carry the last ~200 characters forward:

```python
overlap_text = chunk_text[-overlap:]
current_lines = [overlap_text + "\n\n" + next_paragraph]
```

### 2. Concurrency Limits Save You

Bedrock has account-level TPS limits. Setting `max_concurrency=10` on the Map state keeps us well under typical limits while still processing large documents quickly.

### 3. Agents Need Strict Prompts

Vague prompts produce vague JSON. Explicit schemas in the prompt dramatically improve parse reliability:

```python
prompt += '\nReturn JSON with this structure:\n{"entities": [...], "clauses": [...]}'
```

### 4. Structured Logging is Non-Negotiable

With parallel execution, correlating logs across chunks is painful without structured logging. We use `structlog` with `job_id` and `chunk_id` bound to every log line.

---

## When to Use This Pattern

This architecture excels when you have:

- **Long documents** that exceed single-prompt context windows
- **Multiple analysis dimensions** (legal + compliance + financial)
- **Audit requirements** (every step is logged in Step Functions execution history)
- **Variable volume** (serverless scaling from 1 to 1000 concurrent documents)

It's overkill for simple classification tasks, but ideal for high-stakes document review where accuracy and traceability matter.

---

## Repository

The full source code, CDK stack, and test suite are available on GitHub:

🔗 **[github.com/winston-brown/step-functions-strands-demo](https://github.com/winston-brown/step-functions-strands-demo)**

---

## About the Author

I'm Winston Brown, a software engineer building AI-powered automation systems. I write about serverless architecture, LLM orchestration, and production engineering at [winstonbrown.me](https://winstonbrown.me).

---

*Published: April 2025*
