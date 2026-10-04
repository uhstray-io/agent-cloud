# inference

Author: Joseph A. Wisneski IV <stray@uhstray.io>.

Nothing is deployed from this directory. It held an empty placeholder for an in-repo LLM
serving stack that was never built here.

Where inference lives instead:

- **The inference edge gateway** is agentgateway: `platform/services/agentgateway/`
  (deployment, and `context/architecture.md` for how it fronts the upstream). Clients reach
  it at the public inference hostname through Caddy.
- **The model server** (vLLM on the DGX Spark pair) belongs to the separate `dgx-spark`
  repository. Its roadmap record for moving the vLLM API into this estate later is the
  dgx-spark OpenSpec change `node-telemetry-and-placement-benchmark`.
- **The decision** that agentgateway is the edge and skynet the orchestrating gateway is
  `plan/architecture/05-platform-infra.md`, "Inference gateway: agentgateway alongside
  skynet" (Proposed).
- **The non-LLM sidecars** are `platform/services/inference-comfyui/` and
  `platform/services/inference-hunyuan3d/`.
