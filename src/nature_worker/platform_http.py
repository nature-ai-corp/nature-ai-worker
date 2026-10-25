"""Minimal HTTP transport for the existing Platform S-C interface.

The actual endpoint paths come from the Platform's shared interface contract;
this client does NOT define a second API or invent route names. It does not
start a local enterprise Agent or persist server data.
"""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit
import httpx


class PlatformInterfaceError(RuntimeError):
    pass


@dataclass(frozen=True)
class ClientRoute:
    method: str
    path: str

    def __post_init__(self):
        if self.method not in ("GET", "POST"):
            raise ValueError("Unsupported client method")
        if not self.path.startswith("/") or self.path.startswith("//") or (
            "?" in self.path or "#" in self.path or "\\" in self.path
        ):
            raise ValueError("Route must be an absolute path without query/fragment")
        if any(segment in (".", "..") for segment in self.path.split("/")):
            raise ValueError("Route traversal rejected")


class PlatformHttpClient:
    def __init__(self, origin: str, *, timeout: float = 15.0,
                 transport: httpx.BaseTransport | None = None):
        parts = urlsplit(origin)
        if (parts.scheme != "https" or not parts.hostname or parts.username or
                parts.password or parts.query or parts.fragment or
                parts.path not in ("", "/")):
            raise PlatformInterfaceError("Platform origin must be an HTTPS origin")
        self.origin = origin.rstrip("/")
        self._http = httpx.Client(
            timeout=timeout, follow_redirects=False, trust_env=False,
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    def request(self, route: ClientRoute, *, session: str | None = None,
                payload: dict | None = None) -> dict:
        if payload is not None and route.method != "POST":
            raise PlatformInterfaceError("GET cannot carry a JSON body")
        headers = {"Accept": "application/json"}
        if session is not None:
            if not session or "\r" in session or "\n" in session:
                raise PlatformInterfaceError("Invalid session token")
            headers["Authorization"] = "Bearer " + session
        try:
            response = self._http.request(
                route.method, self.origin + route.path,
                headers=headers, json=payload,
            )
            response.raise_for_status()
            result = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise PlatformInterfaceError("Platform request failed") from exc
        if not isinstance(result, dict):
            raise PlatformInterfaceError("Platform response must be an object")
        return result
