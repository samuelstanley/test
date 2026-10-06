import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(autouse=True)
def isolated_data_dirs(tmp_path, monkeypatch):
    """Archives, caches and outputs go to a temp folder in every test."""
    from latam_gas_snd import config
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "ARCHIVE_DIR", tmp_path / "data" / "archive")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "data" / "cache")
    monkeypatch.setattr(config, "ENARGAS_QUERY_CACHE", tmp_path / "data" / "enargas_query.json")
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path / "output")
    yield
