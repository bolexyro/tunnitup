from pathlib import Path

import pytest

from tunnitup.mappings import (
    MappingStore,
    MappingStoreError,
    SavedMapping,
    default_mapping_path,
)
from tunnitup.routing import Route


def test_mapping_store_round_trips_named_routes_with_duplicate_public_paths(
    tmp_path: Path,
) -> None:
    store = MappingStore(tmp_path / "mappings.toml")
    mappings = (
        SavedMapping("shop-ui", Route.parse("/=3000")),
        SavedMapping("admin-ui", Route.parse("/=5173")),
        SavedMapping("shop-api", Route.parse("/api=8000", strip_prefix=True)),
    )

    store.save(mappings)

    assert store.load() == mappings
    text = store.path.read_text(encoding="utf-8")
    assert "version = 1" in text
    assert not store.path.with_name(".mappings.toml.tmp").exists()


def test_mapping_store_returns_empty_catalog_when_file_is_missing(tmp_path: Path) -> None:
    assert MappingStore(tmp_path / "missing.toml").load() == ()


def test_mapping_store_rejects_duplicate_names_case_insensitively(tmp_path: Path) -> None:
    path = tmp_path / "mappings.toml"
    path.write_text(
        """
[[mappings]]
name = "api"
path = "/api"
upstream = 8000

[[mappings]]
name = "API"
path = "/other"
upstream = 9000
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(MappingStoreError, match="defined more than once"):
        MappingStore(path).load()


def test_mapping_store_rejects_invalid_mapping_names() -> None:
    with pytest.raises(MappingStoreError, match="may only contain"):
        SavedMapping("my api", Route.parse("/api=8000"))


def test_default_mapping_path_honors_config_home(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("TUNNITUP_CONFIG_HOME", str(tmp_path))

    assert default_mapping_path() == tmp_path / "mappings.toml"