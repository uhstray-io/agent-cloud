Author: Joseph A. Wisneski IV <stray@uhstray.io>

## Purpose

Provides the platform's work tracker. Plane links GitHub issues and pull requests, each repository's OpenSpec changes, and the work cards that the team and its harnesses act on. It replaces tududi as the tracker. Humans sign in through the central identity provider, roles follow identity-provider groups, harnesses act under the user who signed in, and the tududi tracker is torn down in a fixed order.

## ADDED Requirements

### Requirement: Recorded findings gate the phases that depend on them

Each unverified platform behaviour that a later phase depends on MUST be verified in Phase 0, and its dated finding MUST be recorded in the change's design document before the dependent phase starts. A phase that depends on an open finding MUST NOT start.

#### Scenario: Spike findings are recorded before dependent phases start

- **WHEN** a phase depends on a Phase 0 spike
- **THEN** the design document holds a dated finding for that spike, and no row of the Phase 0 table remains open

### Requirement: Each environment runs the Plane tier it can validate

The production deployment SHALL run the Commercial edition on the Pro plan, because single sign-on and the GitHub integration require that plan, and OAuth app registration is not available in the Community edition. The local-dev deployment SHALL run the Commercial Free tier. Behaviour that requires the Pro plan MUST be validated in production and MUST NOT be claimed as validated locally.

#### Scenario: Local validation runs on the Free tier

- **WHEN** the local-dev deployment is validated
- **THEN** it runs on the Free tier, and the single sign-on and GitHub sync checks are not reported as passed for it

#### Scenario: Production runs the Pro plan

- **WHEN** the production deployment is validated
- **THEN** it runs on the Pro plan, and the single sign-on and GitHub sync checks run against it

### Requirement: Plane deploys as a composable service

The Plane service SHALL deploy through Semaphore playbooks, with `deploy.sh` limited to container lifecycle. Long-lived containers SHALL use the `always` restart policy. The one-shot migrator SHALL use the `no` restart policy and carry the label `agent-cloud.one-shot: "true"`. Only the reverse proxy SHALL publish a port to the host. The health check MUST probe the API path rather than the root path. A redeployment MUST NOT regenerate the secrets that the service keeps across redeploys.

#### Scenario: Local deployment is reachable over TLS

- **WHEN** the local deploy playbook completes through local Semaphore
- **THEN** `https://plane.agent-cloud.test` loads over TLS. A successful run records no `service-deploy` result, so none is required here.

#### Scenario: Health is probed on the API path, not the root

- **WHEN** the deploy verify step checks the service
- **THEN** it probes an API path, and a response from the root path alone does not count as healthy

#### Scenario: Only the reverse proxy publishes a port

- **WHEN** the Compose stack is running on the Plane VM
- **THEN** the reverse proxy is the only container that publishes a host port

#### Scenario: A failed deploy is visible to the conformance collector

- **WHEN** a run of the Plane deploy template fails
- **THEN** the conformance collector records the service's `service-deploy` step as failed, with the run's last output lines

#### Scenario: Redeploy keeps generated secrets

- **WHEN** the service is redeployed
- **THEN** the secrets generated once in OpenBao are reused, and no new value is generated for them

### Requirement: Secrets come from OpenBao and never from the repository

Every credential the service uses SHALL come from OpenBao. The license key SHALL be seeded through the isolated seed environment. The repository MUST NOT contain a credential value, and credential handling MUST be scoped to the tasks that handle credentials.

#### Scenario: Repository holds no credential values

- **WHEN** the committed files of the service are scanned
- **THEN** no credential value is present, and each credential is a template variable

#### Scenario: License key is seeded through the isolated environment

- **WHEN** the license key is placed in OpenBao
- **THEN** it is written by the seed playbook running in the isolated seed environment, and its value does not appear in a task result

### Requirement: Backups restore the database with its data intact

The service SHALL provide a backup playbook and a restore playbook for its database. A restore from a backup taken before the restore MUST leave the restored data identical to the backed-up data.

#### Scenario: Backup restores with data intact

- **WHEN** the database is backed up, and then restored from that backup
- **THEN** the restored data matches the backed-up data

### Requirement: Edge and firewall admit only what is declared

The public hostnames SHALL route through the central reverse proxy. The Cloudflare challenge skip SHALL apply to the GitHub webhook path only, and only when the webhook signature check of Phase 0 spike 0.16 has passed. It MUST NOT apply to any other path of the service. The host firewall SHALL deny inbound traffic except from the reverse proxy. Standard output SHALL reach the observability stack through Alloy.

#### Scenario: Only the GitHub webhook path skips the challenge

- **WHEN** a request reaches a path of the Plane hostname other than the GitHub webhook path
- **THEN** the challenge skip does not apply to that request

#### Scenario: Challenge skip is absent when the webhook check fails

- **WHEN** spike 0.16 has not shown that Plane verifies the GitHub webhook signature
- **THEN** no challenge skip exists for the webhook path, and the authenticated boundary recorded in the design document applies instead

#### Scenario: Host firewall denies inbound except from the reverse proxy

- **WHEN** the firewall is applied to the Plane VM
- **THEN** inbound traffic is denied except from the reverse proxy, and the declared egress is the only outbound path opened

#### Scenario: Telemetry reaches the observability stack

- **WHEN** the observability verification runs for the Plane service
- **THEN** the service's logs are visible in the observability stack

### Requirement: The GitHub webhook authenticates its sender before any sync

Plane SHALL verify each GitHub webhook delivery against the configured webhook secret, using the `X-Hub-Signature-256` header, before it acts on the delivery. A delivery that does not verify MUST be refused. Before the challenge skip is added for the webhook path, Phase 0 spike 0.16 MUST have shown this verification working.

#### Scenario: Forged GitHub delivery is rejected

- **WHEN** a delivery arrives at the GitHub webhook path with no valid `X-Hub-Signature-256` header
- **THEN** Plane refuses it, and it creates or changes no work item

### Requirement: Single sign-on through the platform identity provider

Interactive users SHALL sign in to Plane through the Authentik OIDC client. Self sign-up SHALL be turned off, so that only invited members can join, before the OIDC settings are applied. The registered redirect URI MUST be strict, and it MUST be `https://<host>/auth/oidc/callback/` for the service's public host. The requested scopes MUST be openid, email and profile. The client secret SHALL be shared from the identity provider through a shared read, and MUST NOT be copied into a second store.

#### Scenario: Member signs in through Authentik

- **WHEN** a platform member chooses to sign in to Plane
- **THEN** the member is redirected to Authentik, and on approval returns to Plane with an authenticated session

#### Scenario: Redirect URI matches the public host

- **WHEN** the OIDC client is applied for the service's public host
- **THEN** its only redirect URI is `https://<host>/auth/oidc/callback/` with strict matching

#### Scenario: Client secret is shared, not copied

- **WHEN** the service reads its OIDC client secret
- **THEN** it reads the value from the identity provider's shared store, and the value is not written into the Plane secret path

#### Scenario: Invitation-only sign-up is in force before OIDC is enabled

- **WHEN** the OIDC settings are applied for the first time
- **THEN** self sign-up is already turned off, so an account can be created only for an invited member

### Requirement: Plane roles follow identity-provider groups

Plane roles SHALL follow Authentik group membership under decision D3. Members of `platform-admins` MUST hold the Owner role. Members of `platform-developers` and `platform-business` MUST hold the Member role. Members of `platform-user` MUST be denied. The reconcile playbook SHALL be read-only unless it is run with `apply=true`, and a member who has left every member group MUST lose access on apply. The reconcile schedule SHALL be registered only after an apply run has succeeded.

#### Scenario: Administrator receives Owner

- **WHEN** the reconcile playbook applies with `apply=true` for a member of `platform-admins`
- **THEN** that member holds the Owner role in Plane

#### Scenario: Developer and business user receive Member

- **WHEN** the reconcile playbook applies with `apply=true` for a member of `platform-developers` or `platform-business`
- **THEN** that member holds the Member role in Plane

#### Scenario: User group is denied

- **WHEN** a member of `platform-user` who is in no other member group attempts to sign in
- **THEN** that member does not gain a Plane role

#### Scenario: Removed member loses access on apply

- **WHEN** a Plane member is no longer in any member group, and the reconcile playbook applies with `apply=true`
- **THEN** that member is removed from Plane

#### Scenario: Reconcile is read-only without apply

- **WHEN** the reconcile playbook runs without `apply=true`
- **THEN** it reports the changes it would make, and it makes none

#### Scenario: Reconcile schedule starts after a successful apply

- **WHEN** the reconcile schedule is registered in Semaphore
- **THEN** an apply run of the reconcile playbook has already succeeded

### Requirement: GitHub integration syncs issues and pull requests

The GitHub integration SHALL use the GitHub App named Plane. Issues and pull requests SHALL sync between GitHub and Plane. The trigger label `plane` SHALL move an issue from GitHub to Plane, and the trigger label `github` SHALL move it from Plane to GitHub. A pull request that names a work item key, such as `[KEY-123]`, MUST link to that work item. The tududi sync MUST be switched off before the Plane sync is enabled, so that the two bidirectional syncs never run at the same time on the same repository.

#### Scenario: tududi sync is switched off before Plane sync starts

- **WHEN** the Plane GitHub integration is enabled for a repository
- **THEN** the tududi sync for that repository has already been switched off, and the refresher schedules of both the production and the local templates have been removed

#### Scenario: Labelled issue appears in Plane

- **WHEN** an issue in a connected repository receives the `plane` label
- **THEN** a work item for that issue appears in the matching Plane project

#### Scenario: Edit flows back to GitHub

- **WHEN** a work item carrying the `github` label is edited in Plane
- **THEN** the corresponding GitHub issue receives the edit

#### Scenario: Pull request key links to its work item

- **WHEN** a pull request names a work item key such as `[KEY-n]`
- **THEN** the pull request links to that work item

### Requirement: OpenSpec changes project into Plane without overwriting status

The projects sync SHALL read each declared repository's OpenSpec change task files and project them into Plane as modules and work items. Each work item MUST link to its plan file. The generator MUST NOT write a state for any work item, so Plane owns status. A second run of the sync against unchanged input MUST make zero changes.

#### Scenario: Module and work items follow tasks.md

- **WHEN** the sync runs against a repository with an OpenSpec change
- **THEN** the change appears as a Plane module, and each task appears as a work item of that module

#### Scenario: Generator never writes status

- **WHEN** the generator writes its output for a change
- **THEN** no work item in the output carries a `state` value

#### Scenario: Second sync run makes zero changes

- **WHEN** the sync runs a second time against unchanged input
- **THEN** it creates, updates and deletes nothing in Plane

#### Scenario: Status set in Plane survives a re-push

- **WHEN** a work item's state is set in the Plane user interface, and the projects are pushed again
- **THEN** the state set in Plane is still present afterwards

#### Scenario: Work item links to its plan file

- **WHEN** the generator writes a work item for a task
- **THEN** the item carries a link to the plan file that the task belongs to

### Requirement: Harnesses act as the signed-in user

The Plane MCP server SHALL run in OAuth mode behind the reverse proxy. A harness SHALL sign in through Plane's OAuth flow, which goes through Authentik, and each edit it makes MUST be attributed to the user who signed in. The harness configuration MUST NOT contain a secret. weft SHALL use a service identity, a personal access token for a bot user, and MUST NOT use a human's credential.

#### Scenario: Harness lists Plane tools

- **WHEN** a harness with the installed MCP entry connects to the Plane MCP server
- **THEN** the server lists its Plane tools to the harness

#### Scenario: Harness edit is attributed to the signed-in user

- **WHEN** a signed-in user edits a work item through a harness
- **THEN** Plane records the edit under that user

#### Scenario: weft uses its service identity

- **WHEN** weft's builder updates a work item
- **THEN** the update is made with the `weft` bot user's personal access token, and no human credential is used

### Requirement: tududi retires in a fixed order

The tududi retirement SHALL run only after the gates for the earlier phases pass. Open tasks MUST migrate into Plane under the label `migrated-from-tududi`, each carrying its tududi task id as a stable identity. Before every create, the migration MUST query Plane for that identity, so that a rerun after a partial failure never creates a duplicate. The uninstall of the tududi sync GitHub App MUST happen only after the refresher that mints its tokens is gone. The teardown MUST refuse, before any backup or destructive step, unless its confirmation value equals the VM id.

#### Scenario: Open tasks migrate under a marker label

- **WHEN** the one-time migration runs
- **THEN** each open tududi task exists in Plane under the label `migrated-from-tududi`, carrying its tududi task id

#### Scenario: Migration rerun creates no duplicates

- **WHEN** the migration is run again after a partial failure
- **THEN** the items already migrated are skipped, and no item is created a second time

#### Scenario: Teardown refuses without confirmation

- **WHEN** the tududi teardown is launched with a confirmation value that is absent or does not equal the VM id
- **THEN** its preflight refuses before any backup or destructive step, and it changes nothing

#### Scenario: Sync app is uninstalled after the refresher is gone

- **WHEN** the tududi sync GitHub App is uninstalled
- **THEN** the refresher that mints its installation tokens has already been removed

#### Scenario: Retirement leaves no tududi surface

- **WHEN** the teardown completes
- **THEN** no tududi container, route, DNS record, OIDC application or GitHub App remains

### Requirement: Documentation tracks the deployed platform

`AGENTS.md` SHALL describe the Plane service, its workflows and its secrets layout. It MUST NOT describe tududi once the teardown has completed.

#### Scenario: AGENTS.md lists Plane and drops tududi

- **WHEN** the Phase 7 documentation update is complete
- **THEN** `AGENTS.md` lists the Plane workflows and the Plane sub-directory docs line, and it contains no tududi entry
