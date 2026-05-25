# WebSmith

The website-building agent for agent-cloud. WebSmith is a **prompt-only agent** — a structured workflow an LLM follows to walk a human through designing a website end-to-end and produce a signed `SPEC.md`. The spec is then implemented as a `platform/services/<sitename>/` service following agent-cloud's composable deploy pattern.

WebSmith does not run as a daemon, container, or systemd unit. There is no compose file, no `deploy.sh`, no Semaphore template. You invoke WebSmith by pointing an LLM session (Claude Code, Cursor, ChatGPT with file access, etc.) at `context/AGENTS.md` and starting Phase 1.

## Who this is for

- **Humans** who want to build a new website inside agent-cloud — start at [`context/KICKSTART.md`](./context/KICKSTART.md).
- **LLM agents** running a WebSmith session — start at [`context/AGENTS.md`](./context/AGENTS.md).

## How a site flows through agent-cloud

```
WebSmith session (Phases 0–5)
        |
        v
context/spec/SPEC.md (signed, dated)
        |
        v
platform/services/<sitename>/
    deployment/  — compose.yml, deploy.sh, templates/, source code
    context/     — copy of the signed SPEC + service-specific docs
        |
        v
Composable Ansible deploy (deploy-<sitename>.yml) -> Semaphore -> Proxmox VM
```

UhhCraft (at `platform/services/uhhcraft/`) is the first site built this way. Use it as the reference shape for any future WebSmith output.

## Layout

```
agents/websmith/
├── README.md              This file
├── CLAUDE.md              Agent-cloud conventions specific to WebSmith
├── deployment/            Stub — WebSmith is prompt-only, no runtime
│   └── README.md
└── context/
    ├── AGENTS.md          Master index — LLMs read this first
    ├── KICKSTART.md       Human-facing guide
    ├── README.md          Framework's own README (preserved verbatim)
    ├── questionnaire.md   Optional pre-session intake form
    ├── verification.md    Handoff checklist
    ├── og_prompt.md       Original prompt that produced the framework
    ├── phases/            The 5+1 phase docs
    ├── catalogs/          Reference material (archetypes, components, stacks, styles)
    ├── schemas/           JSON Schemas for each phase artifact
    ├── examples/          End-to-end walkthroughs
    ├── architecture/      How WebSmith integrates with agent-cloud
    ├── prompts/           Reusable kickoff + handoff prompts
    ├── skills/            Discrete agent capabilities
    └── use-cases/         Worked examples (UhhCraft, etc.)
```

## Related

- [`platform/services/uhhcraft/`](../../platform/services/uhhcraft/) — first concrete site built with WebSmith.
- [`plan/development/WEBSMITH-INTEGRATION-PLAN.md`](../../plan/development/WEBSMITH-INTEGRATION-PLAN.md) — full integration plan and "second site" recipe.
- [`plan/architecture/WEBSITE-BUILDING-AGENT.md`](../../plan/architecture/WEBSITE-BUILDING-AGENT.md) — architecture doc (added in Phase 9 of the integration plan).
