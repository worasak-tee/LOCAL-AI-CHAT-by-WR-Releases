from __future__ import annotations

import hashlib
import html
import json
import mimetypes
import os
import re
import secrets
import shutil
import time
import zipfile
from pathlib import Path
from typing import Any

from fastapi import File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field


MAX_FILES = 5
MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_BATCH_BYTES = 100 * 1024 * 1024
MAX_TEXT_PREVIEW = 1024 * 1024
MAX_CONTEXT_CHARS = 9000
MAX_ZIP_ENTRIES = 500
STAGE_TTL_SECONDS = 24 * 60 * 60
TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")
TEXT_EXTENSIONS = {
    ".txt", ".md", ".csv", ".json", ".log", ".py", ".js", ".ts", ".tsx", ".jsx",
    ".html", ".htm", ".css", ".xml", ".yaml", ".yml", ".ini", ".cfg", ".conf",
    ".sql", ".ps1", ".bat", ".cmd", ".sh", ".toml", ".env", ".properties", ".rtf",
}
SAFE_INLINE_IMAGES = {"image/jpeg", "image/png", "image/gif", "image/webp", "image/bmp", "image/avif"}


class StageContextRequest(BaseModel):
    tokens: list[str] = Field(default_factory=list, max_length=MAX_FILES)
    source: str = "General AI"


class CommitRequest(BaseModel):
    chat_id: int
    user_created_at: str = Field(min_length=8, max_length=80)
    tokens: list[str] = Field(default_factory=list, max_length=MAX_FILES)


def _safe_name(value: str) -> str:
    name = Path(str(value or "attachment.bin")).name
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", name).strip(" .")
    return name[:180] or "attachment.bin"


def _user_hash(user_key: str) -> str:
    return hashlib.sha256(str(user_key).encode("utf-8")).hexdigest()[:24]


def _is_text(name: str, content_type: str) -> bool:
    ctype = str(content_type or "").lower()
    if ctype.startswith("text/"):
        return True
    if ctype in {"application/json", "application/xml", "application/javascript", "application/x-yaml"}:
        return True
    return Path(name).suffix.lower() in TEXT_EXTENSIONS


def _meta_file(stage_dir: Path) -> Path:
    return stage_dir / "meta.json"


def _load_meta(stage_dir: Path) -> dict[str, Any]:
    try:
        data = json.loads(_meta_file(stage_dir).read_text(encoding="utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=404, detail="Staged attachment was not found") from exc
    if not isinstance(data, dict):
        raise HTTPException(status_code=404, detail="Staged attachment was not found")
    return data


def _stage_dir(stage_root: Path, user_key: str, token: str) -> Path:
    clean = str(token or "").strip().lower()
    if not TOKEN_RE.fullmatch(clean):
        raise HTTPException(status_code=400, detail="Invalid attachment token")
    return stage_root / _user_hash(user_key) / clean


def _cleanup_staging(stage_root: Path) -> None:
    if not stage_root.exists():
        return
    cutoff = time.time() - STAGE_TTL_SECONDS
    for user_dir in stage_root.iterdir():
        if not user_dir.is_dir():
            continue
        for token_dir in user_dir.iterdir():
            try:
                if token_dir.is_dir() and token_dir.stat().st_mtime < cutoff:
                    shutil.rmtree(token_dir, ignore_errors=True)
            except OSError:
                continue


def _preview_headers() -> dict[str, str]:
    return {
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; img-src data:; media-src 'self'; frame-ancestors 'self'",
        "Referrer-Policy": "no-referrer",
    }


def _html_page(title: str, body: str) -> HTMLResponse:
    safe_title = html.escape(title)
    page = f"""<!doctype html><html lang='th'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>{safe_title}</title><style>body{{font-family:system-ui,-apple-system,Segoe UI,sans-serif;margin:24px;color:#172033;background:#f7f9fc}}main{{max-width:1100px;margin:auto;background:#fff;border:1px solid #e1e6ee;border-radius:14px;padding:20px}}h1{{font-size:18px;margin:0 0 14px}}pre{{white-space:pre-wrap;word-break:break-word;background:#f6f8fb;border:1px solid #e3e8ef;border-radius:10px;padding:14px;max-height:75vh;overflow:auto}}table{{border-collapse:collapse;width:100%;font-size:13px}}td,th{{padding:8px;border-bottom:1px solid #e8ecf1;text-align:left}}small{{color:#64748b}}</style></head><body><main><h1>{safe_title}</h1>{body}</main></body></html>"""
    return HTMLResponse(page, headers=_preview_headers())


def _zip_preview(path: Path, name: str) -> HTMLResponse:
    rows: list[str] = []
    try:
        with zipfile.ZipFile(path, "r") as archive:
            infos = archive.infolist()
            for info in infos[:MAX_ZIP_ENTRIES]:
                rows.append(
                    "<tr><td>" + html.escape(info.filename) + "</td><td>" + str(int(info.file_size)) + "</td><td>" + str(int(info.compress_size)) + "</td></tr>"
                )
            suffix = ""
            if len(infos) > MAX_ZIP_ENTRIES:
                suffix = f"<p><small>แสดง {MAX_ZIP_ENTRIES} จาก {len(infos)} รายการ</small></p>"
            table = "<table><thead><tr><th>ไฟล์ภายใน ZIP</th><th>Size</th><th>Compressed</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table>" + suffix
            return _html_page(name, table)
    except (zipfile.BadZipFile, OSError):
        return _html_page(name, "<p>ไม่สามารถอ่านรายการภายใน ZIP ได้</p>")


def _preview_file(path: Path, name: str, content_type: str, size: int):
    ctype = str(content_type or "application/octet-stream").split(";", 1)[0].strip().lower()
    suffix = Path(name).suffix.lower()
    if suffix == ".zip" or ctype in {"application/zip", "application/x-zip-compressed"}:
        return _zip_preview(path, name)
    if _is_text(name, ctype):
        raw = path.read_bytes()[:MAX_TEXT_PREVIEW]
        text = raw.decode("utf-8", errors="replace").replace("\x00", "")
        note = ""
        if size > len(raw):
            note = f"<p><small>Preview เฉพาะ {len(raw):,} bytes แรกจาก {size:,} bytes</small></p>"
        return _html_page(name, note + "<pre>" + html.escape(text) + "</pre>")
    if ctype in SAFE_INLINE_IMAGES or ctype == "application/pdf" or ctype.startswith(("audio/", "video/")):
        response = FileResponse(path=str(path), media_type=ctype, filename=name)
        response.headers.update(_preview_headers())
        response.headers["Content-Disposition"] = f"inline; filename*=UTF-8''{_urlquote(name)}"
        return response
    guessed = mimetypes.guess_type(name)[0] or ctype or "application/octet-stream"
    body = (
        "<p>Browser ไม่มี Preview แบบปลอดภัยสำหรับไฟล์ชนิดนี้ แต่ไฟล์ถูกเก็บไว้ใน Chat และดาวน์โหลดได้หลังส่ง</p>"
        f"<table><tr><th>ชื่อไฟล์</th><td>{html.escape(name)}</td></tr><tr><th>ชนิด</th><td>{html.escape(guessed)}</td></tr><tr><th>ขนาด</th><td>{size:,} bytes</td></tr></table>"
    )
    return _html_page(name, body)


def _urlquote(value: str) -> str:
    from urllib.parse import quote
    return quote(str(value), safe="")


def _context_for_file(path: Path, meta: dict[str, Any], remaining: int) -> str:
    name = str(meta.get("name") or "attachment.bin")
    ctype = str(meta.get("content_type") or "application/octet-stream")
    size = int(meta.get("size") or 0)
    suffix = Path(name).suffix.lower()
    header = f"\n\n[ไฟล์แนบ: {name} | {ctype} | {size} bytes]"
    if suffix == ".zip" or ctype.lower() in {"application/zip", "application/x-zip-compressed"}:
        try:
            with zipfile.ZipFile(path, "r") as archive:
                entries = [f"- {i.filename} ({i.file_size} bytes)" for i in archive.infolist()[:120]]
            block = header + "\nรายการภายใน ZIP:\n" + "\n".join(entries)
        except Exception:
            block = header + "\n[ไม่สามารถอ่านรายการภายใน ZIP ได้]"
    elif _is_text(name, ctype):
        raw = path.read_bytes()[: min(MAX_TEXT_PREVIEW, max(1024, remaining * 4))]
        text = raw.decode("utf-8", errors="replace").replace("\x00", "")
        block = header + "\n" + text + "\n[จบไฟล์แนบ]"
    else:
        block = header + "\n[ไฟล์ binary ถูกแนบและเก็บไว้ใน Chat; ใช้ชื่อ/ชนิด/ขนาดเป็นบริบท]"
    return block[:remaining]


def install_attachment_extension(app, ctx: dict[str, Any]) -> None:
    db = ctx["db"]
    settings = ctx["settings"]
    require_session = ctx["_require_session"]
    require_csrf = ctx["_require_csrf"]
    chat_files_dir = Path(ctx["CHAT_FILES_DIR"])
    stage_root = Path(settings.data_dir) / "chat_upload_staging"
    stage_root.mkdir(parents=True, exist_ok=True)

    managed_paths = {
        "/api/attachments/stage",
        "/api/attachments/stage/context",
        "/api/attachments/stage/{token}",
        "/api/attachments/stage/{token}/preview",
        "/api/attachments/commit",
        "/api/attachments/{attachment_id}/preview",
    }
    app.router.routes[:] = [route for route in app.router.routes if getattr(route, "path", None) not in managed_paths]

    @app.post("/api/attachments/stage")
    async def stage_attachments(request: Request, files: list[UploadFile] = File(...)):
        session = require_session(request)
        require_csrf(request, session)
        _cleanup_staging(stage_root)
        if not files or len(files) > MAX_FILES:
            raise HTTPException(status_code=400, detail=f"แนบได้สูงสุด {MAX_FILES} ไฟล์ต่อข้อความ")
        staged: list[dict[str, Any]] = []
        total = 0
        created_dirs: list[Path] = []
        try:
            for upload in files:
                token = secrets.token_hex(16)
                target_dir = _stage_dir(stage_root, session["user_key"], token)
                target_dir.mkdir(parents=True, exist_ok=False)
                created_dirs.append(target_dir)
                payload_path = target_dir / "payload.bin"
                size = 0
                with payload_path.open("wb") as out:
                    while True:
                        chunk = await upload.read(1024 * 1024)
                        if not chunk:
                            break
                        size += len(chunk)
                        total += len(chunk)
                        if size > MAX_FILE_BYTES:
                            raise HTTPException(status_code=413, detail=f"{upload.filename}: ขนาดไฟล์สูงสุด 50 MB")
                        if total > MAX_BATCH_BYTES:
                            raise HTTPException(status_code=413, detail="ไฟล์แนบรวมต่อครั้งสูงสุด 100 MB")
                        out.write(chunk)
                name = _safe_name(upload.filename or "attachment.bin")
                content_type = str(upload.content_type or mimetypes.guess_type(name)[0] or "application/octet-stream")
                meta = {"token": token, "name": name, "content_type": content_type, "size": size, "created_at": time.time()}
                _meta_file(target_dir).write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
                staged.append({**meta, "preview_url": f"/api/attachments/stage/{token}/preview"})
            return {"ok": True, "files": staged}
        except Exception:
            for directory in created_dirs:
                shutil.rmtree(directory, ignore_errors=True)
            raise
        finally:
            for upload in files:
                try:
                    await upload.close()
                except Exception:
                    pass

    @app.delete("/api/attachments/stage/{token}")
    def delete_staged(request: Request, token: str):
        session = require_session(request)
        require_csrf(request, session)
        target_dir = _stage_dir(stage_root, session["user_key"], token)
        shutil.rmtree(target_dir, ignore_errors=True)
        return {"ok": True}

    @app.get("/api/attachments/stage/{token}/preview")
    def preview_staged(request: Request, token: str):
        session = require_session(request)
        target_dir = _stage_dir(stage_root, session["user_key"], token)
        meta = _load_meta(target_dir)
        payload = target_dir / "payload.bin"
        if not payload.is_file():
            raise HTTPException(status_code=404, detail="Staged attachment was not found")
        return _preview_file(payload, str(meta.get("name") or "attachment.bin"), str(meta.get("content_type") or ""), int(meta.get("size") or payload.stat().st_size))

    @app.post("/api/attachments/stage/context")
    def staged_context(request: Request, payload: StageContextRequest):
        session = require_session(request)
        require_csrf(request, session)
        blocks: list[str] = []
        remaining = MAX_CONTEXT_CHARS
        for token in payload.tokens[:MAX_FILES]:
            if remaining <= 200:
                break
            target_dir = _stage_dir(stage_root, session["user_key"], token)
            meta = _load_meta(target_dir)
            file_path = target_dir / "payload.bin"
            if not file_path.is_file():
                continue
            block = _context_for_file(file_path, meta, remaining)
            blocks.append(block)
            remaining -= len(block)
        return {"ok": True, "context": "".join(blocks), "source": payload.source}

    @app.post("/api/attachments/commit")
    def commit_attachments(request: Request, payload: CommitRequest):
        session = require_session(request)
        require_csrf(request, session)
        chat = db.get_chat(payload.chat_id, session["user_key"])
        if not chat:
            raise HTTPException(status_code=404, detail="Chat not found")
        with db.connect() as conn:
            row = conn.execute(
                "SELECT id FROM web_messages WHERE chat_id=? AND user_key=? AND role='user' AND created_at=? ORDER BY id DESC LIMIT 1",
                (int(payload.chat_id), session["user_key"], str(payload.user_created_at)),
            ).fetchone()
        if not row:
            raise HTTPException(status_code=409, detail="Could not match attachment to the sent message")
        message_id = int(row["id"])
        dest_dir = chat_files_dir / _user_hash(session["user_key"]) / str(int(payload.chat_id)) / "user_uploads"
        dest_dir.mkdir(parents=True, exist_ok=True)
        attachments: list[dict[str, Any]] = []
        for token in payload.tokens[:MAX_FILES]:
            target_dir = _stage_dir(stage_root, session["user_key"], token)
            meta = _load_meta(target_dir)
            source_path = target_dir / "payload.bin"
            if not source_path.is_file():
                continue
            name = _safe_name(str(meta.get("name") or "attachment.bin"))
            target = dest_dir / name
            if target.exists():
                stem, suffix = target.stem, target.suffix
                for index in range(2, 1002):
                    candidate = dest_dir / f"{stem}_{index}{suffix}"
                    if not candidate.exists():
                        target = candidate
                        break
            os.replace(source_path, target)
            record = db.add_attachment(
                message_id=message_id,
                chat_id=int(payload.chat_id),
                user_key=session["user_key"],
                filename=target.name,
                stored_path=str(target),
                content_type=str(meta.get("content_type") or "application/octet-stream"),
                size=int(meta.get("size") or target.stat().st_size),
                source="User Upload",
            )
            record["preview_url"] = f"/api/attachments/{int(record['id'])}/preview"
            attachments.append(record)
            shutil.rmtree(target_dir, ignore_errors=True)
        return {"ok": True, "attachments": attachments}

    @app.get("/api/attachments/{attachment_id}/preview")
    def preview_attachment(request: Request, attachment_id: int):
        session = require_session(request)
        row = db.attachment(attachment_id, session["user_key"])
        if not row:
            raise HTTPException(status_code=404, detail="Attachment not found")
        path = Path(str(row.get("stored_path") or "")).resolve()
        root = chat_files_dir.resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="Attachment path is invalid") from exc
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Attachment file not found")
        return _preview_file(path, str(row.get("filename") or path.name), str(row.get("content_type") or ""), int(row.get("size") or path.stat().st_size))
