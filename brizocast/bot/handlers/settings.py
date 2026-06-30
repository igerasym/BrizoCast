"""``/settings`` handler — edit a subscription's notification preferences.

Implements the ``/settings`` conversation (Req 13.5): the user picks one of
their subscriptions, then edits its notification preferences — notification mode
(Req 10.2), quiet hours (Req 11.1), and mute/snooze state (Req 11.3, 11.4). Each
change is persisted through
:class:`~brizocast.services.subscription_service.SubscriptionService` and
confirmed back to the user.

Thin-handler + DI pattern
-------------------------
This module is a thin Telegram adapter: handlers parse the inbound update, call
a service, and format a reply. No business rules live here. The owning
:class:`SubscriptionService` is supplied to :func:`build_settings_handlers` and
captured by closure, so the handlers never construct services or touch the
``Application``. Conversation state (the picked subscription id) lives in
``context.user_data``; the per-turn flow position is tracked by the
:class:`telegram.ext.ConversationHandler`.

User resolution
---------------
The service keys subscriptions by the internal user id, while Telegram updates
carry the Telegram user id. The onboarding flow (task 7.2) resolves/creates the
user on first interaction and stores the internal id under
:data:`USER_DB_ID_KEY` in ``context.user_data``; this handler reads it from
there and asks the user to run ``/start`` first if it is absent.

Requirements covered: 10.2, 11.1, 11.3, 11.4, 13.5.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from typing import Final

from telegram import Update
from telegram.ext import (
    BaseHandler,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from brizocast.bot.keyboards.notifications import (
    build_notification_mode_keyboard,
    notification_mode_label,
    parse_notification_mode_callback,
)
from brizocast.bot.keyboards.settings import (
    SettingsAction,
    build_settings_menu_keyboard,
    build_snooze_keyboard,
    parse_settings_callback,
)
from brizocast.bot.keyboards.subscriptions import (
    SubscriptionPickPurpose,
    build_subscription_pick_keyboard,
    parse_subscription_callback,
)
from brizocast.core.errors import DomainValidationError, NotFoundError
from brizocast.services.subscription_service import SubscriptionService

__all__ = ["USER_DB_ID_KEY", "build_settings_handlers"]

# ``context.user_data`` key holding the internal (DB) user id, populated by the
# onboarding flow (task 7.2) when the user is created/resolved.
USER_DB_ID_KEY: Final = "db_user_id"

# ``context.user_data`` key holding the subscription currently being edited.
_SUBSCRIPTION_ID_KEY: Final = "settings_subscription_id"

# Conversation states.
_PICK_SUBSCRIPTION: Final = 0
_EDIT_MENU: Final = 1
_AWAIT_QUIET_HOURS: Final = 2

# Callback-data routing patterns (anchored to each keyboard family's namespace).
_PICK_PATTERN: Final = rf"^sub:1:{SubscriptionPickPurpose.SETTINGS.value}:"
_SETTINGS_PATTERN: Final = r"^set:1:"
_MODE_PATTERN: Final = r"^nm:1:"

_SESSION_EXPIRED = "That settings session expired. Send /settings again."
_SUBSCRIPTION_GONE = "That subscription no longer exists."
_FOLLOW_UP = "\n\nAnything else?"


def _resolve_user_db_id(context: ContextTypes.DEFAULT_TYPE) -> int | None:
    """Return the internal user id stashed by onboarding, or ``None``."""

    if context.user_data is None:
        return None
    raw = context.user_data.get(USER_DB_ID_KEY)
    return raw if isinstance(raw, int) else None


def _stored_subscription_id(context: ContextTypes.DEFAULT_TYPE) -> int | None:
    """Return the subscription id under edit, or ``None`` if unset."""

    if context.user_data is None:
        return None
    raw = context.user_data.get(_SUBSCRIPTION_ID_KEY)
    return raw if isinstance(raw, int) else None


def _parse_quiet_hours(text: str) -> tuple[time, time]:
    """Parse a ``HH:MM-HH:MM`` (or space-separated) quiet-hours string.

    :raises DomainValidationError: If the text is not two ``HH:MM`` times.
    """

    tokens = text.replace("-", " ").split()
    if len(tokens) != 2:
        raise DomainValidationError(
            "send quiet hours as two 24h times, e.g. '22:00-07:00'"
        )
    try:
        start = datetime.strptime(tokens[0], "%H:%M").time()
        end = datetime.strptime(tokens[1], "%H:%M").time()
    except ValueError as exc:
        raise DomainValidationError(
            "could not read the times; use 24h 'HH:MM', e.g. '22:00-07:00'"
        ) from exc
    return start, end


def build_settings_handlers(
    subscription_service: SubscriptionService,
) -> list[BaseHandler[Update, ContextTypes.DEFAULT_TYPE, object]]:
    """Build the ``/settings`` conversation handlers (Req 13.5).

    The injected :class:`SubscriptionService` is captured by closure, so the
    returned handlers persist preference changes without constructing services
    or touching the ``Application``. The conversation lets a user pick a
    subscription and edit its notification mode (Req 10.2), quiet hours
    (Req 11.1), and mute/snooze state (Req 11.3, 11.4).

    :param subscription_service: The service used to persist preference changes.
    :returns: A single-element list holding the ``/settings`` conversation
        handler.
    """

    async def cmd_settings(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        """Entry point: present the subscription pick for ``/settings``."""

        message = update.effective_message
        if message is None:
            return ConversationHandler.END

        user_db_id = _resolve_user_db_id(context)
        if user_db_id is None:
            await message.reply_text("Please run /start first to set up your account.")
            return ConversationHandler.END

        summaries = await subscription_service.summarize_for_user(user_db_id)
        if not summaries:
            await message.reply_text(
                "You have no subscriptions yet. Use /add to create one."
            )
            return ConversationHandler.END

        await message.reply_text(
            "Which subscription's settings do you want to edit?",
            reply_markup=build_subscription_pick_keyboard(
                summaries, SubscriptionPickPurpose.SETTINGS
            ),
        )
        return _PICK_SUBSCRIPTION

    async def on_subscription_picked(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        """Store the picked subscription and show the preferences menu."""

        query = update.callback_query
        if query is None or query.data is None:
            return _PICK_SUBSCRIPTION
        await query.answer()

        pick = parse_subscription_callback(query.data)
        if context.user_data is not None:
            context.user_data[_SUBSCRIPTION_ID_KEY] = pick.subscription_id

        await query.edit_message_text(
            "What would you like to change?",
            reply_markup=build_settings_menu_keyboard(),
        )
        return _EDIT_MENU

    async def on_settings_action(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        """Dispatch a settings-menu tap to the right preference edit."""

        query = update.callback_query
        if query is None or query.data is None:
            return _EDIT_MENU
        await query.answer()

        subscription_id = _stored_subscription_id(context)
        if subscription_id is None:
            await query.edit_message_text(_SESSION_EXPIRED)
            return ConversationHandler.END

        callback = parse_settings_callback(query.data)
        action = callback.action

        if action is SettingsAction.MODE:
            await query.edit_message_text(
                "Choose a notification mode:",
                reply_markup=build_notification_mode_keyboard(),
            )
            return _EDIT_MENU

        if action is SettingsAction.QUIET:
            await query.edit_message_text(
                "Send your quiet hours as two 24h times, e.g. '22:00-07:00'."
            )
            return _AWAIT_QUIET_HOURS

        if action is SettingsAction.SNOOZE_MENU:
            await query.edit_message_text(
                "Snooze this subscription for how long?",
                reply_markup=build_snooze_keyboard(),
            )
            return _EDIT_MENU

        try:
            if action is SettingsAction.QUIET_CLEAR:
                await subscription_service.set_quiet_hours(subscription_id, None, None)
                confirmation = "Quiet hours cleared."
            elif action is SettingsAction.MUTE:
                muted = callback.arg == "1"
                await subscription_service.set_muted(subscription_id, muted)
                confirmation = (
                    "Subscription muted." if muted else "Subscription unmuted."
                )
            else:  # SettingsAction.SNOOZE
                minutes = int(callback.arg)
                if minutes <= 0:
                    await subscription_service.snooze(subscription_id, None)
                    confirmation = "Snooze cleared."
                else:
                    until = datetime.now(UTC) + timedelta(minutes=minutes)
                    await subscription_service.snooze(subscription_id, until)
                    confirmation = f"Snoozed for {minutes // 60}h."
        except NotFoundError:
            await query.edit_message_text(_SUBSCRIPTION_GONE)
            return ConversationHandler.END

        await query.edit_message_text(
            f"{confirmation}{_FOLLOW_UP}",
            reply_markup=build_settings_menu_keyboard(),
        )
        return _EDIT_MENU

    async def on_mode_chosen(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        """Persist the chosen notification mode (Req 10.2)."""

        query = update.callback_query
        if query is None or query.data is None:
            return _EDIT_MENU
        await query.answer()

        subscription_id = _stored_subscription_id(context)
        if subscription_id is None:
            await query.edit_message_text(_SESSION_EXPIRED)
            return ConversationHandler.END

        mode = parse_notification_mode_callback(query.data)
        try:
            await subscription_service.set_notification_mode(subscription_id, mode.value)
        except NotFoundError:
            await query.edit_message_text(_SUBSCRIPTION_GONE)
            return ConversationHandler.END

        await query.edit_message_text(
            f"Notification mode set to {notification_mode_label(mode)}.{_FOLLOW_UP}",
            reply_markup=build_settings_menu_keyboard(),
        )
        return _EDIT_MENU

    async def on_quiet_hours_input(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        """Parse and persist quiet hours from the user's text reply (Req 11.1)."""

        message = update.effective_message
        if message is None or message.text is None:
            return _AWAIT_QUIET_HOURS

        subscription_id = _stored_subscription_id(context)
        if subscription_id is None:
            await message.reply_text(_SESSION_EXPIRED)
            return ConversationHandler.END

        try:
            start, end = _parse_quiet_hours(message.text)
        except DomainValidationError as exc:
            await message.reply_text(str(exc))
            return _AWAIT_QUIET_HOURS

        try:
            await subscription_service.set_quiet_hours(subscription_id, start, end)
        except NotFoundError:
            await message.reply_text(_SUBSCRIPTION_GONE)
            return ConversationHandler.END

        await message.reply_text(
            f"Quiet hours set to {start:%H:%M}-{end:%H:%M}.{_FOLLOW_UP}",
            reply_markup=build_settings_menu_keyboard(),
        )
        return _EDIT_MENU

    conversation: ConversationHandler[ContextTypes.DEFAULT_TYPE] = ConversationHandler(
        entry_points=[
            CommandHandler("settings", cmd_settings),
        ],
        states={
            _PICK_SUBSCRIPTION: [
                CallbackQueryHandler(on_subscription_picked, pattern=_PICK_PATTERN),
            ],
            _EDIT_MENU: [
                CallbackQueryHandler(on_settings_action, pattern=_SETTINGS_PATTERN),
                CallbackQueryHandler(on_mode_chosen, pattern=_MODE_PATTERN),
            ],
            _AWAIT_QUIET_HOURS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, on_quiet_hours_input),
            ],
        },
        fallbacks=[CommandHandler("settings", cmd_settings)],
        name="settings_conversation",
    )
    return [conversation]
