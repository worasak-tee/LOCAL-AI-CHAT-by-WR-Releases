from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse


class _SystemAdminSetupGuard(BaseHTTPMiddleware):
    """Keep System Setup writes separate from AI permission levels."""

    def __init__(self, app, *, session_resolver):
        super().__init__(app)
        self.session_resolver = session_resolver

    async def dispatch(self, request, call_next):
        if request.method.upper() == "POST" and request.url.path in {"/api/setup", "/api/setup/test"}:
            session = self.session_resolver(request)
            if not session:
                return JSONResponse({"detail": "Login required"}, status_code=401)
            if str(session.get("role") or "").strip().lower() != "admin":
                return JSONResponse(
                    {"detail": "System Administrator role is required to change LLM Server / Setup"},
                    status_code=403,
                )
        return await call_next(request)


def _system_admin_only(session: dict[str, Any]) -> bool:
    return str(session.get("role") or "").strip().lower() == "admin"


def install_runtime_extensions() -> dict[str, Any]:
    """Install required Web runtime extensions on canonical webapp.main:app.

    Called by Windows Service and manual run_web before Uvicorn starts.
    The canonical ASGI target remains webapp.main:app.
    """
    from webapp import main as main_module

    app: FastAPI = main_module.app
    errors: list[str] = []
    file_intelligence: dict[str, Any] = {"ok": False, "parsers": {}}

    main_module._can_manage_setup = _system_admin_only

    try:
        from webapp.update_manager import install_update_routes
        install_update_routes(app, vars(main_module))
    except Exception as exc:
        errors.append(f"update_manager: {type(exc).__name__}: {exc}")

    optional_errors = vars(main_module).get("_wr_optional_extension_errors")
    if isinstance(optional_errors, list):
        errors.extend(str(item) for item in optional_errors if str(item).strip())

    try:
        from webapp.chat_file_bridge import install_chat_file_bridge
        file_intelligence = install_chat_file_bridge(app, vars(main_module))
    except Exception as exc:
        errors.append(f"file_intelligence: {type(exc).__name__}: {exc}")

    if not getattr(app.state, "wr_system_admin_guard_installed", False):
        app.add_middleware(_SystemAdminSetupGuard, session_resolver=main_module._session)
        app.state.wr_system_admin_guard_installed = True

    paths = {str(getattr(route, "path", "")) for route in app.router.routes}
    update_ready = "/api/update/check" in paths and "/api/update/install" in paths
    attachment_ready = (
        "/api/attachments/stage" in paths
        and "/api/attachments/commit" in paths
        and "/api/attachments/{attachment_id}/preview" in paths
    )
    profile_ready = "/api/profile/photo" in paths
    parsers = file_intelligence.get("parsers") if isinstance(file_intelligence, dict) else {}
    pdf_ready = isinstance(parsers, dict) and str(parsers.get("pdf") or "").lower() not in {"", "unavailable", "error"}
    intelligence_ready = (
        "/health/file-intelligence" in paths
        and bool(file_intelligence.get("ok"))
        and pdf_ready
    )
    if not pdf_ready:
        errors.append("file_intelligence: PDF parser is not available")

    app.router.routes[:] = [
        route for route in app.router.routes if getattr(route, "path", None) != "/health/extensions"
    ]

    @app.get("/health/extensions")
    def health_extensions():
        current_paths = {str(getattr(route, "path", "")) for route in app.router.routes}
        current_update = "/api/update/check" in current_paths and "/api/update/install" in current_paths
        current_attachment = (
            "/api/attachments/stage" in current_paths
            and "/api/attachments/commit" in current_paths
            and "/api/attachments/{attachment_id}/preview" in current_paths
        )
        current_profile = "/api/profile/photo" in current_paths
        current_intelligence = "/health/file-intelligence" in current_paths and pdf_ready
        return {
            "ok": bool(current_update and current_attachment and current_profile and current_intelligence and not errors),
            "update": current_update,
            "attachment": current_attachment,
            "profile": current_profile,
            "file_intelligence": current_intelligence,
            "parsers": parsers,
            "errors": errors,
        }

    return {
        "ok": bool(update_ready and attachment_ready and profile_ready and intelligence_ready and not errors),
        "update": update_ready,
        "attachment": attachment_ready,
        "profile": profile_ready,
        "file_intelligence": intelligence_ready,
        "parsers": parsers,
        "errors": errors,
    }
