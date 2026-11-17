# Design: agentgateway content generation

Author: Joseph A. Wisneski IV <stray@uhstray.io>.

Plan 27 (`plan/development/27-agentgateway-content-generation.md`) is the design record for this
change: its Decisions table (D1 to D8), its Verified facts table with their citations, the budget
arithmetic, the architecture diagram and the open questions. This file does not repeat them; it
adds the risks the tasks carry.

## Risks

- **Budget too low.** A legitimate burst could be refused. Gate 2.4 re-measures the figure.
- **Cooperative request count.** If task 3.3 settles on a count kept by the calling flow, a bug in
  the flow can exceed the limit until the gateway enforces one.
- **Shared request bucket.** The global bucket is shared by every client. A looping flow can use it
  up for everyone.
- **Sidecar.** If the spike says a piece cannot present a leaf, the sidecar is one more component in
  the Activepieces project.
- **Pin upgrade.** If v1.5.0 lacks images support, the pin moves for every client, and gate 2.4 is
  run again on the new pin.
- **Renewed leaf not in use.** A long-running piece or sidecar that reads its certificate once keeps
  presenting the old one after renewal. Task 2.2 proves the running consumer, not only the files
  (plan 27, D8).
- **Plane item keys.** The module already carries the sync's key (external source `openspec`,
  `uhstray-io/agent-cloud/agentgateway-content-generation`, read 2026-11-16). Its twelve items get
  their task keys before this change merges; an item without one would be created again by the
  first sync.
