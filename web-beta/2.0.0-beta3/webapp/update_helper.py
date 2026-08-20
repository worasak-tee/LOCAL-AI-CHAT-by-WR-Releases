from __future__ import annotations

import argparse
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
MAX_FILES = 80
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 80 * 1024 * 1024


def write_job(path: Path, **fields: Any) -> None:
    current: dict[str, Any] = {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            current = loaded
    except Exception:
        pass
    current.update(fields)
    current["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def download(url: str, max_bytes: int) -> bytes:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "raw.githubusercontent.com":
        raise RuntimeError("Update file URL must use GitHub raw HTTPS")
    if not parsed.path.startswith("/worasak-tee/LOCAL-AI-CHAT-by-WR-Releases/"):
        raise RuntimeError("Update file repository is not allowed")
    request = urllib.request.Request(url, headers={"User-Agent": "LOCAL-AI-CHAT-by-WR-Web-Updater/1.0"})
    with urllib.request.urlopen(request, timeout=30) as response:
        data = response.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise RuntimeError("Downloaded update file is too large")
    return data


def load_manifest(url: str) -> dict[str, Any]:
    data = download(url, 512 * 1024)
    payload = json.loads(data.decode("utf-8"))
    if not isinstance(payload, dict) or payload.get("product") != PRODUCT:
        raise RuntimeError("Invalid update manifest")
    version = str(payload.get("version") or "").strip()
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:-(?:alpha|beta|rc)\d+)?", version):
        raise RuntimeError("Invalid target version")
    files = payload.get("files")
    if not isinstance(files, list) or not files or len(files) > MAX_FILES:
        raise RuntimeError("Invalid update file list")
    return payload


def safe_target(root: Path, value: str) -> Path:
    clean = str(value or "").replace("\\", "/").strip("/")
    if not clean or clean.startswith(".") or ".." in clean.split("/"):
        raise RuntimeError(f"Unsafe update path: {value}")
    if not clean.startswith("webapp/") or clean.startswith("webapp/data/"):
        raise RuntimeError(f"Protected update path: {value}")
    target = (root / clean).resolve()
    target.relative_to(root.resolve())
    return target


def git_blob_sha1(payload: bytes) -> str:
    header = f"blob {len(payload)}\0".encode("ascii")
    return hashlib.sha1(header + payload).hexdigest()


def read_port(root: Path) -> int:
    config = root / "webapp" / "data" / "web_runtime_config.json"
    try:
        data = json.loads(config.read_text(encoding="utf-8"))
        port = int(data.get("web_port") or 9780)
        return port if 1 <= port <= 65535 else 9780
    except Exception:
        return 9780


def service_cmd(action: str) -> int:
    return subprocess.run(["sc", action, SERVICE], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode


def service_state() -> str:
    try:
        out = subprocess.check_output(["sc", "query", SERVICE], stderr=subprocess.STDOUT, text=True, errors="replace")
    except Exception:
        return "unknown"
    upper = out.upper()
    if "STOPPED" in upper: return "stopped"
    if "RUNNING" in upper: return "running"
    if "STOP_PENDING" in upper: return "stop_pending"
    if "START_PENDING" in upper: return "start_pending"
    return "unknown"


def stop_service(timeout: int = 30) -> None:
    service_cmd("stop")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if service_state() == "stopped": return
        time.sleep(1)
    raise RuntimeError("Windows Service did not stop in time")


def start_service(timeout: int = 35) -> None:
    rc = service_cmd("start")
    if rc not in (0, 1056):
        raise RuntimeError(f"Windows Service start failed: {rc}")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if service_state() == "running": return
        time.sleep(1)
    raise RuntimeError("Windows Service did not reach Running state")


def health(port: int, version: str | None = None, timeout: int = 50) -> bool:
    deadline = time.time() + timeout
    url = f"http://127.0.0.1:{port}/health"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as response:
                body = json.loads(response.read(64 * 1024).decode("utf-8"))
            if response.status == 200 and isinstance(body, dict) and body.get("ok") is True:
                if version is None or str(body.get("version") or "") == version:
                    return True
        except Exception:
            pass
        time.sleep(1.5)
    return False


def backup_files(backup_root: Path, targets: list[tuple[str, Path]]) -> dict[str, bool]:
    existed: dict[str, bool] = {}
    for rel, target in targets:
        existed[rel] = target.exists()
        if target.exists():
            saved = backup_root / rel
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, saved)
    return existed


def restore_files(backup_root: Path, targets: list[tuple[str, Path]], existed: dict[str, bool]) -> None:
    for rel, target in targets:
        saved = backup_root / rel
        if saved.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(saved, target)
        elif not existed.get(rel, False):
            target.unlink(missing_ok=True)


def install_files(staging: Path, targets: list[tuple[str, Path]]) -> None:
    for rel, target in targets:
        source = staging / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".wrupdate")
        shutil.copy2(source, tmp)
        os.replace(tmp, target)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--job", required=True)
    parser.add_argument("--manifest", required=True)
    args = parser.parse_args()
    job_file = Path(args.job).resolve()
    root = Path(__file__).resolve().parent.parent
    data_dir = root / "webapp" / "data"
    job_id = job_file.stem
    staging = data_dir / "update_staging" / job_id
    backup_root = data_dir / "update_backups" / job_id
    port = read_port(root)
    targets: list[tuple[str, Path]] = []
    existed: dict[str, bool] = {}
    service_stopped = False
    try:
        time.sleep(1.5)
        write_job(job_file, status="running", stage="Downloading manifest", message="Checking release metadata")
        manifest = load_manifest(str(args.manifest))
        version = str(manifest["version"])
        files = manifest["files"]
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True, exist_ok=True)
        write_job(job_file, status="running", stage="Downloading", target_version=version, message="Downloading update files")
        total = 0
        for index, row in enumerate(files, start=1):
            if not isinstance(row, dict): raise RuntimeError("Invalid file entry in update manifest")
            rel = str(row.get("path") or "").replace("\\", "/").strip("/")
            target = safe_target(root, rel)
            url = str(row.get("url") or "").strip()
            expected = str(row.get("git_sha1") or "").strip().lower()
            if not re.fullmatch(r"[0-9a-f]{40}", expected): raise RuntimeError(f"Missing Git blob hash for {rel}")
            payload = download(url, MAX_FILE_BYTES)
            total += len(payload)
            if total > MAX_TOTAL_BYTES: raise RuntimeError("Update package exceeds maximum size")
            if git_blob_sha1(payload) != expected: raise RuntimeError(f"Git blob integrity mismatch: {rel}")
            out = staging / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(payload)
            targets.append((rel, target))
            write_job(job_file, status="running", stage="Downloading", progress=int(index * 35 / len(files)), message=f"Downloaded {index}/{len(files)} files")
        write_job(job_file, status="running", stage="Validating", progress=40, message="Validating Python files")
        for rel, _target in targets:
            if rel.lower().endswith(".py"): py_compile.compile(str(staging / rel), doraise=True)
        shutil.rmtree(backup_root, ignore_errors=True)
        backup_root.mkdir(parents=True, exist_ok=True)
        existed = backup_files(backup_root, targets)
        write_job(job_file, status="running", stage="Stopping Service", progress=48, message="Stopping LOCALAIChatWeb")
        stop_service(); service_stopped = True
        write_job(job_file, status="running", stage="Installing", progress=60, message="Replacing verified runtime files")
        install_files(staging, targets)
        write_job(job_file, status="running", stage="Starting Service", progress=78, message="Starting LOCALAIChatWeb")
        start_service(); service_stopped = False
        write_job(job_file, status="running", stage="Health Check", progress=88, message=f"Verifying Web {version}")
        if not health(port, version=version, timeout=55): raise RuntimeError(f"Health check did not return version {version}")
        write_job(job_file, status="success", stage="Completed", progress=100, current_version=version, target_version=version, message=f"Updated successfully to {version}", backup_path=str(backup_root))
        shutil.rmtree(staging, ignore_errors=True)
        return 0
    except Exception as exc:
        try:
            write_job(job_file, status="rollback", stage="Rollback", message=str(exc))
            if not service_stopped:
                try: stop_service(); service_stopped = True
                except Exception: pass
            if targets and backup_root.exists(): restore_files(backup_root, targets, existed)
            try: start_service(); service_stopped = False
            except Exception: pass
            rollback_ok = health(port, version=None, timeout=35)
            write_job(job_file, status="failed", stage="Rolled back" if rollback_ok else "Rollback requires attention", message=str(exc), rollback_ok=rollback_ok)
        except Exception:
            pass
        return 10


if __name__ == "__main__":
    raise SystemExit(main())
