"""Reusable inline-keyboard builders shared across conversational flows (pure).

Two general-purpose builders the other keyboard modules and handlers reuse:

* :func:`build_single_choice_keyboard` — lay out an arbitrary list of
  ``(label, callback_data)`` choices into an inline keyboard, validating that
  each ``callback_data`` fits Telegram's 64-byte limit. Every "pick one of these
  predefined options" interaction (Req 13.6) can be rendered with this rather
  than hand-building :class:`telegram.InlineKeyboardMarkup` rows.
* :func:`build_confirm_keyboard` / :func:`parse_confirm_callback` — a yes/no
  confirmation keyboard plus its callback-data codec, used to gate destructive
  or final actions (e.g. confirming a ``/remove`` deletion, Req 3.6).

All functions are pure and presentation-only: no service calls, no I/O.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from brizocast.bot.keyboards.callbacks import (
    NAMESPACE_CONFIRM,
    TELEGRAM_CALLBACK_DATA_MAX_BYTES,
    encode_fields,
    split_fields,
)

__all__ = [
    "ConfirmCallbackData",
    "build_confirm_keyboard",
    "build_single_choice_keyboard",
    "encode_confirm_callback",
    "parse_confirm_callback",
]

# Single-character answer tokens keep the confirm payload compact.
_YES_TOKEN = "y"
_NO_TOKEN = "n"

_CONFIRM_FIELD_COUNT = 2  # answer token + free-form action

# Default confirm button glyphs.
_YES_LABEL = "✅ Yes"
_NO_LABEL = "❌ No"


def build_single_choice_keyboard(
    choices: Sequence[tuple[str, str]],
    *,
    columns: int = 1,
) -> InlineKeyboardMarkup:
    """Build an inline keyboard from ``(label, callback_data)`` choices.

    The generic single-choice builder reused across flows (Req 13.6). Choices
    are laid out row by row, ``columns`` buttons per row (the last row may be
    shorter). Callers supply ``callback_data`` already encoded by the relevant
    per-keyboard codec, so this builder stays scheme-agnostic.

    :param choices: Ordered ``(button_label, callback_data)`` pairs.
    :param columns: Maximum number of buttons per row (must be >= 1).
    :returns: The assembled :class:`telegram.InlineKeyboardMarkup`.
    :raises ValueError: If ``columns`` < 1, ``choices`` is empty, or any
        ``callback_data`` exceeds Telegram's 64-byte limit.
    """

    if columns < 1:
        raise ValueError(f"columns must be >= 1, got {columns}")
    if not choices:
        raise ValueError("build_single_choice_keyboard requires at least one choice")

    buttons: list[InlineKeyboardButton] = []
    for label, callback_data in choices:
        encoded_bytes = len(callback_data.encode("utf-8"))
        if encoded_bytes > TELEGRAM_CALLBACK_DATA_MAX_BYTES:
            raise ValueError(
                "choice callback data exceeds Telegram's "
                f"{TELEGRAM_CALLBACK_DATA_MAX_BYTES}-byte limit "
                f"({encoded_bytes} bytes): {callback_data!r}"
            )
        buttons.append(InlineKeyboardButton(label, callback_data=callback_data))

    rows = [buttons[i : i + columns] for i in range(0, len(buttons), columns)]
    return InlineKeyboardMarkup(rows)


@dataclass(frozen=True, slots=True)
class ConfirmCallbackData:
    """Parsed identity carried by a yes/no confirm button.

    ``answer`` is the user's decision; ``action`` is an opaque, caller-defined
    token identifying *what* is being confirmed (e.g. ``"remove:42"``) so the
    handler can route the confirmation to the right follow-up.
    """

    answer: bool
    action: str


def encode_confirm_callback(data: ConfirmCallbackData) -> str:
    """Encode a confirm decision into a ``callback_data`` string.

    Scheme: ``cf:1:<y|n>:<action>``. The ``action`` token is placed last and may
    contain the separator.

    :param data: The confirm identity to encode.
    :returns: A ``callback_data`` string within Telegram's 64-byte limit.
    :raises ValueError: If ``action`` is empty or the payload would be too long.
    """

    if not data.action:
        raise ValueError("confirm callback requires a non-empty action token")
    token = _YES_TOKEN if data.answer else _NO_TOKEN
    return encode_fields(NAMESPACE_CONFIRM, (token, data.action))


def parse_confirm_callback(raw: str) -> ConfirmCallbackData:
    """Parse confirm ``callback_data`` back into a :class:`ConfirmCallbackData`.

    Inverse of :func:`encode_confirm_callback`.

    :param raw: The ``callback_data`` string received from Telegram.
    :returns: The decoded confirm identity.
    :raises ValueError: If ``raw`` is not a well-formed current-version confirm
        payload (wrong prefix/version, missing fields, unknown answer token, or
        empty action).
    """

    token, action = split_fields(raw, NAMESPACE_CONFIRM, _CONFIRM_FIELD_COUNT)
    if token == _YES_TOKEN:
        answer = True
    elif token == _NO_TOKEN:
        answer = False
    else:
        raise ValueError(f"unknown confirm answer token {token!r}: {raw!r}")
    if not action:
        raise ValueError(f"confirm callback has empty action: {raw!r}")
    return ConfirmCallbackData(answer=answer, action=action)


def build_confirm_keyboard(
    action: str,
    *,
    yes_label: str = _YES_LABEL,
    no_label: str = _NO_LABEL,
) -> InlineKeyboardMarkup:
    """Build a yes/no confirmation keyboard for ``action`` (Req 13.6).

    Both buttons carry the same ``action`` token and differ only by the answer
    they record, so the handler learns both the decision and what it applies to.

    :param action: An opaque token identifying what is being confirmed.
    :param yes_label: Label for the affirmative button.
    :param no_label: Label for the negative button.
    :returns: A single-row :class:`telegram.InlineKeyboardMarkup`.
    :raises ValueError: If ``action`` is empty (see :func:`encode_confirm_callback`).
    """

    yes = InlineKeyboardButton(
        yes_label,
        callback_data=encode_confirm_callback(ConfirmCallbackData(answer=True, action=action)),
    )
    no = InlineKeyboardButton(
        no_label,
        callback_data=encode_confirm_callback(ConfirmCallbackData(answer=False, action=action)),
    )
    return InlineKeyboardMarkup([[yes, no]])
