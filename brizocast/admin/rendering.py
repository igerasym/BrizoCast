"""Shared template-rendering helper for the admin routers.

Centralises the one-shot flash-message handling so every page renders the flash
set by the preceding redirect (the Post/Redirect/Get pattern) exactly once: the
flash is popped from its signed cookie, injected into the template context, and
the cookie is cleared on the rendered response.
"""

from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse

from brizocast.admin.flash import FLASH_COOKIE_NAME, pop_flash

__all__ = ["render"]


def render(
    request: Request, template_name: str, context: dict[str, Any]
) -> HTMLResponse:
    """Render ``template_name`` with ``context`` plus the one-shot flash message.

    Pops any flash message from the request's signed cookie, adds it to the
    context as ``flash``, renders the template, and clears the flash cookie on
    the response so it is shown only once.
    """
    templates = request.app.state.templates
    flash = pop_flash(request)
    merged: dict[str, Any] = {**context, "flash": flash}
    response: HTMLResponse = templates.TemplateResponse(request, template_name, merged)
    if flash is not None:
        response.delete_cookie(FLASH_COOKIE_NAME, path="/")
    return response
