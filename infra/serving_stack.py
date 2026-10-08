"""CDK stack serving the Task 9 agent loop behind an HTTP API (Task 10, REQ-B-4, REQ-X-6.1).

This stack provisions the serving layer (Subsystem B, design §3.8/§3.10): an HTTP API
Gateway with a POST /query route proxied to a single serving Lambda that composes
``movieintel.agent.loop.run_agent`` via the ``handlers.serve.handler.handler`` wrapper
(FEAT-001). It is deployed against ALREADY-DEPLOYED pipeline/KB resources, so the KB and
table/GSI ids are pinned as env-var literals (KB ``YNXUIXEYBQ``, table ``MovieIntel``,
GSI ``GSI1``) rather than cross-stack references.

The SERVING Guardrail (``movieintel-serving-guardrail``) is defined IN this stack, so the
serving Lambda references its id/arn in-stack (no cross-stack reference, no deadly
embrace). It carries the SAME harmful-content filters, denied topic, and PII policy as the
enrichment guardrail (shared via the ``constants`` helpers) PLUS PROMPT_ATTACK on input,
because serving takes untrusted free-text ``/query`` input. Enrichment, a trusted batch
path, deliberately omits PROMPT_ATTACK; the two guardrails differ ONLY by that filter.

Packaging reuses the pipeline's handler-asset layout and the SharedPackageLayer
(``movieintel`` + pydantic under ``/opt/python``). The serving Lambda does NOT attach the
``_sqlite`` layer - it never touches the source DBs. The Lambda scales to zero: no
provisioned concurrency (REQ-B-4.5, REQ-X-5.1).

IAM is least-privilege and reuses the pipeline's verified inference-profile pattern (via
:func:`constants.bedrock_invoke_model_resources`): bedrock:InvokeModel on the Sonnet 4.5
inference-profile ARN AND the three cross-region foundation-model ARNs,
bedrock:ApplyGuardrail on the in-stack serving-guardrail ARN, bedrock:Retrieve on the KB
ARN, and DynamoDB read on the MovieIntel table + GSI1. No ``bedrock:*``, no Resource
``*``. AWS steering: hyphens, never em dashes, in names and descriptions.
"""

from __future__ import annotations

from aws_cdk import (
    CfnOutput,
    Duration,
    Stack,
)
from aws_cdk import (
    aws_apigatewayv2 as apigwv2,
)
from aws_cdk import (
    aws_apigatewayv2_integrations as apigwv2_integrations,
)
from aws_cdk import (
    aws_bedrock as bedrock,
)
from aws_cdk import (
    aws_iam as iam,
)
from aws_cdk import (
    aws_lambda as lambda_,
)
from aws_cdk import (
    aws_wafv2 as wafv2,
)
from constants import (
    REPO_ROOT,
    bedrock_invoke_model_resources,
    guardrail_content_filters,
    guardrail_denied_topics,
    guardrail_pii_entities,
    shared_layer_bundling,
)
from constructs import Construct
from pipeline_stack import HANDLER_ASSET_EXCLUDES, INFRA_DIR

from movieintel.config import BedrockConfig
from movieintel.persistence.config import PersistenceConfig

#: Logical name of the serving Bedrock Guardrail (hyphens only, no em dashes). The serving
#: stack now defines its OWN guardrail in-stack (rather than pinning the previously-shared
#: deployed id), so the serving Lambda references the resource's id/arn directly.
SERVING_GUARDRAIL_NAME = "movieintel-serving-guardrail"

#: The deployed Bedrock Knowledge Base id the serving Lambda retrieves from (the env var
#: ``KBConfig`` reads as ``MOVIEINTEL_KB_ID``). Pinned for the same reason as above.
DEPLOYED_KNOWLEDGE_BASE_ID = "YNXUIXEYBQ"

#: Route the serving API exposes. Lambda-proxy integration passes the handler's
#: statusCode/headers/body straight through (REQ-B-4.1/.4).
QUERY_ROUTE_PATH = "/query"

#: Warmup route the serving API exposes alongside POST /query. GET /health is served by
#: the SAME ServingFn (so hitting it warms the real serving container) and short-circuits
#: inside the handler before the agent loop.
HEALTH_ROUTE_PATH = "/health"

#: Async submit route (POST /jobs) - creates a job, async-invokes the worker, returns 202.
#: Both /jobs routes are served by the SAME SubmitFn (mirroring how ServingFn serves both
#: /query and /health).
JOBS_ROUTE_PATH = "/jobs"

#: Async poll route (GET /jobs/{id}) - a single DynamoDB GetItem. ``{id}`` is a standard
#: HTTP API v2 path variable the handler reads from ``event['pathParameters']['id']``.
JOB_POLL_ROUTE_PATH = "/jobs/{id}"

#: CDK context key + default for the CORS allow-origin on the HTTP API's preflight.
CORS_ALLOW_ORIGIN_CONTEXT_KEY = "cors_allow_origin"
# design 2.5: '*' is a first-deploy placeholder. A wildcard origin is incompatible with
# credentialed (cookie/Authorization) requests, so once FEAT-003 deploys the CloudFront
# distribution, pin this via `--context cors_allow_origin=https://<domain>` to the exact
# CloudFront domain. No origin-string validation is performed here by design - the context
# value is passed through verbatim.
DEFAULT_CORS_ALLOW_ORIGIN = "*"

#: CDK context key gating the WAF WebACL association. The WebACL is defined unconditionally;
#: the association to the API stage is opt-in (default off) so synth/deploy stay decoupled.
WAF_ASSOCIATE_CONTEXT_KEY = "associate_waf"


class MovieIntelServingStack(Stack):
    """API Gateway -> serving Lambda invoking the agent loop over the deployed resources."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs: object) -> None:
        super().__init__(scope, construct_id, **kwargs)  # type: ignore[arg-type]

        self.bedrock = BedrockConfig.from_env()
        self.persistence = PersistenceConfig.from_env()

        self.guardrail = self._guardrail()
        self.shared_layer = self._shared_layer()
        self.serving_function = self._serving_lambda()
        self._grant_iam()
        # Async submit-then-poll path (design Decision 1/3): build the worker FIRST so the
        # submit function can reference its name/ARN for the Event-type invoke.
        self.worker_function = self._worker_lambda()
        self.submit_function = self._submit_lambda()
        self._grant_async_iam()
        self.api = self._http_api()
        self.web_acl = self._waf()
        self._outputs()

    # -- guardrail ------------------------------------------------------------

    def _guardrail(self) -> bedrock.CfnGuardrail:
        """Create the SERVING Guardrail. Hyphens only, no em dashes.

        Serving takes untrusted free-text ``/query`` input, so it KEEPS PROMPT_ATTACK on
        input (``include_prompt_attack=True``; input strength HIGH, output strength NONE).
        It shares the harmful-content filters, denied topic, and PII policy with the
        enrichment guardrail via the ``constants`` helpers, so the two guardrails differ
        ONLY by PROMPT_ATTACK.
        """
        return bedrock.CfnGuardrail(
            self,
            "ServingGuardrail",
            name=SERVING_GUARDRAIL_NAME,
            description=(
                "Guardrail for the movieintel serving agent - blocks unsafe content, "
                "denies non-movie advice, anonymizes PII, and filters prompt-injection "
                "on untrusted user input."
            ),
            blocked_input_messaging="Request blocked by the movieintel serving guardrail.",
            blocked_outputs_messaging="Response blocked by the movieintel serving guardrail.",
            content_policy_config=bedrock.CfnGuardrail.ContentPolicyConfigProperty(
                filters_config=guardrail_content_filters(include_prompt_attack=True)
            ),
            topic_policy_config=bedrock.CfnGuardrail.TopicPolicyConfigProperty(
                topics_config=guardrail_denied_topics()
            ),
            sensitive_information_policy_config=(
                bedrock.CfnGuardrail.SensitiveInformationPolicyConfigProperty(
                    pii_entities_config=guardrail_pii_entities()
                )
            ),
        )

    # -- lambda layer ---------------------------------------------------------

    def _shared_layer(self) -> lambda_.LayerVersion:
        """Build the SharedPackageLayer (movieintel + pydantic under /opt/python).

        Identical bundling to the pipeline stack - point the asset at ``src/`` and stage
        it into a ``python/`` subtree so the package lands on the Lambda import path. The
        serving Lambda needs NO ``_sqlite`` layer.
        """
        return lambda_.LayerVersion(
            self,
            "SharedPackageLayer",
            code=lambda_.Code.from_asset(
                str(REPO_ROOT / "src"),
                bundling=shared_layer_bundling(),
            ),
            compatible_runtimes=[lambda_.Runtime.PYTHON_3_12],
            description="movieintel package and pinned pydantic for the serving Lambda.",
        )

    # -- lambda ---------------------------------------------------------------

    def _environment(self) -> dict[str, str]:
        """Env for the serving Lambda.

        BEDROCK_MODEL_ID (the Sonnet 4.5 inference profile) comes from ``BedrockConfig``;
        table/GSI names come from ``PersistenceConfig``. The guardrail id/version point at
        the in-stack serving guardrail (CDK owns it, mirroring how the pipeline env uses
        ``self.guardrail.attr_version``). The KB id stays pinned as a literal because the
        serving stack still runs against an already-deployed Knowledge Base.
        """
        return {
            "BEDROCK_MODEL_ID": self.bedrock.model_id,
            "BEDROCK_GUARDRAIL_ID": self.guardrail.attr_guardrail_id,
            "BEDROCK_GUARDRAIL_VERSION": self.guardrail.attr_version,
            "MOVIEINTEL_KB_ID": DEPLOYED_KNOWLEDGE_BASE_ID,
            "MOVIEINTEL_TABLE_NAME": self.persistence.table_name,
            "MOVIEINTEL_GSI1_NAME": self.persistence.gsi1_name,
        }

    def _serving_lambda(self) -> lambda_.Function:
        """One serving Lambda (PYTHON_3_12, shared layer, scale-to-zero, agent-loop sized).

        No provisioned concurrency, so the function scales to zero when idle
        (REQ-B-4.5, REQ-X-5.1). The timeout and memory are sized for an agent loop that
        makes several Converse + Retrieve + DynamoDB round trips per request.
        """
        return lambda_.Function(
            self,
            "ServingFn",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handlers.serve.handler.handler",
            code=lambda_.Code.from_asset(
                str(INFRA_DIR),
                exclude=HANDLER_ASSET_EXCLUDES,
            ),
            layers=[self.shared_layer],
            environment=self._environment(),
            timeout=Duration.minutes(5),
            memory_size=1024,
        )

    def _worker_lambda(self) -> lambda_.Function:
        """The async worker Lambda (PYTHON_3_12, shared layer, agent-loop sized).

        Runs ``run_agent`` to completion and writes real per-phase progress + the terminal
        job record. It carries the full serving env (model/guardrail/KB/table) and the same
        300s timeout / 1024MB sizing as ``ServingFn``. It is NOT wired to any API route - it
        is invoked only asynchronously (InvocationType='Event') by ``SubmitFn``. No
        ``_sqlite`` layer (it never touches the source DBs); scales to zero.
        """
        return lambda_.Function(
            self,
            "WorkerFn",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handlers.worker.handler.handler",
            code=lambda_.Code.from_asset(
                str(INFRA_DIR),
                exclude=HANDLER_ASSET_EXCLUDES,
            ),
            layers=[self.shared_layer],
            environment=self._environment(),
            timeout=Duration.minutes(5),
            memory_size=1024,
        )

    def _submit_lambda(self) -> lambda_.Function:
        """The fast submit/poll Lambda (PYTHON_3_12, shared layer, scale-to-zero).

        Handles ``POST /jobs`` (validate + write a queued record + async-invoke the worker,
        202) and ``GET /jobs/{id}`` (single GetItem poll). It never runs the agent loop, so
        it is sized small (10s timeout, 256MB) and gets no Bedrock grant. Its env points at
        the worker (for the Event invoke) and the job table/GSI. The worker MUST exist
        before this runs so ``WORKER_FUNCTION_NAME`` resolves.
        """
        submit = lambda_.Function(
            self,
            "SubmitFn",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handlers.submit.handler.handler",
            code=lambda_.Code.from_asset(
                str(INFRA_DIR),
                exclude=HANDLER_ASSET_EXCLUDES,
            ),
            layers=[self.shared_layer],
            environment={
                "MOVIEINTEL_TABLE_NAME": self.persistence.table_name,
                "MOVIEINTEL_GSI1_NAME": self.persistence.gsi1_name,
            },
            timeout=Duration.seconds(10),
            memory_size=256,
        )
        submit.add_environment("WORKER_FUNCTION_NAME", self.worker_function.function_name)
        return submit

    # -- IAM ------------------------------------------------------------------

    def _grant_iam(self) -> None:
        """Least-privilege IAM for the serving role (mirrors the pipeline's grants).

        The Bedrock-invoke ARN list is built by the shared helper in constants.py so the
        serving grant matches the pipeline's verified inference-profile pattern exactly.
        """
        region = self.region
        account = self.account

        self.serving_function.add_to_role_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["bedrock:InvokeModel"],
                resources=bedrock_invoke_model_resources(region, account),
            )
        )
        self.serving_function.add_to_role_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["bedrock:ApplyGuardrail"],
                resources=[self.guardrail.attr_guardrail_arn],
            )
        )
        self.serving_function.add_to_role_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["bedrock:Retrieve"],
                resources=[
                    f"arn:aws:bedrock:{region}:{account}:knowledge-base/{DEPLOYED_KNOWLEDGE_BASE_ID}"
                ],
            )
        )

        table_arn = f"arn:aws:dynamodb:{region}:{account}:table/{self.persistence.table_name}"
        index_arn = f"{table_arn}/index/{self.persistence.gsi1_name}"
        self.serving_function.add_to_role_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    "dynamodb:GetItem",
                    "dynamodb:BatchGetItem",
                    "dynamodb:Query",
                    "dynamodb:Scan",
                ],
                resources=[table_arn, index_arn],
            )
        )

    def _grant_async_iam(self) -> None:
        """Least-privilege IAM for the async submit + worker roles (design 'IAM' section).

        SubmitFn is the fast path: it only writes/reads a single job item by PK/SK and
        async-invokes the worker, so it gets NO Bedrock grant, NO GSI1, and NO
        Query/Scan/BatchGetItem. WorkerFn mirrors the ServingFn Bedrock/KB/DynamoDB tool
        surface plus the job-write actions. No ``bedrock:*`` and no Resource ``*`` anywhere.
        """
        region = self.region
        account = self.account
        table_arn = f"arn:aws:dynamodb:{region}:{account}:table/{self.persistence.table_name}"
        index_arn = f"{table_arn}/index/{self.persistence.gsi1_name}"

        # -- SubmitFn ---------------------------------------------------------
        # S1: job keyspace writes/reads on the base table ARN ONLY (no GSI1, no
        # Query/Scan/BatchGetItem) - the submit path touches a single job item by PK/SK.
        self.submit_function.add_to_role_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:GetItem"],
                resources=[table_arn],
            )
        )
        # S2: dispatch the worker. grant_invoke scopes lambda:InvokeFunction to the worker
        # function ARN (least privilege).
        self.worker_function.grant_invoke(self.submit_function)

        # -- WorkerFn ---------------------------------------------------------
        # W1: Bedrock model (identical to the ServingFn grant).
        self.worker_function.add_to_role_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["bedrock:InvokeModel"],
                resources=bedrock_invoke_model_resources(region, account),
            )
        )
        # W2: guardrail on the in-stack serving guardrail ARN.
        self.worker_function.add_to_role_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["bedrock:ApplyGuardrail"],
                resources=[self.guardrail.attr_guardrail_arn],
            )
        )
        # W3: KB retrieve on the deployed knowledge base.
        self.worker_function.add_to_role_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["bedrock:Retrieve"],
                resources=[
                    f"arn:aws:bedrock:{region}:{account}:knowledge-base/{DEPLOYED_KNOWLEDGE_BASE_ID}"
                ],
            )
        )
        # W4: DynamoDB tool reads on the table + GSI1 (identical to the ServingFn grant).
        self.worker_function.add_to_role_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    "dynamodb:GetItem",
                    "dynamodb:BatchGetItem",
                    "dynamodb:Query",
                    "dynamodb:Scan",
                ],
                resources=[table_arn, index_arn],
            )
        )
        # W5: DynamoDB job writes on the base table ARN only. The GetItem here overlaps W4's
        # GetItem - acceptable; the two statements have different resource scopes.
        self.worker_function.add_to_role_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:GetItem"],
                resources=[table_arn],
            )
        )

    # -- API ------------------------------------------------------------------

    def _cors_allow_origin(self) -> str:
        """Resolve the CORS allow-origin from CDK context, defaulting to the placeholder.

        Reads the ``cors_allow_origin`` context value (set to the CloudFront domain after
        FEAT-003 deploys) and falls back to :data:`DEFAULT_CORS_ALLOW_ORIGIN`.
        """
        origin = self.node.try_get_context(CORS_ALLOW_ORIGIN_CONTEXT_KEY)
        return origin if isinstance(origin, str) and origin else DEFAULT_CORS_ALLOW_ORIGIN

    def _http_api(self) -> apigwv2.HttpApi:
        """HTTP API with POST /query and GET /health routes proxied to the serving Lambda.

        Lambda-proxy (AWS_PROXY) integration passes the handler's statusCode/headers/body
        through unchanged (REQ-B-4.1), so the structured 200/400 responses the handler
        builds reach the caller verbatim. GET /health is attached to the SAME integration
        so the warmup ping warms the real serving container. The browser front end is
        cross-origin, so the API carries a CORS preflight config.
        """
        api = apigwv2.HttpApi(
            self,
            "ServingHttpApi",
            description="movieintel serving API - POST /query and GET /health over the agent loop.",
            cors_preflight=apigwv2.CorsPreflightOptions(
                allow_origins=[self._cors_allow_origin()],
                allow_methods=[
                    apigwv2.CorsHttpMethod.GET,
                    apigwv2.CorsHttpMethod.POST,
                    apigwv2.CorsHttpMethod.OPTIONS,
                ],
                allow_headers=["content-type", "authorization"],
            ),
        )
        integration = apigwv2_integrations.HttpLambdaIntegration(
            "ServingIntegration",
            handler=self.serving_function,
        )
        api.add_routes(
            path=QUERY_ROUTE_PATH,
            methods=[apigwv2.HttpMethod.POST],
            integration=integration,
        )
        api.add_routes(
            path=HEALTH_ROUTE_PATH,
            methods=[apigwv2.HttpMethod.GET],
            integration=integration,
        )

        # Async submit-then-poll routes on the SAME HTTP API (design Decision 1). Both go
        # to one SubmitFn integration, mirroring how ServingFn serves /query + /health.
        # CORS/WAF inherit at the API level, so no per-route change is needed.
        submit_integration = apigwv2_integrations.HttpLambdaIntegration(
            "SubmitIntegration",
            handler=self.submit_function,
        )
        api.add_routes(
            path=JOBS_ROUTE_PATH,
            methods=[apigwv2.HttpMethod.POST],
            integration=submit_integration,
        )
        api.add_routes(
            path=JOB_POLL_ROUTE_PATH,
            methods=[apigwv2.HttpMethod.GET],
            integration=submit_integration,
        )
        return api

    # -- WAF ------------------------------------------------------------------

    def _associate_waf(self) -> bool:
        """True when the ``associate_waf`` context flag opts in to the stage association."""
        return bool(self.node.try_get_context(WAF_ASSOCIATE_CONTEXT_KEY))

    def _waf(self) -> wafv2.CfnWebACL:
        """Define a rate-based REGIONAL WebACL and (opt-in) associate it with the API stage.

        The WebACL is created UNCONDITIONALLY so it is always in the template; it allows by
        default and blocks any single IP exceeding 600 requests in the rate window. The
        association to the HTTP API ``$default`` stage is created ONLY when the
        ``associate_waf`` context flag is set, keeping synth decoupled from a live stage.
        """
        web_acl = wafv2.CfnWebACL(
            self,
            "ServingWebAcl",
            name="movieintel-serving-waf",
            scope="REGIONAL",
            default_action=wafv2.CfnWebACL.DefaultActionProperty(
                allow=wafv2.CfnWebACL.AllowActionProperty()
            ),
            visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                cloud_watch_metrics_enabled=True,
                metric_name="movieintel-serving-waf",
                sampled_requests_enabled=True,
            ),
            rules=[
                wafv2.CfnWebACL.RuleProperty(
                    name="per-ip-rate-limit",
                    priority=1,
                    action=wafv2.CfnWebACL.RuleActionProperty(
                        block=wafv2.CfnWebACL.BlockActionProperty()
                    ),
                    statement=wafv2.CfnWebACL.StatementProperty(
                        rate_based_statement=wafv2.CfnWebACL.RateBasedStatementProperty(
                            limit=600,
                            aggregate_key_type="IP",
                        )
                    ),
                    visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                        cloud_watch_metrics_enabled=True,
                        metric_name="movieintel-serving-per-ip-rate-limit",
                        sampled_requests_enabled=True,
                    ),
                )
            ],
        )

        # TODO(waf): the association is deploy-verified rather than synth-asserted against a
        # live stage. Associate it with
        # `npx aws-cdk@2 deploy MovieIntelServingStack --context associate_waf=true` (design
        # 2.6). Never drop the WebACL itself - only the association is flag-gated.
        if self._associate_waf():
            # The HTTP API auto-creates the $default stage, so default_stage is non-None
            # here; assert it for the type checker before reading its name.
            default_stage = self.api.default_stage
            assert default_stage is not None  # noqa: S101
            stage_arn = Stack.of(self).format_arn(
                service="apigateway",
                account="",
                resource="/apis",
                resource_name=f"{self.api.api_id}/stages/{default_stage.stage_name}",
            )
            wafv2.CfnWebACLAssociation(
                self,
                "ServingWebAclAssociation",
                resource_arn=stage_arn,
                web_acl_arn=web_acl.attr_arn,
            )
        return web_acl

    # -- outputs --------------------------------------------------------------

    def _outputs(self) -> None:
        """Export the API endpoint URL (the base URL; POST /query is the serving route)."""
        CfnOutput(
            self,
            "ServingApiUrl",
            value=self.api.api_endpoint,
            description="Base URL of the movieintel serving HTTP API - POST /query to invoke.",
        )
        CfnOutput(
            self,
            "ServingSubmitUrl",
            value=f"{self.api.api_endpoint}{JOBS_ROUTE_PATH}",
            description=(
                "Async submit route - POST /jobs returns 202 + a poll_url; "
                "GET /jobs/{id} polls job status."
            ),
        )
