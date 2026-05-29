# WebSmith kickoff prompts

Copy-paste these into your LLM session to start a WebSmith run. The agent must have read access to this repo for either path.

## With questionnaire (recommended)

> *I want to build a website inside agent-cloud. Read `agents/websmith/context/AGENTS.md` and `agents/websmith/CLAUDE.md` and follow them. Here is my filled-in questionnaire (from `agents/websmith/context/questionnaire.md`): <paste the filled questionnaire>. Start with Phase 0 by reading `agents/websmith/context/phases/0-intake.md`. Work through all phases with me. The spec artifacts go in `platform/services/<my-sitename>/context/spec/` — not in the WebSmith directory. Do not write any code until I have signed off on the unified SPEC.md.*

Replace `<my-sitename>` with a kebab-case service name (e.g., `my-bakery`, `dev-blog`).

## Without questionnaire

> *I want to build a website inside agent-cloud. Read `agents/websmith/context/AGENTS.md` and `agents/websmith/CLAUDE.md` and follow them. I have not filled out the questionnaire — start at Phase 1 by reading `agents/websmith/context/phases/1-purpose.md` and we'll extract intake conversationally. The spec artifacts go in `platform/services/<my-sitename>/context/spec/` — not in the WebSmith directory. Do not write any code until I have signed off on the unified SPEC.md.*

## Resuming a session

> *We're resuming a WebSmith session. Read `agents/websmith/context/AGENTS.md` and `agents/websmith/CLAUDE.md`, then read the spec artifacts already in `platform/services/<my-sitename>/context/spec/`. Tell me which phase we're in, summarize decisions so far, and continue.*

## Notes

- The agent-cloud override of "spec goes in a separate working directory" is critical — without it, the LLM follows the framework's default and writes spec files in the wrong place.
- The "no code until signoff" rule is non-negotiable. If the agent tries to scaffold code during phases 1–5, stop it.
- Catch-all reminder: every phase exit must include the agent asking *"Is there anything I haven't asked about that you think matters for this site?"*
