## 0. Unblock orchestrated automation

Discovered at apply time, not planning time: the orchestrator's REST API answers every
non-browser client with a Cloudflare managed challenge, and its internal port is
restricted to the central reverse proxy by the very firewall rule this change also
relies on. Without this phase, no later phase can be driven programmatically.

- [x] 0.1 Add a Cloudflare WAF skip rule for the orchestrator's API prefix on its public
      host, in the existing OpenTofu root — narrower than the precedent rule beside it by
      additionally requiring an Authorization header, so credential-less scanners are
      still challenged
- [x] 0.2 **Operator, one browser-triggered run:** run the Cloudflare OpenTofu template
      with `tofu_action=plan`, review the diff (expect exactly one added rule), then
      re-run with `tofu_action=apply`. This is the bootstrap step that cannot itself be
      automated — the fix for "cannot reach the orchestrator" has to be applied through
      the orchestrator
- [x] 0.3 Confirm a token-bearing request to the orchestrator's API now returns JSON
      rather than a challenge page
- [x] 0.4 Validation gate: the orchestrator API is reachable with a token from a
      non-browser client, so every remaining phase can run through it rather than by hand

## 1. Host access hardening — production host

Every task here is a separate orchestrated run. Do not batch them: 1.5 is the gate that
protects 1.7, which is the only irreversible step in the change.

- [x] 1.1 Record the host's bootstrap login and privilege-escalation credential into the
      secret store, additively, and confirm the pre-existing administrative key pair at
      that location is untouched
- [ ] 1.2 Register the host in the private inventory repo with its service identity,
      deployment path, and container runtime; confirm the orchestrator reaches it **by
      password** — this is the baseline access being protected
      REGISTRATION DONE; the confirmation is now unblocked but not yet run. The host
      carries its service identity, deployment path and runtime in both the private
      inventory and the orchestrator's stored copy, which points it at the declared
      address. What blocked the confirmation was not credentials: another guest was
      claiming the same address, so the orchestrator's connection reached that guest
      instead and its password was rejected — while the same password succeeded from a
      workstation whose resolution happened to land on the intended host. Probed at one
      moment from two machines, the address returned two different SSH host keys. The
      squatting guest was powered off on the operator's instruction 2026-08-25, and the
      address now resolves to the intended host from every vantage tested, serving only
      SSH. Remaining: run one orchestrator task against the group to confirm the
      password path end to end.
- [x] 1.3 Issue the host's own key pair into the secret store; confirm re-running returns
      the same pair rather than generating a new one; back the pair up per the private
      repo's convention
- [ ] 1.4 Distribute the administrative and per-service keys to the host; confirm both are
      authorized, that the host's authentication configuration was not modified, and that
      password authentication still works
- [ ] 1.5 Prove key-only access from two independent directions: an orchestrator run using
      the key credential, and an operator-workstation connection with password
      authentication explicitly refused. **If either fails, stop here** — the password path
      is still the safety net and 1.7 removes it
- [ ] 1.6 Install the container runtime on the host — this doubles as the cheapest proof
      that privilege escalation still works over the new credential, while the password
      fallback is still open
- [ ] 1.7 Harden authentication: disable password and interactive authentication, disable
      direct administrative login, configure validated passwordless escalation for the
      service account; confirm the step's own verification passed and re-confirm 1.5's
      workstation check afterwards
- [ ] 1.8 Apply the host firewall in its administrative-access-only form (no service is
      running yet, so no service port is detected); re-verify administrative access from
      the workstation
- [ ] 1.9 Validation gate: scenario "Both proofs succeed, so hardening may proceed" and
      scenario "Enforcement does not lock out the orchestrator" both hold — key-only
      access is confirmed from both directions, password authentication is refused, and
      administrative access survived firewall enforcement

## 2. Service definition

- [x] 2.1 Rewrite the committed compose definition: five services (application,
      datastore, cache, workflow engine, engine datastore), fully env-parameterized, with
      **no credential values**; remove the existing hardcoded credentials
- [x] 2.2 Configure the workflow engine for relational-datastore visibility with the
      search node absent and no dynamic-configuration path set; add a comment naming the
      add-back path in case phase 5 shows it is needed
- [x] 2.3 Publish only the application's port, bound per inventory; confirm the datastores,
      cache, and workflow engine publish no host port
- [x] 2.4 Write the application config template covering URLs, signing secret, datastore
      and cache addresses, workflow engine address, storage settings, registration state,
      rate ceiling, the identity-provider block, and every social provider slot rendering
      empty when unseeded
- [x] 2.5 Write the compose-substitution template: image tags, published bind and port,
      and the two datastore passwords
- [x] 2.6 Write the lifecycle script — container operations only, no secret handling:
      verify both rendered files present, pull, recreate so re-rendered config actually
      applies, wait for health with enough headroom for first-boot schema setup
- [x] 2.7 Write the local-environment overlay: memory caps, the label option the local
      runtime needs for volume mounts, the shared local network on the application only,
      and the internal trust root plus the variable that makes the runtime honour it
- [x] 2.8 Write the service's operator and agent documentation
- [x] 2.9 Validation gate: scenario "Repository contains no credential values" holds —
      the committed definition and templates contain only deployment-time references, and
      scenario "Internal components are not exposed" holds by construction

## 3. Automation and secret management

- [x] 3.1 Write the deploy playbook on the composable pattern: place the repo, ensure the
      runtime and rootless persistence, manage secrets, distribute the internal trust root
      when local, render both config files, run the lifecycle script, verify health
- [x] 3.2 Declare the three generate-once-and-reuse secrets (signing secret, datastore
      password, engine datastore password) and the cross-service read of the OIDC client
      secret owned by the identity provider
- [x] 3.3 Scope credential-handling steps into their own tasks and confine log suppression
      to those; leave deploys, waits, health checks, and verification visible
- [x] 3.4 Write the destructive clean-deploy playbook on the shared teardown task
- [x] 3.5 Write the secret-seeding playbook that reads the operator's existing values and
      writes the nine social-platform credentials into the secret store under the exact
      keys the config template expects, preserving unrelated keys at that path
- [x] 3.6 Add the service to the estate-wide health-check playbook as its own play block
      (per the onboarding checklist), so it is covered by the Validate All template
- [x] 3.7 Add the service's credentials to the live credential-verification playbook, so
      its datastore and API auth are exercised by the Validate Secrets template
- [ ] 3.8 Validation gate: scenario "A social platform credential is added later" holds —
      seeding a previously-unset provider credential and redeploying makes that provider
      connectable with no code change
      NO-CODE-CHANGE HALF VERIFIED by inspection 2026-08-25; the "connectable" half
      still needs a live deploy and an OAuth round trip. Every provider credential the
      config template reads defaults to empty when unset, so an unseeded provider
      renders harmlessly and a later-seeded one is picked up by a redeploy alone. The
      seeder takes only the values it is given, so adding one is a seed plus a
      redeploy. Cross-checked the two lists against each other, because a credential
      seeded into a key nothing reads is the failure this repo has already recorded
      once: of the keys the seeder can place, ZERO are unread by the template, and
      every key the template reads has a declared source — two generated once and
      deliberately never regenerated, one shared-read from the identity provider, and
      two operator-supplied but not social-platform credentials. No orphans in either
      direction.

## 4. Identity, edge, and inventory wiring

- [x] 4.1 Write the identity-provider client blueprint: confidential client, secret
      injected from the provider's environment, strict redirect at the public URL with the
      upstream-dictated `/settings` suffix, and the openid/profile/**email** scope
      mappings — email is load-bearing
- [x] 4.2 Wire the client secret into the identity provider's deploy so the provider owns
      it and this service reads the same value; never commit it
- [x] 4.3 Register the service in all three inventory files — local, the public
      placeholder, and the private production repo — including the per-environment public
      URL and redirect, bind and port, image tag, firewall upstream, and the registration
      state variable defaulting to open
- [x] 4.4 Add the local reverse-proxy route to the service by container name, with no
      edge authentication gate
- [x] 4.5 Prepare the production reverse-proxy site block for the private repo: public
      hostname, existing DNS-01 TLS flow, single upstream, no edge authentication gate
- [x] 4.6 Add local and production orchestrator templates: deploy, clean-deploy, and
      secret seeding; publish them through the template-management playbook
- [x] 4.7 Record the host's static address and VM id in the private repo's VM spec file,
      so the estate inventory reflects a host that was provisioned before this change
- [x] 4.8 Validation gate: scenario "Advertised URL does not match the browser URL" is
      guarded — the public URL, the registered redirect, and the environment's actual
      browse URL are the same string in each environment, sourced from one inventory
      variable
      SATISFIED 2026-08-25, but it FAILED first and needed a fix. Production stated the
      hostname three times independently — as the service's advertised URL on one host,
      and as the registered redirect and launch URL on the identity provider's host —
      each a hand-written string that "MUST byte-match" the others by comment alone.
      It is now declared once in the shared group vars both hosts can see, with the
      redirect and launch URL derived from it. Verified by rendering them through
      Ansible on the identity-provider host: both resolve to exactly the strings they
      previously hard-coded, so the change is behaviour-identical and can no longer
      drift. A mismatch here fails at the IdP rather than at the service that drifted,
      which is why one declaration matters more than a careful comment.

## 5. Local validation

- [x] 5.1 Seed the social-platform credentials into the local secret store; verify all
      nine are present with the read-only inventory playbook
- [x] 5.2 Deploy the identity provider so the new client blueprint is applied; confirm the
      application and provider exist
- [x] 5.3 Deploy the service locally; confirm all five containers reach health and the
      rendered config contains no unsubstituted template markers and is not
      world-readable
- [x] 5.4 Load the interface over TLS at its local hostname; confirm it renders and offers
      the identity-provider sign-in path
- [ ] 5.5 Complete a sign-in round trip through the identity provider; confirm a session
      is established and the account created
- [ ] 5.6 Flip the registration variable to closed and redeploy; confirm a second identity
      cannot register and the closed state held without a manual step on the host
- [ ] 5.7 **The workflow-engine gate.** Connect one social account, schedule a post a few
      minutes out, and confirm it publishes. If it does not, add the search node back as
      the inventory-gated block scoped in task 2.2 and re-run before proceeding
- [ ] 5.8 Restart the service with a post still scheduled in the future; confirm it
      publishes at its scheduled time
- [ ] 5.9 Generate an API key; confirm the automation endpoint answers with it, refuses
      without it, and is reachable with no browser session
- [x] 5.10 Add shell tests for the lifecycle script; run the repo's linters and test
      suites plus the local smoke check
- [ ] 5.11 Validation gate: scenario "Post publishes at its scheduled time" and scenario
      "Automation endpoint is reachable without an interactive session" both hold — the
      trimmed topology executes scheduled work, and the automation path is not gated by
      interactive authentication

## 6. Promotion to production

- [x] 6.1 Update the repository documentation the change touches: root README service
      list, the agent instructions' service and workflow tables, and the local-dev tier
      table
- [ ] 6.2 Run the simplification and security review passes over the branch changes
- [ ] 6.3 Open a pull request into the integration branch **only when explicitly asked**;
      wait for every check to pass, address findings, confirm green, then merge with a
      merge commit
- [x] 6.4 Operator prerequisite: create the public DNS record for the service hostname
      — ALREADY DONE: `postiz` is declared in the Cloudflare OpenTofu root's platform
      subdomain set and the record exists live (confirmed in a plan run). No action needed.
- [ ] 6.5 Operator prerequisite: update the OAuth redirect destination at all four social
      platforms to the new public host — account connection fails until this is done
- [ ] 6.6 Open the promotion pull request to the production branch **only when explicitly
      asked** and merge it with a merge commit, so the orchestrator deploys from there
- [ ] 6.7 Validation gate: scenario "Interface and automation share one hostname" is
      satisfiable in production — the DNS record resolves and the redirect destinations
      registered at the identity provider and the four social platforms all name that same
      hostname

## 7. Production deployment and lockdown

- [ ] 7.1 Seed the social-platform credentials into the production secret store; verify
      with the read-only inventory playbook
- [ ] 7.2 Deploy the identity provider so the client blueprint is applied with production
      URLs
- [ ] 7.3 Deploy the service; confirm all five containers reach health
- [ ] 7.4 Publish the production reverse-proxy site block; confirm the public URL serves
      over TLS and that the proxy's own validation passed
- [ ] 7.5 Re-apply the host firewall so it detects and permits the now-published service
      port from the reverse proxy only; re-verify both the public URL and administrative
      access
- [ ] 7.6 Complete the first production sign-in through the identity provider, then flip
      registration to closed and redeploy
- [ ] 7.7 Connect the social accounts and confirm a scheduled post publishes in production
- [ ] 7.8 Validation gate: scenario "Only the two intended paths are reachable" holds —
      administrative access from the designated ranges and the service port from the
      reverse proxy succeed, everything else inbound is refused; and scenario "Closed
      state survives redeployment" holds

## 8. Automation contract for n8n

- [x] 8.1 Write the integration contract document: base endpoint, the API-key auth model,
      the media-upload then create-post then schedule flow, and the rate ceiling as the
      budget callers must respect
- [x] 8.2 Record why automation reaches the service over the public host rather than
      directly on the internal network, so the reasoning survives a later "optimization"
- [x] 8.3 Note where the API key belongs in the secret store for when the automation
      workflows are actually built — no automation deployment or configuration in this
      change
- [x] 8.4 Validation gate: scenario "Rate ceiling is enforced" is documented with its
      configured value and window, so the contract states a budget a caller can actually
      respect

## 9. Close out

- [ ] 9.1 Retain one outcome memory into the repo's experience bank: whether the trimmed
      workflow-engine topology worked, labelled worked / dead end / corrected, with the
      root cause of anything that failed and any constraint discovered along the way
- [ ] 9.2 Run the change's validation and archive it
- [ ] 9.3 Validation gate: scenario "Re-running a completed sequence changes nothing"
      holds — re-running the deploy and firewall playbooks against the finished production
      host reports no change
