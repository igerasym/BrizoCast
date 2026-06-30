# Implementation Plan: Admin Web Panel ("BrizoCast Admin")

## Overview

This plan turns the design into incremental, test-first code that integrates with the
already-built BrizoCast bot. It proceeds foundation-first: shared DB models and the
override-aware settings accessor, then the minimal non-invasive bot-side wiring (live overrides +
command-queue drain on the scheduler tick), then the editable surf-spot and regional-preset
stores, then the FastAPI admin package and its routes, and finally the Dockerized LAN-bound
deployment and end-to-end wiring. Every task references the requirements and/or correctness
properties it implements, and the final wiring step guarantees no orphaned code: each new module
ends up wired into either the admin app or the bot's scheduler.

Language: Python 3.12 (async SQLAlchemy 2.0, FastAPI, Jinja2/HTMX, Uvicorn) — matching the
existing codebase.

Property-based tests use Hypothesis (minimum 100 iterations each) and are tagged
`# Feature: admin-web-panel, Property {n}: {text}`. Test sub-tasks marked with `*` are optional.

## Tasks

- [x] 1. Shared DB models, schema bump, and override-aware settings accessor
  - [x] 1.1 Add new SQLAlchemy models and re-export them
    - Create `brizocast/models/config_override.py` (`ConfigOverride`: `key` PK, JSON `value`, `updated_at`)
    - Create `brizocast/models/admin_command.py` (`AdminCommandStatus` enum + `AdminCommand`: `id`, `type`, JSON `payload`, indexed `status`, `created_at`, `processed_at`)
    - Create `brizocast/models/scheduler_run.py` (`SchedulerRun`: single-row `id=1`, nullable `last_success_at`)
    - Import `Base` from `brizocast.models.base` and re-export all three from `brizocast/models/__init__.py` so `bootstrap_database` creates them
    - _Requirements: 11.2, 12.1; Design: Data Models_

  - [x] 1.2 Bump the schema version so old deployments recreate cleanly
    - Increment `SCHEMA_VERSION` to `2` in `brizocast/database/bootstrap.py`
    - _Requirements: 12.1; Design: Data Models (recreate-on-incompatible)_

  - [x] 1.3 Implement the override store and override-aware settings accessor
    - Create `brizocast/config/overrides.py` with `OVERRIDE_KEYS`, `ConfigOverrideStore` (async `get`/`set`/`all` over `config_overrides`, JSON values, `updated_at=now` on upsert)
    - Implement `OverrideAwareSettings` wrapping `Settings`: async `monetization_enabled()`, `plan_limits()`, `forecast_provider()` resolving override-first-else-`.env`, re-reading the store on every access; `__getattr__` pass-through for non-overridable fields
    - _Requirements: 6.4, 7.3, 12.2_

  - [ ]* 1.4 Write property test for override resolution
    - **Property 3: Override resolution is override-first, else `.env` default**
    - Run against a temp SQLite DB with the real store; assert a write between two reads changes the second read
    - **Validates: Requirements 6.4, 7.3, 12.2**

  - [x] 1.5 Implement DB-backed scheduler-run state
    - Create `brizocast/services/sqlite_scheduler_state.py` with `SqliteSchedulerState` implementing the existing `SchedulerRunReader`/`SchedulerRunRecorder` protocols by upserting/reading `scheduler_runs` row `id=1`
    - _Requirements: 11.2, 11.3_

  - [x] 1.6 Implement the config admin service (override writes + validation)
    - Create `brizocast/services/config_admin_service.py` to write/read the monetization flag, plan-limit map, and forecast-provider override via `ConfigOverrideStore`
    - Validate plan-limit max subscriptions ≥ 1 (reject without writing) and forecast provider ∈ forecast `_REGISTRY` keys (reject without writing)
    - _Requirements: 6.2, 6.3, 6.5, 7.2, 7.5, 12.1_

  - [ ]* 1.7 Write property test for config-override round trip
    - **Property 4: Config override persist-then-read round trip**
    - Include building a forecast provider from the resolved provider key and asserting it is the selected provider
    - **Validates: Requirements 6.2, 6.3, 7.2**

  - [ ]* 1.8 Write property test for plan-limit minimum rejection
    - **Property 10: Plan-limit minimum subscriptions below one is rejected**
    - Assert no override is written on rejection
    - **Validates: Requirements 6.5**

  - [ ]* 1.9 Write property test for unknown-provider rejection
    - **Property 11: Unknown forecast provider is rejected**
    - Assert no override is written on rejection
    - **Validates: Requirements 7.5**

- [ ] 2. Bot-side live overrides and command-queue drain
  - [x] 2.1 Implement the admin command enqueue side
    - Create `brizocast/services/admin_command_service.py` with `AdminCommandType` (`run_forecast_check`, `broadcast`) and `AdminCommandService.enqueue(type_, payload)` inserting a `pending` row and returning its id
    - _Requirements: 8.1, 9.1_

  - [ ] 2.2 Implement the command-queue drain with idempotency and isolation
    - Add `AdminCommandService.drain(handlers)` to claim each pending row oldest-first via a guarded `pending -> processing` UPDATE, invoke the handler, set `processed` + `processed_at` on success, set `failed` and log on error, and continue with the rest
    - _Requirements: 8.3, 9.3, 12.3, 12.4_

  - [ ]* 2.3 Write property test for enqueue
    - **Property 14: Triggering enqueues exactly one well-formed command**
    - Assert broadcast rows carry exactly the submitted text in payload
    - **Validates: Requirements 8.1, 9.1**

  - [ ]* 2.4 Write property test for idempotent draining
    - **Property 16: Draining processes pending commands exactly once (idempotent)**
    - Assert a second drain pass performs no further handler invocations for already-processed commands
    - **Validates: Requirements 8.3, 9.3, 12.3**

  - [ ]* 2.5 Write property test for per-command failure isolation
    - **Property 17: Command draining isolates per-command failures**
    - Assert a raising handler marks that row `failed` while every other pending command is still processed
    - **Validates: Requirements 12.4**

  - [ ] 2.6 Wire OverrideAwareSettings into the entitlement gate
    - Inject `OverrideAwareSettings` into `EntitlementService` and `await monetization_enabled()` / `plan_limits()` inside `assert_can_create_subscription`, `max_subscriptions_for`, `allowed_notification_modes`
    - Update `core/container.py` to build the entitlement service with the override-aware accessor
    - _Requirements: 6.4_

  - [ ] 2.7 Make the forecast provider resolve live per tick
    - Add a `ProviderSelector.current()` that builds the provider from `await overrides.forecast_provider()` (relying on `build_forecast_provider`'s safe fallback)
    - Wire it via `core/container.py` so the forecast-check job asks for the live provider at the start of each `run_once`
    - _Requirements: 7.3_

  - [ ] 2.8 Swap in DB-backed scheduler-run state
    - Replace `InMemorySchedulerState()` with `SqliteSchedulerState` in `brizocast/bot/app.py` and confirm the scheduler runner records success into it
    - _Requirements: 11.2, 11.3_

  - [ ] 2.9 Add the command-drain scheduler job and its handlers
    - In `brizocast/bot/app.py` register a dedicated `admin-command-drain` APScheduler job (`max_instances=1, coalesce=True`) that calls `AdminCommandService.drain`
    - Implement handlers: `run_forecast_check` → `ForecastCheckJob.run_once()`; `broadcast` → enumerate distinct `User.telegram_user_id` and deliver via the bot's `TelegramSender` (`RetryingNotificationSender.send_batch`)
    - _Requirements: 8.3, 9.3, 12.4_

  - [ ]* 2.10 Write integration test for the broadcast handler
    - Drive `broadcast` drain end-to-end against a fake `TelegramSender`, asserting the message reaches every enumerated user and the command is marked `processed`
    - _Requirements: 9.3_

  - [ ] 2.11 Checkpoint
    - Ensure all tests pass, ask the user if questions arise.

- [ ] 3. Editable surf-spot dataset and DB-backed regional presets
  - [ ] 3.1 Relocate the surf-spot dataset to the shared volume with freshness reload
    - Add `ensure_spot_dataset_seeded(path)` to copy the bundled JSON to `data/surf_spots.json` once if absent
    - Point `JsonSpotRepository` at the settings path and add an mtime check so `_load()` reloads when the file changes; update `_build_spot_repository` in `core/container.py` to pass the path
    - _Requirements: 4.1, 14.2; Design: Surf spots_

  - [ ] 3.2 Implement the surf-spot admin service (atomic CRUD + validation)
    - Create `brizocast/services/spot_admin_service.py` with create/edit/delete that write via temp file + `os.replace` under an `asyncio.Lock`
    - Validate latitude ∈ [-90, 90], longitude ∈ [-180, 180], and unique `spot_key`, rejecting without mutating the file
    - _Requirements: 4.2, 4.3, 4.4, 4.5, 4.6_

  - [ ]* 3.3 Write property test for spot dataset round trip
    - **Property 5: Surf-spot dataset write/read round trip**
    - Assert each create/edit/delete is reflected on reload and the file is always a complete, parseable JSON array
    - **Validates: Requirements 4.2, 4.3, 4.4**

  - [ ]* 3.4 Write property test for invalid coordinates
    - **Property 6: Invalid coordinates are rejected without mutation**
    - **Validates: Requirements 4.5**

  - [ ]* 3.5 Write property test for duplicate identifiers
    - **Property 7: Duplicate spot identifiers are rejected without mutation**
    - **Validates: Requirements 4.6**

  - [x] 3.6 Implement the regional-preset admin service (DB-backed)
    - Create `brizocast/services/preset_admin_service.py` to create/edit persisted default presets (`owner_user_id IS NULL`, `is_default=True`, `region=<name>`) via `SqlAlchemyPresetRepository`
    - Validate minimum wave height ≤ maximum wave height, rejecting without persisting
    - _Requirements: 5.2, 5.3, 5.4_

  - [ ]* 3.7 Write property test for inverted wave range
    - **Property 8: Inverted wave range is rejected**
    - **Validates: Requirements 5.4**

  - [ ]* 3.8 Write property test for regional-preset round trip
    - **Property 9: Regional preset persist-then-read round trip**
    - Assert the persisted default is visible to the bot's preset resolution for that region
    - **Validates: Requirements 5.2, 5.3**

- [ ] 4. FastAPI admin package foundation
  - [x] 4.1 Implement panel settings and fail-fast loader
    - Create `brizocast/admin/settings.py` with `PanelSettings` (ADMIN_USERNAME/PASSWORD, ADMIN_BIND_HOST default `127.0.0.1`, ADMIN_PORT, DATABASE_URL, SPOT_DATASET_PATH) and `load_panel_settings()` that logs the missing field name and raises `SystemExit(1)` when ADMIN_USERNAME/ADMIN_PASSWORD is absent
    - _Requirements: 13.1, 13.2, 13.3, 13.4_

  - [x] 4.2 Implement the Basic Auth dependency
    - Create `brizocast/admin/auth.py` with `require_admin` using `HTTPBasic(auto_error=False)`, constant-time compares of both username and password, returning 401 + `WWW-Authenticate: Basic` on missing/mismatched credentials and proceeding on a match
    - _Requirements: 1.2, 1.3, 1.4, 1.6_

  - [ ]* 4.3 Write property test for auth authorization
    - **Property 1: Auth authorizes iff credentials match**
    - **Validates: Requirements 1.3, 1.4**

  - [ ]* 4.4 Write property test for missing-credential challenge
    - **Property 2: Missing credentials are challenged**
    - **Validates: Requirements 1.2**

  - [x] 4.5 Implement panel DI providers and flash/CSRF helper
    - Create `brizocast/admin/dependencies.py` to build/cache the panel `Container` at the shared DB and expose `Depends` providers for the reused services
    - Create `brizocast/admin/flash.py` with the signed-cookie flash-message + per-session CSRF token helper and a write-guard dependency that rejects POST/DELETE with a mismatched token
    - _Requirements: 12.5, 13.4_

  - [ ] 4.6 Implement the app factory skeleton and uvicorn entrypoint
    - Create `brizocast/admin/app.py` `build_admin_app(panel)` applying `dependencies=[Depends(require_admin)]` app-wide, mounting `/static`, setting `app.state` (container, panel, overrides), and a startup hook calling `bootstrap_database` + `ensure_spot_dataset_seeded`; set `Cache-Control: no-store` and a self-only CSP
    - Create `brizocast/admin/__main__.py` running `uvicorn.run(...)` bound to `panel.ADMIN_BIND_HOST` on container port 8000
    - _Requirements: 1.1, 1.5, 13.4_

  - [x] 4.7 Create base templates and vendored static assets
    - Add `brizocast/admin/templates/base.html` + `_partials` for HTMX swaps and `brizocast/admin/static/` with vendored `htmx.min.js` and minimal CSS (no CDN)
    - _Requirements: 1.1; Design: Routes / Pages_

  - [ ]* 4.8 Write example tests for app-wide auth and startup validation
    - Assert every mounted route returns 401 without credentials (Req 1.1) and that a missing ADMIN_USERNAME/ADMIN_PASSWORD aborts startup naming the field (Req 13.3)
    - _Requirements: 1.1, 13.3_

- [ ] 5. Route and page groups
  - [ ] 5.1 Implement the users router and templates
    - Create `brizocast/admin/routers/users.py` + templates: list (telegram id, plan, sub count), detail (profile, plan, subscriptions; 404 when absent), and `POST /users/{id}/plan` change with confirmation, via `UserService`/plan repo
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 3.2_

  - [ ]* 5.2 Write property test for plan change persistence
    - **Property 13: Plan change persists**
    - **Validates: Requirements 2.3**

  - [ ]* 5.3 Write property test for user-detail subscription scoping
    - **Property 20: User detail lists exactly that user's subscriptions**
    - **Validates: Requirements 3.2**

  - [ ] 5.4 Implement the subscriptions router and template
    - Create `brizocast/admin/routers/subscriptions.py` + template listing owner, activity, location, radius, and notification mode
    - _Requirements: 3.1_

  - [ ] 5.5 Implement the surf-spot CRUD router and templates
    - Create `brizocast/admin/routers/spots.py` + templates with list, `POST` create, `POST /spots/{key}` edit, `DELETE /spots/{key}` over `spot_admin_service`, rendering validation messages via the flash helper
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6_

  - [ ] 5.6 Implement the regional-preset router and templates
    - Create `brizocast/admin/routers/presets.py` + templates with list, `POST` create, `POST /presets/{id}` edit over `preset_admin_service`
    - _Requirements: 5.1, 5.2, 5.3, 5.4_

  - [ ] 5.7 Implement the monetization router and templates
    - Create `brizocast/admin/routers/monetization.py` + templates showing the flag + plan limits, `POST /monetization/flag` and `POST /monetization/limits` persisting overrides via `config_admin_service`
    - _Requirements: 6.1, 6.2, 6.3, 6.5_

  - [ ] 5.8 Implement the forecast settings router, cache clear, and templates
    - Create `brizocast/admin/routers/forecast.py` + templates showing the current provider + available ids, `POST /forecast/provider` (persist override), and `POST /forecast/cache/clear`
    - Add a clear-all method to the forecast-cache repository if one is absent
    - _Requirements: 7.1, 7.2, 7.4, 7.5_

  - [ ]* 5.9 Write property test for cache clear
    - **Property 12: Clearing the cache empties the forecast cache**
    - **Validates: Requirements 7.4**

  - [ ] 5.10 Implement the commands router (run-check-now, broadcast) and templates
    - Create `brizocast/admin/routers/commands.py` + templates with `POST /commands/run-check` and `POST /commands/broadcast` (reject empty/whitespace text) enqueuing via `AdminCommandService`, each with an enqueued-confirmation
    - _Requirements: 8.1, 8.2, 9.1, 9.2, 9.4_

  - [ ]* 5.11 Write property test for empty-broadcast rejection
    - **Property 15: Empty broadcast text is rejected**
    - Assert no command is enqueued on rejection
    - **Validates: Requirements 9.4**

  - [ ] 5.12 Implement the feedback router and template
    - Create `brizocast/admin/routers/feedback.py` + template listing each entry (owner, spot, score, value, timestamp) plus up/down counts via `FeedbackService`
    - _Requirements: 10.1, 10.2_

  - [ ]* 5.13 Write property test for feedback counts
    - **Property 18: Feedback counts equal actual feedback**
    - **Validates: Requirements 10.2**

  - [ ] 5.14 Implement the stats/health router, dashboard, and templates
    - Create `brizocast/admin/routers/stats.py` + templates for `/` and `/stats`: total users, per-tier counts, total subscriptions, total spots, and last scheduler run from `SqliteSchedulerState` (showing "never" when none)
    - _Requirements: 11.1, 11.2, 11.3_

  - [ ]* 5.15 Write property test for stats totals
    - **Property 19: Stats totals equal actual counts**
    - Assert per-tier counts sum to total users
    - **Validates: Requirements 11.1**

- [x] 6. Dockerized separate-service LAN-bound deployment
  - [x] 6.1 Add the admin Docker Compose service
    - Add an `admin` service to `docker-compose.yml` using the same image, `command: ["python", "-m", "brizocast.admin"]`, mounting the same `./data` volume, publishing `"${ADMIN_BIND_HOST}:${ADMIN_PORT}:8000"`, `depends_on: brizocast`
    - _Requirements: 14.1, 14.2, 14.3, 14.4, 1.5_

  - [x] 6.2 Add panel configuration to env files
    - Add ADMIN_USERNAME, ADMIN_PASSWORD, ADMIN_BIND_HOST, ADMIN_PORT (and optional SPOT_DATASET_PATH) to `.env` and `.env.example` with LAN-only guidance (never `0.0.0.0`)
    - _Requirements: 13.2, 1.5, 14.3_

  - [ ]* 6.3 Write smoke test for compose/config wiring
    - Assert the compose `admin` service binds to `${ADMIN_BIND_HOST}` (not all-interfaces) and shares the `./data` volume, and that `load_panel_settings` reads the credential + bind config
    - _Requirements: 14.1, 14.2, 14.3, 1.5, 13.2_

- [ ] 7. Final wiring and checkpoint
  - [ ] 7.1 Register all routers into the app factory
    - Edit `build_admin_app` to include every router (users, subscriptions, spots, presets, monetization, forecast, commands, feedback, stats) in the include loop and finalize `__main__`, so all routes are reachable under the app-wide auth dependency
    - _Requirements: 1.1; Design: App factory_

  - [ ] 7.2 Final checkpoint
    - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional test sub-tasks and can be skipped for a faster MVP.
- Property tests use Hypothesis with a minimum of 100 iterations each and are tagged
  `# Feature: admin-web-panel, Property {n}: {text}`.
- The cross-process accessor, queue, spot, preset, and stats properties run against an
  in-memory/temp SQLite database with the real repositories; the broadcast handler uses a fake
  `TelegramSender`.
- Per the design's PBT guidance, app-wide auth wiring (1.1), LAN bind/config loading (1.5, 1.6,
  13.*), and Docker compose layout (14.*) are verified by example/smoke tests rather than
  property tests.
- The bot-side changes are deliberately non-invasive: override accessor at three read sites, a
  dedicated drain job, the scheduler-state swap, and the spot-dataset relocation.
- The final wiring task ensures no orphaned code — every new module is reachable from either the
  admin app or the bot's scheduler.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "1.3", "4.1", "4.2", "4.7", "6.1", "6.2"] },
    { "id": 1, "tasks": ["1.2", "1.5", "1.6", "2.1", "3.6", "4.3", "4.4", "4.5"] },
    { "id": 2, "tasks": ["1.4", "1.7", "1.8", "1.9", "2.2", "2.6", "3.7", "3.8", "4.6"] },
    { "id": 3, "tasks": ["2.3", "2.4", "2.5", "2.7", "2.8", "4.8", "5.1", "5.4", "5.6", "5.7", "5.8", "5.10", "5.12", "5.14"] },
    { "id": 4, "tasks": ["2.9", "3.1", "5.2", "5.3", "5.9", "5.11", "5.13", "5.15"] },
    { "id": 5, "tasks": ["2.10", "3.2"] },
    { "id": 6, "tasks": ["3.3", "3.4", "3.5", "5.5"] },
    { "id": 7, "tasks": ["7.1"] },
    { "id": 8, "tasks": ["6.3"] }
  ]
}
```
