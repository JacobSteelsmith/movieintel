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
        self.api = self._http_api()
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

    # -- API ------------------------------------------------------------------

    def _http_api(self) -> apigwv2.HttpApi:
        """HTTP API with a POST /query route proxied to the serving Lambda.

        Lambda-proxy (AWS_PROXY) integration passes the handler's statusCode/headers/body
        through unchanged (REQ-B-4.1), so the structured 200/400 responses the handler
        builds reach the caller verbatim.
        """
        api = apigwv2.HttpApi(
            self,
            "ServingHttpApi",
            description="movieintel serving API - POST /query over the agent loop.",
        )
        api.add_routes(
            path=QUERY_ROUTE_PATH,
            methods=[apigwv2.HttpMethod.POST],
            integration=apigwv2_integrations.HttpLambdaIntegration(
                "ServingIntegration",
                handler=self.serving_function,
            ),
        )
        return api

    # -- outputs --------------------------------------------------------------

    def _outputs(self) -> None:
        """Export the API endpoint URL (the base URL; POST /query is the serving route)."""
        CfnOutput(
            self,
            "ServingApiUrl",
            value=self.api.api_endpoint,
            description="Base URL of the movieintel serving HTTP API - POST /query to invoke.",
        )
