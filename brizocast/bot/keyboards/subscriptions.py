"""Subscription-pick inline keyboard and its callback-data codec (pure).

Builds the keyboard that lets a user pick one of their subscriptions, reused by
``/remove`` (Req 3.6), ``/forecast`` (Req 13.4), and ``/settings`` (Req 13.5).
Because the same keyboard shape serves several flows, the callback data carries
a *purpose* alongside the subscription id so the callback router can dispatch a
tap to the correct follow-up. The codec uses the distinct ``"sub"`` namespace.

All functions are pure: no service calls, no I/O.

Callback-data scheme
--------------------
::

    sub:1:<purpose>:<subscription_id>
    │   │ │         └─ subscription id (int)
    │   │ └─────────── purpose token (one of SubscriptionPickPurpose's values)
    │   └───────────── scheme version
    └───────────────── namespace prefix
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from brizocast.bot.keyboards.callbacks import (
    NAMESPACE_SUBSCRIPTION,
    encode_fields,
    split_fields,
)
from brizocast.services.subscription_service import SubscriptionSummary

__all__ = [
    "SubscriptionPick",
    "SubscriptionPickPurpose",
    "build_subscription_pick_keyboard",
    "encode_subscription_callback",
    "parse_subscription_callback",
]

_SUBSCRIPTION_FIELD_COUNT = 2  # purpose token + subscription id


class SubscriptionPickPurpose(StrEnum):
    """Why a subscription is being picked, so the router can dispatch the tap."""

    REMOVE = "remove"
    FORECAST = "forecast"
    SETTINGS = "settings"


@dataclass(frozen=True, slots=True)
class SubscriptionPick:
    """Parsed identity of a subscription-pick tap."""

    purpose: SubscriptionPickPurpose
    subscription_id: int


def encode_subscription_callback(
    purpose: SubscriptionPickPurpose,
    subscription_id: int,
) -> str:
    """Encode a subscription pick into a ``callback_data`` string.

    :param purpose: The flow the pick belongs to.
    :param subscription_id: The chosen subscription's id.
    :returns: A ``callback_data`` string within Telegram's 64-byte limit.
    """

    return encode_fields(NAMESPACE_SUBSCRIPTION, (purpose.value, str(subscription_id)))


def parse_subscription_callback(raw: str) -> SubscriptionPick:
    """Parse subscription ``callback_data`` back into a :class:`SubscriptionPick`.

    Inverse of :func:`encode_subscription_callback`.

    :param raw: The ``callback_data`` string received from Telegram.
    :returns: The decoded subscription pick.
    :raises ValueError: If ``raw`` is malformed, carries an unknown purpose
        token, or has a non-integer subscription id.
    """

    purpose_token, sub_raw = split_fields(
        raw, NAMESPACE_SUBSCRIPTION, _SUBSCRIPTION_FIELD_COUNT
    )
    try:
        purpose = SubscriptionPickPurpose(purpose_token)
    except ValueError as exc:
        raise ValueError(f"unknown subscription pick purpose {purpose_token!r}: {raw!r}") from exc
    try:
        subscription_id = int(sub_raw)
    except ValueError as exc:
        raise ValueError(f"subscription callback has non-integer id: {raw!r}") from exc
    return SubscriptionPick(purpose=purpose, subscription_id=subscription_id)


def _button_label(summary: SubscriptionSummary) -> str:
    """Render a concise one-line label for a subscription pick button."""

    return f"{summary.activity_display_name} · {summary.location_label}"


def build_subscription_pick_keyboard(
    summaries: Sequence[SubscriptionSummary],
    purpose: SubscriptionPickPurpose,
) -> InlineKeyboardMarkup:
    """Build a subscription-pick keyboard for ``purpose`` (Req 3.6, 13.4, 13.5).

    Renders one button per subscription, one per row, labelled with the activity
    and location and carrying callback data that pairs the subscription id with
    ``purpose`` so the tap routes to the right flow.

    :param summaries: The user's subscription summaries, in display order.
    :param purpose: The flow this keyboard drives.
    :returns: The assembled :class:`telegram.InlineKeyboardMarkup`.
    :raises ValueError: If ``summaries`` is empty.
    """

    if not summaries:
        raise ValueError("build_subscription_pick_keyboard requires at least one subscription")

    rows = [
        [
            InlineKeyboardButton(
                _button_label(summary),
                callback_data=encode_subscription_callback(purpose, summary.subscription_id),
            )
        ]
        for summary in summaries
    ]
    return InlineKeyboardMarkup(rows)
