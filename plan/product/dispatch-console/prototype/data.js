// SYNTHETIC prototype data. No real addresses, hostnames, people or credentials.
// Semaphore template names are the real ones from agent-cloud's templates catalog.
window.AC = (() => {
  // deterministic pseudo-random series so every load draws the same telemetry
  function series(seed, n, base, spread, trend = 0) {
    let s = seed, out = [];
    for (let i = 0; i < n; i++) {
      s = (s * 9301 + 49297) % 233280;
      const r = s / 233280;
      out.push(Math.max(0, +(base + trend * i + (r - .5) * spread + Math.sin(i / 4 + seed) * spread * .3).toFixed(1)));
    }
    return out;
  }

  const tenants = [
    { id: "uhstray", name: "Uhstray.io", orgs: [
      { id: "platform", name: "Platform", sites: [
        { id: "site-hq", name: "site-hq", envs: ["prod", "dev", "local"], clusters: [
          { id: "pve-a", name: "pve-cluster-a", kind: "Proxmox", hosts: [
            { id: "pve-a1", name: "pve-a1", st: "ok", vms: 14 },
            { id: "pve-a2", name: "pve-a2", st: "warn", vms: 11, why: "Memory 88% committed" },
            { id: "pve-a3", name: "pve-a3", st: "ok", vms: 9 } ] },
          { id: "spark", name: "spark-pair", kind: "DGX Spark", hosts: [
            { id: "spark-1", name: "spark-1", st: "ok", role: "vLLM head" },
            { id: "spark-2", name: "spark-2", st: "ok", role: "Ray worker" } ] } ] },
        { id: "site-edge", name: "site-edge", envs: ["prod"], clusters: [
          { id: "pve-b", name: "pve-cluster-b", kind: "Proxmox", hosts: [
            { id: "pve-b1", name: "pve-b1", st: "ok", vms: 4 } ] } ] },
        // hosted sites (named by the user); host names under them are examples
        { id: "do-nyc1", name: "do-nyc1", provider: "DigitalOcean NYC1", envs: ["prod"], clusters: [
          { id: "do-droplets", name: "nyc1-droplets", kind: "DigitalOcean Droplets", hosts: [
            { id: "do-web-1", name: "do-web-1", st: "ok", role: "droplet" },
            { id: "do-web-2", name: "do-web-2", st: "ok", role: "droplet" } ] } ] },
        { id: "cloudflare", name: "cloudflare", provider: "Cloudflare", envs: ["prod"], clusters: [
          { id: "cf-edge", name: "cf-edge", kind: "Cloudflare edge", hosts: [
            { id: "cf-zone", name: "zone (WAF + DNS)", st: "ok", role: "edge" },
            { id: "cf-r2", name: "r2 tofu state", st: "ok", role: "object storage" } ] } ] } ] },
      { id: "commerce", name: "Commerce", sites: [
        { id: "site-hq-c", name: "site-hq", envs: ["prod"], clusters: [
          { id: "pve-a-c", name: "pve-cluster-a", kind: "Proxmox (shared)", hosts: [
            { id: "uhhcraft-vm", name: "uhhcraft", st: "ok" } ] } ] } ] } ] },
    { id: "kestrel", name: "Kestrel Robotics", synthetic: true, orgs: [
      { id: "research", name: "Research", sites: [
        { id: "site-k1", name: "site-k1", envs: ["prod", "dev"], clusters: [
          { id: "pve-k", name: "pve-cluster-k", kind: "Proxmox", hosts: [
            { id: "pve-k1", name: "pve-k1", st: "ok", vms: 6 },
            { id: "pve-k2", name: "pve-k2", st: "crit", vms: 5, why: "Node unreachable for 4m (corosync)" } ] } ] } ] } ] },
  ];

  // layer = row in the four-layer guardrails model
  const services = [
    { id: "skynet", name: "skynet", layer: "ai", kind: "Agent orchestrator", st: "warn", why: "Deploy in progress (AC-1184 queue)", host: "skynet-vm", icon: "bot" },
    { id: "agentgateway", name: "agentgateway", layer: "ai", kind: "Inference edge", st: "ok", host: "agw-vm", icon: "door-open" },
    { id: "vllm", name: "vLLM", layer: "ai", kind: "Inference (spark-pair)", st: "ok", host: "spark-1", icon: "cpu" },
    { id: "honcho", name: "honcho", layer: "ai", kind: "Team memory", st: "ok", host: "honcho-vm", icon: "brain" },
    { id: "authentik", name: "Authentik", layer: "guard", kind: "Identity / OIDC", st: "ok", host: "auth-vm", icon: "fingerprint" },
    { id: "openbao", name: "OpenBao", layer: "guard", kind: "Secrets", st: "ok", host: "bao-vm", icon: "key-round" },
    { id: "opa", name: "OPA", layer: "guard", kind: "Policy engine", st: "ok", host: "opa-vm", icon: "scale" },
    { id: "semaphore", name: "Semaphore", layer: "auto", kind: "Automation", st: "ok", host: "semaphore-vm", icon: "workflow" },
    { id: "n8n", name: "n8n", layer: "auto", kind: "Workflows", st: "ok", host: "n8n-vm", icon: "git-fork" },
    { id: "gha", name: "GitHub Actions", layer: "auto", kind: "CI/CD + 2 runners", st: "ok", host: "gh-runner-01/02", icon: "git-pull-request-arrow" },
    { id: "proxmox", name: "Proxmox", layer: "plat", kind: "Virtualization", st: "warn", why: "pve-a2 memory 88%", host: "pve-cluster-a", icon: "server" },
    { id: "netbox", name: "NetBox", layer: "plat", kind: "Inventory / IPAM", st: "warn", why: "Discovery last ran 26h ago", host: "netbox-vm", icon: "network" },
    { id: "caddy", name: "Caddy", layer: "plat", kind: "Reverse proxy", st: "ok", host: "caddy-vm", icon: "globe" },
    { id: "o11y", name: "o11y", layer: "plat", kind: "Grafana · Prom · Loki", st: "ok", host: "o11y-vm", icon: "activity" },
    { id: "postiz", name: "Postiz", layer: "plat", kind: "Social publishing", st: "crit", why: "Temporal search node restarting (3× in 10m)", host: "postiz-vm", icon: "send" },
    { id: "tududi", name: "tududi", layer: "plat", kind: "Tasks", st: "ok", host: "tududi-vm", icon: "list-checks" },
  ];

  // where each service lives: [Proxmox VM ID(s), node, IP]. null VM = not a VM (bare metal or the hypervisor itself).
  // Addresses are RFC 5737 documentation addresses and DNS names use .example: illustrative only.
  const placement = {
    skynet: ["140", "pve-a1", "192.0.2.40"], agentgateway: ["131", "pve-a3", "192.0.2.31"], vllm: [null, "spark-1", "192.0.2.60"], honcho: ["133", "pve-a3", "192.0.2.33"],
    authentik: ["104", "pve-a1", "192.0.2.14"], openbao: ["102", "pve-a1", "192.0.2.12"], opa: ["106", "pve-a2", "192.0.2.16"],
    semaphore: ["101", "pve-a1", "192.0.2.11"], n8n: ["112", "pve-a2", "192.0.2.22"], gha: ["121–122", "pve-a3", "192.0.2.51–52"],
    proxmox: [null, "pve-a1 · a2 · a3", "192.0.2.2–4"], netbox: ["110", "pve-a2", "192.0.2.20"], caddy: ["103", "pve-a1", "192.0.2.13"], o11y: ["115", "pve-a3", "192.0.2.25"], postiz: ["125", "pve-a2", "192.0.2.35"], tududi: ["127", "pve-a3", "192.0.2.37"],
  };
  // business domain each service serves (example mapping; the real one belongs in the service inventory)
  const domainOf = { postiz: "Products", agentgateway: "Products", vllm: "Products", honcho: "Professional Services", tududi: "Professional Services",
    skynet: "Automation", semaphore: "Automation", n8n: "Automation", gha: "Automation",
    authentik: "Administration", openbao: "Administration", opa: "Administration", proxmox: "IT", netbox: "IT", caddy: "IT", o11y: "IT" };
  const domains = [["Products", "package"], ["Professional Services", "briefcase"], ["Automation", "workflow"], ["Administration", "shield-check"], ["IT", "server"]];
  const dnsOverride = { gha: "gh-runner-0{1,2}.site-hq.example", proxmox: "pve-a{1,2,3}.site-hq.example" };
  services.forEach((s) => {
    s.domain = domainOf[s.id];
    [s.vm, s.node, s.ip] = placement[s.id];
    s.where = s.vm ? `VM ${s.vm} · ${s.node}` : s.node;
    s.dns = dnsOverride[s.id] ?? `${s.host}.site-hq.example`;
  });

  // typed integrations: api | oidc | mcp | data
  const edges = [
    ["skynet", "agentgateway", "api", "/v1 chat completions"],
    ["agentgateway", "vllm", "api", "OpenAI-compatible upstream"],
    ["skynet", "opa", "api", "Action authorization"],
    ["skynet", "semaphore", "api", "Launch templates"],
    ["skynet", "netbox", "mcp", "Device + IPAM tools (net-agent, planned)"],
    ["skynet", "honcho", "mcp", "Team memory"],
    ["semaphore", "openbao", "api", "AppRole secret reads"],
    ["semaphore", "proxmox", "api", "VM lifecycle"],
    ["semaphore", "netbox", "api", "Conformance write-back"],
    ["authentik", "semaphore", "oidc", "SSO"],
    ["authentik", "agentgateway", "oidc", "Operator UI"],
    ["authentik", "o11y", "oidc", "Grafana SSO"],
    ["authentik", "tududi", "oidc", "SSO"],
    ["n8n", "tududi", "api", "GitHub sync"],
    ["n8n", "postiz", "api", "Scheduled posts"],
    ["gha", "semaphore", "api", "Promotion triggers"],
    // data flow, split by signal: logs → Loki (via Alloy), metrics → Prometheus, traces → Tempo (planned prod addition; Alloy OTLP receiver is Phase 2)
    ["caddy", "o11y", "logs", "Access logs → Loki"],
    ["semaphore", "o11y", "logs", "Task results → Loki"],
    ["authentik", "o11y", "logs", "Auth events → Loki"],
    ["vllm", "o11y", "metrics", "Inference metrics → Prometheus"],
    ["proxmox", "o11y", "metrics", "Node metrics → Prometheus"],
    ["agentgateway", "o11y", "metrics", "Token budgets → Prometheus"],
    ["skynet", "o11y", "traces", "Planned: agent spans → Tempo"],
    ["agentgateway", "o11y", "traces", "Planned: request spans → Tempo"],
  ];

  const agents = {
    skynet: { name: "skynet", color: "#2448b8", icon: "bot", role: "Orchestrator" },
    schedule: { name: "Schedule", color: "#6b7480", icon: "clock", role: "Semaphore schedule" },
  };

  const people = {
    you: { name: "You", initials: "JW", color: "#15191e" },
    ari: { name: "Ari Mendez", initials: "AM", color: "#8a4b14", synthetic: true },
    priya: { name: "Priya Natarajan", initials: "PN", color: "#7a2a62", synthetic: true },
    sam: { name: "Sam Okafor", initials: "SO", color: "#225e2f", synthetic: true },
  };

  // state: proposed | queued | running | blocked | done | failed
  const work = [
    { key: "AC-1187", title: "Provision an LLM key for the Research team", state: "proposed", st: "warn",
      scope: ["Kestrel Robotics", "Research", "site-k1", "prod"], target: "agentgateway", agent: "skynet", by: "sam", via: "Natural language", age: "4m",
      ask: "Give the research team access to the inference gateway, 200k tokens an hour, starting today.",
      opa: { decision: "allow", rule: "Budget ≤ 250k tokens/h for a Maintainer request", cond: "Needs one Admin approval: new client identity" },
      steps: [["Parse request", "done"], ["Plan (skynet)", "done"], ["Policy (OPA)", "done"], ["Approval", "wait"], ["Semaphore", "todo"]],
      tasks: [
        { name: "Add client identity research-team to inventory", wf: "site-config PR (inventory)", st: "wait" },
        { name: "Re-render gateway config + key hash", wf: "Deploy agentgateway", st: "todo" },
        { name: "Hand off key to requester", wf: "Back Up Credentials to site-config", st: "todo" } ] },
    { key: "AC-1190", title: "Rotate the Postiz signing secret", state: "proposed", st: "warn",
      scope: ["Uhstray.io", "Platform", "site-hq", "prod"], target: "postiz", agent: "skynet", by: "ari", via: "Natural language", age: "9m",
      ask: "Rotate the Postiz signing secret; it was shared in a support ticket.",
      opa: { decision: "allow", rule: "Credential rotation through a declared deploy", cond: "Needs Admin approval: a new signing secret ends every session and invalidates every API key" },
      steps: [["Parse request", "done"], ["Plan (skynet)", "done"], ["Policy (OPA)", "done"], ["Approval", "wait"], ["Rotate and redeploy", "todo"], ["Verify", "todo"]],
      tasks: [{ name: "Generate and store a new signing secret, redeploy", wf: "Deploy Postiz", st: "todo" }, { name: "Re-capture the API key after sign-in", wf: "Store Postiz API Key", st: "todo" }] },
    { key: "AC-1191", title: "Move agentgateway to the next pinned release", state: "proposed", st: "warn",
      scope: ["Uhstray.io", "Platform", "site-hq", "prod"], target: "agentgateway", agent: "skynet", by: "priya", via: "Form", age: "34m",
      ask: "Bump agentgateway to the next pinned release in tonight's window.",
      opa: { decision: "allow", rule: "Image bump of a declared service", cond: "Production: runs in the change window after Admin approval" },
      steps: [["Plan (skynet)", "done"], ["Policy (OPA)", "done"], ["Approval", "wait"], ["Deploy", "todo"], ["Verify", "todo"]],
      tasks: [{ name: "Render config and redeploy with the new pin", wf: "Deploy agentgateway", st: "todo" }] },
    { key: "AC-1184", title: "Restore Postiz search node", state: "running", st: "run",
      scope: ["Uhstray.io", "Platform", "site-hq", "prod"], target: "postiz", agent: "skynet", by: "you", via: "Fix from alert", age: "11m", sem: 6214, pct: 62,
      ask: "Postiz is red. Fix it.",
      opa: { decision: "allow", rule: "Redeploy of a declared service, same image digest" },
      steps: [["Diagnose", "done"], ["Plan (skynet)", "done"], ["Policy (OPA)", "done"], ["Redeploy", "run"], ["Verify", "todo"]],
      tasks: [
        { name: "Read container state + restart counts", wf: "Inspect Service Runtime", st: "done", sem: 6211 },
        { name: "Redeploy with search overlay", wf: "Deploy Postiz", st: "run", sem: 6214 },
        { name: "Confirm app + workflow engine healthy", wf: "Validate All", st: "todo" } ],
      log: [["", "TASK [Render env files] ok"], ["ok", "TASK [deploy.sh] changed"], ["", "TASK [Wait for temporal search node] …"], ["w", "  retry 2/10: search node not ready"], ["ok", "  search node healthy after 41s"], ["", "TASK [Verify app] running"]] },
    { key: "AC-1181", title: "Give the NetBox VM two more cores", state: "queued", st: "idle",
      scope: ["Uhstray.io", "Platform", "site-hq", "prod"], target: "netbox", agent: "skynet", by: "ari", via: "Form", age: "26m",
      opa: { decision: "allow", rule: "Grow-only resize within cluster headroom" },
      steps: [["Plan", "done"], ["Policy (OPA)", "done"], ["Window", "wait"], ["Resize VM", "todo"], ["Verify", "todo"]],
      tasks: [{ name: "Converge spec 4→6 cores", wf: "Resize VM", st: "todo" }, { name: "Reboot in maintenance window", wf: "Resize VM (allow_reboot)", st: "todo" }] },
    { key: "AC-1179", title: "Add device spark-3 to NetBox", state: "blocked", st: "crit",
      scope: ["Uhstray.io", "Platform", "site-hq", "prod"], target: "netbox", agent: "skynet", by: "priya", via: "Natural language", age: "1h",
      opa: { decision: "deny", rule: "Device role gpu-node is not in the skynet action catalog" },
      steps: [["Parse request", "done"], ["Plan", "done"], ["Policy (OPA)", "fail"], ["Create device", "todo"], ["Verify", "todo"]],
      tasks: [{ name: "Create device record", wf: "Create NetBox Device", st: "todo" }] },
    { key: "AC-1172", title: "Upgrade n8n to the next minor", state: "queued", st: "idle",
      scope: ["Uhstray.io", "Platform", "site-hq", "prod"], target: "n8n", agent: "skynet", by: "you", via: "Natural language", age: "2h",
      opa: { decision: "allow", rule: "Image bump with a fresh database dump first" },
      steps: [["Plan", "done"], ["Policy (OPA)", "done"], ["Back up DB", "todo"], ["Deploy", "todo"], ["Verify", "todo"]],
      tasks: [{ name: "Timestamped pg_dump", wf: "Back Up n8n DB", st: "todo" }, { name: "Deploy pinned image", wf: "Deploy n8n", st: "todo" }] },
    { key: "AC-1170", title: "Service conformance sweep", state: "running", st: "run",
      scope: ["Uhstray.io", "Platform", "site-hq", "prod"], target: "semaphore", agent: "schedule", by: "schedule", via: "Every 15 min", age: "2m", sem: 6216, pct: 30,
      opa: { decision: "allow", rule: "Read-only collector" },
      steps: [["Collect", "run"], ["Write NetBox", "todo"], ["Push Loki", "todo"]],
      tasks: [{ name: "Collect step results", wf: "Collect Service Conformance", st: "run", sem: 6216 }] },
    { key: "AC-1169", title: "Deny egress for gh-runner-02", state: "failed", st: "crit",
      scope: ["Uhstray.io", "Platform", "site-hq", "prod"], target: "gha", agent: "skynet", by: "ari", via: "Natural language", age: "3h", sem: 6190,
      opa: { decision: "allow", rule: "Declared firewall change on a semi-trusted host" },
      steps: [["Plan", "done"], ["Policy (OPA)", "done"], ["Apply", "fail"], ["Verify", "todo"]],
      tasks: [{ name: "Apply default-deny + egress list", wf: "Apply Firewall", st: "fail", sem: 6190 }],
      log: [["", "TASK [Allow SSH before enable] ok"], ["", "TASK [Enable firewall] changed"], ["err", "TASK [Force fresh SSH handshake] FAILED"], ["err", "  anti-lockout check failed; firewall rolled back"]] },
    { key: "AC-1176", title: "Rotate the n8n API key", state: "done", st: "ok",
      scope: ["Uhstray.io", "Platform", "site-hq", "prod"], target: "n8n", agent: "skynet", by: "you", via: "Natural language", age: "5h", sem: 6177,
      opa: { decision: "allow", rule: "Credential rotation, value never printed" },
      steps: [["Plan", "done"], ["Policy", "done"], ["Mint + capture", "done"], ["Verify", "done"]],
      tasks: [{ name: "Mint and store key", wf: "Store n8n API Key", st: "done", sem: 6177 }] },
    { key: "AC-1175", title: "Route the gateway admin UI through Caddy", state: "done", st: "ok",
      scope: ["Uhstray.io", "Platform", "site-hq", "prod"], target: "caddy", agent: "skynet", by: "ari", via: "Form", age: "1d", sem: 6150,
      opa: { decision: "allow", rule: "Declared route in caddy_managed_sites" },
      steps: [["Plan", "done"], ["Policy", "done"], ["Apply", "done"], ["Verify", "done"]],
      tasks: [{ name: "Insert managed site block", wf: "Manage Caddy Sites", st: "done", sem: 6150 }] },
  ];

  const users = [
    { who: "you", uid: "operator", email: "operator@uhstray.example", group: "platform-admins", level: "Admin", scope: "Uhstray.io (all)" },
    { who: "ari", uid: "ari.mendez", email: "ari@uhstray.example", group: "platform-maintainers", level: "Maintainer", scope: "Uhstray.io › Platform" },
    { who: "priya", uid: "priya.natarajan", email: "priya@uhstray.example", group: "platform-developers", level: "Developer", scope: "Uhstray.io › Platform › site-hq › dev" },
    { who: "sam", uid: "sam.okafor", email: "sam@kestrel.example", group: "kestrel-research-maintainers", level: "Maintainer", scope: "Kestrel Robotics › Research" },
  ];

  // agent groups and roles as the gateway configuration defines them: one client identity (key, models, budget) per group
  const agentGroups = [
    { name: "Skynet", gateway: "prod", client: "skynet", note: "skynet and the sub-agents it dispatches", members: [
      { name: "skynet", role: "Orchestrator: routes requests to roles", st: "run", task: "AC-1184", color: "#2448b8", icon: "bot", lead: true },
      // skynet's domain roles (names as Joe's reference designs use them); status and current work are derived from the work queue
      { name: "infra-agent", domain: "Infrastructure", role: "Compute, network, firewall and placement", initials: "IA", color: "#2f5f8f", dynamic: true },
      { name: "security-agent", domain: "Security & access", role: "Policy, credentials, identity and approvals", initials: "SA", color: "#8a3f7a", dynamic: true },
      { name: "service-agent", domain: "Service delivery", role: "Deploys, upgrades and service lifecycle", initials: "SV", color: "#4f4fb0", dynamic: true },
      { name: "o11y-agent", domain: "Observability", role: "Verifies health from metrics, logs and conformance", initials: "OA", color: "#2f7a55", dynamic: true },
      { name: "net-agent", domain: "Networking", role: "Planned: network, IPAM and firewall changes; infra-agent covers this today", initials: "NA", color: "#6b7480", dynamic: true, planned: true } ] },
    { name: "Chat", gateway: "prod", client: "wisbot", note: "Discord-facing assistant", members: [
      { name: "WisBot", role: "Answers and files requests from Discord", st: "idle", color: "#8a4b14", icon: "message-circle" } ] },
    { name: "Task", gateway: "prod", client: "platform-team", note: "agents built for tool calling and scheduled automation, not chat", members: [
      // plan/development/15 D7: netclaw and nemoclaw are not used as agents going forward; member name is an example
      { name: "task-agent", role: "Example: tool calling and scheduled automation, read-only unless approved", st: "idle", color: "#0e7f6a", icon: "cog" } ] },
    // development gateway: developer workflows and agents (member names are examples)
    { name: "Developer", gateway: "dev", client: "local-dev", note: "coding agents and a local skynet on developer machines", members: [
      { name: "skynet (dev)", role: "Local orchestrator under the local-dev podman stack", st: "idle", color: "#2448b8", icon: "bot", lead: true },
      { name: "Coding agents", role: "Developer coding assistants calling models through the dev gateway", st: "run", color: "#4f4fb0", icon: "code" },
      { name: "CI review agent", role: "Reviews pull requests before dev → main promotion", st: "idle", color: "#2f7a55", icon: "git-pull-request-arrow" } ] },
  ];
  // how one Access Level lands in each authority (PRINCIPLES: Authentik groups are the source of truth)
  const roleMap = [
    { level: "Admin", semaphore: "admin flag + all projects", netbox: "superuser", openbao: "platform-admin policy", agw: "operator UI admin", gh: "org owner" },
    { level: "Maintainer", semaphore: "project owner", netbox: "change in scope", openbao: "scoped write", agw: "manage keys in scope", gh: "maintain" },
    { level: "Developer", semaphore: "task_runner", netbox: "view + propose", openbao: "none (via Semaphore)", agw: "own key", gh: "write" },
    { level: "User", semaphore: "guest", netbox: "view", openbao: "none", agw: "own key", gh: "read" },
  ];

  const secrets = [
    { path: "secret/services/agentgateway", fields: 9, rotated: "6d", readers: ["semaphore"], note: "client_* keys, upstream key" },
    { path: "secret/services/authentik", fields: 14, rotated: "21d", readers: ["semaphore", "shared-read: 6 services"] },
    { path: "secret/services/n8n", fields: 7, rotated: "5h", readers: ["semaphore"] },
    { path: "secret/services/postiz", fields: 11, rotated: "30d", readers: ["semaphore"] },
    { path: "secret/services/o11y", fields: 2, rotated: "12d", readers: ["semaphore"] },
    { path: "secret/services/ssh/<service>", fields: 26, rotated: "—", readers: ["semaphore"], note: "per-service keypairs" },
  ];
  const approles = [
    { name: "semaphore", policy: "semaphore.hcl", scope: "secret/data/services/*", st: "ok", note: "Only role allowed the wildcard" },
    { name: "orb-agent", policy: "orb-agent.hcl", scope: "services/netbox (orb fields)", st: "warn", note: "Manual creds pending replacement" },
    { name: "skynet", policy: "skynet.hcl", scope: "services/skynet", st: "ok" },
  ];

  const policies = [
    { name: "Agent action catalog", pkg: "agentcloud.actions", decisions: 412, denies: 9, st: "ok" },
    { name: "NetBox device create", pkg: "agentcloud.netbox.device", decisions: 37, denies: 4, st: "warn", note: "4 denies in 24h: unknown device roles" },
    { name: "Gateway budgets", pkg: "agentcloud.gateway.budget", decisions: 88, denies: 0, st: "ok" },
    { name: "Production change window", pkg: "agentcloud.window", decisions: 51, denies: 2, st: "ok" },
  ];

  const llmClients = [
    { name: "skynet", agent: "skynet", owner: "skynet (agent)", budget: 600, used: 418, models: 2, st: "ok" },
    { name: "wisbot", agent: "WisBot", owner: "WisBot (Discord)", budget: 100, used: 91, models: 1, st: "warn" },
    { name: "platform-team", agent: null, owner: "platform-maintainers", budget: 250, used: 64, models: 2, st: "ok" },
    { name: "local-dev", agent: null, owner: "You", budget: 50, used: 0, models: 1, st: "idle" },
  ];

  const runs = [
    { wf: "Lint and Test", check: "Static Analysis · Security Scan · Unit Tests", ref: "PR #731 → dev", st: "run", age: "3m", runner: "github-hosted" },
    { wf: "Lint and Test", check: "Unit Tests", ref: "PR #729 → dev", st: "crit", age: "41m", runner: "github-hosted", why: "2 BATS failures" },
    { wf: "sync-main-to-dev", check: "merge main → dev", ref: "main", st: "ok", age: "2h", runner: "github-hosted" },
    { wf: "skynet image", check: "multi-arch build", ref: "skynet · main", st: "ok", age: "5h", runner: "gh-runner-01" },
    { wf: "site-config validate", check: "inventory schema", ref: "site-config · PR #81", st: "ok", age: "6h", runner: "gh-runner-02" },
  ];

  // approved services that can be requested into a scope; deploy templates are the real Semaphore template names (AGENTS.md)
  const catalog = [
    { id: "tududi", name: "tududi", what: "To-do and task tracking", template: "Deploy tududi" },
    { id: "honcho", name: "honcho", what: "Team memory API", template: "Deploy honcho" },
    { id: "postiz", name: "Postiz", what: "Social publishing", template: "Deploy Postiz" },
    { id: "n8n", name: "n8n", what: "Workflow automation", template: "Deploy n8n" },
    { id: "openhands", name: "OpenHands", what: "Agent canvas", template: "Deploy OpenHands" },
    { id: "netbox", name: "NetBox", what: "Inventory and IPAM", template: "Deploy NetBox" },
    { id: "agentgateway", name: "agentgateway", what: "Inference edge gateway", template: "Deploy agentgateway" },
    { id: "gha", name: "GitHub runner", what: "Self-hosted CI runner", template: "Deploy GitHub Runner" },
  ];

  // network per site, as NetBox would hold it: ingress path, then segments and what is attached.
  // Components are agent-cloud's real ones (Cloudflare edge, pfSense gateway, Caddy); segment names and VLAN IDs are examples.
  const networks = {
    "site-hq": {
      path: [{ icon: "globe", name: "Internet", sub: "public clients", st: "ok" }, { icon: "cloud", name: "Cloudflare edge", sub: "WAF · DNS · proxied", st: "ok" },
        { icon: "shield", name: "pfSense", sub: "gateway · firewall · DHCP", st: "ok" }, { icon: "split", name: "Caddy", sub: "reverse proxy · TLS", st: "ok", svc: "caddy" }],
      segments: [
        { name: "mgmt", vlan: 10, note: "hypervisors and node management", members: [["host", "pve-a1"], ["host", "pve-a2"], ["host", "pve-a3"], ["host", "spark-1"], ["host", "spark-2"]] },
        { name: "services", vlan: 20, note: "service VMs behind Caddy", members: [["vm", "semaphore-vm"], ["vm", "auth-vm"], ["vm", "netbox-vm"], ["vm", "postiz-vm"], ["vm", "o11y-vm"], ["vm", "agw-vm"], ["vm", "skynet-vm"]] },
        { name: "gpu", vlan: 30, note: "vLLM head and its Ray worker", members: [["host", "spark-1"], ["host", "spark-2"], ["svc", "vllm"]], link: "spark-1 ⇄ spark-2 Ray interconnect" },
        { name: "runners", vlan: 40, note: "self-hosted CI runners · egress denied by host firewall", members: [["label", "gh-runner-01"], ["label", "gh-runner-02"]], restricted: true },
      ] },
    "do-nyc1": {
      path: [{ icon: "globe", name: "Internet", sub: "public clients", st: "ok" }, { icon: "cloud", name: "Cloudflare edge", sub: "WAF · DNS · proxied", st: "ok" }, { icon: "shield", name: "Cloud firewall", sub: "DigitalOcean", st: "ok" }],
      segments: [{ name: "nyc1-vpc", vlan: null, note: "droplet private network", members: [["host", "do-web-1"], ["host", "do-web-2"]] }] },
    "cloudflare": {
      path: [{ icon: "globe", name: "Internet", sub: "public clients", st: "ok" }, { icon: "cloud", name: "Cloudflare edge", sub: "WAF rulesets · DNS records · rate limit", st: "ok" }],
      segments: [] },
  };

  // open changes and where they sit on the promotion path (PR numbers and titles are examples).
  // Required checks: dev PRs run the three CI gates; a dev → main promotion also needs "Promotion source (dev -> main)" (.github/rulesets/protect-main.json).
  const deliveries = [
    { repo: "agent-cloud", pr: 731, title: "Observability enrollment for new hosts", branch: "feat/o11y-enroll", base: "dev", ahead: 4, review: ["warn", "PR review required"],
      checks: [["Static Analysis", "ok"], ["Security Scan", "ok"], ["Unit Tests", "ok"]], stage: 0, note: "Deploy verification is separate" },
    { repo: "agent-cloud", pr: 729, title: "Collector history window", branch: "fix/collector-window", base: "dev", ahead: 2, review: ["crit", "Changes requested"],
      checks: [["Static Analysis", "ok"], ["Security Scan", "ok"], ["Unit Tests", "crit"]], stage: 0, note: "2 BATS failures block the merge" },
    { repo: "agent-cloud", pr: 732, title: "Promote dev → main", branch: "dev", base: "main", ahead: 11, review: ["ok", "Approved"],
      checks: [["Static Analysis", "ok"], ["Security Scan", "ok"], ["Unit Tests", "ok"], ["Promotion source (dev -> main)", "run"]], stage: 1, note: "Merge commit only; sync-main-to-dev follows" },
    { repo: "site-config", pr: 81, title: "Inventory: research-team gateway client", branch: "inventory/research-team", base: "main", ahead: 1, review: ["warn", "Waiting on Admin approval"],
      checks: [["inventory schema", "ok"]], stage: 0, work: "AC-1187", note: "Private repo · runs on the self-hosted pool" },
  ];

  const vms = [
    { vmid: 101, name: "semaphore-vm", node: "pve-a1", cpu: 4, mem: 8, cpuPct: 22, memPct: 61, st: "ok" },
    { vmid: 110, name: "netbox-vm", node: "pve-a2", cpu: 4, mem: 16, cpuPct: 71, memPct: 83, st: "warn" },
    { vmid: 125, name: "postiz-vm", node: "pve-a2", cpu: 4, mem: 8, cpuPct: 48, memPct: 77, st: "crit" },
    { vmid: 104, name: "auth-vm", node: "pve-a1", cpu: 2, mem: 6, cpuPct: 12, memPct: 54, st: "ok" },
    { vmid: 115, name: "o11y-vm", node: "pve-a3", cpu: 4, mem: 12, cpuPct: 35, memPct: 66, st: "ok" },
    { vmid: 131, name: "agw-vm", node: "pve-a3", cpu: 2, mem: 4, cpuPct: 18, memPct: 40, st: "ok" },
    { vmid: 140, name: "skynet-vm", node: "pve-a1", cpu: 8, mem: 16, cpuPct: 64, memPct: 58, st: "warn" },
    { vmid: 201, name: "edge-caddy-vm", node: "pve-b1", cpu: 2, mem: 4, cpuPct: 14, memPct: 38, st: "ok" },
    { vmid: 202, name: "edge-dns-vm", node: "pve-b1", cpu: 1, mem: 2, cpuPct: 6, memPct: 30, st: "ok" },
    { vmid: 301, name: "k-sim-vm", node: "pve-k1", cpu: 8, mem: 32, cpuPct: 52, memPct: 61, st: "ok" },
    { vmid: 302, name: "k-ros-vm", node: "pve-k1", cpu: 4, mem: 16, cpuPct: 31, memPct: 44, st: "ok" },
    { vmid: 303, name: "k-train-vm", node: "pve-k2", cpu: 8, mem: 32, cpuPct: 0, memPct: 0, st: "crit" },
  ];

  // physical capacity per node: cores, memory GiB, CPU used %, memory committed %
  const capacity = {
    "pve-a1": { cores: 32, mem: 128, cpu: 41, memc: 62 }, "pve-a2": { cores: 32, mem: 128, cpu: 77, memc: 88 }, "pve-a3": { cores: 24, mem: 96, cpu: 33, memc: 51 },
    "spark-1": { cores: 20, mem: 128, cpu: 58, memc: 71, gpu: true }, "spark-2": { cores: 20, mem: 128, cpu: 44, memc: 66, gpu: true },
    "pve-b1": { cores: 16, mem: 64, cpu: 18, memc: 35 },
    "do-web-1": { cores: 4, mem: 8, cpu: 37, memc: 58 }, "do-web-2": { cores: 2, mem: 4, cpu: 22, memc: 49 },
    "pve-k1": { cores: 24, mem: 96, cpu: 29, memc: 47 }, "pve-k2": { cores: 24, mem: 96, cpu: 0, memc: 0, down: true },
  };

  const telemetry = {
    cpu: series(3, 40, 38, 18), mem: series(7, 40, 62, 8, .3), rps: series(11, 40, 140, 60),
    err: series(5, 40, 0.6, 0.8), tokens: series(13, 40, 180, 70, 2), restarts: series(17, 40, 0.2, .6),
  };

  const svc = (id) => services.find((s) => s.id === id);
  return { catalog, domains, networks, deliveries, agentGroups, tenants, services, edges, agents, people, work, users, roleMap, secrets, approles, policies, llmClients, runs, vms, capacity, telemetry, series, svc };
})();
