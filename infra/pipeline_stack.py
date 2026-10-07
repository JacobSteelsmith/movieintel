"""CDK stack defining the whole movieintel enrichment pipeline (REQ-A-6, REQ-X-6).

The stack wires the FEAT-001 Lambda handlers into a Step Functions state machine and
provisions the backing stores: an on-demand DynamoDB single table (+ GSI1), an S3
Vectors bucket + index built from the native ``aws_s3vectors`` L1 module (isolated in
:meth:`_vector_store`, with a stop-if-unavailable contract and NO OpenSearch
substitution), and a Bedrock Guardrail. The read-only source DBs are packaged as a
Lambda layer from ``_sqlite/`` behind a synth-time stop-if-absent guard.

Resource names, index names, and model ids come from the ``movieintel`` config objects
(``PersistenceConfig`` / ``BedrockConfig``), never inlined. IAM for the Bedrock/S3
Vectors callers is scoped to specific resource ARNs (no ``bedrock:*``, no Resource
``*``). AWS steering: hyphens, never em dashes, in names and descriptions.
"""

from __future__ import annotations

from pathlib import Path

from aws_cdk import (
    Duration,
    RemovalPolicy,
    Stack,
)
from aws_cdk import (
    aws_bedrock as bedrock,
)
from aws_cdk import (
    aws_dynamodb as dynamodb,
)
from aws_cdk import (
    aws_iam as iam,
)
from aws_cdk import (
    aws_lambda as lambda_,
)
from aws_cdk import (
    aws_stepfunctions as sfn,
)
from aws_cdk import (
    aws_stepfunctions_tasks as tasks,
)
from constants import (
    EMBEDDING_DIMENSION,
    REPO_ROOT,
    SQLITE_SOURCE_DIR,
    bedrock_invoke_model_resources,
    check_source_dbs,
    guardrail_content_filters,
    guardrail_denied_topics,
    guardrail_pii_entities,
    import_s3vectors,
    shared_layer_bundling,
    sqlite_layer_bundling,
)
from constructs import Construct

from movieintel.config import BedrockConfig
from movieintel.persistence.config import PersistenceConfig

#: The infra/ directory. It is the Lambda code asset root so the ``handlers`` package
#: deploys as a top-level package (``/var/task/handlers/...``) and the intra-handler
#: ``from handlers.shared import ...`` imports resolve at runtime, not just under pytest's
#: ``pythonpath``. The handler string is therefore ``handlers.<step>.handler.handler``.
INFRA_DIR = Path(__file__).resolve().parent

#: Everything under infra/ that must NOT ship inside the handler code asset: the CDK app
#: and stack modules, the synth output, caches, and the source-DB dir (shipped as its own
#: layer). Keeping the asset to the ``handlers`` package keeps the function bundle small.
HANDLER_ASSET_EXCLUDES = [
    "app.py",
    "pipeline_stack.py",
    "constants.py",
    "cdk.json",
    "cdk.out",
    "README.md",
    "_sqlite",
    "**/__pycache__",
    "**/*.pyc",
]

#: The directory inside the Lambda filesystem where the SQLite layer is mounted.
SQLITE_MOUNT_DIR = "/opt/sqlite"

#: Logical name of the Bedrock Guardrail (hyphens only, no em dashes).
GUARDRAIL_NAME = "movieintel-enrichment-guardrail"


class MovieIntelPipelineStack(Stack):
    """The enrichment pipeline: stores, Guardrail, Lambdas, and the state machine."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs: object) -> None:
        super().__init__(scope, construct_id, **kwargs)  # type: ignore[arg-type]

        self.persistence = PersistenceConfig.from_env()
        self.bedrock = BedrockConfig.from_env()

        self.table = self._dynamodb_table()
        self.vector_bucket_arn, self.vector_index_arn = self._vector_store()
        self.guardrail = self._guardrail()
        self.shared_layer, self.sqlite_layer = self._layers()
        self.functions = self._lambdas()
        self._grant_iam()
        self._state_machine()

    # -- stores ---------------------------------------------------------------

    def _dynamodb_table(self) -> dynamodb.Table:
        """On-demand single table with a GSI1 on GSI1PK/GSI1SK (design §3.4, REQ-A-5)."""
        table = dynamodb.Table(
            self,
            "MovieIntelTable",
            table_name=self.persistence.table_name,
            partition_key=dynamodb.Attribute(name="PK", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="SK", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
        )
        table.add_global_secondary_index(
            index_name=self.persistence.gsi1_name,
            partition_key=dynamodb.Attribute(name="GSI1PK", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="GSI1SK", type=dynamodb.AttributeType.STRING),
            projection_type=dynamodb.ProjectionType.ALL,
        )
        return table

    def _vector_store(self) -> tuple[str, str]:
        """Provision the S3 Vectors bucket + index via the native L1 module.

        Isolated so the only coupling to ``aws_s3vectors`` lives here. If the module is
        unavailable, :func:`import_s3vectors` raises :class:`S3VectorsUnavailableError`
        and synth stops; there is NO OpenSearch substitution. Returns the bucket and
        index ARNs for least-privilege IAM scoping.
        """
        s3vectors = import_s3vectors()
        bucket = s3vectors.CfnVectorBucket(
            self,
            "VectorBucket",
            vector_bucket_name=self.persistence.vector_bucket_name,
        )
        index = s3vectors.CfnIndex(
            self,
            "VectorIndex",
            index_name=self.persistence.vector_index_name,
            data_type="float32",
            dimension=EMBEDDING_DIMENSION,
            distance_metric="cosine",
            vector_bucket_name=self.persistence.vector_bucket_name,
        )
        index.add_dependency(bucket)
        return bucket.attr_vector_bucket_arn, index.attr_index_arn

    def _guardrail(self) -> bedrock.CfnGuardrail:
        """Create the ENRICHMENT Guardrail (REQ-A-4.4). Hyphens only, no em dashes.

        Enrichment is a trusted batch path with no untrusted user input, so it OMITS
        PROMPT_ATTACK (``include_prompt_attack=False``): a PROMPT_ATTACK input filter was
        misclassifying the enrichment prompt's JSON-formatting directives as injection and
        blocked ~98/100 enrichments. It shares the harmful-content filters, denied topic,
        and PII policy with the serving guardrail via the ``constants`` helpers, so the two
        guardrails differ ONLY by PROMPT_ATTACK. The construct id and name are unchanged so
        the logical id / resource identity stay stable across the split.
        """
        return bedrock.CfnGuardrail(
            self,
            "EnrichmentGuardrail",
            name=GUARDRAIL_NAME,
            description=(
                "Guardrail for movieintel LLM enrichment - blocks unsafe content and "
                "anonymizes PII on Converse input and output."
            ),
            blocked_input_messaging="Request blocked by the movieintel enrichment guardrail.",
            blocked_outputs_messaging="Response blocked by the movieintel enrichment guardrail.",
            content_policy_config=bedrock.CfnGuardrail.ContentPolicyConfigProperty(
                filters_config=guardrail_content_filters(include_prompt_attack=False)
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

    # -- lambda layers --------------------------------------------------------

    def _layers(self) -> tuple[lambda_.LayerVersion, lambda_.LayerVersion]:
        """Build the shared-package layer and the SQLite source-DB layer.

        The SQLite layer is guarded by :func:`check_source_dbs` at synth time: if a
        source DB is missing, synth stops with :class:`MissingSourceDatabaseError`.
        """
        shared_layer = lambda_.LayerVersion(
            self,
            "SharedPackageLayer",
            # Point the asset at src/ (which contains movieintel/) and bundle it into a
            # python/ subtree so movieintel and pydantic land on the Lambda import path
            # (/opt/python). A flat copy would mount at /opt/movieintel and fail to import.
            code=lambda_.Code.from_asset(
                str(REPO_ROOT / "src"),
                bundling=shared_layer_bundling(),
            ),
            compatible_runtimes=[lambda_.Runtime.PYTHON_3_12],
            description="movieintel package and pinned pydantic for the handler Lambdas.",
        )

        check_source_dbs(SQLITE_SOURCE_DIR)
        sqlite_layer = lambda_.LayerVersion(
            self,
            "SqliteSourceLayer",
            # Stage the DBs under a sqlite/ subtree so they mount at /opt/sqlite (the
            # Extract handler's SQLITE_DIR). A flat asset would land them at /opt and the
            # handler would not find /opt/sqlite/movies.db.
            code=lambda_.Code.from_asset(
                str(SQLITE_SOURCE_DIR),
                bundling=sqlite_layer_bundling(),
            ),
            compatible_runtimes=[lambda_.Runtime.PYTHON_3_12],
            description="Read-only movies.db and ratings.db source databases.",
        )
        return shared_layer, sqlite_layer

    # -- lambdas --------------------------------------------------------------

    def _base_environment(self) -> dict[str, str]:
        """Env common to the handler Lambdas (config-sourced names, no inline literals)."""
        return {
            "MOVIEINTEL_TABLE_NAME": self.persistence.table_name,
            "MOVIEINTEL_GSI1_NAME": self.persistence.gsi1_name,
            "MOVIEINTEL_VECTOR_BUCKET": self.persistence.vector_bucket_name,
            "MOVIEINTEL_VECTOR_INDEX": self.persistence.vector_index_name,
            "MOVIEINTEL_EMBEDDING_MODEL_ID": self.persistence.embedding_model_id,
            "BEDROCK_MODEL_ID": self.bedrock.model_id,
            "BEDROCK_GUARDRAIL_ID": self.guardrail.attr_guardrail_id,
            "BEDROCK_GUARDRAIL_VERSION": self.guardrail.attr_version,
        }

    def _make_function(
        self, name: str, package: str, *, with_sqlite: bool = False
    ) -> lambda_.Function:
        """Build one handler Lambda (PYTHON_3_12, shared layer, config env)."""
        layers = [self.shared_layer]
        environment = self._base_environment()
        if with_sqlite:
            layers = [self.shared_layer, self.sqlite_layer]
            environment = {**environment, "SQLITE_DIR": SQLITE_MOUNT_DIR}
        return lambda_.Function(
            self,
            name,
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler=f"handlers.{package}.handler.handler",
            code=lambda_.Code.from_asset(
                str(INFRA_DIR),
                exclude=HANDLER_ASSET_EXCLUDES,
            ),
            layers=layers,
            environment=environment,
            timeout=Duration.minutes(5),
            memory_size=512,
        )

    def _lambdas(self) -> dict[str, lambda_.Function]:
        """The six pipeline-step Lambdas plus Evaluate."""
        return {
            "extract": self._make_function("ExtractFn", "extract", with_sqlite=True),
            "compute_pes": self._make_function("ComputePesFn", "compute_pes"),
            "enrich": self._make_function("EnrichFn", "enrich"),
            "validate": self._make_function("ValidateFn", "validate"),
            "persist": self._make_function("PersistFn", "persist"),
            "embed_vector": self._make_function("EmbedVectorFn", "embed_vector"),
            "evaluate": self._make_function("EvaluateFn", "evaluate"),
        }

    def _grant_iam(self) -> None:
        """Least-privilege IAM: scope Bedrock/S3Vectors actions to specific ARNs.

        The persist handler only PutItems (idempotent upsert), so it gets write-only
        DynamoDB access, not read-write. The embed_vector handler never touches DynamoDB
        (it only embeds + upserts vectors), so it gets NO DynamoDB grant at all - the
        previous grant_read_write_data on it was over-broad and has been removed
        (REQ-X-5.4).
        """
        self.table.grant_write_data(self.functions["persist"])

        # Invoking a cross-region inference profile needs bedrock:InvokeModel on BOTH the
        # profile ARN (account-scoped) AND each underlying foundation-model ARN the
        # profile routes to (account-less, one per target region). Granting only the
        # profile ARN - or a single ::foundation-model ARN - yields runtime AccessDenied.
        # The ARN list is built by the shared helper in constants.py so the serving stack
        # reuses the exact same inference-profile grant.
        invoke_model_resources = bedrock_invoke_model_resources(self.region, self.account)
        titan_arn = (
            f"arn:aws:bedrock:{self.region}::foundation-model/{self.persistence.embedding_model_id}"
        )
        guardrail_arn = self.guardrail.attr_guardrail_arn

        self.functions["enrich"].add_to_role_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["bedrock:InvokeModel"],
                resources=invoke_model_resources,
            )
        )
        self.functions["enrich"].add_to_role_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["bedrock:ApplyGuardrail"],
                resources=[guardrail_arn],
            )
        )
        self.functions["embed_vector"].add_to_role_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["bedrock:InvokeModel"],
                resources=[titan_arn],
            )
        )
        self.functions["embed_vector"].add_to_role_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["s3vectors:PutVectors"],
                resources=[self.vector_index_arn],
            )
        )

    # -- state machine --------------------------------------------------------

    def _state_machine(self) -> sfn.StateMachine:
        """Extract -> Map[per-item chain with Catch] -> Evaluate -> Done (REQ-A-6)."""
        extract = tasks.LambdaInvoke(
            self,
            "Extract",
            lambda_function=self.functions["extract"],
            payload_response_only=True,
        )

        compute_pes = tasks.LambdaInvoke(
            self,
            "ComputePES",
            lambda_function=self.functions["compute_pes"],
            payload_response_only=True,
        )
        enrich = tasks.LambdaInvoke(
            self,
            "Enrich",
            lambda_function=self.functions["enrich"],
            payload_response_only=True,
        )
        validate = tasks.LambdaInvoke(
            self,
            "Validate",
            lambda_function=self.functions["validate"],
            payload_response_only=True,
        )
        persist = tasks.LambdaInvoke(
            self,
            "PersistDynamoDB",
            lambda_function=self.functions["persist"],
            payload_response_only=True,
        )
        embed = tasks.LambdaInvoke(
            self,
            "EmbedAndUpsertVector",
            lambda_function=self.functions["embed_vector"],
            payload_response_only=True,
        )

        # A caught per-item failure routes to a Pass that records a skip-shaped item so
        # Evaluate still receives one entry per movie (REQ-A-6.2); the batch continues.
        # The failure record MUST carry ``movie_id`` so the Evaluate handler can key the
        # skip (otherwise Evaluate KeyErrors on the whole batch). The id lives at a
        # different JSONPath depending on which task failed, so there are two record-skip
        # Pass states - one for the early chain (input still carries the SampledMovie
        # under ``$.movie``) and one for the persist/embed chain (input is the Validate
        # output carrying a top-level ``$.movie_id``).
        record_failure_early = sfn.Pass(
            self,
            "RecordItemFailureEarly",
            parameters={
                "status": "skipped",
                "outcome": "failed",
                "movie_id.$": "$.movie.movie.movie_id",
                "error.$": "$.error",
            },
        )
        record_failure_persisted = sfn.Pass(
            self,
            "RecordItemFailurePersisted",
            parameters={
                "status": "skipped",
                "outcome": "failed",
                "movie_id.$": "$.movie_id",
                "error.$": "$.error",
            },
        )

        # A Validate 'skipped' output bypasses Persist/EmbedVector (REQ-A-4.3) and flows
        # its skip record straight to the Map result for Evaluate to count.
        skipped = sfn.Pass(self, "ItemSkipped")

        # Per-item Catch (REQ-A-6.2): any task failure in the item chain routes to a
        # record-skip Pass so one movie's failure never aborts the batch. compute_pes,
        # enrich, and validate still have the SampledMovie at ``$.movie``; persist and
        # embed have replaced it with the Validate output that carries ``$.movie_id``.
        for task in (compute_pes, enrich, validate):
            task.add_catch(record_failure_early, result_path="$.error")
        for task in (persist, embed):
            task.add_catch(record_failure_persisted, result_path="$.error")

        persist_then_embed = persist.next(embed)
        branch_on_status = (
            sfn.Choice(self, "EnrichedOrSkipped")
            .when(
                sfn.Condition.string_equals("$.status", "enriched"),
                persist_then_embed,
            )
            .otherwise(skipped)
        )

        item_chain = compute_pes.next(enrich).next(validate).next(branch_on_status)

        process_movies = sfn.Map(
            self,
            "ProcessMovies",
            items_path="$.movies",
            item_selector={
                "movie.$": "$$.Map.Item.Value",
                "scale.$": "$.scale",
            },
            max_concurrency=5,
        )
        process_movies.item_processor(item_chain)

        evaluate = tasks.LambdaInvoke(
            self,
            "Evaluate",
            lambda_function=self.functions["evaluate"],
            payload=sfn.TaskInput.from_object({"records.$": "$"}),
            payload_response_only=True,
        )

        done = sfn.Succeed(self, "Done")

        definition = extract.next(process_movies).next(evaluate).next(done)

        return sfn.StateMachine(
            self,
            "EnrichmentPipeline",
            definition_body=sfn.DefinitionBody.from_chainable(definition),
            timeout=Duration.hours(1),
            comment="movieintel enrichment pipeline - extract, enrich, persist, evaluate.",
        )
