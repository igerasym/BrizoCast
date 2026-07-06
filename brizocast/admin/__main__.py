"""Uvicorn entrypoint for the admin panel: ``python -m brizocast.admin``.

Loads and validates :class:`~brizocast.admin.settings.PanelSettings` (aborting
startup with :class:`SystemExit` if the admin credential is missing), builds the
FastAPI application via :func:`brizocast.admin.app.build_admin_app`, and serves
it with uvicorn.

Inside the container the app always listens on port ``8000``; Docker Compose
publishes it only on ``${ADMIN_BIND_HOST}`` (the host's LAN address) at
``${ADMIN_PORT}``. Uvicorn binds to ``panel.ADMIN_BIND_HOST`` — never
``0.0.0.0`` — so the panel is not exposed on all interfaces (Req 1.5).
"""

from __future__ import annotations

import uvicorn

from brizocast.admin.app import build_admin_app
from brizocast.admin.settings import load_panel_settings


def main() -> None:
    """Load panel settings, build the app, and run uvicorn on container port 8000.

    Inside Docker the app always binds 0.0.0.0:8000 — Docker Compose's port
    mapping restricts external access to ADMIN_BIND_HOST:ADMIN_PORT. Outside
    Docker (local dev) it binds to ADMIN_BIND_HOST:ADMIN_PORT directly.
    """
    import os

    panel = load_panel_settings()
    app = build_admin_app(panel)

    # Detect if running inside a container (presence of /.dockerenv or cgroup).
    in_docker = os.path.exists("/.dockerenv") or os.path.isfile("/proc/1/cgroup")
    if in_docker:
        uvicorn.run(app, host="0.0.0.0", port=8000)
    else:
        uvicorn.run(app, host=panel.ADMIN_BIND_HOST, port=panel.ADMIN_PORT)


if __name__ == "__main__":
    main()
