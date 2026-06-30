# BrizoCast — Outdoor Conditions Alert Bot

A production-ready Telegram bot that monitors weather and ocean conditions for
outdoor sports and sends smart, low-noise notifications when conditions are
favorable. The MVP implements the **Surf** activity end-to-end, with every layer
designed around a common `Activity` abstraction so future sports can be added
without modifying existing code.

## Tech stack

- Python 3.12+, fully type-annotated (`mypy --strict`)
- `python-telegram-bot` (async), SQLAlchemy 2.x over SQLite, APScheduler
- Pydantic v2 + `pydantic-settings`, `httpx`, optional `google-generativeai`
- Testing: `pytest`, `pytest-asyncio`, `hypothesis`

## Development

```bash
# Create a virtual environment and install with dev dependencies
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Type-check and run the test suite
mypy
pytest
```

## Project layout

The package lives under `brizocast/` and follows Clean Architecture: the
Telegram-facing layer (`bot/`) and persistence layer depend inward on the domain
(`core/`, `activities/`, `models/`) and service (`services/`) layers. External
dependencies are reached only through ports (`core/ports/`) wired by a
dependency-injection container at composition time.
