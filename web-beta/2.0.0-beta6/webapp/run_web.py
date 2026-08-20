from __future__ import annotations

import uvicorn

from webapp.config import settings
from webapp.runtime_extensions import install_runtime_extensions


if __name__ == "__main__":
    install_runtime_extensions()
    uvicorn.run("webapp.main:app", host=settings.host, port=settings.port, reload=False)
