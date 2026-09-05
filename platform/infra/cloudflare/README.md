# Cloudflare — OpenTofu (platform IaC)

OpenTofu manages the platform WAF ruleset and Caddy-facing service DNS records as
code. Run changes through Semaphore **Apply Cloudflare Tofu**, which invokes
[`apply-cloudflare-tofu.yml`](../../playbooks/apply-cloudflare-tofu.yml).

## Ownership

`waf.tf` and `dns.tf` declare managed resources. `imports.tf` records completed
WAF/DNS adoption; it is not a pending DNS phase. Unmanaged resources remain outside
this root. Removing a resource already owned in state from configuration can plan
its deletion, so review the complete plan before applying.

Keep email, vendor verification, external-host records, and Caddy's dynamic
`_acme-challenge` TXT records outside this root. The legacy
`manage-cloudflare-records.yml` remains in the repository; do not let it manage
records already owned by OpenTofu.

## Credentials and state

The Ansible wrapper reads `secret/services/cloudflare` from OpenBao, supplies the
Cloudflare token and R2 access keys through environment variables, and renders the
R2 backend configuration. The same secret record supplies zone and origin values.
Do not put credentials in this directory or pass them on a command line.

## Operation

1. Change the declared HCL through the repository branch workflow.
2. Run **Apply Cloudflare Tofu** with `tofu_action=plan` (the default) and inspect the diff.
3. Run the same template with `tofu_action=apply` after reviewing the intended changes.

The apply branch uses `-auto-approve`; the preceding review is essential. The
checked-in template does not itself implement an automatic PR-plan/merge-apply pipeline.
For any future adoption, declare imports and verify the plan preserves existing
objects before applying; do not recreate live DNS or WAF objects to gain ownership.

See the [Cloudflare plan](../../../plan/development/13-cloudflare-iac.md) for design history.
