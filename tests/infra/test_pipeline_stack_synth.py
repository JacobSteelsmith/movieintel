"""Synth/assertion tests for the enrichment pipeline stack (FEAT-002, REQ-A-6).

These synthesize ``MovieIntelPipelineStack`` into a CloudFormation template with
``aws_cdk.assertions.Template`` and assert the contract the stack must satisfy:

* exactly one on-demand DynamoDB table with a GSI1 on GSI1PK/GSI1SK;
* a Step Functions state machine whose definition orders the seven states and carries a
  Catch inside the Map state;
* a Bedrock Guardrail resource;
* at least seven Lambda functions (six pipeline steps + Evaluate);
* the Enrich Lambda's Bedrock IAM is scoped to specific ARNs (no ``bedrock:*``, no
  Resource ``*`` on the Bedrock statement).
"""

from __future__ import annotations

import json

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template
from pipeline_stack import MovieIntelPipelineStack


@pytest.fixture(scope="module")
def template() -> Template:
    """Synthesize the stack once per module."""
    app = cdk.App()
    stack = MovieIntelPipelineStack(
        app, "MovieIntelPipelineStack", env=cdk.Environment(region="us-east-1")
    )
    return Template.from_stack(stack)


def test_single_on_demand_table_with_gsi1(template: Template) -> None:
    """One DynamoDB table, PAY_PER_REQUEST, with a GSI1 on GSI1PK/GSI1SK."""
    template.resource_count_is("AWS::DynamoDB::Table", 1)
    template.has_resource_properties(
        "AWS::DynamoDB::Table",
        {
            "BillingMode": "PAY_PER_REQUEST",
            "GlobalSecondaryIndexes": [
                {
                    "IndexName": "GSI1",
                    "KeySchema": [
                        {"AttributeName": "GSI1PK", "KeyType": "HASH"},
                        {"AttributeName": "GSI1SK", "KeyType": "RANGE"},
                    ],
                }
            ],
        },
    )


def test_table_has_ttl_on_expires_at(template: Template) -> None:
    """The MovieIntel table enables DynamoDB TTL on the ``expires_at`` attribute.

    Async-job records (design Decision 2) carry an ``expires_at`` epoch-seconds attribute
    so DynamoDB reaps them automatically; existing movie items without it never expire.
    """
    template.has_resource_properties(
        "AWS::DynamoDB::Table",
        {"TimeToLiveSpecification": {"AttributeName": "expires_at", "Enabled": True}},
    )


def test_state_machine_orders_states_and_has_map_catch(template: Template) -> None:
    """The state machine definition orders the seven states and has a Map Catch.

    Asserts on the parsed Amazon States Language JSON rather than string offsets: the
    top-level chain runs Extract -> the Map -> Evaluate, and the Map's item processor
    chains ComputePES -> Enrich -> Validate -> PersistDynamoDB -> EmbedAndUpsertVector
    with a per-item Catch.
    """
    template.resource_count_is("AWS::StepFunctions::StateMachine", 1)
    machines = template.find_resources("AWS::StepFunctions::StateMachine")
    (definition,) = (res["Properties"]["DefinitionString"] for res in machines.values())
    text = _flatten_definition(definition)
    asl = json.loads(text)

    top_states = asl["States"]
    assert "Extract" in top_states
    assert "Evaluate" in top_states

    # The top-level flow: Extract -> <Map> -> Evaluate.
    map_name = top_states["Extract"]["Next"]
    map_state = top_states[map_name]
    assert map_state["Type"] == "Map"
    assert map_state["Next"] == "Evaluate"

    # Walk the Map's item-processor chain and confirm the ordered state names.
    processor = map_state["ItemProcessor"]
    item_states = processor["States"]
    chain_order = _linear_chain(item_states, processor["StartAt"])
    expected_prefix = [
        "ComputePES",
        "Enrich",
        "Validate",
    ]
    assert chain_order[: len(expected_prefix)] == expected_prefix, chain_order
    # Persist and Embed live on the 'enriched' branch of the Choice after Validate.
    assert "PersistDynamoDB" in item_states
    assert "EmbedAndUpsertVector" in item_states
    assert item_states["PersistDynamoDB"]["Next"] == "EmbedAndUpsertVector"

    # Per-item Catch exists on the processing tasks.
    assert any("Catch" in state for state in item_states.values()), "Map item chain has no Catch"

    # The Catch targets are skip-recording Pass states that MUST carry movie_id forward
    # so the downstream Evaluate state can key the skip (regression guard: a failure
    # record without movie_id KeyErrored the whole Evaluate step on the live run). The id
    # lives at a different path pre- vs post-persist, so there are two record-skip states.
    failure_states = {
        name: state
        for name, state in item_states.items()
        if state.get("Type") == "Pass"
        and isinstance(state.get("Parameters"), dict)
        and state["Parameters"].get("status") == "skipped"
    }
    assert failure_states, "no skip-recording Pass state found in the Map item chain"
    for name, state in failure_states.items():
        params = state["Parameters"]
        assert "movie_id.$" in params, f"{name} does not carry movie_id forward: {params}"


def _linear_chain(states: dict[str, dict[str, object]], start: str) -> list[str]:
    """Follow ``Next`` links from ``start`` until a terminal/branching state."""
    order: list[str] = []
    current: str | None = start
    while current is not None and current in states:
        order.append(current)
        state = states[current]
        nxt = state.get("Next")
        current = nxt if isinstance(nxt, str) else None
    return order


def test_guardrail_resource_exists(template: Template) -> None:
    """A Bedrock Guardrail is created by this stack with hyphenated name/description.

    Extends the em-dash check to the topic and PII config descriptions so no part of the
    guardrail policy introduces an em dash (AWS steering: hyphens only).
    """
    template.resource_count_is("AWS::Bedrock::Guardrail", 1)
    guardrails = template.find_resources("AWS::Bedrock::Guardrail")
    (props,) = (res["Properties"] for res in guardrails.values())
    assert "\u2014" not in props["Name"], "em dash in guardrail name"
    assert "\u2014" not in props.get("Description", ""), "em dash in guardrail description"
    assert props["ContentPolicyConfig"]["FiltersConfig"]
    assert props["TopicPolicyConfig"]["TopicsConfig"]
    assert props["SensitiveInformationPolicyConfig"]["PiiEntitiesConfig"]
    for topic in props["TopicPolicyConfig"]["TopicsConfig"]:
        assert "\u2014" not in topic.get("Definition", ""), "em dash in denied-topic definition"
        for example in topic.get("Examples", []):
            assert "\u2014" not in example, "em dash in denied-topic example"
    assert "\u2014" not in props.get("BlockedInputMessaging", ""), "em dash in blocked-input msg"
    assert "\u2014" not in props.get("BlockedOutputsMessaging", ""), "em dash in blocked-out msg"


def test_enrichment_guardrail_content_filters_omit_prompt_attack(template: Template) -> None:
    """The ENRICHMENT guardrail has the five harmful-content filters and NO PROMPT_ATTACK.

    Enrichment is a trusted batch path with no untrusted user input, so it deliberately
    omits PROMPT_ATTACK: a PROMPT_ATTACK input filter was misclassifying the enrichment
    prompt's JSON-formatting directives as injection and blocked ~98/100 enrichments. The
    five harmful-content filters (HATE, VIOLENCE, SEXUAL, INSULTS, MISCONDUCT) are HIGH on
    input and output; PROMPT_ATTACK is asserted ABSENT. PROMPT_ATTACK lives on the serving
    guardrail instead (see tests/infra/test_serving_stack_synth.py). PII ANONYMIZE covers
    EMAIL/PHONE (NAME is intentionally excluded because ANONYMIZE on NAME rewrites
    legitimate movie cast/director/character names in the model's JSON output and corrupted
    it) and the ``non-movie-advice`` denied topic is present.
    """
    guardrails = template.find_resources("AWS::Bedrock::Guardrail")
    (props,) = (res["Properties"] for res in guardrails.values())

    filters = props["ContentPolicyConfig"]["FiltersConfig"]
    by_type = {f["Type"]: f for f in filters}
    assert set(by_type) == {
        "HATE",
        "VIOLENCE",
        "SEXUAL",
        "INSULTS",
        "MISCONDUCT",
    }, by_type
    for filter_type in ("HATE", "VIOLENCE", "SEXUAL", "INSULTS", "MISCONDUCT"):
        assert by_type[filter_type]["InputStrength"] == "HIGH", filter_type
        assert by_type[filter_type]["OutputStrength"] == "HIGH", filter_type
    # Positive regression guard: enrichment must NOT carry PROMPT_ATTACK.
    assert "PROMPT_ATTACK" not in by_type, by_type

    pii = {e["Type"] for e in props["SensitiveInformationPolicyConfig"]["PiiEntitiesConfig"]}
    assert pii == {"EMAIL", "PHONE"}, pii
    actions = {e["Action"] for e in props["SensitiveInformationPolicyConfig"]["PiiEntitiesConfig"]}
    assert actions == {"ANONYMIZE"}, actions

    topics = {t["Name"] for t in props["TopicPolicyConfig"]["TopicsConfig"]}
    assert "non-movie-advice" in topics, topics


def test_at_least_seven_lambda_functions(template: Template) -> None:
    """Six pipeline steps + Evaluate means at least seven Lambda functions."""
    functions = template.find_resources("AWS::Lambda::Function")
    pipeline = [
        name for name, res in functions.items() if res["Properties"].get("Runtime") == "python3.12"
    ]
    assert len(pipeline) >= 7, f"expected >= 7 python3.12 Lambdas, found {len(pipeline)}"


def test_handlers_use_the_handlers_package_prefix(template: Template) -> None:
    """Handler strings are ``handlers.<step>.handler.handler`` (packaging seam guard).

    The code asset ships the ``handlers`` package as a top-level package, so the runtime
    handler string must carry the ``handlers.`` prefix to match the deployed layout and
    the intra-handler ``from handlers.shared import ...`` imports. A bare ``<step>.``
    prefix would import-fail at invoke time (the asset has no top-level ``<step>`` dir).
    """
    functions = template.find_resources("AWS::Lambda::Function")
    handlers = [
        res["Properties"]["Handler"]
        for res in functions.values()
        if res["Properties"].get("Runtime") == "python3.12"
    ]
    assert handlers, "no python3.12 Lambda handlers found"
    for handler in handlers:
        assert handler.startswith("handlers."), handler
        assert handler.endswith(".handler.handler"), handler


def test_enrich_lambda_bedrock_iam_is_scoped(template: Template) -> None:
    """No Bedrock statement uses ``bedrock:*`` or Resource ``*`` (least privilege)."""
    policies = template.find_resources("AWS::IAM::Policy")
    saw_bedrock_statement = False
    for res in policies.values():
        statements = res["Properties"]["PolicyDocument"]["Statement"]
        for stmt in statements:
            actions = stmt.get("Action", [])
            if isinstance(actions, str):
                actions = [actions]
            bedrock_actions = [
                a for a in actions if isinstance(a, str) and a.startswith("bedrock:")
            ]
            if not bedrock_actions:
                continue
            saw_bedrock_statement = True
            assert "bedrock:*" not in bedrock_actions, "wildcard bedrock action present"
            resource = stmt.get("Resource")
            assert resource != "*", "bedrock statement grants Resource '*'"
            if isinstance(resource, list):
                assert "*" not in resource, "bedrock statement grants Resource '*'"
    assert saw_bedrock_statement, "no scoped Bedrock IAM statement found"


def test_enrich_lambda_env_pins_sonnet_45_inference_profile(template: Template) -> None:
    """At least one Lambda carries BEDROCK_MODEL_ID = the Sonnet 4.5 inference profile id."""
    functions = template.find_resources("AWS::Lambda::Function")
    model_ids = [
        res["Properties"].get("Environment", {}).get("Variables", {}).get("BEDROCK_MODEL_ID")
        for res in functions.values()
        if res["Properties"].get("Runtime") == "python3.12"
    ]
    assert "us.anthropic.claude-sonnet-4-5-20250929-v1:0" in model_ids, model_ids
    assert all(mid != "anthropic.claude-3-5-sonnet-20240620-v1:0" for mid in model_ids), (
        f"a Lambda still pins the retired model id: {model_ids}"
    )


def test_enrich_invoke_model_covers_profile_and_cross_region_fm_arns(template: Template) -> None:
    """The InvokeModel grant lists the profile ARN + the three cross-region FM ARNs.

    This is the regression guard for the single-ARN bug: invoking a cross-region
    inference profile needs bedrock:InvokeModel on both the ``inference-profile/...``
    ARN and each underlying ``::foundation-model/...`` ARN the profile routes to. The
    account id is a synth-time token, so match on the ARN resource-path substrings via
    the stringified statement (same token-tolerant approach as _flatten_definition).
    """
    profile_suffix = "inference-profile/us.anthropic.claude-sonnet-4-5-20250929-v1:0"
    fm_arns = {
        f"arn:aws:bedrock:{region}::foundation-model/anthropic.claude-sonnet-4-5-20250929-v1:0"
        for region in ("us-east-1", "us-east-2", "us-west-2")
    }

    policies = template.find_resources("AWS::IAM::Policy")
    saw_invoke_profile = False
    for res in policies.values():
        statements = res["Properties"]["PolicyDocument"]["Statement"]
        for stmt in statements:
            actions = stmt.get("Action", [])
            if isinstance(actions, str):
                actions = [actions]
            if "bedrock:InvokeModel" not in actions:
                continue
            stmt_text = json.dumps(stmt)
            if profile_suffix not in stmt_text:
                continue
            saw_invoke_profile = True
            for fm_arn in fm_arns:
                assert fm_arn in stmt_text, f"missing cross-region FM ARN {fm_arn} in {stmt_text}"
            # The retired model must not appear anywhere in the enrich grant.
            assert "anthropic.claude-3-5-sonnet" not in stmt_text, stmt_text
    assert saw_invoke_profile, "no bedrock:InvokeModel grant covering the inference-profile ARN"


def _invoke_model_statements(template: Template) -> list[dict[str, object]]:
    """Return every IAM policy statement that grants bedrock:InvokeModel."""
    statements: list[dict[str, object]] = []
    for res in template.find_resources("AWS::IAM::Policy").values():
        for stmt in res["Properties"]["PolicyDocument"]["Statement"]:
            actions = stmt.get("Action", [])
            if isinstance(actions, str):
                actions = [actions]
            if "bedrock:InvokeModel" in actions:
                statements.append(stmt)
    return statements


def _as_list(resource: object) -> list[object]:
    """Normalize an IAM Resource field to a list."""
    if isinstance(resource, list):
        return resource
    return [resource]


def test_enrich_invoke_model_lists_exactly_profile_and_three_fm_arns(template: Template) -> None:
    """The enrich InvokeModel grant lists EXACTLY the profile ARN + 3 FM ARNs, nothing broader.

    The account id is a synth-time token so the profile ARN resolves via Fn::Join; match
    it on the stringified statement. The three cross-region foundation-model ARNs are
    account-less literals, so assert each appears and that the Resource list has exactly
    four entries (one profile + three FMs) with no ``*`` and no ``bedrock:*``.
    """
    profile_suffix = "inference-profile/us.anthropic.claude-sonnet-4-5-20250929-v1:0"
    fm_arns = {
        f"arn:aws:bedrock:{region}::foundation-model/anthropic.claude-sonnet-4-5-20250929-v1:0"
        for region in ("us-east-1", "us-east-2", "us-west-2")
    }

    enrich_stmt = None
    for stmt in _invoke_model_statements(template):
        if profile_suffix in json.dumps(stmt):
            enrich_stmt = stmt
            break
    assert enrich_stmt is not None, "no enrich InvokeModel statement covering the profile ARN"

    actions = enrich_stmt.get("Action", [])
    if isinstance(actions, str):
        actions = [actions]
    assert actions == ["bedrock:InvokeModel"], actions

    resources = _as_list(enrich_stmt.get("Resource"))
    assert len(resources) == 4, f"expected exactly 4 ARNs, found {len(resources)}: {resources}"
    assert "*" not in resources, resources
    stmt_text = json.dumps(enrich_stmt)
    for fm_arn in fm_arns:
        assert fm_arn in stmt_text, f"missing FM ARN {fm_arn}"
    # Exactly one of the four is the profile ARN (Fn::Join token), the other three are FMs.
    literal_fm_count = sum(1 for r in resources if isinstance(r, str) and r in fm_arns)
    assert literal_fm_count == 3, resources


def test_embed_invoke_model_scoped_to_titan_only(template: Template) -> None:
    """The embed InvokeModel grant is exactly the Titan v2 FM ARN; PutVectors on the index."""
    titan_arn = "arn:aws:bedrock:us-east-1::foundation-model/amazon.titan-embed-text-v2:0"
    embed_stmt = None
    for stmt in _invoke_model_statements(template):
        resources = _as_list(stmt.get("Resource"))
        if titan_arn in [r for r in resources if isinstance(r, str)]:
            embed_stmt = stmt
            break
    assert embed_stmt is not None, "no embed InvokeModel statement scoped to the Titan v2 ARN"
    resources = _as_list(embed_stmt.get("Resource"))
    assert resources == [titan_arn], resources

    # s3vectors:PutVectors must be scoped to the index ARN (not Resource '*').
    saw_put = False
    for res in template.find_resources("AWS::IAM::Policy").values():
        for stmt in res["Properties"]["PolicyDocument"]["Statement"]:
            actions = stmt.get("Action", [])
            if isinstance(actions, str):
                actions = [actions]
            if "s3vectors:PutVectors" not in actions:
                continue
            saw_put = True
            assert "s3vectors:*" not in actions, "wildcard s3vectors action present"
            assert stmt.get("Resource") != "*", "s3vectors PutVectors grants Resource '*'"
    assert saw_put, "no s3vectors:PutVectors grant found"


def _dynamodb_actions_in_template(template: Template) -> set[str]:
    """Collect every DynamoDB action granted anywhere in the pipeline template."""
    actions_seen: set[str] = set()
    for res in template.find_resources("AWS::IAM::Policy").values():
        for stmt in res["Properties"]["PolicyDocument"]["Statement"]:
            actions = stmt.get("Action", [])
            if isinstance(actions, str):
                actions = [actions]
            for action in actions:
                if isinstance(action, str) and action.startswith("dynamodb:"):
                    actions_seen.add(action)
    return actions_seen


def test_persist_lambda_has_write_but_not_broad_table_access(template: Template) -> None:
    """The pipeline grants DynamoDB write (persist) but never a wildcard dynamodb:* action.

    The persist handler only PutItems, so it needs write access; no pipeline Lambda should
    hold a ``dynamodb:*`` wildcard. This also guards against re-introducing a read-write
    grant where only write is required.
    """
    dynamo_actions = _dynamodb_actions_in_template(template)
    assert dynamo_actions, "no DynamoDB grant found in the pipeline template"
    assert "dynamodb:*" not in dynamo_actions, dynamo_actions
    # A write grant must be present (persist PutItem path).
    assert any(a in dynamo_actions for a in ("dynamodb:PutItem", "dynamodb:BatchWriteItem")), (
        dynamo_actions
    )


def test_embed_lambda_role_has_no_dynamodb_access(template: Template) -> None:
    """Regression guard: the embed_vector Lambda role grants NO DynamoDB action at all.

    The embed handler only embeds + upserts vectors; it never touches DynamoDB, so the
    previously over-broad grant_read_write_data on it was removed. Match the embed role's
    inline policy by the Titan InvokeModel + s3vectors:PutVectors fingerprint and assert
    that same policy document carries no dynamodb action.
    """
    titan_arn = "arn:aws:bedrock:us-east-1::foundation-model/amazon.titan-embed-text-v2:0"
    for res in template.find_resources("AWS::IAM::Policy").values():
        statements = res["Properties"]["PolicyDocument"]["Statement"]
        doc_text = json.dumps(statements)
        if titan_arn in doc_text and "s3vectors:PutVectors" in doc_text:
            # This is the embed_vector role's inline policy; it must carry no DynamoDB action.
            assert "dynamodb:" not in doc_text, (
                f"embed_vector role has a DynamoDB grant: {doc_text}"
            )
            return
    raise AssertionError("could not locate the embed_vector role inline policy")


def _flatten_definition(definition: object) -> str:
    """Flatten an Fn::Join DefinitionString into a single searchable string."""
    if isinstance(definition, str):
        return definition
    if isinstance(definition, dict):
        if "Fn::Join" in definition:
            _, parts = definition["Fn::Join"]
            return "".join(_flatten_definition(p) for p in parts)
        # Token ref (e.g. {"Ref": ...} / {"Fn::GetAtt": ...}). These sit inside an
        # already-quoted ASL string value, so substitute a plain placeholder that keeps
        # the surrounding JSON valid.
        return "TOKEN"
    if isinstance(definition, list):
        return "".join(_flatten_definition(p) for p in definition)
    return str(definition)
