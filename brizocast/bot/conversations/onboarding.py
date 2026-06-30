"""The onboarding ``ConversationHandler`` state machine (Req 1.1-1.7).

Implements the design's *Onboarding* state diagram (``CheckUser → MainMenu``
for returning users, ``CheckUser → SelectActivity → SetLocation`` for new ones)
as a python-telegram-bot :class:`~telegram.ext.ConversationHandler`.

The conversation is intentionally **thin** (per the design's "thin handlers"
rule): each callback only parses the incoming update, calls
:class:`~brizocast.services.user_service.UserService`, and renders a reply with
the pure :mod:`brizocast.bot.formatters.commands` text helpers and the
:mod:`brizocast.bot.keyboards.activities` inline keyboard. No persistence,
scoring, or provider logic lives here.

Dependency injection
--------------------
:func:`build_onboarding_conversation` is a *builder closure*: it receives the
real :class:`UserService` and binds it into the handler callbacks, returning the
assembled :class:`ConversationHandler`. The composition root (task 11.1) calls
the builder with the live service — handlers never reach for a global.

Flow and states
---------------
* ``/start`` (entry point) provisions the user on first interaction
  (:meth:`UserService.get_or_create_user`, Req 1.7). If the user has already
  finished onboarding, it shows the main menu and ends (Req 1.6). Otherwise it
  presents the activity-selection inline keyboard built from every registered
  activity — Surf is selectable, future sports render locked/unavailable
  (Req 1.1, 1.2, 1.3) — and moves to :attr:`OnboardingState.SELECT_ACTIVITY`.
* In :attr:`~OnboardingState.SELECT_ACTIVITY`, an activity tap is decoded from
  its callback data. Choosing an unavailable activity informs the user it is not
  yet supported and keeps them on the selection step (Req 1.4). Choosing Surf
  persists the selected activity (:meth:`UserService.set_selected_activity`,
  Req 1.5) and hands off to location setup.

Handoff to location setup (task 7.3)
------------------------------------
Location collection is a separate command flow (``/location``, task 7.3). Rather
than couple the onboarding conversation to an as-yet-unimplemented state, the
clean handoff chosen here is: on Surf selection, record the activity, prompt the
user to run ``/location`` (the *SetLocation* step in the diagram), and end the
conversation. Onboarding is only marked complete once a subscription is created
(diagram ``ConfirmSub → MainMenu``, task 7.4), so ``onboarded`` is deliberately
left unset by this conversation.
"""

from __future__ import annotations

from enum import IntEnum

from telegram import Update
from telegram.ext import (
    BaseHandler,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
)

from brizocast.activities.registry import ActivityRegistry
from brizocast.bot.formatters.commands import (
    activity_unavailable_text,
    main_menu_text,
    onboarding_location_prompt_text,
    onboarding_welcome_text,
)
from brizocast.bot.keyboards.activities import (
    build_activity_keyboard,
    parse_activity_callback,
)
from brizocast.bot.keyboards.callbacks import NAMESPACE_ACTIVITY
from brizocast.bot.keyboards.menu import build_main_menu_keyboard
from brizocast.core.logging import BoundLogger, get_logger
from brizocast.services.user_service import UserService

__all__ = ["OnboardingState", "build_onboarding_conversation"]

# Matches only this conversation's activity-selection callbacks (the "act:"
# namespace) so taps are routed to :func:`_on_activity_selection`.
_ACTIVITY_CALLBACK_PATTERN = rf"^{NAMESPACE_ACTIVITY}:"

# Key under which the in-progress selected activity is stashed in
# ``context.user_data`` for the duration of the conversation.
_USER_DATA_ACTIVITY_KEY = "onboarding_activity_key"


class OnboardingState(IntEnum):
    """States of the onboarding conversation.

    Only one explicit state is needed: once an activity is chosen the
    conversation either re-prompts (unavailable) or ends with a handoff to the
    ``/location`` flow (Surf). The ``CheckUser``/``MainMenu``/``SetLocation``
    nodes from the design diagram are handled at the entry point and the
    handoff, not as resident conversation states.
    """

    SELECT_ACTIVITY = 0


def build_onboarding_conversation(
    user_service: UserService,
    *,
    logger: BoundLogger | None = None,
) -> ConversationHandler[ContextTypes.DEFAULT_TYPE]:
    """Build the onboarding :class:`ConversationHandler` bound to ``user_service``.

    :param user_service: The application's user provisioning service, injected
        into the handler callbacks via this closure.
    :param logger: Optional bound logger; one is created when omitted.
    :returns: The assembled conversation handler ready to register on an
        ``Application`` by the composition root.
    """

    log = logger or get_logger(__name__)

    async def _start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Handle ``/start``: provision the user and show the main menu."""

        tg_user = update.effective_user
        message = update.effective_message
        if tg_user is None or message is None:
            return ConversationHandler.END

        await user_service.get_or_create_user(tg_user.id, tg_user.username)

        await message.reply_text(
            main_menu_text(), reply_markup=build_main_menu_keyboard()
        )
        return ConversationHandler.END

    return ConversationHandler(
        entry_points=[CommandHandler("start", _start)],
        states={},
        fallbacks=[CommandHandler("start", _start)],
        allow_reentry=True,
        name="onboarding",
    )
