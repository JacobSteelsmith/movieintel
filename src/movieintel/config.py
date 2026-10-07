"""Single config source for Bedrock region, model id, and Guardrail (design §1.3, §11).

The Bedrock model id is NEVER hard-coded inline in the enrichment client; the one
documented default lives here as :data:`DEFAULT_MODEL_ID` and is overridable via the
``BEDROCK_MODEL_ID`` environment variable. A Guardrail is required on every Converse
call (REQ-A-4.4, REQ-X-4.1), so :meth:`BedrockConfig.guardrail_config` raises when no
Guardrail id is configured rather than silently sending an unguarded request.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# Documented default region (design §1.3): broadest Bedrock model availability and
# within the S3 Vectors + Bedrock KB preview-region set.
DEFAULT_REGION = "us-east-1"

# Documented default Anthropic Claude model id reachable via the Converse API
# (design §1.3/§11). This is now a cross-region INFERENCE PROFILE id - the ``us.``
# prefix marks a Claude Sonnet 4.5 inference profile that routes Converse calls across
# the us-east-1/us-east-2/us-west-2 foundation-model endpoints. Overridable via
# ``BEDROCK_MODEL_ID``. This constant is the ONLY model-id literal in the package - the
# client reads ``BedrockConfig.model_id``, never a literal.
DEFAULT_MODEL_ID = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"

# Bedrock Guardrail versions accept the literal "DRAFT" or a published number.
DEFAULT_GUARDRAIL_VERSION = "DRAFT"


class GuardrailNotConfiguredError(RuntimeError):
    """Raised when a Converse call is attempted with no Guardrail id configured.

    A Guardrail must be attached to every enrichment call (REQ-A-4.4); refusing to
    build a ``guardrailConfig`` without an id keeps that invariant honest instead of
    silently sending an unguarded request.
    """

    def __init__(self) -> None:
        super().__init__(
            "No Bedrock Guardrail id configured (set BEDROCK_GUARDRAIL_ID); "
            "a Guardrail is required on every Converse call."
        )


@dataclass(frozen=True, slots=True)
class BedrockConfig:
    """Resolved Bedrock configuration (region + model id + Guardrail).

    Built from the environment via :meth:`from_env` with documented defaults so a
    missing variable degrades to a sane value rather than failing opaquely. The
    exception is the Guardrail id, which defaults to ``None`` and is enforced at the
    point of use (:meth:`guardrail_config`).
    """

    region: str = DEFAULT_REGION
    model_id: str = DEFAULT_MODEL_ID
    guardrail_id: str | None = None
    guardrail_version: str = DEFAULT_GUARDRAIL_VERSION

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> BedrockConfig:
        """Build a config from environment variables with documented defaults.

        Region resolves from ``BEDROCK_REGION`` first, then ``AWS_REGION``, then the
        :data:`DEFAULT_REGION` default. ``env`` is injectable for tests; it defaults
        to :data:`os.environ`.
        """
        source = os.environ if env is None else env
        region = source.get("BEDROCK_REGION") or source.get("AWS_REGION") or DEFAULT_REGION
        guardrail_id = source.get("BEDROCK_GUARDRAIL_ID") or None
        return cls(
            region=region,
            model_id=source.get("BEDROCK_MODEL_ID") or DEFAULT_MODEL_ID,
            guardrail_id=guardrail_id,
            guardrail_version=source.get("BEDROCK_GUARDRAIL_VERSION") or DEFAULT_GUARDRAIL_VERSION,
        )

    def guardrail_config(self) -> dict[str, str]:
        """Return the Converse ``guardrailConfig`` dict, or raise if unconfigured.

        :raises GuardrailNotConfiguredError: when :attr:`guardrail_id` is unset.
        """
        if self.guardrail_id is None:
            raise GuardrailNotConfiguredError
        return {
            "guardrailIdentifier": self.guardrail_id,
            "guardrailVersion": self.guardrail_version,
        }
