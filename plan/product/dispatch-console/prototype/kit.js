// Shared component kit for the three console prototypes. Every component here
// carries a "K." id so a pick in one prototype means the same thing in all three.
window.K = (() => {
  const D = window.AC;
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const ic = (n, cls = "") => `<i data-lucide="${n}" class="${cls}"></i>`;

  const ST = {
    ok: ["circle-check", "Healthy"], warn: ["triangle-alert", "Degraded"], crit: ["octagon-x", "Down"],
    run: ["loader-circle", "Running"], idle: ["circle-dashed", "Idle"],
  };
  const st = (s, label, pill) => `<span class="st ${s}${pill ? " pill" : ""}">${ic(ST[s][0], s === "run" ? "spin" : "")}${esc(label ?? ST[s][1])}</span>`;
  const stateLabel = { proposed: "Awaiting approval", queued: "Queued", running: "Running", paused: "Paused", blocked: "Blocked by policy", done: "Done", failed: "Failed", denied: "Denied" };
  const stateSt = { proposed: "warn", queued: "idle", running: "run", paused: "warn", blocked: "crit", done: "ok", failed: "crit", denied: "crit" };

  const agent = (id) => { const a = D.agents[id]; return `<span class="agent"><span class="av" style="background:${a.color}">${ic(a.icon)}</span>${esc(a.name)}</span>`; };
  const person = (id) => { const p = D.people[id]; if (!p) return agent(id); return `<span class="agent"><span class="av human" style="background:${p.color}">${p.initials}</span>${esc(p.name)}</span>`; };

  const scopeKinds = ["Tenant", "Org", "Site", "Env"];
  // tenantInSidebar: a page that shows and switches the tenant in exactly one place (Dispatch) sets this;
  // every other display then starts at Org and lists only the selected tenant's resources and people
  const opts = { tenantInSidebar: false };
  const curTenant = () => D.tenants.find((t) => t.name === curScope[0]) ?? D.tenants[0];
  const inTenant = (w) => !opts.tenantInSidebar || w.scope[0] === curTenant().name;
  // example data: every listed service belongs to the first tenant
  const tenantServices = () => (opts.tenantInSidebar && curTenant() !== D.tenants[0] ? [] : D.services);
  const scopeText = (arr) => (opts.tenantInSidebar ? arr.slice(1) : arr).join(" › ");
  let curScope = [];
  const scope = (arr) => { curScope = arr; const from = opts.tenantInSidebar ? 1 : 0; return `<nav class="scope" aria-label="Current scope">${arr.map((v, i) => i < from ? "" :
    `${i > from ? `<span class="scope-sep">${ic("chevron-right")}</span>` : ""}<button class="scope-chip${i === 3 ? " env" : ""}" data-level="${i}" ${i === 3 ? `data-env="${esc(v)}"` : ""} aria-haspopup="listbox" title="Change ${scopeKinds[i].toLowerCase()}"><small>${scopeKinds[i]}</small><b>${esc(v)}</b>${ic("chevron-down")}</button>`).join("")}</nav>`; };
  // options at each level, given the parents already chosen; deeper levels reset to their first option
  function scopeOptions(level, s) {
    const t = D.tenants.find((x) => x.name === s[0]) ?? D.tenants[0];
    const o = t.orgs.find((x) => x.name === s[1]) ?? t.orgs[0];
    const site = o.sites.find((x) => x.name === s[2]) ?? o.sites[0];
    return [D.tenants.map((x) => x.name), t.orgs.map((x) => x.name), o.sites.map((x) => x.name), site.envs][level];
  }
  function resolveScope(level, value) {
    const s = curScope.slice(0, level).concat(value);
    for (let l = level + 1; l < 4; l++) s.push(scopeOptions(l, s)[0]);
    return s;
  }
  function scopeMenu(chip) {
    document.querySelector(".scope-menu")?.remove();
    const level = +chip.dataset.level, r = chip.getBoundingClientRect();
    const m = document.createElement("div"); m.className = "scope-menu card"; m.setAttribute("role", "listbox");
    m.style.left = Math.min(r.left, innerWidth - 230) + "px"; m.style.top = r.bottom + 4 + "px";
    m.innerHTML = `<div class="label" style="padding:6px 10px 4px">${scopeKinds[level]}</div>` + scopeOptions(level, curScope).map((v) => `<button role="option" aria-selected="${v === curScope[level]}" data-pick="${esc(v)}">${esc(v)}${v === curScope[level] ? ic("check") : ""}</button>`).join("");
    document.body.appendChild(m); icons(); m.querySelector("button[aria-selected=true]")?.focus();
    m.addEventListener("click", (e) => { const b = e.target.closest("[data-pick]"); if (!b) return; m.remove(); window.onScopeChange?.(resolveScope(level, b.dataset.pick)); });
  }
  document.addEventListener("click", (e) => {
    const qh = e.target.closest("[data-qhere]");
    if (qh) { e.preventDefault(); e.stopPropagation(); window.onQueueHere?.(qh.dataset.qhere, qh.dataset.node); return; }
    const chip = e.target.closest(".scope-chip");
    if (chip) { e.stopPropagation(); scopeMenu(chip); return; }
    if (!e.target.closest(".scope-menu")) document.querySelector(".scope-menu")?.remove();
  }, true);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") document.querySelector(".scope-menu, .drawer")?.remove(); });
  function findHost(id) {
    for (const t of D.tenants) for (const o of t.orgs) for (const s of o.sites) for (const c of s.clusters) for (const h of c.hosts)
      if (h.id === id) return { h, c, path: [t.name, o.name, s.name, c.name] };
    return null;
  }

  // ---------- work ----------
  const stepCls = { done: "done", run: "run", fail: "fail", wait: "wait", todo: "" };
  const steps = (w) => `<div class="steps" aria-hidden="true">${w.steps.map((s) => `<i class="${stepCls[s[1]]}"></i>`).join("")}</div>`;
  const strip = (w, sel) => {
    const bar = { proposed: "warn", queued: "", running: "run", paused: "warn", blocked: "crit", done: "ok", failed: "crit", denied: "crit" }[w.state];
    return `<article class="strip" role="button" tabindex="0" data-key="${w.key}" aria-selected="${!!sel}">
      <div class="strip-bar ${bar}"></div>
      <div class="strip-b">
        <div class="strip-top"><span class="strip-key">${w.key}</span><span class="strip-title">${esc(w.title)}</span>${st(stateSt[w.state], stateLabel[w.state])}</div>
        <div class="strip-meta">${agent(w.agent)}<span>${ic("crosshair")} ${esc(D.svc(w.target)?.name ?? w.target)}</span><span class="mono">${esc(w.scope.slice(2).join(" › "))}</span>${w.sem ? `<span class="mono">#${w.sem}</span>` : ""}<span class="faint">${w.age} ago</span></div>
        ${steps(w)}
      </div></article>`;
  };

  const tst = { done: "ok", run: "run", fail: "crit", wait: "warn", todo: "idle" };
  const tlabel = { done: "Done", run: "Running", fail: "Failed", wait: "Waiting", todo: "Pending" };
  const policyBox = (o) => {
    const cls = o.decision === "deny" ? "deny" : o.cond ? "cond" : "";
    const icon = o.decision === "deny" ? "shield-alert" : "shield-check";
    return `<div class="policy ${cls}">${ic(icon)}<div><b>OPA ${o.decision === "deny" ? "denied" : "allowed"}</b> · ${esc(o.rule)}${o.cond ? `<div class="muted">${esc(o.cond)}</div>` : ""}</div></div>`;
  };
  // ---------- vertical workflow: one stepper for executing work and for drafting plans ----------
  // Each step names the role that owns it (skynet sub-agents, OPA, you); the execution step expands into its Semaphore workflows.
  const INFRA = new Set(["proxmox", "netbox", "caddy", "gha"]), SECURITY = new Set(["authentik", "openbao", "opa"]);
  function roleFor(name, w) {
    const n = name.toLowerCase();
    if (w.agent === "schedule") return "Semaphore schedule";
    if (/parse|plan/.test(n)) return "skynet";
    if (/policy|opa/.test(n)) return "security-agent";
    if (/approv/.test(n)) return "You · Admin approval";
    if (/window/.test(n)) return "Change window";
    if (/verify|confirm|test|replay|evidence/.test(n)) return "o11y-agent";
    const t = w.target;
    if (SECURITY.has(t)) return "security-agent";
    if (INFRA.has(t) || /firewall|egress|\bvm\b/i.test(w.title)) return "infra-agent"; // by target first; title words only for host-level changes
    return "service-agent";
  }
  const currentRole = (w) => { const c = w.steps.find((x) => x[1] !== "done"); return c ? c[3] ?? roleFor(c[0], w) : null; };
  const roleOf = (step, w) => step[3] ?? roleFor(step[0], w);
  const EXEC_RE = /semaphore|deploy|redeploy|apply|resize|create|mint|back up|collect|snapshot|analy|write|push|draft|merge|checks/i;
  const vIcon = { done: "check", fail: "x" };
  function vsteps(w, mode) {
    const cur = w.steps.findIndex((x) => x[1] !== "done");
    let execIdx = w.steps.findIndex((x) => EXEC_RE.test(x[0])); if (execIdx < 0) execIdx = Math.max(0, w.steps.length - 2);
    return `<ol class="vsteps">${w.steps.map(([name, state, desc, role], i) => {
      const open = i === cur || (mode === "plan" && i === execIdx) || (cur < 0 && i === execIdx);
      const tasks = i === execIdx ? w.tasks : [];
      const body = tasks.length ? `${tasks.map((t) => `<div class="vtask">${ic("clipboard-list")}<span class="grow">${mode === "plan" ? "Will run" : "Semaphore"}: <span class="mono">${esc(t.wf)}</span>${t.name ? `<span class="faint"> · ${esc(t.name)}</span>` : ""}</span>${mode === "plan" ? "" : st(tst[t.st], "")}</div>`).join("")}
        <div class="vreceipt">${w.sem && mode !== "plan" ? `Receipt: Semaphore task <a class="mono" href="#" data-noop>#${w.sem}</a>` : mode === "plan" ? "Runs only after approval" : "No Semaphore task yet"}</div>` : `<div class="vreceipt">${{ done: "Completed", run: "In progress", wait: "Waiting", fail: "Stopped here", todo: "Not started" }[state]}</div>`;
      return `<li class="vstep ${state}"><details ${open ? "open" : ""}><summary><span class="vnum">${vIcon[state] ? ic(vIcon[state]) : i + 1}</span><span class="grow"><b>${esc(name)}</b><span>${esc(role ?? roleFor(name, w))}${desc ? ` · ${esc(desc)}` : ""}</span></span>${ic("chevron-down", "vchev")}</summary><div class="vbody">${body}</div></details></li>`;
    }).join("")}</ol>`;
  }
  const targetKind = (w) => (D.svc(w.target) ? "service" : w.asset ? w.asset.kind.toLowerCase() : "resource");
  const drill = (w) => {
    const cur = w.steps.find((x) => x[1] !== "done"), plan = w.state === "proposed";
    return `<div class="xdetail" data-xkey="${w.key}">
      <div class="res-head"><span class="res-tile idle">${ic("clipboard-list", "i-lg")}</span><div style="min-width:0"><span class="faint" style="font-size:12.5px">Work / ${w.key}</span><h2 class="res-name">${plan ? "Plan detail" : "Execution detail"}</h2></div></div>
      <div class="stack" style="gap:8px"><h2 style="font-size:19px">${esc(w.title)}</h2>
        <div class="row wrap">${st(stateSt[w.state], stateLabel[w.state], true)}<span class="tag">${esc(w.via)}</span></div>
        ${w.ask ? `<p class="muted" style="margin:0">“${esc(w.ask)}” · ${esc(D.people[w.by]?.name ?? w.by)}</p>` : ""}</div>
      <dl class="facts res-facts"><dt>Assigned role</dt><dd>${esc(cur ? roleOf(cur, w) : w.role ?? "o11y-agent")}</dd><dt>Orchestrator</dt><dd>${w.agent === "schedule" ? "Semaphore schedule" : "skynet"}</dd>
        <dt>Target</dt><dd>${esc(D.svc(w.target)?.name ?? w.target)} · ${targetKind(w)}</dd><dt>Scope</dt><dd class="mono">${esc(scopeText(w.scope))}</dd><dt>Requested</dt><dd>${esc(D.people[w.by]?.name ?? w.by)} · ${w.age}${w.age === "now" ? "" : " ago"}</dd></dl>
      ${policyBox(w.opa)}
      <div class="stack" style="gap:4px"><h3>Agent → workflow → tasks</h3><span class="faint" style="font-size:12.5px">${plan ? "Drafted plan · runs only after approval" : `${w.tasks.length} ${w.tasks.length === 1 ? "workflow" : "workflows"} · example execution`}</span>${vsteps(w, plan ? "plan" : "exec")}</div>
      ${w.log ? `<div><div class="row" style="margin-bottom:6px"><span class="label grow">Live output · Semaphore #${w.sem}</span>${w.state === "running" ? `<span class="st run">${ic("circle-dot", "pulse")}Streaming</span>` : ""}</div><div class="log">${w.log.map((l) => `<span class="${l[0]}">${esc(l[1])}</span>`).join("\n")}</div></div>` : ""}
      <div class="row wrap">${w.state === "proposed" ? `<button class="btn primary" data-act="approve" data-key="${w.key}">${ic("check")}Approve and queue</button>` : ""}
        ${w.state === "failed" ? `<button class="btn primary" data-act="retry" data-key="${w.key}">${ic("rotate-ccw")}Ask skynet to retry</button>` : ""}
        ${w.state === "blocked" ? `<button class="btn primary" data-act="policy" data-key="${w.key}">${ic("file-code-2")}Propose policy change</button>` : ""}
        ${w.state === "running" ? `<button class="btn" data-act="pause" data-key="${w.key}">${ic("circle-pause")}Pause after current task</button>` : ""}
        ${w.state === "paused" ? `<button class="btn primary" data-act="resume" data-key="${w.key}">${ic("play")}Resume</button>` : ""}
        <button class="btn ghost" data-grafana-scope="work=${w.key} · metrics">${ic("activity")}Grafana</button><button class="btn ghost" data-noop-sem>${ic("external-link")}Open in Semaphore</button></div>
      ${["running", "queued"].includes(w.state) ? `<div><button type="button" class="btn res-assess xadvance" data-advance="${w.key}">${ic("arrow-right")}Advance demo one step</button><p class="faint" style="margin:6px 0 0;font-size:12px">Demo control only. Production progress comes from skynet and verified Semaphore receipts.</p></div>` : ""}
    </div>`;
  };
  // demo: move a work item one step along its workflow
  function advance(w) {
    const i = w.steps.findIndex((x) => x[1] !== "done"); if (i < 0 || w.steps[i][1] === "fail") return;
    if (w.steps[i][1] === "todo") w.steps[i][1] = "run"; else { w.steps[i][1] = "done"; if (w.steps[i + 1]) w.steps[i + 1][1] = "run"; }
    const execIdx = w.steps.findIndex((x) => EXEC_RE.test(x[0]));
    if (execIdx >= 0 && w.steps[execIdx][1] === "done") w.tasks.forEach((t) => (t.st = "done"));
    else if (execIdx >= 0 && w.steps[execIdx][1] === "run") { const t = w.tasks.find((x) => x.st !== "done"); if (t) t.st = "run"; }
    if (w.steps.every((x) => x[1] === "done")) { w.state = "done"; w.tasks.forEach((t) => (t.st = "done")); } else w.state = "running";
  }
  document.addEventListener("click", (e) => {
    const b = e.target.closest("[data-advance]"); if (!b) return;
    const w = D.work.find((x) => x.key === b.dataset.advance); if (!w) return;
    advance(w);
    document.querySelectorAll(`.xdetail[data-xkey="${w.key}"]`).forEach((el) => { el.outerHTML = drill(w); });
    after(document.querySelector(".drawer") ?? document);
    window.onWorkAdvanced?.(w); window.onWorkChanged?.();
    toast(w.state === "done" ? `${w.key} finished; evidence recorded.` : `${w.key}: ${w.steps.find((x) => x[1] === "run")?.[0] ?? "next step"}`, "arrow-right");
  });

  // ---------- natural-language request → proposal ----------
  let seq = 1188;
  function parse(text, sc) {
    const t = text.toLowerCase();
    const base = { key: `AC-${seq++}`, state: "proposed", st: "warn", agent: "skynet", by: "you", via: "Natural language", age: "now", ask: text, scope: sc };
    if (/key|llm|token|gateway|model/.test(t)) return { ...base, title: "Provision an LLM key", target: "agentgateway",
      opa: { decision: "allow", rule: "Budget within the requester's ceiling", cond: "New client identity needs one Admin approval" },
      steps: [["Parse request", "done"], ["Plan (skynet)", "done"], ["Policy (OPA)", "done"], ["Approval", "wait"], ["Semaphore", "todo"]],
      tasks: [{ name: "Add client identity to inventory", wf: "site-config PR (inventory)", st: "wait" }, { name: "Re-render gateway config", wf: "Deploy agentgateway", st: "todo" }] };
    if (/core|cpu|memory|ram|resize|bigger/.test(t)) return { ...base, title: "Resize a VM", target: "proxmox",
      opa: { decision: "allow", rule: "Grow-only resize within cluster headroom" },
      steps: [["Parse request", "done"], ["Plan", "done"], ["Policy (OPA)", "done"], ["Window", "wait"], ["Resize VM", "todo"]],
      tasks: [{ name: "Converge declared spec", wf: "Resize VM", st: "todo" }] };
    if (/firewall|egress|port|block/.test(t)) return { ...base, title: "Change a host firewall", target: "proxmox",
      opa: { decision: "allow", rule: "Declared firewall change", cond: "Production: runs in the change window" },
      steps: [["Parse request", "done"], ["Plan (skynet)", "done"], ["Policy (OPA)", "done"], ["Window", "wait"], ["Apply", "todo"]],
      tasks: [{ name: "Apply default-deny + declared rules", wf: "Apply Firewall", st: "todo" }] };
    if (/user|access|invite|add .* to/.test(t)) return { ...base, title: "Grant access", target: "authentik",
      opa: { decision: "allow", rule: "Role assignment through an Authentik group" },
      steps: [["Parse request", "done"], ["Plan", "done"], ["Policy (OPA)", "done"], ["Approval", "wait"], ["Apply blueprint", "todo"]],
      tasks: [{ name: "Add member to group", wf: "Deploy Authentik (blueprints)", st: "todo" }] };
    return { ...base, title: text.length > 54 ? text.slice(0, 52) + "…" : text, target: "postiz",
      opa: { decision: "allow", rule: "Redeploy of a declared service" },
      steps: [["Parse request", "done"], ["Plan (skynet)", "done"], ["Policy (OPA)", "done"], ["Approval", "wait"], ["Semaphore", "todo"]],
      tasks: [{ name: "Inspect, then redeploy", wf: "Inspect Service Runtime → Deploy Postiz", st: "todo" }] };
  }
  const proposal = (w, opts = {}) => `<div class="stack">
      <div class="row wrap"><span class="strip-key">${w.key}</span><span class="tag accent">${ic("sparkles")}Proposal</span><span class="grow"></span><span class="faint" style="font-size:12px">${esc(scopeText(w.scope))}</span></div>
      <h2>${esc(w.title)}</h2>
      ${w.ask ? `<p class="muted" style="margin:0">“${esc(w.ask)}”</p>` : ""}
      <dl class="facts res-facts"><dt>Orchestrator</dt><dd>skynet</dd><dt>Target</dt><dd>${esc(D.svc(w.target)?.name ?? w.target)} · ${targetKind(w)}</dd><dt>Scope</dt><dd class="mono">${esc(scopeText(w.scope))}</dd></dl>
      <div class="stack" style="gap:4px"><h3>Plan: agent → workflow → tasks</h3><span class="faint" style="font-size:12.5px">Nothing runs until you approve</span>${vsteps(w, "plan")}</div>
      ${policyBox(w.opa)}
      ${opts.actions !== false ? `<div class="row wrap"><button class="btn primary" data-act="approve" data-key="${w.key}">${ic("check")}Approve and queue</button><button class="btn" data-act="edit">${ic("pencil")}Edit plan</button><button class="btn ghost" data-act="discard" data-key="${w.key}">Discard</button></div>` : ""}
    </div>`;

  // ---------- telemetry ----------
  function spark(vals, { color = "var(--accent)", h = 44, w = 240, unit = "" } = {}) {
    const max = Math.max(...vals) * 1.15 || 1, step = w / (vals.length - 1);
    const pts = vals.map((v, i) => [+(i * step).toFixed(1), +(h - (v / max) * (h - 4) - 2).toFixed(1)]);
    const d = pts.map((p, i) => (i ? "L" : "M") + p.join(",")).join("");
    const last = pts[pts.length - 1];
    return `<svg class="spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" data-vals="${vals.join(",")}" data-unit="${esc(unit)}" role="img" aria-label="Last ${vals.length} samples, latest ${vals[vals.length - 1]}${esc(unit)}">
      <line class="grid" x1="0" x2="${w}" y1="${h - 1}" y2="${h - 1}"/><line class="grid" x1="0" x2="${w}" y1="${(h / 2).toFixed(0)}" y2="${(h / 2).toFixed(0)}" stroke-dasharray="2 3"/>
      <path class="area" d="${d}L${w},${h}L0,${h}Z" fill="${color}"/><path class="ln" d="${d}" stroke="${color}" vector-effect="non-scaling-stroke"/>
      <circle cx="${last[0]}" cy="${last[1]}" r="3" fill="${color}" stroke="var(--raised)" stroke-width="2" vector-effect="non-scaling-stroke"/></svg>`;
  }
  const grafanaNote = `<div class="grafana-note">${ic("info")}Prototype draws synthetic series. In the console this panel is a Prometheus query rendered natively, with “Open in Grafana” deep-linking to the same query.</div>`;
  function bindSparks(root) {
    root.querySelectorAll("svg.spark").forEach((svg) => {
      if (svg.dataset.bound) return; svg.dataset.bound = 1;
      const vals = svg.dataset.vals.split(",").map(Number), unit = svg.dataset.unit, host = svg.parentElement;
      let tip;
      svg.addEventListener("pointermove", (e) => {
        const r = svg.getBoundingClientRect(), i = Math.round(((e.clientX - r.left) / r.width) * (vals.length - 1));
        if (!tip) { tip = document.createElement("div"); tip.className = "tip"; host.appendChild(tip); }
        const hr = host.getBoundingClientRect();
        tip.textContent = `−${(vals.length - 1 - i) * 30}s · ${vals[Math.max(0, Math.min(i, vals.length - 1))]}${unit}`;
        tip.style.left = e.clientX - hr.left + "px"; tip.style.top = r.top - hr.top + "px";
      });
      svg.addEventListener("pointerleave", () => { tip?.remove(); tip = null; });
    });
  }

  // ---------- topology (rows = the four-layer guardrails model) ----------
  const LANES = [["ai", "AI"], ["guard", "Guardrails"], ["auto", "Automation"], ["plat", "Platform"]];
  const ORDER = { ai: ["honcho", "skynet", "agentgateway", "vllm"], guard: ["authentik", "opa", "openbao"], auto: ["gha", "semaphore", "n8n"], plat: ["netbox", "proxmox", "o11y", "caddy", "tududi", "postiz"] };
  const EDGE_LABEL = { api: "API", oidc: "OIDC / SSO", mcp: "MCP", logs: "Logs", metrics: "Metrics", traces: "Traces (planned)" };
  const DATA_FLOW = new Set(["logs", "metrics", "traces"]);
  function layout(W = 1000) {
    const LH = 124, LX = 104, NW = 128, NH = 60, pos = {};
    LANES.forEach(([id], li) => {
      const ids = ORDER[id], slot = (W - LX - 16) / ids.length;
      ids.forEach((s, i) => { pos[s] = { x: LX + slot * i + (slot - NW) / 2, y: li * LH + (LH - NH) / 2, w: NW, h: NH, lane: li }; });
    });
    return { pos, LH, W, H: LANES.length * LH };
  }
  function edgePath(a, b) {
    const ax = a.x + a.w / 2, bx = b.x + b.w / 2;
    if (a.lane === b.lane) { const y = a.y, lift = 26 + Math.abs(ax - bx) * .08; return `M${ax},${y}C${ax},${y - lift} ${bx},${y - lift} ${bx},${y}`; }
    const down = b.lane > a.lane, ay = down ? a.y + a.h : a.y, by = down ? b.y : b.y + b.h, my = (ay + by) / 2;
    return `M${ax},${ay}C${ax},${my} ${bx},${my} ${bx},${by}`;
  }
  function topology({ selected } = {}) {
    const { pos, LH, W, H } = layout();
    const lanes = LANES.map(([id, label], i) => `<rect class="${i % 2 ? "lane-alt" : "lane"}" x="0" y="${i * LH}" width="${W}" height="${LH}"/><text class="lane-label" x="16" y="${i * LH + LH / 2 + 4}">${label}</text>`).join("");
    const edges = D.edges.map(([a, b, t, note], i) => `<path class="edge ${t}" data-a="${a}" data-b="${b}" d="${edgePath(pos[a], pos[b])}"><title>${esc(D.svc(a).name)} → ${esc(D.svc(b).name)} · ${EDGE_LABEL[t]}: ${esc(note)}</title></path>`).join("");
    const nodes = D.services.map((s) => { const p = pos[s.id]; return `<g class="n ${s.st}${s.id === selected ? " sel" : ""}" data-id="${s.id}" tabindex="0" role="button" aria-label="${esc(s.name)}, ${ST[s.st][1]}" transform="translate(${p.x.toFixed(1)},${p.y.toFixed(1)})">
        <rect width="${p.w}" height="${p.h}" rx="8"/><circle class="sd ${s.st}" cx="14" cy="17" r="4.5"/>
        <text x="25" y="21">${esc(s.name)}</text><text class="sub" x="14" y="36">${esc((s.st === "ok" ? s.kind : ST[s.st][1] + " · " + s.kind).slice(0, 24))}</text><text class="loc" x="14" y="51">${esc(s.where.slice(0, 22))}</text><title>${esc(s.name)} · ${esc(s.where)}</title></g>`; }).join("");
    return `<div class="topo-wrap"><svg class="topo" viewBox="0 0 ${W} ${H}" role="group" aria-label="Service dependency map">${lanes}${edges}${nodes}</svg></div>`;
  }
  // data flow is one hue; logs / metrics / traces are its subsets, told apart by dash pattern
  const LEG_DASH = { oidc: "stroke-dasharray:6 3", mcp: "stroke-dasharray:2 3;stroke-linecap:round", logs: "stroke-dasharray:10 4", metrics: "stroke-dasharray:4 3", traces: "stroke-dasharray:1 4;stroke-linecap:round;opacity:.6" };
  const legItem = ([t, l]) => `<label><input type="checkbox" checked data-edge="${t}"><svg viewBox="0 0 26 8"><path d="M1,4H25" style="fill:none;stroke-width:2;stroke:var(--e-${DATA_FLOW.has(t) ? "data" : t});${LEG_DASH[t] ?? ""}"/></svg>${l}</label>`;
  const legend = () => `<div class="legend" role="group" aria-label="Integration types">${Object.entries(EDGE_LABEL).filter(([t]) => !DATA_FLOW.has(t)).map(legItem).join("")}
    <span class="legend-group" role="group" aria-label="Data flow"><b>Data flow</b>${Object.entries(EDGE_LABEL).filter(([t]) => DATA_FLOW.has(t)).map(legItem).join("")}</span></div>`;
  function bindTopology(root, onSelect) {
    const svg = root.querySelector("svg.topo"); if (!svg) return;
    const focus = (id) => {
      svg.querySelectorAll(".n").forEach((n) => n.classList.toggle("sel", n.dataset.id === id));
      const near = new Set([id]);
      svg.querySelectorAll(".edge").forEach((e) => { const hot = e.dataset.a === id || e.dataset.b === id; e.classList.toggle("hot", hot); if (hot) { near.add(e.dataset.a); near.add(e.dataset.b); } });
      svg.querySelectorAll(".n").forEach((n) => n.classList.toggle("hot", near.has(n.dataset.id)));
      svg.classList.add("dim"); onSelect?.(id);
    };
    svg.addEventListener("click", (e) => { const n = e.target.closest(".n"); if (n) focus(n.dataset.id); else { svg.classList.remove("dim"); svg.querySelectorAll(".sel").forEach((x) => x.classList.remove("sel")); onSelect?.(null); } });
    svg.addEventListener("keydown", (e) => { const n = e.target.closest(".n"); if (n && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); focus(n.dataset.id); } });
    root.querySelectorAll("[data-edge]").forEach((cb) => cb.addEventListener("change", () => svg.classList.toggle("hide-" + cb.dataset.edge, !cb.checked)));
    return focus;
  }

  // ---------- hierarchy with status rollups ----------
  const rank = { crit: 3, warn: 2, ok: 1, idle: 0 };
  function hosts(node) { return node.hosts ?? (node.clusters ?? node.sites ?? node.orgs ?? []).flatMap(hosts); }
  function roll(node) { const c = { ok: 0, warn: 0, crit: 0 }; hosts(node).forEach((h) => c[h.st]++); const worst = c.crit ? "crit" : c.warn ? "warn" : "ok"; return { c, worst }; }
  const rollup = (c) => `<span class="rollup"><span title="Healthy">${ic("circle-check")}${c.ok}</span>${c.warn ? `<span title="Degraded" style="color:var(--fg)">${ic("triangle-alert")}${c.warn}</span>` : ""}${c.crit ? `<span title="Down" style="color:var(--fg)">${ic("octagon-x")}${c.crit}</span>` : ""}</span>`;
  function hier({ compact } = {}) {
    const q = (kind, name, id = "") => `<button class="qhere" type="button" data-qhere="${esc(kind)}: ${esc(name)}" data-node="${esc(id)}" title="Queue work on this ${kind.toLowerCase()}" aria-label="Queue work on ${esc(name)}">${ic("plus")}</button>`;
    const row = (kind, name, node, extra = "") => { const r = roll(node); return `<summary class="hrow"><span class="dot ${r.worst}"></span><span class="hk">${kind}</span><span class="grow hn">${esc(name)}</span>${extra}${compact ? "" : rollup(r.c)}${q(kind, name, node.id)}</summary>`; };
    const host = (h) => `<li class="hleaf" tabindex="0" data-host="${h.id}"><span class="dot ${h.st}"></span><span class="hk">Host</span><span class="grow hn mono">${esc(h.name)}</span>${h.st !== "ok" ? st(h.st, h.st === "crit" ? "Down" : "Degraded") : `<span class="faint" style="font-size:12px">${h.vms ? h.vms + " VMs" : esc(h.role ?? "")}</span>`}${q("Host", h.name, h.id)}</li>`;
    const orgs = (t) => t.orgs.map((o) => `<details open>${row("Org", o.name, o)}${o.sites.map((s) => `<details ${compact ? "" : "open"}>${row("Site", s.name, s, `<span class="faint mono" style="font-size:11px">${s.provider ? esc(s.provider) + " · " : ""}${s.envs.join(" · ")}</span>`)}${s.clusters.map((c) => `<details open>${row("Cluster", c.name, c, `<span class="tag">${esc(c.kind)}</span>`)}<ul class="list">${c.hosts.map(host).join("")}</ul></details>`).join("")}</details>`).join("")}</details>`).join("");
    if (opts.tenantInSidebar) return `<div class="hier">${orgs(D.tenants.find((t) => t.name === curScope[0]) ?? D.tenants[0])}</div>`;
    return `<div class="hier">${D.tenants.map((t) => `<details open><summary class="hrow t"><span class="dot ${roll(t).worst}"></span><span class="hk">Tenant</span><span class="grow hn">${esc(t.name)}${t.synthetic ? ' <span class="faint">(example)</span>' : ""}</span>${compact ? "" : rollup(roll(t).c)}${q("Tenant", t.name, t.id)}</summary>
      ${orgs(t)}</details>`).join("")}</div>`;
  }

  // ---------- review aid: component tags + picks ----------
  const comp = (cid, name, html, cls = "", tag = "section") => `<${tag} class="comp ${cls}" data-cid="${cid}" data-cname="${esc(name)}">${html}</${tag}>`;
  const P = { proto: "?", kept: new Map(), db: null, uid: null, mode: "local" };
  const sid = (s) => s.replace(/[^A-Za-z0-9_-]/g, "-");
  function loadLocal() { try { P.kept = new Map(JSON.parse(localStorage.getItem("picks-" + P.proto) || "[]")); } catch { P.kept = new Map(); } }
  function saveLocal() { try { localStorage.setItem("picks-" + P.proto, JSON.stringify([...P.kept])); } catch {} }
  function paintPicks() {
    document.querySelectorAll(".comp[data-cid]").forEach((el) => {
      let t = el.querySelector(":scope > .ctag");
      if (!t) { t = document.createElement("div"); t.className = "ctag"; t.innerHTML = `<span>${esc(el.dataset.cid)}</span><button type="button"></button>`; el.prepend(t);
        t.querySelector("button").addEventListener("click", (e) => { e.stopPropagation(); togglePick(el.dataset.cid, el.dataset.cname); }); }
      const on = P.kept.has(el.dataset.cid);
      t.classList.toggle("kept", on);
      t.querySelector("button").innerHTML = `${ic(on ? "check" : "plus")}${on ? "Kept" : "Keep"}`;
      t.querySelector("button").setAttribute("aria-label", `${on ? "Remove" : "Keep"} ${el.dataset.cname}`);
    });
    document.querySelectorAll("[data-picks-count]").forEach((c) => (c.textContent = P.kept.size));
    icons();
  }
  async function togglePick(cid, name) {
    const on = !P.kept.has(cid);
    if (on) P.kept.set(cid, name); else P.kept.delete(cid);
    paintPicks();
    if (P.db && P.uid) {
      const ref = P.db.collection("picks").doc(sid(`${cid}__${P.uid}`));
      try { on ? await ref.set({ cid, name, proto: P.proto, uid: P.uid, at: Date.now() }) : await ref.delete(); }
      catch (e) { toast("Couldn't save that pick. It is kept in this browser only."); saveLocal(); }
    } else saveLocal();
  }
  async function initPicks(proto) {
    P.proto = proto; loadLocal(); paintPicks();
    try {
      if (!window.claude?.use) return;
      const [db, user] = await Promise.all([claude.use("db"), claude.use("user")]);
      if (!db) return;
      P.uid = (user && (await user.id())) || "viewer"; P.db = db;
      db.collection("picks").where("uid", "==", P.uid).onSnapshot((snap) => {
        P.kept = new Map(snap.docs.map((d) => [d.data().cid, d.data().name])); P.mode = "shared"; paintPicks();
      }, () => {});
    } catch {}
  }
  function picksDrawer() {
    const d = document.createElement("aside"); d.className = "drawer"; d.setAttribute("aria-label", "Kept components");
    const items = [...P.kept];
    d.innerHTML = `<div class="card-h"><h2>Kept from prototype ${P.proto}</h2><button class="iconbtn" data-close aria-label="Close">${ic("x")}</button></div>
      <div class="drawer-b stack">${items.length ? `<ul class="list">${items.map(([cid, n]) => `<li class="row" style="padding:8px 0"><span class="grow"><b>${esc(n)}</b><br><span class="mono faint">${esc(cid)}</span></span><button class="btn sm ghost" data-unpick="${esc(cid)}">Remove</button></li>`).join("")}</ul>`
        : `<div class="empty">${ic("mouse-pointer-click", "i-lg")}<b>Nothing kept yet</b>Every component has a dashed tag on its top edge. Press <span class="kbd">Keep</span> on the ones you want in the real console.</div>`}
      <p class="faint" style="font-size:12px;margin:0">${P.mode === "shared" ? "Picks are saved to this artifact, so Claude can read them back." : "Picks are saved in this browser only."} Use comment mode for anything you want changed.</p></div>`;
    document.body.appendChild(d); icons();
    d.addEventListener("click", (e) => { if (e.target.closest("[data-close]")) d.remove(); const u = e.target.closest("[data-unpick]"); if (u) { togglePick(u.dataset.unpick, ""); d.remove(); picksDrawer(); } });
  }

  // ---------- misc ----------
  function toast(msg, icon = "check") {
    document.querySelector(".toast")?.remove();
    const t = document.createElement("div"); t.className = "toast"; t.setAttribute("role", "status"); t.innerHTML = `${ic(icon)}<span>${esc(msg)}</span>`;
    document.body.appendChild(t); icons(); setTimeout(() => t.remove(), 3200);
  }
  function icons() { try { window.lucide?.createIcons(); } catch {} }
  function drawer(title, html) {
    document.querySelector(".drawer")?.remove();
    const d = document.createElement("aside"); d.className = "drawer";
    d.innerHTML = `<div class="card-h"><h2>${esc(title)}</h2><button class="iconbtn" data-close aria-label="Close">${ic("x")}</button></div><div class="drawer-b">${html}</div>`;
    document.body.appendChild(d); d.addEventListener("click", (e) => { if (e.target.closest("[data-close]")) d.remove(); }); after(d); return d;
  }
  function after(root = document) { icons(); bindSparks(root); paintPicks(); root.querySelectorAll("a[data-noop]").forEach((a) => (a.onclick = (e) => { e.preventDefault(); toast("Opens this task in Semaphore", "external-link"); })); }

  const NAV = [
    ["home", "Home", "house"], ["work", "Work", "list-todo"], ["topology", "Topology", "waypoints"], ["services", "Services", "boxes"],
    ["compute", "Compute", "server"], ["access", "Access", "users"], ["secrets", "Secrets", "key-round"], ["policy", "Policy", "scale"],
    ["llm", "LLM access", "message-square-lock"], ["agents", "AI Gateway", "door-open"], ["cicd", "CI/CD", "git-pull-request-arrow"], ["telemetry", "Telemetry", "activity"],
  ];
  const authority = (name, icon) => `<span class="authority" title="Source of truth for this view">${ic(icon)}Source of truth: ${esc(name)}</span>`;
  const head = (title, icon, sub, auth, actions = "") => `<div class="view-h"><h1>${ic(icon, "i-lg")}${esc(title)}</h1>${auth}${actions}<p>${sub}</p></div>`;
  const siteBanner = () => (opts.tenantInSidebar && curScope[2] ? scopeBanner({ kind: "Site", name: curScope[2] }) : "");

  // Fine-grained observability (one service, node, host or VM) lives in Grafana; the console links there, pre-filtered.
  // Only tenant/org/site/cluster rollups are drawn in the console itself (Telemetry page).
  const grafanaLinks = (kind, name) => `<div class="stack" style="gap:6px"><span class="label">Observability</span><div class="row wrap"><button class="btn sm" data-grafana-scope="${kind}=${esc(name)} · metrics">${ic("activity")}Metrics in Grafana</button><button class="btn sm" data-grafana-scope="${kind}=${esc(name)} · logs">${ic("scroll-text")}Logs in Grafana</button></div><span class="faint" style="font-size:12px">Charts for a single ${kind} live in Grafana. These open it filtered to ${esc(name)}.</span></div>`;

  // ---------- service placement + detail ----------
  const hostCell = (s) => `<div class="hostcell"><b class="mono">${esc(s.host)}</b><span class="mono">${s.vm ? `VM ${esc(s.vm)}` : "not a VM"} · ${esc(s.node)}</span><span class="mono faint">${esc(s.dns)}</span><span class="mono faint">${esc(s.ip)}</span></div>`;
  const EDGE_SHORT = { api: "API", oidc: "OIDC", mcp: "MCP", logs: "Logs", metrics: "Metrics", traces: "Traces · planned" };
  function serviceDetail(id) {
    const s = D.svc(id), ins = D.edges.filter((e) => e[1] === id), outs = D.edges.filter((e) => e[0] === id);
    const work = D.work.filter((w) => w.target === id), seed = id.length * 7;
    const secret = D.secrets.find((x) => x.path === `secret/services/${id}`);
    const layer = LANES.find((l) => l[0] === s.layer)[1];
    return comp("K.service-detail", "Service detail drill-down", `<div class="drill">
      <div class="stack" style="gap:6px"><span class="label">${esc(s.kind)} · ${layer} layer</span><h2 style="display:flex;gap:8px;align-items:center">${ic(s.icon, "i-lg")}${esc(s.name)}</h2>
        <div class="row wrap">${st(s.st, null, true)}</div>
        ${s.why ? `<div class="policy ${s.st === "crit" ? "deny" : "cond"}">${ic(s.st === "crit" ? "octagon-x" : "triangle-alert")}<span>${esc(s.why)}</span></div>` : ""}
        <div class="row wrap">${s.st !== "ok" ? `<button class="btn primary" data-fix="${esc(s.name)}: ${esc(s.why)}. Fix it.">${ic("sparkles")}Fix with skynet</button>` : ""}${verifyBtn("Service", id, s.st)}<button class="btn" data-act="queue-svc" data-svc="${id}">${ic("plus")}Queue work</button><button class="btn ghost" data-grafana>${ic("external-link")}Grafana</button></div></div>
      ${scopeBanner({ kind: "Service", name: s.name })}
      <div><div class="label" style="margin-bottom:6px">Where it runs</div><dl class="facts">
        <dt>Host</dt><dd class="mono">${esc(s.host)}</dd><dt>VM ID</dt><dd class="mono">${esc(s.vm ?? "— (not a VM)")}</dd><dt>Node</dt><dd class="mono">${esc(s.node)}</dd>
        <dt>DNS</dt><dd class="mono">${esc(s.dns)}</dd><dt>IP</dt><dd class="mono">${esc(s.ip)}</dd><dt>Site</dt><dd>site-hq · prod</dd></dl></div>
      ${grafanaLinks("service", s.name)}
      <div><span class="label">Integrations · ${ins.length + outs.length}</span><ul class="list integ">
        ${outs.map((e) => `<li class="row" style="padding:7px 0"><span class="tag">${EDGE_SHORT[e[2]]}</span><span class="grow">→ <b>${esc(D.svc(e[1]).name)}</b> <span class="faint">${esc(e[3])}</span></span></li>`).join("")}
        ${ins.map((e) => `<li class="row" style="padding:7px 0"><span class="tag">${EDGE_SHORT[e[2]]}</span><span class="grow">← <b>${esc(D.svc(e[0]).name)}</b> <span class="faint">${esc(e[3])}</span></span></li>`).join("") || `<li class="faint" style="padding:7px 0">No recorded integrations</li>`}</ul></div>
      <div><span class="label">Credentials</span><p class="muted" style="margin:4px 0 0;font-size:13px">${secret ? `<span class="mono">${esc(secret.path)}</span> · ${secret.fields} fields · rotated ${secret.rotated} ago. Values are never shown here.` : `No dedicated OpenBao path recorded for this service.`}</p></div>
      <div class="stack" style="gap:8px"><span class="label">Work on ${esc(s.name)}</span>${work.length ? work.map((w) => strip(w)).join("") : `<div class="faint" style="font-size:13px">No work queued.</div>`}</div>
    </div>`, "", "div");
  }
  const openService = (id) => drawer(D.svc(id).name, serviceDetail(id));
  document.addEventListener("click", (e) => {
    const r = e.target.closest("[data-svc-row]");
    if (r && !e.target.closest("button, a")) openService(r.dataset.svcRow);
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Enter" && e.target.matches?.("[data-svc-row]")) openService(e.target.dataset.svcRow); });

  // ---------- request dashboard (Ask's home; also Dispatch's Dashboard) ----------
  const DASH = { target: null };
  function dashHtml() {
    const sc = curScope.length ? curScope : ["Uhstray.io", "Platform", "site-hq", "prod"];
    const attention = [...tenantServices().filter((s) => s.st !== "ok").map((s) => ({ name: s.name, st: s.st, why: s.why, kind: s.kind, verify: `Service:${s.id}`, fix: `${s.name} is ${s.st === "crit" ? "down" : "degraded"}: ${s.why}. Fix it.` })),
      ...(!opts.tenantInSidebar || curTenant().name === "Kestrel Robotics" ? [{ name: "pve-k2", st: "crit", verify: "Node:pve-k2", why: "Node unreachable for 4m (corosync)", kind: opts.tenantInSidebar ? "Proxmox host" : "Proxmox host · Kestrel Robotics", fix: "pve-k2 is unreachable. Diagnose and fence it if needed." }] : [])]
      .sort((a, b) => (b.st === "crit") - (a.st === "crit"));
    const running = D.work.filter((w) => w.state === "running" && inTenant(w));
    const recent = D.work.filter((w) => w.ask && w.state !== "proposed" && inTenant(w)).concat(D.work.filter((w) => ["done", "blocked"].includes(w.state) && !w.ask && inTenant(w))).slice(0, 4);
    return `<div class="dash-home" data-dash>
    <div class="dash-col">
      ${comp("K.composer", "Request composer (natural language)", `<form class="card composer" id="ask">
        <h1>What should agent-cloud do?</h1>
        <div class="composer-box">
          <textarea id="askText" rows="2" placeholder="${DASH.target ? `What should happen on ${esc(DASH.target)}?` : "Describe the change. For example: give the research team an LLM key, 200k tokens an hour"}">${DASH.target ? "" : "Postiz is red. Fix it."}</textarea>
          <div class="composer-foot">
            <span class="tag" title="Requests apply to this target">${ic("crosshair")}${esc(DASH.target ?? scopeText(sc))}</span>
            <span class="tag">${ic("bot")}skynet plans</span><span class="grow"></span>
            <span class="faint" style="font-size:12px">Enter to send</span>
            <button class="btn primary" type="submit">${ic("corner-down-left")}Ask</button>
          </div>
        </div>
        <div class="chips">${["Give the research team an LLM key, 200k tokens an hour", "Add 2 cores to the NetBox VM", "Invite dana@kestrel.example as a developer on site-k1 dev", "Deny egress for gh-runner-02 except GitHub"].map((c) => `<button type="button" class="chip">${esc(c)}</button>`).join("")}</div>
        <div class="flowline">${ic("info")}<b>You ask</b>${ic("arrow-right")}<b>skynet plans</b>${ic("arrow-right")}<b>OPA checks</b>${ic("arrow-right")}<b>you approve</b>${ic("arrow-right")}<b>Semaphore runs it</b></div>
      </form>`)}
      ${proposalQueue()}
      ${comp("K.recent-requests", "Recent requests, each with its work item", `<div class="card"><div class="card-h"><h3>Recent requests</h3><a href="#work" class="btn sm ghost">All work ${ic("arrow-right")}</a></div>
        <ul class="list thread">${recent.map((w) => `<li>${w.ask ? `<div class="quote"><span class="av human" style="background:${D.people[w.by].color}" title="${esc(D.people[w.by].name)}">${D.people[w.by].initials}</span><span>“${esc(w.ask)}”</span></div>` : ""}${strip(w)}</li>`).join("")}</ul></div>`)}
    </div>
    <div class="dash-col">
      ${comp("K.attention", "Needs attention, with one-click fix", `<div class="card"><div class="card-h"><h3>Needs attention</h3><span class="rollup"><span>${ic("octagon-x")}${attention.filter((a) => a.st === "crit").length}</span><span>${ic("triangle-alert")}${attention.filter((a) => a.st === "warn").length}</span></span></div>
        <ul class="list att">${attention.map((a) => `<li>${st(a.st, "")}<b>${esc(a.name)}</b><span class="row" style="gap:4px">${a.verify ? `<button class="btn sm ghost" data-verify="${a.verify}" title="Dispatch o11y-agent to verify health">${ic("heart-pulse")}Verify</button>` : ""}<button class="btn sm" data-dash-fix="${esc(a.fix)}">${ic("sparkles")}Fix</button></span><span class="why">${esc(a.why)} · <span class="faint">${esc(a.kind)}</span></span></li>`).join("")}</ul></div>`)}
      ${comp("K.inflight", "In flight: who is working on what", `<div class="card"><div class="card-h"><h3>In flight</h3><span class="faint num" style="font-size:12px">${running.length} running</span></div><div class="card-b stack" style="gap:8px">${running.map((w) => strip(w)).join("")}</div></div>`)}
      ${comp("K.hier", "Hierarchy with health rollups", `<div class="card"><div class="card-h"><h3>Health by scope</h3></div><div class="card-b">${hier({ compact: true })}</div></div>`)}
    </div></div>`;
  }
  const rerenderDash = () => { const d = document.querySelector("[data-dash]"); if (d) { d.outerHTML = dashHtml(); after(document.getElementById("view") ?? document); } };
  // ---- proposals awaiting approval: expand one to review, comment, deny, edit the plan or approve ----
  const PQ = { open: new Set(), editing: null };
  function proposalQueue() {
    const ps = D.work.filter((w) => w.state === "proposed" && inTenant(w));
    return comp("K.proposal-queue", "Proposals awaiting approval (expand to review)", `<div class="card"><div class="card-h"><h3>Proposals awaiting approval</h3><span class="faint num" style="font-size:12px">${ps.length}</span></div>
      ${ps.length ? `<ul class="list pq">${ps.map(pqRow).join("")}</ul>` : `<div class="empty">${ic("sparkles", "i-lg")}<b>Nothing waiting on approval</b>Ask for something above; the plan lands here before anything runs.</div>`}</div>`);
  }
  function pqRow(w) {
    const open = PQ.open.has(w.key), editing = PQ.editing === w.key;
    return `<li class="pq-row${open ? " open" : ""}"><button type="button" class="pq-head" data-pq-toggle="${w.key}" aria-expanded="${open}">
        <span class="strip-key">${w.key}</span><span class="grow"><b>${esc(w.title)}</b><span class="faint">${esc(scopeText(w.scope))} · ${esc(D.people[w.by]?.name ?? w.by)} · ${w.age}${w.age === "now" ? "" : " ago"}</span></span>
        ${w.opa.decision === "deny" ? st("crit", "OPA denied") : st("ok", "OPA allowed")}${ic("chevron-down", "vchev")}</button>
      ${open ? `<div class="pq-body">
        ${w.ask ? `<p class="muted" style="margin:0">“${esc(w.ask)}”</p>` : ""}${policyBox(w.opa)}
        ${editing ? `<ol class="pq-edit">${w.steps.map((x, i) => `<li><span class="grow">${esc(x[0])} <span class="faint">· ${esc(x[3] ?? roleFor(x[0], w))}</span></span>${x[1] === "todo" ? `<button type="button" class="btn sm ghost" data-pq-rm="${w.key}:${i}" aria-label="Remove step ${esc(x[0])}">${ic("x")}Remove</button>` : `<span class="faint" style="font-size:12px">${x[1] === "done" ? "done" : "in progress"}</span>`}</li>`).join("")}</ol>
          <div class="row wrap"><input class="input grow" id="pq-add-${w.key}" placeholder="Add a step, e.g. “Back up the database first”" style="min-width:200px"><button type="button" class="btn sm" data-pq-addstep="${w.key}">${ic("plus")}Add step</button></div>`
          : vsteps(w, "plan")}
        <div class="pq-comments">${(w.comments ?? []).map((c) => `<div class="quote"><span class="av human" style="background:${D.people[c.who]?.color ?? "#6b7480"}">${D.people[c.who]?.initials ?? "?"}</span><span>${esc(c.text)} <span class="faint">· ${c.at}</span></span></div>`).join("")}
          <div class="row wrap"><input class="input grow" id="pq-c-${w.key}" placeholder="Comment on this proposal" style="min-width:200px"><button type="button" class="btn sm" data-pq-comment="${w.key}">${ic("message-square-text")}Comment</button></div></div>
        <div class="row wrap">${editing ? `<button type="button" class="btn primary" data-pq-save="${w.key}">${ic("check")}Save plan (OPA re-checks)</button><button type="button" class="btn ghost" data-pq-cancel="${w.key}">Cancel</button>`
          : `<button type="button" class="btn primary" data-pq-approve="${w.key}">${ic("check")}Approve</button><button type="button" class="btn" data-pq-editplan="${w.key}">${ic("pencil")}Edit plan</button><button type="button" class="btn danger" data-pq-deny="${w.key}">${ic("thumbs-down")}Deny</button>`}</div>
      </div>` : ""}</li>`;
  }
  document.addEventListener("click", (e) => {
    const t = e.target.closest("[data-pq-toggle],[data-pq-approve],[data-pq-deny],[data-pq-editplan],[data-pq-save],[data-pq-cancel],[data-pq-rm],[data-pq-addstep],[data-pq-comment]"); if (!t) return;
    e.stopPropagation();
    const ds = t.dataset, key = ds.pqToggle ?? ds.pqApprove ?? ds.pqDeny ?? ds.pqEditplan ?? ds.pqSave ?? ds.pqCancel ?? ds.pqAddstep ?? ds.pqComment ?? ds.pqRm.split(":")[0], w = D.work.find((x) => x.key === key);
    if (!w) return;
    if (ds.pqToggle) { if (PQ.open.has(key)) PQ.open.delete(key); else PQ.open.add(key); }
    if (ds.pqApprove) { w.state = "queued"; w.steps.forEach((x) => x[1] === "wait" && (x[1] = "done")); PQ.open.delete(key); toast(`${key} approved. It moves to Queued.`); window.onWorkChanged?.(); }
    if (ds.pqDeny) { w.state = "denied"; w.why = "Denied by You"; (w.comments ??= []).push({ who: "you", text: "Denied.", at: "now" }); PQ.open.delete(key); toast(`${key} denied. Nothing ran.`, "thumbs-down"); window.onWorkChanged?.(); }
    if (ds.pqEditplan) PQ.editing = key;
    if (ds.pqCancel) PQ.editing = null;
    if (ds.pqSave) { PQ.editing = null; (w.comments ??= []).push({ who: "you", text: "Edited the plan. OPA re-checked it: allowed.", at: "now" }); toast(`${key}: plan saved; OPA re-checked it.`); }
    if (ds.pqRm) { const i = +ds.pqRm.split(":")[1]; if (w.steps[i]?.[1] === "todo") w.steps.splice(i, 1); }
    if (ds.pqAddstep) { const v = document.getElementById(`pq-add-${key}`)?.value.trim(); if (v) w.steps.splice(Math.max(0, w.steps.length - 1), 0, [v, "todo"]); }
    if (ds.pqComment) { const v = document.getElementById(`pq-c-${key}`)?.value.trim(); if (v) (w.comments ??= []).push({ who: "you", text: v, at: "now" }); }
    rerenderDash();
  }, true);
  function dashSubmit(text) {
    if (!text.trim()) { toast("Describe what you want changed first.", "info"); return; }
    const w = parse(text, curScope.length ? curScope : ["Uhstray.io", "Platform", "site-hq", "prod"]);
    D.work.unshift(w); PQ.open.add(w.key); rerenderDash(); window.onWorkChanged?.();
    toast(`${w.key} drafted. Review it under Proposals awaiting approval.`, "sparkles");
    document.querySelector('[data-cid="K.proposal-queue"]')?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }
  // the dashboard owns its own controls; capture phase so page-level handlers never double-handle them
  document.addEventListener("click", (e) => {
    if (!e.target.closest("[data-dash]")) return;
    const chip = e.target.closest(".chip"), fix = e.target.closest("[data-dash-fix]");
    if (chip) { e.stopPropagation(); dashSubmit(chip.textContent); return; }
    if (fix) { e.stopPropagation(); dashSubmit(fix.dataset.dashFix); return; }
  }, true);
  document.addEventListener("submit", (e) => { if (e.target.id === "ask" && e.target.closest("[data-dash]")) { e.preventDefault(); e.stopPropagation(); dashSubmit(document.getElementById("askText").value); } }, true);
  document.addEventListener("keydown", (e) => { if (e.target.id === "askText" && e.key === "Enter" && !e.shiftKey) { e.preventDefault(); dashSubmit(e.target.value); } });
  const dash = { html: dashHtml, submit: dashSubmit, state: DASH };

  // ---------- resource panel (site / cluster / node) with "Assess resource" ----------
  // Sites and clusters get an in-console trend (rollup); a single node's charts stay in Grafana.
  function findRes(kind, id) {
    for (const t of D.tenants) for (const o of t.orgs) for (const s of o.sites) {
      if (kind === "Site" && s.id === id) return { node: s, t, o, s, hs: hosts(s) };
      for (const c of s.clusters) {
        if (kind === "Cluster" && c.id === id) return { node: c, t, o, s, c, hs: c.hosts };
        for (const h of c.hosts) if (kind === "Node" && h.id === id) return { node: h, t, o, s, c, hs: [h] };
      }
    }
    return null;
  }
  const RES_ICON = { Site: "building-2", Cluster: "layers", Node: "server" };
  const RES_TABS = ["Overview", "Metrics", "Logs", "Traces", "Access", "Links"];
  function connected(kind, r) {
    const nodeIds = new Set(r.hs.map((h) => h.id));
    const svcs = D.services.filter((x) => nodeIds.has(x.node)).map((x) => ({ icon: x.icon, name: x.name, st: x.st, open: `data-svc-row="${x.id}"` }));
    if (kind === "Site") return [...r.s.clusters.map((c) => ({ icon: "layers", name: c.name, st: roll(c).worst, open: `data-resource="Cluster:${c.id}"` })), ...svcs];
    if (kind === "Cluster") return [...r.c.hosts.map((h) => ({ icon: "server", name: h.name, st: h.st, open: D.capacity[h.id] ? `data-resource="Node:${h.id}"` : "" })), ...svcs];
    return [...D.vms.filter((v) => v.node === r.node.id).map((v) => ({ icon: "monitor", name: v.name, st: v.st, open: "" })), ...svcs];
  }
  function resourcePanel(kind, id, tab = "Overview") {
    const r = findRes(kind, id); if (!r) return `<div class="empty">Resource not found.</div>`;
    const a = capOf(r.hs), status = kind === "Node" ? r.node.st : roll(r.node).worst, name = r.node.name;
    const parent = kind === "Site" ? r.o.name : kind === "Cluster" ? r.s.name : r.c.name;
    const placement = kind === "Site" ? r.s.name : kind === "Cluster" ? `${r.s.name} / ${r.c.name}` : `${r.c.name} / ${name}`;
    const why = kind === "Node" ? r.node.why : r.hs.find((h) => h.why)?.why;
    const nodeIds = new Set(r.hs.map((h) => h.id)), svcs = D.services.filter((x) => nodeIds.has(x.node)), passing = svcs.filter((x) => x.st === "ok").length;
    const body = {
      Overview: () => `${a.n ? (kind === "Node"
          ? `<div class="res-metric"><span>Memory committed</span><b class="num">${a.memc}<small>%</small></b></div><button class="btn sm" data-grafana-scope="node=${esc(name)} · metrics">${ic("external-link")}Charts for this node in Grafana</button>`
          : `<div class="res-metric"><span>Memory committed</span><b class="num">${a.memc}<small>%</small></b></div>${spark([...D.series(name.length * 13, 29, Math.max(5, a.memc - 9), 5, 0.3), a.memc], { color: status === "ok" ? "var(--e-api)" : `var(--${status})`, h: 96, w: 300, unit: "%" })}<div class="row faint" style="font-size:11.5px"><span class="grow">30 min ago</span><span>Now · example data</span></div>`)
        : `<p class="faint" style="margin:0;font-size:12.5px">No compute capacity reported for this ${kind.toLowerCase()}.</p>`}
        <dl class="facts res-facts"><dt>Resource</dt><dd>${esc(kind === "Node" ? (D.capacity[id]?.gpu ? "GPU node" : "Proxmox node") : kind === "Cluster" ? r.c.kind : r.s.provider ?? "Site")}${why ? ` · ${esc(why.toLowerCase())}` : ""}</dd>
          <dt>Placement</dt><dd class="mono">${esc(placement)}</dd><dt>Environment</dt><dd>${esc(r.s.envs.join(" · "))}</dd>
          ${a.n ? `<dt>Headroom</dt><dd class="num">${Math.max(0, a.freeC)} cores · ${Math.max(0, a.freeM)} GiB free</dd>` : ""}
          <dt>Conformance</dt><dd>${svcs.length ? `${passing} of ${svcs.length} services healthy <span class="faint">· Collect Service Conformance</span>` : `<span class="faint">No services placed here</span>`}</dd></dl>
        <div class="stack" style="gap:2px"><h3 style="margin-bottom:4px">Connected resources</h3><ul class="list res-conn">${connected(kind, r).map((c) => `<li><button type="button" ${c.open || "disabled"}>${ic(c.icon)}<span class="grow">${esc(c.name)}</span><span class="dot ${c.st}" title="${ST[c.st][1]}"></span>${c.open ? ic("chevron-right") : ""}</button></li>`).join("") || `<li class="faint" style="padding:8px 0;font-size:12.5px">Nothing connected</li>`}</ul></div>`,
      Metrics: () => grafanaLinks(kind.toLowerCase(), name),
      Logs: () => `<div class="stack" style="gap:8px"><p class="muted" style="margin:0;font-size:13px">Logs for ${esc(name)} are in Loki.</p><button class="btn sm" data-grafana-scope="${kind.toLowerCase()}=${esc(name)} · logs">${ic("scroll-text")}Open logs in Grafana</button></div>`,
      Traces: () => `<p class="muted" style="margin:0;font-size:13px">Traces are not collected yet: Tempo is a planned production addition to the o11y stack, and Alloy's OTLP receiver is Phase 2. Today the stack is Prometheus (metrics), Loki (logs), Alloy and Grafana.</p>`,
      Access: () => { const us = D.users.filter((u) => u.scope.startsWith(r.t.name) && (u.scope.includes("(all)") || u.scope === `${r.t.name} › ${r.o.name}` || u.scope.includes(r.s.name)));
        return us.length ? `<ul class="list">${us.map((u) => `<li class="row" style="padding:8px 0">${person(u.who)}<span class="grow"></span><span class="lvl ${u.level}">${u.level}</span></li>`).join("")}</ul>` : `<p class="faint" style="margin:0">Nobody has a role scoped here.</p>`; },
      Links: () => `<ul class="list res-conn">${[["network", "NetBox record"], ["server", "Proxmox"], ["activity", "Grafana dashboard"], ["workflow", "Semaphore task history"]].map(([i, l]) => `<li><button type="button" data-res-link="${l}">${ic(i)}<span class="grow">${l}</span>${ic("external-link")}</button></li>`).join("")}</ul>`,
    }[tab]();
    return `<div class="res" data-res-kind="${kind}" data-res-id="${esc(id)}">
      <div class="res-head"><span class="res-tile ${status}">${ic(RES_ICON[kind], "i-lg")}</span><div style="min-width:0"><span class="faint" style="font-size:12.5px">${kind} / ${esc(parent)}</span><h2 class="res-name">${esc(name)}</h2></div></div>
      <div class="row">${st(status, null, true)}<span class="grow"></span><span class="faint" style="font-size:12px">Example data · just now</span></div>
      ${scopeBanner({ kind: kind === "Node" ? "Host" : kind, name })}
      <nav class="res-tabs" role="tablist">${RES_TABS.map((t) => `<button type="button" role="tab" aria-selected="${t === tab}" data-rtab="${t}">${t}</button>`).join("")}</nav>
      <div class="res-body stack">${body}</div>
      ${status !== "ok" ? verifyBtn(kind, id, status, "btn res-assess") : ""}
      ${comp("K.assess", "Assess resource (health + rightsizing)", `<button type="button" class="btn primary res-assess" data-assess="${kind}:${esc(id)}">${ic("sparkles")}Assess resource</button><p class="faint" style="margin:6px 0 0;font-size:12px">Scope is carried into the request. Assessments are read-only; any rightsizing change comes back as a proposal for your approval.</p>`, "", "div")}
    </div>`;
  }
  const openResource = (kind, id) => { const r = findRes(kind, id); if (r) drawer(r.node.name, resourcePanel(kind, id)); };
  document.addEventListener("click", (e) => {
    const rr = e.target.closest("[data-resource]");
    if (rr && !e.target.closest("[data-qhere], [data-grafana-scope]")) { e.preventDefault(); const [k, i] = rr.dataset.resource.split(":"); openResource(k, i); return; }
    const tb = e.target.closest("[data-rtab]");
    if (tb) { const res = tb.closest(".res"); res.outerHTML = resourcePanel(res.dataset.resKind, res.dataset.resId, tb.dataset.rtab); after(document.querySelector(".drawer") ?? document); return; }
    const lk = e.target.closest("[data-res-link]"); if (lk) { toast(`Opens ${lk.dataset.resLink} for this resource`, "external-link"); return; }
    const as = e.target.closest("[data-assess]"); if (!as) return;
    const [kind, id] = as.dataset.assess.split(":"), r = findRes(kind, id), name = r.node.name;
    const w = { key: `AC-${seq++}`, title: `Assess ${kind.toLowerCase()} ${name}: health and rightsizing`, state: "queued", st: "idle", agent: "skynet", by: "you", via: "Assess resource", age: "now",
      ask: `Assess ${name} for health and rightsizing.`, scope: [r.t.name, r.o.name, r.s.name, curScope[3] ?? r.s.envs[0]], target: name, asset: { kind: kind === "Node" ? "Host" : kind, name },
      opa: { decision: "allow", rule: "Read-only assessment: nothing changes without a separate approved proposal" },
      steps: [["Snapshot", "todo"], ["Analyze 7d metrics", "todo"], ["Recommend", "todo"]],
      tasks: [{ name: "Snapshot placement, capacity and state (names only)", wf: "Snapshot Service Assessment", st: "todo" },
        { name: "Compare 7 days of Prometheus usage with allocated capacity", wf: "skynet analysis (read-only)", st: "todo" },
        { name: "Write health and rightsizing recommendation; changes return as a proposal", wf: "Proposal, only if a change is recommended", st: "todo" }] };
    D.work.unshift(w); document.querySelector(".drawer")?.remove();
    toast(`${w.key} queued: skynet assesses ${name}. Recommendations come back for approval.`, "sparkles");
    window.onWorkChanged?.(); if (window.onWorkCreated) window.onWorkCreated(w);
  });

  // ---------- work in a scope: one matcher for the board filter and every "running in this scope" banner ----------
  const hostOfWork = (w) => { const n = D.svc(w.target)?.node; return findHost(n) ? n : null; };
  const clusterOfWork = (w) => (w.target === "proxmox" ? "pve-cluster-a" : hostOfWork(w) && findHost(hostOfWork(w)).path[3]);
  const domainOfWork = (w) => D.svc(w.target)?.domain ?? (w.target === "opa" ? "Administration" : "IT");
  const groups = {}; // banner → Work hand-off for multi-select scopes (members don't fit in a data attribute)
  function workMatches(w, a) {
    if (w.asset && w.asset.kind === a.kind && w.asset.name === a.name) return true;
    if (a.kind === "Group") return a.members.some((m) => workMatches(w, m));
    const v = { Domain: domainOfWork(w), Tenant: w.scope[0], Org: w.scope[1], Site: w.scope[2], Cluster: clusterOfWork(w), Host: hostOfWork(w) && findHost(hostOfWork(w)).h.name, VM: D.svc(w.target)?.host, Service: D.svc(w.target)?.name }[a.kind];
    return v === a.name;
  }
  function scopeBanner(a) {
    if (a.kind === "Group") groups[a.name] = a;
    const ws = D.work.filter((w) => inTenant(w) && w.state !== "done" && workMatches(w, a)), run = ws.filter((w) => w.state === "running").length, rest = ws.length - run;
    return comp("K.scope-banner", "Workflows running in this scope → filtered Work", `<button type="button" class="scope-banner${run ? "" : " quiet"}" data-goto-work="${esc(a.kind)}:${esc(a.name)}">
      ${ic("activity", "i-lg")}<span class="grow"><b>${run ? `${run} ${run === 1 ? "workflow" : "workflows"} running in this scope` : "No workflows running in this scope"}</b>
      <span>${rest ? `${rest} more queued or waiting · ` : ""}Follow agents, tasks and verification evidence</span></span>${ic("arrow-right", "i-lg")}</button>`, "", "div");
  }
  document.addEventListener("click", (e) => {
    const g = e.target.closest("[data-goto-work]"); if (!g) return;
    const [kind, ...rest] = g.dataset.gotoWork.split(":"); document.querySelector(".drawer")?.remove();
    const a = kind === "Group" ? groups[rest.join(":")] : { kind, name: rest.join(":") };
    if (window.onGotoWork) window.onGotoWork(a); else location.hash = "work";
  });

  // ---------- agent roles: what each skynet role is running now ----------
  function roleLoad(name) {
    const live = D.work.filter((w) => inTenant(w) && w.state !== "done");
    const now = live.filter((w) => currentRole(w) === name);
    const involved = live.filter((w) => !now.includes(w) && w.steps.some((x) => roleOf(x, w) === name));
    const done = D.work.filter((w) => inTenant(w) && w.state === "done" && w.steps.some((x) => roleOf(x, w) === name));
    const st = now.some((w) => w.state === "running") ? "run" : now.some((w) => ["blocked", "failed"].includes(w.state)) ? "crit" : now.length ? "warn" : "idle";
    return { now, involved, done, st };
  }
  const roleMember = (name) => D.agentGroups.flatMap((g) => g.members).find((m) => m.name === name);
  const roleTile = (m, big) => `<span class="role-tile${big ? " big" : ""}" style="--tint:${m.color}">${m.initials ? esc(m.initials) : ic(m.icon)}</span>`;
  function roleView(name) {
    const m = roleMember(name); if (!m) return `<div class="view"><div class="empty">Pick an agent role in the sidebar.</div></div>`;
    const L = roleLoad(name), sec = (title, list, empty) => `<div class="stack" style="gap:8px"><div class="row"><span class="label grow">${title}</span><span class="faint num">${list.length}</span></div>${list.length ? list.map((w) => strip(w)).join("") : `<div class="lane-empty">${empty}</div>`}</div>`;
    return `<div class="view"><div class="view-h"><h1>${roleTile(m, true)}<span style="display:grid"><span>${esc(m.name)}</span><span class="faint" style="font-size:13px;font-weight:500">${esc(m.domain ?? "")} · skynet role</span></span></h1>${st(...{ run: ["run", "Working"], warn: ["warn", "Waiting"], crit: ["crit", "Blocked"], idle: ["idle", "Idle"] }[L.st], true)}<p>${esc(m.role)}. Only work this role owns or takes part in is shown.</p></div>${m.planned ? `<div class="policy cond">${ic("info")}<span><b>Planned role.</b> ${esc(m.name)} is not live in skynet yet. Until it is, infra-agent owns this kind of work.</span></div>` : ""}
      ${comp("K.role-view", "One skynet role and its workloads", `<div class="cols">${sec("Assigned now: this role owns the current step", L.now, "Nothing assigned right now")}${sec("Also involved: owns a later or earlier step", L.involved, "Not involved in other open work")}${sec("Finished recently", L.done, "Nothing finished yet")}</div>`)}</div>`;
  }

  // ---------- verify health: dispatch the SRE agent (read-only) at a single resource with a health issue ----------
  function verifyTarget(kind, id) {
    if (kind === "Service") { const x = D.svc(id), f = findHost(x.node); return { kind, id, name: x.name, st: x.st, why: x.why, where: x.where, path: f ? f.path : ["Uhstray.io", "Platform", "site-hq", x.node] }; }
    const r = findRes(kind, id); if (!r) return null;
    return { kind: kind === "Node" ? "Host" : kind, id, name: r.node.name, st: kind === "Node" ? r.node.st : roll(r.node).worst, why: kind === "Node" ? r.node.why : r.hs.find((h) => h.why)?.why, where: r.c ? `${r.s.name} / ${r.c.name}` : r.s.name, path: [r.t.name, r.o.name, r.s.name, r.c?.name ?? ""] };
  }
  let planSeq = 1;
  function verifyPlan(kind, id) {
    const v = verifyTarget(kind, id), svc = kind === "Service";
    const plan = { agent: "skynet", steps: [["Assess current state", "todo", "read inventory and scoped evidence", "o11y-agent"], ["Run read-only checks", "todo", "OPA validates each template; Semaphore runs it", "o11y-agent"], ["Verify and record", "todo", "prove health, persistence and a telemetry receipt", "o11y-agent"]],
      tasks: svc ? [{ wf: "Inspect Service Runtime", name: "container state, exit codes, restart counts" }, { wf: "Verify Service Persistence", name: "restart policy and boot persistence" }] : [{ wf: "Snapshot Service Assessment", name: "placement, capacity and state, names only" }] };
    return `<div class="stack vplan" data-verify-kind="${kind}" data-verify-id="${esc(id)}">
      <div><span class="faint" style="font-size:12.5px">Proposed plan · P-${planSeq}</span><h2 class="res-name">Verify health: ${esc(v.name)}</h2></div>
      <div class="policy">${ic("check")}<div><b>OPA allowed</b> · Read-only verification of the selected scope.<div class="muted">Each check still needs an OPA decision for its exact action and Semaphore template.</div></div></div>
      <dl class="facts res-facts"><dt>Resolved scope</dt><dd class="mono">${esc(scopeText([v.path[0], v.path[1], v.path[2], curScope[3] ?? "prod"]))}</dd><dt>Work target</dt><dd>${esc(v.name)} · ${v.kind.toLowerCase()}</dd>
        <dt>Current health</dt><dd>${st(v.st)}${v.why ? `<div class="faint" style="font-size:12px">${esc(v.why)}</div>` : ""}</dd><dt>Affected site</dt><dd>${esc(v.path[2])}</dd><dt>Assigned role</dt><dd>o11y-agent (skynet)</dd><dt>Impact</dt><dd>Read-only assessment and verification</dd></dl>
      ${vsteps(plan, "plan")}
      <div class="row wrap"><button type="button" class="btn primary" data-verify-queue>${ic("clipboard-list")}Queue verification</button><span class="faint" style="font-size:12px">Nothing changes; a fix comes back as its own proposal.</span></div>
    </div>`;
  }
  const verifyBtn = (kind, id, st, cls = "btn") => (st === "ok" ? "" : `<button type="button" class="${cls}" data-verify="${kind}:${esc(id)}" title="Dispatch o11y-agent to verify health">${ic("heart-pulse")}Verify health</button>`);
  document.addEventListener("click", (e) => {
    const b = e.target.closest("[data-verify]");
    if (b) { e.stopPropagation(); const [k, i] = b.dataset.verify.split(":"); drawer("Verify health", verifyPlan(k, i)); return; }
    const q = e.target.closest("[data-verify-queue]"); if (!q) return;
    const box = q.closest(".vplan"), kind = box.dataset.verifyKind, id = box.dataset.verifyId, v = verifyTarget(kind, id), svc = kind === "Service";
    const w = { key: `AC-${seq++}`, title: `Verify health: ${v.name}`, state: "queued", st: "idle", agent: "skynet", role: "o11y-agent", by: "you", via: "Verify health", age: "now",
      ask: `Verify the health of ${v.name}${v.why ? ` (${v.why})` : ""}.`, scope: [v.path[0], v.path[1], v.path[2], curScope[3] ?? "prod"], target: svc ? id : v.name, asset: svc ? undefined : { kind: v.kind, name: v.name },
      opa: { decision: "allow", rule: "Read-only verification of the selected scope" },
      steps: [["Assess current state", "todo", undefined, "o11y-agent"], ["Run read-only checks", "todo", undefined, "o11y-agent"], ["Verify and record", "todo", undefined, "o11y-agent"]],
      tasks: svc ? [{ name: "Container state, exit codes, restart counts", wf: "Inspect Service Runtime", st: "todo" }, { name: "Restart policy and boot persistence", wf: "Verify Service Persistence", st: "todo" }]
        : [{ name: "Placement, capacity and state (names only)", wf: "Snapshot Service Assessment", st: "todo" }] };
    planSeq++; D.work.unshift(w); document.querySelector(".drawer")?.remove();
    toast(`${w.key} queued: o11y-agent verifies ${v.name}.`, "heart-pulse");
    window.onWorkChanged?.(); if (window.onWorkCreated) window.onWorkCreated(w);
  });

  // ---------- infrastructure map: site → clusters → nodes; a selected node expands into what it hosts ----------
  const UPDOWN = { ok: "Healthy", warn: "Degraded", crit: "Unavailable", idle: "No signal" };
  function infraMap(site, selId, picked = new Set()) {
    if (!site) return `<div class="empty">Pick a site in the scope bar.</div>`;
    const hostsOn = (id) => [...D.vms.filter((v) => v.node === id).map((v) => ({ icon: "monitor", name: v.name, st: v.st, key: `VM:${v.name}`, attr: `data-mapsel="VM:${v.name}" title="VM ${v.vmid} · ${UPDOWN[v.st]} · shift-click to add"` })),
      ...D.services.filter((x) => x.node === id).map((x) => ({ icon: x.icon, name: x.name, st: x.st, key: `Service:${x.id}`, attr: `data-mapsel="Service:${x.id}" title="${esc(x.name)} · ${UPDOWN[x.st]} · shift-click to add"` }))];
    const nodeText = (h) => (h.st === "crit" ? h.why ?? "Node unreachable" : h.st === "warn" ? h.why ?? "Degraded" : D.capacity[h.id] ? "Node available" : h.role ?? "Available");
    return `<div class="imap-body">
      <div class="imap-site"><span class="imap-diamond" aria-hidden="true"></span><b>${esc(site.name)}</b><span class="faint grow">${esc(site.provider ?? "Proxmox estate")} · ${esc(site.envs.join(" · "))}</span><button class="btn sm ghost" data-resource="Site:${site.id}">${ic("panel-right-open")}Inspect site</button></div>
      <div class="imap-clusters">${site.clusters.map((c) => { const count = c.hosts.length + c.hosts.reduce((n, h) => n + hostsOn(h.id).length, 0);
        return `<section class="imap-cluster"><header>${ic("layers")}<b class="grow">${esc(c.name)}</b><span class="faint">${count} resources</span><button class="iconbtn" data-resource="Cluster:${c.id}" aria-label="Inspect ${esc(c.name)}" title="Inspect cluster">${ic("panel-right-open")}</button></header>
          ${c.hosts.map((h) => { const sel = h.id === selId, kids = hostsOn(h.id);
            return `<button type="button" class="imap-node${sel ? " sel" : ""}${picked.has(`Host:${h.id}`) ? " picked" : ""}" data-imap-node="${h.id}" data-mapsel="Host:${h.id}" aria-expanded="${sel}" aria-pressed="${picked.has(`Host:${h.id}`)}" title="Click to select · shift-click to add to the selection"><span class="imap-ic">${ic("server", "i-lg")}</span><span class="grow"><b>${esc(h.name)}</b><span>${esc(nodeText(h))}</span></span><span class="dot ${h.st}" title="${UPDOWN[h.st]}"></span></button>
            ${sel ? `<div class="imap-hosts"><span class="imap-hosts-label">hosts</span><div class="imap-chips">${kids.map((k) => `<button type="button" class="imap-chip${picked.has(k.key) ? " picked" : ""}" aria-pressed="${picked.has(k.key)}" ${k.attr}><span class="dot ${k.st}"></span>${ic(k.icon)}${esc(k.name)}</button>`).join("") || `<span class="faint" style="font-size:12.5px">Nothing placed on this node</span>`}</div>
              <span class="row" style="gap:6px"><button class="btn sm ghost" data-resource="Node:${h.id}" ${D.capacity[h.id] ? "" : "hidden"}>${ic("panel-right-open")}Inspect ${esc(h.name)}</button>${D.capacity[h.id] ? verifyBtn("Node", h.id, h.st, "btn sm") : ""}</span></div>` : ""}`; }).join("")}</section>`; }).join("")}</div>
    </div>
    <div class="imap-foot">${Object.entries(UPDOWN).map(([k, l]) => `<span><span class="dot ${k}"></span>${l}</span>`).join("")}<span class="grow"></span><span class="faint">Declared placement · example data</span></div>`;
  }

  // ---------- network topology: ingress path → gateway → segments → attached hosts / VMs / services ----------
  function networkMap(site) {
    const n = site && D.networks[site.id];
    if (!n) return `<div class="empty">${ic("network", "i-lg")}<b>No network recorded for this site</b>NetBox holds the network; nothing is declared here yet (example data).</div>`;
    const member = ([kind, id]) => {
      if (kind === "host") { const f = findHost(id); return `<button type="button" class="imap-chip" ${D.capacity[id] ? `data-resource="Node:${id}"` : ""} title="${esc(f?.h.name ?? id)} · ${UPDOWN[f?.h.st ?? "idle"]}"><span class="dot ${f?.h.st ?? "idle"}"></span>${ic("server")}${esc(f?.h.name ?? id)}</button>`; }
      if (kind === "vm") { const v = D.vms.find((x) => x.name === id); return `<span class="imap-chip" title="VM ${v?.vmid ?? ""} · ${UPDOWN[v?.st ?? "idle"]}"><span class="dot ${v?.st ?? "idle"}"></span>${ic("monitor")}${esc(id)}</span>`; }
      if (kind === "svc") { const x = D.svc(id); return `<button type="button" class="imap-chip" data-svc-row="${id}" title="${esc(x.name)} · ${UPDOWN[x.st]}"><span class="dot ${x.st}"></span>${ic(x.icon)}${esc(x.name)}</button>`; }
      return `<span class="imap-chip" title="No health signal in this view"><span class="dot idle"></span>${ic("server")}${esc(id)}</span>`;
    };
    const segSt = (g) => { const sts = g.members.map(([k, id]) => (k === "host" ? findHost(id)?.h.st : k === "vm" ? D.vms.find((x) => x.name === id)?.st : k === "svc" ? D.svc(id)?.st : "idle") ?? "idle"); return sts.includes("crit") ? "crit" : sts.includes("warn") ? "warn" : sts.every((x) => x === "idle") ? "idle" : "ok"; };
    return `<div class="imap-body">
      <div class="imap-site"><span class="imap-diamond" aria-hidden="true"></span><b>${esc(site.name)}</b><span class="faint grow">Network · ${esc(site.provider ?? "on-prem")}</span><button class="btn sm ghost" data-res-link="NetBox">${ic("external-link")}Open in NetBox</button></div>
      <div class="net-path">${n.path.map((p, i) => `${i ? `<span class="net-arrow" aria-hidden="true">${ic("arrow-right")}</span>` : ""}<${p.svc ? `button type="button" data-svc-row="${p.svc}"` : "div"} class="net-hop"><span class="imap-ic">${ic(p.icon, "i-lg")}</span><span class="grow"><b>${esc(p.name)}</b><span>${esc(p.sub)}</span></span><span class="dot ${p.st}" title="${UPDOWN[p.st]}"></span></${p.svc ? "button" : "div"}>`).join("")}</div>
      ${n.segments.length ? `<div class="net-segs">${n.segments.map((g) => { const gs = segSt(g); return `<section class="net-seg ${gs}${g.restricted ? " restricted" : ""}">
          <header>${ic("network")}<b class="grow">${esc(g.name)}${g.vlan ? ` <span class="faint mono" style="font-weight:500">VLAN ${g.vlan}</span>` : ""}</b>${g.restricted ? `<span class="tag" title="Host firewall denies egress">${ic("shield")}egress denied</span>` : ""}<span class="dot ${gs}" title="${UPDOWN[gs]}"></span></header>
          <span class="faint" style="font-size:12px">${esc(g.note)}${g.link ? ` · ${esc(g.link)}` : ""}</span>
          <div class="imap-chips" style="margin-left:0">${g.members.map(member).join("")}</div></section>`; }).join("")}</div>` : `<p class="faint" style="margin:0;font-size:12.5px">Edge-only site: no private segments.</p>`}
    </div>
    <div class="imap-foot">${Object.entries(UPDOWN).map(([k, l]) => `<span><span class="dot ${k}"></span>${l}</span>`).join("")}<span class="grow"></span><span class="faint">Segments and VLAN IDs are examples · source of truth: NetBox</span></div>`;
  }

  // ---------- shared secondary views ----------
  const V = {};

  // ---------- Services: ask about services + request a service ----------
  const SVC = { qa: null };
  const svcByMention = (q) => tenantServices().find((x) => q.toLowerCase().includes(x.name.toLowerCase()));
  function svcChips() {
    const sick = tenantServices().filter((x) => x.st !== "ok");
    const first = sick.find((x) => x.st === "crit") ?? sick[0];
    return [...(first ? [`Why is ${first.name} ${first.st === "crit" ? "down" : "degraded"}?`, `What depends on ${first.name}?`, `Restart ${first.name} safely`] : []), "Which services are unhealthy?"];
  }
  function answerSvc(q) {
    const t = q.toLowerCase(), x = svcByMention(q), all = tenantServices();
    if (x && /restart|redeploy|upgrade|rotate|fix|deploy|scale/.test(t)) return { action: true };
    if (x && /depend|uses|calls|integrat/.test(t)) {
      const ins = D.edges.filter((e) => e[1] === x.id), outs = D.edges.filter((e) => e[0] === x.id);
      return { q, title: `${x.name}: ${ins.length} depend on it, it uses ${outs.length}`, lines: [...ins.map((e) => `${D.svc(e[0]).name} → ${x.name}: ${e[3]}`), ...outs.map((e) => `${x.name} → ${D.svc(e[1]).name}: ${e[3]}`)], items: [x] };
    }
    if (x) { const ws = D.work.filter((w) => w.target === x.id && inTenant(w) && w.state !== "done");
      return { q, title: `${x.name} is ${ST[x.st][1].toLowerCase()}`, lines: [x.why ? `Why: ${x.why}.` : "No health issue recorded.", `Runs on ${x.host} (${x.where}).`, ws.length ? `Work on it: ${ws.map((w) => `${w.key} ${stateLabel[w.state].toLowerCase()}`).join(", ")}.` : "No open work on it."], items: [x] }; }
    if (/unhealthy|down|degraded|red|broken|which/.test(t)) { const sick = all.filter((s2) => s2.st !== "ok"); return { q, title: `${sick.length} unhealthy`, lines: sick.map((s2) => `${s2.name}: ${ST[s2.st][1].toLowerCase()}, ${s2.why}.`), items: sick }; }
    return { q, title: `${all.length} services in this scope`, lines: [`${all.filter((s2) => s2.st === "ok").length} healthy, ${all.filter((s2) => s2.st === "warn").length} degraded, ${all.filter((s2) => s2.st === "crit").length} down.`, "Ask about a service by name: why it is red, what depends on it, or ask to restart it safely."], items: [] };
  }
  const svcComposer = () => comp("K.services-ask", "Ask about services", `<form class="card qchat" id="svcask">
      <div class="qchat-h"><span class="av" style="background:#2448b8">${ic("bot")}</span><b>Ask about services</b><span class="faint">Why something is red, what depends on it, or ask for a safe change. Changes become proposals for approval.</span></div>
      <div class="composer-box"><textarea id="svcText" rows="2" placeholder="Ask about agent-cloud services in ${esc(scopeText(curScope.length ? curScope : ["Uhstray.io", "Platform", "site-hq", "prod"]))}…" aria-label="Ask about services"></textarea>
        <div class="composer-foot"><span class="tag">${ic("crosshair")}${esc(curScope[2] ?? "site-hq")} · ${esc(curScope[3] ?? "prod")}</span><span class="grow"></span><span class="faint" style="font-size:12px">Enter to send</span><button class="btn primary sm" type="submit">${ic("message-circle-question")}Ask</button></div></div>
      <div class="chips">${svcChips().map((c) => `<button type="button" class="chip" data-svcchip>${esc(c)}</button>`).join("")}</div>
      ${SVC.qa ? `<div class="qanswer" role="status"><div class="row"><span class="faint grow" style="font-size:12px">“${esc(SVC.qa.q)}”</span><button type="button" class="iconbtn" data-svcclear aria-label="Dismiss answer" style="width:24px;height:24px">${ic("x")}</button></div><b>${esc(SVC.qa.title)}</b>${SVC.qa.lines.length ? `<ul>${SVC.qa.lines.map((l) => `<li>${esc(l)}</li>`).join("")}</ul>` : ""}${SVC.qa.items.length ? `<div class="row wrap" style="gap:6px">${SVC.qa.items.map((x) => `<button type="button" class="btn sm ghost" data-svc-row="${x.id}">${ic(x.icon)}${esc(x.name)}</button>`).join("")}</div>` : ""}<span class="faint" style="font-size:11.5px">Answered from the service inventory (example data). In the console, skynet answers from live state.</span></div>` : ""}
    </form>`, "", "div");
  function svcSubmit(text) {
    if (!text.trim()) { toast("Type a question about services first.", "info"); return; }
    const a = answerSvc(text);
    if (a.action) { const x = svcByMention(text), w = parse(text, curScope.length ? curScope : ["Uhstray.io", "Platform", "site-hq", "prod"]); w.target = x.id; w.title = `${text.length > 52 ? text.slice(0, 50) + "…" : text}`; SVC.qa = null; newProposal(w); return; }
    SVC.qa = a; const box = document.querySelector('[data-cid="K.services-ask"]'); if (box) { box.outerHTML = svcComposer(); after(document.getElementById("view") ?? document); }
  }
  document.addEventListener("submit", (e) => { if (e.target.id === "svcask") { e.preventDefault(); e.stopPropagation(); svcSubmit(document.getElementById("svcText").value); } }, true);
  document.addEventListener("keydown", (e) => { if (e.target.id === "svcText" && e.key === "Enter" && !e.shiftKey) { e.preventDefault(); svcSubmit(e.target.value); } });

  // request a service: an approved catalog service into this scope, or a new service through onboarding
  let reqTab = "existing";
  function reqSvcHtml() {
    const here = new Set(tenantServices().map((x) => x.id)), t = curTenant();
    const sites = t.orgs.flatMap((o) => o.sites.map((s2) => s2.name));
    return `<div class="stack"><div class="seg" role="group" aria-label="Request type"><button type="button" data-reqtab="existing" aria-pressed="${reqTab === "existing"}">Approved service</button><button type="button" data-reqtab="new" aria-pressed="${reqTab === "new"}">New service</button></div>
      ${reqTab === "existing" ? `<p class="faint" style="margin:0;font-size:12.5px">Deploys an approved agent-cloud service into ${esc(curScope[2] ?? "site-hq")} · ${esc(curScope[3] ?? "prod")}. It becomes a proposal: OPA checks it, an Admin approves, Semaphore runs the service's own deploy template.</p>
        <ul class="list res-conn">${D.catalog.map((c) => `<li><div class="row" style="padding:9px 0;gap:10px"><span class="grow"><b>${esc(c.name)}</b><span class="faint" style="display:block;font-size:12px">${esc(c.what)} · <span class="mono">${esc(c.template)}</span></span></span>${here.has(c.id) ? `<span class="tag">${ic("circle-check")}Running here</span>` : ""}<button type="button" class="btn sm" data-reqsvc-item="${c.id}">${ic("package-plus")}Request</button></div></li>`).join("")}</ul>`
      : `<p class="faint" style="margin:0;font-size:12.5px">A service agent-cloud does not run yet goes through onboarding (plan/architecture/02): a plan, inventory, a deploy playbook, secrets in OpenBao, a Semaphore template and an SSH key. This opens a review item; nothing deploys from here.</p>
        <label><span class="label">Service name</span><input class="input" id="ns-name" placeholder="e.g. wiki"></label>
        <label><span class="label">What it does and who uses it</span><textarea class="input" id="ns-what" rows="3" style="height:auto;padding:8px 10px" placeholder="Internal documentation wiki for the platform team"></textarea></label>
        <div class="row wrap" style="gap:10px"><label class="grow"><span class="label">Owner group</span><select class="input" id="ns-owner">${[...new Set(D.users.map((u) => u.group))].map((g) => `<option>${esc(g)}</option>`).join("")}</select></label>
          <label class="grow"><span class="label">Site</span><select class="input" id="ns-site">${sites.map((x) => `<option ${x === curScope[2] ? "selected" : ""}>${esc(x)}</option>`).join("")}</select></label></div>
        <button type="button" class="btn primary" data-reqsvc-new>${ic("send-horizontal")}Submit onboarding request</button>`}</div>`;
  }
  document.addEventListener("click", (e) => {
    if (e.target.closest("[data-reqsvc]")) { reqTab = "existing"; drawer("Request a service", reqSvcHtml()); return; }
    const tb = e.target.closest("[data-reqtab]"); if (tb) { reqTab = tb.dataset.reqtab; const d = document.querySelector(".drawer .drawer-b"); if (d) { d.innerHTML = reqSvcHtml(); after(d); } return; }
    const sc = curScope.length ? curScope : ["Uhstray.io", "Platform", "site-hq", "prod"];
    const item = e.target.closest("[data-reqsvc-item]");
    if (item) { const c = D.catalog.find((x) => x.id === item.dataset.reqsvcItem); document.querySelector(".drawer")?.remove();
      newProposal({ key: `AC-${seq++}`, title: `Deploy ${c.name} to ${sc[2]} · ${sc[3]}`, state: "proposed", st: "warn", agent: "skynet", by: "you", via: "Service request", age: "now", ask: `Deploy the approved ${c.name} service into ${sc[2]} · ${sc[3]}.`, scope: sc.slice(), target: D.svc(c.id) ? c.id : c.name,
        opa: { decision: "allow", rule: "Approved catalog service", cond: "A new placement needs Admin approval" },
        steps: [["Parse request", "done"], ["Plan (skynet)", "done"], ["Policy (OPA)", "done"], ["Approval", "wait"], ["Check inventory and address", "todo"], ["Provision", "todo"], ["Deploy", "todo"], ["Verify", "todo"]],
        tasks: [{ name: "Declared spec and address claims", wf: "Lookup Service Inventory", st: "todo" }, { name: "Address is free", wf: "Validate Address Free", st: "todo" }, { name: "VM from the declared spec", wf: "Provision VM", st: "todo" }, { name: "Service deploy", wf: c.template, st: "todo" }] }); return; }
    if (e.target.closest("[data-reqsvc-new]")) {
      const name = document.getElementById("ns-name").value.trim(); if (!name) { toast("Give the service a name first.", "info"); return; }
      const what = document.getElementById("ns-what").value.trim(), owner = document.getElementById("ns-owner").value, site = document.getElementById("ns-site").value; document.querySelector(".drawer")?.remove();
      newProposal({ key: `AC-${seq++}`, title: `Onboard new service: ${name}`, state: "proposed", st: "warn", agent: "skynet", by: "you", via: "New service request", age: "now", ask: `${what || "New service"} (owner ${owner}, site ${site}).`, scope: [sc[0], sc[1], site, sc[3]], target: name,
        opa: { decision: "allow", rule: "New services follow the onboarding checklist", cond: "Needs review before any plan or deploy is written" },
        steps: [["Parse request", "done"], ["Draft onboarding plan", "done"], ["Review", "wait"], ["Plan document and inventory", "todo"], ["Deploy playbook and secrets", "todo"], ["Semaphore template and SSH key", "todo"], ["Route and verify", "todo"]],
        tasks: [{ name: "Implementation plan in plan/development", wf: "PR: plan document", st: "todo" }, { name: `deploy-${name.toLowerCase().replace(/[^a-z0-9]+/g, "-")}.yml with manage-secrets.yml`, wf: "PR: deploy playbook", st: "todo" }, { name: "Per-service SSH key", wf: "Generate Service SSH Key", st: "todo" }, { name: "Route through Caddy", wf: "Manage Caddy Sites", st: "todo" }] });
    }
  });

  // ---------- profile and settings ----------
  const isPlatformAdmin = () => D.users.find((u) => u.who === "you")?.group === "platform-admins";
  const PF = { photo: null, color: null };
  const AV_COLORS = ["#15191e", "#2448b8", "#2f7a55", "#8a3f7a", "#8a4b14", "#4f4fb0"];
  const avatarHtml = (big) => { const me = D.people.you; return me.photo ? `<img class="av-img${big ? " big" : ""}" src="${esc(me.photo)}" alt="">` : `<span class="av human${big ? " big" : ""}" style="background:${me.color}">${esc(me.initials)}</span>`; };
  const lockRow = (label, value, note) => `<div class="pf-lock"><span class="label">${label}</span><span class="grow mono">${esc(value)}</span><span class="tag" title="Provided by Authentik over OIDC">${ic("lock")}Managed by Authentik</span>${note ? `<span class="faint" style="flex-basis:100%;font-size:12px">${note}</span>` : ""}</div>`;
  V.profile = () => { const me = D.users.find((u) => u.who === "you"), p = D.people.you, sc = curScope.length ? curScope : ["Uhstray.io", "Platform", "site-hq", "prod"];
    return `<div class="view">${head("Your profile", "user-round", "Your account comes from Authentik over OIDC. What Authentik owns is locked here; change it in Authentik. Everything else is yours to set.", authority("Authentik (OIDC claims)", "fingerprint"))}
    ${comp("K.profile-oidc", "Profile: fields managed by the identity provider", `<div class="card card-b stack"><h3>Account</h3>
      ${lockRow("Legal name", "Platform Operator (example)")}${lockRow("Email", me.email)}${lockRow("Username", me.uid)}${lockRow("Groups", me.group)}${lockRow("Sign-in and MFA", "Password + MFA", "Change these in Authentik.")}
      <div class="row"><button type="button" class="btn sm" data-res-link="Authentik account">${ic("external-link")}Edit in Authentik</button><span class="faint" style="font-size:12px">A local (non-OIDC) account would have every field editable here.</span></div></div>`)}
    ${comp("K.profile-prefs", "Profile: your editable preferences", `<form class="card card-b stack" id="profile-form"><h3>Preferences</h3>
      <div class="row wrap" style="gap:16px;align-items:flex-start"><div class="stack" style="gap:8px;align-items:center"><span id="pf-avatar">${avatarHtml(true)}</span>
          <label class="btn sm" for="pf-photo">${ic("image-up")}Upload photo</label><input type="file" id="pf-photo" accept="image/*" hidden>
          <div class="row" style="gap:4px" role="group" aria-label="Avatar colour">${AV_COLORS.map((c) => `<button type="button" class="swatch" data-pf-color="${c}" style="background:${c}" aria-label="Colour ${c}" aria-pressed="${p.color === c}"></button>`).join("")}</div>
          ${p.photo ? `<button type="button" class="btn sm ghost" data-pf-clearphoto>Use initials</button>` : ""}</div>
        <div class="stack grow" style="min-width:240px">
          <label><span class="label">Display name</span><input class="input" id="pf-name" value="${esc(p.display ?? "Operator")}"></label>
          <div class="row wrap" style="gap:10px"><label class="grow"><span class="label">Pronouns</span><input class="input" id="pf-pronouns" value="${esc(p.pronouns ?? "")}" placeholder="optional"></label>
            <label class="grow"><span class="label">Timezone</span><select class="input" id="pf-tz">${["America/New_York", "America/Chicago", "America/Los_Angeles", "UTC", "Europe/London"].map((z) => `<option ${z === (p.tz ?? "America/New_York") ? "selected" : ""}>${z}</option>`).join("")}</select></label></div>
          <div><span class="label">Theme</span><div class="seg" role="group" aria-label="Theme">${["system", "light", "dark"].map((m) => `<button type="button" data-pf-theme="${m}" aria-pressed="${(document.documentElement.dataset.theme ?? "system") === m}">${m[0].toUpperCase() + m.slice(1)}</button>`).join("")}</div></div>
          <fieldset class="pf-notify"><legend class="label">Notify me when</legend>${[["approval", "a proposal needs my approval", true], ["failed", "my work fails or is blocked", true], ["digest", "a daily summary is ready", false]].map(([id, l, on]) => `<label><input type="checkbox" id="pf-n-${id}" ${on ? "checked" : ""}> ${l}</label>`).join("")}</fieldset>
          <div class="row wrap" style="gap:10px"><label class="grow"><span class="label">Default site</span><select class="input" id="pf-site">${curTenant().orgs.flatMap((o) => o.sites.map((s2) => s2.name)).map((x) => `<option ${x === sc[2] ? "selected" : ""}>${esc(x)}</option>`).join("")}</select></label>
            <label class="grow"><span class="label">Default environment</span><select class="input" id="pf-env">${["prod", "dev", "local"].map((x) => `<option ${x === sc[3] ? "selected" : ""}>${x}</option>`).join("")}</select></label></div>
          <div class="row"><button type="submit" class="btn primary">${ic("check")}Save preferences</button></div>
        </div></div></form>`)}</div>`; };
  document.addEventListener("change", (e) => {
    if (e.target.id !== "pf-photo" || !e.target.files?.[0]) return;
    const r = new FileReader(); r.onload = () => { D.people.you.photo = r.result; const box = document.getElementById("pf-avatar"); if (box) box.innerHTML = avatarHtml(true); window.onWorkChanged?.(); }; r.readAsDataURL(e.target.files[0]);
  });
  document.addEventListener("click", (e) => {
    const c = e.target.closest("[data-pf-color]"); if (c) { D.people.you.color = c.dataset.pfColor; D.people.you.photo = null; dispatchEvent(new HashChangeEvent("hashchange")); return; }
    if (e.target.closest("[data-pf-clearphoto]")) { D.people.you.photo = null; dispatchEvent(new HashChangeEvent("hashchange")); return; }
    const th = e.target.closest("[data-pf-theme]"); if (th) { const m = th.dataset.pfTheme; if (m === "system") delete document.documentElement.dataset.theme; else document.documentElement.dataset.theme = m; th.parentElement.querySelectorAll("button").forEach((b) => b.setAttribute("aria-pressed", b === th)); }
  });
  document.addEventListener("submit", (e) => {
    if (e.target.id !== "profile-form") return; e.preventDefault(); e.stopPropagation();
    const p = D.people.you, name = document.getElementById("pf-name").value.trim() || "Operator";
    p.display = name; p.initials = name.split(/\s+/).map((w) => w[0]).join("").slice(0, 2).toUpperCase(); p.pronouns = document.getElementById("pf-pronouns").value.trim(); p.tz = document.getElementById("pf-tz").value;
    toast("Preferences saved. Account fields stay with Authentik."); window.onWorkChanged?.();
  }, true);

  const adminOnly = (what) => `<div class="view">${head(what, "lock", "Only platform admins can open this page.", authority("Authentik groups", "fingerprint"))}<div class="empty">${ic("lock", "i-lg")}<b>You don't have access</b>Ask a platform admin.</div></div>`;
  V.tenantSettings = () => { if (!isPlatformAdmin()) return adminOnly("Tenant settings"); const t = curTenant();
    return `<div class="view">${head(`Tenant settings · ${t.name}`, "building", "Applies to the tenant chosen in the sidebar. Saving creates a proposal: tenant configuration is code, so changes land as a reviewed change, not a direct edit.", authority("site-config inventory + Authentik", "fingerprint"))}
    ${comp("K.tenant-settings", "Tenant settings", `<form class="card card-b stack" id="tenant-form">
      <div class="row wrap" style="gap:10px"><label class="grow"><span class="label">Tenant name</span><input class="input" id="ts-name" value="${esc(t.name)}"></label>
        <label class="grow"><span class="label">Authentik tenant group</span><input class="input mono" value="tenant-${esc(t.id)}" readonly title="Example group name"></label></div>
      <div class="row wrap" style="gap:10px"><label class="grow"><span class="label">Default LLM budget (tokens / hour)</span><input class="input num" id="ts-budget" value="250,000,000"></label>
        <label class="grow"><span class="label">Iteration length</span><select class="input" id="ts-iter"><option>1 week</option><option selected>2 weeks</option><option>3 weeks</option></select></label>
        <label class="grow"><span class="label">Production approvals</span><select class="input" id="ts-approve"><option>One Admin</option><option>Two Admins</option><option>Admin + change window</option></select></label></div>
      <h3>Organizations</h3><div class="tbl-wrap"><table class="tbl"><thead><tr><th>Organization</th><th>Sites</th><th></th></tr></thead><tbody>${t.orgs.map((o) => `<tr><td><b>${esc(o.name)}</b></td><td>${o.sites.map((s2) => `<span class="tag">${esc(s2.name)}</span>`).join(" ")}</td><td><button type="button" class="btn sm ghost" data-goto-orgsettings="${esc(o.name)}">Settings</button></td></tr>`).join("")}</tbody></table></div>
      <div class="row wrap"><button type="button" class="btn sm" data-settings-add="organization">${ic("plus")}Add organization</button><button type="button" class="btn sm" data-settings-add="site">${ic("plus")}Add site</button><span class="grow"></span><button type="submit" class="btn primary">${ic("send-horizontal")}Propose changes</button></div></form>`)}</div>`; };
  V.orgSettings = () => { if (!isPlatformAdmin() && D.users.find((u) => u.who === "you")?.level !== "Maintainer") return adminOnly("Organization settings");
    const t = curTenant(), o = t.orgs.find((x) => x.name === curScope[1]) ?? t.orgs[0];
    return `<div class="view">${head(`Organization settings · ${o.name}`, "settings-2", "Applies to the organization chosen in the scope bar. Saving creates a proposal for review.", authority("site-config inventory + NetBox tags", "network"))}
    ${comp("K.org-settings", "Organization settings", `<form class="card card-b stack" id="org-form">
      <div class="row wrap" style="gap:10px"><label class="grow"><span class="label">Organization name</span><input class="input" id="os-name" value="${esc(o.name)}"></label>
        <label class="grow"><span class="label">Approvers</span><select class="input" id="os-approvers">${[...new Set(D.users.map((u) => u.group))].map((g) => `<option>${esc(g)}</option>`).join("")}</select></label>
        <label class="grow"><span class="label">Default site</span><select class="input" id="os-site">${o.sites.map((s2) => `<option>${esc(s2.name)}</option>`).join("")}</select></label></div>
      <h3>Business domains</h3><p class="faint" style="margin:0;font-size:12.5px">Which domain each service serves. This drives the domain tiles on Active Work. Stored as service inventory tags.</p>
      <div class="tbl-wrap"><table class="tbl"><thead><tr><th>Service</th><th>Business domain</th></tr></thead><tbody>${tenantServices().map((x) => `<tr><td>${ic(x.icon)} ${esc(x.name)}</td><td><select class="input" data-os-domain="${x.id}" style="height:30px">${D.domains.map(([d]) => `<option ${d === x.domain ? "selected" : ""}>${d}</option>`).join("")}</select></td></tr>`).join("") || `<tr><td colspan="2" class="faint">No services in this organization</td></tr>`}</tbody></table></div>
      <div class="row wrap"><a class="btn sm" href="#access">${ic("users")}Members and roles</a><span class="grow"></span><button type="submit" class="btn primary">${ic("send-horizontal")}Propose changes</button></div></form>`)}</div>`; };
  document.addEventListener("click", (e) => {
    const og = e.target.closest("[data-goto-orgsettings]"); if (og) { location.hash = "orgsettings"; return; }
    const add = e.target.closest("[data-settings-add]"); if (!add) return;
    const what = add.dataset.settingsAdd, sc = curScope.length ? curScope : ["Uhstray.io", "Platform", "site-hq", "prod"];
    newProposal({ key: `AC-${seq++}`, title: `Add ${what} to ${curTenant().name}`, state: "proposed", st: "warn", agent: "skynet", by: "you", via: "Tenant settings", age: "now", ask: `Add a new ${what}.`, scope: sc.slice(), target: what,
      opa: { decision: "allow", rule: "Tenant structure change as code", cond: "Platform admin approval" },
      steps: [["Draft change", "done"], ["Policy (OPA)", "done"], ["Approval", "wait"], ["Inventory change", "todo"], ["Apply groups", "todo"]],
      tasks: [{ name: `${what} in tenant inventory`, wf: "site-config PR (inventory)", st: "todo" }, { name: "Groups and bindings", wf: "Deploy Authentik (blueprints)", st: "todo" }] });
  });
  document.addEventListener("submit", (e) => {
    if (e.target.id !== "tenant-form" && e.target.id !== "org-form") return; e.preventDefault(); e.stopPropagation();
    const tenant = e.target.id === "tenant-form", sc = curScope.length ? curScope : ["Uhstray.io", "Platform", "site-hq", "prod"];
    const changed = tenant ? [] : [...document.querySelectorAll("[data-os-domain]")].filter((sel) => sel.value !== D.svc(sel.dataset.osDomain)?.domain).map((sel) => `${D.svc(sel.dataset.osDomain).name} → ${sel.value}`);
    newProposal({ key: `AC-${seq++}`, title: tenant ? `Tenant settings change: ${curTenant().name}` : `Organization settings change: ${sc[1]}${changed.length ? ` (${changed.length} domain ${changed.length === 1 ? "tag" : "tags"})` : ""}`, state: "proposed", st: "warn", agent: "skynet", by: "you", via: tenant ? "Tenant settings" : "Organization settings", age: "now",
      ask: changed.length ? `Retag services: ${changed.join(", ")}.` : "Update settings.", scope: sc.slice(), target: tenant ? curTenant().name : sc[1],
      opa: { decision: "allow", rule: "Settings change as code", cond: tenant ? "Platform admin approval" : "Organization admin approval" },
      steps: [["Draft change", "done"], ["Policy (OPA)", "done"], ["Approval", "wait"], ["Apply as code", "todo"]],
      tasks: [{ name: tenant ? "Tenant defaults and structure" : "Organization settings and service domain tags", wf: "site-config PR (inventory)", st: "todo" }] });
  }, true);
  V.work = () => {
    const lanes = [["proposed", "Awaiting approval"], ["queued", "Queued"], ["running", "Running"], ["blocked", "Blocked"], ["failed", "Failed"], ["done", "Done"]];
    return `<div class="view">${head("Work", "list-todo", "Every request, job and schedule across your scope. Select one to see the agent, its tasks and the Semaphore workflows behind them.", authority("skynet + Semaphore", "workflow"))}
      ${comp("K.work-lanes", "Work list grouped by state", `<div class="cols">${lanes.map(([s, l]) => { const ws = D.work.filter((w) => w.state === s); return ws.length ? `<div class="stack" style="gap:8px"><div class="row"><span class="label grow">${l}</span><span class="faint num">${ws.length}</span></div>${ws.map((w) => strip(w)).join("")}</div>` : ""; }).join("")}</div>`)}</div>`;
  };
  V.topology = () => `<div class="view">${head("Topology", "waypoints", "Services by layer of the guardrails model, joined by how they integrate. Select a service to trace what it depends on and what depends on it.", authority("NetBox + service inventory", "network"))}
    ${comp("K.topology", "Dependency map (4-layer)", `<div class="card"><div class="card-h"><h3>site-hq · prod</h3>${legend()}</div><div class="card-b">${topology()}</div></div>`)}</div>`;
  V.services = () => `<div class="view">${head("Services", "boxes", "Everything agent-cloud deploys in this scope. Red and amber rows say why.", authority("Semaphore conformance → NetBox", "workflow"), `<button type="button" class="btn primary" data-reqsvc>${ic("package-plus")}Request a service</button>`)}${svcComposer()}${siteBanner()}
    ${comp("K.service-table", "Service table with reasons", `<div class="card tbl-wrap"><table class="tbl"><thead><tr><th>Service</th><th>Layer</th><th>Status</th><th>Host</th><th>Integrations</th><th></th></tr></thead><tbody>${tenantServices().length ? "" : `<tr><td colspan="6"><div class="empty">${ic("boxes", "i-lg")}<b>No services recorded for this tenant</b>The example data only places services under the first tenant.</div></td></tr>`}${tenantServices().map((s) => `<tr class="svc-row" data-svc-row="${s.id}" tabindex="0" title="Open ${esc(s.name)} details"><td><span class="row">${ic(s.icon)}<b>${esc(s.name)}</b>${ic("chevron-right", "faint")}</span><span class="faint" style="font-size:12px">${esc(s.kind)}</span></td><td>${LANES.find((l) => l[0] === s.layer)[1]}</td><td>${st(s.st)}${s.why ? `<div class="faint" style="font-size:12px">${esc(s.why)}</div>` : ""}</td><td>${hostCell(s)}</td><td class="num">${D.edges.filter((e) => e[0] === s.id || e[1] === s.id).length}</td><td><button class="btn sm" data-act="queue-svc" data-svc="${s.id}">${ic("plus")}Queue work</button></td></tr>`).join("")}</tbody></table></div>`)}</div>`;
  // capacity rolls up node → cluster → site, so every level answers "where can this work land?"
  let computeLevel = "node";
  const capOf = (hs) => {
    const live = hs.filter((h) => D.capacity[h.id]);
    const a = { cores: 0, mem: 0, usedC: 0, usedM: 0, n: live.length, worst: "ok", vms: 0 };
    live.forEach((h) => { const c = D.capacity[h.id]; a.cores += c.cores; a.mem += c.mem; a.usedC += c.down ? c.cores : (c.cores * c.cpu) / 100; a.usedM += c.down ? c.mem : (c.mem * c.memc) / 100; a.worst = rank[h.st] > rank[a.worst] ? h.st : a.worst; a.vms += D.vms.filter((v) => v.node === h.id).length; });
    a.cpu = a.cores ? Math.round((a.usedC / a.cores) * 100) : 0; a.memc = a.mem ? Math.round((a.usedM / a.mem) * 100) : 0;
    a.freeC = Math.round(a.cores - a.usedC); a.freeM = Math.round(a.mem - a.usedM);
    return a;
  };
  const capBar = (label, pct, text) => `<div class="row"><span class="faint grow" style="font-size:12px">${label}</span><span class="num" style="font-size:12px">${text}</span></div><div class="bar"><i class="${pct > 85 ? "warn" : ""}" style="width:${pct}%"></i></div>`;
  const capCard = (e, best) => `<div class="card card-b stack cap-card" style="gap:8px" data-resource="${e.kind}:${e.id}" title="Open ${esc(e.name)}">
      <div class="row"><span class="dot ${e.a.worst}"></span><b class="mono grow">${esc(e.name)}</b>${best ? `<span class="tag accent">${ic("arrow-down-to-line")}Most headroom</span>` : ""}${st(e.a.worst)}</div>
      <span class="faint" style="font-size:12px">${esc(e.path)}</span>
      ${e.down ? `<div class="faint" style="font-size:12.5px">Unreachable: capacity counted as unavailable.</div>` : `${capBar("CPU", e.a.cpu, `${e.a.cpu}% of ${e.a.cores} cores`)}${capBar("Memory committed", e.a.memc, `${e.a.memc}% of ${e.a.mem} GiB`)}`}
      <div class="row wrap"><span class="grow" style="font-size:12.5px"><b class="num">${Math.max(0, e.a.freeC)}</b> cores · <b class="num">${Math.max(0, e.a.freeM)}</b> GiB free${e.count ? ` <span class="faint">· ${e.count}</span>` : ""}</span>
        ${e.kind === "Node" ? `<button class="btn sm ghost" data-grafana-scope="node=${esc(e.name)} · metrics" title="Node charts open in Grafana">${ic("external-link")}Grafana</button>` : ""}<button class="btn sm" data-qhere="${e.kind}: ${esc(e.name)}" data-node="${esc(e.id)}">${ic("plus")}Queue work here</button></div></div>`;
  function computeBody(level) {
    const t = D.tenants.find((x) => x.name === curScope[0]) ?? D.tenants[0];
    const sites = t.orgs.flatMap((o) => o.sites.map((s) => ({ ...s, org: o.name }))).filter((s) => capOf(hosts(s)).n);
    let items = [];
    if (level === "site") items = sites.map((s) => { const a = capOf(hosts(s)); return { kind: "Site", id: s.id, name: s.name, path: `${opts.tenantInSidebar ? s.org : `${t.name} › ${s.org}`}${s.provider ? " · " + s.provider : ""} · ${s.envs.join(" · ")}`, a, count: `${s.clusters.length} clusters · ${a.n} nodes · ${a.vms} VMs` }; });
    if (level === "cluster") items = sites.flatMap((s) => s.clusters.filter((c) => capOf(c.hosts).n).map((c) => { const a = capOf(c.hosts); return { kind: "Cluster", id: c.id, name: c.name, path: `${s.name} · ${c.kind}`, a, count: `${a.n} nodes · ${a.vms} VMs` }; }));
    if (level === "node") items = sites.flatMap((s) => s.clusters.flatMap((c) => c.hosts.filter((h) => D.capacity[h.id]).map((h) => { const a = capOf([h]); return { kind: "Node", id: h.id, name: h.name, path: `${s.name} › ${c.name}${D.capacity[h.id].gpu ? " · GPU" : ""}`, a, down: D.capacity[h.id].down, count: a.vms ? `${a.vms} VMs` : h.role ?? "bare metal" }; })));
    if (level === "vm") {
      const vms = D.vms.map((v) => ({ v, f: findHost(v.node) })).filter((x) => x.f && x.f.path[0] === t.name);
      return `<div class="card tbl-wrap"><table class="tbl"><thead><tr><th>ID</th><th>VM</th><th>Site</th><th>Cluster</th><th>Node</th><th>Status</th><th>vCPU</th><th>Memory</th><th>CPU</th><th></th></tr></thead><tbody>${vms.map(({ v, f }) => `<tr><td class="mono num">${v.vmid}</td><td class="mono"><b>${v.name}</b></td><td class="mono">${esc(f.path[2])}</td><td class="mono">${esc(f.path[3])}</td><td class="mono">${v.node}</td><td>${st(v.st)}</td><td class="num">${v.cpu}</td><td class="num">${v.mem} GiB</td><td style="min-width:110px"><div class="bar"><i class="${v.cpuPct > 70 ? "warn" : ""}" style="width:${v.cpuPct}%"></i></div></td><td class="row"><button class="btn sm ghost" data-grafana-scope="vm=${v.name} · metrics" title="VM charts open in Grafana">${ic("external-link")}Grafana</button><button class="btn sm ghost" data-qhere="VM: ${v.name}" data-node="">${ic("plus")}Queue</button><button class="btn sm ghost">Resize</button></td></tr>`).join("")}</tbody></table></div>`;
    }
    const usable = items.filter((e) => !e.down);
    const best = usable.length > 1 ? usable.reduce((m, e) => (e.a.freeM > m.a.freeM ? e : m)) : null;
    return `<div class="cols">${items.map((e) => capCard(e, e === best)).join("")}</div>`;
  }
  const LEVELS = [["site", "Site"], ["cluster", "Cluster"], ["node", "Node"], ["vm", "VM"]];
  document.addEventListener("click", (e) => {
    const b = e.target.closest("[data-clevel]"); if (!b) return;
    computeLevel = b.dataset.clevel;
    b.parentElement.querySelectorAll("button").forEach((x) => x.setAttribute("aria-pressed", x === b));
    const body = document.getElementById("compute-body"); body.innerHTML = computeBody(computeLevel); after(body);
  });
  V.compute = () => {
    const t = D.tenants.find((x) => x.name === curScope[0]) ?? D.tenants[0];
    return `<div class="view">${head("Compute", "server", "Capacity at every level, rolled up from the nodes below it, so you can see where new work fits. Resizes and new VMs run as Semaphore templates, never console clicks.", authority("Proxmox API", "server"), `<button class="btn primary">${ic("plus")}Request a VM</button>`)}${siteBanner()}
    ${comp("K.compute-levels", "Capacity by site / cluster / node / VM", `<div class="stack"><div class="row wrap"><span class="label">Level</span><div class="seg" role="group" aria-label="Capacity level">${LEVELS.map(([id, l]) => `<button type="button" data-clevel="${id}" aria-pressed="${id === computeLevel}">${l}</button>`).join("")}</div><span class="faint grow" style="font-size:12px">${opts.tenantInSidebar ? "This tenant" : `Showing ${esc(t.name)} · change tenant in the scope bar`}</span></div>
      <div id="compute-body">${computeBody(computeLevel)}</div></div>`)}</div>`;
  };
  const accessUsers = () => (opts.tenantInSidebar ? D.users.filter((u) => u.scope.startsWith(curTenant().name)) : D.users);
  function accessScopes() {
    if (!opts.tenantInSidebar) return ["Kestrel Robotics › Research › site-k1 › dev", "Kestrel Robotics › Research", "Uhstray.io › Platform › site-hq › prod"];
    const t = curTenant();
    return ["Whole tenant", ...t.orgs.flatMap((o) => [o.name, ...o.sites.flatMap((s) => [`${o.name} › ${s.name}`, ...s.envs.map((e) => `${o.name} › ${s.name} › ${e}`)])])];
  }
  V.access = () => `<div class="view">${head("Access", "users", "People get roles at a tenant, org, site or environment. The console writes Authentik groups as code; every other system derives its permissions from them.", authority("Authentik groups", "fingerprint"))}
    ${comp("K.add-user", "Add user in one row", `<form class="card card-b stack" data-form="add-user"><div class="row wrap" style="align-items:flex-end;gap:10px">
      <label class="grow" style="min-width:200px"><span class="label">Person</span><input class="input" id="au-email" value="dana@kestrel.example" aria-label="Email"></label>
      <label style="min-width:220px"><span class="label">Scope</span><select class="input" id="au-scope">${accessScopes().map((o) => `<option>${esc(o)}</option>`).join("")}</select></label>
      <div><span class="label">Access level</span><div class="seg" role="radiogroup" id="au-level">${["Admin", "Maintainer", "Developer", "User"].map((l) => `<button type="button" aria-pressed="${l === "Developer"}" data-level="${l}">${l}</button>`).join("")}</div></div>
      <button class="btn primary" type="submit">${ic("user-plus")}Add</button></div>
      <div class="muted" style="font-size:12.5px" data-preview>Joins Authentik group <span class="mono">kestrel-research-dev-developers</span> · Semaphore <b>task_runner</b> · NetBox <b>view + propose</b> · own gateway key. Applied by the Authentik blueprint deploy.</div></form>`)}
    ${comp("K.people", "People and scoped roles", `<div class="card tbl-wrap"><table class="tbl"><thead><tr><th>Person</th><th>Scope</th><th>Level</th><th>Authentik group</th></tr></thead><tbody>${accessUsers().map((u) => `<tr><td>${person(u.who)}<div class="faint mono" style="font-size:11.5px">${esc(u.email)}</div></td><td>${esc(opts.tenantInSidebar ? u.scope.replace(/^[^›(]+(›\s*|\(all\))?/, "").trim() || "Whole tenant" : u.scope)}</td><td><span class="lvl ${u.level}">${u.level}</span></td><td class="mono">${esc(u.group)}</td></tr>`).join("")}</tbody></table></div>`)}
    ${comp("K.role-map", "Access level → per-system roles", `<div class="card"><div class="card-h"><h3>How each access level maps into each system</h3><button class="btn sm">${ic("pencil")}Edit mapping</button></div><div class="tbl-wrap"><table class="tbl"><thead><tr><th>Level</th><th>Semaphore</th><th>NetBox</th><th>OpenBao</th><th>agentgateway</th><th>GitHub</th></tr></thead><tbody>${D.roleMap.map((r) => `<tr><td><span class="lvl ${r.level}">${r.level}</span></td><td>${r.semaphore}</td><td>${r.netbox}</td><td>${r.openbao}</td><td>${r.agw}</td><td>${r.gh}</td></tr>`).join("")}</tbody></table></div></div>`)}</div>`;
  V.secrets = () => `<div class="view">${head("Secrets", "key-round", "Paths, readers and rotation age. The console never displays a secret value; rotation runs as a Semaphore template.", authority("OpenBao", "key-round"))}
    ${comp("K.secret-paths", "Secret paths (names only)", `<div class="card tbl-wrap"><table class="tbl"><thead><tr><th>Path</th><th>Fields</th><th>Last rotated</th><th>Read by</th><th></th></tr></thead><tbody>${D.secrets.map((s) => `<tr><td class="mono">${esc(s.path)}${s.note ? `<div class="faint" style="font-family:var(--sans);font-size:12px">${esc(s.note)}</div>` : ""}</td><td class="num">${s.fields}</td><td class="num">${s.rotated}</td><td>${s.readers.map((r) => `<span class="tag">${esc(r)}</span>`).join(" ")}</td><td><button class="btn sm ghost">${ic("rotate-ccw")}Rotate</button></td></tr>`).join("")}</tbody></table></div>`)}
    ${comp("K.approles", "AppRoles and policies", `<div class="cols">${D.approles.map((a) => `<div class="card card-b stack" style="gap:6px"><div class="row"><b class="mono grow">${a.name}</b>${st(a.st)}</div><span class="mono faint" style="font-size:12px">${a.policy} → ${esc(a.scope)}</span>${a.note ? `<span class="muted" style="font-size:12.5px">${esc(a.note)}</span>` : ""}</div>`).join("")}</div>`)}</div>`;
  // policy changes never self-apply: propose → drafted plan → work item awaiting approval → reviewed PR → deploy
  const POL = { open: false, text: "", pkg: "agentcloud.netbox.device", drafted: false };
  const polForm = () => `<div class="card card-b stack" id="pol-box" data-policy>
      <div class="row"><h3 class="grow">Propose a policy change</h3><button class="iconbtn" data-pol="discard" aria-label="Close">${ic("x")}</button></div>
      <div class="row wrap" style="align-items:flex-end;gap:10px">
        <label class="grow" style="min-width:240px"><span class="label">What should change?</span><textarea class="input" id="pol-text" rows="2" style="height:auto;padding:8px 10px">${esc(POL.text)}</textarea></label>
        <label style="min-width:220px"><span class="label">Policy</span><select class="input" id="pol-pkg">${D.policies.map((p) => `<option value="${p.pkg}" ${p.pkg === POL.pkg ? "selected" : ""}>${esc(p.name)}</option>`).join("")}</select></label>
        <button class="btn primary" type="button" data-pol="draft">${ic("sparkles")}Draft plan</button></div>
      ${POL.drafted ? `<div class="stack" style="gap:10px">
        <div class="label">Drafted by skynet · nothing has changed yet</div>
        ${vsteps({ agent: "skynet", steps: [["Draft Rego change and test", "done"], ["Run opa test and replay 24h", "done"], ["Admin approval", "wait"], ["Merge reviewed PR", "todo"], ["Deploy policy", "todo"]],
          tasks: [{ wf: "PR: policies/agentcloud/agent_actions.rego", name: "Rego change and matching test case" }] }, "plan")}
        <p class="faint" style="margin:0;font-size:12px">Deploy runs <span class="mono">deploy-opa.yml</span>, which no Semaphore template wraps yet.</p>
        <div class="policy cond">${ic("info")}<span><b>Replay impact:</b> 4 requests denied in the last 24h would now be allowed; no request that was allowed would become denied. <span class="faint">(example figures)</span></span></div>
        <pre class="log">  <span class="faint"># example diff</span>
  device_roles_allowed := {"server", "switch", "firewall"<span class="ok">, "gpu-node"</span>}
<span class="ok">+ test_gpu_node_device_allowed if { allow with input as {"action": "netbox.device.create", "role": "gpu-node"} }</span></pre>
        <div class="row wrap"><button class="btn primary" type="button" data-pol="create">${ic("plus")}Create work item for approval</button><button class="btn ghost" type="button" data-pol="redraft">${ic("pencil")}Edit and redraft</button></div>
      </div>` : `<p class="faint" style="margin:0;font-size:12.5px">skynet drafts the Rego change, runs the tests and replays recent decisions. You review the plan before anything is created.</p>`}
    </div>`;
  function proposePolicy(key) {
    const w = D.work.find((x) => x.key === key);
    Object.assign(POL, { open: true, drafted: false, text: w ? `Allow what ${w.key} was denied: ${w.opa.rule}` : "" });
    document.querySelector(".drawer")?.remove();
    if (location.hash === "#policy") dispatchEvent(new HashChangeEvent("hashchange")); else location.hash = "policy";
  }
  document.addEventListener("click", (e) => {
    const b = e.target.closest("[data-pol]"); if (!b) return;
    const box = () => document.getElementById("pol-box"), sync = () => { POL.text = document.getElementById("pol-text")?.value ?? POL.text; POL.pkg = document.getElementById("pol-pkg")?.value ?? POL.pkg; };
    const act = b.dataset.pol;
    if (act === "open") { POL.open = true; POL.drafted = false; dispatchEvent(new HashChangeEvent("hashchange")); return; }
    if (act === "discard") { Object.assign(POL, { open: false, drafted: false, text: "" }); dispatchEvent(new HashChangeEvent("hashchange")); return; }
    sync();
    if (act === "draft") { if (!POL.text.trim()) { toast("Describe the change first.", "info"); return; } POL.drafted = true; }
    if (act === "redraft") POL.drafted = false;
    if (act === "create") {
      const w = { key: `AC-${seq++}`, title: `Policy change: ${POL.text.length > 46 ? POL.text.slice(0, 44) + "…" : POL.text}`, state: "proposed", st: "warn", agent: "skynet", by: "you", via: "Policy proposal", age: "now", ask: POL.text,
        scope: curScope.length ? curScope : ["Uhstray.io", "Platform", "site-hq", "prod"], target: "opa",
        opa: { decision: "allow", rule: "Policy changes are proposals and never apply themselves", cond: "Needs one Admin approval, then a reviewed PR merge" },
        steps: [["Draft Rego", "done"], ["Tests + replay", "done"], ["Approval", "wait"], ["Merge PR", "todo"], ["Deploy OPA", "todo"]],
        tasks: [{ name: "Rego change and test case on a branch", wf: "PR: policies/agentcloud/agent_actions.rego", st: "done" },
          { name: "Run opa test and replay 24h of decisions", wf: "OPA test run", st: "done" },
          { name: "Deploy the merged policy", wf: "deploy-opa.yml (Semaphore template not declared yet)", st: "todo" }] };
      D.work.unshift(w); Object.assign(POL, { open: false, drafted: false, text: "" });
      toast(`${w.key} created. It waits for Admin approval before anything merges.`, "sparkles");
      window.onWorkChanged?.();
      if (window.onWorkCreated) window.onWorkCreated(w); else dispatchEvent(new HashChangeEvent("hashchange"));
      return;
    }
    box().outerHTML = polForm(); after(document.getElementById("view") ?? document);
  });
  V.policy = () => `<div class="view">${head("Policy", "scale", "What agents may do, and why a request was allowed or denied. Policy changes are Rego in Git, proposed here and merged through review.", authority("OPA (Rego in Git)", "scale"), `<button class="btn primary" data-pol="open">${ic("file-code-2")}Propose a change</button>`)}
    ${POL.open ? comp("K.policy-propose", "Policy proposal: draft plan → work item", polForm()) : ""}
    ${comp("K.policy-list", "Policy packages with deny counts", `<div class="card tbl-wrap"><table class="tbl"><thead><tr><th>Policy</th><th>Package</th><th>Decisions 24h</th><th>Denied</th><th>Status</th></tr></thead><tbody>${D.policies.map((p) => `<tr><td><b>${p.name}</b>${p.note ? `<div class="faint" style="font-size:12px">${p.note}</div>` : ""}</td><td class="mono">${p.pkg}</td><td class="num">${p.decisions}</td><td class="num">${p.denies}</td><td>${st(p.st)}</td></tr>`).join("")}</tbody></table></div>`)}
    ${comp("K.decision-log", "Recent decisions", `<div class="card"><div class="card-h"><h3>Recent decisions</h3></div><ul class="list">${D.work.slice(0, 6).map((w) => `<li class="row" style="padding:10px 14px"><span class="strip-key">${w.key}</span><span class="grow">${esc(w.title)}</span>${w.opa.decision === "deny" ? st("crit", "Denied") : st("ok", "Allowed")}</li>`).join("")}</ul></div>`)}</div>`;
  V.llm = () => `<div class="view">${head("LLM access", "message-square-lock", "Who can call the inference gateway, with which models, and how much of their hourly token budget they have used.", authority("agentgateway", "door-open"))}
    ${comp("K.provision-key", "Provision a key in one row", `<form class="card card-b row wrap" style="align-items:flex-end;gap:10px" data-form="key">
      <label style="min-width:190px"><span class="label">User (from Access)</span><select class="input" id="pk-user">${D.users.map((u, i) => `<option value="${i}" ${u.who === "sam" ? "selected" : ""}>${esc(D.people[u.who].name)} · ${esc(u.level)}</option>`).join("")}</select></label>
      <label style="min-width:150px"><span class="label">User ID</span><input class="input mono" id="pk-uid" value="sam.okafor" readonly title="Filled from the Access record"></label>
      <label class="grow" style="min-width:190px"><span class="label">User email</span><input class="input mono" id="pk-email" value="sam@kestrel.example" readonly title="Filled from the Access record"></label>
      <label style="min-width:140px"><span class="label">Client</span><input class="input" id="pk-name" value="research-team"></label>
      <label style="min-width:180px"><span class="label">Owner group</span><input class="input mono" id="pk-owner" value="kestrel-research-maintainers" readonly title="The user's Authentik group"></label>
      <label style="width:150px"><span class="label">Tokens / hour</span><input class="input num" id="pk-budget" value="250,000,000"></label>
      <button class="btn primary" type="submit">${ic("key-round")}Request key</button>
      <div id="pk-targets" class="pk-targets">${pkTargets()}</div>
      <div id="pk-admin" class="pk-admin">${gwAdminRow(D.users.find((u) => u.who === "sam"))}</div></form>`)}
    ${comp("K.llm-clients", "Clients with budget bars", `<div class="card tbl-wrap"><table class="tbl"><thead><tr><th>Client</th><th>Target agent</th><th>Owner</th><th>Budget used this hour</th><th>Models</th><th></th></tr></thead><tbody>${D.llmClients.map((c) => { const p = Math.round((c.used / c.budget) * 100); return `<tr><td class="mono"><b>${c.name}</b></td><td>${c.agent ? `<span class="tag">${ic("bot")}${esc(c.agent)}</span>` : `<span class="faint">Any</span>`}</td><td>${esc(c.owner)}</td><td style="min-width:180px"><div class="row"><div class="bar grow"><i class="${p > 85 ? "warn" : ""}" style="width:${p}%"></i></div><span class="num" style="font-size:12px">${c.used}k / ${c.budget}k</span></div></td><td class="num">${c.models}</td><td><button class="btn sm ghost">Rotate</button><button class="btn sm ghost danger">Revoke</button></td></tr>`; }).join("")}</tbody></table></div>`)}</div>`;
  const PK = { scopes: new Set(), agents: new Set() };
  const scopeAgents = () => { const m = new Map(); D.agentGroups.filter((g) => PK.scopes.has(g.name)).forEach((g) => g.members.forEach((a) => { if (!m.has(a.name)) m.set(a.name, { a, scopes: [] }); m.get(a.name).scopes.push(g.name); })); return [...m.values()]; };
  function pkTargets() {
    const list = scopeAgents();
    return `<div class="pk-step"><span class="label">1 · Target scopes <span class="faint" style="text-transform:none;letter-spacing:0;font-weight:500">from AI Gateway groupings · pick any</span></span><div class="chips">${D.agentGroups.map((g) => `<button type="button" class="chip" data-pkscope="${esc(g.name)}" aria-pressed="${PK.scopes.has(g.name)}">${PK.scopes.has(g.name) ? ic("check") : ""}${esc(g.name)} <span class="faint">· ${g.gateway === "dev" ? "dev" : "prod"}</span></button>`).join("")}</div></div>
      <div class="pk-step"><span class="label">2 · Target agents <span class="faint" style="text-transform:none;letter-spacing:0;font-weight:500">${PK.scopes.size ? `${PK.agents.size} of ${list.filter((x) => !x.a.planned).length} selected` : "pick a scope first"}</span></span>
        ${PK.scopes.size ? `<div class="chips">${list.map(({ a, scopes }) => `<button type="button" class="chip" data-pkagent="${esc(a.name)}" aria-pressed="${PK.agents.has(a.name)}" ${a.planned ? 'disabled title="Planned: not live yet"' : ""}>${PK.agents.has(a.name) ? ic("check") : ""}${esc(a.name)}${scopes.length > 1 ? ` <span class="faint">· ${scopes.map(esc).join(" + ")}</span>` : ""}${a.planned ? ' <span class="faint">· planned</span>' : ""}</button>`).join("")}</div>` : `<p class="faint" style="margin:0;font-size:12.5px">No scope picked: the key is a plain user key for any agent.</p>`}</div>`;
  }
  document.addEventListener("click", (e) => {
    const sc = e.target.closest("[data-pkscope]"), ag = e.target.closest("[data-pkagent]"); if (!sc && !ag) return;
    if (sc) { const n = sc.dataset.pkscope; PK.scopes.has(n) ? PK.scopes.delete(n) : PK.scopes.add(n); const still = new Set(scopeAgents().map((x) => x.a.name)); [...PK.agents].forEach((a) => still.has(a) || PK.agents.delete(a)); }
    if (ag) { const n = ag.dataset.pkagent; PK.agents.has(n) ? PK.agents.delete(n) : PK.agents.add(n); }
    const box = document.getElementById("pk-targets"); box.innerHTML = pkTargets(); icons();
  });
  const GW_ADMIN_GROUP = "platform-admins"; // agentgateway config: agw_ui_admin_group, default platform-admins
  const gwAdminRow = (u) => u.group === GW_ADMIN_GROUP
    ? `${st("ok", "")}<span><b>Operator UI:</b> ${esc(D.people[u.who].name)} already has agentgateway admin access through <span class="mono">${GW_ADMIN_GROUP}</span>.</span>`
    : `${ic("door-open")}<span class="grow"><b>Operator UI:</b> the gateway admits only <span class="mono">${GW_ADMIN_GROUP}</span> (setting <span class="mono">agw_ui_admin_group</span>). Granting ${esc(D.people[u.who].name)} admin access means a group change, which needs approval.</span><button type="button" class="btn sm" data-gw-admin="${u.who}">${ic("user-plus")}Request admin access</button>`;
  const newProposal = (w) => { D.work.unshift(w); toast(`${w.key} created. It waits for Admin approval.`, "sparkles"); window.onWorkChanged?.(); if (window.onWorkCreated) window.onWorkCreated(w); else drawer(w.key, drill(w)); };
  document.addEventListener("submit", (e) => {
    if (e.target.dataset.form !== "key") return;
    e.preventDefault(); e.stopPropagation();
    const u = D.users[+document.getElementById("pk-user").value], client = document.getElementById("pk-name").value.trim() || "new-client";
    const agents = [...PK.agents], scopes = [...PK.scopes], budget = document.getElementById("pk-budget").value;
    const agent = agents.length ? `${agents.length} ${agents.length === 1 ? "agent" : "agents"} (${agents.join(", ")})` : scopes.length ? `scopes ${scopes.join(" + ")}` : "";
    newProposal({ key: `AC-${seq++}`, title: `Gateway key: ${client}${agent ? ` for ${agents.length ? `${agents.length} ${agents.length === 1 ? "agent" : "agents"}` : scopes.join(" + ")}` : ""}`, state: "proposed", st: "warn", agent: "skynet", by: "you", via: "LLM access", age: "now",
      ask: `Provision ${client} for ${D.people[u.who].name} (${u.uid})${agent ? `, limited to ${agent}` : ""}, ${budget} tokens an hour.`, scope: curScope.length ? curScope : ["Uhstray.io", "Platform", "site-hq", "prod"], target: "agentgateway",
      opa: { decision: "allow", rule: "Budget within the requester's ceiling", cond: "New client identity needs one Admin approval" },
      steps: [["Parse request", "done"], ["Policy (OPA)", "done"], ["Approval", "wait"], ["Add client to inventory", "todo"], ["Deploy agentgateway", "todo"], ["Hand off key", "todo"]],
      tasks: [{ name: `client_${client} in inventory${agent ? `, bound to ${agent}` : ""}`, wf: "site-config PR (inventory)", st: "todo" }, { name: "Render key hash and reload", wf: "Deploy agentgateway", st: "todo" }, { name: "Deliver the key without printing it", wf: "Back Up Credentials to site-config", st: "todo" }] });
  }, true);
  document.addEventListener("click", (e) => {
    const g = e.target.closest("[data-gw-admin]"); if (!g) return;
    const u = D.users.find((x) => x.who === g.dataset.gwAdmin), name = D.people[u.who].name;
    newProposal({ key: `AC-${seq++}`, title: `Gateway operator UI access: ${name}`, state: "proposed", st: "warn", agent: "skynet", by: "you", via: "LLM access", age: "now",
      ask: `Give ${name} (${u.uid}) agentgateway operator UI access.`, scope: curScope.length ? curScope : ["Uhstray.io", "Platform", "site-hq", "prod"], target: "authentik",
      opa: { decision: "allow", rule: "Group change through an Authentik blueprint", cond: `Admin approval required: ${GW_ADMIN_GROUP} is the top role in every app; a narrower group needs agw_ui_admin_group changed` },
      steps: [["Parse request", "done"], ["Policy (OPA)", "done"], ["Approval", "wait"], ["Update group membership", "todo"], ["Verify operator UI login", "todo"]],
      tasks: [{ name: `Add ${u.uid} to the admin group, or set a narrower agw_ui_admin_group`, wf: "Deploy Authentik (blueprints)", st: "todo" }] });
  });

  // the user picked in the key form fills ID, email and owner group straight from the Access record
  document.addEventListener("change", (e) => {
    if (e.target.id !== "pk-user") return;
    const u = D.users[+e.target.value];
    document.getElementById("pk-uid").value = u.uid; document.getElementById("pk-email").value = u.email; document.getElementById("pk-owner").value = u.group;
    const row = document.getElementById("pk-admin"); row.innerHTML = gwAdminRow(u); icons();
  });
  document.addEventListener("click", (e) => { const o = e.target.closest("[data-open-work]"); if (o) { const w = D.work.find((x) => x.key === o.dataset.openWork); if (w) drawer(w.key, drill(w)); } });
  const memberSt = { run: ["run", "Working"], idle: ["idle", "Idle"], warn: ["warn", "Waiting"], crit: ["crit", "Blocked"] };
  V.agents = () => `<div class="view">${head("AI Gateway", "door-open", "Agent groups and roles come from the gateway configuration. Each group calls models through one client identity with its own models and hourly token budget; skynet orchestrates across groups.", authority("agentgateway config + skynet", "door-open"))}
    ${[["prod", "Production gateway", "agentgateway · prod → vLLM on spark-pair"], ["dev", "Development gateway", "agentgateway · local-dev → LM Studio · developer workflows and agents"]].map(([gw, title, sub]) => `<div class="stack" style="gap:8px"><div class="row wrap"><h2 class="grow">${title}</h2><span class="faint" style="font-size:12.5px">${sub}</span></div>
    ${comp(gw === "prod" ? "K.gateway-groups" : "K.gateway-dev", gw === "prod" ? "Agent groups and roles from gateway config" : "Development gateway: developer agents", `<div class="cols">${D.agentGroups.filter((g) => g.gateway === gw).map((g) => { const c = D.llmClients.find((x) => x.name === g.client), pct = c ? Math.round((c.used / c.budget) * 100) : 0, busy = g.members.filter((m) => m.st === "run").length;
      return `<div class="card"><div class="card-h"><h3>${esc(g.name)}</h3><span class="tag mono" title="Gateway client identity">${ic("key-round")}${esc(g.client)}</span></div>
        <div class="card-b stack" style="gap:10px"><span class="faint" style="font-size:12.5px">${esc(g.note)} · ${g.members.length} ${g.members.length === 1 ? "agent" : "agents"}, ${busy} working</span>
        ${c ? `<div class="row"><div class="bar grow"><i class="${pct > 85 ? "warn" : ""}" style="width:${pct}%"></i></div><span class="num" style="font-size:12px">${c.used}k / ${c.budget}k tokens this hour · ${c.models} ${c.models === 1 ? "model" : "models"}</span></div>` : ""}
        <ul class="list agent-list">${g.members.map((m0) => { const L = m0.dynamic ? roleLoad(m0.name) : null, m = L ? { ...m0, st: L.st, task: L.now[0]?.key } : m0; return `<li class="${m.lead ? "lead" : "sub"}">${m.initials ? roleTile(m) : `<span class="av" style="background:${m.color}">${ic(m.icon)}</span>`}<span class="grow"><b>${esc(m.name)}</b><span class="faint" style="display:block;font-size:12px">${esc(m.role)}${m.why ? ` · ${esc(m.why)}` : ""}</span></span>${m.task ? `<button class="btn sm ghost mono" data-open-work="${m.task}">${m.task}</button>` : ""}${m.planned ? `<span class="tag">Planned</span>` : st(...memberSt[m.st])}</li>`; }).join("")}</ul></div></div>`; }).join("")}</div>`)}</div>`).join("")}
    <p class="faint" style="margin:0;font-size:12px">Sub-agent names and roles are examples; in the console they come from the gateway's group configuration.</p></div>`;
  V.cicd = () => `<div class="view">${head("CI/CD", "git-pull-request-arrow", "GitHub Actions across agent-cloud repositories and the self-hosted runner pool.", authority("GitHub Actions", "git-pull-request-arrow"))}
    ${comp("K.delivery", "Delivery context: review · checks · promotion path", `<div class="card"><div class="card-h">${ic("git-branch", "i-lg")}<h2>Delivery context</h2><span class="faint" style="font-size:12px">${D.deliveries.length} open changes</span></div>
      ${D.deliveries.map((d) => { const pass = d.checks.filter((c) => c[1] === "ok").length, bad = d.checks.some((c) => c[1] === "crit"), running = d.checks.some((c) => c[1] === "run");
        const path = d.repo === "site-config" ? ["branch", "main"] : ["feature", "dev", "main"];
        return `<div class="delivery">
          <div class="dcol"><span class="label">GitHub review · ${esc(d.repo)} #${d.pr}</span><b>${esc(d.title)}</b><span class="mono faint" style="font-size:12px">${esc(d.branch)} → ${esc(d.base)} · ${d.ahead} ${d.ahead === 1 ? "commit" : "commits"} ahead</span>${st(d.review[0], d.review[1])}${d.work ? `<button class="btn sm ghost mono" data-open-work="${d.work}" style="justify-self:start">${ic("link")}${d.work}</button>` : ""}</div>
          <div class="dcol"><span class="label">GitHub Actions</span><b>${bad ? "Checks failing" : running ? "Checks running" : "Checks passed"}</b>${st(bad ? "crit" : running ? "run" : "ok", `${pass} / ${d.checks.length} checks`)}<ul class="dchecks">${d.checks.map(([n, c]) => `<li>${st(c, "")}<span>${esc(n)}</span></li>`).join("")}</ul></div>
          <div class="dcol"><span class="label">Promotion path</span><div class="dpath">${path.map((p2, i) => { const at = d.repo === "site-config" ? (d.stage === 0 ? 0 : 1) : d.stage; return `${i ? ic("arrow-right") : ""}<span class="dstage ${i < at ? "done" : i === at ? "cur" : ""}">${p2}</span>`; }).join("")}</div><span class="faint" style="font-size:12px">${esc(d.note)}</span></div>
        </div>`; }).join("")}
      <p class="faint" style="margin:0;padding:12px 14px;font-size:12px;border-top:1px solid var(--line)">Changes shown are examples. In production this reads GitHub reviews and checks; self-hosted runners serve only the private repos and never run public agent-cloud workflows.</p></div>`)}
    ${comp("K.runs", "Workflow runs", `<div class="card tbl-wrap"><table class="tbl"><thead><tr><th>Workflow</th><th>Ref</th><th>Status</th><th>Runner</th><th>Age</th><th></th></tr></thead><tbody>${D.runs.map((r) => `<tr><td><b>${r.wf}</b><div class="faint" style="font-size:12px">${r.check}</div></td><td class="mono">${r.ref}</td><td>${st(r.st, r.st === "run" ? "Running" : r.st === "crit" ? "Failed" : "Passed")}${r.why ? `<div class="faint" style="font-size:12px">${r.why}</div>` : ""}</td><td class="mono">${r.runner}</td><td class="num">${r.age}</td><td>${r.st === "crit" ? `<button class="btn sm">${ic("rotate-ccw")}Re-run</button>` : ""}</td></tr>`).join("")}</tbody></table></div>`)}</div>`;
  // Telemetry stops at the cluster: anything finer (a service, a VM) is a jump to Grafana, filtered to that scope
  let telLevel = "site";
  function telItems(level) {
    const t = D.tenants.find((x) => x.name === curScope[0]) ?? D.tenants[0];
    const alerts = (hs) => hs.filter((h) => h.st !== "ok").length;
    if (level === "tenant") return D.tenants.map((x) => ({ kind: "Tenant", key: "tenant", name: x.name, path: `${x.orgs.length} orgs`, hs: hosts(x) }));
    const orgs = t.orgs.map((o) => ({ o, path: opts.tenantInSidebar ? `${o.sites.length} ${o.sites.length === 1 ? "site" : "sites"}` : t.name }));
    if (level === "org") return orgs.map(({ o, path }) => ({ kind: "Org", key: "org", name: o.name, path, hs: hosts(o) }));
    const sites = t.orgs.flatMap((o) => o.sites.map((s) => ({ s, path: opts.tenantInSidebar ? o.name : `${t.name} › ${o.name}` })));
    // two orgs can share one physical site or cluster: show it once, with both orgs' hosts
    const merge = (list) => Object.values(list.reduce((m, e) => { if (m[e.name]) m[e.name].hs = m[e.name].hs.concat(e.hs); else m[e.name] = e; return m; }, {}));
    if (level === "site") return merge(sites.map(({ s, path }) => ({ kind: "Site", key: "site", id: s.id, name: s.name, path: `${path}${s.provider ? " · " + s.provider : ""} · ${s.envs.join(" · ")}`, hs: hosts(s) })));
    return merge(sites.flatMap(({ s }) => s.clusters.map((c) => ({ kind: "Cluster", key: "cluster", id: c.id, name: c.name, path: `${s.name} · ${c.kind}`, hs: c.hosts }))));
  }
  function telCard(e, i) {
    const a = capOf(e.hs), down = e.hs.filter((h) => h.st === "crit").length, warn = e.hs.filter((h) => h.st === "warn").length;
    const seed = e.name.length * 11 + i;
    return `<div class="card card-b stack tel-card" style="gap:10px" ${e.id ? `data-resource="${e.kind}:${e.id}" title="Open ${esc(e.name)}"` : ""}>
      <div class="row"><span class="dot ${a.n ? a.worst : "idle"}"></span><b class="grow">${esc(e.name)}</b>${a.n ? st(a.worst) : st("idle", "No nodes reported")}</div>
      <span class="faint" style="font-size:12px;margin-top:-6px">${esc(e.path)}</span>
      ${a.n ? `<div class="tel-nums"><div><span class="label">CPU</span><b class="num">${a.cpu}%</b></div><div><span class="label">Memory</span><b class="num">${a.memc}%</b></div><div><span class="label">Alerts</span><b class="num">${down + warn}</b></div><div><span class="label">Nodes</span><b class="num">${a.n}</b></div></div>
        <div class="metric"><div class="metric-h"><span class="n">CPU, last 20 min</span></div>${spark(D.series(seed, 40, a.cpu, 14), { color: "var(--e-api)", unit: "%" })}</div>`
        : `<p class="faint" style="margin:0;font-size:12.5px">No compute nodes report here. Open Grafana for this scope's metrics.</p>`}
      <button class="btn sm" data-grafana-scope="${e.key}=${esc(e.name)}">${ic("external-link")}Open in Grafana</button></div>`;
  }
  const TEL_LEVELS = [["tenant", "Tenant"], ["org", "Org"], ["site", "Site"], ["cluster", "Cluster"]];
  const telLevels = () => TEL_LEVELS.filter(([id]) => !(opts.tenantInSidebar && id === "tenant"));
  document.addEventListener("click", (e) => {
    const g = e.target.closest("[data-grafana-scope]");
    if (g) { toast(`Opens Grafana filtered to ${g.dataset.grafanaScope}`, "external-link"); return; }
    const b = e.target.closest("[data-tlevel]"); if (!b) return;
    telLevel = b.dataset.tlevel;
    b.parentElement.querySelectorAll("button").forEach((x) => x.setAttribute("aria-pressed", x === b));
    const body = document.getElementById("tel-body"); body.innerHTML = telBody(); after(body);
  });
  const telBody = () => `<div class="cols">${telItems(telLevel).map(telCard).join("")}</div>`;
  V.telemetry = () => `<div class="view">${head("Telemetry", "activity", "Health rolled up by tenant, org, site and cluster. For a service, VM or log line, open Grafana: it starts filtered to the scope you picked.", authority("o11y (Prometheus · Loki)", "activity"), `<button class="btn" data-grafana-scope="tenant=${esc(curScope[0] ?? D.tenants[0].name)}">${ic("external-link")}Open in Grafana</button>`)}${siteBanner()}
    ${comp("K.telemetry-rollup", "Telemetry rollup by tenant / org / site / cluster", `<div class="stack"><div class="row wrap"><span class="label">Level</span><div class="seg" role="group" aria-label="Telemetry level">${telLevels().map(([id, l]) => `<button type="button" data-tlevel="${id}" aria-pressed="${id === telLevel}">${l}</button>`).join("")}</div><span class="faint grow" style="font-size:12px">${telLevel === "tenant" ? "All tenants" : opts.tenantInSidebar ? "This tenant" : `Showing ${esc(curScope[0] ?? D.tenants[0].name)}`} · no service-level panels by design</span></div>
      <div id="tel-body">${telBody()}</div>${grafanaNote}</div>`)}</div>`;

  return { PQ, pqRow, answerSvc, reqSvcHtml, avatarHtml, isPlatformAdmin, currentRole, domainOfWork, networkMap, roleView, roleLoad, roleTile, roleMember, verifyPlan, vsteps, advance, workMatches, scopeBanner, infraMap, resourcePanel, openResource, inTenant, tenantServices, opts, scopeText, grafanaLinks, proposePolicy, dash, telItems, computeBody, findHost, serviceDetail, openService, hostCell, D, esc, ic, st, agent, person, scope, strip, drill, proposal, parse, spark, grafanaNote, topology, legend, bindTopology, hier, roll, rollup, comp, initPicks, paintPicks, picksDrawer, toast, icons, drawer, after, NAV, V, stateLabel, stateSt, policyBox };
})();
