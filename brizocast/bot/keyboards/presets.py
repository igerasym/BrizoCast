"""Preset-pick inline keyboard and its callback-data codec (pure).

Builds the keyboard that lets a user pick one of the presets surfaced by
``PresetService.list_presets`` (Req 4.3). A :class:`PresetOption` may be either a
persisted preset (with a database ``preset_id``) or a bundled static default
(``preset_id is None``), so the callback data carries the option's **position in
the rendered list** as a stable reference, plus the ``preset_id`` when present
for convenience. The handler (task 7.5) re-lists and resolves the pick by index,
materialising a static default before attaching it to a subscription. The codec
uses the distinct ``"pst"`` namespace.

All functions are pure: no service calls, no I/O.

Callback-data scheme
--------------------
::

    pst:1:<index>:<preset_id|->
    │   │ │       └─ database id, or '-' for a static default
    │   │ └───────── position in the rendered preset list (int)
    │   └─────────── scheme version
    └─────────────── namespace prefix
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from brizocast.bot.keyboards.callbacks import (
    NAMESPACE_PRESET,
    encode_fields,
    split_fields,
)
from brizocast.services.preset_service import PresetOption

__all__ = [
    "PresetPick",
    "build_preset_pick_keyboard",
    "encode_preset_callback",
    "parse_preset_callback",
]

_PRESET_FIELD_COUNT = 2  # list index + preset id (or sentinel)
_NO_ID_SENTINEL = "-"

# Marker appended to AI-generated preset labels (Req 19.5 interchangeability).
_AI_MARKER = " ✨"


@dataclass(frozen=True, slots=True)
class PresetPick:
    """Parsed identity of a preset-pick tap.

    ``index`` is the option's position in the keyboard that was shown — the
    stable reference the handler resolves against a fresh listing. ``preset_id``
    is the database id when the option was already persisted, or ``None`` for a
    static default that must be materialised on selection.
    """

    index: int
    preset_id: int | None


def encode_preset_callback(index: int, preset_id: int | None) -> str:
    """Encode a preset pick into a ``callback_data`` string.

    :param index: The option's position in the rendered preset list (>= 0).
    :param preset_id: The option's database id, or ``None`` for a static default.
    :returns: A ``callback_data`` string within Telegram's 64-byte limit.
    :raises ValueError: If ``index`` is negative.
    """

    if index < 0:
        raise ValueError(f"preset index must be >= 0, got {index}")
    id_field = _NO_ID_SENTINEL if preset_id is None else str(preset_id)
    return encode_fields(NAMESPACE_PRESET, (str(index), id_field))


def parse_preset_callback(raw: str) -> PresetPick:
    """Parse preset ``callback_data`` back into a :class:`PresetPick`.

    Inverse of :func:`encode_preset_callback`.

    :param raw: The ``callback_data`` string received from Telegram.
    :returns: The decoded preset pick.
    :raises ValueError: If ``raw`` is malformed or has non-integer fields.
    """

    index_raw, id_raw = split_fields(raw, NAMESPACE_PRESET, _PRESET_FIELD_COUNT)
    try:
        index = int(index_raw)
    except ValueError as exc:
        raise ValueError(f"preset callback has non-integer index: {raw!r}") from exc

    if id_raw == _NO_ID_SENTINEL:
        preset_id: int | None = None
    else:
        try:
            preset_id = int(id_raw)
        except ValueError as exc:
            raise ValueError(f"preset callback has non-integer preset_id: {raw!r}") from exc
    return PresetPick(index=index, preset_id=preset_id)


def _button_label(option: PresetOption) -> str:
    """Render a concise label for a preset pick button (name, region, AI marker)."""

    label = option.name
    if option.region:
        label = f"{label} ({option.region})"
    if option.ai_generated:
        label = f"{label}{_AI_MARKER}"
    return label


def build_preset_pick_keyboard(options: Sequence[PresetOption]) -> InlineKeyboardMarkup:
    """Build a preset-pick keyboard from listed preset options (Req 4.3, 13.6).

    Renders one button per option, one per row, in the given order. Each
    button's callback data references the option by its list position so a tap
    is resolvable even for static defaults that have no database id.

    :param options: The presets to display, in listing order.
    :returns: The assembled :class:`telegram.InlineKeyboardMarkup`.
    :raises ValueError: If ``options`` is empty.
    """

    if not options:
        raise ValueError("build_preset_pick_keyboard requires at least one preset")

    rows = [
        [
            InlineKeyboardButton(
                _button_label(option),
                callback_data=encode_preset_callback(index, option.preset_id),
            )
        ]
        for index, option in enumerate(options)
    ]
    return InlineKeyboardMarkup(rows)
