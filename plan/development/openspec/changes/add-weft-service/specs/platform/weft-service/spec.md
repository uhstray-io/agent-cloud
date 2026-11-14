## Purpose

Provides weft, uhstray's agent harness, as a platform service: one front door VM, one VM per company workspace, and a staging pair. A weft host runs only images weft's CI signed on `main`, by digest, named by the signed `release.json` a person pinned it to. Every deploy is launched by a person, backs up first, and fails without rolling anything back when its health check or its watch fails; going back is a person's. Every weft hostname is proxied by Cloudflare, no weft VM reaches OpenBao, and the platform sees weft's process logs and metrics but never a company's content.

## ADDED Requirements

### Requirement: Images run only by a signed digest

A weft host SHALL run only images named by digest in a `release.json` whose signature, and each image's signature, names exactly the certificate identity `https://github.com/uhstray-io/weft/.github/workflows/images.yml@refs/heads/main` and the issuer `https://token.actions.githubusercontent.com`. `release.json` SHALL be verified before any field of it is read. `deploy.sh` SHALL refuse an image reference that is not pinned by digest.

#### Scenario: A tag is refused

- **WHEN** a role's `.env` names an image by tag rather than by `@sha256:` digest
- **THEN** `deploy.sh up` exits non-zero before any container is started

#### Scenario: A branch-built image is refused

- **WHEN** a release names an image whose signature's identity is `images.yml` on any ref other than `refs/heads/main`
- **THEN** the deploy fails at verification on that host, and nothing on the host is stopped

### Requirement: A host runs the release a person pinned it to

A rack host SHALL be deployed only to its `weft_release` pin in site-config, of the form `<version>@sha256:<digest>`, where the digest is that of the signed `weft-release` artifact. A deploy SHALL refuse any other pin before anything changes on the host, SHALL fetch `release.json` by that digest, and SHALL refuse a `release.json` whose version is not the pinned one. Staging SHALL also accept a full commit, resolved to its artifact's digest before the signature is checked.

#### Scenario: A pin that is not a signed digest is refused

- **WHEN** a rack host's `weft_release` is a tag, a branch, a short or bare commit, or a commit with a digest
- **THEN** the deploy fails before anything is fetched, stopped or started on that host

#### Scenario: A rack host moves only to its pin

- **WHEN** a person launches Roll Out weft Release after changing some hosts' pins in site-config
- **THEN** each host whose pin changed is deployed to its pin, in order, and each host already running its pin is left alone

### Requirement: The verifiers cannot be handed a different release

cosign and oras SHALL run from root's image store, never from a store the deploy user can write, and the controller SHALL hash what oras fetches against the digest it was fetched by before reading it.

#### Scenario: A fetched file that does not match its digest is refused

- **WHEN** the manifest or the `release.json` oras returns does not hash to the digest it was fetched by
- **THEN** the deploy fails before reading a field of it, and nothing on the host is stopped

### Requirement: Nothing deploys weft unless a person launches it

No weft template SHALL be scheduled, and no weft playbook SHALL decide what to deploy from GitHub. Every deploy, rollout and rollback SHALL be a Semaphore task a person launched.

#### Scenario: Nothing deploys weft unless a person launches it

- **WHEN** the weft templates in `platform/semaphore/templates.yml` and the weft playbooks are read
- **THEN** no weft template has a schedule, and no weft playbook reads GitHub's releases, approvals or workflow runs

### Requirement: The first deploy to a fresh host succeeds

A deploy to a host that has never run a weft release SHALL reach the start of the new release. `deploy.sh stop` SHALL treat a missing `.env` as nothing running, because every weft container is started from a rendered `.env`.

#### Scenario: A fresh host has no .env

- **WHEN** a host has no `.env` in its deployment directory
- **THEN** the deploy's stop exits 0 without a compose command, and the deploy reaches `deploy.sh up`

### Requirement: weft.toml is outside every container mount

A workspace's `weft.toml` SHALL live in a state directory that root owns and no weft container mounts, and every write to it SHALL run as root. It SHALL be mounted only read-only, and only into the containers that read it. `release.json` SHALL NOT be written to a file on the VM.

#### Scenario: weft.toml is mounted by no container writable

- **WHEN** a deploy or a rollback renders `weft.toml`
- **THEN** the path is under the role's state directory, root owns it, the directory is no bind-mount source in either service's `compose.yml` or a `deploy.sh` one-off container, and `weft.toml` is mounted only with `:ro`

### Requirement: A failed deploy rolls nothing back by itself

When a deploy's health check or its watch fails, the deploy SHALL fail the run, SHALL leave the host as it is, and SHALL name the backup set it took. A rollout SHALL stop at that host. Going back SHALL be a person's: they revert the host's pin in site-config and launch "Roll Back weft Host", naming the host twice and the backup set, which restores that set with the release the host is now pinned to and sets the current database aside without deleting it.

#### Scenario: Health fails within the watch window

- **WHEN** a release fails its health check or its 15-minute watch on a host
- **THEN** the run fails naming the backup set, nothing is restored, no host after it in the rollout is touched, and the failed release keeps running until a person rolls it back

#### Scenario: A failed deploy rolls nothing back by itself

- **WHEN** a deploy's health check fails
- **THEN** no `restore` is run and no earlier release is started by that deploy

### Requirement: The seal has one origin

A workspace's seal SHALL be generated once in OpenBao by `manage-secrets` and rendered as `seal.key` (0600) in the workspace directory before the workspace first starts. No playbook SHALL copy a seal from a VM into OpenBao.

#### Scenario: The seal comes only from OpenBao

- **WHEN** a workspace is deployed for the first time
- **THEN** `seal.key` is rendered from `secret/services/<root>/workspaces/<id>` before the board starts, and no task reads `seal.key` back from the VM

### Requirement: Staging records a healthy commit on GitHub

"Deploy weft Staging" SHALL deploy the staging front door, then the staging workspace, to the commit or pin a person launched it with, and SHALL record the GitHub `staging` deployment as `success` on that commit only after both passed their watch, with a GitHub App installation token minted for that run and scoped to weft's deployments.

#### Scenario: Staging runs the commit a person launched

- **WHEN** a person launches Deploy weft Staging with a signed commit main published
- **THEN** the staging front door and then the staging workspace run that commit, and its GitHub deployment `staging` is `success`

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

weft SHALL write its platform-process logs (front door, orchestrator lifecycle, deploys, errors) to stdout in a schema that carries no text a person wrote, and those lines SHALL reach Loki with the `service` label. weft SHALL serve `/metrics` with bounded labels and the `prometheus.io/scrape` and `prometheus.io/port` labels on its containers, and an alert on each weft scrape target's `up` SHALL report a host that stops answering. A company's content (transcripts, prompts, issues, recordings) SHALL stay on its VM.

#### Scenario: A fresh Loki line carries the service label

- **WHEN** the observability verification task runs against a weft host after a deploy
- **THEN** it finds a Loki line from that host within the freshness window with `service` set to weft's service name, and the line holds no text a person wrote

#### Scenario: A workspace that stops answering is reported within 15 minutes

- **WHEN** a workspace stops answering its scrape
- **THEN** the alert on its target's `up` fires and names that workspace within 15 minutes

### Requirement: A company's credentials never pass through the platform

No template, inventory variable, environment file or OpenBao path SHALL hold or name a workspace's model keys or forge token. Its admin enters them in weft, and they stay sealed in its catalog.

#### Scenario: The workspace stack names no credential

- **WHEN** the workspace's compose file, templates and playbooks are read
- **THEN** no service has an `env_file`, no environment variable names a key, token, secret or password, and no playbook reads a workspace credential from inventory or OpenBao
