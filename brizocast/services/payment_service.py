"""Guarded payment-recording entry point (Req 20.5, 20.6).

The ``payment_records`` table exists from day one as a reserved billing surface
(Req 16.8, 20.5) but must **never** be populated while
:attr:`Settings.MONETIZATION_ENABLED` is disabled (Req 20.6). The MVP collects
no payment at all, so rather than scatter ``if MONETIZATION_ENABLED`` checks at
every (currently non-existent) call site, ``PaymentRecordingService`` is the
single, safe place through which any future payment write must pass.

Guard design
------------
:meth:`PaymentRecordingService.record_payment` is the one entry point that
writes a :class:`~brizocast.models.payment.PaymentRecord`. It reads the
monetization flag fresh from the injected :class:`Settings` on every call:

* **While monetization is disabled** it writes nothing and raises
  :class:`~brizocast.core.errors.MonetizationDisabledError` after logging the
  blocked attempt. Raising (rather than silently no-opping) makes accidental
  misuse loud while still guaranteeing the table stays empty — the repository is
  never touched, so no row can be created (Req 20.6).
* **While monetization is enabled** it persists the payment record against the
  plan via :class:`~brizocast.repositories.payment_repo.SqlAlchemyPaymentRepository`
  in one unit of work and returns the stored entity. This path is reserved for
  the future payment integration; the MVP never enables the flag.

Because the flag is read from :class:`Settings` on every call, enabling
monetization later changes behaviour with no code edits to this guard
(consistent with Req 21.7's config-driven philosophy).

Requirements covered: 20.5, 20.6 (supports Property 30).
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brizocast.config.settings import Settings
from brizocast.core.errors import MonetizationDisabledError
from brizocast.core.logging import BoundLogger, get_logger
from brizocast.database.session import session_scope
from brizocast.models.payment import PaymentRecord
from brizocast.repositories.payment_repo import SqlAlchemyPaymentRepository

__all__ = ["PaymentRecordingService"]


class PaymentRecordingService:
    """The single guarded entry point for writing payment records (Req 20.5, 20.6)."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        *,
        logger: BoundLogger | None = None,
    ) -> None:
        """Initialise the service.

        Args:
            session_factory: The application's ``async_sessionmaker``; a payment
                write opens one session via ``session_scope``.
            settings: The validated application configuration. The monetization
                flag is read from here on every call so behaviour tracks config.
            logger: Optional bound logger; one is created when omitted.
        """
        self._session_factory = session_factory
        self._settings = settings
        self._log = logger or get_logger(__name__)

    async def record_payment(
        self,
        plan_id: int,
        *,
        provider: str | None = None,
        external_txn_id: str | None = None,
        amount_cents: int | None = None,
        currency: str | None = None,
        status: str | None = None,
    ) -> PaymentRecord:
        """Persist a payment record for ``plan_id`` — guarded by the flag.

        Args:
            plan_id: The plan the payment is associated with.
            provider: Optional payment provider key.
            external_txn_id: Optional provider-side transaction id.
            amount_cents: Optional amount in minor currency units.
            currency: Optional ISO currency code.
            status: Optional provider-side status string.

        Returns:
            The persisted :class:`~brizocast.models.payment.PaymentRecord` (only
            when monetization is enabled).

        Raises:
            MonetizationDisabledError: While ``MONETIZATION_ENABLED`` is
                disabled — no row is written, guaranteeing ``payment_records``
                stays empty in the MVP (Req 20.6).
        """
        if not self._settings.MONETIZATION_ENABLED:
            self._log.warning(
                "blocked payment recording for plan %s: monetization disabled",
                plan_id,
            )
            raise MonetizationDisabledError(
                "cannot record a payment while monetization is disabled"
            )

        async with session_scope(self._session_factory) as session:
            payments = SqlAlchemyPaymentRepository(session)
            record = await payments.add(
                PaymentRecord(
                    plan_id=plan_id,
                    provider=provider,
                    external_txn_id=external_txn_id,
                    amount_cents=amount_cents,
                    currency=currency,
                    status=status,
                )
            )
        self._log.info("recorded payment for plan %s", plan_id)
        return record
