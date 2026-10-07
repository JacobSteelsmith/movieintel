"""CDK app entry point for the movieintel system (REQ-X-6).

Synthesize with ``cd infra && npx aws-cdk@2 synth`` (no global ``cdk`` on PATH). This app
defines three stacks: the Task 6 pipeline stack (stores, Guardrail, Lambdas, state
machine), the Task 7 Knowledge Base stack (a Bedrock KB over the existing S3 Vectors
index), and the Task 10 serving stack (an HTTP API over the agent loop). All three share
the same region (us-east-1).
"""

from __future__ import annotations

import aws_cdk as cdk
from knowledge_base_stack import KnowledgeBaseStack
from pipeline_stack import MovieIntelPipelineStack
from serving_stack import MovieIntelServingStack

app = cdk.App()
env = cdk.Environment(region="us-east-1")
MovieIntelPipelineStack(
    app,
    "MovieIntelPipelineStack",
    env=env,
)
KnowledgeBaseStack(
    app,
    "KnowledgeBaseStack",
    env=env,
)
MovieIntelServingStack(
    app,
    "MovieIntelServingStack",
    env=env,
)
app.synth()
