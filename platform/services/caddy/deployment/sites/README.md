# Caddy site fragments

This directory holds one Caddy config fragment per service that wants to be fronted by the central platform Caddy. Files are named `<service>.caddy` and are imported by the main `Caddyfile` via `import sites/*.caddy`.

## How fragments get here

You do **not** edit files in this directory by hand. They are written by Ansible during a service's deploy.

```text
1. The service repo ships templates/caddy-site.j2 (Jinja2)
       platform/services/<svc>/deployment/templates/caddy-site.j2

2. The deploy playbook renders the fragment on the service host
       platform/playbooks/deploy-<svc>.yml

3. tasks/distribute-caddy-site.yml delegates to the central Caddy host,
   copies the rendered fragment into THIS directory, and reloads Caddy
       platform/playbooks/tasks/distribute-caddy-site.yml
```

`caddy reload` is zero-downtime — no inflight requests are dropped.

## Naming

```text
sites/<service>.caddy
```

Use the agent-cloud service name (lowercase, hyphenated). One fragment per service. A service hosting multiple subdomains uses one fragment with multiple site blocks.

## Why fragments instead of editing the main Caddyfile

The main Caddyfile is shared infrastructure with `{$VAR}`-driven routes for legacy services. Adding per-service routes to it directly creates merge conflicts and makes per-service rollback difficult. Fragments give each service its own file, mounted read-only, and the deploy playbook is the only writer.

## See also

- [`../Caddyfile`](../Caddyfile) — main config; imports this directory.
- [`../../../../playbooks/tasks/distribute-caddy-site.yml`](../../../../playbooks/tasks/distribute-caddy-site.yml) — the distribution task.
- [`platform/services/uhhcraft/deployment/templates/caddy-site.j2`](../../../uhhcraft/deployment/templates/caddy-site.j2) — first concrete example.
- [`plan/architecture/CADDY-REVERSE-PROXY.md`](../../../../../plan/architecture/CADDY-REVERSE-PROXY.md) — the full convention.
