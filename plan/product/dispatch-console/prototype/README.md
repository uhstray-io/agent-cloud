# Dispatch Console prototype

Static HTML/CSS/JS prototypes of the agent-cloud administration console. **This is a design
reference, not production code.** The production console is built in Rust per
[`plan/development/16-dispatch-console.md`](../../../development/16-dispatch-console.md).

| File | Purpose |
|---|---|
| `dispatch.html` | **The chosen design.** Work-first layout and every page refined in review |
| `ask.html` | Superseded alternative: request-first home |
| `atlas.html` | Superseded alternative: map-first home |
| `shared.css` | Tokens and component styles shared by all three |
| `kit.js` | Shared components and views (the `K.*` ids in [`../DESIGN.md`](../DESIGN.md)) |
| `data.js` | Example data. Everything except the real component and template names is invented |

## Opening it

Open `dispatch.html` in a browser. The files must stay side by side. The only external load
is the Lucide icon script from unpkg, pinned with a subresource-integrity hash. Choices made
with the "Keep" tags are stored in the browser (`localStorage`) outside the claude.ai artifact.

## What is real and what is not

- **Real:** component names (Authentik, OpenBao, OPA, Semaphore, skynet, Proxmox, NetBox,
  agentgateway, Caddy, pfSense, Cloudflare), Semaphore template names, the skynet role names
  from plan 15 D7, the `main` required-check names from `.github/rulesets/protect-main.json`,
  and the agentgateway operator-UI group default (`platform-admins`).
- **Example:** every work item, person, tenant other than Uhstray.io, VM ID, address
  (RFC 5737 documentation range), capacity figure, PR number, segment/VLAN and
  business-domain mapping. Answers from the Work composer are computed from this data;
  production answers come from skynet.
