"""The ``/start`` onboarding handler module (thin adapter, Req 1.1-1.7).

Exposes :func:`build_start_handlers`, the builder closure the composition root
(task 11.1) calls with the live :class:`~brizocast.services.user_service.UserService`
to obtain the python-telegram-bot handler objects to register. The onboarding
state machine itself lives in
:mod:`brizocast.bot.conversations.onboarding`; this module is the handler-layer
entry point that wires the injected service into it.

Keeping the builder here (rather than instantiating handlers at import time)
means the handler receives its service dependency via the closure and never
reaches for a global, matching the thin-handler + DI pattern used across the
bot layer.
"""

from __future__ import annotations

from typing import Any

from telegram.ext import BaseHandler, ContextTypes

from brizocast.bot.conversations.onboarding import build_onboarding_conversation
from brizocast.core.logging import BoundLogger
from brizocast.services.user_service import UserService

__all__ = ["build_start_handlers"]


def build_start_handlers(
    user_service: UserService,
    *,
    logger: BoundLogger | None = None,
) -> list[BaseHandler[Any, ContextTypes.DEFAULT_TYPE, Any]]:
    """Build the ``/start`` onboarding handlers bound to ``user_service``.

    :param user_service: The application's user provisioning service, injected
        into the onboarding conversation via a closure.
    :param logger: Optional bound logger forwarded to the conversation.
    :returns: The handler objects (a single onboarding
        :class:`~telegram.ext.ConversationHandler`) for the composition root to
        register on the ``Application``.
    """

    return [build_onboarding_conversation(user_service, logger=logger)]
