"""Knowledge Base client configuration: KB id + retrieval knobs (Task 7, REQ-B-1).

Mirrors the ``BedrockConfig`` / ``PersistenceConfig`` precedent: no KB id, region, or
retrieval literal is ever hard-coded inline in the client; the single documented default
for each lives here as a module-level constant and is overridable via an environment
variable. The KB id defaults to ``None`` and is enforced at the point of use
(:meth:`KBConfig.require_knowledge_base_id`), exactly as ``BedrockConfig`` enforces the
Guardrail id, so an unconfigured KB fails loudly instead of calling Retrieve with no id.

AWS steering: resource-name defaults use hyphens, never em dashes.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# Documented default region (design §1.3): same precedence as ``BedrockConfig`` /
# ``PersistenceConfig`` so the whole system targets one region.
DEFAULT_REGION = "us-east-1"

# Default number of chunks the Retrieve API returns per query (skill retrieval default is
# 5; design §3.9 keeps ranking in app code). Overridable via MOVIEINTEL_KB_NUM_RESULTS.
DEFAULT_NUMBER_OF_RESULTS = 5


class KnowledgeBaseNotConfiguredError(RuntimeError):
    """Raised when ``semantic_search`` is attempted with no Knowledge Base id configured.

    The Retrieve API requires a ``knowledgeBaseId``; refusing to build a request without
    one keeps the invariant honest instead of calling Retrieve with a missing id (mirrors
    :class:`movieintel.config.GuardrailNotConfiguredError`).
    """

    def __init__(self) -> None:
        super().__init__(
            "No Bedrock Knowledge Base id configured (set MOVIEINTEL_KB_ID); "
            "the Retrieve API requires a knowledgeBaseId."
        )


@dataclass(frozen=True, slots=True)
class KBConfig:
    """Resolved Knowledge Base client configuration (region + KB id + result count).

    Built from the environment via :meth:`from_env` with documented defaults so a missing
    variable degrades to a sane value rather than failing opaquely. The exception is the
    KB id, which defaults to ``None`` and is enforced at the point of use
    (:meth:`require_knowledge_base_id`). ``env`` is injectable for tests; it defaults to
    :data:`os.environ`.
    """

    region: str = DEFAULT_REGION
    knowledge_base_id: str | None = None
    number_of_results: int = DEFAULT_NUMBER_OF_RESULTS

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> KBConfig:
        """Build a config from environment variables with documented defaults.

        Region resolves from ``BEDROCK_REGION`` first, then ``AWS_REGION``, then the
        :data:`DEFAULT_REGION` default (same precedence as ``BedrockConfig``). The KB id
        comes from ``MOVIEINTEL_KB_ID`` (``None`` when unset) and the result count from
        ``MOVIEINTEL_KB_NUM_RESULTS``.
        """
        source = os.environ if env is None else env
        region = source.get("BEDROCK_REGION") or source.get("AWS_REGION") or DEFAULT_REGION
        raw_num = source.get("MOVIEINTEL_KB_NUM_RESULTS")
        number_of_results = int(raw_num) if raw_num else DEFAULT_NUMBER_OF_RESULTS
        return cls(
            region=region,
            knowledge_base_id=source.get("MOVIEINTEL_KB_ID") or None,
            number_of_results=number_of_results,
        )

    def require_knowledge_base_id(self) -> str:
        """Return the KB id, or raise if unconfigured.

        :raises KnowledgeBaseNotConfiguredError: when :attr:`knowledge_base_id` is unset.
        """
        if self.knowledge_base_id is None:
            raise KnowledgeBaseNotConfiguredError
        return self.knowledge_base_id
