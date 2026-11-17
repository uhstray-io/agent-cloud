# Design: agentgateway content generation

Author: Joseph A. Wisneski IV <stray@uhstray.io>.

Plan 27 (`plan/development/27-agentgateway-content-generation.md`) is the design record for this
change: its Decisions table (D1 to D8), its Verified facts table with their citations, the budget
arithmetic, the architecture diagram and the open questions. This file does not repeat them; it
adds the risks the tasks carry.

## Risks

- **Budget too low.** A legitimate burst could be refused. Gate 2.4 re-measures the figure.
- **Cooperative request count.** A count kept by the calling flow is a text-path stopgap only; a bug
  in the flow can exceed it. It does not meet the image requirement (plan 27, D6).
- **Shared request bucket.** Until D6's open scope is decided, the text path has only the
  gateway-wide bucket; a looping flow can use it up for every client (plan 27, D6).
- **Sidecar.** If the spike says a piece cannot present a leaf, the sidecar is one more component in
  the Activepieces project.
- **Pin upgrade.** If v1.5.0 lacks images support, the pin moves for every client, and gate 2.4 is
  run again on the new pin.
- **Renewed leaf not in use.** A piece or sidecar that reads its certificate once keeps presenting
  the old one after renewal, and a reload-`none` declaration would still pass renewal. Task 2.2 waits
  for spike 2.1 and AC-928 (plan 27, D8).
- **Plane item keys.** The module already carries the sync's key (external source `openspec`,
  `uhstray-io/agent-cloud/agentgateway-content-generation`, read 2026-11-16). The ten open
  tasks (2.1 to 3.6) must carry their task keys before the first sync, or it creates them again;
  1.1 and 1.2 are ticked and skipped. Read 2026-11-16: all twelve items carry their task keys.
