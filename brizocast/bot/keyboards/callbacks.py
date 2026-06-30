"""Shared inline-keyboard callback-data codec primitives (pure, no I/O).

Every conversational inline keyboard in :mod:`brizocast.bot.keyboards` attaches
a ``callback_data`` string to its buttons so the callback router (task 7.9) can
tell *which keyboard* a tap came from and decode the user's selection. This
module owns the **common wire format** those per-keyboard codecs share, mirroring
the style established by :mod:`brizocast.bot.keyboards.feedback` (versioned,
colon-delimited, with a 64-byte guard) while keeping each keyboard family in its
own namespace.

Wire format
-----------
Every callback payload is a colon-delimited string whose first two fields are a
namespace prefix and a scheme version::

    <prefix>:<version>:<field>:<field>:...:<free_form?>
    │        │         └──────────────────────────────── per-keyboard fields
    │        └──────────────────────────────────────────  scheme version ('1')
    └───────────────────────────────────────────────────  namespace prefix

* The **namespace prefix** identifies the originating keyboard family. The
  prefixes here are all DISTINCT from the feedback scheme's ``"fb"`` prefix
  (:data:`~brizocast.bot.keyboards.feedback.CALLBACK_PREFIX`) so the router can
  cheaply discriminate every scheme via :func:`callback_namespace`.
* The **version** (:data:`CALLBACK_VERSION`) lets the format evolve without
  misparsing data attached to older, still-pending messages.
* Telegram limits ``callback_data`` to :data:`TELEGRAM_CALLBACK_DATA_MAX_BYTES`
  bytes; :func:`encode_fields` enforces that limit at build time rather than
  letting Telegram reject the send.

A free-form trailing field (one that may itself contain the separator, such as a
human label or an opaque action token) is supported by parsing with a bounded
split — see :func:`split_fields`'s ``max_splits`` parameter, which works exactly
like the feedback codec's first-N-separators split so the last field keeps any
embedded colons losslessly.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

__all__ = [
    "CALLBACK_VERSION",
    "NAMESPACE_ACTIVITY",
    "NAMESPACE_CONFIRM",
    "NAMESPACE_LOCATION",
    "NAMESPACE_LOCATION_CANDIDATE",
    "NAMESPACE_LOCATION_FAVORITE",
    "NAMESPACE_NOTIFICATION_MODE",
    "NAMESPACE_PRESET",
    "NAMESPACE_SETTINGS",
    "NAMESPACE_SUBSCRIPTION",
    "SEP",
    "TELEGRAM_CALLBACK_DATA_MAX_BYTES",
    "callback_namespace",
    "encode_fields",
    "split_fields",
]

# Scheme version shared by every conversational keyboard codec. Bump only on a
# breaking change to a wire format; per-keyspace fields are documented locally.
CALLBACK_VERSION: Final = "1"

# Telegram's hard limit on the size of a callback_data payload, in bytes.
TELEGRAM_CALLBACK_DATA_MAX_BYTES: Final = 64

# Field separator shared by all schemes.
SEP: Final = ":"

# Namespace prefixes — one per conversational keyboard family. All are distinct
# from the feedback scheme's "fb" prefix so callbacks never collide.
NAMESPACE_ACTIVITY: Final = "act"
NAMESPACE_LOCATION: Final = "loc"
# Distinct sub-schemes within the ``/location`` flow: picking a geocoding
# candidate (``lcd``) and selecting a saved favorite to delete (``lfv``). Kept
# separate from the menu-option scheme (``loc``) so the callback router can
# dispatch each step of the conversation unambiguously.
NAMESPACE_LOCATION_CANDIDATE: Final = "lcd"
NAMESPACE_LOCATION_FAVORITE: Final = "lfv"
NAMESPACE_SUBSCRIPTION: Final = "sub"
NAMESPACE_PRESET: Final = "pst"
NAMESPACE_NOTIFICATION_MODE: Final = "nm"
NAMESPACE_CONFIRM: Final = "cf"
NAMESPACE_SETTINGS: Final = "set"


def encode_fields(prefix: str, fields: Sequence[str]) -> str:
    """Encode a namespaced, versioned ``callback_data`` payload.

    Joins ``prefix``, :data:`CALLBACK_VERSION`, and ``fields`` with
    :data:`SEP`. A free-form field (one that may contain the separator) is only
    safe as the **last** element, because :func:`split_fields` bounds its split.

    :param prefix: The keyboard family's namespace prefix.
    :param fields: The keyboard-specific fields, already stringified.
    :returns: A ``callback_data`` string of at most
        :data:`TELEGRAM_CALLBACK_DATA_MAX_BYTES` bytes.
    :raises ValueError: If the encoded payload would exceed Telegram's 64-byte
        ``callback_data`` limit.
    """

    payload = SEP.join((prefix, CALLBACK_VERSION, *fields))
    encoded_bytes = len(payload.encode("utf-8"))
    if encoded_bytes > TELEGRAM_CALLBACK_DATA_MAX_BYTES:
        raise ValueError(
            "encoded callback exceeds Telegram's "
            f"{TELEGRAM_CALLBACK_DATA_MAX_BYTES}-byte limit "
            f"({encoded_bytes} bytes): {payload!r}"
        )
    return payload


def callback_namespace(raw: str) -> str | None:
    """Return the namespace prefix of ``raw`` if it is a current-version payload.

    A cheap discriminator the callback router uses to dispatch a tap to the
    right codec without fully parsing it. Returns ``None`` for anything that is
    not a ``<prefix>:<current-version>:...`` payload (including other-version or
    malformed data), so callers can fall through to other schemes.

    :param raw: The ``callback_data`` string received from Telegram.
    :returns: The namespace prefix, or ``None`` if ``raw`` is not a recognised
        current-version payload.
    """

    parts = raw.split(SEP, 2)
    if len(parts) >= 2 and parts[1] == CALLBACK_VERSION:
        return parts[0]
    return None


def split_fields(raw: str, prefix: str, field_count: int) -> list[str]:
    """Validate ``raw`` against ``prefix`` and split out its fields.

    Inverse of :func:`encode_fields`. Splits on the first ``field_count``
    separators after the prefix/version pair, so the final field keeps any
    embedded separators (lossless round-trip for a free-form trailing field).

    :param raw: The ``callback_data`` string received from Telegram.
    :param prefix: The expected namespace prefix.
    :param field_count: The number of keyboard-specific fields to extract
        (excludes the prefix and version).
    :returns: The ``field_count`` decoded fields, in order.
    :raises ValueError: If ``raw`` has the wrong prefix or version, or does not
        carry exactly ``field_count`` fields.
    """

    # prefix + version + field_count fields => field_count + 2 segments.
    parts = raw.split(SEP, field_count + 1)
    if len(parts) != field_count + 2:
        raise ValueError(f"malformed callback data for {prefix!r}: {raw!r}")

    found_prefix, version = parts[0], parts[1]
    if found_prefix != prefix:
        raise ValueError(
            f"not a {prefix!r} callback (prefix {found_prefix!r}): {raw!r}"
        )
    if version != CALLBACK_VERSION:
        raise ValueError(f"unsupported callback version {version!r}: {raw!r}")

    return parts[2:]
