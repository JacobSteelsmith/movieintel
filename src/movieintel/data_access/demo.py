"""Task-1 demo entry point.

Prints the real ``movies`` and ``ratings`` schemas, the derived rating scale,
and a reproducible 50-100 movie sample joined to ratings (REQ-A-1 demo).

Run with ``uv run movieintel-demo`` (defaults to the bundled sample DBs) or
``uv run movieintel-demo --movies <path> --ratings <path> --n 50 --seed 42``.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from movieintel.data_access.models import ColumnInfo
from movieintel.data_access.ratings import observe_rating_scale, resolve_ratings_schema
from movieintel.data_access.sampling import sample_movies
from movieintel.data_access.sqlite_source import introspect_table, open_source

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_DB_DIR = _REPO_ROOT / "_sqlite"
DEFAULT_MOVIES_DB = _DEFAULT_DB_DIR / "movies.db"
DEFAULT_RATINGS_DB = _DEFAULT_DB_DIR / "ratings.db"


def _format_schema(columns: list[ColumnInfo]) -> str:
    lines = [f"  {c.name:<22} {c.type:<8} notnull={c.notnull!s:<5} pk={c.pk}" for c in columns]
    return "\n".join(lines)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Task-1 data-access demo.")
    parser.add_argument("--movies", type=Path, default=DEFAULT_MOVIES_DB)
    parser.add_argument("--ratings", type=Path, default=DEFAULT_RATINGS_DB)
    parser.add_argument("--n", type=int, default=100, help="sample size (50-100)")
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="retained for compatibility; selection is deterministic by revenue",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    conn_movies = open_source(args.movies)
    conn_ratings = open_source(args.ratings)
    try:
        print("=== movies schema (introspected) ===")
        print(_format_schema(introspect_table(conn_movies, "movies")))

        print("\n=== ratings schema (introspected) ===")
        print(_format_schema(introspect_table(conn_ratings, "ratings")))

        schema = resolve_ratings_schema(conn_ratings)
        print("\n=== resolved ratings roles ===")
        print(f"  movie_key_column = {schema.movie_key_column}")
        print(f"  rating_column    = {schema.rating_column}")

        scale = observe_rating_scale(conn_ratings, schema)
        print("\n=== derived rating scale/range ===")
        print(f"  observed_min  = {scale.observed_min}")
        print(f"  observed_max  = {scale.observed_max}")
        print(f"  declared_max  = {scale.declared_max}")
        print(f"  effective_max = {scale.effective_max}")
        print(f"  fallback_used = {scale.fallback_used}")

        sample = sample_movies(conn_movies, conn_ratings, n=args.n, seed=args.seed)
        with_ratings = sum(1 for s in sample if s.rating is not None)
        print(f"\n=== sample (n={args.n}, seed={args.seed}) — {with_ratings} with ratings ===")
        print(f"  {'movie_id':>8}  {'rating':>6}  {'count':>5}  title")
        for item in sample:
            rating = "-" if item.rating is None else f"{item.rating:.2f}"
            print(
                f"  {item.movie.movie_id:>8}  {rating:>6}  {item.rating_count:>5}  "
                f"{item.movie.title}"
            )
    finally:
        conn_movies.close()
        conn_ratings.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
