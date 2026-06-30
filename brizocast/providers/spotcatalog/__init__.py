"""Spot-catalogue providers — sources of *named* surf spots with coordinates.

A spot catalogue is distinct from a forecast provider: forecast providers
(Open-Meteo, Stormglass, Windy) return a forecast for a coordinate, but carry no
directory of named breaks. A :class:`SpotCatalogProvider` supplies the named
spots themselves (name + coordinates) for an area, which the admin panel imports
into the shared surf-spot dataset.

Adapters:
* ``surfline.SurflineSpotCatalog`` — Surfline's public ``mapview``/``taxonomy``
  endpoints (names + coordinates). Subject to Surfline's terms and bot
  protection; intended for the operator's own use.
"""

from __future__ import annotations
