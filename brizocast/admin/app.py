"""FastAPI application factory and supporting wiring for the admin panel.

:func:`build_admin_app` constructs the ``BrizoCast Admin`` FastAPI application:
the panel's own :class:`~brizocast.core.container.Container` over the shared
database (via :func:`brizocast.admin.dependencies.build_panel_container`), the
override-aware settings facade (via
:func:`brizocast.admin.dependencies.build_overrides`), and the activity registry
(via :func:`brizocast.activities.bootstrap.register_builtin_activities`) so any
reused service can resolve activities.

HTTP Basic Auth is applied **app-wide** through ``dependencies=[Depends(
require_admin)]`` so every route — including routers added later (task 7.1) — is
protected in one place (Req 1.1). The factory also:

* stashes the container, panel settings, and override facade on ``app.state``
  so the ``Depends`` providers in :mod:`brizocast.admin.dependencies` can read
  the live wiring;
* mounts the vendored static assets under ``/static`` (no CDN) and configures
  Jinja2 templates on ``app.state.templates`` for the routers (wave 3);
* runs a startup hook that bootstraps the shared schema (creating the new admin
  tables) and seeds the surf-spot dataset onto the ``./data`` volume; and
* sets restrictive security headers (``Cache-Control: no-store`` and a
  self-only Content-Security-Policy) on every response (Req 1.5, 13.4).

Routers are intentionally **not** included yet — that is task 7.1.

Requirements covered: 1.1, 1.5, 13.4.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import Depends, FastAPI, Request, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import RequestResponseEndpoint

from brizocast.activities.bootstrap import register_builtin_activities
from brizocast.admin.auth import require_admin
from brizocast.admin.dependencies import build_overrides, build_panel_container
from brizocast.admin.flash import prepare_csrf, set_csrf_cookie
from brizocast.admin.routers import (
    feedback,
    forecast,
    logs,
    presets,
    spots,
    subscriptions,
    users,
)
from brizocast.admin.settings import PanelSettings
from brizocast.core.logging import get_logger
from brizocast.database.bootstrap import bootstrap_database
from brizocast.database.session import create_engine

__all__ = ["build_admin_app"]

logger = get_logger(__name__)

# Directory of this module; the vendored static assets and Jinja2 templates live
# alongside it under ``admin/static`` and ``admin/templates``.
_ADMIN_DIR = Path(__file__).resolve().parent


def _static_dir() -> str:
    """Return the absolute path to the vendored ``admin/static`` directory."""
    return str(_ADMIN_DIR / "static")


def _templates_dir() -> str:
    """Return the absolute path to the ``admin/templates`` directory."""
    return str(_ADMIN_DIR / "templates")


def build_admin_app(panel: PanelSettings) -> FastAPI:
    """Build the ``BrizoCast Admin`` FastAPI application.

    Args:
        panel: The validated :class:`PanelSettings` carrying the admin
            credential, shared database URL, bind host, and dataset path.

    Returns:
        A configured :class:`~fastapi.FastAPI` application with app-wide Basic
        Auth (Req 1.1), mounted static assets, configured templates, a
        schema-bootstrap + dataset-seed startup hook, and per-response security
        headers (Req 1.5, 13.4). Routers are added later (task 7.1).
    """

    # Panel-owned container + override facade over the shared database
    # (Req 12.5, 13.4), and the activity registry populated for reused services.
    container = build_panel_container(panel)
    overrides = build_overrides(panel)
    register_builtin_activities()

    # App-wide Basic Auth so every route is protected in one place (Req 1.1).
    app = FastAPI(
        title="BrizoCast Admin",
        dependencies=[Depends(require_admin)],
    )
    app.state.container = container
    app.state.panel = panel
    app.state.overrides = overrides

    # Vendored static assets (htmx + CSS); no CDN so the self-only CSP holds.
    app.mount("/static", StaticFiles(directory=_static_dir()), name="static")

    # Jinja2 templates for the routers to render.
    app.state.templates = Jinja2Templates(directory=_templates_dir())

    # All page/route groups, each protected by the app-wide auth dependency
    # (Req 1.1). Every new module is reachable from here (no orphaned code).
    for router in (
        users,
        subscriptions,
        spots,
        presets,
        forecast,
        feedback,
        logs,
    ):
        app.include_router(router.router)

    @app.get("/", include_in_schema=False)
    async def root_redirect() -> Response:
        from fastapi.responses import RedirectResponse as RR
        return RR(url="/users")

    @app.on_event("startup")
    async def _startup() -> None:
        """Bootstrap the shared schema and seed the surf-spot dataset."""
        # A fresh engine on the shared DB URL is the simplest way to bootstrap
        # the schema; this also creates the new admin tables (Req 16.4 reuse).
        engine = create_engine(panel.DATABASE_URL)
        await bootstrap_database(engine)

        # ``ensure_spot_dataset_seeded`` is delivered later by task 3.1; import
        # it lazily and skip (with a warning) so the app still boots before it
        # lands.
        try:
            from brizocast.repositories.json_spot_repo import (
                ensure_spot_dataset_seeded,
            )
        except ImportError:
            logger.warning(
                "ensure_spot_dataset_seeded not available yet (task 3.1 "
                "pending); skipping surf-spot dataset seeding on startup."
            )
        else:
            await ensure_spot_dataset_seeded(panel.SPOT_DATASET_PATH)

    @app.middleware("http")
    async def _panel_middleware(
        request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        """Issue a CSRF token and set restrictive caching + CSP headers (Req 1.5).

        The raw CSRF token is resolved before the endpoint renders and stashed on
        ``request.state.csrf_token`` so templates can embed it in mutating forms;
        a fresh signed cookie is written on the way out when one is needed.
        """
        raw_token, signed_cookie = prepare_csrf(request)
        request.state.csrf_token = raw_token
        response = await call_next(request)
        if signed_cookie is not None:
            set_csrf_cookie(response, signed_cookie)
        response.headers["Cache-Control"] = "no-store"
        # htmx and CSS are vendored under /static, so a self-only policy holds.
        response.headers["Content-Security-Policy"] = "default-src 'self'"
        return response

    return app
