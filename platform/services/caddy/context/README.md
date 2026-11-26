<!-- omit in toc -->
# Using Caddy on Docker as a Secure Reverse Proxy
Reverse Proxy for Uhstray.io Web Applications

<!-- omit in toc -->
## Table of Contents

- [Caddy Documentation](#caddy-documentation)
  - [Original Documentation Links](#original-documentation-links)
  - [Testing Caddy Locally](#testing-caddy-locally)
- [Automated Setup Script](#automated-setup-script)


## Caddy Documentation

### Original Documentation Links

- [Caddy Webserver and Monitoring Guide](https://betterstack.com/community/guides/web-servers/caddy/)
- [Caddy Server Docker Compose Setup](https://caddyserver.com/docs/running#docker-compose)
- [Caddy Conventions](https://caddyserver.com/docs/conventions)
- [Configuring Caddy Files](https://caddyserver.com/docs/caddyfile/concepts)
- [Caddy Docker Hub](https://hub.docker.com/_/caddy)
- [Caddy Reverse Proxy](https://caddyserver.com/docs/quick-starts/reverse-proxy)
- [Caddy HTTPS Configuration](https://caddyserver.com/docs/quick-starts/https)
- [Caddy File Tutorial](https://caddyserver.com/docs/caddyfile-tutorial)
- [Caddy Cloudflare DNS Module](https://github.com/caddy-dns/cloudflare)
- [Caddy Provider Modules Documentation](https://caddy.community/t/how-to-use-dns-provider-modules-in-caddy-2/8148)

### Testing Caddy Locally

Make sure go is installed:

Install xcaddy
```bash
go install github.com/caddyserver/xcaddy/cmd/xcaddy@latest
```

Build Caddy with the Cloudflare DNS module
```bash
xcaddy build latest --with https://github.com/caddy-dns/cloudflare
```

## Automated Setup Script

`start-caddy.sh` is a legacy helper, not the platform deployment entrypoint.
Run Caddy changes through Semaphore with OpenBao-sourced environment templates.
Use composable site fragments where enabled, or `manage-caddy-sites.yml` for the
flat-Caddyfile deployment. Never pass a Cloudflare credential on the command line.

See [site fragments](../deployment/sites/README.md) and the
[platform infrastructure overview](../../../../plan/architecture/05-platform-infra.md).

Production Caddy (recorded 2026-09-26) is one rootless podman container, `caddy`, on
`restart: always`, created from a legacy standalone directory in the deploy user's home rather
than from `platform/services/caddy/deployment` (Inspect Host Containers, task 1486). The
inventory declares that directory as `compose_working_dir`; `ensure-service-persistence.yml`
enabled linger and podman's user boot unit for it (task 1491), and Verify Service Persistence
passes (task 1521). It declares no health path, so Verify Service Health does not cover it.
