# Requirements Document

## Introduction

The Admin Web Panel ("BrizoCast Admin") is a simple but functional v1 web administration interface for the existing BrizoCast Telegram surf-alert bot. It gives the single operator a server-rendered web UI to inspect bot data (users, subscriptions, feedback, stats) and to perform read-write administrative actions (managing user plans, broadcasting announcements, triggering forecast checks, editing surf spots and regional default presets, toggling monetization, editing plan limits, switching the forecast provider, and clearing the forecast cache).

The Admin Web Panel runs as a separate Docker Compose service in its own container, distinct from the bot process. It shares the same `./data` volume as the bot, reading and writing the same SQLite database (`sqlite+aiosqlite:///data/brizocast.db`) and the same surf-spots JSON dataset. It reuses the existing service and repository layer (Clean Architecture) through its own dependency-injection container instance pointed at the shared database and volume, rather than duplicating business logic, and it holds no shared in-memory state with the bot process.

Because the panel runs in a separate process with no inter-process communication channel to the bot and no Telegram client of its own by default, two categories of action are mediated through the shared database. Runtime configuration overrides (the monetization flag, plan limits, and forecast-provider selection) are persisted as Config Overrides that the bot and providers read at runtime through an override-aware settings accessor, so changes apply live without restarting the bot. Cross-process actions that require the bot (run-forecast-check-now and broadcast announcement) are enqueued as Admin Commands in a database-backed command queue that the bot drains on each scheduler tick.

Security relies on two layers. The panel is bound to the Raspberry Pi's local-area-network address at the host and Docker Compose level so that it is never published on a public, internet-facing interface. In addition, every route is protected by HTTP Basic Authentication using a single administrator credential supplied through configuration. The panel is treated as a trusted-LAN service that is nonetheless always authenticated.

This document defines the requirements for authentication and LAN-only access, the read views (users, subscriptions, feedback, stats and health), the read-write administrative actions (plan management, broadcast, run-forecast-check-now, surf-spot management, regional default-preset management, monetization toggle and plan-limit editing, forecast-provider switching and forecast-cache clearing), the cross-process live-configuration and command mechanism, the panel's configuration, and its Dockerized separate-service LAN-bound deployment.

## Glossary

- **Admin_Panel**: The server-rendered web administration application for BrizoCast, running as a separate Docker container that reuses the bot's service and repository layer through its own dependency-injection container instance.
- **Bot**: The existing BrizoCast Telegram bot process that handles user interactions, runs the Scheduler, and drains Admin Commands. The Admin_Panel and Bot are separate processes sharing the same database and `./data` volume.
- **Administrator**: The single operator who authenticates to and operates the Admin_Panel.
- **Admin_Credential**: The single administrator username and password used to authenticate to the Admin_Panel, supplied through Panel_Configuration as ADMIN_USERNAME and ADMIN_PASSWORD.
- **Basic_Auth**: HTTP Basic Authentication enforced by the Admin_Panel on every route using the Admin_Credential.
- **LAN_Bind_Address**: The Raspberry Pi local-area-network host address and port to which the Admin_Panel is bound, configured so the panel is reachable only on the local network and never on a public interface.
- **Shared_Database**: The SQLite database at `sqlite+aiosqlite:///data/brizocast.db` on the shared `./data` volume, read and written by both the Bot and the Admin_Panel.
- **Shared_Volume**: The `./data` Docker volume that holds the Shared_Database and the Surf_Spot_Dataset, mounted into both the Bot container and the Admin_Panel container.
- **User**: A BrizoCast end user identified by a Telegram user identifier, as stored in the Shared_Database.
- **Plan**: A User's monetization tier, one of Free or Paid, as defined by the existing bot data model.
- **Subscription**: A User-owned surf monitoring configuration stored in the Shared_Database.
- **Surf_Spot**: A surf location entry stored in the Surf_Spot_Dataset.
- **Surf_Spot_Dataset**: The JSON dataset of Surf_Spots on the Shared_Volume.
- **Regional_Preset**: A regional default preset (a named set of surf condition parameters provided per region) that the Administrator can view and edit.
- **Feedback**: A User's thumbs-up or thumbs-down response to an alert, stored in the Shared_Database.
- **Monetization_Flag**: The runtime-overridable feature flag (corresponding to the bot's MONETIZATION_ENABLED setting) that enables Plan-based gating and quotas.
- **Plan_Limit**: The configured quota values for a Plan tier, including maximum number of Subscriptions and the set of available notification modes.
- **Forecast_Provider_Selection**: The runtime-overridable identifier of the active forecast provider used by the Bot.
- **Forecast_Cache**: The cached-forecast store in the Shared_Database keyed per Surf_Spot.
- **Config_Override**: A configuration value persisted by the Admin_Panel in the Shared_Database that overrides the corresponding `.env`-sourced setting, covering the Monetization_Flag, Plan_Limits, and Forecast_Provider_Selection.
- **Override_Aware_Settings_Accessor**: The settings accessor that resolves a configuration value by returning a persisted Config_Override when one exists and otherwise the `.env`-sourced default, read at runtime by the Bot and forecast providers.
- **Admin_Command**: A record enqueued by the Admin_Panel in the Command_Queue requesting a cross-process action from the Bot, specifically run-forecast-check-now or broadcast announcement.
- **Command_Queue**: The database-backed table of Admin_Commands that the Bot drains on each Scheduler tick.
- **Scheduler**: The Bot's APScheduler-based component that runs periodic forecast-check jobs and, on each tick, drains the Command_Queue.
- **Broadcast_Announcement**: An Administrator-authored message to be delivered by the Bot to BrizoCast Users.
- **Scheduler_Run_Time**: The timestamp of the most recent successful forecast-check job recorded in the Shared_Database.
- **Panel_Configuration**: The Admin_Panel settings loaded from a `.env` file, including the Admin_Credential, the LAN_Bind_Address host, and the bind port.

## Requirements

### Requirement 1: Authentication and LAN-Only Access

**User Story:** As the Administrator, I want every page of the panel protected by a single login and reachable only on my local network, so that bot administration is not exposed to the public internet.

#### Acceptance Criteria

1. THE Admin_Panel SHALL require Basic_Auth on every route.
2. WHEN a request is received without valid Basic_Auth credentials, THE Admin_Panel SHALL respond with an HTTP 401 Unauthorized status and SHALL request authentication.
3. WHEN a request is received with Basic_Auth credentials that do not match the Admin_Credential, THE Admin_Panel SHALL respond with an HTTP 401 Unauthorized status.
4. WHEN a request is received with Basic_Auth credentials that match the Admin_Credential, THE Admin_Panel SHALL process the requested route.
5. THE Admin_Panel SHALL bind its listening socket to the LAN_Bind_Address rather than to a publicly published interface.
6. THE Admin_Panel SHALL read the Admin_Credential from Panel_Configuration.

### Requirement 2: Users and Plan Management

**User Story:** As the Administrator, I want to view users and change their plan, so that I can grant or revoke paid access manually.

#### Acceptance Criteria

1. WHEN the Administrator opens the users view, THE Admin_Panel SHALL list each User with the User's Telegram user identifier, current Plan, and Subscription count.
2. WHEN the Administrator opens a User detail view, THE Admin_Panel SHALL display the selected User's profile fields, Plan, and Subscriptions.
3. WHEN the Administrator changes a User's Plan to Free or Paid, THE Admin_Panel SHALL persist the selected Plan for that User in the Shared_Database.
4. WHEN the Administrator changes a User's Plan, THE Admin_Panel SHALL confirm the updated Plan to the Administrator.
5. IF the Administrator opens a User detail view for an identifier that does not exist, THEN THE Admin_Panel SHALL respond with an HTTP 404 Not Found status.

### Requirement 3: Subscriptions View

**User Story:** As the Administrator, I want to view subscriptions, so that I can see what users are monitoring.

#### Acceptance Criteria

1. WHEN the Administrator opens the subscriptions view, THE Admin_Panel SHALL list each Subscription with its owning User identifier, Activity, Location, search radius, and notification mode.
2. WHERE the Administrator views a single User's detail, THE Admin_Panel SHALL list that User's Subscriptions.

### Requirement 4: Surf Spot Management

**User Story:** As the Administrator, I want to view and edit the surf spots, so that I can correct or extend the spot dataset without editing files by hand.

#### Acceptance Criteria

1. WHEN the Administrator opens the surf spots view, THE Admin_Panel SHALL list each Surf_Spot in the Surf_Spot_Dataset with its identifier, name, latitude, longitude, country, and region.
2. WHEN the Administrator creates a Surf_Spot with an identifier, name, latitude, longitude, country, and region, THE Admin_Panel SHALL persist the new Surf_Spot to the Surf_Spot_Dataset.
3. WHEN the Administrator edits an existing Surf_Spot's fields, THE Admin_Panel SHALL persist the updated Surf_Spot to the Surf_Spot_Dataset.
4. WHEN the Administrator deletes a Surf_Spot, THE Admin_Panel SHALL remove the selected Surf_Spot from the Surf_Spot_Dataset.
5. IF the Administrator submits a Surf_Spot with a latitude outside the range -90 to 90 inclusive or a longitude outside the range -180 to 180 inclusive, THEN THE Admin_Panel SHALL reject the submission and SHALL report the invalid coordinate to the Administrator.
6. IF the Administrator submits a Surf_Spot with an identifier that already exists in the Surf_Spot_Dataset, THEN THE Admin_Panel SHALL reject the submission and SHALL report the duplicate identifier to the Administrator.

### Requirement 5: Regional Default Preset Management

**User Story:** As the Administrator, I want to view and edit regional default presets, so that I can tune the default surf conditions offered per region.

#### Acceptance Criteria

1. WHEN the Administrator opens the regional presets view, THE Admin_Panel SHALL list each Regional_Preset with its region and surf condition parameters.
2. WHEN the Administrator edits a Regional_Preset's surf condition parameters, THE Admin_Panel SHALL persist the updated Regional_Preset.
3. WHEN the Administrator creates a Regional_Preset for a region with surf condition parameters, THE Admin_Panel SHALL persist the new Regional_Preset.
4. IF the Administrator submits a Regional_Preset with a minimum wave height greater than the maximum wave height, THEN THE Admin_Panel SHALL reject the submission and SHALL report the invalid range to the Administrator.

### Requirement 6: Monetization Toggle and Plan-Limit Editing with Live Apply

**User Story:** As the Administrator, I want to toggle monetization and edit plan limits and have the change take effect immediately, so that I can adjust gating without restarting the bot.

#### Acceptance Criteria

1. WHEN the Administrator opens the monetization view, THE Admin_Panel SHALL display the current Monetization_Flag value and the current Plan_Limit values for each Plan tier.
2. WHEN the Administrator sets the Monetization_Flag to enabled or disabled, THE Admin_Panel SHALL persist the value as a Config_Override in the Shared_Database.
3. WHEN the Administrator edits a Plan_Limit's maximum number of Subscriptions or set of available notification modes, THE Admin_Panel SHALL persist the updated Plan_Limit as a Config_Override in the Shared_Database.
4. WHEN the Bot reads the Monetization_Flag or a Plan_Limit through the Override_Aware_Settings_Accessor after the Administrator persists a Config_Override, THE Bot SHALL use the persisted Config_Override value without requiring a restart.
5. IF the Administrator submits a Plan_Limit with a maximum number of Subscriptions less than 1, THEN THE Admin_Panel SHALL reject the submission and SHALL report the invalid value to the Administrator.

### Requirement 7: Forecast Provider Switch and Cache Clear with Live Apply

**User Story:** As the Administrator, I want to switch the forecast provider and clear the forecast cache and have the change take effect immediately, so that I can change data sources without restarting the bot.

#### Acceptance Criteria

1. WHEN the Administrator opens the forecast settings view, THE Admin_Panel SHALL display the current Forecast_Provider_Selection and the available forecast provider identifiers.
2. WHEN the Administrator selects a Forecast_Provider_Selection from the available identifiers, THE Admin_Panel SHALL persist the selection as a Config_Override in the Shared_Database.
3. WHEN the Bot reads the Forecast_Provider_Selection through the Override_Aware_Settings_Accessor after the Administrator persists a Config_Override, THE Bot SHALL use the persisted Forecast_Provider_Selection without requiring a restart.
4. WHEN the Administrator triggers a clear of the Forecast_Cache, THE Admin_Panel SHALL remove all cached Forecasts from the Forecast_Cache in the Shared_Database.
5. IF the Administrator selects a forecast provider identifier that is not among the available identifiers, THEN THE Admin_Panel SHALL reject the selection and SHALL report the invalid identifier to the Administrator.

### Requirement 8: Run Forecast Check Now

**User Story:** As the Administrator, I want to trigger a forecast check on demand, so that I can verify the bot end-to-end without waiting for the next scheduled run.

#### Acceptance Criteria

1. WHEN the Administrator triggers run-forecast-check-now, THE Admin_Panel SHALL enqueue a run-forecast-check Admin_Command in the Command_Queue.
2. WHEN the Administrator triggers run-forecast-check-now, THE Admin_Panel SHALL confirm to the Administrator that the request has been enqueued.
3. WHEN the Bot drains the Command_Queue on a Scheduler tick and finds a run-forecast-check Admin_Command, THE Bot SHALL run a forecast-check job and SHALL mark the Admin_Command as processed.

### Requirement 9: Broadcast Announcements

**User Story:** As the Administrator, I want to broadcast an announcement to users, so that I can notify everyone of important information through the bot.

#### Acceptance Criteria

1. WHEN the Administrator submits a Broadcast_Announcement with message text, THE Admin_Panel SHALL enqueue a broadcast Admin_Command containing the message text in the Command_Queue.
2. WHEN the Administrator submits a Broadcast_Announcement, THE Admin_Panel SHALL confirm to the Administrator that the announcement has been enqueued.
3. WHEN the Bot drains the Command_Queue on a Scheduler tick and finds a broadcast Admin_Command, THE Bot SHALL deliver the message text to BrizoCast Users and SHALL mark the Admin_Command as processed.
4. IF the Administrator submits a Broadcast_Announcement with empty message text, THEN THE Admin_Panel SHALL reject the submission and SHALL request non-empty message text.

### Requirement 10: Feedback View

**User Story:** As the Administrator, I want to view user feedback on alerts, so that I can gauge alert quality.

#### Acceptance Criteria

1. WHEN the Administrator opens the feedback view, THE Admin_Panel SHALL list each Feedback entry with its owning User identifier, Surf_Spot, surf score, thumbs-up or thumbs-down value, and timestamp.
2. THE Admin_Panel SHALL display the count of thumbs-up Feedback entries and the count of thumbs-down Feedback entries.

### Requirement 11: Stats and Health

**User Story:** As the Administrator, I want a stats overview, so that I can see the bot's health and activity at a glance.

#### Acceptance Criteria

1. WHEN the Administrator opens the stats view, THE Admin_Panel SHALL display the total number of Users, the number of Users on each Plan tier, the total number of Subscriptions, and the total number of Surf_Spots.
2. WHEN the Administrator opens the stats view, THE Admin_Panel SHALL display the Scheduler_Run_Time of the most recent successful forecast-check job.
3. WHERE no successful forecast-check job has been recorded, THE Admin_Panel SHALL indicate that no Scheduler run has been recorded.

### Requirement 12: Cross-Process Live-Config and Command Mechanism

**User Story:** As the Administrator, I want the panel and bot to coordinate through the shared database, so that configuration changes and triggered actions reach the bot without inter-process communication or a panel-side Telegram client.

#### Acceptance Criteria

1. THE Admin_Panel SHALL persist Config_Overrides and enqueue Admin_Commands in the Shared_Database rather than communicating directly with the Bot process.
2. THE Override_Aware_Settings_Accessor SHALL resolve a configuration value by returning the persisted Config_Override when one exists and otherwise the `.env`-sourced default value.
3. WHEN the Bot completes processing of an Admin_Command, THE Bot SHALL mark the Admin_Command as processed so that the Admin_Command is not processed again.
4. IF processing an Admin_Command raises an error, THEN THE Bot SHALL log the error and SHALL continue draining the remaining Admin_Commands in the Command_Queue.
5. THE Admin_Panel and the Bot SHALL access the Shared_Database through the existing repository layer rather than maintaining shared in-memory state.

### Requirement 13: Panel Configuration

**User Story:** As the Administrator, I want to configure the panel through a .env file, so that I can set credentials and the bind address without changing code.

#### Acceptance Criteria

1. THE Admin_Panel SHALL load Panel_Configuration from a `.env` file at startup.
2. THE Panel_Configuration SHALL include the Admin_Credential username (ADMIN_USERNAME), the Admin_Credential password (ADMIN_PASSWORD), the LAN_Bind_Address host, and the bind port.
3. WHEN the Admin_Panel starts and the ADMIN_USERNAME or ADMIN_PASSWORD value is missing, THE Admin_Panel SHALL terminate startup and SHALL log a message identifying the missing Panel_Configuration value.
4. THE Admin_Panel SHALL point its dependency-injection container instance at the Shared_Database and Shared_Volume.

### Requirement 14: Dockerized Separate-Service LAN-Bound Deployment

**User Story:** As the Administrator, I want the panel to run as its own Docker Compose service bound to my LAN, so that it deploys alongside the bot while staying off the public internet.

#### Acceptance Criteria

1. THE Admin_Panel SHALL run as a Docker Compose service in a container separate from the Bot container.
2. THE Admin_Panel container SHALL mount the same Shared_Volume that the Bot container mounts.
3. THE Admin_Panel service SHALL publish its port bound to the LAN_Bind_Address host rather than to a public, all-interfaces address.
4. THE Admin_Panel SHALL operate as a process separate from the Bot, with no shared in-memory state between the two.
