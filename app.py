#!/usr/bin/env python3
"""AWS CDK app entry point.

Usage:
    cdk bootstrap
    cdk deploy
"""

import aws_cdk as cdk

from cdk.stacks.document_analysis_stack import DocumentAnalysisStack

app = cdk.App()

DocumentAnalysisStack(
    app,
    "DocumentAnalysisStack",
    env=cdk.Environment(
        account=cdk.Aws.ACCOUNT_ID,
        region=cdk.Aws.REGION,
    ),
)

app.synth()
