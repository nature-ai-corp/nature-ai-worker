# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
"""Strict loading of the Platform's Client Interface OpenAPI contract.

This is configuration validation, NOT a replacement or parallel API. The
operations, parameters and authentication come only from the OpenAPI document
the Platform publishes; anything it does not mark as Client Interface is
unreachable through the Worker.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .platform_http import ClientRoute

MARKER = "x-nature-client-interface"
SUPPORTED_VERSION = "1.3"
SESSION_COOKIE = "nature_session"
CSRF_COOKIE = "nature_csrf"
CSRF_HEADER = "X-CSRF-Token"
BUNDLED = Path(__file__).with_name("contracts") / "client-interface-v1.openapi.json"
_METHODS = ("get", "post", "put", "patch", "delete")


class ContractError(ValueError):
    pass


def _parameters(operation: dict) -> tuple[tuple, frozenset, frozenset]:
    path_params, query, headers = [], set(), set()
    for item in operation.get("parameters", []):
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            raise ContractError("Invalid operation parameter")
        where = item.get("in")
        if where == "path":
            kind = item.get("schema", {}).get("type")
            if kind not in ("integer", "string"):
                raise ContractError("Unsupported path parameter type")
            path_params.append((item["name"], kind))
        elif where == "query":
            query.add(item["name"])
        elif where == "header":
            headers.add(item["name"])
        else:
            raise ContractError("Unsupported parameter location")
    return tuple(path_params), frozenset(query), frozenset(headers)


def _check_authentication(document: dict) -> None:
    schemes = document.get("components", {}).get("securitySchemes", {})
    session = schemes.get("portalSession", {})
    csrf = schemes.get("csrfToken", {})
    if (session.get("in"), session.get("name")) != ("cookie", SESSION_COOKIE):
        raise ContractError("Contract session cookie does not match this client")
    if (csrf.get("in"), csrf.get("name")) != ("header", CSRF_HEADER):
        raise ContractError("Contract CSRF header does not match this client")
    if CSRF_COOKIE not in csrf.get("description", ""):
        raise ContractError("Contract CSRF cookie does not match this client")


class PlatformClientContract:
    def __init__(self, operations: dict[str, ClientRoute], *, digest: str = ""):
        if not operations:
            raise ContractError("An actual S-C operation contract is required")
        self._operations = dict(operations)
        self.digest = digest

    @classmethod
    def load(cls, path: Path = BUNDLED) -> "PlatformClientContract":
        raw = path.read_bytes()
        try:
            document = json.loads(raw)
        except ValueError as exc:
            raise ContractError("Contract is not JSON") from exc
        if not isinstance(document, dict) or not str(document.get("openapi", "")).startswith("3."):
            raise ContractError("Contract must be an OpenAPI 3 document")
        _check_authentication(document)
        parsed: dict[str, ClientRoute] = {}
        for path_template, item in document.get("paths", {}).items():
            for method in _METHODS:
                operation = item.get(method) if isinstance(item, dict) else None
                if not isinstance(operation, dict) or MARKER not in operation:
                    continue
                if operation[MARKER] != SUPPORTED_VERSION:
                    raise ContractError("Unsupported Client Interface version")
                name = operation.get("operationId")
                if not isinstance(name, str) or not name.isalnum() or name in parsed:
                    raise ContractError("Invalid or duplicate operationId")
                path_params, query, headers = _parameters(operation)
                security = [next(iter(s)) for s in operation.get("security", []) if s]
                parsed[name] = ClientRoute(
                    method.upper(), path_template, operation_id=name,
                    path_params=path_params, query_params=query, header_params=headers,
                    has_body="requestBody" in operation,
                    authenticated=bool(security),
                )
        return cls(parsed, digest=hashlib.sha256(raw).hexdigest())

    def resolve(self, operation: str) -> ClientRoute:
        try:
            return self._operations[operation]
        except KeyError as exc:
            raise ContractError("Operation not in verified S-C contract") from exc

    def names(self) -> frozenset[str]:
        return frozenset(self._operations)
