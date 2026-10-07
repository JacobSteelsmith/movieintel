"""Error types raised by the data-access layer (REQ-A-1.4, REQ-A-1.5)."""

from __future__ import annotations


class SourceDatabaseError(Exception):
    """Raised when a source SQLite database is missing or cannot be opened/read.

    Names the offending file so a failure is actionable (REQ-A-1.4).
    """

    def __init__(self, path: str, reason: str | None = None) -> None:
        self.path = str(path)
        self.reason = reason
        message = f"Cannot open source database: {self.path}"
        if reason:
            message += f" ({reason})"
        super().__init__(message)


class MissingRatingsColumnError(Exception):
    """Raised when the introspected ``ratings`` schema cannot fill a required role.

    Surfaces the role and the available columns instead of silently producing
    nulls (REQ-A-1.5).
    """

    def __init__(self, role: str, available_columns: list[str]) -> None:
        self.role = role
        self.available_columns = list(available_columns)
        super().__init__(
            f"No column resolves ratings role '{role}'. "
            f"Available columns: {', '.join(available_columns)}"
        )
