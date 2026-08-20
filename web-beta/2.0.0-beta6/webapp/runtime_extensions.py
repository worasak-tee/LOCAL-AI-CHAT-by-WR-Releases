from __future__ import annotations

from typing import Any

from fastapi import FastAPI


def install_runtime_extensions() -> dict[str, Any]:
    """Install optional Web extensions onto the canonical webapp.main:app.

    This function is called explicitly by both the Windows Service and the
    manual run_web entrypoint before Uvicorn starts. The canonical ASGI target
    remains webapp.main:app.
    """
    from webapp import main as main_module

    app: FastAPI = main_module.app
    errors: list[str] = []

    try:
        from webapp.update_manager import install_update_routes

        install_update_routes(app, vars(main_module))
    except Exception as exc:  # keep host alive, but expose failure via health/extensions
        errors.append(f"update_manager: {type(exc).__name__}: {exc}")

    paths = {str(getattr(route, "path", "")) for route in app.router.routes}
    update_ready = "/api/update/check" in paths and "/api/update/install" in paths
    attachment_ready = (
        "/api/attachments/stage" in paths
        and "/api/attachments/commit" in paths
        and "/api/attachments/{attachment_id}/preview" in paths
    )

    # Replace any older diagnostic route so restarts/hotfixes stay idempotent.
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
        return {
            "ok": bool(current_update and current_attachment and not errors),
            "update": current_update,
            "attachment": current_attachment,
            "errors": errors,
        }

    return {
        "ok": bool(update_ready and attachment_ready and not errors),
        "update": update_ready,
        "attachment": attachment_ready,
        "errors": errors,
    }
