from __future__ import annotations

import contextvars
import re
from typing import Any

from fastapi import FastAPI
from starlette.middleware.base import BaseHTTPMiddleware

from .config import settings
from .file_intelligence import build_staged_ai_payload, parser_status
from .ollama import OllamaClient, OllamaError

_TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")
_ATTACHMENT_CONTEXT: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "wr_attachment_context",
    default=None,
)


class _AttachmentContextMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, *, session_resolver):
        super().__init__(app)
        self.session_resolver = session_resolver

    async def dispatch(self, request, call_next):
        state = None
        if request.method.upper() == "POST" and request.url.path == "/api/chat/send":
            raw = str(request.headers.get("X-WR-Attachment-Tokens") or "")
            tokens = [item.strip().lower() for item in raw.split(",") if item.strip()]
            tokens = [item for item in tokens[:5] if _TOKEN_RE.fullmatch(item)]
            if tokens:
                session = self.session_resolver(request)
                if session:
                    state = {
                        "user_key": str(session.get("user_key") or ""),
                        "tokens": tokens,
                    }
        marker = _ATTACHMENT_CONTEXT.set(state)
        try:
            return await call_next(request)
        finally:
            _ATTACHMENT_CONTEXT.reset(marker)


def _augment_messages(messages: list[dict], payload: dict[str, Any], *, include_images: bool = True) -> list[dict]:
    copied = [dict(row) for row in messages]
    target = None
    for row in reversed(copied):
        if str(row.get("role") or "") == "user":
            target = row
            break
    if target is None:
        return copied

    context = str(payload.get("context") or "")
    if context:
        original = str(target.get("content") or "")
        target["content"] = original.rstrip() + context

    images = payload.get("images") if isinstance(payload.get("images"), list) else []
    if include_images and images:
        target["images"] = [str(item) for item in images if item]
    return copied


def _patch_ollama_chat() -> None:
    if getattr(OllamaClient, "__wr_file_intelligence_patched__", False):
        return

    original_chat = OllamaClient.chat

    def intelligent_chat(self: OllamaClient, model: str, messages: list[dict]) -> str:
        state = _ATTACHMENT_CONTEXT.get()
        if not state or not state.get("user_key") or not state.get("tokens"):
            return original_chat(self, model, messages)

        payload = build_staged_ai_payload(
            data_dir=settings.data_dir,
            user_key=str(state["user_key"]),
            tokens=list(state["tokens"]),
        )
        enriched = _augment_messages(messages, payload, include_images=True)
        try:
            return original_chat(self, model, enriched)
        except OllamaError as exc:
            text = str(exc).lower()
            images = payload.get("images") if isinstance(payload.get("images"), list) else []
            image_error = bool(images) and any(
                key in text for key in ("image", "vision", "multimodal", "does not support", "unsupported")
            )
            if not image_error:
                raise
            fallback_payload = dict(payload)
            warning = (
                "\n\n[Vision note: selected model rejected image input. "
                "Image files remain attached, but this answer uses only readable text/metadata context.]"
            )
            fallback_payload["context"] = str(payload.get("context") or "") + warning
            fallback = _augment_messages(messages, fallback_payload, include_images=False)
            return original_chat(self, model, fallback)

    setattr(intelligent_chat, "__wr_file_intelligence_wrapper__", True)
    OllamaClient.chat = intelligent_chat  # type: ignore[assignment]
    OllamaClient.__wr_file_intelligence_patched__ = True  # type: ignore[attr-defined]


def install_chat_file_bridge(app: FastAPI, ctx: dict[str, Any]) -> dict[str, Any]:
    """Attach staged upload context to General AI at Ollama-call time only.

    The original user message saved in Chat History stays unchanged. The browser
    supplies opaque staged tokens in a request header; ownership is resolved
    again from the logged-in Web session and staged files are user-scoped.
    """
    if getattr(app.state, "wr_file_intelligence_installed", False):
        return {"ok": True, "already_installed": True, "parsers": parser_status()}

    session_resolver = ctx["_session"]
    _patch_ollama_chat()
    app.add_middleware(_AttachmentContextMiddleware, session_resolver=session_resolver)

    app.router.routes[:] = [
        route for route in app.router.routes
        if getattr(route, "path", None) != "/health/file-intelligence"
    ]

    @app.get("/health/file-intelligence")
    def file_intelligence_health():
        return {
            "ok": True,
            "installed": True,
            "parsers": parser_status(),
            "note": "General AI uses staged file context. Onsite remains Read-Only Preview and does not receive binary vision payloads.",
        }

    app.state.wr_file_intelligence_installed = True
    return {"ok": True, "parsers": parser_status()}
