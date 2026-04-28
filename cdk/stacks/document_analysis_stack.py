"""CDK Stack: Document Analysis Pipeline

Provisions:
- S3 buckets (input documents + output reports)
- DynamoDB table (pipeline metadata)
- 4 Lambda functions (chunker, agent executor, aggregator, reporter)
- Step Functions state machine with Map parallelism
- IAM roles with least-privilege permissions
- CloudWatch alarms for failed executions
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import (
    Duration,
    RemovalPolicy,
    Stack,
)
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sns_subscriptions as subs
from aws_cdk import aws_stepfunctions as sfn
from aws_cdk import aws_stepfunctions_tasks as tasks
from constructs import Construct


class DocumentAnalysisStack(Stack):
    """Infrastructure for the Step Functions + Strands Agents pipeline."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs: dict) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ------------------------------------------------------------------
        # S3 Buckets
        # ------------------------------------------------------------------
        input_bucket = s3.Bucket(
            self,
            "InputBucket",
            bucket_name=f"doc-analysis-input-{self.account}-{self.region}",
            removal_policy=RemovalPolicy.RETAIN,
            event_bridge_enabled=True,
        )

        output_bucket = s3.Bucket(
            self,
            "OutputBucket",
            bucket_name=f"doc-analysis-output-{self.account}-{self.region}",
            removal_policy=RemovalPolicy.RETAIN,
        )

        # ------------------------------------------------------------------
        # DynamoDB
        # ------------------------------------------------------------------
        metadata_table = dynamodb.Table(
            self,
            "MetadataTable",
            table_name="DocumentAnalysisMetadata",
            partition_key=dynamodb.Attribute(
                name="job_id",
                type=dynamodb.AttributeType.STRING,
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN,
        )

        # ------------------------------------------------------------------
        # Lambda: Shared configuration
        # ------------------------------------------------------------------
        lambda_defaults = {
            "runtime": lambda_.Runtime.PYTHON_3_12,
            "architecture": lambda_.Architecture.ARM_64,
            "timeout": Duration.minutes(5),
            "memory_size": 2048,
            "environment": {
                "PYTHONPATH": "/var/task/src",
                "OUTPUT_BUCKET": output_bucket.bucket_name,
                "METADATA_TABLE": metadata_table.table_name,
                "BEDROCK_MODEL_ID": "us.amazon.nova-pro-v1:0",
            },
            "tracing": lambda_.Tracing.ACTIVE,
        }

        # Chunker
        chunker_fn = lambda_.Function(
            self,
            "ChunkerFunction",
            code=lambda_.Code.from_asset("src/lambdas/chunker"),
            handler="handler.handler",
            description="Splits uploaded documents into chunks for parallel analysis",
            **lambda_defaults,
        )
        input_bucket.grant_read(chunker_fn)

        # Agent Executor
        agent_fn = lambda_.Function(
            self,
            "AgentExecutorFunction",
            code=lambda_.Code.from_asset("src/lambdas/agent_executor"),
            handler="handler.handler",
            description="Runs Strands Agents for contract analysis and compliance checking",
            memory_size= 4096,  # Higher memory for LLM inference
            **lambda_defaults,
        )
        agent_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                resources=["*"],  # Scope down in production to specific model ARNs
            )
        )

        # Aggregator
        aggregator_fn = lambda_.Function(
            self,
            "AggregatorFunction",
            code=lambda_.Code.from_asset("src/lambdas/aggregator"),
            handler="handler.handler",
            description="Aggregates parallel agent results into a final report",
            **lambda_defaults,
        )

        # Reporter
        reporter_fn = lambda_.Function(
            self,
            "ReporterFunction",
            code=lambda_.Code.from_asset("src/lambdas/reporter"),
            handler="handler.handler",
            description="Writes JSON/Markdown reports to S3 and metadata to DynamoDB",
            **lambda_defaults,
        )
        output_bucket.grant_read_write(reporter_fn)
        metadata_table.grant_write_data(reporter_fn)

        # ------------------------------------------------------------------
        # Step Functions State Machine
        # ------------------------------------------------------------------

        # Task: Chunk document
        chunk_task = tasks.LambdaInvoke(
            self,
            "ChunkDocument",
            lambda_function=chunker_fn,
            output_path="$.Payload",
            retry_on_service_exceptions=True,
        )

        # Task: Execute agent on a single chunk (inside Map)
        agent_task = tasks.LambdaInvoke(
            self,
            "ExecuteAgent",
            lambda_function=agent_fn,
            output_path="$.Payload",
            retry_on_service_exceptions=True,
        )

        # Map state: process chunks in parallel
        map_state = sfn.Map(
            self,
            "ParallelAnalysis",
            items_path="$.chunks",
            max_concurrency=10,
            result_selector={"results.$": "$"},
        )
        map_state.iterator(agent_task)

        # Pass state: prepare aggregator input
        prepare_aggregator = sfn.Pass(
            self,
            "PrepareAggregatorInput",
            parameters={
                "results.$": "$.results",
                "job_id.$": "$.job_id",
                "document_key.$": "$.document_key",
                "framework.$": "$.framework",
            },
        )

        # Task: Aggregate results
        aggregate_task = tasks.LambdaInvoke(
            self,
            "AggregateResults",
            lambda_function=aggregator_fn,
            output_path="$.Payload",
        )

        # Task: Write report
        report_task = tasks.LambdaInvoke(
            self,
            "WriteReport",
            lambda_function=reporter_fn,
            output_path="$.Payload",
        )

        # Success/Failure handling
        success_state = sfn.Succeed(self, "PipelineComplete")
        fail_state = sfn.Fail(
            self,
            "PipelineFailed",
            cause="Document analysis pipeline failed",
            error="PipelineError",
        )

        # Chain definition
        definition = (
            chunk_task
            .next(map_state)
            .next(prepare_aggregator)
            .next(aggregate_task)
            .next(report_task)
            .next(success_state)
        )

        state_machine = sfn.StateMachine(
            self,
            "DocumentAnalysisMachine",
            state_machine_name="DocumentAnalysisPipeline",
            definition=definition,
            timeout=Duration.minutes(30),
            tracing_enabled=True,
            logs=sfn.LogOptions(
                destination=cdk.aws_logs.LogGroup(
                    self,
                    "SFNLogs",
                    log_group_name="/aws/stepfunctions/DocumentAnalysisPipeline",
                    retention=cdk.aws_logs.RetentionDays.ONE_WEEK,
                ),
                level=sfn.LogLevel.ALL,
            ),
        )

        # Allow Step Functions to invoke Lambda functions
        for fn in (chunker_fn, agent_fn, aggregator_fn, reporter_fn):
            fn.grant_invoke(state_machine)

        # ------------------------------------------------------------------
        # Alerting
        # ------------------------------------------------------------------
        alert_topic = sns.Topic(
            self,
            "PipelineAlerts",
            topic_name="document-analysis-alerts",
        )
        alert_topic.add_subscription(subs.EmailSubscription("winston@winstonbrown.me"))

        # CloudWatch alarm on failed executions
        cdk.aws_cloudwatch.Alarm(
            self,
            "PipelineFailureAlarm",
            metric=state_machine.metric_failed(),
            threshold=1,
            evaluation_periods=1,
            alarm_name="DocumentAnalysisPipelineFailed",
            alarm_description="Triggered when a document analysis pipeline execution fails",
        ).add_alarm_action(
            cdk.aws_cloudwatch_actions.SnsAction(alert_topic)
        )

        # ------------------------------------------------------------------
        # Outputs
        # ------------------------------------------------------------------
        cdk.CfnOutput(self, "InputBucketName", value=input_bucket.bucket_name)
        cdk.CfnOutput(self, "OutputBucketName", value=output_bucket.bucket_name)
        cdk.CfnOutput(self, "StateMachineArn", value=state_machine.state_machine_arn)
        cdk.CfnOutput(self, "MetadataTableName", value=metadata_table.table_name)
