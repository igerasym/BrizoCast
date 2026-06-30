# Requirements Document

## Introduction

The Outdoor Conditions Alert Bot ("BrizoCast") is a production-ready Telegram bot that monitors weather and ocean conditions for outdoor sports and sends smart, low-noise notifications when conditions are favorable. The system is architected from day one to support multiple activities (Surf, Snowboard, Kiteboarding, Wingfoil, Hiking, Climbing), but the MVP implements only the Surf activity.

Users onboard by choosing an activity, setting one or more locations, and creating subscriptions. Each subscription binds an activity, a location, a search radius, a preset or custom conditions, and notification preferences. A background scheduler periodically discovers nearby surf spots within each subscription's radius, collects forecasts from a pluggable forecast provider, computes a 0-100 surf score via an isolated scoring engine, and sends notifications subject to anti-spam, digest, quiet-hours, and feedback rules.

The system is built with Python 3.12+, python-telegram-bot, SQLAlchemy over SQLite, APScheduler, and Pydantic, following Clean Architecture, SOLID, the repository pattern, a service layer, and dependency injection. It runs in Docker / Docker Compose and is compatible with Raspberry Pi. For the MVP, Open-Meteo Marine is the default free forecast provider (no API key) and Open-Meteo Geocoding is the default geocoding provider.

The system also supports optional AI-assisted preset generation. Following the same abstraction philosophy as the Forecast_Provider and Geocoding_Provider, an AI_Provider interface lets the AI backend be swapped without changing business logic, with Google Gemini as the default implementation. AI usage is optional and configurable; when no AI provider is configured, the system falls back to bundled static Default_Presets and continues functioning.

The system is additionally designed to accommodate future monetization. The product is free in the MVP, but the data model and gating logic for paid membership tiers exist from day one so that a paid plan, quotas, and billing can be enabled later through configuration without redesigning existing features. To avoid terminology collision, the monetization concept is named "Plan" (or "Membership") and is distinct from the surf "Subscription" that defines activity and location monitoring.

This document defines the requirements for onboarding, location management, subscriptions, presets and custom conditions, surf spot discovery, forecast provider abstraction, AI-assisted preset generation, surf scoring, the notification engine (anti-spam, digests, quiet hours, feedback), bot commands and conversational UX, background scheduling, configuration, persistence, multi-sport extensibility, and user plans with monetization-ready entitlements.

## Glossary

- **Bot**: The Telegram-facing component that handles commands, inline keyboards, and conversational flows.
- **System**: The overall Outdoor Conditions Alert Bot application, including the Bot, services, scheduler, and persistence.
- **User**: A Telegram user identified by a unique Telegram user identifier who interacts with the Bot.
- **Activity**: A supported outdoor sport (e.g., Surf). The MVP implements Surf only.
- **Location**: A named geographic point owned by a User, defined by latitude and longitude, optionally with a label, city, and country.
- **Favorite_Location**: A Location saved by a User for reuse across subscriptions.
- **Subscription**: A User-owned configuration binding an Activity, a Location, a search radius, a Preset or Custom_Conditions, and Notification_Preferences.
- **Search_Radius**: The distance in kilometers from a Location within which Surf_Spots are discovered. Default is 30 km.
- **Surf_Spot**: A discoverable surf location with identifier, name, latitude, longitude, country, and region.
- **Spot_Repository**: The component that provides Surf_Spots, initially backed by a local JSON dataset and replaceable by a database.
- **Preset**: A named, reusable set of surf condition parameters. May be a Default_Preset provided per region or a custom Preset created by a User.
- **Default_Preset**: A Preset provided by the System for a region.
- **Custom_Conditions**: User-defined condition overrides for a Subscription, including minimum wave height, maximum wave height, minimum period, maximum wind, acceptable wind direction, acceptable swell direction, optional tide preference, and daylight-only flag.
- **Forecast_Provider**: An abstract interface for retrieving forecasts for a geographic point, with interchangeable implementations (Open-Meteo Marine, Stormglass, Surf-Forecast, Windy).
- **Geocoding_Provider**: An abstract interface for resolving place and city names to geographic coordinates, defaulting to Open-Meteo Geocoding.
- **Forecast**: A time-series of predicted conditions for a Surf_Spot, including wave height, swell period, swell direction, wind speed, wind direction, and timestamps.
- **Forecast_Cache**: A time-to-live cache of Forecasts keyed per Surf_Spot and shared across Subscriptions.
- **Surf_Score**: An integer from 0 to 100 produced by the Scoring_Engine that rates Forecast conditions for surfing.
- **Score_Category**: A label derived from a Surf_Score: Perfect (95-100), Excellent (85-94), Good (70-84), Rideable (50-69), Ignore (below 50).
- **Scoring_Engine**: The isolated component that computes a Surf_Score from a Forecast and a Subscription's Preset or Custom_Conditions.
- **Scorer**: An activity-specific implementation used by the Scoring_Engine; the MVP provides the Surf Scorer.
- **Notification_Engine**: The component that decides whether and how to notify a User and dispatches messages.
- **Notification_Preferences**: Per-Subscription settings controlling notification mode, quiet hours, and mute/snooze state.
- **Notification_Mode**: One of immediate alert, morning digest, evening digest, or weekly best day.
- **Notification_Record**: A persisted record of a sent notification, including Subscription, Surf_Spot, Surf_Score, forecast window, and timestamp.
- **Quiet_Hours**: A daily time window during which the System suppresses immediate alerts for a Subscription.
- **Significant_Improvement**: An increase in Surf_Score beyond a configured threshold relative to the most recent Notification_Record for the same Subscription, Surf_Spot, and forecast window.
- **Feedback**: A User's thumbs-up or thumbs-down response to an alert, persisted for preset tuning and future scoring data.
- **Scheduler**: The APScheduler-based component that runs periodic forecast-check jobs.
- **Scheduler_Interval**: The configured period between Scheduler runs.
- **Configuration**: Application settings loaded from a .env file and validated via Pydantic.
- **AI_Provider**: An abstract interface for AI-assisted features, with interchangeable implementations, used in the MVP to generate Default_Presets for a region and structured for future AI features such as interpreting natural-language condition descriptions into Custom_Conditions. Also referred to as the Preset_Generation_Provider for the preset-generation capability.
- **Gemini**: The Google Gemini implementation of the AI_Provider, used as the default AI backend when AI is enabled.
- **AI_Generated_Preset**: A Default_Preset produced by the AI_Provider for a region, sharing the same surf preset parameter shape as a static Default_Preset so that the two are interchangeable.
- **Plan**: A monetization tier associated with a User that determines entitlements and quotas. The MVP supports a Free Plan and defines a Paid Plan for future activation. Also referred to as Membership. The Plan is distinct from a Subscription.
- **Plan_Tier**: The type of a Plan, one of Free or Paid.
- **Plan_Status**: The lifecycle state of a User's Plan, including active, expired, and canceled.
- **Entitlement**: A capability or quota granted to a User by the User's Plan, such as the maximum number of Subscriptions or the set of available Notification_Modes.
- **Plan_Limits**: The configured quota values associated with each Plan_Tier, such as the maximum number of Subscriptions.
- **Payment_Record**: A persisted record of a billing transaction associated with a User's Plan, reserved for future payment integration and not populated in the MVP.
- **Monetization_Enabled**: A Configuration feature flag that enables Plan-based gating and quotas. WHILE disabled, the System treats every User as entitled to full functionality.

## Requirements

### Requirement 1: Onboarding and Activity Selection

**User Story:** As a new User, I want a guided onboarding flow that lets me choose an activity, so that I can start receiving relevant condition alerts.

#### Acceptance Criteria

1. WHEN a User sends the /start command, THE Bot SHALL present an onboarding flow that requests an Activity selection using an inline keyboard.
2. THE Bot SHALL offer Surf as a selectable Activity.
3. WHERE an Activity other than Surf is displayed, THE Bot SHALL mark that Activity as unavailable in the MVP.
4. WHEN a User selects an unavailable Activity, THE Bot SHALL inform the User that the Activity is not yet supported and SHALL keep the User in the Activity selection step.
5. WHEN a User selects Surf, THE Bot SHALL advance the onboarding flow to the location setup step.
6. IF a User who has already completed onboarding sends /start, THEN THE Bot SHALL present the main menu instead of repeating Activity selection.
7. WHEN a User first interacts with the Bot, THE System SHALL create a User record keyed by the Telegram user identifier.

### Requirement 2: Location Management

**User Story:** As a User, I want to set and save multiple locations by sharing my position or searching by city or place, so that I can create subscriptions for the areas where I do my sport.

#### Acceptance Criteria

1. WHEN a User sends the /location command, THE Bot SHALL present options to share a Telegram location, search by city, search by place name, and view saved Favorite_Locations.
2. WHEN a User shares a Telegram location, THE System SHALL create a Location from the provided latitude and longitude.
3. WHEN a User submits a city or place name for search, THE System SHALL request candidate coordinates from the Geocoding_Provider.
4. WHEN the Geocoding_Provider returns one or more candidates, THE Bot SHALL present the candidates for the User to select using an inline keyboard.
5. WHEN a User selects a geocoding candidate, THE System SHALL create a Location from the selected candidate's latitude, longitude, city, and country.
6. IF the Geocoding_Provider returns no candidates for a search term, THEN THE Bot SHALL inform the User that no matching place was found and SHALL request a new search term.
7. WHEN a User chooses to save a Location, THE System SHALL persist the Location as a Favorite_Location associated with the User.
8. THE System SHALL allow a User to save more than one Favorite_Location.
9. WHEN a User requests to view saved Favorite_Locations, THE Bot SHALL list each Favorite_Location with its label and place name.
10. WHEN a User requests to delete a Favorite_Location, THE System SHALL remove the selected Favorite_Location from that User's saved locations.
11. IF the Geocoding_Provider request fails, THEN THE System SHALL inform the User that the location search is temporarily unavailable and SHALL log the failure.

### Requirement 3: Subscription Management

**User Story:** As a User, I want to create and manage multiple subscriptions that each combine an activity, location, radius, conditions, and notification preferences, so that I can monitor different spots with different criteria.

#### Acceptance Criteria

1. WHEN a User sends the /add command, THE Bot SHALL start a subscription creation flow that collects an Activity, a Location, a Search_Radius, a Preset or Custom_Conditions, and Notification_Preferences.
2. WHERE a User does not specify a Search_Radius during subscription creation, THE System SHALL set the Search_Radius to 30 km.
3. THE System SHALL allow a User to own more than one Subscription.
4. WHEN a User completes the subscription creation flow, THE System SHALL persist a Subscription associated with the User and SHALL confirm creation to the User.
5. WHEN a User sends the /subscriptions command, THE Bot SHALL list each of the User's Subscriptions with its Activity, Location, Search_Radius, and Notification_Mode.
6. WHEN a User sends the /remove command and selects a Subscription, THE System SHALL delete the selected Subscription and SHALL confirm deletion to the User.
7. WHEN a User edits a Subscription's Search_Radius, THE System SHALL persist the updated Search_Radius for that Subscription.
8. IF a User attempts to create a Subscription without a selected Location, THEN THE Bot SHALL prompt the User to set a Location before completing the Subscription.
9. THE System SHALL accept a Search_Radius between 1 km and 200 km inclusive.
10. IF a User submits a Search_Radius outside the accepted range, THEN THE Bot SHALL reject the value and SHALL request a Search_Radius within the accepted range.

### Requirement 4: Presets and Custom Conditions

**User Story:** As a User, I want to use a region's default preset or define my own custom conditions, so that alerts match the conditions I prefer for surfing.

#### Acceptance Criteria

1. THE System SHALL provide one or more Default_Presets for each supported region.
2. THE System SHALL define each surf Preset with minimum wave height, maximum wave height, minimum period, maximum wind, preferred wind direction, and preferred swell direction.
3. WHEN a User sends the /presets command, THE Bot SHALL list the available Default_Presets and the User's custom Presets.
4. WHEN a User selects a Default_Preset for a Subscription, THE System SHALL associate the selected Default_Preset with the Subscription.
5. WHEN a User chooses to create Custom_Conditions, THE Bot SHALL collect minimum wave height, maximum wave height, minimum period, maximum wind, acceptable wind direction, acceptable swell direction, an optional tide preference, and a daylight-only flag.
6. WHEN a User completes Custom_Conditions entry, THE System SHALL persist the Custom_Conditions and SHALL associate the Custom_Conditions with the Subscription.
7. WHERE a Subscription has Custom_Conditions, THE Scoring_Engine SHALL evaluate Forecasts using the Custom_Conditions instead of any Preset.
8. IF a User submits a minimum wave height greater than the maximum wave height, THEN THE Bot SHALL reject the values and SHALL request corrected values.
9. WHERE a User has not selected a Default_Preset and has not provided Custom_Conditions for a Subscription, THE System SHALL apply the region's first Default_Preset for the Subscription's Location.

### Requirement 5: Surf Spot Discovery

**User Story:** As a User, I want the System to automatically find surf spots near my location within my chosen radius, so that I receive alerts for relevant spots without listing them manually.

#### Acceptance Criteria

1. THE Spot_Repository SHALL provide Surf_Spots, with each Surf_Spot defined by an identifier, name, latitude, longitude, country, and region.
2. THE Spot_Repository SHALL load Surf_Spots from a local JSON dataset in the MVP.
3. WHEN the System discovers spots for a Subscription, THE System SHALL return the Surf_Spots whose distance from the Subscription's Location is less than or equal to the Subscription's Search_Radius.
4. THE System SHALL compute distance between a Location and a Surf_Spot using their latitude and longitude coordinates.
5. IF no Surf_Spot is within a Subscription's Search_Radius, THEN THE System SHALL record that the Subscription has no nearby spots and SHALL skip forecast collection for that Subscription.
6. THE System SHALL expose the Spot_Repository through an interface that allows replacing the JSON dataset with a database without changing the spot discovery logic.

### Requirement 6: Forecast Provider Abstraction

**User Story:** As a developer, I want forecast retrieval behind a provider abstraction, so that I can swap forecast sources without changing business logic.

#### Acceptance Criteria

1. THE System SHALL define a Forecast_Provider interface for retrieving a Forecast given a latitude, longitude, and forecast window.
2. THE System SHALL provide an Open-Meteo Marine implementation of the Forecast_Provider as the default in the MVP.
3. WHEN the Configuration selects a Forecast_Provider, THE System SHALL use the selected Forecast_Provider for forecast retrieval.
4. THE Forecast_Provider SHALL return a Forecast containing wave height, swell period, swell direction, wind speed, wind direction, and a timestamp for each forecast time step.
5. IF the network request to a Forecast_Provider fails, THEN THE System SHALL log the failure and SHALL skip evaluation for the affected Surf_Spot during that Scheduler run.
6. THE System SHALL allow adding a new Forecast_Provider implementation without modifying the Scoring_Engine or Notification_Engine.

### Requirement 7: Forecast Caching

**User Story:** As an operator running on a Raspberry Pi, I want forecasts cached per spot with a TTL, so that the System reduces provider rate-limit pressure and device load.

#### Acceptance Criteria

1. THE Forecast_Cache SHALL store Forecasts keyed by Surf_Spot identifier.
2. WHEN the System needs a Forecast for a Surf_Spot and a non-expired cached Forecast exists, THE System SHALL use the cached Forecast instead of requesting the Forecast_Provider.
3. WHEN the System needs a Forecast for a Surf_Spot and no non-expired cached Forecast exists, THE System SHALL request the Forecast from the Forecast_Provider and SHALL store the result in the Forecast_Cache.
4. THE System SHALL treat a cached Forecast older than the configured time-to-live as expired.
5. THE System SHALL share cached Forecasts across all Subscriptions that reference the same Surf_Spot.

### Requirement 8: Surf Scoring Engine

**User Story:** As a User, I want conditions rated with a 0-100 score and a clear category, so that I can quickly judge how good a session will be rather than reading raw thresholds.

#### Acceptance Criteria

1. THE Scoring_Engine SHALL compute a Surf_Score as an integer from 0 to 100 inclusive from a Forecast and the Subscription's Preset or Custom_Conditions.
2. THE Scoring_Engine SHALL assign a Score_Category of Perfect for a Surf_Score from 95 to 100.
3. THE Scoring_Engine SHALL assign a Score_Category of Excellent for a Surf_Score from 85 to 94.
4. THE Scoring_Engine SHALL assign a Score_Category of Good for a Surf_Score from 70 to 84.
5. THE Scoring_Engine SHALL assign a Score_Category of Rideable for a Surf_Score from 50 to 69.
6. THE Scoring_Engine SHALL assign a Score_Category of Ignore for a Surf_Score below 50.
7. THE Scoring_Engine SHALL combine wave height, swell period, wind speed, wind direction, and swell direction into the Surf_Score rather than returning a pass-or-fail threshold comparison.
8. WHERE Custom_Conditions enable the daylight-only flag, THE Scoring_Engine SHALL assign a Score_Category of Ignore to forecast time steps that fall outside daylight hours.
9. THE System SHALL implement the Surf scoring logic in a module separate from forecast retrieval, notification, and persistence logic.
10. THE System SHALL allow adding a Scorer for another Activity without modifying the Surf Scorer.
11. THE Scoring_Engine SHALL produce a per-factor breakdown identifying the contribution of wave height, swell period, wind, wind direction, and swell direction to the Surf_Score.

### Requirement 9: Notification Anti-Spam

**User Story:** As a User, I want to avoid repeated alerts for the same conditions, so that notifications stay meaningful and rare.

#### Acceptance Criteria

1. WHEN the Notification_Engine evaluates a Forecast that produces a Score_Category below Rideable, THE Notification_Engine SHALL NOT send an immediate alert for that forecast window.
2. WHEN the Notification_Engine sends an alert, THE System SHALL persist a Notification_Record containing the Subscription, Surf_Spot, Surf_Score, forecast window, and timestamp.
3. IF a Notification_Record already exists for the same Subscription, Surf_Spot, and forecast window with an equal or higher Surf_Score, THEN THE Notification_Engine SHALL NOT send a duplicate alert.
4. IF a new Surf_Score for the same Subscription, Surf_Spot, and forecast window is higher than the most recent Notification_Record's Surf_Score but does not exceed it by at least the configured Significant_Improvement threshold, THEN THE Notification_Engine SHALL NOT send an alert.
5. WHEN a new Surf_Score for the same Subscription, Surf_Spot, and forecast window exceeds the most recent Notification_Record's Surf_Score by at least the configured Significant_Improvement threshold, THE Notification_Engine SHALL send an updated alert.
6. THE System SHALL define the Significant_Improvement threshold in the Configuration.

### Requirement 10: Notification Modes and Digests

**User Story:** As a User, I want to choose how I receive alerts, including immediate alerts and scheduled digests, so that notifications fit my routine.

#### Acceptance Criteria

1. THE System SHALL support Notification_Modes of immediate alert, morning digest, evening digest, and weekly best day.
2. WHEN a User sets a Subscription's Notification_Mode, THE System SHALL persist the selected Notification_Mode for that Subscription.
3. WHERE a Subscription's Notification_Mode is immediate alert, THE Notification_Engine SHALL send an alert as soon as a qualifying Surf_Score is detected, subject to the anti-spam and quiet-hours rules.
4. IF an immediate alert fails to be delivered, THEN THE Notification_Engine SHALL retry delivery up to the configured retry count and SHALL include the alert in the next digest for the Subscription if all retries fail.
5. WHERE a Subscription's Notification_Mode is morning digest, THE Notification_Engine SHALL send one summary at the configured morning time listing qualifying Surf_Scores since the previous digest.
6. WHERE a Subscription's Notification_Mode is evening digest, THE Notification_Engine SHALL send one summary at the configured evening time listing qualifying Surf_Scores since the previous digest.
7. WHERE a Subscription's Notification_Mode is weekly best day, THE Notification_Engine SHALL send one summary at the configured weekly time identifying the forecast day with the highest Surf_Score for that Subscription.
8. WHERE a digest contains no qualifying Surf_Scores, THE Notification_Engine SHALL NOT send a digest message for that period.

### Requirement 11: Quiet Hours and Mute/Snooze

**User Story:** As a User, I want quiet hours and the ability to mute or snooze a subscription, so that I am not disturbed at times I choose.

#### Acceptance Criteria

1. WHEN a User sets Quiet_Hours for a Subscription, THE System SHALL persist the Quiet_Hours start and end times for that Subscription.
2. WHILE the current time is within a Subscription's Quiet_Hours, THE Notification_Engine SHALL suppress immediate alerts for that Subscription.
3. WHEN a User mutes a Subscription, THE Notification_Engine SHALL suppress all notifications for that Subscription until the User unmutes the Subscription.
4. WHEN a User snoozes a Subscription for a specified duration, THE Notification_Engine SHALL suppress notifications for that Subscription until the snooze duration elapses, including when the Subscription is muted.
5. WHEN a Subscription's snooze duration elapses and the Subscription is not muted, THE Notification_Engine SHALL resume notifications for that Subscription according to its Notification_Mode.
6. WHILE a Subscription is muted, THE Notification_Engine SHALL keep notifications suppressed after the snooze duration elapses until the User unmutes the Subscription.

### Requirement 12: Explainable Alerts and Feedback

**User Story:** As a User, I want each alert to explain why the score is good and let me give feedback, so that I trust the alerts and help improve future scoring.

#### Acceptance Criteria

1. WHEN the Notification_Engine sends an alert, THE alert SHALL include the Surf_Score, the Score_Category, the Surf_Spot name, and the forecast window.
2. WHEN the Notification_Engine sends an alert, THE alert SHALL include the per-factor breakdown of wave height, swell period, and wind contributions produced by the Scoring_Engine.
3. WHEN the Notification_Engine sends an alert, THE alert SHALL include thumbs-up and thumbs-down Feedback controls using an inline keyboard.
4. WHEN a User submits Feedback on an alert, THE System SHALL persist the Feedback associated with the Subscription, Surf_Spot, and Surf_Score.
5. THE System SHALL retain persisted Feedback for preset tuning and future scoring use.

### Requirement 13: Bot Commands and Conversational UX

**User Story:** As a User, I want clear commands and inline-keyboard interactions, so that I can operate the bot without memorizing syntax.

#### Acceptance Criteria

1. THE Bot SHALL support the commands /start, /location, /subscriptions, /add, /remove, /settings, /presets, /status, /forecast, and /help.
2. WHEN a User sends the /help command, THE Bot SHALL list the available commands with a description of each command.
3. WHEN a User sends the /status command, THE Bot SHALL report the number of active Subscriptions and the time of the most recent Scheduler run.
4. WHEN a User sends the /forecast command and selects any of the User's Subscriptions, THE Bot SHALL report the current best Surf_Score and Surf_Spot for that Subscription regardless of the Subscription's mute or snooze state.
5. WHEN a User sends the /settings command, THE Bot SHALL present editable Notification_Preferences including Notification_Mode, Quiet_Hours, and mute or snooze state.
6. WHERE a Bot interaction offers a choice among predefined options, THE Bot SHALL present the options using an inline keyboard.
7. IF a User sends an unrecognized command, THEN THE Bot SHALL inform the User that the command is not recognized and SHALL suggest the /help command.

### Requirement 14: Background Scheduling

**User Story:** As a User, I want the System to check forecasts automatically on a schedule, so that I get alerts without manual requests.

#### Acceptance Criteria

1. THE Scheduler SHALL run a forecast-check job at the configured Scheduler_Interval.
2. WHEN a forecast-check job runs, THE System SHALL load all Subscriptions, discover nearby Surf_Spots for each Subscription, retrieve Forecasts, compute Surf_Scores, compare against existing Notification_Records, and dispatch notifications where required.
3. IF processing one Subscription raises an error during a forecast-check job, THEN THE System SHALL log the error and SHALL continue processing the remaining Subscriptions.
4. WHEN a forecast-check job completes successfully, THE System SHALL record the completion timestamp of the Scheduler run.
5. IF a forecast-check job starts but does not complete successfully, THEN THE System SHALL leave the completion timestamp unchanged.
6. IF recording the completion timestamp fails while the forecast-check job has otherwise succeeded, THEN THE System SHALL treat the job as completed and SHALL log the timestamp recording failure.
7. WHILE the configured Scheduler_Interval has not elapsed since the previous run, THE Scheduler SHALL NOT start another forecast-check job.

### Requirement 15: Configuration

**User Story:** As an operator, I want to configure the System through a .env file, so that I can deploy without changing code.

#### Acceptance Criteria

1. THE System SHALL load Configuration from a .env file at startup.
2. THE Configuration SHALL include the Telegram bot token, the selected Forecast_Provider, the Scheduler_Interval, the database URL, and notification settings.
3. THE System SHALL validate the Configuration using Pydantic at startup.
4. IF a required Configuration value is missing or invalid, THEN THE System SHALL terminate startup and SHALL log a message identifying the missing or invalid Configuration value.
5. WHERE the Configuration does not specify a Forecast_Provider, THE System SHALL use Open-Meteo Marine as the default Forecast_Provider.
6. THE Configuration SHALL include an AI provider enablement flag, a selected AI_Provider, an AI provider API key, and an AI model identifier.
7. WHERE the Configuration enables an AI provider and does not specify an AI_Provider, THE System SHALL use Gemini as the default AI_Provider.
8. IF the Configuration enables an AI provider without a configured API key, THEN THE System SHALL treat the AI provider as unavailable and SHALL use static Default_Presets.
9. THE Configuration SHALL include the Monetization_Enabled feature flag and the Plan_Limits for each Plan_Tier.
10. WHERE the Configuration does not specify the Monetization_Enabled flag, THE System SHALL default the Monetization_Enabled flag to disabled.

### Requirement 16: Persistence

**User Story:** As an operator, I want a normalized relational schema, so that the System reliably stores users, locations, subscriptions, presets, conditions, notifications, spots, and cached forecasts.

#### Acceptance Criteria

1. THE System SHALL persist data in a SQLite database accessed through SQLAlchemy.
2. THE System SHALL maintain persistent tables for users, locations, subscriptions, activities, presets, custom conditions, notifications sent, surf spots, and forecast cache.
3. THE System SHALL access persisted data through repositories that isolate persistence from the service layer.
4. WHEN the System starts and the database schema is absent, THE System SHALL create the database schema.
5. WHEN the System starts and an existing database schema is incompatible with the current schema version, THE System SHALL migrate or recreate the schema to the current schema version.
6. THE System SHALL associate each Subscription with exactly one User, one Activity, and one Location.
7. THE System SHALL maintain a persistent table for User Plans storing the Plan_Tier, the Plan_Status, a start time, and an expiry time.
8. THE System SHALL maintain a persistent Payment_Record table reserved for future payment integration.
9. THE System SHALL associate each Plan with exactly one User.
10. WHERE a Preset is an AI_Generated_Preset, THE System SHALL persist the AI_Generated_Preset using the same preset table and surf preset parameter shape as a static Default_Preset.

### Requirement 17: Multi-Sport Extensibility

**User Story:** As a developer, I want the architecture to support additional sports without changing existing code, so that future activities like Snowboard can be added cleanly.

#### Acceptance Criteria

1. THE System SHALL represent each supported Activity through a common Activity abstraction used by Subscriptions.
2. THE System SHALL require every Activity, including future Activities, to implement the common Activity abstraction.
3. THE System SHALL allow registering a new Activity that implements the common Activity abstraction with its own Scorer, condition parameters, and Forecast_Provider without modifying the Surf Activity implementation.
4. THE System SHALL allow a new Activity to define condition parameters that differ from the surf condition parameters.
5. WHERE a new Activity is registered, THE Scheduler SHALL process Subscriptions for that Activity using the same forecast-check job flow.
6. THE System SHALL select the Scorer for a Subscription based on the Subscription's Activity.

### Requirement 18: Logging and Error Handling

**User Story:** As an operator, I want extensive logging and graceful error handling, so that I can diagnose issues and keep the System running on a Raspberry Pi.

#### Acceptance Criteria

1. THE System SHALL log forecast-check job start and completion events.
2. WHEN an external request to a Forecast_Provider or Geocoding_Provider fails, THE System SHALL log the failure with the affected provider and request context.
3. IF the Bot fails to deliver a notification to a User, THEN THE System SHALL log the delivery failure and SHALL continue processing remaining notifications.
4. THE System SHALL log Configuration validation failures at startup.
5. IF the logging subsystem fails to write a log entry, THEN THE System SHALL continue running forecast-check jobs.
6. THE System SHALL record log entries with a severity level.

### Requirement 19: AI-Assisted Preset Generation Provider Abstraction

**User Story:** As a developer, I want AI-assisted preset generation behind a provider abstraction, so that I can generate region presets with Google Gemini and swap the AI backend without changing business logic.

#### Acceptance Criteria

1. THE System SHALL define an AI_Provider interface for generating a Default_Preset for a region.
2. THE System SHALL provide a Gemini implementation of the AI_Provider as the default when an AI provider is enabled.
3. WHEN the Configuration selects an AI_Provider, THE System SHALL use the selected AI_Provider for AI-assisted preset generation.
4. WHEN the AI_Provider generates a Default_Preset for a region, THE AI_Provider SHALL return a Preset defined by minimum wave height, maximum wave height, minimum period, maximum wind, preferred wind direction, and preferred swell direction.
5. THE System SHALL treat an AI_Generated_Preset as interchangeable with a static Default_Preset wherever a Default_Preset is used.
6. WHERE the Configuration does not enable an AI provider, THE System SHALL use the bundled static Default_Presets for region presets.
7. WHERE the Configuration enables an AI provider without a configured API key, THE System SHALL use the bundled static Default_Presets for region presets.
8. IF a request to the AI_Provider fails, THEN THE System SHALL log the failure with the affected AI_Provider and request context and SHALL fall back to the bundled static Default_Presets.
9. IF a request to the AI_Provider fails during a Scheduler run, THEN THE System SHALL continue the Scheduler run using the bundled static Default_Presets.
10. THE System SHALL allow adding a new AI_Provider implementation without modifying the Scoring_Engine, Notification_Engine, or Forecast_Provider.

### Requirement 20: User Plan and Billing State

**User Story:** As a product owner, I want each user associated with a monetization plan and billing state, so that I can introduce a paid membership tier later without redesigning the data model.

#### Acceptance Criteria

1. THE System SHALL associate each User with exactly one Plan.
2. THE System SHALL represent a Plan with a Plan_Tier of Free or Paid, a Plan_Status, a start time, and an expiry time.
3. WHEN the System creates a User record, THE System SHALL assign the User the Free Plan_Tier with an active Plan_Status.
4. THE System SHALL persist the Plan_Tier, Plan_Status, start time, and expiry time for each User's Plan.
5. THE System SHALL maintain a Payment_Record structure associated with a User's Plan reserved for future payment integration.
6. WHILE Monetization_Enabled is disabled, THE System SHALL NOT collect payment and SHALL NOT populate Payment_Records.
7. WHEN a Paid Plan's expiry time passes, THE System SHALL set the Plan_Status of that Plan to expired.

### Requirement 21: Plan-Based Feature Gating and Quotas

**User Story:** As a product owner, I want plan-based feature gating and quotas defined now, so that Free and Paid limits can be enabled later through configuration without changing existing features.

#### Acceptance Criteria

1. THE System SHALL determine a User's Entitlements from the User's Plan_Tier and the configured Plan_Limits.
2. WHILE Monetization_Enabled is disabled, THE System SHALL treat every User as entitled to full functionality regardless of Plan_Tier.
3. WHILE Monetization_Enabled is enabled, THE System SHALL enforce the configured Plan_Limits for the User's Plan_Tier when a User creates a Subscription.
4. WHILE Monetization_Enabled is enabled, IF a User attempts to create a Subscription that would exceed the User's Plan_Limit for the maximum number of Subscriptions, THEN THE Bot SHALL reject the creation and SHALL inform the User of the Plan_Limit.
5. WHILE Monetization_Enabled is enabled, THE System SHALL restrict the available Notification_Modes for a Subscription to the Notification_Modes permitted by the User's Plan_Tier.
6. THE System SHALL evaluate Entitlements and Plan_Limits without modifying the Subscription, Notification_Engine, or Scoring_Engine logic that exists for the MVP feature set.
7. WHEN the Configuration changes the Plan_Limits or the Monetization_Enabled flag, THE System SHALL apply the updated values without code changes to existing features.
