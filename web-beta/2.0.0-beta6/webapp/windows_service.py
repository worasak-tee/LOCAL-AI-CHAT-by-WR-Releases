from __future__ import annotations

import asyncio
import logging
import os
import sys
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import servicemanager
import win32event
import win32service
import win32serviceutil

VENV_SITE_PACKAGES = ROOT / ".venv-web" / "Lib" / "site-packages"
if VENV_SITE_PACKAGES.is_dir() and str(VENV_SITE_PACKAGES) not in sys.path:
    sys.path.insert(0, str(VENV_SITE_PACKAGES))

import uvicorn

from webapp.config import settings
from webapp.runtime_extensions import install_runtime_extensions


LOG_FILE = settings.data_dir / "web_service.log"
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    filename=str(LOG_FILE),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    force=True,
)
LOGGER = logging.getLogger("LOCAL-AI-CHAT-Web-Service")


class ServiceUvicornServer(uvicorn.Server):
    @contextmanager
    def capture_signals(self):
        yield


class LocalAIChatWebService(win32serviceutil.ServiceFramework):
    _svc_name_ = "LOCALAIChatWeb"
    _svc_display_name_ = "LOCAL-AI-CHAT-by-WR Web"
    _svc_description_ = "LOCAL-AI-CHAT-by-WR V2 Web server"

    def __init__(self, args):
        super().__init__(args)
        self.stop_event = win32event.CreateEvent(None, 0, 0, None)
        self.server: ServiceUvicornServer | None = None
        self.loop: asyncio.AbstractEventLoop | None = None

    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        LOGGER.info("Service stop requested")
        if self.server is not None:
            self.server.should_exit = True
        win32event.SetEvent(self.stop_event)

    def SvcDoRun(self):
        servicemanager.LogInfoMsg(
            f"{self._svc_display_name_} starting on {settings.host}:{settings.port}"
        )
        LOGGER.info("Service starting on %s:%s", settings.host, settings.port)
        LOGGER.info("Service host Python: %s", sys.executable)
        LOGGER.info("Web venv site-packages: %s", VENV_SITE_PACKAGES)
        try:
            extension_status = install_runtime_extensions()
            LOGGER.info("Runtime extensions: %s", extension_status)

            self.loop = asyncio.SelectorEventLoop()
            asyncio.set_event_loop(self.loop)

            config = uvicorn.Config(
                "webapp.main:app",
                host=settings.host,
                port=settings.port,
                reload=False,
                access_log=False,
                log_level="info",
                log_config=None,
            )
            self.server = ServiceUvicornServer(config)
            self.loop.run_until_complete(self.server.serve())
            LOGGER.info("Service stopped normally")
        except Exception:
            LOGGER.exception("Service crashed")
            servicemanager.LogErrorMsg(f"{self._svc_display_name_} crashed; see {LOG_FILE}")
            raise
        finally:
            if self.loop is not None and not self.loop.is_closed():
                self.loop.close()


if __name__ == "__main__":
    win32serviceutil.HandleCommandLine(LocalAIChatWebService)
