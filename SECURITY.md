# Security Policy

## Supported Versions

| Version | Supported |
| ------- | --------- |
| main branch | Yes |
| Feature branches | Development only |

## Reporting a Vulnerability

If you discover a security vulnerability in this repository, please report it responsibly.

### How to Report

1. **Do NOT open a public GitHub issue** for security vulnerabilities.
2. Send an email to **security@uhstray.io** with:
   - A description of the vulnerability
   - Steps to reproduce the issue
   - The potential impact
   - Any suggested fixes (optional)

### What to Expect

- **Acknowledgment** within 48 hours of your report
- **Assessment** within 7 days, including severity classification
- **Resolution timeline** communicated after assessment
- **Credit** in the fix commit (if desired) once the vulnerability is resolved

### Scope

This policy covers the agent-cloud repository and its deployment configurations. The following are in scope:

- Hardcoded credentials or secrets in committed files
- Insecure default configurations
- Authentication or authorization bypass in deployment scripts
- Exposure of private infrastructure details (IPs, hostnames, credentials)
- Vulnerabilities in custom code (deploy scripts, Python workers, Ansible playbooks)

The following are out of scope:

- Vulnerabilities in upstream dependencies (report to the upstream project directly)
- The vendored `netbox-docker/` directory (report to [netbox-community/netbox-docker](https://github.com/netbox-community/netbox-docker))
- Issues requiring physical access to infrastructure

## Security Practices

This repository implements the following security measures:

- **Secret scanning** via trufflehog on every pull request
- **Static analysis** via ruff, shellcheck, ansible-lint, bandit
- **Credential leak regression tests** via BATS (RFC1918 IPs, hardcoded passwords, API keys)
- **Dependency scanning** via GitHub Dependabot (pip, GitHub Actions, Docker base images)
- **No credentials in code** -- all secrets managed through OpenBao with Jinja2 template variables
- **Public/private repository split** -- real IPs and credentials live in a separate private repository
