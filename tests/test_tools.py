"""Built-in data tools and web fetch safeguards."""

import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from nature_worker.tools import ToolContext, ToolError, data, execute, web
from nature_worker.tools.workspace import Workspace


@pytest.fixture
def ws(tmp_path):
    return Workspace(tmp_path)


# -- text / data tools --------------------------------------------------------------

def test_table_summary_csv_and_json(ws, tmp_path):
    (tmp_path / "t.csv").write_text("name,qty,price\na,1,2.5\nb,3,\nc,5,1.5\n")
    out = data.table_summary(ws, {"path": "t.csv"})
    assert "Rows: 3" in out
    assert "qty: numeric, count=3, missing=0, sum=9" in out
    assert "price: numeric, count=2, missing=1, sum=4" in out
    assert "name: text, distinct=3" in out
    (tmp_path / "t.json").write_text('{"items": [{"x": 1}, {"x": 2}]}')
    assert "x: numeric, count=2, missing=0, sum=3" in data.table_summary(ws, {"path": "t.json"})


def test_search_text(ws, tmp_path):
    (tmp_path / "a.md").write_text("hello\nInvoice 42 due\n")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.txt").write_text("another invoice\n")
    (tmp_path / "bin.dat").write_bytes(b"\x00\x01invoice")
    out = data.search_text(ws, {"query": "invoice"})
    assert "a.md:2: Invoice 42 due" in out and "sub/b.txt:1: another invoice" in out
    assert "bin.dat" not in out
    assert data.search_text(ws, {"query": r"\d+ due", "regex": True}).startswith("a.md:2")


@pytest.mark.parametrize("expr,expected", [("2+3*4", "14"), ("(120.5+99)*0.2", "43.9"), ("sqrt(16)+max(1,7)", "11.0"), ("2**10", "1024")])
def test_calculate(ws, expr, expected):
    assert data.calculate(ws, {"expression": expr}).endswith(expected)


@pytest.mark.parametrize("expr", ["__import__('os').system('id')", "open('x')", "().__class__", "9**9**9", "1/0", "a"])
def test_calculate_rejects_unsafe_or_invalid(ws, expr):
    with pytest.raises(ToolError):
        data.calculate(ws, {"expression": expr})


def test_read_long_file_in_chunks(ws, tmp_path):
    (tmp_path / "big.txt").write_text("x" * 150_000)
    first = execute(ToolContext(ws), "read_file", {"path": "big.txt"})
    assert "offset=100000" in first
    second = execute(ToolContext(ws), "read_file", {"path": "big.txt", "offset": 100000})
    assert second == "x" * 50_000


def test_binary_file_is_refused(ws, tmp_path):
    (tmp_path / "img.png").write_bytes(b"\x89PNG\x00\x00binary")
    with pytest.raises(ToolError, match="binary"):
        execute(ToolContext(ws), "read_file", {"path": "img.png"})


# -- web fetch safeguards ---------------------------------------------------------------

@pytest.mark.parametrize("ip,public", [
    ("127.0.0.1", False), ("10.1.2.3", False), ("172.16.0.1", False), ("192.168.1.1", False),
    ("169.254.169.254", False), ("100.64.0.1", False), ("0.0.0.0", False), ("::1", False),
    ("fe80::1", False), ("fc00::1", False), ("::ffff:127.0.0.1", False), ("224.0.0.1", False),
    ("8.8.8.8", True), ("2606:4700:4700::1111", True),
])
def test_is_public_ip(ip, public):
    assert web.is_public_ip(ip) is public


@pytest.fixture
def local_http():
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path.startswith("/redirect"):
                self.send_response(302)
                self.send_header("Location", self.server.redirect_to)
                self.end_headers()
                return
            body = b"<html><head><title>T</title><script>evil()</script></head><body><p>internal data</p></body></html>"
            self.send_response(200)
            self.send_header("content-type", "text/html")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost"])
def test_fetch_of_local_service_is_blocked(ws, local_http, host):
    url = f"http://{host}:{local_http.server_address[1]}/"
    with pytest.raises(ToolError, match="local or private"):
        web.web_fetch(ws, {"url": url})


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.com/x", "http://user:pw@example.com/", "javascript:alert(1)"])
def test_fetch_rejects_bad_urls(ws, url):
    with pytest.raises(ToolError):
        web.web_fetch(ws, {"url": url})


def test_dns_rebinding_and_redirect_to_private_are_blocked(ws, local_http, monkeypatch):
    port = local_http.server_address[1]
    real_getaddrinfo = socket.getaddrinfo
    fake_dns = {"public.test": "127.0.0.1", "internal.test": "10.0.0.5"}

    def getaddrinfo(host, *args, **kwargs):
        if host in fake_dns:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (fake_dns[host], args[0]))]
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    # Pretend 127.0.0.1 is a public address so "public.test" is reachable in this test.
    monkeypatch.setattr(web, "is_public_ip", lambda ip: ip == "127.0.0.1")

    out = web.web_fetch(ws, {"url": f"http://public.test:{port}/"})
    assert "internal data" in out and "evil()" not in out and "Title: T" in out
    assert out.startswith("<untrusted_web_content>")

    local_http.redirect_to = f"http://internal.test:{port}/"
    with pytest.raises(ToolError, match="local or private"):
        web.web_fetch(ws, {"url": f"http://public.test:{port}/redirect"})


def _online() -> bool:
    try:
        socket.create_connection(("example.com", 443), timeout=3).close()
        return True
    except OSError:
        return False


@pytest.mark.skipif(not _online(), reason="no internet access")
def test_fetch_public_page(ws):
    out = web.web_fetch(ws, {"url": "https://example.com/"})
    assert "Example Domain" in out
