"""Signed-cookie flash messages and per-session CSRF protection for the panel.

The admin panel is a server-rendered FastAPI app whose mutations are HTMX
``POST`` / ``DELETE`` requests. This module supplies two small, dependency-free
(beyond ``itsdangerous``) helpers used across the routers:

* **Flash messages** — a one-shot status message (e.g. "saved", or a validation
  error) carried to the next render in a *signed* cookie so it cannot be forged
  or tampered with. :func:`set_flash` writes it on a response; :func:`pop_flash`
  reads and consumes it on the next request.
* **CSRF protection** — although HTTP Basic Auth credentials are not ambient
  cookies (the browser sends the ``Authorization`` header per request, which
  blunts classic CSRF), the panel still defends writes with a double-submit
  token: :func:`issue_csrf_token` ensures a signed token cookie exists and
  returns the raw token to embed in forms / ``hx-headers``; :func:`require_csrf`
  is a FastAPI dependency that rejects unsafe-method requests whose submitted
  token does not match the signed cookie.

Signing secret — trade-off
---------------------------
:class:`~brizocast.admin.settings.PanelSettings` has no dedicated ``SECRET``
field. Rather than overloading ``ADMIN_PASSWORD`` as a signing seed, this module
derives a **per-process random secret** with :func:`secrets.token_urlsafe`. This
is deliberately simple and safe for the single-instance, trusted-LAN panel. The
trade-off is that flash cookies and CSRF tokens issued before a restart become
invalid afterward: a form rendered before a restart that is submitted after it
is rejected with a fresh CSRF challenge, and an in-flight flash is dropped. For a
single-operator LAN tool this is an acceptable cost for not having to manage a
persistent secret.

Requirements covered: 12.5 (CSRF / POST safety from design).
"""

from __future__ import annotations

import secrets
from typing import Final

from fastapi import HTTPException, Request, Response, status
from itsdangerous import BadSignature, TimestampSigner, URLSafeSerializer

__all__ = [
    "CSRF_COOKIE_NAME",
    "CSRF_FORM_FIELD",
    "CSRF_HEADER_NAME",
    "FLASH_COOKIE_NAME",
    "issue_csrf_token",
    "pop_flash",
    "prepare_csrf",
    "require_csrf",
    "set_csrf_cookie",
    "set_flash",
]

#: Cookie carrying the signed, serialised flash payload.
FLASH_COOKIE_NAME: Final = "brizocast_flash"
#: Cookie carrying the signed CSRF token.
CSRF_COOKIE_NAME: Final = "brizocast_csrf"
#: Form field a mutating request may carry its CSRF token in.
CSRF_FORM_FIELD: Final = "csrf_token"
#: Request header a mutating request may carry its CSRF token in (HTMX-friendly).
CSRF_HEADER_NAME: Final = "X-CSRF-Token"

# HTTP methods that never mutate state and therefore bypass the CSRF check.
_SAFE_METHODS: Final[frozenset[str]] = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})

# CSRF tokens expire this many seconds after issuance (12 hours).
_CSRF_MAX_AGE_SECONDS: Final = 12 * 60 * 60

# Per-process random signing secret (see the module docstring for the trade-off).
_SECRET: Final = secrets.token_urlsafe(32)

# Distinct salts so a flash payload can never be mistaken for a CSRF token even
# though both are signed with the same per-process secret.
_flash_serializer: Final = URLSafeSerializer(_SECRET, salt="brizocast-flash")
_csrf_signer: Final = TimestampSigner(_SECRET, salt="brizocast-csrf")


def set_flash(
    response: Response, message: str, category: str = "success"
) -> None:
    """Attach a one-shot flash message to ``response`` via a signed cookie.

    Args:
        response: The response the flash cookie is written on.
        message: The human-readable message to surface on the next render.
        category: A UI category (e.g. ``"success"`` or ``"error"``) the template
            can map to styling. Defaults to ``"success"``.
    """
    payload = _flash_serializer.dumps({"category": category, "message": message})
    response.set_cookie(
        FLASH_COOKIE_NAME,
        payload,
        max_age=_CSRF_MAX_AGE_SECONDS,
        httponly=True,
        samesite="strict",
        path="/",
    )


def pop_flash(request: Request) -> dict[str, str] | None:
    """Read and consume the flash message carried on ``request``.

    Returns the decoded ``{"category", "message"}`` mapping, or ``None`` when no
    (valid) flash cookie is present. The request is marked consumed on
    ``request.state.flash_consumed`` so the response layer can delete the cookie;
    a tampered or unparseable cookie is treated as absent.

    Args:
        request: The incoming request whose flash cookie is read.

    Returns:
        The decoded flash mapping, or ``None`` if there is none to show.
    """
    raw = request.cookies.get(FLASH_COOKIE_NAME)
    if raw is None:
        return None
    try:
        decoded: object = _flash_serializer.loads(raw)
    except BadSignature:
        return None
    # Signal the response layer that the one-shot cookie should be cleared.
    request.state.flash_consumed = True
    if not isinstance(decoded, dict):
        return None
    return {
        "category": str(decoded.get("category", "success")),
        "message": str(decoded.get("message", "")),
    }


def issue_csrf_token(request: Request, response: Response) -> str:
    """Ensure a signed CSRF token cookie exists and return the raw token.

    If ``request`` already carries a valid, unexpired CSRF cookie its token is
    reused; otherwise a fresh token is generated, signed, and written on
    ``response``. The returned raw token is what callers embed in a hidden form
    field (:data:`CSRF_FORM_FIELD`) or an ``hx-headers`` entry
    (:data:`CSRF_HEADER_NAME`); :func:`require_csrf` validates the submitted raw
    token against the signed cookie.

    Args:
        request: The incoming request, inspected for an existing token cookie.
        response: The response a freshly issued token cookie is written on.

    Returns:
        The raw (unsigned) CSRF token to embed in the rendered page.
    """
    existing = request.cookies.get(CSRF_COOKIE_NAME)
    if existing is not None:
        try:
            return _csrf_signer.unsign(
                existing, max_age=_CSRF_MAX_AGE_SECONDS
            ).decode("utf-8")
        except BadSignature:
            pass  # fall through and issue a fresh token
    token = secrets.token_urlsafe(32)
    signed = _csrf_signer.sign(token).decode("utf-8")
    response.set_cookie(
        CSRF_COOKIE_NAME,
        signed,
        max_age=_CSRF_MAX_AGE_SECONDS,
        httponly=True,
        samesite="strict",
        path="/",
    )
    return token


async def require_csrf(request: Request) -> None:
    """FastAPI dependency rejecting unsafe-method requests with a bad CSRF token.

    Safe methods (``GET``/``HEAD``/``OPTIONS``/``TRACE``) pass through untouched.
    For mutating methods (notably ``POST``/``DELETE``) the submitted token — read
    from the :data:`CSRF_HEADER_NAME` header or, failing that, the
    :data:`CSRF_FORM_FIELD` form field — is compared in constant time against the
    token recovered from the signed :data:`CSRF_COOKIE_NAME` cookie.

    Args:
        request: The incoming request to validate.

    Raises:
        HTTPException: ``403 Forbidden`` when the CSRF cookie is missing,
            expired, tampered with, or absent/mismatched against the submitted
            token.
    """
    if request.method in _SAFE_METHODS:
        return

    cookie = request.cookies.get(CSRF_COOKIE_NAME)
    if cookie is None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, detail="Missing CSRF token cookie."
        )
    try:
        expected = _csrf_signer.unsign(
            cookie, max_age=_CSRF_MAX_AGE_SECONDS
        ).decode("utf-8")
    except BadSignature as exc:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token cookie."
        ) from exc

    submitted = request.headers.get(CSRF_HEADER_NAME)
    if submitted is None:
        form = await request.form()
        value = form.get(CSRF_FORM_FIELD)
        submitted = value if isinstance(value, str) else None

    if submitted is None or not secrets.compare_digest(submitted, expected):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, detail="CSRF token mismatch."
        )


def prepare_csrf(request: Request) -> tuple[str, str | None]:
    """Resolve the request's CSRF token, issuing a fresh one if needed.

    Returns ``(raw_token, signed_cookie_value)`` where ``raw_token`` is the
    value to embed in forms / ``hx-headers`` and ``signed_cookie_value`` is the
    signed cookie to set — or ``None`` when the request already carries a valid,
    unexpired CSRF cookie (so no new cookie need be written). Intended to be
    called from middleware *before* the endpoint renders, so templates can read
    the raw token off ``request.state``.
    """
    existing = request.cookies.get(CSRF_COOKIE_NAME)
    if existing is not None:
        try:
            raw = _csrf_signer.unsign(
                existing, max_age=_CSRF_MAX_AGE_SECONDS
            ).decode("utf-8")
            return raw, None
        except BadSignature:
            pass
    raw = secrets.token_urlsafe(32)
    signed = _csrf_signer.sign(raw).decode("utf-8")
    return raw, signed


def set_csrf_cookie(response: Response, signed: str) -> None:
    """Write the signed CSRF token ``signed`` as a hardened cookie on ``response``."""
    response.set_cookie(
        CSRF_COOKIE_NAME,
        signed,
        max_age=_CSRF_MAX_AGE_SECONDS,
        httponly=True,
        samesite="strict",
        path="/",
    )
