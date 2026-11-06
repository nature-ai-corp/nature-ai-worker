# SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
# SPDX-License-Identifier: Apache-2.0
import pickle

import httpx
import pytest
from nature_worker.platform_http import (
    ClientRoute, PlatformHttpClient, PlatformInterfaceError, PlatformProblem, SessionCookies,
    SessionExpired, session_from_response, PlatformResponse,
)

FAR = 4_000_000_000.0


def cookies():
    return SessionCookies("sess-value", "csrf-value", FAR)


def client(handler, **kw):
    return PlatformHttpClient("https://platform.example", transport=httpx.MockTransport(handler), **kw)


def test_get_sends_session_cookie_but_no_csrf_header():
    seen = []
    def handler(request):
        seen.append((request.url.path, request.headers.get("cookie"), request.headers.get("x-csrf-token"),
                     request.headers.get("authorization")))
        return httpx.Response(200, json={"ok": True})
    c = client(handler)
    assert c.request(ClientRoute("GET", "/route"), session=cookies()).data == {"ok": True}
    assert seen == [("/route", "nature_session=sess-value; nature_csrf=csrf-value", None, None)]
    c.close()


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE"])
def test_mutations_repeat_csrf_cookie_in_header(method):
    seen = []
    def handler(request):
        seen.append(request.headers.get("x-csrf-token"))
        return httpx.Response(204)
    c = client(handler)
    assert c.request(ClientRoute(method, "/route"), session=cookies()).status == 204
    assert seen == ["csrf-value"]


def test_set_cookie_is_never_kept_in_a_shared_jar():
    calls = []
    def handler(request):
        calls.append(request.headers.get("cookie"))
        return httpx.Response(200, json={}, headers={"set-cookie": "nature_session=new; Path=/"})
    c = client(handler)
    c.request(ClientRoute("GET", "/a"))
    c.request(ClientRoute("GET", "/a"))
    assert calls == [None, None]


def test_redirect_is_not_followed_or_sent_to_new_origin():
    calls = []
    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://other.example/steal"})
    c = client(handler)
    with pytest.raises(PlatformInterfaceError):
        c.request(ClientRoute("GET", "/route"), session=cookies())
    assert len(calls) == 1


def test_problem_details_are_typed_and_401_is_session_expired():
    def handler(request):
        status = 401 if request.url.path == "/me" else 409
        return httpx.Response(status, json={"type": "about:blank", "title": "T", "status": status,
                                            "detail": "D", "correlation_id": "c-1"},
                              headers={"content-type": "application/problem+json"})
    c = client(handler)
    with pytest.raises(SessionExpired):
        c.request(ClientRoute("GET", "/me"), session=cookies())
    with pytest.raises(PlatformProblem) as info:
        c.request(ClientRoute("POST", "/x"), session=cookies())
    assert (info.value.status, info.value.detail, info.value.correlation_id) == (409, "D", "c-1")


def test_expired_or_discarded_session_is_not_sent():
    calls = []
    c = client(lambda r: calls.append(r) or httpx.Response(200, json={}))
    with pytest.raises(SessionExpired):
        c.request(ClientRoute("GET", "/x"), session=SessionCookies("s", "c", 1.0))
    live = cookies()
    live.discard()
    with pytest.raises(SessionExpired):
        c.request(ClientRoute("GET", "/x"), session=live)
    assert calls == []


def test_session_cookies_are_redacted_and_not_serializable():
    c = cookies()
    assert "sess-value" not in repr(c) and "csrf-value" not in str(c)
    with pytest.raises(TypeError):
        pickle.dumps(c)
    again = SessionCookies.from_token(c.to_token())
    assert again.headers(True) == c.headers(True)


def test_login_cookies_parsed_and_lifetime_capped_at_eight_hours():
    resp = PlatformResponse(200, set_cookies=(
        "nature_session=abc.def; HttpOnly; Max-Age=999999; Path=/; SameSite=lax; Secure",
        "nature_csrf=xyz; Max-Age=999999; Path=/; SameSite=lax; Secure",
    ))
    s = session_from_response(resp, now=1000.0)
    assert s.expires_at == 1000.0 + 8 * 3600
    shorter = session_from_response(PlatformResponse(200, set_cookies=(
        "nature_session=a; Max-Age=60", "nature_csrf=b; Max-Age=60")), now=0.0)
    assert shorter.expires_at == 60
    with pytest.raises(PlatformInterfaceError):
        session_from_response(PlatformResponse(200, set_cookies=("nature_session=a",)))


def test_response_size_is_bounded(monkeypatch):
    import nature_worker.platform_http as h
    monkeypatch.setattr(h, "MAX_JSON_BYTES", 10)
    c = client(lambda r: httpx.Response(200, content=b"[" + b"1," * 50 + b"1]"))
    with pytest.raises(PlatformInterfaceError):
        c.request(ClientRoute("GET", "/big"))


@pytest.mark.parametrize("origin", [
    "http://platform.example", "http://100.101.102.103:8000", "https://u:p@platform.example",
    "https://platform.example/path", "https://platform.example?q=1",
])
def test_insecure_origins_rejected(origin):
    with pytest.raises(PlatformInterfaceError):
        PlatformHttpClient(origin)


def test_http_only_for_explicit_loopback_development():
    with pytest.raises(PlatformInterfaceError):
        PlatformHttpClient("http://127.0.0.1:8000")
    with pytest.raises(PlatformInterfaceError):
        PlatformHttpClient("http://100.101.102.103:8000", allow_insecure_loopback=True)
    PlatformHttpClient("http://127.0.0.1:8000", allow_insecure_loopback=True).close()


@pytest.mark.parametrize("path", ["//other.example", "/a/../b", "/a?token=1", "relative"])
def test_unsafe_routes_rejected(path):
    with pytest.raises(ValueError):
        ClientRoute("GET", path)


def test_path_parameters_are_typed_and_encoded():
    r = ClientRoute("GET", "/t/{n}/r/{rid}", path_params=(("n", "integer"), ("rid", "string")))
    assert r.render({"n": 5, "rid": "a/../b"}) == "/t/5/r/a%2F..%2Fb"
    for bad in ({"n": "5", "rid": "x"}, {"n": True, "rid": "x"}, {"n": 1}, {"n": 1, "rid": ""}):
        with pytest.raises(PlatformInterfaceError):
            r.render(bad)


def test_unknown_query_parameter_and_bad_idempotency_key_rejected():
    c = client(lambda r: httpx.Response(200, json={}))
    with pytest.raises(PlatformInterfaceError):
        c.request(ClientRoute("GET", "/x", query_params=frozenset({"a"})), query={"b": 1})
    with pytest.raises(PlatformInterfaceError):
        c.request(ClientRoute("POST", "/x", header_params=frozenset({"Idempotency-Key"})),
                  idempotency_key="has space")
