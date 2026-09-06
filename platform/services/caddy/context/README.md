<!-- omit in toc -->
# Using Caddy on Docker as a Secure Reverse Proxy
Reverse Proxy for Uhstray.io Web Applications

<!-- omit in toc -->
## Table of Contents

- [Caddy Documentation](#caddy-documentation)
  - [Original Documentation Links](#original-documentation-links)
  - [Testing Caddy Locally](#testing-caddy-locally)
- [Automated Setup Script](#automated-setup-script)
- [NextCloud Configuration](#nextcloud-configuration)


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

## NextCloud Configuration

- [AIO Reverse Proxy Setup](https://github.com/nextcloud/all-in-one/blob/main/reverse-proxy.md#adapting-the-sample-web-server-configurations-below)
- [Docker Compose Example](https://github.com/nextcloud/all-in-one/discussions/575)

**Configuration Note**
> On a different server (in container or not)
Use the <mark>private ip-address</mark> of the host that shall be running AIO. So e.g. `private.ip.address.of.aio.server:$APACHE_PORT` instead of `localhost:$APACHE_PORT`. If you are not sure how to retrieve that, you can run: `ip a | grep "scope global" | head -1 | awk '{print $2}' | sed 's|/.*||'` on the server that shall be running AIO (the commands only work on Linux).
