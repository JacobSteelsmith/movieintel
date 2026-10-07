"""Shared constants and hard-stop guards for the enrichment pipeline stack (REQ-A-6).

Two invariants are enforced here rather than papered over:

* **S3 Vectors is native, with no OpenSearch substitution.** The vector store is built
  from the first-party ``aws_cdk.aws_s3vectors`` L1 module. If that module cannot be
  imported, :func:`import_s3vectors` raises :class:`S3VectorsUnavailableError` and the
  stack stops. We never silently fall back to an OpenSearch collection.
* **The source DBs must exist at synth time.** The read-only SQLite files are packaged
  as a Lambda layer from ``_sqlite/``; :func:`check_source_dbs` raises
  :class:`MissingSourceDatabaseError` when either file is absent so synth fails loudly
  instead of producing a broken layer.

Resource-name/description literals belong in the ``movieintel`` config objects
(``BedrockConfig`` / ``PersistenceConfig``), not here. This module only holds the
embedding dimension (a model fact) and the two guards. AWS steering: hyphens, never em
dashes.
"""

from __future__ import annotations

import importlib
import shutil
import subprocess
from pathlib import Path
from types import ModuleType

import jsii
from aws_cdk import BundlingOptions, BundlingOutput, DockerImage, ILocalBundling
from aws_cdk import aws_bedrock as bedrock

# Amazon Titan Text Embeddings V2 emits 1024-dim float32 vectors (design §1.3,
# REQ-A-5.3). The S3 Vectors index dimension must match the embedder.
EMBEDDING_DIMENSION = 1024

#: The Claude Sonnet 4.5 cross-region inference profile the agent/enrich Lambdas invoke
#: via Converse. Invoking an inference profile requires bedrock:InvokeModel on BOTH the
#: profile ARN AND each underlying foundation-model ARN the profile routes to, so the IAM
#: grant is built from the profile id plus the underlying model id across the profile's
#: target regions below. The profile id must stay in sync with ``DEFAULT_MODEL_ID`` in
#: src/movieintel/config.py (the Lambda env-var source).
INFERENCE_PROFILE_ID = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"

#: The foundation model that the above inference profile fronts (same id, no ``us.``
#: prefix), used to build the per-region ``::foundation-model/`` resource ARNs.
UNDERLYING_FOUNDATION_MODEL_ID = "anthropic.claude-sonnet-4-5-20250929-v1:0"

#: Regions the inference profile routes Converse requests to. The grant must cover the
#: foundation-model ARN in each so a cross-region route does not hit AccessDenied.
INFERENCE_PROFILE_TARGET_REGIONS = ("us-east-1", "us-east-2", "us-west-2")


def bedrock_invoke_model_resources(region: str, account: str) -> list[str]:
    """Return the ARNs a bedrock:InvokeModel grant needs to call the inference profile.

    Invoking a cross-region inference profile needs bedrock:InvokeModel on BOTH the
    profile ARN (account-scoped) AND each underlying foundation-model ARN the profile
    routes to (account-less, one per target region). Granting only the profile ARN - or a
    single ::foundation-model ARN - yields runtime AccessDenied. Returns the profile ARN
    followed by the three cross-region foundation-model ARNs.
    """
    profile_arn = f"arn:aws:bedrock:{region}:{account}:inference-profile/{INFERENCE_PROFILE_ID}"
    foundation_model_arns = [
        f"arn:aws:bedrock:{target_region}::foundation-model/{UNDERLYING_FOUNDATION_MODEL_ID}"
        for target_region in INFERENCE_PROFILE_TARGET_REGIONS
    ]
    return [profile_arn, *foundation_model_arns]


# -- shared Bedrock Guardrail policy --------------------------------------------
#
# Both guardrails (the enrichment guardrail in pipeline_stack.py and the serving guardrail
# in serving_stack.py) share the SAME harmful-content filters, denied topic, and PII
# policy. These builders define that shared policy exactly once so the two guardrails
# differ ONLY by PROMPT_ATTACK. Enrichment is a trusted batch path with no untrusted user
# input and passes ``include_prompt_attack=False``; serving takes untrusted free-text
# queries and passes ``include_prompt_attack=True``. Hyphens only, never em dashes
# (AWS steering).

#: The five harmful-content filters both guardrails always apply, HIGH on input + output.
_HARMFUL_CONTENT_FILTER_TYPES = ("HATE", "VIOLENCE", "SEXUAL", "INSULTS", "MISCONDUCT")


def guardrail_content_filters(
    *, include_prompt_attack: bool
) -> list[bedrock.CfnGuardrail.ContentFilterConfigProperty]:
    """Return the content filters shared by both guardrails.

    Always includes the five harmful-content filters (HATE, VIOLENCE, SEXUAL, INSULTS,
    MISCONDUCT) at HIGH on both Converse input and output. When ``include_prompt_attack``
    is True (serving only), appends PROMPT_ATTACK with input strength HIGH and output
    strength NONE (Bedrock supports an input strength only for PROMPT_ATTACK). Enrichment
    passes False: it is a trusted batch path with no untrusted user input, and a
    PROMPT_ATTACK input filter was misclassifying the enrichment prompt's JSON-formatting
    directives as injection and blocking ~98/100 enrichments.
    """
    filters: list[bedrock.CfnGuardrail.ContentFilterConfigProperty] = [
        bedrock.CfnGuardrail.ContentFilterConfigProperty(
            type=filter_type,
            input_strength="HIGH",
            output_strength="HIGH",
        )
        for filter_type in _HARMFUL_CONTENT_FILTER_TYPES
    ]
    if include_prompt_attack:
        filters.append(
            bedrock.CfnGuardrail.ContentFilterConfigProperty(
                type="PROMPT_ATTACK",
                input_strength="HIGH",
                output_strength="NONE",
            )
        )
    return filters


def guardrail_denied_topics() -> list[bedrock.CfnGuardrail.TopicConfigProperty]:
    """Return the shared ``non-movie-advice`` DENY topic (hyphens only, no em dashes)."""
    return [
        bedrock.CfnGuardrail.TopicConfigProperty(
            name="non-movie-advice",
            type="DENY",
            definition=(
                "Requests for medical, legal, or financial advice unrelated "
                "to movie metadata enrichment."
            ),
            examples=[
                "What stock should I buy?",
                "Diagnose my symptoms.",
            ],
        )
    ]


def guardrail_pii_entities() -> list[bedrock.CfnGuardrail.PiiEntityConfigProperty]:
    """Return the shared PII policy: EMAIL and PHONE ANONYMIZE (NAME intentionally excluded).

    NAME is intentionally excluded: ANONYMIZE on NAME rewrites legitimate movie
    cast/director/character names in the model's structured JSON output, corrupting it
    (observed: 98/100 enrichments blocked). A movie-domain assistant must be able to emit
    real people's names; EMAIL/PHONE stay anonymized as incidental contact details with no
    place in enrichment output.
    """
    return [
        bedrock.CfnGuardrail.PiiEntityConfigProperty(
            type=pii_type,
            action="ANONYMIZE",
        )
        for pii_type in ("EMAIL", "PHONE")
    ]


# The read-only source DBs live in the gitignored _sqlite/ at the repo root. This file
# is infra/constants.py, so the repo root is two parents up.
REPO_ROOT = Path(__file__).resolve().parent.parent
SQLITE_SOURCE_DIR = REPO_ROOT / "_sqlite"

#: The two source DB filenames the Extract handler expects under SQLITE_DIR.
SOURCE_DB_FILENAMES = ("movies.db", "ratings.db")


class S3VectorsUnavailableError(RuntimeError):
    """Raised when the native ``aws_cdk.aws_s3vectors`` module cannot be imported.

    The vector store has no OpenSearch substitution; if the L1 module is missing the
    stack stops rather than provisioning a different backend.
    """

    def __init__(self) -> None:
        super().__init__(
            "aws_cdk.aws_s3vectors is unavailable; the S3 Vectors store cannot be "
            "provisioned and there is no OpenSearch substitution. Upgrade aws-cdk-lib "
            "to a version that ships aws_s3vectors."
        )


class MissingSourceDatabaseError(RuntimeError):
    """Raised at synth time when a source SQLite DB is absent from the layer directory."""

    def __init__(self, missing: list[str], directory: Path) -> None:
        joined = ", ".join(missing)
        super().__init__(
            f"Missing source database file(s) [{joined}] under {directory}; "
            "the SQLite Lambda layer cannot be built. Restore the read-only _sqlite/ "
            "DBs before synthesizing."
        )
        self.missing = missing
        self.directory = directory


def import_s3vectors() -> ModuleType:
    """Return the native ``aws_cdk.aws_s3vectors`` module, or raise.

    :raises S3VectorsUnavailableError: when the module cannot be imported. There is no
        fallback backend.
    """
    try:
        return importlib.import_module("aws_cdk.aws_s3vectors")
    except ImportError as exc:
        raise S3VectorsUnavailableError from exc


def check_source_dbs(directory: Path) -> None:
    """Verify both source DBs are present under ``directory``.

    :raises MissingSourceDatabaseError: when ``movies.db`` or ``ratings.db`` is absent.
    """
    missing = [name for name in SOURCE_DB_FILENAMES if not (directory / name).is_file()]
    if missing:
        raise MissingSourceDatabaseError(missing, directory)


# The ``movieintel`` package source and the third-party deps the handlers import at
# runtime (``pydantic``; ``boto3``/``botocore`` ship with the Lambda runtime). The shared
# layer must place these under ``python/`` so they land on the Lambda import path
# (``/opt/python``) - a flat copy of ``src/`` would mount at ``/opt/movieintel`` which is
# NOT importable (design §2, REQ-X-6).
MOVIEINTEL_SRC_DIR = REPO_ROOT / "src" / "movieintel"

#: Runtime deps the handlers pull in transitively through ``movieintel`` that are not in
#: the Lambda base image. Pinned loosely to the pyproject floor; the exact resolved
#: versions come from the environment the layer is bundled in.
SHARED_LAYER_PIP_REQUIREMENTS = ("pydantic>=2.7",)


def _stage_shared_layer(output_dir: str) -> None:
    """Assemble the shared layer under ``<output_dir>/python`` (``movieintel`` + deps).

    Copies the ``movieintel`` package and pip-installs the runtime deps the handlers need
    (``pydantic``) into the ``python/`` subdirectory so they resolve on the Lambda import
    path. Raises :class:`SharedLayerBundlingError` if staging fails.
    """
    python_dir = Path(output_dir) / "python"
    python_dir.mkdir(parents=True, exist_ok=True)

    shutil.copytree(
        MOVIEINTEL_SRC_DIR,
        python_dir / "movieintel",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        dirs_exist_ok=True,
    )

    try:
        subprocess.run(
            [
                "uv",
                "pip",
                "install",
                "--target",
                str(python_dir),
                *SHARED_LAYER_PIP_REQUIREMENTS,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = exc.stderr if isinstance(exc, subprocess.CalledProcessError) else str(exc)
        raise SharedLayerBundlingError(detail) from exc


class SharedLayerBundlingError(RuntimeError):
    """Raised when the shared Lambda layer cannot be staged (copy or pip install failed)."""

    def __init__(self, detail: str) -> None:
        super().__init__(
            "Failed to stage the shared movieintel Lambda layer under python/; "
            f"the handler Lambdas would not be able to import movieintel/pydantic. {detail}"
        )


@jsii.implements(ILocalBundling)
class _SharedLayerLocalBundling:
    """Local (no Docker) bundler that stages the shared layer in place.

    Returning ``True`` from :meth:`try_bundle` tells CDK the asset was produced locally so
    the Docker image in :func:`shared_layer_bundling` is never pulled.
    """

    def try_bundle(self, output_dir: str, *, image: DockerImage, **_: object) -> bool:
        _stage_shared_layer(output_dir)
        return True


def shared_layer_bundling() -> BundlingOptions:
    """Bundling for the shared layer: stage ``movieintel`` + pydantic under ``python/``.

    Uses local bundling (``uv pip install`` + copy) so synth needs no Docker; the Docker
    ``image`` is only a declared fallback CDK never reaches when local bundling succeeds.
    """
    return BundlingOptions(
        image=DockerImage.from_registry("public.ecr.aws/lambda/python:3.12"),
        local=_SharedLayerLocalBundling(),
        command=[
            "bash",
            "-c",
            "cp -r /asset-input/movieintel /asset-output/python/movieintel && "
            "pip install pydantic>=2.7 --target /asset-output/python",
        ],
        output_type=BundlingOutput.NOT_ARCHIVED,
    )


#: Subdirectory inside the SQLite layer under which the source DBs are staged. A Lambda
#: layer mounts at ``/opt``, so staging the DBs under ``sqlite/`` lands them at
#: ``/opt/sqlite/`` - the directory the Extract handler reads via ``SQLITE_DIR``. A flat
#: copy of ``_sqlite/`` would mount the DBs at ``/opt/movies.db`` and the handler, which
#: looks under ``/opt/sqlite``, would not find them.
SQLITE_LAYER_SUBDIR = "sqlite"


def _stage_sqlite_layer(output_dir: str) -> None:
    """Copy the two read-only source DBs into ``<output_dir>/sqlite``.

    Staging under a ``sqlite/`` subtree lands them at ``/opt/sqlite/`` on the Lambda so
    the Extract handler's ``SQLITE_DIR=/opt/sqlite`` resolves ``movies.db``/``ratings.db``.
    """
    sqlite_dir = Path(output_dir) / SQLITE_LAYER_SUBDIR
    sqlite_dir.mkdir(parents=True, exist_ok=True)
    for name in SOURCE_DB_FILENAMES:
        shutil.copyfile(SQLITE_SOURCE_DIR / name, sqlite_dir / name)


@jsii.implements(ILocalBundling)
class _SqliteLayerLocalBundling:
    """Local (no Docker) bundler that stages the source DBs under ``sqlite/``.

    Returning ``True`` from :meth:`try_bundle` tells CDK the asset was produced locally so
    the Docker image in :func:`sqlite_layer_bundling` is never pulled.
    """

    def try_bundle(self, output_dir: str, *, image: DockerImage, **_: object) -> bool:
        _stage_sqlite_layer(output_dir)
        return True


def sqlite_layer_bundling() -> BundlingOptions:
    """Bundling for the SQLite layer: stage the source DBs under ``sqlite/``.

    Uses local bundling (a plain file copy) so synth needs no Docker; the Docker ``image``
    is only a declared fallback CDK never reaches when local bundling succeeds.
    """
    return BundlingOptions(
        image=DockerImage.from_registry("public.ecr.aws/lambda/python:3.12"),
        local=_SqliteLayerLocalBundling(),
        command=[
            "bash",
            "-c",
            "mkdir -p /asset-output/sqlite && cp /asset-input/*.db /asset-output/sqlite/",
        ],
        output_type=BundlingOutput.NOT_ARCHIVED,
    )
