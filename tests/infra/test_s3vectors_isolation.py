"""Isolation + stop-if-impossible guards for the pipeline stack (FEAT-002, REQ-A-6).

These assert the two hard-stop contracts of ``infra.constants`` BEFORE the stack is
built:

* S3 Vectors is provisioned via the native ``aws_cdk.aws_s3vectors`` L1 module. There is
  NO OpenSearch substitution: when that module cannot be imported the lazy helper raises
  :class:`S3VectorsUnavailableError` rather than silently falling back.
* The read-only source SQLite DBs are packaged as a Lambda layer from ``_sqlite/``. When
  either ``movies.db`` or ``ratings.db`` is absent the synth-time guard raises
  :class:`MissingSourceDatabaseError`.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import constants
import pytest


def test_embedding_dimension_is_titan_v2() -> None:
    """Titan Text Embeddings V2 produces 1024-dim vectors (design §1.3, REQ-A-5.3)."""
    assert constants.EMBEDDING_DIMENSION == 1024


def test_import_s3vectors_returns_native_module() -> None:
    """The lazy helper returns the real ``aws_s3vectors`` module when it is importable."""
    module = constants.import_s3vectors()
    assert hasattr(module, "CfnVectorBucket")
    assert hasattr(module, "CfnIndex")


def test_import_s3vectors_raises_when_module_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """When ``aws_s3vectors`` cannot be imported the helper raises, with NO fallback."""
    # Evict any cached copy and block re-import so the lazy helper hits ImportError.
    monkeypatch.delitem(sys.modules, "aws_cdk.aws_s3vectors", raising=False)
    real_import_module = importlib.import_module

    def fake_import_module(name: str, package: str | None = None) -> object:
        if name == "aws_cdk.aws_s3vectors":
            raise ImportError("simulated missing aws_s3vectors")
        return real_import_module(name, package)

    monkeypatch.setattr(importlib, "import_module", fake_import_module)
    with pytest.raises(constants.S3VectorsUnavailableError):
        constants.import_s3vectors()


def test_check_source_dbs_passes_for_real_sqlite_dir() -> None:
    """The real ``_sqlite/`` directory satisfies the guard (both DBs present)."""
    constants.check_source_dbs(constants.SQLITE_SOURCE_DIR)


def test_check_source_dbs_raises_for_bogus_path(tmp_path: Path) -> None:
    """An empty directory (no DBs) trips :class:`MissingSourceDatabaseError`."""
    with pytest.raises(constants.MissingSourceDatabaseError):
        constants.check_source_dbs(tmp_path)


def test_check_source_dbs_raises_when_one_db_missing(tmp_path: Path) -> None:
    """Only ``movies.db`` present is still a stop condition (``ratings.db`` missing)."""
    (tmp_path / "movies.db").write_bytes(b"")
    with pytest.raises(constants.MissingSourceDatabaseError):
        constants.check_source_dbs(tmp_path)


def test_shared_layer_stages_under_python_prefix(tmp_path: Path) -> None:
    """The shared layer bundler stages movieintel + pydantic under ``python/``.

    A Lambda layer mounts at ``/opt`` and only ``/opt/python`` is on the import path, so
    the handler Lambdas can import ``movieintel``/``pydantic`` only if they live under a
    ``python/`` subtree. This guards the layer-wiring seam (REQ-X-6).
    """
    constants._stage_shared_layer(str(tmp_path))

    python_dir = tmp_path / "python"
    assert (python_dir / "movieintel" / "__init__.py").is_file()
    # pydantic (and its compiled core) are pip-installed into the same python/ subtree.
    assert (python_dir / "pydantic").is_dir()
    assert any(python_dir.glob("pydantic_core*"))


def test_sqlite_layer_stages_under_sqlite_prefix(tmp_path: Path) -> None:
    """The SQLite layer bundler stages the source DBs under ``sqlite/``.

    A Lambda layer mounts at ``/opt`` and the Extract handler reads ``SQLITE_DIR``
    (``/opt/sqlite``), so the source DBs resolve only if they live under a ``sqlite/``
    subtree. A flat copy would land them at ``/opt/movies.db`` and the handler would
    fail to open ``/opt/sqlite/movies.db``. This guards the layer-mount seam (REQ-X-6).
    """
    constants._stage_sqlite_layer(str(tmp_path))

    sqlite_dir = tmp_path / "sqlite"
    for name in constants.SOURCE_DB_FILENAMES:
        assert (sqlite_dir / name).is_file()
