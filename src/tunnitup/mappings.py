from __future__ import annotations

import json
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tunnitup.routing import Route, RouteConfigurationError


class MappingStoreError(ValueError):
    """Raised when the saved mapping catalog cannot be read or written."""


@dataclass(frozen=True, slots=True)
class SavedMapping:
    name: str
    route: Route

    def __post_init__(self) -> None:
        name = self.name.strip()
        if not name:
            raise MappingStoreError("mapping name cannot be empty")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name):
            raise MappingStoreError(
                "mapping name may only contain letters, numbers, dots, dashes, and underscores"
            )
        object.__setattr__(self, "name", name)


def default_mapping_path() -> Path:
    override = os.environ.get("TUNNITUP_CONFIG_HOME")
    if override:
        return Path(override).expanduser() / "mappings.toml"
    if os.name == "nt":
        root = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    else:
        root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return root / "tunnitup" / "mappings.toml"


class MappingStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    @classmethod
    def default(cls) -> MappingStore:
        return cls(default_mapping_path())

    def load(self) -> tuple[SavedMapping, ...]:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ()
        except OSError as exc:
            raise MappingStoreError(f"could not read saved mappings: {exc}") from exc
        try:
            raw = tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            raise MappingStoreError(f"invalid TOML in {self.path}: {exc}") from exc
        if set(raw) - {"version", "mappings"}:
            raise MappingStoreError("saved mappings contain unknown top-level fields")
        if raw.get("version", 1) != 1:
            raise MappingStoreError("saved mapping catalog uses an unsupported version")
        items = raw.get("mappings", [])
        if not isinstance(items, list):
            raise MappingStoreError("mappings must be an array of tables")

        mappings: list[SavedMapping] = []
        for index, item in enumerate(items, start=1):
            if not isinstance(item, dict):
                raise MappingStoreError(f"mapping {index} must be a table")
            mappings.append(self._parse_mapping(item, index))
        self._validate_unique_names(mappings)
        return tuple(mappings)

    def save(self, mappings: tuple[SavedMapping, ...]) -> None:
        self._validate_unique_names(list(mappings))
        lines = ["version = 1", ""]
        for mapping in mappings:
            route = mapping.route
            lines.extend(
                [
                    "[[mappings]]",
                    f"name = {json.dumps(mapping.name)}",
                    f"path = {json.dumps(route.path)}",
                    f"upstream = {json.dumps(str(route.upstream))}",
                    f"strip_prefix = {'true' if route.strip_prefix else 'false'}",
                    "",
                ]
            )
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(f".{self.path.name}.tmp")
            temporary.write_text("\n".join(lines), encoding="utf-8")
            temporary.replace(self.path)
        except OSError as exc:
            raise MappingStoreError(f"could not save mappings: {exc}") from exc

    @staticmethod
    def _parse_mapping(item: dict[str, Any], index: int) -> SavedMapping:
        unknown = set(item) - {"name", "path", "upstream", "strip_prefix"}
        if unknown:
            raise MappingStoreError(f"mapping {index} contains unknown fields")
        name = item.get("name")
        path = item.get("path")
        upstream = item.get("upstream")
        strip_prefix = item.get("strip_prefix", False)
        if not isinstance(name, str) or not isinstance(path, str):
            raise MappingStoreError(f"mapping {index} requires string name and path fields")
        if isinstance(upstream, bool) or not isinstance(upstream, str | int):
            raise MappingStoreError(f"mapping {index} upstream must be a port or URL")
        if not isinstance(strip_prefix, bool):
            raise MappingStoreError(f"mapping {index} strip_prefix must be true or false")
        try:
            route = Route.parse(f"{path}={upstream}", strip_prefix=strip_prefix)
            return SavedMapping(name, route)
        except RouteConfigurationError as exc:
            raise MappingStoreError(f"mapping {index}: {exc}") from exc

    @staticmethod
    def _validate_unique_names(mappings: list[SavedMapping]) -> None:
        names = [mapping.name.casefold() for mapping in mappings]
        if len(names) != len(set(names)):
            duplicate = next(name for name in names if names.count(name) > 1)
            raise MappingStoreError(f"mapping name {duplicate!r} is defined more than once")


def suggested_mapping_name(route: Route, existing: tuple[SavedMapping, ...] = ()) -> str:
    base = route.path.strip("/").replace("/", "-") or "root"
    if route.upstream.port:
        base = f"{base}-{route.upstream.port}"
    used = {mapping.name.casefold() for mapping in existing}
    candidate = base
    suffix = 2
    while candidate.casefold() in used:
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate
