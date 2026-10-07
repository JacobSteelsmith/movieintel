"""CDK stack: a Bedrock Knowledge Base over the EXISTING S3 Vectors index (Task 7, REQ-B-1).

This stack creates a Customer-managed, VECTOR-type Bedrock Knowledge Base whose vector
store is the S3 Vectors bucket/index that ``MovieIntelPipelineStack`` provisions in Task 6
(``movieintel-vectors`` / ``movieintel-overviews``). It binds to that store BY CONFIGURED
NAME - it does NOT create a new vector bucket or index. The embedding model is Amazon Titan
Text Embeddings V2 (1024-dim), matching the Task 6 index dimension.

Resource names, index names, and model ids come from the ``movieintel`` config objects
(``PersistenceConfig`` / ``BedrockConfig``), never inlined. The KB service role is scoped
to the specific S3 Vectors index ARN and the Titan foundation-model ARN - no ``bedrock:*``,
no ``s3vectors:*``, no Resource ``*``. AWS steering: hyphens, never em dashes.

Fallback switch (design §9.1, REQ-B-1.3/.4): :func:`resolve_vector_store` reads
``MOVIEINTEL_KB_VECTOR_STORE`` and defaults to :data:`VectorStore.S3_VECTORS`. The
OpenSearch Serverless branch is present but INERT by default - it provisions NO OpenSearch
collection (which carries a ~$345/mo floor) and is only reached when the switch is
explicitly set to ``opensearch_serverless``. There is no silent auto-switch: if the S3
Vectors + Bedrock KB integration is unavailable at deploy time, the deploy STOPS and
reports back (consistent with the Task 6 ``S3VectorsUnavailableError`` no-substitution
contract); the user decides whether to flip the switch. The ``semantic_search`` client's
interface is identical for either store (REQ-B-1.4) because it depends only on the KB id
and the Retrieve API.

No Bedrock-managed ``CfnDataSource`` is created: the Task 6 pipeline writes embeddings
directly into the S3 Vectors index via ``movieintel.persistence.vectors`` and retrieval
uses the ``Retrieve`` API over those raw vectors (design §3.9), so there is no
Bedrock-managed chunking/ingestion data source. The "data source id if applicable" output
is therefore intentionally absent; the KB id and KB role ARN outputs are emitted.
"""

from __future__ import annotations

import os
from enum import StrEnum

from aws_cdk import (
    CfnOutput,
    Stack,
)
from aws_cdk import (
    aws_bedrock as bedrock,
)
from aws_cdk import (
    aws_iam as iam,
)
from constructs import Construct

from movieintel.persistence.config import PersistenceConfig

#: Logical name of the Knowledge Base (hyphens only, no em dashes).
KNOWLEDGE_BASE_NAME = "movieintel-overviews-kb"

#: Logical name of the KB service role (hyphens only, no em dashes).
KB_ROLE_NAME = "movieintel-kb-service-role"

#: Environment variable selecting the KB vector store; defaults to S3 Vectors.
VECTOR_STORE_ENV = "MOVIEINTEL_KB_VECTOR_STORE"


class VectorStore(StrEnum):
    """The vector store backing the Knowledge Base (design §9.1, REQ-B-1.3/.4).

    ``S3_VECTORS`` is the default and the only store provisioned/referenced by default.
    ``OPENSEARCH_SERVERLESS`` is the documented fallback: the stack keeps the code path
    present but INERT, never provisioning an OpenSearch collection unless the switch is
    explicitly set to it.
    """

    S3_VECTORS = "s3_vectors"
    OPENSEARCH_SERVERLESS = "opensearch_serverless"


def resolve_vector_store(env: dict[str, str] | None = None) -> VectorStore:
    """Resolve the KB vector store from ``MOVIEINTEL_KB_VECTOR_STORE`` (default S3 Vectors).

    An unset or unrecognized value resolves to :data:`VectorStore.S3_VECTORS` - the
    no-fallback-by-default posture. ``env`` is injectable for tests; it defaults to
    :data:`os.environ`.
    """
    source = os.environ if env is None else env
    raw = source.get(VECTOR_STORE_ENV)
    if not raw:
        return VectorStore.S3_VECTORS
    try:
        return VectorStore(raw)
    except ValueError:
        return VectorStore.S3_VECTORS


class KnowledgeBaseStack(Stack):
    """A Bedrock KB bound to the existing Task 6 S3 Vectors index (default), with a role."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs: object) -> None:
        super().__init__(scope, construct_id, **kwargs)  # type: ignore[arg-type]

        self.persistence = PersistenceConfig.from_env()
        self.vector_store = resolve_vector_store()

        # ARNs for the EXISTING Task 6 index/bucket and the Titan v2 foundation model,
        # built from region/account + configured names (never from a new construct).
        self.vector_bucket_arn = self._vector_bucket_arn()
        self.vector_index_arn = self._vector_index_arn()
        self.embedding_model_arn = (
            f"arn:aws:bedrock:{self.region}::foundation-model/{self.persistence.embedding_model_id}"
        )

        self.kb_role = self._kb_service_role()
        self.knowledge_base = self._knowledge_base()
        self._outputs()

    # -- ARNs for the existing Task 6 store -----------------------------------

    def _vector_bucket_arn(self) -> str:
        """ARN of the EXISTING Task 6 S3 Vectors bucket (referenced, not created)."""
        return (
            f"arn:aws:s3vectors:{self.region}:{self.account}:bucket/"
            f"{self.persistence.vector_bucket_name}"
        )

    def _vector_index_arn(self) -> str:
        """ARN of the EXISTING Task 6 S3 Vectors index (referenced, not created)."""
        return (
            f"arn:aws:s3vectors:{self.region}:{self.account}:bucket/"
            f"{self.persistence.vector_bucket_name}/index/{self.persistence.vector_index_name}"
        )

    # -- IAM ------------------------------------------------------------------

    def _kb_service_role(self) -> iam.Role:
        """Least-privilege KB service role (confused-deputy guarded trust + scoped policy).

        Trust: ``bedrock.amazonaws.com`` with ``aws:SourceAccount`` + an ``ArnLike``
        ``aws:SourceArn`` of ``arn:aws:bedrock:<region>:<account>:knowledge-base/*``.
        Permissions are scoped to the Titan foundation-model ARN and the specific S3
        Vectors index ARN - no ``bedrock:*``, no ``s3vectors:*``, no Resource ``*``.
        """
        role = iam.Role(
            self,
            "KnowledgeBaseServiceRole",
            role_name=KB_ROLE_NAME,
            assumed_by=iam.ServicePrincipal(
                "bedrock.amazonaws.com",
                conditions={
                    "StringEquals": {"aws:SourceAccount": self.account},
                    "ArnLike": {
                        "aws:SourceArn": (
                            f"arn:aws:bedrock:{self.region}:{self.account}:knowledge-base/*"
                        )
                    },
                },
            ),
            description=(
                "Service role for the movieintel Bedrock Knowledge Base - scoped to the "
                "Titan v2 model and the S3 Vectors index."
            ),
        )

        # Invoke the Titan v2 embedding model (scoped to the one foundation-model ARN).
        role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["bedrock:InvokeModel"],
                resources=[self.embedding_model_arn],
            )
        )

        # Read/write the EXISTING S3 Vectors index (scoped to the index ARN). Query/Get
        # back retrieval; Put/Delete keep ingestion parity with the Task 6 writer.
        role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    "s3vectors:GetIndex",
                    "s3vectors:QueryVectors",
                    "s3vectors:GetVectors",
                    "s3vectors:PutVectors",
                    "s3vectors:DeleteVectors",
                ],
                resources=[self.vector_index_arn],
            )
        )
        return role

    # -- Knowledge Base -------------------------------------------------------

    def _storage_configuration(
        self,
    ) -> bedrock.CfnKnowledgeBase.StorageConfigurationProperty:
        """Build the storage config for the selected vector store (default S3 Vectors).

        The default S3 Vectors branch references the EXISTING Task 6 index by ARN + name.
        The OpenSearch Serverless branch is present but INERT: it is only reached when the
        switch is explicitly ``opensearch_serverless``, and it does NOT provision an
        OpenSearch collection here - the collection ARN / index would be supplied by the
        operator who opts in. Keeping it inert honors the Task 6 no-substitution contract
        and avoids the ~$345/mo OpenSearch floor by default.
        """
        if self.vector_store is VectorStore.OPENSEARCH_SERVERLESS:
            # INERT fallback path (documented, not reached by default). An operator who
            # opts in supplies the collection ARN / index via config; this stack never
            # auto-provisions OpenSearch Serverless.
            collection_arn = os.environ.get("MOVIEINTEL_KB_OPENSEARCH_COLLECTION_ARN", "")
            index_name = os.environ.get(
                "MOVIEINTEL_KB_OPENSEARCH_INDEX", self.persistence.vector_index_name
            )
            return bedrock.CfnKnowledgeBase.StorageConfigurationProperty(
                type="OPENSEARCH_SERVERLESS",
                opensearch_serverless_configuration=(
                    bedrock.CfnKnowledgeBase.OpenSearchServerlessConfigurationProperty(
                        collection_arn=collection_arn,
                        vector_index_name=index_name,
                        field_mapping=(
                            bedrock.CfnKnowledgeBase.OpenSearchServerlessFieldMappingProperty(
                                metadata_field="metadata",
                                text_field="text",
                                vector_field="vector",
                            )
                        ),
                    )
                ),
            )

        # DEFAULT: bind to the EXISTING Task 6 S3 Vectors index by CONFIGURED NAME. The
        # CfnKnowledgeBase S3 Vectors config is a oneOf: either ['IndexArn'] or
        # ['IndexName', 'VectorBucketArn'] - supplying all three fails CloudFormation
        # validation ("valid under more than one oneOf schema"). We bind by name (bucket
        # ARN + index name), matching the Task 6 configured names, and never create a new
        # bucket/index here.
        return bedrock.CfnKnowledgeBase.StorageConfigurationProperty(
            type="S3_VECTORS",
            s3_vectors_configuration=bedrock.CfnKnowledgeBase.S3VectorsConfigurationProperty(
                index_name=self.persistence.vector_index_name,
                vector_bucket_arn=self.vector_bucket_arn,
            ),
        )

    def _knowledge_base(self) -> bedrock.CfnKnowledgeBase:
        """Create the VECTOR-type Customer-managed KB (Titan v2 + selected store)."""
        knowledge_base = bedrock.CfnKnowledgeBase(
            self,
            "MovieIntelKnowledgeBase",
            name=KNOWLEDGE_BASE_NAME,
            role_arn=self.kb_role.role_arn,
            description=(
                "Knowledge Base over movieintel enriched movie overviews, backed by the "
                "S3 Vectors index and queried with the Retrieve API."
            ),
            knowledge_base_configuration=(
                bedrock.CfnKnowledgeBase.KnowledgeBaseConfigurationProperty(
                    type="VECTOR",
                    vector_knowledge_base_configuration=(
                        bedrock.CfnKnowledgeBase.VectorKnowledgeBaseConfigurationProperty(
                            embedding_model_arn=self.embedding_model_arn,
                        )
                    ),
                )
            ),
            storage_configuration=self._storage_configuration(),
        )

        # Bedrock validates the service role's S3 Vectors / InvokeModel permissions when
        # it CREATES the KB (it assumes the role and calls s3vectors:QueryVectors). The
        # `role_arn` reference only orders the KB after the ROLE, not after the role's
        # inline DefaultPolicy, so CloudFormation creates the KB and the policy in parallel
        # and KB validation can race ahead of the policy attachment (observed 403:
        # "no identity-based policy allows the s3vectors:QueryVectors action"). Depend on
        # the role's node so the inline policy attaches before the KB is validated.
        knowledge_base.node.add_dependency(self.kb_role)
        return knowledge_base

    # -- outputs --------------------------------------------------------------

    def _outputs(self) -> None:
        """Export the KB id and the KB service role ARN as stack outputs.

        No data source id is exported: this architecture has no Bedrock-managed data
        source (embeddings are written directly to S3 Vectors by Task 6).
        """
        CfnOutput(
            self,
            "KnowledgeBaseId",
            value=self.knowledge_base.attr_knowledge_base_id,
            description="Bedrock Knowledge Base id - set as MOVIEINTEL_KB_ID for the client.",
        )
        CfnOutput(
            self,
            "KnowledgeBaseServiceRoleArn",
            value=self.kb_role.role_arn,
            description="ARN of the least-privilege Knowledge Base service role.",
        )
