## Purpose

Provides weft, uhstray's agent harness, as a platform service: one front door VM, one VM per company workspace, and a staging pair. A weft host runs only images weft's CI signed on `main`, by digest. Each deploy backs up first and rolls itself back when its health check or its watch fails. Staging follows weft's `main` on a timer, and the rack moves only on an approved release. Every weft hostname is proxied by Cloudflare, no weft VM reaches OpenBao, and the platform sees weft's process logs and metrics but never a company's content.

## ADDED Requirements

### Requirement: Images run only by a signed digest

A weft host SHALL run only images named by digest in a `release.json` whose signature, and each image's signature, names exactly the certificate identity `https://github.com/uhstray-io/weft/.github/workflows/images.yml@refs/heads/main` and the issuer `https://token.actions.githubusercontent.com`. `release.json` SHALL be verified before any field of it is read. `deploy.sh` SHALL refuse an image reference that is not pinned by digest.

#### Scenario: A tag is refused

- **WHEN** a role's `.env` names an image by tag rather than by `@sha256:` digest
- **THEN** `deploy.sh <role> up` exits non-zero before any container is started

#### Scenario: A branch-built image is refused

- **WHEN** a release names an image whose signature's identity is `images.yml` on any ref other than `refs/heads/main`
- **THEN** the deploy fails at verification on that host, and nothing on the host is stopped

### Requirement: The first deploy to a fresh host succeeds

A deploy to a host that has never run a weft release SHALL reach the start of the new release. `deploy.sh <role> stop` SHALL treat a missing `.env` as nothing running, because every weft container is started from a rendered `.env`.

#### Scenario: A fresh host has no .env

- **WHEN** a host has no `.env` in its deployment directory and no deploy record
- **THEN** the deploy's stop exits 0 without a compose command, the deploy reaches `deploy.sh <role> up`, and the host's record names the new release

### Requirement: What decides which image runs is outside every container mount

The deploy record (`deployed.json`), the replaced release (`release.replaced.json`), the fetched release (`release-incoming/`) and the deploy lock (`deploying/`) SHALL live in a state directory that no weft container mounts. A rollback SHALL re-check that the previous release's images are weft images in weft's registry, by digest, and SHALL verify their signatures before it renders them.

#### Scenario: The state directory is mounted by no container

- **WHEN** a deploy or a rollback reads or writes `deployed.json`, `release.replaced.json`, `release-incoming/` or `deploying/`
- **THEN** the path is under the role's state directory, and neither that directory nor any directory above or below it is a bind-mount source in `compose.workspace.yml`, `compose.door.yml` or a `deploy.sh` one-off container

#### Scenario: A rollback refuses an unverifiable previous image

- **WHEN** the previous release in `deployed.json` names an image outside weft's registry, not pinned by digest, or not signed by the pinned identity
- **THEN** the rollback stops before it renders `.env`, leaves every container stopped and every file in place, and asks a person in Discord

### Requirement: A failed deploy restores the previous release inside its watch window

When a deploy's health check or its watch fails, the deploy SHALL restore the backup it took minutes earlier with the previous release's image, SHALL record the failed release as refused on that host, and SHALL fail the run so a rollout stops there. A timer SHALL NOT deploy a release a host has refused. Outside the watch window, a rollback SHALL be a person's, naming the host twice and the backup set.

#### Scenario: Health fails within the watch window

- **WHEN** a release fails its health check or its 15-minute watch on a host
- **THEN** the previous release runs again on that host from the deploy's own backup, the failed catalog is set aside and not deleted, the failed release is listed as refused in the host's record, and the next timer tick does not deploy it there

### Requirement: Staging follows weft's main

The staging front door and workspace SHALL be deployed, without a person's approval, with the newest commit on weft's `main` whose image build succeeded. The staging timer SHALL be a scheduled template bound to the repository record it runs from, and SHALL place that same branch on the staging hosts.

#### Scenario: Staging runs a signed commit within one tick

- **WHEN** weft's `main` publishes a signed commit and no staging host holds a deploy lock or has refused it
- **THEN** within one tick of "Follow weft main (Dev)" the staging front door and then the staging workspace run that commit, and its GitHub deployment `staging` is `success`

### Requirement: The rack moves only on an approved release

A rack front door or workspace SHALL be deployed only with a published weft release whose `rack` environment was approved by the declared approver, as read from GitHub's API before any host is queued. A rollout SHALL deploy the front door first, then each workspace one at a time, and SHALL stop at the first failure.

#### Scenario: No rack approval, no deploy

- **WHEN** the newest release has no approval of `rack` by `weft_release_approver`
- **THEN** the rollout queues no host and changes nothing

### Requirement: Every weft hostname is proxied by Cloudflare

Each weft DNS record SHALL be declared in `platform/infra/cloudflare/dns.tf` with `proxied = true`, like every platform hostname, so no weft name publishes the origin address. The client address the front door counts sign-in attempts by SHALL come from `CF-Connecting-IP`, trusted only from Cloudflare's published ranges, and Caddy SHALL overwrite any forwarding header a client sent before it reaches the front door.

#### Scenario: The plan holds every weft name proxied

- **WHEN** Apply Cloudflare Tofu plans after the weft records are applied
- **THEN** the plan is zero-diff and every weft record has `proxied = true`

#### Scenario: A client-supplied forwarding header does not reach the door

- **WHEN** a client sends its own `X-Forwarded-For` or `CF-Connecting-IP` to a weft hostname
- **THEN** the front door sees the address Cloudflare connected from, not the one the client wrote

### Requirement: No weft VM reaches OpenBao

Every weft VM SHALL be denied egress to OpenBao, Semaphore, the hypervisors and every other weft VM by its host firewall. Every weft secret SHALL be read by Semaphore's orchestrator on the controller and delivered as a rendered file.

#### Scenario: A weft VM attempting OpenBao is refused

- **WHEN** a process on a weft VM opens a connection to OpenBao's address
- **THEN** the connection is refused by that VM's firewall, and `fw-assess` reports the deny

### Requirement: The platform observes weft without its customers' content

weft SHALL write its platform-process logs (front door, orchestrator lifecycle, deploys, errors) to stdout in a schema that carries no text a person wrote, and those lines SHALL reach Loki with the `service` label. weft SHALL serve `/metrics` with bounded labels and the `prometheus.io/scrape` and `prometheus.io/port` labels on its containers. A company's content (transcripts, prompts, issues, recordings) SHALL stay on its VM.

#### Scenario: A fresh Loki line carries the service label

- **WHEN** the observability verification task runs against a weft host after a deploy
- **THEN** it finds a Loki line from that host within the freshness window with `service` set to weft's service name, and the line holds no text a person wrote

#### Scenario: A workspace that stops answering is reported within 15 minutes

- **WHEN** a workspace stops answering its health route through the edge
- **THEN** a Discord alert names that workspace within 15 minutes

### Requirement: A company's credentials never pass through the platform

No template, inventory variable, environment file or OpenBao path SHALL hold or name a workspace's model keys or forge token. Its admin enters them in weft, and they stay sealed in its catalog.

#### Scenario: The workspace stack names no credential

- **WHEN** the workspace's compose file, templates and playbooks are read
- **THEN** no service has an `env_file`, no environment variable names a key, token, secret or password, and no playbook reads a workspace credential from inventory or OpenBao
