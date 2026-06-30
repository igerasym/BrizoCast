"""Admin panel route groups.

Each module here exposes a module-level ``router`` (:class:`fastapi.APIRouter`)
that the app factory includes under the app-wide Basic-Auth dependency. The
routers are thin HTTP adapters over the reused bot services and the panel-only
admin services; all rendering is server-side Jinja2 (with HTMX for swaps).
"""

from __future__ import annotations
