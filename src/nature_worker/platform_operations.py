"""Bind only explicitly supplied Platform contract operations to the HTTP client.

The real shared S-C contract must be supplied by the Platform, not guessed
by the Worker. No local enterprise Agent or business API is created.
"""
from __future__ import annotations

from .platform_contract import PlatformClientContract
from .platform_http import PlatformHttpClient


class PlatformOperations:
    def __init__(self, client: PlatformHttpClient, contract: PlatformClientContract):
        self.client = client
        self.contract = contract

    def execute(self, operation: str, *, session: str | None = None,
                payload: dict | None = None) -> dict:
        route = self.contract.resolve(operation)
        return self.client.request(route, session=session, payload=payload)
