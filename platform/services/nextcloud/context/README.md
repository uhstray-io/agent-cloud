# Nextcloud — upstream reference

This directory retains upstream context; the service deployment directory is a
scaffold, not a completed agent-cloud integration. A supported deployment needs a
Semaphore playbook, OpenBao credential flow, and Caddy routing before rollout.

The former direct Docker recipe bypassed those mechanisms and is not a platform
installation procedure. Use the [upstream AIO documentation](https://github.com/nextcloud/all-in-one)
when designing the integration. Do not extract passwords from a container volume
as an alternative to an OpenBao-managed credential lifecycle.
