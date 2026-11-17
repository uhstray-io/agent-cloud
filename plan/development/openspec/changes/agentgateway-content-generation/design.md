# Design: agentgateway content generation

Author: Joseph A. Wisneski IV <stray@uhstray.io>. The reasoning, the budget arithmetic and the
architecture diagram are in `plan/development/27-agentgateway-content-generation.md`. This file
keeps the decisions and the facts they rest on, with their citations.

## Context

Content automation drafts posts in Activepieces flows. It needs a text model now and an image model
later, both served through the agentgateway. The gateway identifies each caller by an API key and
bounds it with a token budget and a model allow-list. Its listener also takes client certificates
from the internal CA. The content caller is new: it has no identity, no budget and no network path.

## Decisions

**D1. Network path.** Activepieces reaches the gateway on its internal listener with an internal-CA
client leaf. The public inference route is not used. Decided on 2026-11-12 (Plane AC-783). The
rejected route carries a Cloudflare per-source limit of 10 requests per 10 seconds on `/v1/*` and a
125-second read timeout (AGENTS.md, Deployment Status, inference edge entry).

**D2. Identity.** The identity is `activepieces`. It is never the first entry of `agw_clients`,
because the deploy's keyed verify runs as the first identity (`deploy-agentgateway.yml:971`). It is
not in `agw_openrouter_identities`. Its `allowed_models` names the text model only until task 3.2.

**D3. Budget.** `tokens_per_hour` is 250,000, provisional. Task 2.3 sets it, and gate 2.4
re-measures it from the response usage fields. The value lives in site-config. The arithmetic and
its assumptions are in plan 27, "Identity and budget". The figure is one eighth of the 2,000,000
default (`config.yaml.j2:171`).

**D4. Studio identity.** None for now. A studio identity is needed only if the studio calls models
directly (Plane AC-810, undecided).

**D5. Phasing.** The text path (tasks 2.1 to 2.4) goes first. The image path (tasks 3.1 to 3.6)
waits for the image model's licence (Plane AC-870) and for image serving on the DGX Spark pair
(dgx-spark module `qwen-image-nvfp4-serving`, planned, not built).

**D6. Per-identity request limit for images.** Not decided here; task 3.3 (Plane AC-788) decides it.
Options: (a) a gateway release with a per-key request limit, enforced by the gateway; (b) a count
kept by the calling flow, which is cooperative; (c) an edge rule, which is off the decided path.
Recommendation: (a), with (b) and the global request bucket as the interim.

**D7. Key handout.** Deploy agentgateway generates `secret/services/agentgateway:client_activepieces`
once and reuses it. A `no_log` playbook owned by `social-content-automation` (Plane AC-797) carries
it into the Activepieces connection. No person pastes it by hand.

**D8. Client certificate presentation.** Whether an Activepieces piece can present a client
certificate is UNVERIFIED. Spike 2.1 (Plane AC-826) decides it. The fallback is a minimal mTLS
egress sidecar on an internal network, holding the leaf and the CA bundle.

## Verified facts

- Identities are `agw_clients` plus admin-derived identities, each with one generated
  `client_<name>` field of 48 characters (`platform/playbooks/vars/secret-declarations/agentgateway.yml:42-50`,
  `:249-254`).
- Budget: a rolling one-hour window. The amount is the identity's `tokens_per_hour`, then its tier
  figure, then `agw_rate_tokens_per_hour`, which defaults to 2,000,000. It is charged after the
  response, from reported usage (`platform/services/agentgateway/deployment/templates/config.yaml.j2:155-175`;
  amount at `:171`).
- The budget is a spend guard, not a hard cap. The global request bucket is named as the hard
  admission control (`config.yaml.j2:161-162`).
- Model allow-list: `agw_client_policies.<name>.allowed_models` (`config.yaml.j2:136-140`). OpenRouter
  names are removed from identities outside the union (`config.yaml.j2:55`, `:65`, `:140`).
- The keyed verify runs as the first identity of the merged list (`platform/playbooks/deploy-agentgateway.yml:971`).
- `agw_client_cert_allowlist` defaults to `['caddy']` (`deploy-agentgateway.yml:125`). An entry that
  names no declared client-profile leaf is refused before render (`:294-305`). The allowlist holds
  leaf names.
- Renewal proof: a client leaf with reload `none` on a non-gateway host is proof class
  `probe-client` only when that host sets `agw_verify_base_url`, which must be `https://` when
  listener TLS is on (`platform/playbooks/renew-internal-certs.yml:235-243`). One such leaf per host
  (`:251-254`). The proof text is "probe path answered 401, refused without a client certificate"
  (`:958`). A leaf with no proof path stops renewal before anything is issued (`:301-302`).
- `Probe agentgateway Client TLS (Dev)` refuses a leaf on the allowlist (`platform/playbooks/probe-agentgateway-client-tls.yml:17-20`,
  `:108-115`). It cannot prove an allowlisted leaf is admitted.
- Image pin: `cr.agentgateway.dev/agentgateway:v1.5.0`, overridable through `AGW_IMAGE`
  (`platform/services/agentgateway/deployment/compose.yml:36`). Model entries render the
  `completions` and `responses` formats only (`config.yaml.j2:382-384`, `:426-428`).
- The vLLM entries render one base URL (`config.yaml.j2:430`). The validator refuses any provider
  other than `openrouter` (`secret-declarations/agentgateway.yml:309-310`).
- One global request bucket of 120 a minute by default (`config.yaml.j2:367-371`). The comment says
  v1.5.0 has no per-identity request bucket under the `llm` shortcut, and that the `key` field was
  on main only as of 2026-09-17 (`config.yaml.j2:358-366`).
- Conformance cases are built in `body_for()` (`platform/services/agentgateway/deployment/tests/conformance.sh:172`).
  The direct side is one vLLM URL (`platform/playbooks/run-agw-conformance.yml:407`).
- OpenSpec sync keys: item `<owner/repo>/<change>/<task id>` (`platform/services/plane/lib/openspec_to_plane.py:159`),
  module `<owner/repo>/<change>` (`:188`), checked tasks skipped as done before import (`:148-150`).

## Open questions

- Does v1.5.0 proxy the OpenAI images API? UNVERIFIED. Task 3.1 (AC-786).
- Can an Activepieces piece present a client certificate? UNVERIFIED. Task 2.1 (AC-826).
- Does any gateway release ship a per-key request limit? UNVERIFIED. Tasks 3.1 and 3.3.
- Does the image model's card report no safety checker? UNVERIFIED, not checked here.
- Post cadence: undeclared (Plane AC-802, AC-836). The budget figure is provisional.

## Risks

- **Budget too low.** A legitimate burst could be refused. Gate 2.4 re-measures the figure.
- **Cooperative request count.** Under option (b) a bug in the calling flow can exceed the limit
  until option (a) exists.
- **Shared request bucket.** The global bucket is shared by every client. A looping flow can use it
  up for everyone.
- **Sidecar.** If the spike says a piece cannot present a leaf, the sidecar is one more component in
  the Activepieces project.
- **Pin upgrade.** If v1.5.0 lacks images support, the pin moves for every client.
- **Plane item keys.** UNVERIFIED whether the existing Plane items carry the sync's external ids. If
  they do not, the first sync run after merge could create duplicates. Not checked in this session.
