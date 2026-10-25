"""Strict loading of routes supplied by the existing Platform S-C contract.

This is configuration validation, NOT a replacement or parallel API. The
actual Platform OpenAPI/contract must be supplied by the Platform project.
"""
from __future__ import annotations
import json
from pathlib import Path

from .platform_http import ClientRoute


class ContractError(ValueError):
    pass


class PlatformClientContract:
    def __init__(self, operations: dict[str, ClientRoute]):
        if not operations:
            raise ContractError("An actual S-C operation contract is required")
        self._operations = dict(operations)

    @classmethod
    def load(cls, path: Path) -> "PlatformClientContract":
        document = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict) or document.get("schema") != 1:
            raise ContractError("Unsupported S-C route manifest")
        ops = document.get("operations")
        if not isinstance(ops, dict) or not ops:
            raise ContractError("Missing S-C operations")
        parsed = {}
        for name, item in ops.items():
            if not isinstance(name, str) or not name or not name.replace("_", "").isalnum():
                raise ContractError("Invalid operation name")
            if not isinstance(item, dict) or set(item) != {"method", "path"}:
                raise ContractError("Invalid S-C operation definition")
            parsed[name] = ClientRoute(item["method"], item["path"])
        return cls(parsed)

    def resolve(self, operation: str) -> ClientRoute:
        try:
            return self._operations[operation]
        except KeyError as exc:
            raise ContractError("Operation not in verified S-C manifest") from exc
