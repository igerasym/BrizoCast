"""Domain exception hierarchy for BrizoCast.

All errors raised by BrizoCast's own code derive from :class:`BrizoCastError`,
giving the application a single, catchable root for domain failures and keeping
them distinct from incidental runtime errors (``ValueError``, ``KeyError``, …).

The hierarchy mirrors the boundaries described in the design's *Error Handling*
section, where each failure is mapped to a handling policy:

* :class:`ConfigurationError` — required configuration missing or invalid at
  startup; startup terminates and logs the offending field (Req 15.3, 15.4,
  18.4).
* :class:`ProviderRequestError` — an external forecast / geocoding / AI request
  failed; the caller logs with provider context and degrades gracefully (skips
  the spot, informs the user, or falls back to static presets) (Req 6.5, 2.11,
  19.8, 18.2).
* :class:`QuotaExceededError` — a monetization gate was hit; the bot informs the
  user of the limit. Carries the limit that was exceeded (Req 21.4).
* :class:`NotFoundError` — a requested entity (user, subscription, preset, …)
  does not exist.
* :class:`DomainValidationError` — a user-supplied value violates a domain rule
  such as the search-radius range or ``min_wave <= max_wave`` (Req 3.10, 4.8).

The module is intentionally free of any framework, provider, or persistence
imports so it can be imported from every layer, including the pure domain core.

Requirements covered: 18.6 (and the architectural foundation for the composition
root used by 11.1).
"""

from __future__ import annotations

__all__ = [
    "BrizoCastError",
    "ConfigurationError",
    "ProviderRequestError",
    "QuotaExceededError",
    "MonetizationDisabledError",
    "NotFoundError",
    "DomainValidationError",
]


class BrizoCastError(Exception):
    """Base class for every domain error raised within BrizoCast.

    Catching this type at a boundary catches all application-defined failures
    while letting unexpected, non-domain exceptions propagate.
    """


class ConfigurationError(BrizoCastError):
    """Raised when required configuration is missing or invalid at startup.

    The composition root raises this (or re-raises after logging) so that
    startup terminates loudly, naming the offending field (Req 15.3, 15.4,
    18.4).
    """

    def __init__(self, message: str, *, field: str | None = None) -> None:
        """Initialise the error.

        Args:
            message: Human-readable description of the configuration problem.
            field: Optional name of the specific configuration field at fault.
        """
        super().__init__(message)
        self.field = field


class ProviderRequestError(BrizoCastError):
    """Raised when an external provider request fails.

    Covers forecast, geocoding, and AI providers. The handling boundary logs
    this with provider/request context and degrades gracefully rather than
    aborting the run (Req 6.5, 2.11, 19.8, 18.2).
    """

    def __init__(
        self,
        message: str,
        *,
        provider: str | None = None,
    ) -> None:
        """Initialise the error.

        Args:
            message: Human-readable description of the request failure.
            provider: Optional provider key/name that failed (e.g.
                ``"open_meteo_marine"``), used for structured log context.
        """
        super().__init__(message)
        self.provider = provider


class QuotaExceededError(BrizoCastError):
    """Raised when an action would exceed a plan's quota (monetization gate).

    Carries the limit that was exceeded so the bot can tell the user exactly
    what the cap is (Req 21.4). Only raised when monetization is enabled.
    """

    def __init__(self, message: str, *, limit: int) -> None:
        """Initialise the error.

        Args:
            message: Human-readable description of the quota that was hit.
            limit: The numeric limit that was exceeded (e.g. the maximum number
                of subscriptions allowed on the user's plan).
        """
        super().__init__(message)
        self.limit = limit


class MonetizationDisabledError(BrizoCastError):
    """Raised when a payment-recording attempt is made while monetization is off.

    The MVP collects no payment and never populates ``payment_records`` while
    :attr:`Settings.MONETIZATION_ENABLED` is disabled (Req 20.6). The guarded
    payment-recording entry point
    (:class:`brizocast.services.payment_service.PaymentRecordingService`) raises
    this rather than writing a row, so the reserved table can never be populated
    by accident while the flag is off. Carries no payload; the message names the
    blocked operation.
    """


class NotFoundError(BrizoCastError):
    """Raised when a requested entity cannot be found.

    Examples include looking up a user, subscription, preset, or favorite that
    does not exist.
    """


class DomainValidationError(BrizoCastError):
    """Raised when a user-supplied value violates a domain rule.

    Examples include a search radius outside the accepted ``[1, 200]`` km range
    or a custom-conditions entry whose minimum wave height exceeds its maximum
    (Req 3.10, 4.8). The handling boundary surfaces the message to the user and
    re-requests the value.
    """
