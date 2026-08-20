from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
VENV_PYTHON = ROOT / ".venv-web" / "Scripts" / "python.exe"


def _has_module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except Exception:
        return False


def ensure_web_runtime_dependencies() -> dict[str, Any]:
    """Ensure small pure-Python Web-only dependencies required by runtime features.

    Beta 7 adds PDF text extraction. Older Web installs did not include pypdf in
    requirements-web.txt, so an in-place Auto Update must repair that one missing
    dependency before the runtime contract is allowed to become healthy.
    """
    status: dict[str, Any] = {"ok": True, "installed": [], "already": []}
    if _has_module("pypdf"):
        status["already"].append("pypdf")
        return status

    python_exe = VENV_PYTHON if VENV_PYTHON.is_file() else Path(sys.executable)
    env = dict(os.environ)
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    command = [
        str(python_exe),
        "-m",
        "pip",
        "install",
        "--no-input",
        "--disable-pip-version-check",
        "pypdf>=5,<7",
    ]
    try:
        result = subprocess.run(
            command,
            cwd=str(ROOT),
            env=env,
            text=True,
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=45,
        )
    except Exception as exc:
        return {"ok": False, "installed": [], "already": [], "error": f"pypdf install failed: {exc}"}

    if result.returncode != 0:
        output = (result.stdout or "").strip().splitlines()
        tail = " | ".join(output[-6:])[:1200]
        return {
            "ok": False,
            "installed": [],
            "already": [],
            "error": f"pypdf install returned {result.returncode}: {tail}",
        }

    importlib.invalidate_caches()
    if not _has_module("pypdf"):
        return {"ok": False, "installed": [], "already": [], "error": "pypdf install completed but module is not importable"}

    status["installed"].append("pypdf")
    return status
