# Implementation Plan: BrizoCast — Outdoor Conditions Alert Bot

## Overview

This plan turns the Clean-Architecture design into an incremental, test-first Python
implementation. It follows the design's `M1–M10` milestone ordering as the backbone:
Foundations → pure domain core → providers & caching → services & persistence →
notification engine → Telegram bot → scheduler → AI presets → monetization → hardening.

Each task builds on prior tasks with no orphaned code: domain logic is written and
property-tested in isolation, infrastructure adapters implement the ports, services
compose the domain over the repositories, and everything is wired into the DI container
(`core/container.py`) and the scheduler/bot composition root (`bot/app.py`) by the end.

Conventions used throughout:
- Language/runtime: Python 3.12+, fully type-annotated targeting `mypy --strict`.
- Frameworks: `python-telegram-bot` (async, long-polling, outbound only), SQLAlchemy 2.x
  over SQLite (WAL), APScheduler (AsyncIOScheduler), Pydantic v2 + `pydantic-settings`,
  `httpx`, `google-generativeai` (optional).
- Property-based tests use **Hypothesis** with a **minimum of 100 iterations**
  (`@settings(max_examples=100)` or higher) and fake/in-memory I/O. Each correctness
  property `1–31` is covered by exactly **one** property test, tagged with a comment in
  the format `# Feature: outdoor-conditions-alert-bot, Property {number}: {property_text}`.
- Tasks marked with `*` are optional (unit / property / integration tests) and may be
  skipped for a faster MVP; non-`*` tasks are core implementation.

## Tasks

- [x] 1. M1 — Foundations: project skeleton, config, persistence, container, Docker
  - [x] 1.1 Create project skeleton and tooling
    - Create the `brizocast/` package tree (`bot/`, `core/`, `activities/`, `providers/`, `notifications/`, `scheduler/`, `services/`, `repositories/`, `models/`, `database/`, `storage/`, `config/`, `tests/{unit,property,integration}`) with `__init__.py` files
    - Author `pyproject.toml` with dependencies (python-telegram-bot, sqlalchemy, aiosqlite, apscheduler, pydantic, pydantic-settings, httpx, google-generativeai, alembic) and dev deps (pytest, pytest-asyncio, hypothesis, mypy)
    - Configure `mypy --strict`, `pytest`, and Hypothesis defaults
    - _Requirements: 16.1_

  - [x] 1.2 Implement Pydantic Settings configuration loader
    - Implement `config/settings.py` `Settings` (BaseSettings) and `PlanLimit` with all fields and defaults from the design (Telegram token, DATABASE_URL, scheduler interval, forecast/geocoding providers, cache TTL, notification settings, AI settings, monetization flag + PLAN_LIMITS)
    - Load from `.env`; fail startup with a log message naming the offending field on missing/invalid required values
    - Default forecast provider to Open-Meteo Marine, AI provider to Gemini, monetization to disabled when unspecified
    - _Requirements: 15.1, 15.2, 15.3, 15.4, 15.5, 15.6, 15.7, 15.9, 15.10_

  - [ ]* 1.3 Write unit tests for configuration validation
    - Test that missing/invalid required values terminate startup and log the field name; test defaulting behavior
    - _Requirements: 15.3, 15.4, 18.4_

  - [x] 1.4 Implement SQLAlchemy ORM models
    - Implement `models/base.py` (DeclarativeBase + timestamp mixin) and all entity models: users, plans, payment_records, activities, locations, subscriptions, presets, custom_conditions, surf_spots, forecast_cache, notifications_sent, feedback
    - Encode the normalized schema and constraints from the ERD (one user↔one plan, subscription↔one user/activity/location, single presets table for default/custom/AI)
    - _Requirements: 16.1, 16.2, 16.6, 16.7, 16.8, 16.9, 16.10, 20.2_

  - [x] 1.5 Implement database session and bootstrap
    - Implement `database/session.py` (async engine, session factory, WAL pragma) and `database/bootstrap.py` (create schema if absent; migrate/recreate when the schema version is incompatible) plus Alembic scaffolding
    - _Requirements: 16.4, 16.5_

  - [ ]* 1.6 Write integration tests for schema bootstrap
    - Against a temporary SQLite DB, verify create-if-absent and migrate/recreate on incompatible version
    - _Requirements: 16.4, 16.5_

  - [x] 1.7 Implement logging setup
    - Configure structured `logging` with severity levels and contextual fields (provider, subscription id, spot key); ensure a failure to write a log entry does not crash the process
    - _Requirements: 18.1, 18.5, 18.6_

  - [x] 1.8 Implement DI container skeleton and domain error hierarchy
    - Implement `core/errors.py` (domain exception hierarchy: `ProviderRequestError`, `QuotaExceededError`, etc.) and `core/container.py` skeleton holding config, session factory, and logging, with registration hooks to be filled by later milestones
    - _Requirements: 18.6_

  - [x] 1.9 Author Docker, Compose, and health module
    - Write the multi-arch `Dockerfile` (python:3.12-slim, non-root user, `/app/data` volume), `docker-compose.yml` (env_file, persistent SQLite volume, healthcheck, restart policy, ARM/Raspberry Pi compatible, no exposed ports), `.env.example`, and a `brizocast/health.py` module used by the healthcheck
    - _Requirements: 15.1, 15.2_

- [x] 2. M2 — Pure domain core (geo, scoring, anti-spam, activity registry)
  - [x] 2.1 Implement domain value objects
    - Implement `core/domain` value objects / Pydantic models: `GeoPoint`, `GeoCandidate`, `ForecastWindow` (+ `key()`), `ForecastStep`, `Forecast`, `FactorContribution`, `DaylightInfo`, `PresetParams`/`SurfConditions` placeholders shared by ports
    - _Requirements: 6.4_

  - [x] 2.2 Implement geo distance and discovery math
    - Implement `core/domain/geo.py` haversine distance (non-negative, zero for identical points, symmetric) and a pure `spots_within(center, radius_km, spots)` filter
    - _Requirements: 5.3, 5.4_

  - [ ]* 2.3 Write property test for spot discovery and distance metric
    - `# Feature: outdoor-conditions-alert-bot, Property 10: Spot discovery returns exactly the spots within radius`
    - **Property 10** — discovered set equals exactly spots with great-circle distance ≤ radius; distance is non-negative, zero for identical points, symmetric
    - **Validates: Requirements 5.3, 5.4**

  - [x] 2.4 Implement daylight calculation
    - Implement `core/domain/daylight.py` sunrise/sunset computation and `DaylightInfo.is_daylight(timestamp)`
    - _Requirements: 8.8_

  - [x] 2.5 Implement ScoreCategory and band mapping
    - Implement `ScoreCategory` enum and `from_score(score)` with bands Perfect 95-100, Excellent 85-94, Good 70-84, Rideable 50-69, Ignore <50
    - _Requirements: 8.2, 8.3, 8.4, 8.5, 8.6_

  - [ ]* 2.6 Write property test for score category partition
    - `# Feature: outdoor-conditions-alert-bot, Property 2: Score categories partition the 0-100 range`
    - **Property 2** — every integer 0-100 maps to exactly one mutually-exclusive, jointly-exhaustive category band
    - **Validates: Requirements 8.2, 8.3, 8.4, 8.5, 8.6**

  - [x] 2.7 Implement SurfConditions schema, factor curves, and weighted SurfScorer
    - Implement `activities/surf/conditions.py` (`SurfConditions` Pydantic schema) and `activities/surf/scorer.py`: pure factor curves (`wave_height_curve`, `period_curve`, `wind_speed_curve`, `direction_match`), weighted combine to a clamped 0-100 int, `ScoreResult`/`ScoreBreakdown`, and a daylight gate that returns Ignore (score 0) outside daylight; expose `score()` and `score_series()`; keep the module free of forecast/notification/persistence imports
    - _Requirements: 8.1, 8.7, 8.8, 8.9, 8.11_

  - [ ]* 2.8 Write property test for bounded surf score
    - `# Feature: outdoor-conditions-alert-bot, Property 1: Surf score is always a bounded integer`
    - **Property 1** — for any forecast step and valid conditions the score is an integer in [0, 100]
    - **Validates: Requirements 8.1**

  - [ ]* 2.9 Write property test for weighted (non pass/fail) scoring
    - `# Feature: outdoor-conditions-alert-bot, Property 3: Score is a weighted combination, not a pass/fail threshold`
    - **Property 3** — improving one favorable factor while holding others fixed never decreases the score, and scores strictly between 0 and 100 are achievable
    - **Validates: Requirements 8.7**

  - [ ]* 2.10 Write property test for daylight-only suppression
    - `# Feature: outdoor-conditions-alert-bot, Property 4: Daylight-only suppresses non-daylight steps`
    - **Property 4** — with daylight-only enabled, any step outside daylight hours is assigned Ignore (score 0)
    - **Validates: Requirements 8.8**

  - [ ]* 2.11 Write property test for complete score breakdown
    - `# Feature: outdoor-conditions-alert-bot, Property 5: Score breakdown is complete`
    - **Property 5** — the result carries a contribution for each of wave height, swell period, wind speed, wind direction, swell direction, and the factor weights sum to 1
    - **Validates: Requirements 8.11**

  - [x] 2.12 Implement pure anti-spam decision policy
    - Implement `core/domain/antispam.py` `NotificationDecision` enum, `AntiSpamConfig`, and `decide(candidate, last, cfg)` matching the decision table (below Rideable → SUPPRESS; no prior → SEND_NEW; ≤ prior → SUPPRESS; improvement < threshold → SUPPRESS; ≥ threshold → SEND_IMPROVED)
    - _Requirements: 9.1, 9.3, 9.4, 9.5_

  - [ ]* 2.13 Write property test for the anti-spam decision table
    - `# Feature: outdoor-conditions-alert-bot, Property 6: Anti-spam decision table holds`
    - **Property 6** — decision is exactly determined by candidate category, prior record, and significant-improvement threshold per the table
    - **Validates: Requirements 9.1, 9.3, 9.4, 9.5**

  - [x] 2.14 Implement Activity abstraction, registry, Scorer port, and SurfActivity
    - Implement `core/ports/scorer.py` (`Scorer` Protocol), `activities/base.py` (`Activity` ABC), `activities/registry.py` (`register`/`get`/`all`/`available`), and `activities/surf/activity.py` (`SurfActivity` exposing scorer, conditions schema, default forecast-provider key, `available_in_mvp=True`); register Surf
    - _Requirements: 1.3, 8.10, 17.1, 17.2, 17.3, 17.4, 17.6_

  - [ ]* 2.15 Write property test for activity abstraction conformance
    - `# Feature: outdoor-conditions-alert-bot, Property 31: Activity abstraction conformance`
    - **Property 31** — every registered activity is an `Activity` exposing a scorer, conditions schema, and default forecast-provider key; non-MVP activities report as unavailable
    - **Validates: Requirements 1.3, 17.2**

- [x] 3. M3 — Providers and caching
  - [x] 3.1 Define provider and repository ports
    - Implement `core/ports/forecast_provider.py`, `core/ports/geocoding_provider.py`, `core/ports/ai_provider.py`, `core/ports/spot_repository.py`, and `core/ports/repositories.py` Protocols exactly as specified
    - _Requirements: 6.1, 6.6_

  - [x] 3.2 Implement Open-Meteo Marine forecast provider and factory
    - Implement `providers/forecast/open_meteo_marine.py` (httpx call, response → `Forecast`/`ForecastStep` mapping, raising `ProviderRequestError` on network failure) and `providers/forecast/factory.py` (key→impl resolution; unknown/empty → Open-Meteo Marine default); add Stormglass/Windy pluggable stubs implementing the port
    - _Requirements: 6.2, 6.3, 6.4, 6.5, 15.5, 18.2_

  - [ ]* 3.3 Write property test for complete forecast steps
    - `# Feature: outdoor-conditions-alert-bot, Property 13: Forecast steps are complete`
    - **Property 13** — every produced forecast step carries wave height, swell period, swell direction, wind speed, wind direction, and a timestamp
    - **Validates: Requirements 6.4**

  - [ ]* 3.4 Write integration test for Open-Meteo response mapping
    - Map a recorded sample Open-Meteo Marine payload to `Forecast` and assert field mapping
    - _Requirements: 6.2_

  - [x] 3.5 Implement Open-Meteo geocoding provider and factory
    - Implement `providers/geocoding/open_meteo_geocoding.py` (`search()` → `list[GeoCandidate]`, empty list on no matches, `ProviderRequestError` on failure) and `providers/geocoding/factory.py`
    - _Requirements: 2.3, 2.4, 2.6, 2.11, 18.2_

  - [x] 3.6 Implement JSON spot repository and discovery service
    - Implement `storage/spots/surf_spots.json` seed dataset, `providers`/`repositories` `JsonSpotRepository` (implements `SpotRepository`), and `services/spot_discovery_service.py` using `geo.spots_within`; when no spots are within radius, record no-nearby-spots and skip forecast collection
    - _Requirements: 5.1, 5.2, 5.5, 5.6_

  - [ ]* 3.7 Write property test for empty-discovery skip
    - `# Feature: outdoor-conditions-alert-bot, Property 11: Empty discovery skips forecast collection`
    - **Property 11** — a subscription whose discovery yields no spots is recorded as no-nearby-spots and triggers no forecast-provider request
    - **Validates: Requirements 5.5**

  - [x] 3.8 Implement forecast cache repository and ForecastService
    - Implement `repositories/forecast_cache_repo.py` (keyed by `spot_key`, `expires_at = fetched_at + TTL`) and `services/forecast_service.py` cache logic: return non-expired cached forecast without calling the provider; otherwise call the provider once and store; share cached forecasts across all subscriptions referencing the same spot
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.5_

  - [ ]* 3.9 Write property test for cache freshness and sharing
    - `# Feature: outdoor-conditions-alert-bot, Property 12: Forecast cache freshness and sharing`
    - **Property 12** — cache returns without provider call exactly when a stored entry's age < TTL; otherwise provider called once and stored; provider called at most once per TTL window across subscriptions
    - **Validates: Requirements 7.2, 7.3, 7.4, 7.5**

  - [x] 3.10 Implement AI provider port wiring, NullAIProvider, and AI factory
    - Implement `providers/ai/null_ai.py` (`is_available()` → False) and `providers/ai/factory.py` `build_ai_provider(cfg)` resolving the provider key (Null when AI disabled or unkeyed; default key "gemini" when enabled and unspecified), with a registry so the Gemini impl can register later; register forecast/geocoding/AI factories in the container
    - _Requirements: 6.3, 15.5, 15.7, 15.10, 19.1, 19.3_

  - [ ]* 3.11 Write property test for provider factory selection and defaults
    - `# Feature: outdoor-conditions-alert-bot, Property 22: Provider factory selection and defaults`
    - **Property 22** — registered keys resolve to a matching implementation; unspecified forecast → Open-Meteo Marine; AI enabled & unspecified → Gemini; unspecified monetization → disabled
    - **Validates: Requirements 6.3, 15.5, 15.7, 15.10, 19.3**

- [x] 4. M4 — Services and persistence
  - [x] 4.1 Implement SQLAlchemy repositories
    - Implement `repositories/base.py` and user, location, subscription, preset, condition, notification, feedback, plan, and payment repositories implementing the port Protocols (forecast_cache repo already done in 3.8); isolate persistence from services
    - _Requirements: 16.3_

  - [ ]* 4.2 Write property test for persistence round-trip
    - `# Feature: outdoor-conditions-alert-bot, Property 15: Persistence round-trip for user-owned entities`
    - **Property 15** — persisting then reading back a location/favorite/custom conditions/preset (incl. AI-generated)/notification preference/plan yields equal fields; AI presets persist in the same presets table/shape as static defaults
    - **Validates: Requirements 2.2, 2.5, 2.7, 3.7, 4.6, 10.2, 11.1, 16.10, 19.4, 20.4**

  - [x] 4.3 Implement UserService with Free plan provisioning
    - Implement `services/user_service.py`: create/lookup user keyed by Telegram id (idempotent) and assign exactly one Free, active Plan on creation
    - _Requirements: 1.7, 20.1, 20.2, 20.3, 20.4_

  - [ ]* 4.4 Write property test for idempotent user provisioning
    - `# Feature: outdoor-conditions-alert-bot, Property 21: User provisioning is idempotent with a Free active plan`
    - **Property 21** — any sequence of first interactions yields exactly one user per Telegram id, associated with exactly one Free active plan
    - **Validates: Requirements 1.7, 20.1, 20.3**

  - [x] 4.5 Implement LocationService and favorites management
    - Implement `services/location_service.py`: create locations from a shared point or a selected geocoding candidate; save/list/delete favorites associated with the user
    - _Requirements: 2.2, 2.5, 2.7, 2.8, 2.9, 2.10_

  - [ ]* 4.6 Write property test for favorites collection integrity
    - `# Feature: outdoor-conditions-alert-bot, Property 16: Favorites collection integrity`
    - **Property 16** — listing favorites returns all saved (with label and place name); deleting one removes exactly that favorite leaving the rest
    - **Validates: Requirements 2.7, 2.8, 2.9, 2.10**

  - [x] 4.7 Implement SubscriptionService with validation
    - Implement `services/subscription_service.py`: create/list/remove/edit subscriptions bound to one user/activity/location; validate `search_radius_km` in [1, 200] (default 30); require a location before completion; persist radius edits
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.6, 3.7, 3.8, 3.9, 3.10, 16.6_

  - [ ]* 4.8 Write property test for search radius validation boundary
    - `# Feature: outdoor-conditions-alert-bot, Property 17: Search radius validation boundary`
    - **Property 17** — creation/edit accepts a radius iff it lies in [1, 200] km; values outside are rejected
    - **Validates: Requirements 3.9, 3.10**

  - [ ]* 4.9 Write property test for subscription set operations
    - `# Feature: outdoor-conditions-alert-bot, Property 20: Subscription set operations`
    - **Property 20** — all created subscriptions persist for the user; removing a selected one deletes exactly that subscription and leaves the others
    - **Validates: Requirements 3.3, 3.6**

  - [x] 4.10 Implement PresetService, static presets, and effective-conditions resolution
    - Implement `activities/surf/presets.py` (bundled static `Default_Presets` per region) and `services/preset_service.py`: list defaults + custom presets, persist custom conditions (reject min wave > max wave), and resolve effective conditions (custom → selected preset → region's first default)
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 4.9, 16.10_

  - [ ]* 4.11 Write property test for effective conditions resolution
    - `# Feature: outdoor-conditions-alert-bot, Property 14: Effective conditions resolution`
    - **Property 14** — conditions used are custom when present, else the selected preset, else the region's first default preset for the subscription's location
    - **Validates: Requirements 4.7, 4.9**

  - [ ]* 4.12 Write property test for wave-height bounds validation
    - `# Feature: outdoor-conditions-alert-bot, Property 18: Wave-height bounds validation`
    - **Property 18** — custom-conditions entry is accepted iff minimum wave height ≤ maximum wave height
    - **Validates: Requirements 4.8**

  - [x] 4.13 Implement subscription listing summary builder
    - Add a pure `summarize()`/listing-data method to `SubscriptionService` producing one structured entry per subscription with activity, location, search radius, and notification mode (consumed later by the `/subscriptions` formatter)
    - _Requirements: 3.5_

  - [ ]* 4.14 Write property test for subscription listing completeness
    - `# Feature: outdoor-conditions-alert-bot, Property 19: Subscription listing completeness`
    - **Property 19** — the listing includes a line for each subscription showing activity, location, search radius, and notification mode
    - **Validates: Requirements 3.5**

- [x] 5. M5 — Notification engine
  - [x] 5.1 Implement NotificationEngine gating and mode routing
    - Implement `notifications/engine.py` `process(subscription, score_results, now)`: apply the pure anti-spam policy, then gate by mute, snooze (suppress while `now < snooze_until`, persist past snooze when muted), and quiet hours (immediate mode); route qualifying results to immediate dispatch or digest buffering
    - _Requirements: 9.2, 10.3, 11.2, 11.3, 11.4, 11.5, 11.6_

  - [ ]* 5.2 Write property test for notification gating
    - `# Feature: outdoor-conditions-alert-bot, Property 8: Notification gating (quiet hours, mute, snooze)`
    - **Property 8** — dispatch is suppressed exactly when muted, or before snooze_until, or (immediate mode) within quiet hours; resumes when not muted, snooze elapsed, and outside quiet hours
    - **Validates: Requirements 11.2, 11.3, 11.4, 11.5, 11.6**

  - [x] 5.3 Implement notification modes and digest selection
    - Implement `notifications/modes.py`: immediate, morning digest, evening digest, weekly best day; buffer qualifying scores per period and select the weekly best day by highest max score; emit nothing for an empty period
    - _Requirements: 10.1, 10.5, 10.6, 10.7, 10.8_

  - [ ]* 5.4 Write property test for empty digest and weekly best day
    - `# Feature: outdoor-conditions-alert-bot, Property 9: Empty digest sends nothing`
    - **Property 9** — an empty digest period dispatches no message; a non-empty weekly digest selects the day with the highest maximum surf score
    - **Validates: Requirements 10.7, 10.8**

  - [x] 5.5 Implement NotificationService record persistence and window identity
    - Implement `notifications/window.py` (forecast-window identity/`key()` helpers) and `services/notification_service.py`: persist a `Notification_Record` (subscription, spot, score, forecast window, timestamp) on successful dispatch; query latest record per (subscription, spot, window)
    - _Requirements: 9.2, 10.2_

  - [ ]* 5.6 Write property test for faithful alert recording
    - `# Feature: outdoor-conditions-alert-bot, Property 7: Sent alerts are recorded faithfully`
    - **Property 7** — a persisted `Notification_Record` carries the same subscription, spot, score, forecast window, and a send timestamp as the dispatched alert
    - **Validates: Requirements 9.2**

  - [x] 5.7 Implement Telegram sender with retry, digest fallback, and feedback persistence
    - Implement `notifications/sender.py` (delivery port impl): retry up to `NOTIFY_RETRY_COUNT`, add to the next digest on exhaustion, log each delivery failure and continue the batch; implement `repositories/feedback_repo.py`-backed persistence of 👍/👎 feedback (subscription, spot, score, rating)
    - _Requirements: 10.4, 12.4, 12.5, 18.3_

  - [ ]* 5.8 Write property test for delivery resilience
    - `# Feature: outdoor-conditions-alert-bot, Property 26: Notification delivery resilience`
    - **Property 26** — for a batch where some deliveries fail, every remaining delivery is attempted and each failure logged rather than aborting the batch
    - **Validates: Requirements 18.3**

  - [x] 5.9 Implement explainable alert formatter
    - Implement `bot/formatters` alert text including surf score, score category, spot name, forecast window, and the wave/period/wind per-factor breakdown, plus an inline 👍/👎 feedback keyboard
    - _Requirements: 12.1, 12.2, 12.3_

  - [ ]* 5.10 Write property test for explainable alert content and feedback persistence
    - `# Feature: outdoor-conditions-alert-bot, Property 27: Explainable alert content and feedback persistence`
    - **Property 27** — formatted alert text includes score, category, spot name, window, and wave/period/wind breakdown with 👍/👎 controls; a feedback action persists subscription, spot, score, and rating
    - **Validates: Requirements 12.1, 12.2, 12.3, 12.4**

- [x] 6. Checkpoint — core domain, providers, services, and notifications
  - Ensure all tests pass, ask the user if questions arise.

- [x] 7. M6 — Telegram bot (thin handlers and conversations)
  - [x] 7.1 Implement inline keyboard builders and message formatters
    - Implement `bot/keyboards/` pure inline-keyboard builders and `bot/formatters/` renderers, including the `/subscriptions` list formatter consuming the summary builder from 4.13
    - _Requirements: 13.6_

  - [x] 7.2 Implement onboarding conversation
    - Implement `bot/handlers/start.py` + `bot/conversations/` onboarding: create the user on first interaction, present activity selection inline keyboard, mark non-MVP activities unavailable and keep the user in selection on choosing one, advance to location setup on Surf, and show the main menu for already-onboarded users
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7_

  - [x] 7.3 Implement location handler and conversation
    - Implement `bot/handlers/location.py`: share-location / city / place / favorites options, geocoding candidate selection via inline keyboard, no-match re-prompt, save/list/delete favorites, and "temporarily unavailable" on geocoding failure
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.9, 2.10, 2.11_

  - [x] 7.4 Implement subscription conversations
    - Implement `bot/handlers/subscriptions.py` + conversations for `/add` (activity→location→radius→preset/custom→prefs), `/subscriptions` (list), and `/remove` (select→delete→confirm); prompt to set a location when missing
    - _Requirements: 3.1, 3.4, 3.5, 3.6, 3.8_

  - [x] 7.5 Implement presets handler and custom-conditions conversation
    - Implement `bot/handlers/presets.py` (list default + custom presets) and the custom-conditions conversation collecting all fields with min>max wave rejection
    - _Requirements: 4.1, 4.3, 4.5, 4.8_

  - [x] 7.6 Implement settings handler
    - Implement `bot/handlers/settings.py`: edit notification mode, quiet hours, and mute/snooze state; persist preferences on the subscription
    - _Requirements: 10.2, 11.1, 11.3, 11.4, 13.5_

  - [x] 7.7 Implement status and forecast handlers
    - Implement `services/status_service.py` and `bot/handlers/status.py`: `/status` reports active subscription count and most recent scheduler-run time; `/forecast` reports the current best surf score and spot for a selected subscription regardless of mute/snooze
    - _Requirements: 13.3, 13.4_

  - [ ]* 7.8 Write property test for /status and /forecast reporting
    - `# Feature: outdoor-conditions-alert-bot, Property 28: /status and /forecast reporting`
    - **Property 28** — `/status` reports active subscription count and last scheduler-run time; `/forecast` reports best score and spot regardless of mute/snooze state
    - **Validates: Requirements 13.3, 13.4**

  - [x] 7.9 Implement help, unknown-command fallback, feedback callback, and command registration
    - Implement `bot/handlers/help.py` (list commands + descriptions and unknown-command fallback suggesting `/help`) and `bot/handlers/feedback.py` (👍/👎 callbacks); register all command handlers (`/start`, `/location`, `/subscriptions`, `/add`, `/remove`, `/settings`, `/presets`, `/status`, `/forecast`, `/help`)
    - _Requirements: 12.3, 13.1, 13.2, 13.7_

  - [ ]* 7.10 Write unit tests for handler and conversation flows
    - Cover unavailable-activity re-prompt, already-onboarded main menu, geocode no-match, missing-location prompt, `/help` listing, settings editing, and unknown-command fallback
    - _Requirements: 1.4, 1.5, 1.6, 2.6, 3.8, 13.2, 13.5, 13.7_

- [x] 8. M7 — Scheduler
  - [x] 8.1 Implement the forecast-check job pipeline
    - Implement `scheduler/forecast_check_job.py`: load all active subscriptions, select each subscription's scorer via `ActivityRegistry`, discover nearby spots, fetch forecasts via `ForecastService` (skip spot on provider error, logging context), resolve effective conditions, compute scores, look up notification history, apply anti-spam + gating, and dispatch; isolate per-subscription errors (log and continue)
    - _Requirements: 5.5, 6.5, 14.2, 14.3, 17.5, 17.6, 18.1, 18.2_

  - [ ]* 8.2 Write property test for scheduler pipeline correctness and resilience
    - `# Feature: outdoor-conditions-alert-bot, Property 24: Scheduler pipeline correctness and resilience`
    - **Property 24** — a run selects each subscription's scorer by activity, computes scores, applies anti-spam/gating, and dispatches exactly the permitted alerts; an error in one subscription still processes the rest
    - **Validates: Requirements 14.2, 14.3, 17.5, 17.6**

  - [x] 8.3 Implement scheduler runner with interval guard and timestamp semantics
    - Implement `scheduler/runner.py` (AsyncIOScheduler): run at the configured interval, do not start a new job before the interval elapses, record the completion timestamp only on success (leave unchanged if the job fails before completion), and treat the job as completed while logging a timestamp-write failure
    - _Requirements: 14.1, 14.4, 14.5, 14.6, 14.7_

  - [ ]* 8.4 Write property test for interval and completion-timestamp semantics
    - `# Feature: outdoor-conditions-alert-bot, Property 25: Scheduler interval and completion-timestamp semantics`
    - **Property 25** — no new job starts before previous-run + interval; a successful job updates the last-run timestamp; a job that starts but does not complete leaves it unchanged
    - **Validates: Requirements 14.4, 14.5, 14.7**

  - [x] 8.5 Implement digest jobs
    - Implement `scheduler/digest_jobs.py`: morning/evening/weekly triggers that drain each subscription's buffered qualifying scores and emit a single summary, sending nothing when empty
    - _Requirements: 10.5, 10.6, 10.7_

  - [ ]* 8.6 Write integration test for scheduler wiring
    - Verify the forecast-check job is registered at the configured interval and runs end-to-end against fakes (fake provider, in-memory repos, fake sender)
    - _Requirements: 14.1, 14.2_

- [x] 9. M8 — AI-assisted preset generation (optional capability)
  - [x] 9.1 Implement Gemini AI provider
    - Implement `providers/ai/gemini.py` (`GeminiProvider` implementing `AIProvider`): `is_available()`, `generate_region_preset(region, activity_key)` returning `PresetParams` with the same shape as a static preset; register it in the AI factory under "gemini"
    - _Requirements: 19.1, 19.2, 19.3, 19.4_

  - [x] 9.2 Implement AI/static preset resolution and fallback in PresetService
    - Extend `services/preset_service.py` `get_region_presets`: use static defaults when AI is disabled/unkeyed; when available, prepend the AI-generated preset; on AI failure, log with provider/context and fall back to static defaults so a scheduler run continues; keep scoring/notification/forecast layers free of AI references
    - _Requirements: 15.6, 15.8, 19.5, 19.6, 19.7, 19.8, 19.9, 19.10_

  - [ ]* 9.3 Write property test for preset source resolution and AI interchangeability
    - `# Feature: outdoor-conditions-alert-bot, Property 23: Preset source resolution and AI interchangeability`
    - **Property 23** — AI disabled/unkeyed/failed → static defaults are used; scoring with an AI preset equals scoring with a static preset carrying identical parameters
    - **Validates: Requirements 15.8, 19.5, 19.6, 19.7, 19.8, 19.9**

- [x] 10. M9 — Monetization scaffolding
  - [x] 10.1 Implement EntitlementService and gate subscription creation
    - Implement `services/entitlement_service.py`: `max_subscriptions`, `allowed_notification_modes`, and `assert_can_create_subscription` (unlimited/all-modes while monetization disabled; enforce configured `PLAN_LIMITS` per tier otherwise, raising `QuotaExceededError`); call `assert_can_create_subscription` at the start of `SubscriptionService.create` as the sole touch-point and restrict available notification modes by tier
    - _Requirements: 21.1, 21.2, 21.3, 21.4, 21.5, 21.6, 21.7_

  - [ ]* 10.2 Write property test for config-driven entitlement gating
    - `# Feature: outdoor-conditions-alert-bot, Property 29: Entitlement gating is config-driven`
    - **Property 29** — disabled → unlimited subscriptions and all modes; enabled → creation allowed iff count < tier `max_subscriptions`, allowed modes equal the tier's configured modes, tracking config without code changes
    - **Validates: Requirements 21.1, 21.2, 21.3, 21.4, 21.5, 21.7**

  - [x] 10.3 Implement plan-expiry check and payment-record guard
    - Implement a periodic plan-expiry check that sets a Paid plan's status to expired when its expiry time has passed, and ensure `Payment_Record`s are never populated while `MONETIZATION_ENABLED` is disabled
    - _Requirements: 20.5, 20.6, 20.7_

  - [ ]* 10.4 Write property test for paid plan expiry transition
    - `# Feature: outdoor-conditions-alert-bot, Property 30: Paid plan expiry transition`
    - **Property 30** — a paid plan whose expiry is past becomes expired on the check; while monetization is disabled the payment-records table stays empty
    - **Validates: Requirements 20.6, 20.7**

- [x] 11. M10 — Composition root wiring and hardening
  - [x] 11.1 Wire the composition root and application bootstrap
    - Complete `core/container.py` registrations for all services, providers, repositories, the notification engine, and the scheduler; implement `bot/app.py` bootstrap that validates config, bootstraps the database, builds the python-telegram-bot Application with all handlers, starts the APScheduler jobs (forecast-check + digests + plan-expiry), and runs long-polling
    - _Requirements: 15.1, 16.4_

  - [ ]* 11.2 Write smoke tests
    - Verify configuration load + validation, required tables present, all command handlers registered, and log entries carry severity levels
    - _Requirements: 13.1, 15.1, 15.2, 15.3, 15.4, 16.1, 16.2, 18.6_

  - [ ]* 11.3 Write extensibility integration test
    - Add a fake `ForecastProvider`, `AIProvider`, and `Activity` end-to-end and verify they work without modifying the scoring or notification engines
    - _Requirements: 6.6, 8.10, 17.3, 19.10_

- [x] 12. Final checkpoint — full test sweep
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional (unit / property / integration tests) and can be skipped for a faster MVP; core implementation tasks are never optional.
- Each correctness property `1–31` is implemented by exactly one Hypothesis property test (min 100 iterations) tagged `# Feature: outdoor-conditions-alert-bot, Property {number}: {property_text}`, placed close to the implementation it validates to catch errors early.
- Pure domain targets (`SurfScorer`, `ScoreCategory.from_score`, `antispam.decide`, `geo`/haversine + discovery, `ForecastService`/cache freshness, `EntitlementService`, effective-conditions resolution, provider/AI factory selection) are the prime property-based-test targets; I/O is faked (in-memory repositories, fake providers, fake Telegram sender).
- Each task references the specific requirement clauses and/or design correctness properties it implements for traceability.
- Checkpoints provide incremental validation; the final wiring (11.1) integrates every component into the DI container and the scheduler/long-polling bot so there is no orphaned code.
- The bot uses Telegram long-polling (outbound only); the Docker image is multi-arch and runs on Raspberry Pi (ARM) with a persistent SQLite volume.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["1.2", "1.4", "1.7"] },
    { "id": 2, "tasks": ["1.3", "1.5", "1.8", "1.9", "2.1"] },
    { "id": 3, "tasks": ["1.6", "2.2", "2.4", "2.5", "3.1"] },
    { "id": 4, "tasks": ["2.3", "2.6", "2.7", "3.2", "3.5", "3.6", "3.10"] },
    { "id": 5, "tasks": ["2.8", "2.9", "2.10", "2.11", "2.12", "2.14", "3.3", "3.4", "3.7", "3.8", "3.11", "4.1", "9.1"] },
    { "id": 6, "tasks": ["2.13", "2.15", "3.9", "4.2", "4.3", "4.5", "4.10", "5.1"] },
    { "id": 7, "tasks": ["4.4", "4.6", "4.7", "4.11", "4.12", "5.2", "5.3", "5.5", "5.9", "9.2"] },
    { "id": 8, "tasks": ["4.8", "4.9", "4.13", "5.4", "5.6", "5.7", "8.1", "9.3", "10.1"] },
    { "id": 9, "tasks": ["4.14", "5.8", "5.10", "7.1", "8.2", "8.3", "10.2"] },
    { "id": 10, "tasks": ["7.2", "7.3", "7.4", "7.5", "7.6", "7.7", "8.4", "8.5", "8.6", "10.3"] },
    { "id": 11, "tasks": ["7.8", "7.9", "10.4"] },
    { "id": 12, "tasks": ["7.10", "11.1"] },
    { "id": 13, "tasks": ["11.2", "11.3"] }
  ]
}
```
