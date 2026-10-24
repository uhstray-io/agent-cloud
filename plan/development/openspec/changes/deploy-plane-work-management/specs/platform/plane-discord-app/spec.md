Author: Joseph A. Wisneski IV <stray@uhstray.io>

## Purpose

Provides the Forge Discord app for Plane. Forge is the platform's Discord bot. The Plane app posts Plane events to the matching project channels, answers slash commands, and unfurls links to work items. Slash commands are received over the Discord Gateway and answered through Discord's HTTP interaction callback. Commands that change Plane are limited to members who hold a declared Discord role and who are mapped to a Plane member. The app uses the shared platform bot token, and it does not mirror Discord threads into Plane.

## ADDED Requirements

### Requirement: The app uses the shared platform bot token and never copies it

The Plane Discord app SHALL read the platform bot token from the shared Discord secret store at `secret/services/discord:bot_token`. The token MUST NOT be written to the Plane secret path or into any file in the repository. The service's documentation MUST record that the app is a long-lived holder of the platform bot token.

#### Scenario: Bot token is read from the shared store, never copied

- **WHEN** the Plane Discord app starts
- **THEN** it reads the bot token from the shared Discord secret path, and the token value appears in no file in the service directory or in the Plane secret path

### Requirement: The webhook receiver accepts only authentic events from Plane

The webhook receiver SHALL verify each delivery before it acts on the delivery. It MUST compute HMAC-SHA256 over the raw request body bytes, using the digest encoding confirmed by Phase 0 spike 0.15, and MUST compare the result with the `X-Plane-Signature` header in constant time. The verification secret SHALL be stored at `secret/services/plane:discord_webhook_secret`, captured when the webhook is created, and read by the app through a shared read. A delivery whose signature does not verify MUST be refused. The receiver SHALL be reachable only on the internal network from Plane. A verified delivery SHALL be posted to the Discord channel that site-config declares for its project.

#### Scenario: Signed event posts to the project channel

- **WHEN** Plane sends a delivery whose `X-Plane-Signature` verifies over the raw body for a declared project
- **THEN** the app posts the event to that project's Discord channel

#### Scenario: Unsigned event is refused

- **WHEN** a request arrives at the webhook receiver with a missing or invalid `X-Plane-Signature`
- **THEN** the receiver refuses the request, and it posts nothing to Discord

#### Scenario: Receiver is reachable only from Plane

- **WHEN** the webhook receiver is deployed
- **THEN** it accepts connections only from the internal network that Plane uses, and it is not published on a public route

#### Scenario: Rotated secret invalidates the old one

- **WHEN** the webhook secret is regenerated in Plane, the new value is stored, and the app is redeployed
- **THEN** a delivery signed with the old secret is refused from the moment the new secret is generated

### Requirement: Each delivery is posted at most once

Before it posts a verified delivery, the app MUST atomically claim the event's identity, the identifier Plane keeps unchanged across retries of one event (the payload's event id or the `X-Plane-Delivery` header, whichever spike 0.15 shows is stable), in a persistent store on the app's own volume. The claim SHALL be a unique-key insert. A delivery identity that has been claimed MUST NOT be posted again, so that retries and concurrent deliveries cannot produce duplicate posts.

#### Scenario: Redelivered event is not posted twice

- **WHEN** Plane redelivers an event whose delivery identity has already been claimed
- **THEN** the app posts nothing for the redelivery

#### Scenario: Concurrent deliveries post once

- **WHEN** two deliveries that carry the same delivery identity arrive at the same time
- **THEN** exactly one of them is posted to Discord

### Requirement: Slash commands are received over the Gateway and answered over HTTP

The app SHALL provide the slash commands `/plane create`, `/plane assign` and `/plane status`. It SHALL receive each interaction as an `INTERACTION_CREATE` event over the Discord Gateway websocket. It SHALL respond through Discord's HTTP interaction callback, `POST /interactions/{id}/{token}/callback`, within 3 seconds. When the work will take longer than that window, it SHALL first send a deferred acknowledgement through the same callback endpoint. The app MUST NOT send an interaction response through the Gateway connection. Both paths are outbound, so the app MUST NOT require an inbound public route for either of them.

#### Scenario: Status command answers through the HTTP callback

- **WHEN** a Discord user runs `/plane status` in a channel where the app is present
- **THEN** the app receives the interaction over the Gateway, and it answers through the HTTP interaction callback within 3 seconds

#### Scenario: Slow command is deferred within three seconds

- **WHEN** a slash command's work will not finish within 3 seconds
- **THEN** the app sends a deferred acknowledgement through the HTTP interaction callback within 3 seconds, and it completes the reply afterwards

### Requirement: Commands that change Plane require a declared role and a member mapping

The app SHALL run `/plane create` and `/plane assign` only for a Discord user who holds a Discord role declared in site-config and who appears in the site-config mapping from Discord user id to Plane workspace member. The app SHALL perform the write with its bot token, and it SHALL record the mapped Plane member in the work item, as a comment or as activity. An invocation by a user who is not authorized, or who has no mapping, MUST be denied with an ephemeral reply, and it MUST make no write to Plane. The `/plane status` command is not subject to this requirement.

#### Scenario: Mapped member creates a work item attributed to them

- **WHEN** a Discord user who holds the declared role and has a mapping runs `/plane create`
- **THEN** the app creates the work item, and the mapped Plane member is recorded on it

#### Scenario: Unmapped user cannot create a work item

- **WHEN** a Discord user with no mapping runs `/plane create`
- **THEN** the app replies with an ephemeral denial, and no work item is created in Plane

#### Scenario: Unmapped user cannot assign a work item

- **WHEN** a Discord user with no mapping runs `/plane assign`
- **THEN** the app replies with an ephemeral denial, and no work item's assignee changes in Plane

### Requirement: Work item links unfurl in Discord with the message content intent

The app SHALL unfurl a link to a work item key such as `KEY-123`, and a link to a work item URL, into a preview in Discord. The app SHALL run with the privileged `MESSAGE_CONTENT` intent enabled on the Bot page under Privileged Gateway Intents. Without that intent, the message `content` field is empty except for mentions and direct messages. Requesting an intent that is not enabled closes the Gateway connection with close code 4014.

#### Scenario: Work item link unfurls in Discord

- **WHEN** a Discord message contains a work item key or a work item URL
- **THEN** the app posts a preview of that work item in the channel

#### Scenario: Missing message content intent fails the deploy

- **WHEN** the app connects to the Gateway without the `MESSAGE_CONTENT` intent enabled
- **THEN** the Gateway closes with code 4014, and the deploy verify step, which checks that the Gateway session is up, fails

### Requirement: The app deploys as its own composable service, idempotently

The Plane Discord app SHALL deploy as its own composable service on the Plane VM, through its own playbook and Semaphore template. Its image SHALL be built to GHCR by a workflow. It MUST NOT be deployed as part of the Plane service playbook. Running the deploy a second time MUST converge: it changes nothing, creates no duplicate resources, and does not redeploy the Plane service.

#### Scenario: Forge app deploys on the Plane VM as its own service

- **WHEN** the Forge app deploy playbook runs
- **THEN** the app runs as a separate service on the Plane VM, and the Plane service is not redeployed

#### Scenario: Second deploy converges without change

- **WHEN** the Forge app deploy playbook runs a second time with no change in its inputs
- **THEN** it changes nothing, creates no duplicate resource, and does not redeploy the Plane service

### Requirement: Discord threads are not mirrored into Plane

The app MUST NOT synchronise Discord threads or their messages into Plane work items. Its only Plane-to-Discord paths are notifications, slash-command responses and link previews.

#### Scenario: Discord thread messages are not mirrored into Plane

- **WHEN** a message is posted in a Discord thread in a channel where the app is present
- **THEN** no work item or comment is created in Plane from that message
