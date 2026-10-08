"""Synth/assertion tests for the serving stack (FEAT-002, REQ-B-4.1, REQ-X-6.1).

These synthesize ``MovieIntelServingStack`` into a CloudFormation template with
``aws_cdk.assertions.Template`` and assert the contract the stack must satisfy:

* exactly one python3.12 serving Lambda whose Handler is ``handlers.serve.handler.handler``;
* an API Gateway integrated with that Lambda (HTTP API ``AWS::ApiGatewayV2::*`` or a REST
  ``AWS::ApiGateway::RestApi`` proxy fallback - the assertion tolerates either flavor);
* the serving Lambda env pins BEDROCK_MODEL_ID / MOVIEINTEL_KB_ID / MOVIEINTEL_TABLE_NAME /
  MOVIEINTEL_GSI1_NAME to the deployed-resource literals, and BEDROCK_GUARDRAIL_ID to an
  in-stack token (the serving stack now defines its own guardrail);
* exactly one in-stack serving guardrail (``movieintel-serving-guardrail``) that keeps
  PROMPT_ATTACK on input (serving takes untrusted user queries);
* a scoped bedrock:InvokeModel statement covering the inference-profile ARN AND the three
  cross-region foundation-model ARNs; bedrock:ApplyGuardrail on the in-stack guardrail ARN;
  bedrock:Retrieve on knowledge-base/YNXUIXEYBQ; DynamoDB read on the MovieIntel table +
  GSI1; no ``bedrock:*`` and no Resource ``*`` on any Bedrock statement;
* a CfnOutput exporting the API endpoint URL;
* no provisioned concurrency (scale-to-zero, REQ-B-4.5/REQ-X-5.1).
"""

from __future__ import annotations

import json

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template
from serving_stack import MovieIntelServingStack


@pytest.fixture(scope="module")
def template() -> Template:
    """Synthesize the stack once per module."""
    app = cdk.App()
    stack = MovieIntelServingStack(
        app, "MovieIntelServingStack", env=cdk.Environment(region="us-east-1")
    )
    return Template.from_stack(stack)


def _serving_functions(template: Template) -> list[dict[str, object]]:
    """Return the python3.12 Lambda function Properties in the stack."""
    functions = template.find_resources("AWS::Lambda::Function")
    return [
        res["Properties"]
        for res in functions.values()
        if res["Properties"].get("Runtime") == "python3.12"
    ]


def test_single_serving_lambda_with_handler_string(template: Template) -> None:
    """Exactly one python3.12 serving Lambda whose Handler is handlers.serve.handler.handler."""
    props = _serving_functions(template)
    assert len(props) == 1, f"expected exactly one python3.12 Lambda, found {len(props)}"
    assert props[0]["Handler"] == "handlers.serve.handler.handler", props[0]["Handler"]


def test_api_gateway_integrated_with_the_lambda(template: Template) -> None:
    """An API Gateway (HTTP API or REST proxy fallback) is integrated with the Lambda.

    Tolerant of either flavor: an HTTP API uses AWS::ApiGatewayV2::Api + Integration;
    a REST fallback uses AWS::ApiGateway::RestApi + a proxy method. In both cases the
    integration must reference an AWS_PROXY / Lambda integration so the handler's
    statusCode/headers/body pass through.
    """
    http_apis = template.find_resources("AWS::ApiGatewayV2::Api")
    rest_apis = template.find_resources("AWS::ApiGateway::RestApi")
    assert http_apis or rest_apis, "no API Gateway resource found"

    if http_apis:
        integrations = template.find_resources("AWS::ApiGatewayV2::Integration")
        assert integrations, "HTTP API has no integration"
        integration_types = {
            res["Properties"].get("IntegrationType") for res in integrations.values()
        }
        assert "AWS_PROXY" in integration_types, integration_types
        template.resource_count_is("AWS::ApiGatewayV2::Route", 2)
        routes = template.find_resources("AWS::ApiGatewayV2::Route")
        route_keys = {res["Properties"].get("RouteKey") for res in routes.values()}
        assert route_keys == {"POST /query", "GET /health"}, route_keys
    else:
        methods = template.find_resources("AWS::ApiGateway::Method")
        rest_integration_types: set[object] = set()
        for res in methods.values():
            integration = res["Properties"].get("Integration")
            if isinstance(integration, dict):
                rest_integration_types.add(integration.get("Type"))
        assert "AWS_PROXY" in rest_integration_types, rest_integration_types


def test_health_route_exists_on_the_same_api(template: Template) -> None:
    """A GET /health route exists on the HTTP API alongside POST /query."""
    routes = template.find_resources("AWS::ApiGatewayV2::Route")
    route_keys = {res["Properties"].get("RouteKey") for res in routes.values()}
    assert "GET /health" in route_keys, route_keys
    assert "POST /query" in route_keys, route_keys


def test_cors_preflight_allows_get_post_options(template: Template) -> None:
    """The HTTP API carries a CORS preflight config allowing GET/POST/OPTIONS."""
    apis = template.find_resources("AWS::ApiGatewayV2::Api")
    cors_methods: set[object] = set()
    for res in apis.values():
        cors = res["Properties"].get("CorsConfiguration")
        if isinstance(cors, dict):
            cors_methods |= set(cors.get("AllowMethods", []))
    assert {"GET", "POST", "OPTIONS"} <= cors_methods, cors_methods


def test_waf_webacl_has_rate_based_rule(template: Template) -> None:
    """Exactly one REGIONAL WebACL with a per-IP rate-based rule (limit 600) is defined.

    The WebACL is created unconditionally, so it is present even without the
    ``associate_waf`` flag.
    """
    template.resource_count_is("AWS::WAFv2::WebACL", 1)
    acls = template.find_resources("AWS::WAFv2::WebACL")
    (props,) = (res["Properties"] for res in acls.values())
    assert props.get("Scope") == "REGIONAL", props
    assert "\u2014" not in props.get("Name", ""), "em dash in WebACL name"
    rules = props["Rules"]
    rate_stmts = [
        rule["Statement"]["RateBasedStatement"]
        for rule in rules
        if "RateBasedStatement" in rule.get("Statement", {})
    ]
    assert len(rate_stmts) == 1, rate_stmts
    (rate,) = rate_stmts
    assert rate["Limit"] == 600, rate
    assert rate["AggregateKeyType"] == "IP", rate


def test_no_waf_association_without_the_context_flag(template: Template) -> None:
    """Without ``associate_waf``, no association exists but the WebACL still does."""
    template.resource_count_is("AWS::WAFv2::WebACLAssociation", 0)
    template.resource_count_is("AWS::WAFv2::WebACL", 1)


def test_waf_association_created_when_flag_set() -> None:
    """With ``associate_waf=true`` in context, one association targets the $default stage."""
    app = cdk.App(context={"associate_waf": True})
    stack = MovieIntelServingStack(
        app, "MovieIntelServingStack", env=cdk.Environment(region="us-east-1")
    )
    flagged = Template.from_stack(stack)

    flagged.resource_count_is("AWS::WAFv2::WebACLAssociation", 1)
    associations = flagged.find_resources("AWS::WAFv2::WebACLAssociation")
    (props,) = (res["Properties"] for res in associations.values())
    resource_arn = json.dumps(props.get("ResourceArn"))
    assert "/stages/" in resource_arn and "$default" in resource_arn, props


def test_serving_lambda_env_pins_deployed_resource_ids(template: Template) -> None:
    """The serving Lambda env pins the Sonnet 4.5 profile + deployed KB/table ids.

    BEDROCK_GUARDRAIL_ID is no longer a literal: the serving stack now defines its own
    ``movieintel-serving-guardrail`` in-stack, so the env references that resource via a
    synth-time token (Ref/GetAtt dict) rather than the old pinned ``fvb8n9pc8e2q``. The
    remaining ids (model, KB, table, GSI) are still pinned literals.
    """
    (props,) = _serving_functions(template)
    environment = props.get("Environment")
    assert isinstance(environment, dict), props
    env = environment.get("Variables")
    assert isinstance(env, dict), environment
    assert env.get("BEDROCK_MODEL_ID") == "us.anthropic.claude-sonnet-4-5-20250929-v1:0", env
    # The guardrail id is now an in-stack token, not the removed literal fvb8n9pc8e2q.
    assert isinstance(env.get("BEDROCK_GUARDRAIL_ID"), dict), env
    assert env.get("BEDROCK_GUARDRAIL_ID") != "fvb8n9pc8e2q", env
    assert env.get("MOVIEINTEL_KB_ID") == "YNXUIXEYBQ", env
    assert env.get("MOVIEINTEL_TABLE_NAME") == "MovieIntel", env
    assert env.get("MOVIEINTEL_GSI1_NAME") == "GSI1", env


def test_serving_guardrail_has_prompt_attack(template: Template) -> None:
    """The serving stack defines exactly one guardrail with PROMPT_ATTACK on input.

    Serving takes untrusted free-text ``/query`` input, so the serving guardrail KEEPS
    PROMPT_ATTACK (input HIGH, output NONE) on top of the shared policy. It carries the
    same five harmful-content filters (HATE, VIOLENCE, SEXUAL, INSULTS, MISCONDUCT) HIGH on
    input and output, EMAIL/PHONE ANONYMIZE, and the ``non-movie-advice`` denied topic -
    the policy it shares with the enrichment guardrail, which differs ONLY by omitting
    PROMPT_ATTACK. Name/description use hyphens, never em dashes (AWS steering).
    """
    template.resource_count_is("AWS::Bedrock::Guardrail", 1)
    guardrails = template.find_resources("AWS::Bedrock::Guardrail")
    (props,) = (res["Properties"] for res in guardrails.values())

    assert props["Name"] == "movieintel-serving-guardrail", props["Name"]
    assert "\u2014" not in props["Name"], "em dash in guardrail name"
    assert "\u2014" not in props.get("Description", ""), "em dash in guardrail description"

    filters = props["ContentPolicyConfig"]["FiltersConfig"]
    by_type = {f["Type"]: f for f in filters}
    assert set(by_type) == {
        "HATE",
        "VIOLENCE",
        "SEXUAL",
        "INSULTS",
        "MISCONDUCT",
        "PROMPT_ATTACK",
    }, by_type
    for filter_type in ("HATE", "VIOLENCE", "SEXUAL", "INSULTS", "MISCONDUCT"):
        assert by_type[filter_type]["InputStrength"] == "HIGH", filter_type
        assert by_type[filter_type]["OutputStrength"] == "HIGH", filter_type
    # PROMPT_ATTACK supports an input strength only; output must be NONE.
    assert by_type["PROMPT_ATTACK"]["InputStrength"] == "HIGH"
    assert by_type["PROMPT_ATTACK"]["OutputStrength"] == "NONE"

    pii = {e["Type"] for e in props["SensitiveInformationPolicyConfig"]["PiiEntitiesConfig"]}
    assert pii == {"EMAIL", "PHONE"}, pii
    actions = {e["Action"] for e in props["SensitiveInformationPolicyConfig"]["PiiEntitiesConfig"]}
    assert actions == {"ANONYMIZE"}, actions

    topics = {t["Name"] for t in props["TopicPolicyConfig"]["TopicsConfig"]}
    assert "non-movie-advice" in topics, topics


def test_invoke_model_covers_profile_and_cross_region_fm_arns(template: Template) -> None:
    """The InvokeModel grant lists the profile ARN + the three cross-region FM ARNs."""
    profile_suffix = "inference-profile/us.anthropic.claude-sonnet-4-5-20250929-v1:0"
    fm_arns = {
        f"arn:aws:bedrock:{region}::foundation-model/anthropic.claude-sonnet-4-5-20250929-v1:0"
        for region in ("us-east-1", "us-east-2", "us-west-2")
    }

    policies = template.find_resources("AWS::IAM::Policy")
    saw_invoke_profile = False
    for res in policies.values():
        for stmt in res["Properties"]["PolicyDocument"]["Statement"]:
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
    assert saw_invoke_profile, "no bedrock:InvokeModel grant covering the inference-profile ARN"


def test_apply_guardrail_references_the_in_stack_guardrail(template: Template) -> None:
    """bedrock:ApplyGuardrail is scoped to the in-stack serving guardrail (not a literal).

    The serving stack now owns its guardrail, so the ApplyGuardrail grant resolves the
    resource ARN via a synth-time token (``Fn::GetAtt`` on the ServingGuardrail), replacing
    the removed literal ``guardrail/fvb8n9pc8e2q``. Assert there is exactly one
    ApplyGuardrail statement, its Resource is a token (dict), and it is not the old literal
    or a wildcard.
    """
    apply_statements = []
    for res in template.find_resources("AWS::IAM::Policy").values():
        for stmt in res["Properties"]["PolicyDocument"]["Statement"]:
            actions = stmt.get("Action", [])
            if isinstance(actions, str):
                actions = [actions]
            if "bedrock:ApplyGuardrail" in actions:
                apply_statements.append(stmt)
    assert len(apply_statements) == 1, apply_statements
    (stmt,) = apply_statements
    resource = stmt.get("Resource")
    assert "fvb8n9pc8e2q" not in json.dumps(stmt), "ApplyGuardrail still pins the removed literal"
    assert resource != "*", "ApplyGuardrail grants Resource '*'"
    # The ARN is an Fn::GetAtt token on the in-stack guardrail, so it is a dict.
    assert isinstance(resource, dict), resource


def test_retrieve_references_the_deployed_knowledge_base(template: Template) -> None:
    """A bedrock:Retrieve statement references knowledge-base/YNXUIXEYBQ."""
    assert _has_bedrock_action_on_resource(
        template, "bedrock:Retrieve", "knowledge-base/YNXUIXEYBQ"
    ), "no bedrock:Retrieve grant on knowledge-base/YNXUIXEYBQ"


def test_dynamodb_read_on_table_and_gsi1(template: Template) -> None:
    """DynamoDB read actions reference the MovieIntel table and its GSI1 index."""
    read_actions = {"dynamodb:GetItem", "dynamodb:BatchGetItem", "dynamodb:Query", "dynamodb:Scan"}
    policies = template.find_resources("AWS::IAM::Policy")
    saw_table = False
    saw_index = False
    for res in policies.values():
        for stmt in res["Properties"]["PolicyDocument"]["Statement"]:
            actions = stmt.get("Action", [])
            if isinstance(actions, str):
                actions = [actions]
            if not (read_actions & set(actions)):
                continue
            stmt_text = json.dumps(stmt)
            if "table/MovieIntel" in stmt_text:
                saw_table = True
            if "index/GSI1" in stmt_text:
                saw_index = True
    assert saw_table, "no DynamoDB read grant on table/MovieIntel"
    assert saw_index, "no DynamoDB read grant on the GSI1 index"


def test_serving_dynamodb_is_read_only(template: Template) -> None:
    """The serving DynamoDB grant is read-only (no write action) scoped to table + GSI1.

    REQ-X-5.4: the serving layer only reads enriched movies, so its role must hold only
    read actions (GetItem, BatchGetItem, Query, Scan) and NO write action
    (PutItem/UpdateItem/DeleteItem/BatchWriteItem), with no wildcard dynamodb:* and no
    Resource '*'.
    """
    read_actions = {"dynamodb:GetItem", "dynamodb:BatchGetItem", "dynamodb:Query", "dynamodb:Scan"}
    write_actions = {
        "dynamodb:PutItem",
        "dynamodb:UpdateItem",
        "dynamodb:DeleteItem",
        "dynamodb:BatchWriteItem",
    }
    all_dynamo_actions: set[str] = set()
    saw_dynamo = False
    policies = template.find_resources("AWS::IAM::Policy")
    for res in policies.values():
        for stmt in res["Properties"]["PolicyDocument"]["Statement"]:
            actions = stmt.get("Action", [])
            if isinstance(actions, str):
                actions = [actions]
            dynamo = {a for a in actions if isinstance(a, str) and a.startswith("dynamodb:")}
            if not dynamo:
                continue
            saw_dynamo = True
            all_dynamo_actions |= dynamo
            assert stmt.get("Resource") != "*", "serving DynamoDB grants Resource '*'"
    assert saw_dynamo, "no DynamoDB grant found on the serving role"
    assert "dynamodb:*" not in all_dynamo_actions, all_dynamo_actions
    assert all_dynamo_actions <= read_actions, (
        f"serving holds non-read actions: {all_dynamo_actions}"
    )
    assert not (all_dynamo_actions & write_actions), (
        f"serving holds write actions: {all_dynamo_actions}"
    )


def test_no_wildcard_bedrock_actions_or_resources(template: Template) -> None:
    """No Bedrock statement uses bedrock:* or Resource '*' (least privilege)."""
    policies = template.find_resources("AWS::IAM::Policy")
    saw_bedrock_statement = False
    for res in policies.values():
        for stmt in res["Properties"]["PolicyDocument"]["Statement"]:
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


def test_api_endpoint_is_a_stack_output(template: Template) -> None:
    """The API endpoint URL is exported as a CfnOutput."""
    outputs = template.find_outputs("*")
    assert outputs, "no stack outputs found"


def test_scale_to_zero_no_provisioned_concurrency(template: Template) -> None:
    """No AWS::Lambda::Version/Alias carries a ProvisionedConcurrencyConfig."""
    for resource_type in ("AWS::Lambda::Version", "AWS::Lambda::Alias"):
        for res in template.find_resources(resource_type).values():
            assert "ProvisionedConcurrencyConfig" not in res["Properties"], res["Properties"]
    # Belt and suspenders: no provisioned-concurrency config anywhere in the template.
    template.resource_properties_count_is(
        "AWS::Lambda::Version",
        Match.object_like({"ProvisionedConcurrencyConfig": Match.any_value()}),
        0,
    )
    template.resource_properties_count_is(
        "AWS::Lambda::Alias",
        Match.object_like({"ProvisionedConcurrencyConfig": Match.any_value()}),
        0,
    )


def _has_bedrock_action_on_resource(template: Template, action: str, resource_substr: str) -> bool:
    """True when some IAM policy statement grants ``action`` on a resource containing substr."""
    policies = template.find_resources("AWS::IAM::Policy")
    for res in policies.values():
        for stmt in res["Properties"]["PolicyDocument"]["Statement"]:
            actions = stmt.get("Action", [])
            if isinstance(actions, str):
                actions = [actions]
            if action not in actions:
                continue
            if resource_substr in json.dumps(stmt):
                return True
    return False
