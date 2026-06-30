"""``/help``, the unknown-command fallback, and the misc-handler aggregator.

Two thin Telegram adapters plus a small composition helper:

* :func:`build_help_handlers` — the ``/help`` command. It renders the full
  command list with a description of each (Req 13.2) via the pure
  :func:`~brizocast.bot.formatters.commands.format_help`, which is built from
  :data:`~brizocast.bot.formatters.commands.COMMANDS`, the single source of
  truth for the supported command set (Req 13.1).
* :func:`build_unknown_command_handler` — a catch-all
  :class:`~telegram.ext.MessageHandler` on ``filters.COMMAND`` that tells the
  user the command was not recognised and suggests ``/help`` (Req 13.7). Because
  it matches *any* command, it **must be registered last**, after every
  per-feature command handler, or it would shadow them.
* :func:`build_misc_handlers` — convenience aggregator the composition root
  (task 11.1) can call as the single place that contributes ``/help``, the
  feedback callbacks (Req 12.3), and the unknown-command fallback. The returned
  list is ordered so the catch-all fallback is **last**; the whole list must be
  registered after the per-feature handlers.

Thin handler + dependency injection
-----------------------------------
Handlers only parse the inbound update and render a reply (``/help`` and the
fallback are stateless and need no services). The aggregator forwards the
injected :class:`~brizocast.services.feedback_service.FeedbackService` to
:func:`~brizocast.bot.handlers.feedback.build_feedback_handlers`. No module here
constructs services or touches the ``Application``.

Requirements covered: 12.3, 13.1, 13.2, 13.7.
"""

from __future__ import annotations

from telegram import Update
from telegram.ext import (
    BaseHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from brizocast.bot.formatters.commands import format_help, unknown_command_text
from brizocast.bot.handlers.feedback import build_feedback_handlers
from brizocast.bot.keyboards.menu import (
    MENU_PROMPT,
    build_main_menu_keyboard,
)
from brizocast.core.logging import BoundLogger
from brizocast.services.feedback_service import FeedbackService

__all__ = [
    "build_help_handlers",
    "build_misc_handlers",
    "build_unknown_command_handler",
]

# A bot handler bound to the default context type, with an opaque result type.
_Handler = BaseHandler[Update, ContextTypes.DEFAULT_TYPE, object]


async def _cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reply with the full command list and descriptions (Req 13.1, 13.2)."""

    message = update.effective_message
    if message is None:
        return
    await message.reply_text(format_help())


async def _on_unknown_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Reply that the command is unrecognised and suggest ``/help`` (Req 13.7)."""

    message = update.effective_message
    if message is None:
        return
    await message.reply_text(unknown_command_text())


async def _cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the persistent main-menu navigation keyboard (Req 13.1)."""

    message = update.effective_message
    if message is None:
        return
    await message.reply_text(MENU_PROMPT, reply_markup=build_main_menu_keyboard())


def build_help_handlers(*, logger: BoundLogger | None = None) -> list[_Handler]:
    """Build the ``/help`` command handler (Req 13.1, 13.2).

    :param logger: Optional bound logger (accepted for builder-signature
        symmetry; ``/help`` is stateless and does no I/O).
    :returns: A single-element list holding the ``/help`` command handler.
    """

    return [CommandHandler("help", _cmd_help)]


def build_unknown_command_handler(*, logger: BoundLogger | None = None) -> _Handler:
    """Build the catch-all unknown-command fallback handler (Req 13.7).

    The returned :class:`~telegram.ext.MessageHandler` matches **any** message
    that is a command (``filters.COMMAND``), so it must be registered **last** —
    after every real command handler — or it would intercept them.

    :param logger: Optional bound logger (accepted for builder-signature
        symmetry; the fallback is stateless and does no I/O).
    :returns: The unknown-command :class:`~telegram.ext.MessageHandler`.
    """

    return MessageHandler(filters.COMMAND, _on_unknown_command)


def build_misc_handlers(
    feedback_service: FeedbackService,
    *,
    logger: BoundLogger | None = None,
) -> list[_Handler]:
    """Build ``/help``, feedback callbacks, and the unknown-command fallback.

    A single entry point the composition root can use for the cross-cutting
    handlers that do not belong to one feature: ``/help`` (Req 13.1, 13.2), the
    👍/👎 feedback callbacks (Req 12.3), and the unknown-command fallback
    (Req 13.7).

    The list is ordered so the catch-all fallback is **last**. The whole list
    must itself be registered **after** all per-feature handlers, otherwise the
    fallback would shadow the real commands.

    :param feedback_service: Service injected into the feedback callback handler.
    :param logger: Optional bound logger forwarded to the sub-builders.
    :returns: The misc handlers, fallback last.
    """

    return [
        *build_help_handlers(logger=logger),
        CommandHandler("menu", _cmd_menu),
        *build_feedback_handlers(feedback_service, logger=logger),
        build_unknown_command_handler(logger=logger),
    ]
