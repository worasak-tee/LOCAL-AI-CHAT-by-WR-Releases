from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import py_compile
import re
import shutil
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

SERVICE = "LOCALAIChatWeb"
PRODUCT = "LOCAL-AI-CHAT-by-WR-Web"
MANIFEST_URL = "https://raw.githubusercontent.com/worasak-tee/LOCAL-AI-CHAT-by-WR-Releases/main/web-beta/manifest.json"
MAX_FILE_BYTES = 8 * 1024 * 1024


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def download(url: str, max_bytes: int = MAX_FILE_BYTES) -> bytes:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "raw.githubusercontent.com":
        raise RuntimeError("Recovery URL must use GitHub raw HTTPS")
    if not parsed.path.startswith("/worasak-tee/LOCAL-AI-CHAT-by-WR-Releases/"):
        raise RuntimeError("Recovery repository is not allowed")
    req = urllib.request.Request(url, headers={"User-Agent": "LOCAL-AI-CHAT-Web-Recovery/6.0"})
    with urllib.request.urlopen(req, timeout=30) as response:
        payload = response.read(max_bytes + 1)
    if len(payload) > max_bytes:
        raise RuntimeError("Downloaded file is too large")
    return payload


def git_blob_sha1(payload: bytes) -> str:
    header = f"blob {len(payload)}\0".encode("ascii")
    return hashlib.sha1(header + payload).hexdigest()


def safe_target(root: Path, rel: str) -> Path:
    clean = str(rel or "").replace("\\", "/").strip("/")
    if not clean or clean.startswith(".") or ".." in clean.split("/"):
        raise RuntimeError(f"Unsafe update path: {rel}")
    if not clean.startswith("webapp/") or clean.startswith("webapp/data/"):
        raise RuntimeError(f"Protected update path: {rel}")
    target = (root / clean).resolve()
    target.relative_to(root.resolve())
    return target


def service_state() -> str:
    try:
        out = subprocess.check_output(["sc", "query", SERVICE], stderr=subprocess.STDOUT, text=True, errors="replace")
    except Exception:
        return "unknown"
    upper = out.upper()
    if "STOPPED" in upper:
        return "stopped"
    if "RUNNING" in upper:
        return "running"
    return "pending"


def stop_service(timeout: int = 35) -> None:
    subprocess.run(["sc", "stop", SERVICE], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if service_state() == "stopped":
            return
        time.sleep(1)
    raise RuntimeError("LOCALAIChatWeb did not stop in time")


def start_service(timeout: int = 40) -> None:
    rc = subprocess.run(["sc", "start", SERVICE], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode
    if rc not in (0, 1056):
        raise RuntimeError(f"LOCALAIChatWeb start failed: {rc}")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if service_state() == "running":
            return
        time.sleep(1)
    raise RuntimeError("LOCALAIChatWeb did not reach RUNNING")


def read_port(root: Path) -> int:
    try:
        cfg = json.loads((root / "webapp" / "data" / "web_runtime_config.json").read_text(encoding="utf-8"))
        port = int(cfg.get("web_port") or 9780)
        return port if 1 <= port <= 65535 else 9780
    except Exception:
        return 9780


def json_get(url: str, timeout: float = 4.0) -> tuple[int, Any]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            raw = response.read(1024 * 1024)
            return int(response.status), json.loads(raw.decode("utf-8"))
    except Exception as exc:
        code = int(getattr(exc, "code", 0) or 0)
        return code, None


def wait_health(port: int, version: str, timeout: int = 55) -> None:
    deadline = time.time() + timeout
    base = f"http://127.0.0.1:{port}"
    last = ""
    while time.time() < deadline:
        status, body = json_get(base + "/health", timeout=3)
        if status == 200 and isinstance(body, dict) and body.get("ok") is True and str(body.get("version") or "") == version:
            status2, ext = json_get(base + "/health/extensions", timeout=3)
            if status2 == 200 and isinstance(ext, dict):
                if ext.get("ok") is True and ext.get("update") is True and ext.get("attachment") is True:
                    return
                last = json.dumps(ext, ensure_ascii=False)
            else:
                last = f"/health/extensions HTTP {status2}"
        time.sleep(1.5)
    raise RuntimeError(f"Extension health failed: {last or 'no healthy response'}")


def openapi_contract(port: int) -> None:
    status, body = json_get(f"http://127.0.0.1:{port}/openapi.json", timeout=5)
    if status != 200 or not isinstance(body, dict):
        raise RuntimeError(f"OpenAPI check failed: HTTP {status}")
    paths = body.get("paths") if isinstance(body.get("paths"), dict) else {}
    required = {
        "/api/update/check",
        "/api/update/install",
        "/api/attachments/stage",
        "/api/attachments/commit",
        "/api/attachments/{attachment_id}/preview",
    }
    missing = sorted(required.difference(paths.keys()))
    if missing:
        raise RuntimeError("Missing runtime routes: " + ", ".join(missing))


def backup_files(root: Path, backup: Path, targets: list[tuple[str, Path]]) -> dict[str, bool]:
    existed: dict[str, bool] = {}
    shutil.rmtree(backup, ignore_errors=True)
    for rel, target in targets:
        existed[rel] = target.exists()
        if target.exists():
            saved = backup / rel
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, saved)
    return existed


def restore_files(backup: Path, targets: list[tuple[str, Path]], existed: dict[str, bool]) -> None:
    for rel, target in targets:
        saved = backup / rel
        if saved.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(saved, target)
        elif not existed.get(rel, False):
            target.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    args = parser.parse_args()

    if os.name != "nt" or not is_admin():
        print("[ERROR] Administrator permission is required")
        return 2

    root = Path(args.root).resolve()
    webapp = root / "webapp"
    if not webapp.is_dir() or not (root / ".venv-web" / "Scripts" / "python.exe").is_file():
        print("[ERROR] Invalid LOCAL-AI-CHAT-by-WR_Web folder")
        return 3

    print("[1/8] Download Beta 6 manifest")
    manifest = json.loads(download(MANIFEST_URL, 512 * 1024).decode("utf-8"))
    if manifest.get("product") != PRODUCT or manifest.get("version") != "2.0.0-beta6":
        raise RuntimeError("Beta 6 manifest is not active")
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise RuntimeError("Manifest has no files")

    staging = webapp / "data" / "beta6_extension_recovery_staging"
    backup = webapp / "data" / "update_backups" / "beta6_extension_recovery"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)
    targets: list[tuple[str, Path]] = []

    print("[2/8] Download + verify Git hashes")
    for row in files:
        rel = str(row.get("path") or "").replace("\\", "/").strip("/")
        url = str(row.get("url") or "")
        expected = str(row.get("git_sha1") or "").lower()
        if not re.fullmatch(r"[0-9a-f]{40}", expected):
            raise RuntimeError(f"Invalid hash for {rel}")
        payload = download(url)
        if git_blob_sha1(payload) != expected:
            raise RuntimeError(f"Git integrity mismatch: {rel}")
        out = staging / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(payload)
        targets.append((rel, safe_target(root, rel)))

    print("[3/8] Validate Python")
    for rel, _target in targets:
        if rel.lower().endswith(".py"):
            py_compile.compile(str(staging / rel), doraise=True)

    existed = backup_files(root, backup, targets)
    service_stopped = False
    port = read_port(root)
    try:
        print("[4/8] Stop Web Service")
        stop_service()
        service_stopped = True

        print("[5/8] Install verified Beta 6 runtime")
        for rel, target in targets:
            source = staging / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(target.suffix + ".beta6recover")
            shutil.copy2(source, tmp)
            os.replace(tmp, target)

        print("[6/8] Start Web Service")
        start_service()
        service_stopped = False

        print("[7/8] Health + Extension routes")
        wait_health(port, "2.0.0-beta6")

        print("[8/8] OpenAPI contract")
        openapi_contract(port)

        shutil.rmtree(staging, ignore_errors=True)
        print("\n[PASS] Beta 6 extension recovery completed")
        print("Software Update and Full Attachment backend are mounted.")
        print("Open the Web page again and press Ctrl+F5 once.")
        return 0
    except Exception as exc:
        print(f"\n[ERROR] {exc}")
        try:
            if not service_stopped:
                stop_service()
                service_stopped = True
        except Exception:
            pass
        restore_files(backup, targets, existed)
        try:
            start_service()
            service_stopped = False
        except Exception:
            pass
        print("Rollback completed. webapp/data and user data were not modified by the package.")
        return 10


if __name__ == "__main__":
    raise SystemExit(main())
