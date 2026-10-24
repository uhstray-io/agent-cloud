Author: Joseph A. Wisneski IV <stray@uhstray.io>

## Purpose

Provides the Forge Discord app for Plane. Forge is the platform's Discord bot. The Plane app posts Plane events to the matching project channels, answers slash commands, and unfurls links to work items. It uses the shared platform bot token, and it does not mirror Discord threads into Plane.

## ADDED Requirements

### Requirement: The app uses the shared platform bot token and never copies it

The Plane Discord app SHALL read the platform bot token from the shared Discord secret store at `secret/services/discord:bot_token`. The token MUST NOT be written to the Plane secret path or into any file in the repository. The service's documentation MUST record that the app is a long-lived holder of the platform bot token.

#### Scenario: Bot token is read from the shared store, never copied

- **WHEN** the Plane Discord app starts
- **THEN** it reads the bot token from the shared Discord secret path, and the token value appears in no file in the service directory or in the Plane secret path

### Requirement: The webhook receiver accepts only signed events from Plane

The webhook receiver SHALL verify the `X-Plane-Signature` header of each event with HMAC-SHA256 before it acts on the event. An event whose signature does not verify MUST be refused. The receiver SHALL be reachable only on the internal network from Plane. A verified event SHALL be posted to the Discord channel that site-config declares for its project.

#### Scenario: Signed event posts to the project channel

- **WHEN** Plane sends an event with a valid `X-Plane-Signature` for a declared project
- **THEN** the app posts the event to that project's Discord channel

#### Scenario: Unsigned event is refused

- **WHEN** a request arrives at the webhook receiver with a missing or invalid `X-Plane-Signature`
- **THEN** the receiver refuses the request, and it posts nothing to Discord

#### Scenario: Receiver is reachable only from Plane

- **WHEN** the webhook receiver is deployed
- **THEN** it accepts connections only from the internal network that Plane uses, and it is not published on a public route

### Requirement: Slash commands act on Plane through the Discord gateway

The app SHALL provide the slash commands `/plane create`, `/plane assign` and `/plane status`. It SHALL receive them through the Discord gateway websocket, which is an outbound connection, and it MUST NOT require an inbound public route for them.

#### Scenario: Slash command answers through the gateway

- **WHEN** a Discord user runs `/plane status` in a channel where the app is present
- **THEN** the app answers in that channel through the gateway connection

### Requirement: Work item links unfurl in Discord

The app SHALL unfurl a link to a work item key such as `KEY-123`, and a link to a work item URL, into a preview in Discord.

#### Scenario: Work item link unfurls in Discord

- **WHEN** a Discord message contains a work item key or a work item URL
- **THEN** the app posts a preview of that work item in the channel

### Requirement: The app deploys as its own composable service

The Plane Discord app SHALL deploy as its own composable service on the Plane VM, through its own playbook and Semaphore template. Its image SHALL be built to GHCR by a workflow. It MUST NOT be deployed as part of the Plane service playbook.

#### Scenario: Forge app deploys on the Plane VM as its own service

- **WHEN** the Forge app deploy playbook runs
- **THEN** the app runs as a separate service on the Plane VM, and the Plane service is not redeployed

### Requirement: Discord threads are not mirrored into Plane

The app MUST NOT synchronise Discord threads or their messages into Plane work items. Its only Plane-to-Discord paths are notifications, slash-command responses and link previews.

#### Scenario: Discord thread messages are not mirrored into Plane

- **WHEN** a message is posted in a Discord thread in a channel where the app is present
- **THEN** no work item or comment is created in Plane from that message
