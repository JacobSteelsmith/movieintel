"""Synth/assertion tests for the serving stack (FEAT-002, REQ-B-4.1, REQ-X-6.1).

These synthesize ``MovieIntelServingStack`` into a CloudFormation template with
``aws_cdk.assertions.Template`` and assert the contract the stack must satisfy:

* three python3.12 Lambdas - the synchronous serve handler plus the async submit and worker
  handlers (FEAT-003); the worker is 300s/1024MB, the submit is 10s/256MB, and neither async
  function attaches a ``_sqlite`` layer (both attach the shared layer);
* an API Gateway integrated with the serve Lambda (HTTP API ``AWS::ApiGatewayV2::*`` or a REST
  ``AWS::ApiGateway::RestApi`` proxy fallback - the assertion tolerates either flavor), now
  carrying FOUR routes (POST /query, GET /health, POST /jobs, GET /jobs/{id});
* per-role least-privilege IAM: the SubmitFn role writes/reads the job item on the table ARN
  only (no GSI1, no Query/Scan/BatchGetItem, no Bedrock) and invokes the worker; the WorkerFn
  role mirrors the serving Bedrock/KB/DynamoDB read surface plus the job-write actions, with no
  ``bedrock:*`` and no Resource ``*``;
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
from typing import Any

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


def test_three_lambdas_with_expected_handler_strings(template: Template) -> None:
    """Three python3.12 Lambdas: the sync serve + the async submit and worker handlers.

    The async submit-then-poll path (FEAT-003) adds SubmitFn and WorkerFn alongside the
    existing ServingFn, so the stack now carries exactly three python3.12 functions whose
    Handler strings are the serve, submit, and worker handlers.
    """
    props = _serving_functions(template)
    assert len(props) == 3, f"expected exactly three python3.12 Lambdas, found {len(props)}"
    handlers = {p["Handler"] for p in props}
    assert handlers == {
        "handlers.serve.handler.handler",
        "handlers.submit.handler.handler",
        "handlers.worker.handler.handler",
    }, handlers


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
        template.resource_count_is("AWS::ApiGatewayV2::Route", 4)
        routes = template.find_resources("AWS::ApiGatewayV2::Route")
        route_keys = {res["Properties"].get("RouteKey") for res in routes.values()}
        assert route_keys == {
            "POST /query",
            "GET /health",
            "POST /jobs",
            "GET /jobs/{id}",
        }, route_keys
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
    serve = [
        p for p in _serving_functions(template) if p["Handler"] == "handlers.serve.handler.handler"
    ]
    assert len(serve) == 1, serve
    props = serve[0]
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
    # ServingFn AND WorkerFn each hold one ApplyGuardrail statement on the in-stack
    # guardrail, so there are now two - both must resolve the ARN via a synth token and
    # neither may pin the removed literal or a wildcard.
    assert len(apply_statements) == 2, apply_statements
    for stmt in apply_statements:
        resource = stmt.get("Resource")
        assert "fvb8n9pc8e2q" not in json.dumps(stmt), (
            "ApplyGuardrail still pins the removed literal"
        )
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
    """The ServingFn DynamoDB grant is read-only (no write action) scoped to table + GSI1.

    REQ-X-5.4: the SYNCHRONOUS serving layer only reads enriched movies, so the ServingFn
    role must hold only read actions (GetItem, BatchGetItem, Query, Scan) and NO write
    action (PutItem/UpdateItem/DeleteItem/BatchWriteItem). The async SubmitFn/WorkerFn roles
    legitimately hold job-write actions now, so this check is scoped to the ServingFn role,
    identified by its bedrock:Retrieve + DynamoDB-read fingerprint (SubmitFn has no Bedrock
    grant; WorkerFn's role carries bedrock:InvokeModel/ApplyGuardrail AND job writes).
    """
    read_actions = {"dynamodb:GetItem", "dynamodb:BatchGetItem", "dynamodb:Query", "dynamodb:Scan"}
    write_actions = {
        "dynamodb:PutItem",
        "dynamodb:UpdateItem",
        "dynamodb:DeleteItem",
        "dynamodb:BatchWriteItem",
    }
    policies = template.find_resources("AWS::IAM::Policy")
    serving_doc = None
    for res in policies.values():
        statements = res["Properties"]["PolicyDocument"]["Statement"]
        doc_text = json.dumps(statements)
        # The ServingFn role is the one carrying bedrock:Retrieve AND a DynamoDB read but
        # NO Bedrock InvokeModel/ApplyGuardrail is not a reliable discriminator (ServingFn
        # DOES hold InvokeModel); the WorkerFn role is the one that ALSO holds DynamoDB
        # write actions. ServingFn is the Bedrock+DynamoDB role whose DynamoDB actions are
        # all reads. Pick the role with bedrock:Retrieve whose DynamoDB actions are a
        # read-only subset.
        if "bedrock:Retrieve" not in doc_text:
            continue
        dynamo_here: set[str] = set()
        for stmt in statements:
            actions = stmt.get("Action", [])
            if isinstance(actions, str):
                actions = [actions]
            dynamo_here |= {a for a in actions if isinstance(a, str) and a.startswith("dynamodb:")}
        if dynamo_here and dynamo_here <= read_actions:
            serving_doc = statements
            break
    assert serving_doc is not None, "could not locate the read-only ServingFn DynamoDB role"

    all_dynamo_actions: set[str] = set()
    for stmt in serving_doc:
        actions = stmt.get("Action", [])
        if isinstance(actions, str):
            actions = [actions]
        dynamo = {a for a in actions if isinstance(a, str) and a.startswith("dynamodb:")}
        if not dynamo:
            continue
        all_dynamo_actions |= dynamo
        assert stmt.get("Resource") != "*", "serving DynamoDB grants Resource '*'"
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


def _function_by_handler(template: Template, handler: str) -> dict[str, Any]:
    """Return the single python3.12 Lambda Properties whose Handler matches ``handler``."""
    matches = [p for p in _serving_functions(template) if p.get("Handler") == handler]
    assert len(matches) == 1, f"expected one {handler} Lambda, found {len(matches)}"
    return matches[0]


def _policy_statements(template: Template) -> list[Any]:
    """Return each IAM policy's Statement list."""
    return [
        res["Properties"]["PolicyDocument"]["Statement"]
        for res in template.find_resources("AWS::IAM::Policy").values()
    ]


def test_submit_and_worker_function_sizing_and_layers(template: Template) -> None:
    """SubmitFn (10s/256MB) and WorkerFn (300s/1024MB) attach the shared layer, not _sqlite.

    Both async functions ship the ``handlers`` asset with the SharedPackageLayer and NO
    ``_sqlite`` layer (they never touch the source DBs). The worker is agent-loop sized
    (300s timeout, 1024MB); the submit is small (10s, 256MB) because it only validates +
    writes + async-invokes.
    """
    functions = template.find_resources("AWS::Lambda::Function")
    # Only one layer (the shared package layer) exists in this stack; capture its logical id.
    layers = template.find_resources("AWS::Lambda::LayerVersion")
    assert len(layers) == 1, f"expected exactly one Lambda layer (shared), found {len(layers)}"
    (shared_layer_id,) = layers.keys()

    worker = _function_by_handler(template, "handlers.worker.handler.handler")
    assert worker["Timeout"] == 300, worker
    assert worker["MemorySize"] == 1024, worker

    submit = _function_by_handler(template, "handlers.submit.handler.handler")
    assert submit["Timeout"] == 10, submit
    assert submit["MemorySize"] == 256, submit

    # Neither async function references a _sqlite layer; both reference the shared layer.
    # The template renders Layers entries as {"Ref": "<layerLogicalId>"}.
    for props in (worker, submit):
        layer_refs = props.get("Layers", [])
        assert isinstance(layer_refs, list) and layer_refs, props
        ref_ids = {ref.get("Ref") for ref in layer_refs if isinstance(ref, dict)}
        assert shared_layer_id in ref_ids, (ref_ids, shared_layer_id)
        # No layer logical id in this stack mentions sqlite; the only layer is the shared one.
        assert all("qlite" not in str(rid).lower() for rid in ref_ids), ref_ids
    # Belt and suspenders: the stack defines no sqlite layer at all.
    assert all(
        "qlite" not in name.lower()
        for name in functions  # function logical ids
    ), functions
    assert all("qlite" not in lid.lower() for lid in layers), layers


def test_jobs_routes_present_integrated_to_submit(template: Template) -> None:
    """POST /jobs and GET /jobs/{id} routes exist on the HTTP API alongside the sync routes.

    The async routes share one HttpLambdaIntegration (SubmitIntegration) to the SubmitFn,
    mirroring how /query + /health share the ServingFn integration. The existing
    POST /query and GET /health routes are retained (four routes total).
    """
    template.resource_count_is("AWS::ApiGatewayV2::Route", 4)
    routes = template.find_resources("AWS::ApiGatewayV2::Route")
    route_keys = {res["Properties"].get("RouteKey") for res in routes.values()}
    assert route_keys == {
        "POST /query",
        "GET /health",
        "POST /jobs",
        "GET /jobs/{id}",
    }, route_keys

    # The two job routes must target the SAME integration (the submit integration), and
    # that target must differ from the serve integration used by /query + /health.
    def _target(route_key: str) -> object:
        for res in routes.values():
            if res["Properties"].get("RouteKey") == route_key:
                return json.dumps(res["Properties"].get("Target"))
        raise AssertionError(f"route {route_key} not found")

    jobs_target = _target("POST /jobs")
    poll_target = _target("GET /jobs/{id}")
    query_target = _target("POST /query")
    assert jobs_target == poll_target, (jobs_target, poll_target)
    assert jobs_target != query_target, (jobs_target, query_target)


def test_submit_role_scoped_to_job_item_and_worker_invoke(template: Template) -> None:
    """The SubmitFn role writes/reads the job item on the table ARN only and invokes worker.

    S1: dynamodb PutItem/UpdateItem/GetItem scoped to the base table ARN with NO GSI1
    resource and NO Query/Scan/BatchGetItem action and NO bedrock:* action. S2:
    lambda:InvokeFunction scoped to the worker function. The SubmitFn role is identified as
    the DynamoDB role that holds a lambda:InvokeFunction grant and NO Bedrock action.
    """
    submit_doc = None
    for statements in _policy_statements(template):
        doc_text = json.dumps(statements)
        has_invoke = "lambda:InvokeFunction" in doc_text
        has_dynamo = "dynamodb:" in doc_text
        has_bedrock = "bedrock:" in doc_text
        if has_invoke and has_dynamo and not has_bedrock:
            submit_doc = statements
            break
    assert submit_doc is not None, "could not locate the SubmitFn role policy"

    doc_text = json.dumps(submit_doc)
    assert "bedrock:" not in doc_text, "SubmitFn holds a Bedrock grant"
    assert "index/GSI1" not in doc_text, "SubmitFn is scoped to GSI1"
    for forbidden in ("dynamodb:Query", "dynamodb:Scan", "dynamodb:BatchGetItem"):
        assert forbidden not in doc_text, f"SubmitFn holds {forbidden}"

    # The DynamoDB statement grants exactly the three job-item actions on the table ARN.
    dynamo_actions: set[str] = set()
    saw_invoke = False
    for stmt in submit_doc:
        actions = stmt.get("Action", [])
        if isinstance(actions, str):
            actions = [actions]
        dynamo = {a for a in actions if isinstance(a, str) and a.startswith("dynamodb:")}
        if dynamo:
            dynamo_actions |= dynamo
            stmt_text = json.dumps(stmt)
            assert "table/MovieIntel" in stmt_text, stmt_text
            assert "index/GSI1" not in stmt_text, stmt_text
            assert stmt.get("Resource") != "*", stmt
        if "lambda:InvokeFunction" in actions:
            saw_invoke = True
            assert stmt.get("Resource") != "*", "SubmitFn lambda invoke grants Resource '*'"
    assert dynamo_actions == {
        "dynamodb:PutItem",
        "dynamodb:UpdateItem",
        "dynamodb:GetItem",
    }, dynamo_actions
    assert saw_invoke, "SubmitFn has no lambda:InvokeFunction grant"


def test_worker_role_has_bedrock_kb_dynamo_read_and_job_writes(template: Template) -> None:
    """The WorkerFn role mirrors the serving Bedrock/KB/DynamoDB reads plus job writes.

    It holds bedrock:InvokeModel, bedrock:ApplyGuardrail, bedrock:Retrieve, the DynamoDB
    tool-read actions on table+GSI1, AND the DynamoDB job-write actions on the table. No
    statement uses Resource '*' and no action is a bedrock:* wildcard. The WorkerFn role is
    the one that holds BOTH Bedrock actions AND a DynamoDB write action.
    """
    worker_doc = None
    for statements in _policy_statements(template):
        doc_text = json.dumps(statements)
        has_bedrock = "bedrock:InvokeModel" in doc_text
        has_dynamo_write = "dynamodb:PutItem" in doc_text or "dynamodb:UpdateItem" in doc_text
        if has_bedrock and has_dynamo_write:
            worker_doc = statements
            break
    assert worker_doc is not None, "could not locate the WorkerFn role policy"

    all_actions: set[str] = set()
    for stmt in worker_doc:
        actions = stmt.get("Action", [])
        if isinstance(actions, str):
            actions = [actions]
        all_actions |= {a for a in actions if isinstance(a, str)}
        resource = stmt.get("Resource")
        assert resource != "*", "WorkerFn statement grants Resource '*'"
        if isinstance(resource, list):
            assert "*" not in resource, "WorkerFn statement grants Resource '*'"

    assert "bedrock:*" not in all_actions, all_actions
    for required in (
        "bedrock:InvokeModel",
        "bedrock:ApplyGuardrail",
        "bedrock:Retrieve",
        "dynamodb:GetItem",
        "dynamodb:BatchGetItem",
        "dynamodb:Query",
        "dynamodb:Scan",
        "dynamodb:PutItem",
        "dynamodb:UpdateItem",
    ):
        assert required in all_actions, (required, all_actions)

    doc_text = json.dumps(worker_doc)
    assert "knowledge-base/YNXUIXEYBQ" in doc_text, "WorkerFn missing KB retrieve resource"
    assert "table/MovieIntel" in doc_text, "WorkerFn missing table resource"
    assert "index/GSI1" in doc_text, "WorkerFn missing GSI1 read resource"


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
