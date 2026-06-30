"""``/status`` and ``/forecast`` handlers (thin Telegram adapters, Req 13.3, 13.4).

Two thin ``python-telegram-bot`` handlers assembled by
:func:`build_status_handlers`, following the handler-registration / DI pattern
used across the bot layer (e.g. task 7.2): the composition root (task 11.1)
passes the live services and gets back the handlers to register on the
``Application``; this module never touches the ``Application`` itself. All
status/forecast logic lives in
:class:`~brizocast.services.status_service.StatusService` — the handlers only
parse the update, call a service, and format a reply.

Commands implemented
---------------------
* ``/status`` — reports the user's active-subscription count and the most recent
  scheduler-run time, rendered by
  :func:`~brizocast.bot.formatters.commands.format_status` (Req 13.3).
* ``/forecast`` — a small :class:`telegram.ext.ConversationHandler`: it shows a
  subscription-pick keyboard, then reports the current best surf score and spot
  for the chosen subscription via
  :meth:`StatusService.best_forecast_for_subscription`, **regardless of the
  subscription's mute or snooze state** (Req 13.4). The result is rendered by
  :func:`~brizocast.bot.formatters.commands.format_forecast_result`, or
  :func:`~brizocast.bot.formatters.commands.format_forecast_no_spots` when no
  scorable spot is within range.

User resolution
---------------
Both commands key off the internal database user id cached by the onboarding
layer (task 7.2) under :data:`USER_DB_ID_KEY` in ``context.user_data``; when it
is absent the handler asks the user to run ``/start`` first.

Requirements covered: 13.3, 13.4.
"""

from __future__ import annotations

from typing import Final

from telegram import Update
from telegram.ext import (
    BaseHandler,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
)

from brizocast.bot.formatters.commands import (
    format_forecast_no_spots,
    format_forecast_result,
    format_status,
)
from brizocast.bot.keyboards.menu import (
    any_menu_label_filter,
    build_main_menu_keyboard,
)
from brizocast.bot.keyboards.subscriptions import (
    SubscriptionPickPurpose,
    build_subscription_pick_keyboard,
    parse_subscription_callback,
)
from brizocast.core.errors import NotFoundError
from brizocast.services.status_service import BestForecast, StatusService
from brizocast.services.subscription_service import SubscriptionService

__all__ = ["USER_DB_ID_KEY", "build_status_handlers"]

#: ``context.user_data`` key holding the internal database user id (task 7.2).
USER_DB_ID_KEY: Final = "db_user_id"

# Single state of the /forecast pick conversation.
_PICK_SUBSCRIPTION: Final = 0

# Routes the subscription-pick tap for the /forecast purpose only.
_FORECAST_PICK_PATTERN: Final = (
    rf"^sub:1:{SubscriptionPickPurpose.FORECAST.value}:"
)

_NEED_START_TEXT: Final = "Please run /start first so I can set up your account."
_FORECAST_EMPTY_TEXT: Final = (
    "You have no subscriptions yet. Use /add to create one, then try /forecast."
)
_FORECAST_PROMPT: Final = "Which subscription would you like a forecast for?"
_SUBSCRIPTION_GONE_TEXT: Final = "That subscription no longer exists."

_END: Final = ConversationHandler.END


def _db_user_id(context: ContextTypes.DEFAULT_TYPE) -> int | None:
    """Return the cached internal database user id, or ``None`` if unset."""
    data = context.user_data
    if data is None:  # pragma: no cover - PTB always provides it for updates
        return None
    raw = data.get(USER_DB_ID_KEY)
    return raw if isinstance(raw, int) else None


def _render_best_forecast(best: BestForecast) -> str:
    """Render a :class:`BestForecast` as ``/forecast`` reply text (Req 13.4)."""
    if best.has_result:
        assert best.spot is not None and best.score is not None
        category_label = (
            best.category.name.title() if best.category is not None else ""
        )
        return format_forecast_result(
            location_label=best.location_label,
            spot_name=best.spot.name,
            score=best.score,
            category_label=category_label,
        )
    return format_forecast_no_spots(best.location_label)


def build_status_handlers(
    status_service: StatusService,
    subscription_service: SubscriptionService,
) -> list[BaseHandler[Update, ContextTypes.DEFAULT_TYPE, object]]:
    """Build the ``/status`` command and ``/forecast`` conversation handlers.

    The services are captured by the closures below (dependency injection) so the
    handlers stay thin and the wiring lives at the composition root (task 11.1).
    This function does not touch the ``Application``.

    :param status_service: Supplies the active-subscription count, last
        scheduler-run time, and the on-demand best forecast.
    :param subscription_service: Lists the user's subscriptions for the
        ``/forecast`` pick keyboard.
    :returns: The handlers to register, in registration order.
    """

    # -- /status (Req 13.3) --------------------------------------------- #
    async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        if message is None:
            return
        user_id = _db_user_id(context)
        if user_id is None:
            await message.reply_text(_NEED_START_TEXT)
            return
        count = await status_service.active_subscription_count(user_id)
        last_run = await status_service.last_scheduler_run()
        await message.reply_text(format_status(count, last_run))

    # -- /forecast (Req 13.4) ------------------------------------------- #
    async def cmd_forecast(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        message = update.effective_message
        if message is None:
            return _END
        user_id = _db_user_id(context)
        if user_id is None:
            await message.reply_text(_NEED_START_TEXT)
            return _END

        summaries = await subscription_service.summarize_for_user(user_id)
        if not summaries:
            await message.reply_text(_FORECAST_EMPTY_TEXT)
            return _END

        # Single subscription → skip pick step, show forecast immediately
        if len(summaries) == 1:
            try:
                best = await status_service.best_forecast_for_subscription(
                    summaries[0].subscription_id
                )
            except NotFoundError:
                await message.reply_text(_SUBSCRIPTION_GONE_TEXT)
                return _END
            await message.reply_text(
                _render_best_forecast(best),
                reply_markup=build_main_menu_keyboard(),
            )
            return _END

        await message.reply_text(
            _FORECAST_PROMPT,
            reply_markup=build_subscription_pick_keyboard(
                summaries, SubscriptionPickPurpose.FORECAST
            ),
        )
        return _PICK_SUBSCRIPTION

    async def on_subscription_picked(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        query = update.callback_query
        if query is None or query.data is None:  # pragma: no cover - pattern guards
            return _PICK_SUBSCRIPTION
        await query.answer()

        pick = parse_subscription_callback(query.data)
        try:
            best = await status_service.best_forecast_for_subscription(
                pick.subscription_id
            )
        except NotFoundError:
            await query.edit_message_text(_SUBSCRIPTION_GONE_TEXT)
            return _END

        await query.edit_message_text(_render_best_forecast(best))
        # Send a follow-up to restore the persistent keyboard
        msg = update.effective_message
        if msg is not None:
            await msg.reply_text("Tap below to navigate:", reply_markup=build_main_menu_keyboard())
        return _END

    async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        return _END

    forecast_handler: ConversationHandler[ContextTypes.DEFAULT_TYPE] = (
        ConversationHandler(
            entry_points=[
                CommandHandler("forecast", cmd_forecast),
            ],
            states={
                _PICK_SUBSCRIPTION: [
                    CallbackQueryHandler(
                        on_subscription_picked, pattern=_FORECAST_PICK_PATTERN
                    ),
                ],
            },
            fallbacks=[CommandHandler("forecast", cmd_forecast)],
            name="forecast_conversation",
            persistent=False,
        )
    )

    return [
        CommandHandler("status", cmd_status),
        forecast_handler,
    ]
