"""Tiny JSON state file so a half-finished deployment can be re-run safely."""

from __future__ import annotations

import json
import pathlib
from typing import Any
from urllib.parse import urlparse


class State:
    def __init__(self, path: pathlib.Path, read_only: bool = False):
        self.path = path
        self.read_only = read_only
        self.data: dict[str, Any] = {}
        if path.exists():
            try:
                self.data = json.loads(path.read_text())
            except ValueError:
                self.data = {}

    @classmethod
    def for_env(cls, directory: str | pathlib.Path, org_url: str, solution: str, read_only: bool = False) -> "State":
        host = (urlparse(org_url if org_url.startswith("http") else "https://" + org_url).netloc or "env").split(".")[0]
        folder = pathlib.Path(directory)
        folder.mkdir(parents=True, exist_ok=True)
        return cls(folder / f"{host}.{solution}.json", read_only)

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value
        self.save()

    def done(self, key: str) -> bool:
        return bool(self.data.get(key))

    def save(self) -> None:
        if self.read_only:
            return
        self.path.write_text(json.dumps(self.data, indent=1, sort_keys=True))
