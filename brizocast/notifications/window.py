"""Forecast-window identity helpers (the anti-spam dedup key).

The notification engine deduplicates alerts per ``(subscription, spot,
forecast window)``. The *forecast window* is identified by a stable string key
produced by :meth:`brizocast.core.domain.forecast.ForecastWindow.key`; that key
is what the :class:`~brizocast.notifications` engine stores on a
``NotificationSent`` record and what it passes to
``notification_repo.latest(subscription_id, spot_key, forecast_window_key)`` to
locate the most recent alert for the same window (Req 9.2-9.5).

This module centralises that identity so the engine, the persistence service,
and any future digest logic all derive the key the same way:

* :func:`window_key` — the canonical wrapper over ``ForecastWindow.key()`` used
  everywhere a dedup identity string is needed. Routing every call through this
  helper keeps the dedup identity in one place: if the key scheme ever changes,
  only :class:`ForecastWindow` and this wrapper need updating.
* :func:`window_from_step` — build the ``ForecastWindow`` a single
  :class:`~brizocast.core.domain.forecast.ForecastStep` represents, given the
  step's duration (the spacing between forecast steps).
* :func:`window_for_forecast` — build the ``ForecastWindow`` spanning an entire
  :class:`~brizocast.core.domain.forecast.Forecast` series.

All helpers are pure and free of I/O, Telegram, and persistence imports.

Requirements covered (with the engine/service): 9.2.
"""

from __future__ import annotations

from datetime import timedelta

from brizocast.core.domain.forecast import Forecast, ForecastStep, ForecastWindow

__all__ = [
    "window_for_forecast",
    "window_from_step",
    "window_key",
]


def window_key(window: ForecastWindow) -> str:
    """Return the stable anti-spam dedup identity for ``window``.

    Thin, intention-revealing wrapper over
    :meth:`brizocast.core.domain.forecast.ForecastWindow.key`. This is the exact
    string persisted as ``NotificationSent.forecast_window_key`` and passed to
    ``notification_repo.latest(...)`` for the duplicate-alert lookup, so equal
    windows always map to the same record bucket (Req 9.2-9.5).
    """

    return window.key()


def window_from_step(step: ForecastStep, duration: timedelta) -> ForecastWindow:
    """Build the :class:`ForecastWindow` a single forecast ``step`` represents.

    A forecast step is a point sample that stands in for the interval beginning
    at its timestamp and lasting ``duration`` (typically the spacing between
    consecutive steps, e.g. 1h or 3h). The resulting window's
    :func:`window_key` is the dedup identity for an alert raised on that step.

    Args:
        step: The forecast step whose timestamp starts the window.
        duration: The length of the window; must be positive.

    Returns:
        A ``ForecastWindow`` from ``step.timestamp`` to
        ``step.timestamp + duration``.

    Raises:
        ValueError: If ``duration`` is zero or negative.
    """

    if duration <= timedelta(0):
        raise ValueError("duration must be positive")
    return ForecastWindow(start=step.timestamp, end=step.timestamp + duration)


def window_for_forecast(forecast: Forecast) -> ForecastWindow:
    """Build the :class:`ForecastWindow` spanning an entire ``forecast`` series.

    The window runs from the earliest to the latest step timestamp in the
    series, giving a single dedup identity for whole-series summaries (e.g.
    digests) rather than per-step alerts.

    Args:
        forecast: A forecast carrying at least one step.

    Returns:
        A ``ForecastWindow`` from the minimum to the maximum step timestamp.

    Raises:
        ValueError: If ``forecast`` has no steps.
    """

    if not forecast.steps:
        raise ValueError("cannot build a window for a forecast with no steps")
    timestamps = [step.timestamp for step in forecast.steps]
    return ForecastWindow(start=min(timestamps), end=max(timestamps))
