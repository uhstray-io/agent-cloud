# Use case: UhhCraft

> **Status:** Placeholder. Filled in during Phase 2 of `plan/development/WEBSMITH-INTEGRATION-PLAN.md` once UhhCraft has been carved into `platform/services/uhhcraft/`.

UhhCraft is the **first concrete site** built with WebSmith and integrated into agent-cloud. It is an e-commerce storefront for AI-designed, one-of-a-kind stickers and 3D-printed items.

## What this use case will document (Phase 2)

- How each of UhhCraft's six phase artifacts (`intake.md`, `purpose.md`, `template.md`, `tooling.md`, `style.md`, `considerations.md`) translated into concrete service decisions.
- Where each SPEC requirement maps to in the codebase at `platform/services/uhhcraft/deployment/`.
- The Podman-vs-Docker deviation from the signed spec, and how that deviation was registered.
- The split between UhhCraft itself and the two inference services (`inference-comfyui`, `inference-hunyuan3d`).
- The Caddy fragment pattern for routing `uhhcraft.uhstray.io`.

## Until then

The signed SPEC lives at [`platform/services/uhhcraft/context/spec/SPEC.md`](../../../../platform/services/uhhcraft/context/spec/SPEC.md) (after Phase 2). The original framework output lives in the source `website_framework/output/` repository.

## See also

- [`plan/development/WEBSMITH-INTEGRATION-PLAN.md`](../../../../plan/development/WEBSMITH-INTEGRATION-PLAN.md) — full integration plan, phase by phase.
