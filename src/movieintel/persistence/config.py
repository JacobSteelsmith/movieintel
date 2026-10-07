"""Persistence configuration: resource names and the embedding model id (REQ-A-5, REQ-X-5.1).

No resource name, index name, or model id is ever hard-coded inline in the repository,
vector writer, or embedder code. The single documented default for each lives here as a
module-level constant and is overridable via an environment variable, mirroring the
``BedrockConfig`` precedent in :mod:`movieintel.config`.

Billing posture: the ``MovieIntel`` table is intended to run in on-demand
(``PAY_PER_REQUEST``) billing mode so it scales to zero (REQ-A-5.2, REQ-X-5.1). Task 5
code never sets throughput and never assumes provisioned capacity; the capacity mode is a
table property created by CDK in Task 6. This dataclass records that intent for the reader.

AWS steering: resource-name defaults use hyphens, never em dashes.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# Documented default region (design §1.3): same precedence as ``BedrockConfig`` so the
# whole system targets one region.
DEFAULT_REGION = "us-east-1"

# DynamoDB single-table design (design §3.4, §7.3).
DEFAULT_TABLE_NAME = "MovieIntel"
DEFAULT_GSI1_NAME = "GSI1"

# S3 Vectors bucket + index (design §3.4, §7.6). Hyphenated, no em dashes (AWS steering).
DEFAULT_VECTOR_BUCKET_NAME = "movieintel-vectors"
DEFAULT_VECTOR_INDEX_NAME = "movieintel-overviews"

# Amazon Titan Text Embeddings V2 model id (design §1.3, REQ-A-5.3). This is the ONLY
# embedding-model-id literal in the package; embedder code reads it from config.
DEFAULT_EMBEDDING_MODEL_ID = "amazon.titan-embed-text-v2:0"


@dataclass(frozen=True, slots=True)
class PersistenceConfig:
    """Resolved persistence configuration (table/GSI/vector names + embedding model id).

    Built from the environment via :meth:`from_env` with documented defaults so a missing
    variable degrades to a sane value rather than failing opaquely. ``env`` is injectable
    for tests; it defaults to :data:`os.environ`.

    The ``MovieIntel`` table is created by CDK in Task 6 with on-demand
    (``PAY_PER_REQUEST``) billing; this layer never assumes provisioned throughput.
    """

    region: str = DEFAULT_REGION
    table_name: str = DEFAULT_TABLE_NAME
    gsi1_name: str = DEFAULT_GSI1_NAME
    vector_bucket_name: str = DEFAULT_VECTOR_BUCKET_NAME
    vector_index_name: str = DEFAULT_VECTOR_INDEX_NAME
    embedding_model_id: str = DEFAULT_EMBEDDING_MODEL_ID

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> PersistenceConfig:
        """Build a config from environment variables with documented defaults.

        Region resolves from ``BEDROCK_REGION`` first, then ``AWS_REGION``, then the
        :data:`DEFAULT_REGION` default (same precedence as ``BedrockConfig``). Each
        resource name and the embedding model id are overridable via their own variable.
        """
        source = os.environ if env is None else env
        region = source.get("BEDROCK_REGION") or source.get("AWS_REGION") or DEFAULT_REGION
        return cls(
            region=region,
            table_name=source.get("MOVIEINTEL_TABLE_NAME") or DEFAULT_TABLE_NAME,
            gsi1_name=source.get("MOVIEINTEL_GSI1_NAME") or DEFAULT_GSI1_NAME,
            vector_bucket_name=(
                source.get("MOVIEINTEL_VECTOR_BUCKET") or DEFAULT_VECTOR_BUCKET_NAME
            ),
            vector_index_name=(source.get("MOVIEINTEL_VECTOR_INDEX") or DEFAULT_VECTOR_INDEX_NAME),
            embedding_model_id=(
                source.get("MOVIEINTEL_EMBEDDING_MODEL_ID") or DEFAULT_EMBEDDING_MODEL_ID
            ),
        )
