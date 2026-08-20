from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request


MANIFEST_URL = os.getenv(
    "WR_WEB_UPDATE_MANIFEST",
    "https://raw.githubusercontent.com/worasak-tee/LOCAL-AI-CHAT-by-WR-Releases/main/web-beta/manifest.json",
).strip()
PRODUCT = "LOCAL-AI-CHAT-by-WR-Web"
MAX_MANIFEST_BYTES = 512 * 1024


def _version_key(value: str) -> tuple[int, int, int, int, int]:
    text = str(value or "").strip().lower().lstrip("v")
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:[-.]?(alpha|beta|rc)(\d+)?)?", text)
    if not match:
        return (0, 0, 0, 0, 0)
    major, minor, patch = (int(match.group(i)) for i in (1, 2, 3))
    stage = match.group(4) or "stable"
    stage_no = int(match.group(5) or 0)
    rank = {"alpha": 1, "beta": 2, "rc": 3, "stable": 4}[stage]
    return (major, minor, patch, rank, stage_no)


def _fetch_manifest() -> dict[str, Any]:
    parsed = urllib.parse.urlparse(MANIFEST_URL)
    if parsed.scheme != "https" or parsed.hostname != "raw.githubusercontent.com":
        raise RuntimeError("Update manifest must use GitHub raw HTTPS")
    if not parsed.path.startswith("/worasak-tee/LOCAL-AI-CHAT-by-WR-Releases/"):
        raise RuntimeError("Update manifest repository is not allowed")
    request = urllib.request.Request(
        MANIFEST_URL,
        headers={"User-Agent": "LOCAL-AI-CHAT-by-WR-Web-Updater/1.2", "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        payload = response.read(MAX_MANIFEST_BYTES + 1)
    if len(payload) > MAX_MANIFEST_BYTES:
        raise RuntimeError("Update manifest is too large")
    data = json.loads(payload.decode("utf-8"))
    if not isinstance(data, dict) or data.get("product") != PRODUCT:
        raise RuntimeError("Invalid update manifest")
    version = str(data.get("version") or "").strip()
    files = data.get("files")
    if not version or _version_key(version) == (0, 0, 0, 0, 0):
        raise RuntimeError("Update manifest version is invalid")
    if not isinstance(files, list) or not files:
        raise RuntimeError("Update manifest has no files")
    return data


def _read_job(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_job(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _helper_python(root: Path) -> Path:
    if os.name == "nt":
        candidate = root / ".venv-web" / "Scripts" / "python.exe"
        if candidate.is_file():
            return candidate
        raise RuntimeError("Updater Python was not found: .venv-web\\Scripts\\python.exe")
    candidate = Path(sys.executable)
    if candidate.is_file():
        return candidate
    raise RuntimeError("Updater Python interpreter was not found")


def _launch_helper(*, root: Path, helper: Path, job_file: Path, log_file: Path) -> str:
    python_exe = _helper_python(root)
    args = [str(python_exe), str(helper), "--job", str(job_file), "--manifest", MANIFEST_URL]
    log_file.parent.mkdir(parents=True, exist_ok=True)
    base_flags = 0
    breakaway_flag = 0
    if os.name == "nt":
        base_flags = int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)) | int(getattr(subprocess, "DETACHED_PROCESS", 0))
        breakaway_flag = int(getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000))
    attempts = [base_flags | breakaway_flag] if breakaway_flag else [base_flags]
    if breakaway_flag:
        attempts.append(base_flags)
    last_error: Exception | None = None
    for flags in attempts:
        log_handle = None
        try:
            log_handle = open(log_file, "ab", buffering=0)
            subprocess.Popen(
                args,
                cwd=str(root),
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=log_handle,
                close_fds=True,
                creationflags=flags,
            )
            return str(python_exe)
        except OSError as exc:
            last_error = exc
        finally:
            if log_handle is not None:
                log_handle.close()
    raise RuntimeError(f"Could not launch updater helper with {python_exe}: {last_error}")


def _install_optional_extensions(app, ctx: dict[str, Any]) -> None:
    errors: list[str] = []
    try:
        from .profile_rename_extension import install_profile_rename
        install_profile_rename(app, ctx)
    except Exception as exc:
        errors.append(f"profile_rename: {exc}")
    try:
        from .attachment_extension import install_attachment_extension
        install_attachment_extension(app, ctx)
    except Exception as exc:
        errors.append(f"attachment: {exc}")
    if errors:
        ctx["_wr_optional_extension_errors"] = errors


def install_update_routes(app, ctx: dict[str, Any]) -> None:
    require_session = ctx["_require_session"]
    require_csrf = ctx["_require_csrf"]
    settings = ctx["settings"]
    web_version = str(ctx.get("WEB_VERSION") or "")
    base_dir = Path(ctx["BASE_DIR"])
    root = base_dir.parent
    job_dir = Path(settings.data_dir) / "update_jobs"
    helper = base_dir / "update_helper.py"
    managed = {"/api/update/check", "/api/update/install", "/api/update/status/{job_id}"}
    app.router.routes[:] = [route for route in app.router.routes if getattr(route, "path", None) not in managed]

    def require_admin(request: Request, *, csrf: bool = False) -> dict[str, Any]:
        session = require_session(request)
        if str(session.get("role") or "").strip().lower() != "admin":
            raise HTTPException(status_code=403, detail="System Administrator role is required for Software Update")
        if csrf:
            require_csrf(request, session)
        return session

    @app.get("/api/update/check")
    def update_check(request: Request):
        require_admin(request)
        try:
            manifest = _fetch_manifest()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Update check failed: {exc}") from exc
        latest = str(manifest.get("version") or "")
        return {
            "ok": True,
            "current_version": web_version,
            "latest_version": latest,
            "update_available": _version_key(latest) > _version_key(web_version),
            "channel": str(manifest.get("channel") or "beta"),
            "published_at": str(manifest.get("published_at") or ""),
            "notes": manifest.get("notes") if isinstance(manifest.get("notes"), list) else [],
        }

    @app.post("/api/update/install")
    def update_install(request: Request):
        session = require_admin(request, csrf=True)
        if not helper.exists():
            raise HTTPException(status_code=500, detail="Update helper is not installed")
        try:
            manifest = _fetch_manifest()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Update check failed: {exc}") from exc
        latest = str(manifest.get("version") or "")
        if _version_key(latest) <= _version_key(web_version):
            return {"ok": True, "started": False, "message": "Already up to date", "version": web_version}
        import secrets
        job_id = secrets.token_hex(10)
        job_file = job_dir / f"{job_id}.json"
        log_file = job_dir / f"{job_id}.log"
        _write_job(job_file, {
            "job_id": job_id,
            "status": "queued",
            "stage": "Queued",
            "current_version": web_version,
            "target_version": latest,
            "requested_by": str(session.get("username") or "admin"),
            "message": "Waiting for updater helper",
        })
        try:
            runtime = _launch_helper(root=root, helper=helper, job_file=job_file, log_file=log_file)
            _write_job(job_file, {
                **_read_job(job_file),
                "status": "queued",
                "stage": "Helper launched",
                "message": "Updater helper started outside the Web service process",
                "runtime": runtime,
            })
        except Exception as exc:
            _write_job(job_file, {
                "job_id": job_id,
                "status": "failed",
                "stage": "Launch failed",
                "current_version": web_version,
                "target_version": latest,
                "message": str(exc),
            })
            raise HTTPException(status_code=500, detail=f"Could not start updater helper: {exc}") from exc
        return {"ok": True, "started": True, "job_id": job_id, "target_version": latest}

    @app.get("/api/update/status/{job_id}")
    def update_status(request: Request, job_id: str):
        require_admin(request)
        clean = str(job_id or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{20}", clean):
            raise HTTPException(status_code=400, detail="Invalid update job")
        job_file = job_dir / f"{clean}.json"
        if not job_file.exists():
            raise HTTPException(status_code=404, detail="Update job not found")
        return _read_job(job_file)

    _install_optional_extensions(app, ctx)
