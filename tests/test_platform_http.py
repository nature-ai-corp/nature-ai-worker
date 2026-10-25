import httpx
import pytest
from nature_worker.platform_http import ClientRoute, PlatformHttpClient, PlatformInterfaceError


def test_explicit_route_uses_auth_without_redirect():
    calls = []
    def handler(request):
        calls.append((request.url.path, request.headers.get("authorization")))
        return httpx.Response(200, json={"ok": True})
    client = PlatformHttpClient("https://platform.example", transport=httpx.MockTransport(handler))
    assert client.request(ClientRoute("GET", "/contract-provided-route"),
                          session="fake-session") == {"ok": True}
    assert calls == [("/contract-provided-route", "Bearer fake-session")]
    client.close()


def test_redirect_is_not_followed_or_sent_to_new_origin():
    calls = []
    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://other.example/steal"})
    client = PlatformHttpClient("https://platform.example", transport=httpx.MockTransport(handler))
    with pytest.raises(PlatformInterfaceError):
        client.request(ClientRoute("GET", "/route"), session="test-token")
    assert len(calls) == 1


@pytest.mark.parametrize("origin", [
    "http://platform.example", "https://u:p@platform.example",
    "https://platform.example/path", "https://platform.example?q=1",
])
def test_insecure_origins_rejected(origin):
    with pytest.raises(PlatformInterfaceError):
        PlatformHttpClient(origin)


@pytest.mark.parametrize("path", ["//other.example", "/a/../b", "/a?token=1", "relative"])
def test_unsafe_routes_rejected(path):
    with pytest.raises(ValueError):
        ClientRoute("GET", path)
