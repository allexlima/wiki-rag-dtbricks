#!/usr/bin/env python3
import os
import aws_cdk as cdk
from mediawiki_stack import MediaWikiStack

app = cdk.App()

region = (
    app.node.try_get_context("region")
    or os.environ.get("CDK_DEFAULT_REGION")
    or os.environ.get("AWS_DEFAULT_REGION")
    or "us-east-1"
)

MediaWikiStack(
    app,
    "WikiRagMediaWiki",
    env=cdk.Environment(
        account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
        region=region,
    ),
)

app.synth()
