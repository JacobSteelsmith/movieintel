"""Synth/assertion tests for the Knowledge Base stack (Task 7, REQ-B-1, REQ-X-6.1).

These synthesize ``KnowledgeBaseStack`` into a CloudFormation template with
``aws_cdk.assertions.Template`` and assert the contract the stack must satisfy:

* exactly one Bedrock KB bound to the EXISTING S3 Vectors store (type ``S3_VECTORS``,
  referencing the configured bucket/index), creating NO new vector bucket/index here;
* the embedding model is Titan Text Embeddings V2;
* a least-privilege KB service role (trust = ``bedrock.amazonaws.com`` with
  SourceAccount/SourceArn; no ``bedrock:*``, no ``s3vectors:*``, no Resource ``*``);
* the ``vector_store`` switch defaults to S3 Vectors and provisions NO OpenSearch
  Serverless collection by default;
* outputs include the KB id and the KB role ARN;
* no em dash in the KB name/description.
"""

from __future__ import annotations

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template
from knowledge_base_stack import (
    VECTOR_STORE_ENV,
    KnowledgeBaseStack,
    VectorStore,
    resolve_vector_store,
)


@pytest.fixture(scope="module")
def template() -> Template:
    """Synthesize the stack once per module (default = S3 Vectors)."""
    app = cdk.App()
    stack = KnowledgeBaseStack(
        app, "KnowledgeBaseStack", env=cdk.Environment(account="123456789012", region="us-east-1")
    )
    return Template.from_stack(stack)


def test_single_kb_bound_to_s3_vectors(template: Template) -> None:
    """Exactly one KB whose storage is S3_VECTORS, referencing the configured bucket/index."""
    template.resource_count_is("AWS::Bedrock::KnowledgeBase", 1)
    kbs = template.find_resources("AWS::Bedrock::KnowledgeBase")
    (props,) = (res["Properties"] for res in kbs.values())
    storage = props["StorageConfiguration"]
    assert storage["Type"] == "S3_VECTORS"
    s3v = storage["S3VectorsConfiguration"]
    # Bound by CONFIGURED NAME (index name + bucket ARN) - the CfnKnowledgeBase oneOf
    # requires either IndexArn alone or (IndexName + VectorBucketArn), not both.
    assert s3v["IndexName"] == "movieintel-overviews"
    assert "movieintel-vectors" in s3v["VectorBucketArn"]
    assert "IndexArn" not in s3v


def test_does_not_create_a_new_vector_bucket_or_index(template: Template) -> None:
    """The KB binds to the EXISTING Task 6 store; this stack creates no S3 Vectors resources."""
    template.resource_count_is("AWS::S3Vectors::VectorBucket", 0)
    template.resource_count_is("AWS::S3Vectors::Index", 0)


def test_embedding_model_is_titan_v2(template: Template) -> None:
    """The KB vector config uses the Titan Text Embeddings V2 model ARN (1024-dim match)."""
    kbs = template.find_resources("AWS::Bedrock::KnowledgeBase")
    (props,) = (res["Properties"] for res in kbs.values())
    model_arn = props["KnowledgeBaseConfiguration"]["VectorKnowledgeBaseConfiguration"][
        "EmbeddingModelArn"
    ]
    assert "amazon.titan-embed-text-v2:0" in model_arn


def test_no_opensearch_serverless_by_default(template: Template) -> None:
    """The default switch provisions NO OpenSearch Serverless collection (~$345/mo floor)."""
    template.resource_count_is("AWS::OpenSearchServerless::Collection", 0)


def test_kb_service_role_trust_is_confused_deputy_guarded(template: Template) -> None:
    """The KB role trusts bedrock.amazonaws.com with SourceAccount + SourceArn conditions."""
    template.has_resource_properties(
        "AWS::IAM::Role",
        {
            "AssumeRolePolicyDocument": {
                "Statement": [
                    Match.object_like(
                        {
                            "Effect": "Allow",
                            "Principal": {"Service": "bedrock.amazonaws.com"},
                            "Action": "sts:AssumeRole",
                            "Condition": {
                                "StringEquals": {"aws:SourceAccount": "123456789012"},
                                "ArnLike": {
                                    "aws:SourceArn": Match.string_like_regexp(
                                        r"arn:aws:bedrock:.*:knowledge-base/\*"
                                    )
                                },
                            },
                        }
                    )
                ]
            }
        },
    )


def test_kb_role_iam_is_least_privilege(template: Template) -> None:
    """No KB policy statement uses wildcard actions or Resource '*' (least privilege)."""
    policies = template.find_resources("AWS::IAM::Policy")
    saw_bedrock = False
    saw_s3vectors = False
    for res in policies.values():
        for stmt in res["Properties"]["PolicyDocument"]["Statement"]:
            actions = stmt.get("Action", [])
            if isinstance(actions, str):
                actions = [actions]
            resource = stmt.get("Resource")

            bedrock_actions = [
                a for a in actions if isinstance(a, str) and a.startswith("bedrock:")
            ]
            if bedrock_actions:
                saw_bedrock = True
                assert "bedrock:*" not in bedrock_actions, "wildcard bedrock action present"
                assert resource != "*", "bedrock statement grants Resource '*'"

            s3v_actions = [a for a in actions if isinstance(a, str) and a.startswith("s3vectors:")]
            if s3v_actions:
                saw_s3vectors = True
                assert "s3vectors:*" not in s3v_actions, "wildcard s3vectors action present"
                assert resource != "*", "s3vectors statement grants Resource '*'"

    assert saw_bedrock, "no scoped Bedrock IAM statement found on the KB role"
    assert saw_s3vectors, "no scoped S3 Vectors IAM statement found on the KB role"


def test_kb_role_scoped_to_index_and_titan_only(template: Template) -> None:
    """The KB role grants InvokeModel only on the Titan FM ARN and s3vectors only on the index.

    REQ-X-5.4: bedrock:InvokeModel is scoped to the Titan v2 foundation-model ARN; the
    s3vectors actions are scoped to the specific index ARN, with no Resource '*' and no
    bedrock:*/s3vectors:* wildcard action.
    """
    import json as _json

    titan_substr = "amazon.titan-embed-text-v2:0"
    index_substr = "movieintel-vectors/index/movieintel-overviews"
    policies = template.find_resources("AWS::IAM::Policy")
    saw_invoke = False
    saw_s3vectors = False
    for res in policies.values():
        for stmt in res["Properties"]["PolicyDocument"]["Statement"]:
            actions = stmt.get("Action", [])
            if isinstance(actions, str):
                actions = [actions]
            resource = stmt.get("Resource")
            stmt_text = _json.dumps(stmt)

            if "bedrock:InvokeModel" in actions:
                saw_invoke = True
                assert "bedrock:*" not in actions, "wildcard bedrock action present"
                assert resource != "*", "InvokeModel grants Resource '*'"
                assert titan_substr in stmt_text, f"InvokeModel not scoped to Titan: {stmt_text}"

            s3v = [a for a in actions if isinstance(a, str) and a.startswith("s3vectors:")]
            if s3v:
                saw_s3vectors = True
                assert "s3vectors:*" not in s3v, "wildcard s3vectors action present"
                assert resource != "*", "s3vectors grants Resource '*'"
                assert index_substr in stmt_text, f"s3vectors not scoped to the index: {stmt_text}"

    assert saw_invoke, "no scoped bedrock:InvokeModel grant on the KB role"
    assert saw_s3vectors, "no scoped s3vectors grant on the KB role"


def test_outputs_include_kb_id_and_role_arn(template: Template) -> None:
    """Stack exports the Knowledge Base id and the KB service role ARN."""
    outputs = template.find_outputs("*")
    descriptions = " ".join(o.get("Description", "") for o in outputs.values())
    assert "Knowledge Base id" in descriptions
    assert "Knowledge Base service role" in descriptions


def test_kb_name_and_description_have_no_em_dash(template: Template) -> None:
    """AWS steering: hyphens only, never em dashes, in KB name/description."""
    kbs = template.find_resources("AWS::Bedrock::KnowledgeBase")
    (props,) = (res["Properties"] for res in kbs.values())
    assert "\u2014" not in props["Name"], "em dash in KB name"
    assert "\u2014" not in props.get("Description", ""), "em dash in KB description"


def test_vector_store_switch_defaults_to_s3_vectors() -> None:
    """The switch defaults to S3 Vectors when unset or unrecognized (no-fallback-by-default)."""
    assert resolve_vector_store(env={}) is VectorStore.S3_VECTORS
    assert resolve_vector_store(env={VECTOR_STORE_ENV: "nonsense"}) is VectorStore.S3_VECTORS
    assert resolve_vector_store(env={VECTOR_STORE_ENV: "s3_vectors"}) is VectorStore.S3_VECTORS


def test_vector_store_switch_honors_explicit_opensearch_selection() -> None:
    """The fallback is reachable only by explicit opt-in (present but inert by default)."""
    assert (
        resolve_vector_store(env={VECTOR_STORE_ENV: "opensearch_serverless"})
        is VectorStore.OPENSEARCH_SERVERLESS
    )
