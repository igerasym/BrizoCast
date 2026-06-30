"""Inline 👍/👎 feedback keyboard builder and callback-data codec (pure).

This module owns the **stable callback-data scheme** that ties an alert's inline
feedback buttons back to the alert they belong to. The notification formatter
attaches the keyboard built here to every dispatched alert (Req 12.3); the
feedback callback handler (task 7.9) parses the callback data with
:func:`parse_feedback_callback` and hands the result to ``FeedbackService`` so a
``Feedback`` row (subscription, spot, score, rating) can be persisted
(Req 12.4, 12.5).

Everything here is pure and framework-light: it depends only on
``python-telegram-bot``'s keyboard value objects and the
:class:`~brizocast.models.feedback.FeedbackRating` enum (a plain ``StrEnum``).
No service calls, no I/O.

Callback-data scheme
--------------------
Telegram limits ``callback_data`` to **64 bytes**. The scheme encodes the four
pieces of identity a feedback action needs in a single colon-delimited string::

    fb:1:<rating>:<subscription_id>:<surf_score>:<spot_key>
    │  │ │        │                 │            └─ spot key (may contain ':')
    │  │ │        │                 └────────────── surf score, 0..100
    │  │ │        └──────────────────────────────── subscription id (int)
    │  │ └───────────────────────────────────────── rating token: 'u' up / 'd' down
    │  └─────────────────────────────────────────── scheme version ('1')
    └────────────────────────────────────────────── namespace prefix ('fb')

Field rules:

* ``prefix`` is always :data:`CALLBACK_PREFIX` (``"fb"``) — it namespaces
  feedback callbacks so the handler can cheaply tell them apart from other
  inline callbacks via :func:`is_feedback_callback`.
* ``version`` is :data:`CALLBACK_VERSION` (``"1"``) so the wire format can evolve
  without misparsing data attached to older, still-pending messages.
* ``rating`` is a single-character token (``"u"``/``"d"``) to conserve bytes; it
  maps to :class:`FeedbackRating`.
* ``spot_key`` is placed **last** and is the only free-form field. A surf
  ``spot_key`` is an opaque slug that could itself contain a ``":"``; putting it
  last lets the parser split on the first five separators only and keep any
  remaining colons as part of the key, so the round-trip is lossless.

:func:`encode_feedback_callback` rejects payloads that would exceed
:data:`TELEGRAM_CALLBACK_DATA_MAX_BYTES`, surfacing the Telegram limit at build
time rather than letting Telegram reject the send.
"""

from __future__ import annotations

from dataclasses import dataclass

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from brizocast.models.feedback import FeedbackRating

__all__ = [
    "CALLBACK_PREFIX",
    "CALLBACK_VERSION",
    "TELEGRAM_CALLBACK_DATA_MAX_BYTES",
    "FeedbackCallbackData",
    "build_feedback_keyboard",
    "encode_feedback_callback",
    "is_feedback_callback",
    "parse_feedback_callback",
]

# Namespace prefix and version for the feedback callback-data wire format.
CALLBACK_PREFIX = "fb"
CALLBACK_VERSION = "1"

# Telegram's hard limit on the size of a callback_data payload, in bytes.
TELEGRAM_CALLBACK_DATA_MAX_BYTES = 64

# Field separator and the number of leading separators before the free-form
# spot key (prefix, version, rating, subscription_id, surf_score -> 5 splits).
_SEP = ":"
_FIELD_SPLITS = 5

# Compact, single-character rating tokens keep the payload small.
_RATING_TO_TOKEN: dict[FeedbackRating, str] = {
    FeedbackRating.UP: "u",
    FeedbackRating.DOWN: "d",
}
_TOKEN_TO_RATING: dict[str, FeedbackRating] = {
    token: rating for rating, token in _RATING_TO_TOKEN.items()
}

# Button glyphs for the thumbs-up / thumbs-down controls (Req 12.3).
_THUMBS_UP_LABEL = "👍"
_THUMBS_DOWN_LABEL = "👎"


@dataclass(frozen=True, slots=True)
class FeedbackCallbackData:
    """Parsed identity carried by a feedback button's callback data.

    Binds a feedback action to the alert it was shown on: the originating
    ``subscription_id`` and ``spot_key``, the ``surf_score`` that was alerted on,
    and the user's :class:`FeedbackRating`. The callback handler (task 7.9) uses
    these four fields verbatim to persist a ``Feedback`` row (Req 12.4).
    """

    subscription_id: int
    spot_key: str
    surf_score: int
    rating: FeedbackRating


def encode_feedback_callback(data: FeedbackCallbackData) -> str:
    """Encode feedback identity into a Telegram ``callback_data`` string.

    Produces the colon-delimited scheme documented in the module docstring. The
    ``spot_key`` is emitted last so it may safely contain the separator.

    :param data: The feedback identity to encode.
    :returns: A ``callback_data`` string of at most
        :data:`TELEGRAM_CALLBACK_DATA_MAX_BYTES` bytes.
    :raises ValueError: If the rating is unknown, or the encoded payload would
        exceed Telegram's 64-byte ``callback_data`` limit.
    """

    token = _RATING_TO_TOKEN.get(data.rating)
    if token is None:  # pragma: no cover - defensive; enum is exhaustive
        raise ValueError(f"unsupported feedback rating: {data.rating!r}")

    payload = _SEP.join(
        (
            CALLBACK_PREFIX,
            CALLBACK_VERSION,
            token,
            str(data.subscription_id),
            str(data.surf_score),
            data.spot_key,
        )
    )

    encoded_bytes = len(payload.encode("utf-8"))
    if encoded_bytes > TELEGRAM_CALLBACK_DATA_MAX_BYTES:
        raise ValueError(
            "encoded feedback callback exceeds Telegram's "
            f"{TELEGRAM_CALLBACK_DATA_MAX_BYTES}-byte limit "
            f"({encoded_bytes} bytes); spot_key is likely too long: {data.spot_key!r}"
        )
    return payload


def is_feedback_callback(raw: str) -> bool:
    """Return whether ``raw`` callback data belongs to the feedback scheme.

    A cheap prefix/version check the handler can use to route callbacks without
    fully parsing them. Returns ``True`` only for the current scheme version.
    """

    parts = raw.split(_SEP, 2)
    return (
        len(parts) >= 2
        and parts[0] == CALLBACK_PREFIX
        and parts[1] == CALLBACK_VERSION
    )


def parse_feedback_callback(raw: str) -> FeedbackCallbackData:
    """Parse feedback ``callback_data`` back into a :class:`FeedbackCallbackData`.

    Inverse of :func:`encode_feedback_callback`. Splits on the first five
    separators only, so the free-form ``spot_key`` keeps any embedded colons.

    :param raw: The ``callback_data`` string received from Telegram.
    :returns: The decoded feedback identity.
    :raises ValueError: If ``raw`` is not a well-formed current-version feedback
        payload (wrong prefix/version, missing fields, unknown rating token, or
        non-integer subscription id / score).
    """

    parts = raw.split(_SEP, _FIELD_SPLITS)
    if len(parts) != _FIELD_SPLITS + 1:
        raise ValueError(f"malformed feedback callback data: {raw!r}")

    prefix, version, token, sub_raw, score_raw, spot_key = parts
    if prefix != CALLBACK_PREFIX:
        raise ValueError(f"not a feedback callback (prefix {prefix!r}): {raw!r}")
    if version != CALLBACK_VERSION:
        raise ValueError(f"unsupported feedback callback version {version!r}: {raw!r}")

    rating = _TOKEN_TO_RATING.get(token)
    if rating is None:
        raise ValueError(f"unknown feedback rating token {token!r}: {raw!r}")
    if not spot_key:
        raise ValueError(f"feedback callback has empty spot_key: {raw!r}")

    try:
        subscription_id = int(sub_raw)
        surf_score = int(score_raw)
    except ValueError as exc:
        raise ValueError(f"feedback callback has non-integer field: {raw!r}") from exc

    return FeedbackCallbackData(
        subscription_id=subscription_id,
        spot_key=spot_key,
        surf_score=surf_score,
        rating=rating,
    )


def build_feedback_keyboard(
    subscription_id: int,
    spot_key: str,
    surf_score: int,
) -> InlineKeyboardMarkup:
    """Build the inline 👍/👎 feedback keyboard for an alert (Req 12.3).

    Both buttons carry callback data encoding the same alert identity
    (``subscription_id``, ``spot_key``, ``surf_score``) and differ only by the
    :class:`FeedbackRating` they record, so the callback handler can persist the
    user's rating against the exact alert it was shown on.

    :param subscription_id: The subscription the alert was sent for.
    :param spot_key: The surf spot the alert scored.
    :param surf_score: The surf score the alert reported (0..100).
    :returns: A single-row :class:`telegram.InlineKeyboardMarkup` with a
        thumbs-up and a thumbs-down button.
    :raises ValueError: If either button's callback data would exceed Telegram's
        64-byte limit (see :func:`encode_feedback_callback`).
    """

    up = InlineKeyboardButton(
        _THUMBS_UP_LABEL,
        callback_data=encode_feedback_callback(
            FeedbackCallbackData(
                subscription_id=subscription_id,
                spot_key=spot_key,
                surf_score=surf_score,
                rating=FeedbackRating.UP,
            )
        ),
    )
    down = InlineKeyboardButton(
        _THUMBS_DOWN_LABEL,
        callback_data=encode_feedback_callback(
            FeedbackCallbackData(
                subscription_id=subscription_id,
                spot_key=spot_key,
                surf_score=surf_score,
                rating=FeedbackRating.DOWN,
            )
        ),
    )
    return InlineKeyboardMarkup([[up, down]])
