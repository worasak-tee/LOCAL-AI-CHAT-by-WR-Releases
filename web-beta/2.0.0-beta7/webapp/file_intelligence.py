from __future__ import annotations

import base64
import hashlib
import json
import re
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")
MAX_FILES = 5
MAX_CONTEXT_CHARS = 24000
MAX_TEXT_BYTES = 1024 * 1024
MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_IMAGES = 4
MAX_ZIP_ENTRIES = 240
MAX_ZIP_TEXT_FILES = 10
MAX_ZIP_TEXT_BYTES = 256 * 1024

TEXT_EXTENSIONS = {
    ".txt", ".md", ".csv", ".json", ".log", ".py", ".js", ".ts", ".tsx", ".jsx",
    ".html", ".htm", ".css", ".xml", ".yaml", ".yml", ".ini", ".cfg", ".conf",
    ".sql", ".ps1", ".bat", ".cmd", ".sh", ".toml", ".env", ".properties", ".rtf",
}
IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}


def _user_hash(user_key: str) -> str:
    return hashlib.sha256(str(user_key).encode("utf-8")).hexdigest()[:24]


def _safe_stage_dir(data_dir: Path, user_key: str, token: str) -> Path:
    clean = str(token or "").strip().lower()
    if not TOKEN_RE.fullmatch(clean):
        raise ValueError("Invalid attachment token")
    root = (Path(data_dir) / "chat_upload_staging" / _user_hash(user_key)).resolve()
    target = (root / clean).resolve()
    target.relative_to(root)
    return target


def _load_stage(data_dir: Path, user_key: str, token: str) -> tuple[Path, dict[str, Any]]:
    stage = _safe_stage_dir(data_dir, user_key, token)
    meta_file = stage / "meta.json"
    payload = stage / "payload.bin"
    if not meta_file.is_file() or not payload.is_file():
        raise FileNotFoundError("Staged attachment was not found")
    meta = json.loads(meta_file.read_text(encoding="utf-8"))
    if not isinstance(meta, dict):
        raise ValueError("Invalid staged attachment metadata")
    return payload, meta


def _decode_text(payload: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "cp874", "windows-1252"):
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            continue
    return payload.decode("utf-8", errors="replace")


def _plain_text(path: Path) -> str:
    return _decode_text(path.read_bytes()[:MAX_TEXT_BYTES]).replace("\x00", "")


def _docx_text(path: Path) -> str:
    with zipfile.ZipFile(path, "r") as archive:
        raw = archive.read("word/document.xml")
    root = ET.fromstring(raw)
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    lines: list[str] = []
    for para in root.findall(".//w:p", ns):
        parts = [node.text or "" for node in para.findall(".//w:t", ns)]
        text = "".join(parts).strip()
        if text:
            lines.append(text)
    return "\n".join(lines)


def _xlsx_shared_strings(archive: zipfile.ZipFile) -> list[str]:
    try:
        raw = archive.read("xl/sharedStrings.xml")
    except KeyError:
        return []
    root = ET.fromstring(raw)
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    out: list[str] = []
    for si in root.findall("m:si", ns):
        text = "".join((node.text or "") for node in si.findall(".//m:t", ns))
        out.append(text)
    return out


def _xlsx_text(path: Path) -> str:
    rows_out: list[str] = []
    with zipfile.ZipFile(path, "r") as archive:
        shared = _xlsx_shared_strings(archive)
        sheet_names = sorted(
            name for name in archive.namelist()
            if name.startswith("xl/worksheets/sheet") and name.endswith(".xml")
        )
        ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
        for sheet_no, sheet_name in enumerate(sheet_names[:20], start=1):
            root = ET.fromstring(archive.read(sheet_name))
            rows_out.append(f"[Sheet {sheet_no}: {Path(sheet_name).name}]")
            for row in root.findall(".//m:sheetData/m:row", ns)[:300]:
                values: list[str] = []
                for cell in row.findall("m:c", ns)[:80]:
                    ref = str(cell.attrib.get("r") or "")
                    cell_type = str(cell.attrib.get("t") or "")
                    value_node = cell.find("m:v", ns)
                    inline = cell.find("m:is", ns)
                    value = ""
                    if inline is not None:
                        value = "".join((n.text or "") for n in inline.findall(".//m:t", ns))
                    elif value_node is not None and value_node.text is not None:
                        value = value_node.text
                        if cell_type == "s":
                            try:
                                value = shared[int(value)]
                            except (ValueError, IndexError):
                                pass
                    if value != "":
                        values.append(f"{ref}={value}" if ref else value)
                if values:
                    rows_out.append(" | ".join(values))
                if sum(len(x) for x in rows_out) >= MAX_CONTEXT_CHARS:
                    return "\n".join(rows_out)
    return "\n".join(rows_out)


def _pdf_text(path: Path) -> tuple[str, str]:
    reader_cls = None
    engine = ""
    try:
        from pypdf import PdfReader as Reader  # type: ignore
        reader_cls = Reader
        engine = "pypdf"
    except Exception:
        try:
            from PyPDF2 import PdfReader as Reader  # type: ignore
            reader_cls = Reader
            engine = "PyPDF2"
        except Exception:
            return "", "unavailable"
    try:
        reader = reader_cls(str(path))
        parts: list[str] = []
        for page in list(reader.pages)[:30]:
            text = str(page.extract_text() or "").strip()
            if text:
                parts.append(text)
            if sum(len(x) for x in parts) >= MAX_CONTEXT_CHARS:
                break
        return "\n\n".join(parts), engine
    except Exception:
        return "", engine or "error"


def _zip_context(path: Path) -> str:
    lines: list[str] = []
    text_budget = MAX_ZIP_TEXT_BYTES
    text_files = 0
    with zipfile.ZipFile(path, "r") as archive:
        infos = archive.infolist()[:MAX_ZIP_ENTRIES]
        lines.append(f"ZIP entries: {len(infos)}" + ("+" if len(archive.infolist()) > len(infos) else ""))
        for info in infos:
            lines.append(f"- {info.filename} ({info.file_size} bytes)")
            suffix = Path(info.filename).suffix.lower()
            if (
                text_files < MAX_ZIP_TEXT_FILES
                and suffix in TEXT_EXTENSIONS
                and 0 < info.file_size <= 128 * 1024
                and text_budget > 0
                and not info.is_dir()
            ):
                try:
                    raw = archive.read(info)[: min(info.file_size, text_budget)]
                    text = _decode_text(raw).replace("\x00", "").strip()
                    if text:
                        lines.append(f"[Text: {info.filename}]\n{text}")
                        text_files += 1
                        text_budget -= len(raw)
                except Exception:
                    pass
    return "\n".join(lines)


def parser_status() -> dict[str, Any]:
    pdf = "unavailable"
    try:
        import pypdf  # type: ignore  # noqa: F401
        pdf = "pypdf"
    except Exception:
        try:
            import PyPDF2  # type: ignore  # noqa: F401
            pdf = "PyPDF2"
        except Exception:
            pass
    return {
        "text": True,
        "docx": True,
        "xlsx": True,
        "zip": True,
        "images": True,
        "pdf": pdf,
    }


def build_staged_ai_payload(*, data_dir: Path, user_key: str, tokens: list[str]) -> dict[str, Any]:
    context_blocks: list[str] = []
    images: list[str] = []
    files: list[dict[str, Any]] = []
    warnings: list[str] = []

    for token in [str(x).strip().lower() for x in tokens[:MAX_FILES]]:
        try:
            path, meta = _load_stage(Path(data_dir), user_key, token)
        except Exception as exc:
            warnings.append(f"attachment {token[:8]}: {exc}")
            continue

        name = str(meta.get("name") or "attachment.bin")
        content_type = str(meta.get("content_type") or "application/octet-stream").split(";", 1)[0].strip().lower()
        size = int(meta.get("size") or path.stat().st_size)
        suffix = Path(name).suffix.lower()
        block = ""
        mode = "metadata"

        try:
            if content_type in IMAGE_TYPES or suffix in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
                mode = "vision"
                if len(images) < MAX_IMAGES and size <= MAX_IMAGE_BYTES:
                    images.append(base64.b64encode(path.read_bytes()).decode("ascii"))
                    block = f"[Image attachment: {name} | {content_type} | {size} bytes | supplied to vision model]"
                else:
                    block = f"[Image attachment: {name} | {content_type} | {size} bytes | stored, vision payload skipped because of size/count limit]"
            elif suffix == ".docx":
                mode = "docx"
                block = _docx_text(path)
            elif suffix == ".xlsx":
                mode = "xlsx"
                block = _xlsx_text(path)
            elif suffix == ".pdf" or content_type == "application/pdf":
                mode = "pdf"
                text, engine = _pdf_text(path)
                if text:
                    block = text
                else:
                    block = f"[PDF attachment: {name} | text extraction unavailable with parser={engine}; file remains available for View/Download]"
                    warnings.append(f"{name}: PDF text extraction {engine}")
            elif suffix == ".zip" or content_type in {"application/zip", "application/x-zip-compressed"}:
                mode = "zip"
                block = _zip_context(path)
            elif suffix in TEXT_EXTENSIONS or content_type.startswith("text/") or content_type in {"application/json", "application/xml", "application/javascript"}:
                mode = "text"
                block = _plain_text(path)
            else:
                block = f"[Binary attachment: {name} | {content_type} | {size} bytes | no safe text parser]"
        except Exception as exc:
            warnings.append(f"{name}: {type(exc).__name__}: {exc}")
            block = f"[Attachment: {name} | {content_type} | {size} bytes | parser error]"

        files.append({"name": name, "content_type": content_type, "size": size, "mode": mode})
        header = f"\n\n--- FILE CONTEXT: {name} ({content_type}, {size} bytes) ---\n"
        context_blocks.append(header + (block or "[No readable content]") + "\n--- END FILE CONTEXT ---")

    context = "".join(context_blocks)
    if len(context) > MAX_CONTEXT_CHARS:
        context = context[:MAX_CONTEXT_CHARS] + "\n[File context truncated by server safety limit]"

    return {
        "context": context,
        "images": images,
        "files": files,
        "warnings": warnings,
        "parsers": parser_status(),
    }
