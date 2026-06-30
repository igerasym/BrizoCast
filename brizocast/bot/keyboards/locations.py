"""Location-options inline keyboard and its callback-data codec (pure).

Builds the ``/location`` entry keyboard offering the four ways a user can set a
location (Req 2.1): share a Telegram location, search by city, search by place
name, or view saved favorites. The codec lives under the distinct ``"loc"``
namespace.

All functions are pure: no service calls, no I/O.

Callback-data scheme
--------------------
::

    loc:1:<option>
    │   │ └─ option token (one of LocationOption's values)
    │   └─── scheme version
    └─────── namespace prefix
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from brizocast.bot.keyboards.callbacks import (
    NAMESPACE_LOCATION,
    NAMESPACE_LOCATION_CANDIDATE,
    NAMESPACE_LOCATION_FAVORITE,
    encode_fields,
    split_fields,
)
from brizocast.core.domain.geo import GeoCandidate

__all__ = [
    "LocationOption",
    "build_candidate_keyboard",
    "build_favorites_delete_keyboard",
    "build_location_options_keyboard",
    "encode_candidate_callback",
    "encode_favorite_delete_callback",
    "encode_location_callback",
    "parse_candidate_callback",
    "parse_favorite_delete_callback",
    "parse_location_callback",
]

_LOCATION_FIELD_COUNT = 1  # option token
_CANDIDATE_FIELD_COUNT = 1  # candidate index
_FAVORITE_FIELD_COUNT = 1  # favorite location id


class LocationOption(StrEnum):
    """The ways a user can provide a location from the ``/location`` menu (Req 2.1)."""

    SHARE = "share"
    SEARCH_CITY = "city"
    SEARCH_PLACE = "place"
    FAVORITES = "favorites"


# User-facing button labels per option, in display order.
_OPTION_LABELS: dict[LocationOption, str] = {
    LocationOption.SHARE: "📍 Share my location",
    LocationOption.SEARCH_CITY: "🏙️ Search by city",
    LocationOption.SEARCH_PLACE: "🔎 Search by place name",
    LocationOption.FAVORITES: "⭐ Saved favorites",
}


def encode_location_callback(option: LocationOption) -> str:
    """Encode a location option into a ``callback_data`` string.

    :param option: The chosen :class:`LocationOption`.
    :returns: A ``callback_data`` string within Telegram's 64-byte limit.
    """

    return encode_fields(NAMESPACE_LOCATION, (option.value,))


def parse_location_callback(raw: str) -> LocationOption:
    """Parse location ``callback_data`` back into a :class:`LocationOption`.

    Inverse of :func:`encode_location_callback`.

    :param raw: The ``callback_data`` string received from Telegram.
    :returns: The decoded :class:`LocationOption`.
    :raises ValueError: If ``raw`` is not a well-formed current-version location
        payload or carries an unknown option token.
    """

    (token,) = split_fields(raw, NAMESPACE_LOCATION, _LOCATION_FIELD_COUNT)
    try:
        return LocationOption(token)
    except ValueError as exc:
        raise ValueError(f"unknown location option {token!r}: {raw!r}") from exc


def build_location_options_keyboard() -> InlineKeyboardMarkup:
    """Build the ``/location`` options keyboard (Req 2.1, 13.6).

    Renders one button per :class:`LocationOption`, one per row, each carrying
    its encoded callback data.

    :returns: The assembled :class:`telegram.InlineKeyboardMarkup`.
    """

    rows = [
        [InlineKeyboardButton(_OPTION_LABELS[option], callback_data=encode_location_callback(option))]
        for option in LocationOption
    ]
    return InlineKeyboardMarkup(rows)


# --------------------------------------------------------------------------- #
# Geocoding candidate-pick scheme (namespace ``lcd``)
# --------------------------------------------------------------------------- #
def encode_candidate_callback(index: int) -> str:
    """Encode a geocoding-candidate choice into ``callback_data``.

    Only the candidate's *position* in the presented list is encoded; the full
    :class:`~brizocast.core.domain.geo.GeoCandidate` (lat/lon/city/country) is
    held in the conversation's ``user_data`` and looked up by this index when
    the user taps. This keeps the payload tiny and well within Telegram's
    64-byte ``callback_data`` limit regardless of place-name length.

    :param index: Zero-based position of the candidate in the offered list.
    :returns: A ``callback_data`` string under the ``lcd`` namespace.
    :raises ValueError: If ``index`` is negative.
    """

    if index < 0:
        raise ValueError(f"candidate index must be >= 0, got {index}")
    return encode_fields(NAMESPACE_LOCATION_CANDIDATE, (str(index),))


def parse_candidate_callback(raw: str) -> int:
    """Parse candidate ``callback_data`` back into its list index.

    Inverse of :func:`encode_candidate_callback`.

    :param raw: The ``callback_data`` string received from Telegram.
    :returns: The zero-based candidate index.
    :raises ValueError: If ``raw`` is malformed or carries a non-integer index.
    """

    (token,) = split_fields(raw, NAMESPACE_LOCATION_CANDIDATE, _CANDIDATE_FIELD_COUNT)
    try:
        index = int(token)
    except ValueError as exc:
        raise ValueError(f"candidate callback has non-integer index: {raw!r}") from exc
    if index < 0:
        raise ValueError(f"candidate callback has negative index: {raw!r}")
    return index


def _candidate_label(candidate: GeoCandidate) -> str:
    """Render a concise one-line label for a candidate-pick button (Req 2.4)."""

    parts = [candidate.name]
    context = ", ".join(p for p in (candidate.city, candidate.country) if p)
    if context and context != candidate.name:
        parts.append(f"({context})")
    return " ".join(parts)


def build_candidate_keyboard(candidates: Sequence[GeoCandidate]) -> InlineKeyboardMarkup:
    """Build the geocoding-candidate selection keyboard (Req 2.4).

    Renders one button per candidate, in order, each carrying its list index as
    callback data so the handler can resolve the tap to the stored candidate and
    create a location from it (Req 2.5).

    :param candidates: The provider's ranked candidates, in display order.
    :returns: The assembled :class:`telegram.InlineKeyboardMarkup`.
    :raises ValueError: If ``candidates`` is empty.
    """

    if not candidates:
        raise ValueError("build_candidate_keyboard requires at least one candidate")
    rows = [
        [
            InlineKeyboardButton(
                _candidate_label(candidate),
                callback_data=encode_candidate_callback(index),
            )
        ]
        for index, candidate in enumerate(candidates)
    ]
    return InlineKeyboardMarkup(rows)


# --------------------------------------------------------------------------- #
# Favorite-delete scheme (namespace ``lfv``)
# --------------------------------------------------------------------------- #
def encode_favorite_delete_callback(location_id: int) -> str:
    """Encode a favorite-delete choice into ``callback_data`` (Req 2.10).

    :param location_id: Id of the favorite location to delete.
    :returns: A ``callback_data`` string under the ``lfv`` namespace.
    """

    return encode_fields(NAMESPACE_LOCATION_FAVORITE, (str(location_id),))


def parse_favorite_delete_callback(raw: str) -> int:
    """Parse favorite-delete ``callback_data`` back into a location id.

    Inverse of :func:`encode_favorite_delete_callback`.

    :param raw: The ``callback_data`` string received from Telegram.
    :returns: The favorite location's id.
    :raises ValueError: If ``raw`` is malformed or carries a non-integer id.
    """

    (token,) = split_fields(raw, NAMESPACE_LOCATION_FAVORITE, _FAVORITE_FIELD_COUNT)
    try:
        return int(token)
    except ValueError as exc:
        raise ValueError(f"favorite callback has non-integer id: {raw!r}") from exc


def build_favorites_delete_keyboard(
    favorites: Sequence[tuple[int, str]],
) -> InlineKeyboardMarkup:
    """Build a keyboard to delete one of the user's saved favorites (Req 2.10).

    :param favorites: ``(location_id, label)`` pairs, in display order. The
        label is presentation-only; the id is what the callback carries.
    :returns: The assembled :class:`telegram.InlineKeyboardMarkup`.
    :raises ValueError: If ``favorites`` is empty.
    """

    if not favorites:
        raise ValueError("build_favorites_delete_keyboard requires at least one favorite")
    rows = [
        [
            InlineKeyboardButton(
                f"🗑️ {label}",
                callback_data=encode_favorite_delete_callback(location_id),
            )
        ]
        for location_id, label in favorites
    ]
    return InlineKeyboardMarkup(rows)
