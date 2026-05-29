# WebSmith deployment

WebSmith is a **prompt-only agent**. There is nothing to deploy here.

By the `deployment/ + context/` convention used across `agents/` and `platform/services/`, every agent has a `deployment/` directory. WebSmith's is intentionally empty (this README aside) because the agent's "runtime" is an LLM session, not a process.

## To use WebSmith

1. Open a Claude Code (or other repo-aware LLM) session pointed at this repo.
2. Tell the agent: *"Read `agents/websmith/context/AGENTS.md` and run a WebSmith session with me."*
3. Optionally fill in `agents/websmith/context/questionnaire.md` first and paste it as Phase 0 intake.

That's it. There is no service to start, no port to expose, no compose file to bring up.

## Why this directory exists at all

Two reasons:

1. **Consistency.** Tooling and documentation that walk `agents/*/deployment/` (linters, doc generators, future automation) shouldn't special-case WebSmith.
2. **Future-proofing.** If WebSmith ever grows a runtime component (e.g., a small HTTP service that serves the spec to remote agents, or a CI hook that validates committed `SPEC.md` files against `context/schemas/`), it lands here without restructuring.

If you're looking for the framework itself, it's in [`../context/`](../context/).
