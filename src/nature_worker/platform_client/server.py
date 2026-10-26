"""Local web UI and API of the Platform Client process.

Only this process ever holds the Platform session cookies, and only in
memory. The browser receives a separate local UI cookie, issued after a real
Platform sign-in, so nothing else on this computer can drive the session
through the local port.
"""
from __future__ import annotations

import contextlib
import hashlib
import hmac
import os
import re
import secrets
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from ..installation_mode import ModeError
from ..linux_isolation import inspect_linux_isolation, linux_isolation_verified
from ..platform_contract import BUNDLED, SUPPORTED_VERSION, PlatformClientContract
from ..platform_http import (
    PlatformHttpClient, PlatformInterfaceError, PlatformProblem, SessionCookies, SessionExpired,
)
from ..platform_operations import (
    ClientInterface, CookieSessionTransport, PlatformOperations, new_idempotency_key,
)
from ..platform_progress import describe
from ..platform_runtime import check_platform_startup
from ..platform_session import PlatformSession, PlatformSessionError

STATIC_DIR = Path(__file__).with_name("static")
_MEDIA = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
          ".css": "text/css; charset=utf-8"}
UI_COOKIE = "nwpc_ui"
LOGIN_ATTEMPTS_PER_MINUTE = 5
_FILENAME = re.compile(r"[^A-Za-z0-9._ -]")


@dataclass
class ClientSettings:
    data_dir: Path
    platform_origin: str
    standalone_data_dir: Path
    standalone_uid: int | None = None
    allow_insecure_loopback: bool = False
    ca_bundle: str | None = None
    host: str = "127.0.0.1"
    port: int = 8090

    @classmethod
    def from_env(cls) -> "ClientSettings":
        def env(name, default=""):
            return os.environ.get(name, "").strip() or default
        return cls(
            data_dir=Path(env("NWPC_DATA_DIR", "/var/lib/nature-worker-platform-client")),
            platform_origin=env("NWPC_PLATFORM_ORIGIN"),
            standalone_data_dir=Path(env("NWPC_STANDALONE_DATA_DIR", "/nonexistent")),
            standalone_uid=int(env("NWPC_STANDALONE_UID")) if env("NWPC_STANDALONE_UID") else None,
            allow_insecure_loopback=env("NWPC_ALLOW_INSECURE_LOOPBACK") == "1",
            ca_bundle=env("NWPC_CA_BUNDLE") or None,
            host=env("NWPC_HOST", "127.0.0.1"),
            port=int(env("NWPC_PORT", "8090")),
        )


@dataclass
class Submission:
    conversation_id: str
    state: str = "running"   # running | done | error | unknown
    task_number: int | None = None
    execution_status: str | None = None
    error: str | None = None
    started_at: float = field(default_factory=time.time)


class Account:
    """The one signed-in Platform account of this process (memory only)."""

    def __init__(self, ops: PlatformOperations):
        self.transport = CookieSessionTransport(ops)
        self.session = PlatformSession(self.transport)
        self.ui_token: str | None = None
        self.submissions: dict[str, Submission] = {}
        self.lock = threading.RLock()

    def cookies(self) -> SessionCookies:
        with self.lock:
            if self.session.session is None:
                raise SessionExpired(401, "Not signed in")
            cookies = SessionCookies.from_token(self.session.session)
        if cookies.expired():
            self.drop("expired")
            raise SessionExpired(401, "Session expired")
        return cookies

    def drop(self, _reason: str) -> None:
        """The Platform ended the session (401/expiry): forget it locally."""
        with self.lock:
            self.session.session = None
            self.forget()

    def forget(self) -> None:
        """Discard every piece of this account's S-C state held in memory."""
        self.transport.user = None
        self.ui_token = None
        self.submissions.clear()


def startup_findings(settings: ClientSettings, contract_ok: bool):
    findings = inspect_linux_isolation(settings.data_dir, settings.standalone_data_dir,
                                       settings.standalone_uid)
    preflight = check_platform_startup(
        settings.data_dir,
        sc_runtime_available=contract_ok and bool(settings.platform_origin),
        os_isolation_verified=linux_isolation_verified(findings),
        # Linux sessions are memory-only: no credential is ever persisted.
        credential_backend_verified=sys.platform.startswith("linux"),
    )
    return preflight, findings


def compose_template_content(template: dict, values: dict, additional: str) -> str:
    """Compose a template submission as the instruction text the Platform receives."""
    lines = []
    for area in template.get("areas", []):
        value = values.get(str(area.get("slot")))
        if isinstance(value, list):
            value = ", ".join(str(v) for v in value if str(v).strip())
        value = (str(value) if value is not None else "").strip()
        if area.get("required") and not value:
            raise ValueError(f"'{area.get('label')}' is required")
        if value:
            lines.append(f"{area.get('label')}: {value}")
    extra = (additional or "").strip()
    if template.get("additional_instructions", {}).get("required") and not extra:
        raise ValueError("Additional instructions are required")
    if extra:
        label = template.get("additional_instructions", {}).get("label") or "Additional instructions"
        lines.append(f"{label}: {extra}")
    if not lines:
        raise ValueError("Enter the request")
    return "\n".join(lines)


class LoginIn(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=1024)


class SubmitIn(BaseModel):
    content: str | None = Field(default=None, max_length=50000)
    template_id: str | None = Field(default=None, max_length=256)
    values: dict[str, str | list[str]] = Field(default_factory=dict)
    additional: str = Field(default="", max_length=20000)
    conversation_id: str | None = Field(default=None, max_length=256)


class DecisionIn(BaseModel):
    approve: bool
    comment: str | None = Field(default=None, max_length=2000)


def load_ui_assets(directory: Path = STATIC_DIR) -> tuple[dict[str, tuple[bytes, str]], str]:
    """Read the UI once, at startup, so the page a process serves always matches
    the code answering its API - replacing files on disk cannot mix versions."""
    assets = {f.name: (f.read_bytes(), _MEDIA[f.suffix])
              for f in sorted(directory.iterdir()) if f.suffix in _MEDIA}
    digest = hashlib.sha256()
    for name, (content, _) in assets.items():
        digest.update(name.encode() + b"\0" + content)
    return assets, digest.hexdigest()[:16]


def create_app(settings: ClientSettings | None = None, *,
               transport=None, verify_startup: bool = True) -> FastAPI:
    settings = settings or ClientSettings.from_env()
    contract = PlatformClientContract.load(BUNDLED)
    if verify_startup:
        preflight, findings = startup_findings(settings, True)
        if not preflight.allowed:
            detail = "; ".join(f"{f.check}: {f.detail}" for f in findings if f.clean is not True)
            raise ModeError("Platform Client startup blocked: " + ", ".join(preflight.blockers)
                            + (f" ({detail})" if detail else ""))
    http = PlatformHttpClient(
        settings.platform_origin, transport=transport,
        allow_insecure_loopback=settings.allow_insecure_loopback,
        verify=settings.ca_bundle or True,
    )
    ops = PlatformOperations(http, contract)
    api = ClientInterface(ops)
    account = Account(ops)
    submissions = account.submissions
    login_times: list[float] = []

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        yield
        with contextlib.suppress(Exception):
            if account.session.session is not None or account.session.pending_revoke:
                account.session.sign_out()
        http.close()

    app = FastAPI(title="Nature AI Worker - Platform Client", docs_url=None, redoc_url=None,
                  openapi_url=None, lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware,
                       allowed_hosts=["localhost", "127.0.0.1", "[::1]", "testserver"])

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if request.method not in ("GET", "HEAD"):
            origin = request.headers.get("origin")
            if origin and urlsplit(origin).netloc != request.headers.get("host"):
                return JSONResponse({"detail": "Cross-origin request blocked."}, status_code=403)
        path = request.url.path
        if path.startswith("/api/") and path not in ("/api/status", "/api/login"):
            token = request.cookies.get(UI_COOKIE, "")
            with account.lock:
                expected = account.ui_token
            if not expected or not hmac.compare_digest(token, expected):
                return JSONResponse({"detail": "Sign in to the Platform."}, status_code=401)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        return response

    @app.exception_handler(SessionExpired)
    async def expired(_r, _exc):
        account.drop("platform-401")
        resp = JSONResponse({"detail": "The Platform session ended. Sign in again."}, status_code=401)
        resp.delete_cookie(UI_COOKIE)
        return resp

    @app.exception_handler(PlatformProblem)
    async def problem(_r, exc: PlatformProblem):
        status = exc.status if 400 <= exc.status < 600 else 502
        return JSONResponse({"detail": exc.detail or exc.title or "Platform error",
                             "title": exc.title, "correlation_id": exc.correlation_id},
                            status_code=status)

    @app.exception_handler(PlatformInterfaceError)
    async def unreachable(_r, _exc):
        return JSONResponse({"detail": "The Platform could not be reached."}, status_code=502)

    @app.exception_handler(PlatformSessionError)
    async def session_error(_r, exc):
        return JSONResponse({"detail": str(exc)}, status_code=502)

    assets, ui_build = load_ui_assets(STATIC_DIR)

    def asset(name: str) -> Response:
        if name not in assets:
            return JSONResponse({"detail": "Not found."}, status_code=404)
        content, media_type = assets[name]
        return Response(content, media_type=media_type)

    @app.get("/", include_in_schema=False)
    def index():
        return asset("index.html")

    @app.get("/static/{name}", include_in_schema=False)
    def static(name: str):
        return asset(name)

    @app.get("/api/status")
    def status(request: Request):
        with account.lock:
            user = account.transport.user
            signed_in = (account.session.session is not None and account.ui_token is not None
                         and hmac.compare_digest(request.cookies.get(UI_COOKIE, ""), account.ui_token))
            expires = (SessionCookies.from_token(account.session.session).expires_at
                       if signed_in else None)
            pending = account.session.pending_revoke is not None
        return {
            "signed_in": signed_in,
            "user": {"display_name": user.get("display_name"), "email": user.get("email")}
                    if signed_in and user else None,
            "session_expires_at": expires,
            "revocation_pending": pending,
            "platform": settings.platform_origin,
            "client_interface": SUPPORTED_VERSION,
            "contract_sha256": contract.digest,
            "ui_build": ui_build,
        }

    @app.post("/api/login")
    def login(body: LoginIn):
        now = time.time()
        login_times[:] = [t for t in login_times if now - t < 60]
        if len(login_times) >= LOGIN_ATTEMPTS_PER_MINUTE:
            return JSONResponse({"detail": "Too many sign-in attempts. Wait a minute."}, status_code=429)
        login_times.append(now)
        with account.lock:
            if account.session.pending_revoke is not None:
                account.session.retry_revocation()
            if account.session.session is not None:
                return JSONResponse({"detail": "Already signed in. Sign out first."}, status_code=409)
            account.transport.offer_password(body.email, body.password)
            try:
                account.session.sign_in()
            except SessionExpired:
                return JSONResponse({"detail": "Sign-in failed."}, status_code=401)
            finally:
                account.transport.withdraw()
            account.ui_token = secrets.token_urlsafe(32)
            token = account.ui_token
        resp = JSONResponse({"signed_in": True})
        resp.set_cookie(UI_COOKIE, token, httponly=True, samesite="strict", path="/")
        return resp

    @app.post("/api/logout")
    def logout():
        with account.lock:
            account.ui_token = None
            try:
                account.session.sign_out()
            except PlatformSessionError:
                account.forget()
                resp = JSONResponse({"detail": "Signed out locally, but the Platform did not confirm "
                                               "the session ended. Retry sign-out.",
                                     "revocation_pending": True}, status_code=502)
                resp.delete_cookie(UI_COOKIE)
                return resp
            account.forget()
        resp = Response(status_code=204)
        resp.delete_cookie(UI_COOKIE)
        return resp

    @app.post("/api/logout/retry")
    def retry_logout():
        account.session.retry_revocation()
        return {"revocation_pending": account.session.pending_revoke is not None}

    # -- Templates, Tasks, results, files, approvals --------------------------

    @app.get("/api/templates")
    def templates():
        return api.task_templates(account.cookies())

    @app.get("/api/tasks")
    def tasks(queue: str | None = None, search: str = "", task_state: str | None = None,
              page: int = 1):
        return api.my_tasks(account.cookies(), queue=queue, search=search or None,
                            task_state=task_state, page=page)

    @app.get("/api/tasks/{n}")
    def task(n: int):
        s = account.cookies()
        detail = api.task(s, n)
        history = api.work_history(s, n)
        approvals = api.approvals(s, n)
        return {"task": detail, "work_history": history, "approvals": approvals,
                "progress": describe(detail, history, approvals=approvals)}

    @app.get("/api/tasks/{n}/results")
    def results(n: int):
        return api.results(account.cookies(), n)

    @app.get("/api/tasks/{n}/results/{rid}")
    def result(n: int, rid: str):
        return api.result(account.cookies(), n, rid)

    @app.get("/api/tasks/{n}/files")
    def files(n: int):
        return api.files(account.cookies(), n)

    @app.get("/api/tasks/{n}/files/{fid}")
    def download(n: int, fid: str):
        s = account.cookies()
        listed = {f["file_id"]: f for f in api.files(s, n)}
        meta = listed.get(fid)
        if meta is None:
            return JSONResponse({"detail": "File not found."}, status_code=404)
        got = api.download(s, n, fid)
        name = _FILENAME.sub("_", meta.get("file_name") or "download")[:180] or "download"
        return Response(got.content, media_type="application/octet-stream", headers={
            "Content-Disposition": f'attachment; filename="{name}"',
        })

    @app.post("/api/tasks/{n}/approvals/{aid}")
    def decide(n: int, aid: str, body: DecisionIn):
        return api.decide(account.cookies(), n, aid, approve=body.approve, comment=body.comment)

    @app.post("/api/tasks/{n}/complete")
    def complete(n: int):
        return api.complete(account.cookies(), n)

    @app.post("/api/tasks/{n}/continue")
    def continue_task(n: int):
        return api.continue_in_conversation(account.cookies(), n)

    # -- Chat -------------------------------------------------------------------

    @app.get("/api/conversations")
    def conversations():
        return api.conversations(account.cookies())

    @app.get("/api/conversations/{cid}")
    def conversation(cid: str):
        s = account.cookies()
        conv = api.conversation(s, cid)
        task_view = api.conversation_task(s, cid) if conv.get("task_id") else None
        return {"conversation": conv, "messages": api.messages(s, cid), "task": task_view}

    def run_submission(sid: str, cookies: SessionCookies, content: str, start_task: bool,
                       template_id: str | None, key: str):
        sub = submissions.get(sid)
        if sub is None:
            return
        for attempt in range(2):
            try:
                out = api.post_message(cookies, sub.conversation_id, content=content,
                                       start_task=start_task, template_id=template_id,
                                       idempotency_key=key)
            except SessionExpired:
                account.drop("platform-401")
                sub.state, sub.error = "error", "The Platform session ended. Sign in again."
                return
            except PlatformProblem as exc:
                sub.state, sub.error = "error", exc.detail or exc.title or f"HTTP {exc.status}"
                return
            except PlatformInterfaceError:
                if attempt == 0:
                    continue  # same Idempotency-Key: a replay never repeats the work
                sub.state = "unknown"
                sub.error = ("No answer from the Platform yet. The request may still be "
                             "running - check My Tasks.")
                return
            sub.execution_status = out.get("execution_status")
            sub.task_number = (out.get("task") or {}).get("task_number")
            sub.state = "done"
            return

    @app.post("/api/submit")
    def submit(body: SubmitIn):
        s = account.cookies()
        start_task, template_id = False, None
        if body.template_id:
            template = next((t for t in api.task_templates(s) if t.get("id") == body.template_id), None)
            if template is None:
                return JSONResponse({"detail": "Template is not available."}, status_code=404)
            try:
                content = compose_template_content(template, body.values, body.additional)
            except ValueError as exc:
                return JSONResponse({"detail": str(exc)}, status_code=422)
            start_task, template_id = template.get("kind") == "task", template["id"]
        else:
            content = (body.content or "").strip()
            if not content:
                return JSONResponse({"detail": "Enter a message."}, status_code=422)
        cid = body.conversation_id
        if not cid:
            cid = api.create_conversation(s, idempotency_key=new_idempotency_key())["id"]
        cutoff = time.time() - 24 * 3600
        for old in [k for k, v in submissions.items() if v.started_at < cutoff]:
            submissions.pop(old, None)
        sid = secrets.token_urlsafe(12)
        submissions[sid] = Submission(conversation_id=cid)
        threading.Thread(target=run_submission, daemon=True,
                         args=(sid, s, content, start_task, template_id, new_idempotency_key())).start()
        return {"submission_id": sid, "conversation_id": cid}

    @app.get("/api/submissions/{sid}")
    def submission(sid: str):
        sub = submissions.get(sid)
        if sub is None:
            return JSONResponse({"detail": "Unknown submission."}, status_code=404)
        return {"state": sub.state, "conversation_id": sub.conversation_id,
                "task_number": sub.task_number, "execution_status": sub.execution_status,
                "error": sub.error, "elapsed_seconds": int(time.time() - sub.started_at)}

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    app.state.account = account
    app.state.submissions = submissions
    return app
