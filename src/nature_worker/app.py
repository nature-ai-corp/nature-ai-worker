"""FastAPI application: JSON API plus the static web UI."""

from __future__ import annotations

import contextlib
import logging
import mimetypes
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__
from .agent import Agent, ConversationBusy
from .config import Config
from .credentials import CredentialStore
from .db import Database
from .providers import PROVIDERS, ProviderError, effective_endpoint, make_provider
from .tools import TOOLS, ToolContext, ToolError
from .tools.workspace import Workspace
from .templates import load_templates
from .work import summarize

log = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "static"
DEFAULT_MAX_STEPS = 20
PREVIEW_EXTENSIONS = (".md", ".txt", ".csv", ".json")
MAX_PREVIEW_BYTES = 1_000_000


class SettingsIn(BaseModel):
    provider: str
    model: str = Field(min_length=1, max_length=200)
    endpoint: str | None = Field(default=None, max_length=500)
    api_key: str | None = Field(default=None, max_length=1000)  # write-only; None/"" keeps the stored key
    clear_api_key: bool = False
    web_fetch_enabled: bool = False
    max_steps: int = Field(default=DEFAULT_MAX_STEPS, ge=1, le=100)


class TestIn(BaseModel):
    provider: str
    model: str = Field(min_length=1, max_length=200)
    endpoint: str | None = Field(default=None, max_length=500)
    api_key: str | None = Field(default=None, max_length=1000)


class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=100_000)


class ApprovalIn(BaseModel):
    approve: bool


def _validate_endpoint(endpoint: str | None) -> str | None:
    if not endpoint:
        return None
    parts = urlsplit(endpoint.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise HTTPException(400, "The endpoint must be an http:// or https:// URL.")
    return endpoint.strip().rstrip("/")


def create_app(config: Config | None = None) -> FastAPI:
    config = config or Config.from_env()
    config.ensure_dirs()
    db = Database(config.data_dir / "worker.db")
    creds = CredentialStore(config.data_dir)
    workspace = Workspace(config.workspace_dir)

    def settings() -> dict:
        s = db.get_settings()
        s.setdefault("provider", None)
        s.setdefault("model", "")
        s.setdefault("endpoint", None)
        s.setdefault("web_fetch_enabled", False)
        s.setdefault("max_steps", DEFAULT_MAX_STEPS)
        return s

    def build_provider(provider: str | None, model: str, endpoint: str | None, api_key: str | None = None):
        if not provider:
            from .providers import provider_error

            raise provider_error("config", "No model provider configured. Open Settings to add one.")
        key = api_key or creds.get(provider, effective_endpoint(provider, endpoint))[0]
        return make_provider(provider, model, key, endpoint)

    def current_provider():
        s = settings()
        return build_provider(s["provider"], s["model"], s["endpoint"])

    agent = Agent(
        db,
        provider_factory=current_provider,
        context_factory=lambda: ToolContext(workspace, bool(settings()["web_fetch_enabled"])),
        max_steps=lambda: int(settings()["max_steps"]),
    )

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        db.close()

    app = FastAPI(title="Nature AI Worker", version=__version__, docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.db, app.state.agent, app.state.config = db, agent, config
    if "*" not in config.allowed_hosts:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=config.allowed_hosts)

    @app.middleware("http")
    async def same_origin_only(request: Request, call_next):
        # Block cross-site requests (CSRF) to state-changing endpoints.
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin and urlsplit(origin).netloc != request.headers.get("host"):
                return JSONResponse({"detail": "Cross-origin request blocked."}, status_code=403)
        return await call_next(request)

    # -- UI -------------------------------------------------------------------

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})

    # -- settings ---------------------------------------------------------------

    def _key_status(p: str) -> str:
        return creds.get(p, creds.stored_endpoint(p))[1]  # whether any key exists, not for which endpoint

    def settings_view() -> dict:
        s = settings()
        provider = s["provider"]
        key_source = creds.get(provider, effective_endpoint(provider, s["endpoint"]))[1] if provider else "none"
        spec = PROVIDERS.get(provider or "", {})
        configured = bool(
            provider and s["model"] and (key_source != "none" or not spec.get("needs_key"))
            and (s["endpoint"] or not spec.get("needs_endpoint") or spec.get("default_endpoint"))
        )
        return {
            "provider": provider,
            "model": s["model"],
            "endpoint": s["endpoint"],
            "api_key_source": key_source,  # never the key itself
            "web_fetch_enabled": s["web_fetch_enabled"],
            "max_steps": s["max_steps"],
            "configured": configured,
            "workspace": str(config.workspace_dir),
            "providers": PROVIDERS,
            # Status only (never the key). For configurable endpoints, the endpoint a stored key is bound to.
            "key_sources": {p: _key_status(p) for p in PROVIDERS},
            "key_endpoints": {p: creds.stored_endpoint(p) for p in PROVIDERS if PROVIDERS[p]["needs_endpoint"]},
            "version": __version__,
        }

    @app.get("/api/settings")
    def get_settings():
        return settings_view()

    @app.put("/api/settings")
    def put_settings(body: SettingsIn):
        if body.provider not in PROVIDERS:
            raise HTTPException(400, "Unknown provider.")
        endpoint = _validate_endpoint(body.endpoint)
        if body.clear_api_key:
            creds.set(body.provider, None)
        elif body.api_key and body.api_key.strip():
            # The key is bound to the endpoint it is saved for.
            creds.set(body.provider, body.api_key.strip(), effective_endpoint(body.provider, endpoint))
        db.set_settings(
            {
                "provider": body.provider,
                "model": body.model.strip(),
                "endpoint": endpoint,
                "web_fetch_enabled": body.web_fetch_enabled,
                "max_steps": body.max_steps,
            }
        )
        return settings_view()

    @app.post("/api/settings/test")
    def test_settings(body: TestIn):
        """Make a real call to the provider and check that the model can call tools."""
        try:
            provider = build_provider(
                body.provider, body.model.strip(), _validate_endpoint(body.endpoint),
                (body.api_key or "").strip() or None,
            )
            result = provider.chat(
                "You are a connection test. Follow the instruction exactly.",
                [{"role": "user", "content": "Use the calculate tool to compute 6*7. Do not answer without calling the tool."}],
                [TOOLS["calculate"].schema()],
            )
        except ProviderError as e:
            return {"ok": False, "tools": False, "message": e.message}
        if any(tc.name == "calculate" for tc in result.tool_calls):
            return {"ok": True, "tools": True, "message": "Connected. The model called a tool successfully."}
        return {
            "ok": True,
            "tools": False,
            "message": "Connected, but the model answered without calling the tool. "
            "It may not support tool calling reliably; the agent works best with tool-capable models.",
        }

    # -- conversations ---------------------------------------------------------

    def conversation_or_404(conv_id: str) -> dict:
        conv = db.get_conversation(conv_id)
        if not conv:
            raise HTTPException(404, "Conversation not found.")
        return conv

    @app.get("/api/conversations")
    def list_conversations():
        return db.list_conversations()

    @app.post("/api/conversations")
    def create_conversation():
        return db.create_conversation()

    def work_summary(conv: dict, messages: list[dict]) -> dict:
        return summarize(conv, messages, db.list_approvals(conv["id"]), agent.is_running(conv["id"]), workspace)

    @app.get("/api/conversations/{conv_id}")
    def get_conversation(conv_id: str):
        conv = conversation_or_404(conv_id)
        messages = db.get_messages(conv_id)
        return {
            **conv,
            "messages": messages,
            "approvals": db.pending_approvals(conv_id),
            "running": agent.is_running(conv_id),
            "work": work_summary(conv, messages),
        }

    templates = load_templates()  # checked at startup, so a broken file fails early and clearly

    @app.get("/api/templates")
    def list_templates():
        """New Work templates (enabled only). The first one is the default."""
        return {"templates": templates, "default": templates[0]["id"]}

    @app.get("/api/work")
    def list_work():
        """My Work: every conversation with its derived status, newest activity first."""
        out = []
        for conv in db.list_conversations():
            w = work_summary(conv, db.get_messages(conv["id"]))
            out.append({**conv, "status": w["status"], "status_label": w["status_label"],
                        "instruction": w["instruction"], "deliverables": len(w["deliverables"]),
                        "approvals_pending": w["facts"]["approvals_pending"]})
        return out

    @app.delete("/api/conversations/{conv_id}")
    def delete_conversation(conv_id: str):
        conversation_or_404(conv_id)
        if agent.is_running(conv_id):
            raise HTTPException(409, "The agent is still working in this conversation.")
        db.delete_conversation(conv_id)
        return {"ok": True}

    @app.post("/api/conversations/{conv_id}/messages")
    def send_message(conv_id: str, body: MessageIn):
        conversation_or_404(conv_id)
        try:
            status = agent.send(conv_id, body.text)
        except ConversationBusy:
            raise HTTPException(409, "The agent is still working on the previous message.") from None
        return {"status": status}

    @app.post("/api/approvals/{approval_id}")
    def resolve_approval(approval_id: str, body: ApprovalIn):
        approval = db.get_approval(approval_id)
        if not approval:
            raise HTTPException(404, "Approval request not found.")
        try:
            status = agent.resolve_approval(approval, body.approve)
        except ConversationBusy:
            raise HTTPException(409, "The agent is busy. Try again in a moment.") from None
        if status == "stale":
            raise HTTPException(409, "This approval request was already answered.")
        return {"status": status}

    # -- workspace files ------------------------------------------------------------

    def resolve_or_400(path: str):
        try:
            return workspace.resolve(path)
        except ToolError as e:
            raise HTTPException(400, str(e)) from None

    @app.get("/api/files")
    def list_files():
        try:
            return {"root": str(config.workspace_dir), "files": workspace.list(".", recursive=True)}
        except ToolError as e:
            raise HTTPException(400, str(e)) from None

    @app.get("/api/files/content")
    def file_content(path: str):
        """Read-only text preview for the web UI. Other files can still be downloaded."""
        full = resolve_or_400(path)
        if not full.is_file():
            raise HTTPException(404, "File not found.")
        if full.suffix.lower() not in PREVIEW_EXTENSIONS:
            raise HTTPException(415, "Preview is available for .md, .txt, .csv and .json files. Use Download to open this file.")
        size = full.stat().st_size
        if size > MAX_PREVIEW_BYTES:
            raise HTTPException(
                413, f"This file is too large to preview ({size / 1_000_000:.1f} MB; the limit is 1 MB). Use Download to open it."
            )
        try:
            text = workspace.read_text(path)
        except ToolError as e:
            raise HTTPException(400, str(e)) from None
        return {"path": path, "content": text}

    @app.get("/api/files/download")
    def download(path: str):
        full = resolve_or_400(path)
        if not full.is_file():
            raise HTTPException(404, "File not found.")
        media = mimetypes.guess_type(full.name)[0] or "application/octet-stream"
        return FileResponse(full, media_type=media, filename=full.name)

    @app.put("/api/files")
    async def upload(path: str, request: Request, overwrite: bool = False):
        full = resolve_or_400(path)
        if full.exists() and not overwrite:
            raise HTTPException(409, f"'{path}' already exists.")
        data = await request.body()
        if len(data) > 20_000_000:
            raise HTTPException(413, "File too large (limit 20 MB).")
        if full == workspace.root or full.is_dir():
            raise HTTPException(400, "Choose a file name, not a folder.")
        full.parent.mkdir(parents=True, exist_ok=True)
        resolve_or_400(path)
        full.write_bytes(data)
        return {"ok": True, "path": path, "size": len(data)}

    @app.exception_handler(Exception)
    async def unexpected(request: Request, exc: Exception):
        log.exception("Unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse({"detail": "Unexpected server error. See the server log for details."}, status_code=500)

    @app.get("/api/health")
    def health():
        return {"ok": True, "version": __version__}

    return app


__all__ = ["create_app"]
