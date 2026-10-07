"""Golden-set fixture loader (REQ-X-2.1).

Parses the committed ``tests/fixtures/golden/*.json`` array into typed
:class:`GoldenItem`s. Self-contained: the fixtures carry every input inline, so loading
never touches the real ``_sqlite/`` databases (which are read-only and git-ignored).
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import TypeAdapter

from movieintel.eval.models import GoldenItem

_GOLDEN_ADAPTER = TypeAdapter(list[GoldenItem])


def load_golden(path: str | Path) -> list[GoldenItem]:
    """Load and validate the golden set at ``path`` into typed :class:`GoldenItem`s.

    :raises pydantic.ValidationError: if any fixture item is malformed.
    """
    raw = Path(path).read_text(encoding="utf-8")
    return _GOLDEN_ADAPTER.validate_python(json.loads(raw))
