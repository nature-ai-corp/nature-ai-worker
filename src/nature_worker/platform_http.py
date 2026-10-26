# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
"""HTTP transport for the existing Platform S-C Client Interface.

The endpoint paths, parameters and authentication come from the Platform's
shared interface contract; this client does NOT define a second API or invent
route names. It does not start a local enterprise Agent or persist server data.

Authentication is the Platform's portal session: ``nature_session`` and
``nature_csrf`` cookies set by native login, with the CSRF cookie's value
repeated in ``X-CSRF-Token`` on every POST/PUT/PATCH/DELETE. Cookies are never
kept in a shared jar: they live only in a ``SessionCookies`` object, which
refuses to be printed, pickled or copied into logs.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from http.cookies import CookieError, SimpleCookie
from urllib.parse import quote, urlsplit

import httpx

SESSION_COOKIE = "nature_session"
CSRF_COOKIE = "nature_csrf"
CSRF_HEADER = "X-CSRF-Token"
MAX_SESSION_SECONDS = 8 * 3600
MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_FILE_BYTES = 64 * 1024 * 1024
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})
_MUTATING = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
_COOKIE_VALUE = re.compile(r"[\x21\x23-\x2b\x2d-\x3a\x3c-\x5b\x5d-\x7e]{1,4096}")
_IDEMPOTENCY_KEY = re.compile(r"[\x21-\x7e]{1,255}")


class PlatformInterfaceError(RuntimeError):
    pass


class PlatformProblem(PlatformInterfaceError):
    """An RFC 9457 answer from the Platform; carries no request content."""

    def __init__(self, status: int, title: str = "", detail: str = "",
                 code: str | None = None, correlation_id: str | None = None):
        self.status, self.title, self.detail = status, title, detail
        self.code, self.correlation_id = code, correlation_id
        super().__init__(f"Platform answered {status}: {title or 'error'}")


class SessionExpired(PlatformProblem):
    """401: the session is missing, invalid, revoked or expired."""


@dataclass(frozen=True)
class ClientRoute:
    method: str
    path: str
    operation_id: str = ""
    path_params: tuple[tuple[str, str], ...] = ()
    query_params: frozenset[str] = frozenset()
    header_params: frozenset[str] = frozenset()
    has_body: bool = False
    authenticated: bool = False

    def __post_init__(self):
        if self.method not in _METHODS:
            raise ValueError("Unsupported client method")
        if not self.path.startswith("/") or self.path.startswith("//") or (
            "?" in self.path or "#" in self.path or "\\" in self.path
        ):
            raise ValueError("Route must be an absolute path without query/fragment")
        if any(segment in (".", "..") for segment in self.path.split("/")):
            raise ValueError("Route traversal rejected")
        if self.path_params and (
            sorted(_PLACEHOLDER.findall(self.path)) != sorted(n for n, _ in self.path_params)
        ):
            raise ValueError("Route placeholders do not match its path parameters")

    @property
    def mutating(self) -> bool:
        return self.method in _MUTATING

    def render(self, values: dict | None) -> str:
        values = values or {}
        if set(values) != {name for name, _ in self.path_params}:
            raise PlatformInterfaceError("Path parameters do not match the contract")
        path = self.path
        for name, kind in self.path_params:
            value = values[name]
            if kind == "integer":
                if type(value) is not int or value < 0:
                    raise PlatformInterfaceError("Path parameter must be a non-negative integer")
                text = str(value)
            else:
                if not isinstance(value, str) or not value or len(value) > 256:
                    raise PlatformInterfaceError("Invalid path parameter")
                text = quote(value, safe="")
            path = path.replace("{" + name + "}", text)
        return path


class SessionCookies:
    """The two portal-session cookies plus their local expiry. Opaque by design."""

    __slots__ = ("_session", "_csrf", "expires_at")

    def __init__(self, session: str, csrf: str, expires_at: float):
        if not _COOKIE_VALUE.fullmatch(session or "") or not _COOKIE_VALUE.fullmatch(csrf or ""):
            raise PlatformInterfaceError("Platform did not set valid session cookies")
        self._session, self._csrf, self.expires_at = session, csrf, expires_at

    def __repr__(self) -> str:
        return "SessionCookies(<redacted>)"

    __str__ = __repr__

    def __reduce__(self):
        raise TypeError("Platform session cookies cannot be serialized")

    def expired(self, now: float | None = None) -> bool:
        return self._session == "" or (now if now is not None else time.time()) >= self.expires_at

    def headers(self, mutating: bool) -> dict[str, str]:
        if self._session == "":
            raise SessionExpired(401, "Signed out")
        headers = {"Cookie": f"{SESSION_COOKIE}={self._session}; {CSRF_COOKIE}={self._csrf}"}
        if mutating:
            headers[CSRF_HEADER] = self._csrf
        return headers

    def discard(self) -> None:
        # Python strings cannot be zeroed in place; drop the only references.
        self._session = self._csrf = ""
        self.expires_at = 0.0

    def to_token(self) -> str:
        """Encoding for PlatformSession and the Windows revocation journal only.

        ';' never occurs in a cookie value, so the framing is unambiguous.
        """
        return f"v1;{int(self.expires_at)};{self._session};{self._csrf}"

    @classmethod
    def from_token(cls, token: str) -> "SessionCookies":
        parts = token.split(";") if isinstance(token, str) else []
        if len(parts) != 4 or parts[0] != "v1" or not parts[1].isdigit():
            raise PlatformInterfaceError("Invalid Platform session token")
        return cls(parts[2], parts[3], float(parts[1]))


def session_from_response(response: "PlatformResponse", *, now: float | None = None) -> SessionCookies:
    """Read the session and CSRF cookies a successful native login set.

    The local expiry is the earliest of the cookies' Max-Age and eight hours;
    the Platform remains the authority and may end the session sooner (401).
    """
    values: dict[str, str] = {}
    lifetimes: list[int] = [MAX_SESSION_SECONDS]
    for header in response.set_cookies:
        jar = SimpleCookie()
        try:
            jar.load(header)
        except CookieError as exc:
            raise PlatformInterfaceError("Platform set an unreadable cookie") from exc
        for name in (SESSION_COOKIE, CSRF_COOKIE):
            if name in jar:
                values[name] = jar[name].value
                max_age = jar[name]["max-age"]
                if max_age.isdigit():
                    lifetimes.append(int(max_age))
    if set(values) != {SESSION_COOKIE, CSRF_COOKIE}:
        raise PlatformInterfaceError("Platform login did not set both session cookies")
    start = now if now is not None else time.time()
    return SessionCookies(values[SESSION_COOKIE], values[CSRF_COOKIE], start + min(lifetimes))


@dataclass
class PlatformResponse:
    status: int
    data: object = None
    content: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)
    set_cookies: tuple[str, ...] = ()


def _validate_origin(origin: str, allow_insecure_loopback: bool) -> str:
    parts = urlsplit(origin)
    if (not parts.hostname or parts.username or parts.password or parts.query or
            parts.fragment or parts.path not in ("", "/")):
        raise PlatformInterfaceError("Platform origin must be a bare origin")
    if parts.scheme == "https":
        return origin.rstrip("/")
    if parts.scheme == "http" and allow_insecure_loopback and parts.hostname in LOOPBACK_HOSTS:
        return origin.rstrip("/")
    raise PlatformInterfaceError(
        "Platform origin must be HTTPS (HTTP only for explicitly enabled local development on loopback)"
    )


def _problem(status: int, content_type: str, body: bytes) -> PlatformProblem:
    title = detail = ""
    code = correlation = None
    if content_type.split(";")[0].strip() in ("application/problem+json", "application/json") \
            and len(body) <= 65536:
        try:
            parsed = json.loads(body)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            def text(key):
                value = parsed.get(key)
                return value[:300] if isinstance(value, str) else None
            title, detail = text("title") or "", text("detail") or ""
            code, correlation = text("code"), text("correlation_id")
    kind = SessionExpired if status == 401 else PlatformProblem
    return kind(status, title, detail, code, correlation)


class PlatformHttpClient:
    def __init__(self, origin: str, *, timeout: float = 30.0,
                 transport: httpx.BaseTransport | None = None,
                 allow_insecure_loopback: bool = False,
                 verify: bool | str = True):
        self.origin = _validate_origin(origin, allow_insecure_loopback)
        self._http = httpx.Client(
            timeout=httpx.Timeout(timeout, connect=10.0), follow_redirects=False,
            trust_env=False, transport=transport, verify=verify,
        )

    def close(self) -> None:
        self._http.close()

    def request(self, route: ClientRoute, *, session: SessionCookies | None = None,
                path: dict | None = None, query: dict | None = None,
                payload: dict | None = None, idempotency_key: str | None = None,
                binary: bool = False, read_timeout: float | None = None) -> PlatformResponse:
        if payload is not None and not route.has_body:
            raise PlatformInterfaceError("Operation takes no request body")
        unknown = set(query or {}) - route.query_params
        if unknown:
            raise PlatformInterfaceError("Query parameter not in the contract")
        headers = {"Accept": "application/octet-stream" if binary else "application/json"}
        if session is not None:
            if session.expired():
                raise SessionExpired(401, "Session expired")
            headers.update(session.headers(route.mutating))
        if idempotency_key is not None:
            if "Idempotency-Key" not in route.header_params or not _IDEMPOTENCY_KEY.fullmatch(idempotency_key):
                raise PlatformInterfaceError("Invalid Idempotency-Key")
            headers["Idempotency-Key"] = idempotency_key
        params = {k: v for k, v in (query or {}).items() if v is not None}
        timeout = None if read_timeout is None else httpx.Timeout(read_timeout, connect=10.0)
        try:
            with self._http.stream(
                route.method, self.origin + route.render(path), headers=headers,
                params=params, json=payload,
                **({"timeout": timeout} if timeout is not None else {}),
            ) as response:
                status, response_headers = response.status_code, response.headers
                limit = MAX_FILE_BYTES if binary and response.is_success else MAX_JSON_BYTES
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > limit:
                        raise PlatformInterfaceError("Platform response too large")
        except httpx.HTTPError as exc:
            raise PlatformInterfaceError("Platform request failed") from exc
        finally:
            # Never let a shared jar hold or replay session cookies.
            self._http.cookies.clear()
        content = bytes(body)
        if 300 <= status < 400:
            raise PlatformInterfaceError("Platform redirect refused")
        if not 200 <= status < 300:
            raise _problem(status, response_headers.get("content-type", ""), content)
        result = PlatformResponse(status, set_cookies=tuple(response_headers.get_list("set-cookie")),
                                  headers={
            k: v for k, v in response_headers.items()
            if k.lower() in ("content-type", "content-disposition", "content-length",
                             "x-request-id", "idempotent-replayed")
        })
        if binary:
            result.content = content
        elif status != 204 and content:
            try:
                result.data = json.loads(content)
            except ValueError as exc:
                raise PlatformInterfaceError("Platform response is not JSON") from exc
        return result
