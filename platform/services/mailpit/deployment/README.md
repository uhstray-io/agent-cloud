# Mailpit — local-dev mail catcher

Author: Joseph A. Wisneski IV

Mailpit receives mail over SMTP and keeps it, so a developer can read it in a web UI or through its
API. It exists for local-dev only. It accepts every message and every AUTH credential, so nothing in
production may point at it. There is no production inventory entry for it, no production Semaphore
template and no (Dev) variant.

## What it runs

- Image: `docker.io/axllent/mailpit:v1.34.0`, pinned by its image index digest in `compose.yml`. Mailpit
  is MIT licensed.
- Runs as uid and gid 65534. The image's default user is root, so `compose.yml` sets `user:`. The owner
  ran it as 65534 and confirmed the readiness check, SMTP delivery and the API listing (2026-11-27).
- `restart: always`, so the container starts again after a machine reboot on podman as well as docker.
- Messages are kept in the container's temporary database and are not persisted. A recreate drops them.

## Ports

| Port | Inside the container | Published on the host | Use |
| --- | --- | --- | --- |
| SMTP | 1025 | `MAILPIT_BIND`:`MAILPIT_SMTP_PORT` (default `127.0.0.1:1025`) | Mail submission |
| Web UI and API | 8025 | `MAILPIT_BIND`:`MAILPIT_HTTP_PORT` (default `127.0.0.1:8025`) | Browser and API |

The published ports bind loopback only. Mailpit warns that its API is reachable from any host that
routes to an interface it listens on, so the default keeps it off the network.

## How services point at it

On the `local-dev` network, which `compose.local.yml` joins, services reach Mailpit by name:

- SMTP: `mailpit:1025`. Plain SMTP, no TLS, any AUTH username and password accepted.
- API: `http://mailpit:8025`. `GET /api/v1/info` answers 200 while the service is up.

## How it is deployed

Local-dev only. The deploy is the "Deploy mailpit (Local)" template, or `make local-deploy-mailpit`.
Both are wired in a later step of this change, not in this one. The playbook is
`platform/playbooks/deploy-mailpit.yml`, and `deploy.sh` does the container lifecycle only: it writes
a non-secret `.env` from its environment, pulls the image, starts the container and waits for it to
be running. The playbook refuses any host that is not named `<name>-local` or that does not use the
local connection.

## Bumping the image

1. Find the image index digest of the new tag at the registry. Use the index (multi-architecture)
   digest, not a per-architecture digest.
2. Replace the tag and the digest together in the `image:` line of `compose.yml`.
3. Deploy again. `compose_up_if_changed` sees the new image and recreates the container.
