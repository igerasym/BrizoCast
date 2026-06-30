"""HTTP Basic Authentication dependency for the admin panel.

Every admin-panel route is protected by HTTP Basic Auth using the single
:class:`~brizocast.admin.settings.PanelSettings` ``ADMIN_USERNAME`` /
``ADMIN_PASSWORD`` credential (Req 1.1, 1.6). :func:`require_admin` is applied
app-wide as a FastAPI dependency so the challenge logic lives in exactly one
place.

The dependency uses ``HTTPBasic(auto_error=False)`` so that *this* module crafts
the ``401`` + ``WWW-Authenticate: Basic`` challenge rather than relying on the
default behavior, giving uniform responses for both the missing-credentials
(Req 1.2) and wrong-credentials (Req 1.3) cases. Credentials are compared with
:func:`secrets.compare_digest` on the UTF-8 ``bytes`` of both the username and
the password, and the two boolean results are combined with the bitwise ``&``
operator (not ``and``) so the comparison does not short-circuit — neither field
leaks via response timing.

Requirements covered: 1.2, 1.3, 1.4, 1.6.
"""

from __future__ import annotations

import secrets

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from brizocast.admin.settings import PanelSettings

# ``auto_error=False`` so we emit the 401 + challenge ourselves (Req 1.2, 1.3).
_basic = HTTPBasic(auto_error=False)


def require_admin(
    request: Request,
    creds: HTTPBasicCredentials | None = Depends(_basic),
) -> str:
    """Authorize a request via HTTP Basic Auth against the configured credential.

    Args:
        request: The incoming request; its ``app.state.panel`` holds the
            validated :class:`PanelSettings` carrying the admin credential.
        creds: Parsed Basic-Auth credentials, or ``None`` when the request
            carries no (or an unparseable) ``Authorization`` header.

    Returns:
        The authenticated administrator's username (Req 1.4).

    Raises:
        HTTPException: ``401 Unauthorized`` with a ``WWW-Authenticate: Basic``
            challenge when credentials are missing (Req 1.2) or do not match the
            configured credential (Req 1.3).
    """

    panel: PanelSettings = request.app.state.panel
    if creds is None:
        # Req 1.2 — no/unparseable credentials: challenge for authentication.
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            headers={"WWW-Authenticate": "Basic"},
        )
    # Constant-time compare of BOTH fields. The bitwise ``&`` (not ``and``)
    # avoids short-circuit evaluation so neither the username nor the password
    # leaks through response timing.
    user_ok = secrets.compare_digest(
        creds.username.encode("utf-8"), panel.ADMIN_USERNAME.encode("utf-8")
    )
    pass_ok = secrets.compare_digest(
        creds.password.encode("utf-8"), panel.ADMIN_PASSWORD.encode("utf-8")
    )
    if not (user_ok & pass_ok):
        # Req 1.3 — wrong credentials: 401 (with challenge, matching Req 1.2).
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            headers={"WWW-Authenticate": "Basic"},
        )
    return creds.username  # Req 1.4 — authorized; the route proceeds.
