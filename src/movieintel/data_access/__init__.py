"""Data-access layer: SQLite open, introspection, and reproducible sampling.

Public API re-exported for ergonomic imports (REQ-A-1).
"""

from __future__ import annotations

from movieintel.data_access.errors import (
    MissingRatingsColumnError,
    SourceDatabaseError,
)
from movieintel.data_access.models import (
    ColumnInfo,
    MovieRow,
    RatingScale,
    RatingsSchema,
    SampledMovie,
)
from movieintel.data_access.movies import (
    MOVIES_DOCUMENTED_COLUMNS,
    read_movies,
)
from movieintel.data_access.ratings import (
    RATINGS_MOVIE_KEY_CANDIDATES,
    RATINGS_RATING_CANDIDATES,
    observe_rating_scale,
    resolve_ratings_schema,
)
from movieintel.data_access.sampling import sample_movies
from movieintel.data_access.sqlite_source import introspect_table, open_source

__all__ = [
    "MOVIES_DOCUMENTED_COLUMNS",
    "RATINGS_MOVIE_KEY_CANDIDATES",
    "RATINGS_RATING_CANDIDATES",
    "ColumnInfo",
    "MissingRatingsColumnError",
    "MovieRow",
    "RatingScale",
    "RatingsSchema",
    "SampledMovie",
    "SourceDatabaseError",
    "introspect_table",
    "observe_rating_scale",
    "open_source",
    "read_movies",
    "resolve_ratings_schema",
    "sample_movies",
]
