// SCF Wonder dashboard — LangGraph-driven agentic SCF platform.
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const fmtUsd = (v) => {
  if (v === null || v === undefined || isNaN(v)) return "—";
  return Number(v).toLocaleString("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 2 });
};
const fmtPct = (v) => (v === null || v === undefined ? "—" : `${(Number(v) * 100).toFixed(2)}%`);
const fmtTs = (s) => s ? new Date(s).toLocaleString() : "";
const fmtRevenue = (v) => {
  if (v === null || v === undefined) return `<span class="muted">—</span>`;
  const n = Number(v);
  if (n >= 1e12) return `$${(n / 1e12).toFixed(2)}T`;
  if (n >= 1e9)  return `$${(n / 1e9).toFixed(2)}B`;
  if (n >= 1e6)  return `$${(n / 1e6).toFixed(2)}M`;
  if (n >= 1e3)  return `$${(n / 1e3).toFixed(1)}K`;
  return `$${n.toFixed(0)}`;
};
const renderRatingPill = (r) => r ? `<span class="rating-pill rating-${r}">${r}</span>` : `<span class="muted">—</span>`;

// ---------- tabs ----------
function setupTabs() {
  $$(".nav-link").forEach((link) => {
    link.addEventListener("click", (ev) => {
      ev.preventDefault();
      const tab = link.dataset.tab;
      $$(".nav-link").forEach((l) => l.classList.remove("active"));
      link.classList.add("active");
      $$(".tab-panel").forEach((p) => p.classList.remove("active"));
      $(`#${tab}`).classList.add("active");
      if (tab === "agents") loadEvents();
      if (tab === "programs") loadPrograms();
      if (tab === "companies") loadCompanies();
      if (tab === "dashboard") loadDashboard();
      if (tab === "transactions") loadTransactions();
      if (tab === "graph") renderGraphTab();
    });
  });
}

// ---------- meta ----------
let metaCache = null;
async function loadMeta() {
  metaCache = await fetch("/api/meta").then((r) => r.json());
  $("#base-rate-value").textContent = metaCache.base_rate_pct;
  $("#currency-select").innerHTML = metaCache.currencies.map((c) => `<option value="${c}">${c}</option>`).join("");
}
async function ensureMeta() { if (!metaCache) await loadMeta(); return metaCache; }

// ---------- dashboard ----------
async function loadDashboard() {
  const data = await fetch("/api/summary").then((r) => r.json());
  $("#stat-grid").innerHTML = `
    <div class="stat"><div class="stat-label">Companies</div><div class="stat-value">${data.totals.companies.toLocaleString()}</div><div class="stat-sub">in roster</div></div>
    <div class="stat"><div class="stat-label">Programs</div><div class="stat-value">${data.totals.programs.toLocaleString()}</div><div class="stat-sub">active relationships</div></div>
    <div class="stat"><div class="stat-label">Invoices</div><div class="stat-value">${data.totals.invoices.toLocaleString()}</div><div class="stat-sub">all-time</div></div>
    <div class="stat"><div class="stat-label">Agent events</div><div class="stat-value">${data.totals.agent_events.toLocaleString()}</div><div class="stat-sub">logged</div></div>
    <div class="stat"><div class="stat-label">Base rate</div><div class="stat-value">${(data.base_rate*100).toFixed(2)}%</div><div class="stat-sub">today</div></div>
  `;
  $("#recent-decisions").innerHTML = data.recent_decisions
    .map((d) => renderEvent({ ...d, severity: "DECISION" })).join("") || `<div class="muted">No decisions yet.</div>`;

  $("#status-table").innerHTML =
    `<thead><tr><th>Status</th><th>Count</th><th>Amount (USD)</th></tr></thead>
     <tbody>${Object.entries(data.by_status).sort((a,b) => b[1].count - a[1].count)
        .map(([s, v]) => `<tr><td><span class="badge ${s}">${s}</span></td><td>${v.count.toLocaleString()}</td><td>${fmtUsd(v.amount_usd)}</td></tr>`).join("")}</tbody>`;
  $("#product-table").innerHTML =
    `<thead><tr><th>Product</th><th>Count</th><th>Amount (USD)</th></tr></thead>
     <tbody>${Object.entries(data.by_product)
        .map(([p, v]) => `<tr><td>${p.replaceAll("_"," ")}</td><td>${v.count.toLocaleString()}</td><td>${fmtUsd(v.amount_usd)}</td></tr>`).join("")}</tbody>`;
}

// ---------- events ----------
function renderEvent(e) {
  const sev = e.severity || "INFO";
  return `
    <div class="event severity-${sev}">
      <span class="agent-tag ${e.agent}">${e.agent}</span>
      <div class="body">
        <div class="head">
          <span>${e.action}</span><span>·</span><span>${fmtTs(e.timestamp)}</span>
          ${e.node ? `<span>· node: ${e.node}</span>` : ""}
          ${e.invoice_id ? `<span>· invoice #${e.invoice_id}</span>` : ""}
          ${e.company_id ? `<span>· company #${e.company_id}</span>` : ""}
          ${e.program_id ? `<span>· program #${e.program_id}</span>` : ""}
        </div>
        <div class="msg">${e.message}</div>
      </div>
    </div>`;
}
async function loadEvents() {
  const agent = $("#filter-agent").value;
  const severity = $("#filter-severity").value;
  const params = new URLSearchParams();
  if (agent) params.set("agent", agent);
  if (severity) params.set("severity", severity);
  params.set("limit", "200");
  const data = await fetch("/api/events?" + params.toString()).then((r) => r.json());
  $("#event-list").innerHTML = data.items.map(renderEvent).join("") || `<div class="muted">No events.</div>`;
}

// ---------- programs ----------
async function loadPrograms() {
  const data = await fetch("/api/programs?limit=100").then((r) => r.json());
  const tbody = $("#programs-table tbody");
  tbody.innerHTML = data.items.map((p) => `
    <tr data-id="${p.id}">
      <td>#${p.id}</td>
      <td>${p.name}</td>
      <td><span class="badge">${p.product.replaceAll("_"," ")}</span></td>
      <td>${p.buyer.name}</td>
      <td>${p.seller.name}</td>
      <td>${fmtUsd(p.credit_limit_usd)}</td>
      <td>${fmtUsd(p.utilised_usd)}</td>
      <td><span class="badge ${p.status}">${p.status}</span></td>
      <td><button class="ghost explain-btn" data-id="${p.id}">Explain</button></td>
    </tr>`).join("");
  tbody.querySelectorAll("tr").forEach((tr) => {
    tr.addEventListener("click", (ev) => {
      if (ev.target.classList.contains("explain-btn")) return;
      const id = tr.dataset.id;
      const program = data.items.find((x) => x.id == id);
      if (program) {
        document.querySelector('[data-tab="companies"]').click();
        loadCompanyDetail(program.buyer.id);
      }
    });
  });
  tbody.querySelectorAll(".explain-btn").forEach((btn) => {
    btn.addEventListener("click", async (ev) => {
      ev.stopPropagation();
      const id = btn.dataset.id;
      $("#programs-facility-card").hidden = false;
      $("#programs-facility-body").innerHTML = `<div class="muted">Loading explanation…</div>`;
      const data = await fetch(`/api/programs/${id}/facility`).then((r) => r.json());
      $("#programs-facility-title").textContent = `Facility Limit Explanation — ${data.program.name}`;
      $("#programs-facility-body").innerHTML = renderFacility(data);
      $("#programs-facility-card").scrollIntoView({ behavior: "smooth", block: "start" });
    });
  });
}

function renderFacility(data) {
  const t = data.totals;
  const fxRows = data.open_invoices_by_currency
    .sort((a, b) => b.amount_usd - a.amount_usd)
    .map((r) => `
      <tr>
        <td><span class="badge">${r.currency}</span></td>
        <td>${r.count}</td>
        <td>${r.amount_native.toLocaleString()} ${r.currency}</td>
        <td>${r.fx_to_usd}</td>
        <td>${fmtUsd(r.amount_usd)}</td>
      </tr>`).join("") || `<tr><td colspan="5" class="muted">No live invoices.</td></tr>`;

  const buyerBreakdown = Object.entries(data.buyer_hierarchy_breakdown)
    .map(([k, v]) => `<tr><td>${k}</td><td>${fmtUsd(v)}</td></tr>`).join("");
  const sellerBreakdown = Object.entries(data.seller_hierarchy_breakdown)
    .map(([k, v]) => `<tr><td>${k}</td><td>${fmtUsd(v)}</td></tr>`).join("");
  const stepsHtml = data.explanation.map((s) => `
    <div class="step"><div class="step-num">${s.step}</div>
      <div><div class="step-title">${s.title}</div>
      <div class="step-detail">${s.detail}</div></div></div>`).join("");
  return `
    <div class="kv">
      <div class="k">Program</div><div>#${data.program.id} · ${data.program.name}</div>
      <div class="k">Product</div><div><span class="badge">${data.program.product}</span></div>
      <div class="k">Buyer</div><div>${data.program.buyer.name}</div>
      <div class="k">Seller</div><div>${data.program.seller.name}</div>
      <div class="k">Bilateral limit</div><div>${fmtUsd(t.program_limit_usd)}</div>
      <div class="k">Utilised</div><div>${fmtUsd(t.program_utilised_usd)}</div>
      <div class="k">Program headroom</div><div>${fmtUsd(t.program_headroom_usd)}</div>
      <div class="k">Buyer subtree headroom</div><div>${fmtUsd(t.buyer_subtree_headroom_usd)}</div>
      <div class="k">Seller subtree headroom</div><div>${fmtUsd(t.seller_subtree_headroom_usd)}</div>
      <div class="k">Binding constraint</div><div><span class="badge REVIEW">${t.binding_constraint}</span> at ${fmtUsd(t.binding_headroom_usd)}</div>
    </div>
    <div class="section-title">Live Invoices Aggregated by Currency</div>
    <table class="data-table">
      <thead><tr><th>Currency</th><th>Count</th><th>Native amount</th><th>FX → USD</th><th>USD</th></tr></thead>
      <tbody>${fxRows}</tbody>
      <tfoot><tr><td colspan="4"><strong>Total open exposure</strong></td><td><strong>${fmtUsd(t.open_amount_usd)}</strong></td></tr></tfoot>
    </table>
    <div class="section-title">Step-by-step Reasoning</div>
    <div class="steps">${stepsHtml}</div>
    <div class="grid-2">
      <div><div class="section-title">Buyer Hierarchy Breakdown</div>
        <table class="data-table"><thead><tr><th>Ancestor : product</th><th>Headroom</th></tr></thead>
          <tbody>${buyerBreakdown || `<tr><td colspan="2" class="muted">—</td></tr>`}</tbody></table></div>
      <div><div class="section-title">Seller Hierarchy Breakdown</div>
        <table class="data-table"><thead><tr><th>Ancestor : product</th><th>Headroom</th></tr></thead>
          <tbody>${sellerBreakdown || `<tr><td colspan="2" class="muted">—</td></tr>`}</tbody></table></div>
    </div>`;
}

// ---------- transactions ----------
async function loadTransactions() {
  const data = await fetch("/api/transactions/summary").then((r) => r.json());
  const t = data.totals;
  $("#tx-stat-grid").innerHTML = `
    <div class="stat"><div class="stat-label">Invoices</div><div class="stat-value">${t.invoice_count.toLocaleString()}</div><div class="stat-sub">all-time</div></div>
    <div class="stat"><div class="stat-label">Volume</div><div class="stat-value">${fmtUsd(t.amount_usd)}</div><div class="stat-sub">USD-eq</div></div>
    <div class="stat"><div class="stat-label">Funded</div><div class="stat-value">${fmtUsd(t.funded_usd)}</div><div class="stat-sub">net of fees</div></div>
    <div class="stat"><div class="stat-label">Fees</div><div class="stat-value">${fmtUsd(t.fee_usd)}</div><div class="stat-sub">platform</div></div>
    <div class="stat"><div class="stat-label">Base rate</div><div class="stat-value">${(data.base_rate*100).toFixed(2)}%</div><div class="stat-sub">today</div></div>
  `;
  $("#tx-status-table").innerHTML =
    `<thead><tr><th>Status</th><th>Count</th><th>Amount</th><th>Fees</th></tr></thead>
     <tbody>${Object.entries(data.by_status).sort((a,b) => b[1].count - a[1].count)
        .map(([s, v]) => `<tr><td><span class="badge ${s}">${s}</span></td><td>${v.count.toLocaleString()}</td><td>${fmtUsd(v.amount_usd)}</td><td>${fmtUsd(v.fee_usd)}</td></tr>`).join("")}</tbody>`;
  $("#tx-product-table").innerHTML =
    `<thead><tr><th>Product</th><th>Count</th><th>Amount</th><th>Fees</th></tr></thead>
     <tbody>${Object.entries(data.by_product)
        .map(([p, v]) => `<tr><td>${p.replaceAll("_"," ")}</td><td>${v.count.toLocaleString()}</td><td>${fmtUsd(v.amount_usd)}</td><td>${fmtUsd(v.fee_usd)}</td></tr>`).join("")}</tbody>`;
  $("#tx-currency-table").innerHTML =
    `<thead><tr><th>Currency</th><th>Count</th><th>Native amount</th></tr></thead>
     <tbody>${Object.entries(data.by_currency).sort((a,b) => b[1].count - a[1].count)
        .map(([c, v]) => `<tr><td><span class="badge">${c}</span></td><td>${v.count.toLocaleString()}</td><td>${v.amount_native.toLocaleString()} ${c}</td></tr>`).join("")}</tbody>`;

  $("#tx-top-programs-table tbody").innerHTML = data.top_programs.map((p) => `
    <tr data-id="${p.program_id}">
      <td>#${p.program_id} · ${p.name}</td>
      <td><span class="badge">${p.product.replaceAll("_"," ")}</span></td>
      <td>${p.buyer.name}</td>
      <td>${p.seller.name}</td>
      <td>${p.invoice_count.toLocaleString()}</td>
      <td>${fmtUsd(p.amount_usd)}</td>
      <td><button class="ghost tx-explain-btn" data-id="${p.program_id}">Explain</button></td>
    </tr>`).join("");
  $$("#tx-top-programs-table .tx-explain-btn").forEach((btn) => {
    btn.addEventListener("click", async (ev) => {
      ev.stopPropagation();
      const id = btn.dataset.id;
      $("#facility-card").hidden = false;
      $("#facility-explanation").innerHTML = `<div class="muted">Loading explanation…</div>`;
      const f = await fetch(`/api/programs/${id}/facility`).then((r) => r.json());
      $("#facility-card-title").textContent = `Facility Limit Explanation — ${f.program.name}`;
      $("#facility-explanation").innerHTML = renderFacility(f);
      $("#facility-card").scrollIntoView({ behavior: "smooth", block: "start" });
    });
  });
  $("#tx-recent-table tbody").innerHTML = data.recent_invoices.map((inv) => `
    <tr>
      <td>#${inv.id}</td><td>${inv.invoice_number}</td>
      <td>${inv.seller.name}</td><td>${inv.buyer.name}</td>
      <td>${inv.product.replaceAll("_"," ")}</td>
      <td>${inv.amount.toLocaleString()} ${inv.currency}</td>
      <td>${fmtUsd(inv.amount_usd)}</td>
      <td><span class="badge ${inv.status}">${inv.status}</span></td>
    </tr>`).join("");
}

// ---------- companies ----------
async function loadCompanies() {
  const q = $("#company-search").value.trim();
  const role = $("#company-role").value;
  const rating = $("#company-rating").value;
  const sort = $("#company-sort").value || "name";
  const params = new URLSearchParams();
  if (role) params.set("role", role);
  if (rating) params.set("rating", rating);
  if (q) params.set("q", q);
  if (sort) params.set("sort", sort);
  params.set("limit", "500");
  const data = await fetch("/api/companies?" + params.toString()).then((r) => r.json());
  const items = data.items;
  $("#company-count").textContent = `${items.length.toLocaleString()} of ${data.total.toLocaleString()} companies`;
  const tbody = $("#companies-table tbody");
  tbody.innerHTML = items.map((c) => `
    <tr data-id="${c.id}">
      <td>${c.name}</td>
      <td>${renderRatingPill(c.rating)}</td>
      <td>${c.credit_spread !== null && c.credit_spread !== undefined ? fmtPct(c.credit_spread) : '<span class="muted">—</span>'}</td>
      <td>${fmtRevenue(c.annual_revenue_usd)}</td>
      <td><span class="badge">${c.role}</span></td>
      <td>${c.industry ?? ""}</td>
      <td>${c.country ?? ""}</td>
      <td>${c.parent_name ? c.parent_name : '<span class="muted">—</span>'}</td>
    </tr>`).join("") || `<tr><td colspan="8" class="muted">No matches.</td></tr>`;
  tbody.querySelectorAll("tr[data-id]").forEach((tr) => {
    tr.addEventListener("click", () => loadCompanyDetail(tr.dataset.id));
  });
}

async function loadCompanyDetail(id) {
  const c = await fetch(`/api/companies/${id}`).then((r) => r.json());
  const card = $("#company-detail-card");
  const rp = c.risk_profile;
  const limits = c.credit_limits.map((cl) => `
    <tr><td>${cl.product}</td><td>${fmtUsd(cl.limit_usd)}</td><td>${fmtUsd(cl.utilised_usd)}</td><td>${fmtUsd(cl.headroom_usd)}</td></tr>`).join("")
    || `<tr><td colspan="4" class="muted">No limits.</td></tr>`;
  const programs = c.programs.map((p) => `
    <tr><td>#${p.id}</td><td>${p.name}</td><td><span class="badge">${p.product}</span></td>
        <td>${fmtUsd(p.credit_limit_usd)}</td><td>${fmtUsd(p.utilised_usd)}</td>
        <td><span class="badge ${p.status}">${p.status}</span></td></tr>`).join("")
    || `<tr><td colspan="6" class="muted">No programs.</td></tr>`;
  const childrenTree = c.children.map((ch) =>
    `<li><a href="#" data-id="${ch.id}">${ch.name}</a> <span class="muted">(${ch.role})</span></li>`).join("");
  const parentLine = c.parent
    ? `<li class="root"><a href="#" data-id="${c.parent.id}">${c.parent.name}</a> <span class="muted">(parent)</span></li>` : "";
  card.innerHTML = `
    <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:12px;">
      <div><h2 style="margin-bottom:4px;">${c.name}</h2>
        <div class="muted">${c.industry ?? ""} · ${c.country ?? ""} · ${c.role}</div></div>
      ${rp ? renderRatingPill(rp.rating) : ""}
    </div>
    <div class="kv">
      <div class="k">Legal name</div><div>${c.legal_name ?? "—"}</div>
      <div class="k">Annual revenue</div><div>${fmtUsd(c.annual_revenue_usd)}</div>
      <div class="k">Employees</div><div>${(c.employees ?? 0).toLocaleString()}</div>
      <div class="k">Founded</div><div>${c.founded_year ?? "—"}</div>
      <div class="k">Tax ID</div><div>${c.tax_id ?? "—"}</div>
      <div class="k">Website</div><div>${c.website ?? "—"}</div>
      ${rp ? `
        <div class="k">Credit spread</div><div>${fmtPct(rp.credit_spread)}</div>
        <div class="k">PD (1y)</div><div>${fmtPct(rp.pd_1y)}</div>
        <div class="k">Industry risk</div><div>${fmtPct(rp.industry_risk)}</div>
        <div class="k">Country risk</div><div>${fmtPct(rp.country_risk)}</div>
        <div class="k">Years operated</div><div>${rp.tenure_years}</div>
        <div class="k">Last reviewed</div><div>${fmtTs(rp.last_reviewed)}</div>
        ${rp.notes ? `<div class="k">Underwriter notes</div><div>${rp.notes}</div>` : ""}
      ` : ""}
    </div>
    <div class="section-title">Hierarchy</div>
    <ul class="tree">
      ${parentLine}
      <li class="root"><strong>${c.name}</strong></li>
      ${childrenTree ? `<li class="root"><ul class="tree">${childrenTree}</ul></li>` : `<li class="muted">No children.</li>`}
    </ul>
    <div class="section-title">Credit Limits</div>
    <table class="data-table">
      <thead><tr><th>Product</th><th>Limit</th><th>Used</th><th>Headroom</th></tr></thead>
      <tbody>${limits}</tbody>
    </table>
    <div class="section-title">Programs (${c.programs.length})</div>
    <table class="data-table">
      <thead><tr><th>ID</th><th>Name</th><th>Product</th><th>Limit</th><th>Used</th><th>Status</th></tr></thead>
      <tbody>${programs}</tbody>
    </table>`;
  card.querySelectorAll("a[data-id]").forEach((a) => {
    a.addEventListener("click", (ev) => { ev.preventDefault(); loadCompanyDetail(a.dataset.id); });
  });
}

// ---------- agent graph tab ----------
function renderGraphTab() {
  $("#graph-ascii").textContent = `
                ┌─────────────┐
                │ orchestrator│
                └──────┬──────┘
                       ▼
               ┌───────────────┐
               │ onboarding_ag │───────── (missing + no payload) ──► finalise
               └───────┬───────┘
                       ▼
               ┌───────────────┐
               │ create_invoice│
               └───────┬───────┘
                       ▼
               ┌───────────────┐
               │ ensure_limits │
               └───────┬───────┘
                       ▼
               ┌───────────────┐
               │ find_program  │
               └───┬───────┬───┘
                   │       │
           (no program)  (match)
                   │       │
                   ▼       ▼
     ┌─────────────────┐ ┌───────────────┐
     │ underwrite_prog │ │  limit_check  │
     └───┬──────┬──────┘ └───┬───┬───┬───┘
         │      │            │   │   │
      (decl)  (appr)       hard review ok
         │      │            │   │   │
         ▼      ▼            ▼   ▼   ▼
      finalise limit_check finalise review approve
                                       │       │
                                       ▼       ▼
                                    approve  finalise
                                       │
                                       ▼
                                   finalise`;
  const agents = [
    ["orchestrator", "Validates intake, resolves counterparties, coordinates the team."],
    ["onboarding_agent", "Creates missing counterparties and bootstraps them."],
    ["underwriter_agent", "Risk profiling and new-program underwriting decisions."],
    ["credit_limit_agent", "Hierarchical credit limit arithmetic + reservations."],
    ["transaction_agent", "Program matching + fee pricing + funding."],
    ["review_agent", "Decides on program-limit overages."],
  ];
  const tools = [
    ["lookup_company", "Resolve a company by name (fuzzy)."],
    ["onboard_company", "Create a company row with minimum underwriting inputs."],
    ["build_risk_profile", "Assign rating / PD / spread with policy overrides."],
    ["decide_new_program", "Approve or decline a brand-new bilateral program."],
    ["ensure_limits", "Create GLOBAL + product-specific credit limits."],
    ["hierarchical_headroom", "Walk the corporate tree; return tightest headroom."],
    ["reserve_limits", "Bump utilisation on program + company limits."],
    ["find_program", "Find active program for (buyer, seller, product)."],
    ["price_invoice", "amount × (base_rate + spread) × (tenor + grace) / 360."],
    ["decide_overage", "Approve temp increase if ratings ≥ BBB or within 15% threshold."],
  ];
  $("#agents-table").innerHTML =
    `<thead><tr><th>Agent</th><th>Responsibility</th></tr></thead>
     <tbody>${agents.map(([a, d]) => `<tr><td><span class="agent-tag ${a}">${a}</span></td><td>${d}</td></tr>`).join("")}</tbody>`;
  $("#tools-table").innerHTML =
    `<thead><tr><th>StructuredTool</th><th>What it does</th></tr></thead>
     <tbody>${tools.map(([a, d]) => `<tr><td><code>${a}</code></td><td>${d}</td></tr>`).join("")}</tbody>`;
}

// ---------- new invoice form ----------
const pendingOnboard = { seller: null, buyer: null };
function setupAutocomplete(inputId, panelId, role) {
  const input = $(inputId);
  const panel = $(panelId);
  const statusEl = $(inputId.replace("-input", "-status"));
  let activeReq = 0, checkReq = 0;
  const side = role === "SELLER" ? "seller" : "buyer";

  async function refreshStatus() {
    const q = input.value.trim();
    if (!q) { statusEl.textContent = ""; statusEl.className = "match-status"; pendingOnboard[side] = null; return; }
    const seq = ++checkReq;
    const r = await fetch(`/api/companies/exists?name=${encodeURIComponent(q)}`).then((r) => r.json());
    if (seq !== checkReq) return;
    if (r.exists) {
      const c = r.company;
      statusEl.innerHTML = `Recognised: <strong>${c.name}</strong> ${renderRatingPill(c.rating)}`;
      statusEl.className = "match-status ok";
      pendingOnboard[side] = null;
    } else if (pendingOnboard[side] && pendingOnboard[side].name.trim().toLowerCase() === q.toLowerCase()) {
      statusEl.innerHTML = "Will onboard as a new company.";
      statusEl.className = "match-status warn";
    } else {
      statusEl.innerHTML = `Not on the platform. <a href="#" data-onboard="${side}">Add this company</a>`;
      statusEl.className = "match-status warn";
      statusEl.querySelector("a").addEventListener("click", (ev) => {
        ev.preventDefault();
        openOnboardModal(side, q);
      });
    }
  }

  input.addEventListener("input", async () => {
    const q = input.value.trim();
    pendingOnboard[side] = null;
    if (q.length < 1) { panel.classList.remove("show"); statusEl.textContent = ""; return; }
    const seq = ++activeReq;
    const data = await fetch(`/api/companies/search?q=${encodeURIComponent(q)}&role=${role}&limit=8`).then((r) => r.json());
    if (seq !== activeReq) return;
    panel.innerHTML = data.map((c) => `
      <div class="autocomplete-item" data-name="${c.name.replace(/"/g, '&quot;')}">
        ${c.name}<span class="meta">${c.industry ?? ""} · ${c.country ?? ""}</span>
      </div>`).join("");
    panel.classList.toggle("show", data.length > 0);
    panel.querySelectorAll(".autocomplete-item").forEach((item) => {
      item.addEventListener("mousedown", (ev) => {
        ev.preventDefault();
        input.value = item.dataset.name;
        panel.classList.remove("show");
        refreshStatus();
      });
    });
    refreshStatus();
  });
  input.addEventListener("blur", () => setTimeout(() => panel.classList.remove("show"), 150));
}

async function openOnboardModal(side, prefillName) {
  const meta = await ensureMeta();
  $("#onboard-country").innerHTML = meta.countries
    .map((c) => `<option value="${c.code}">${c.name} (${c.code})</option>`).join("");
  $("#onboard-industry").innerHTML = `<option value="">Industrial (default)</option>` +
    meta.industries.map((i) => `<option value="${i}">${i}</option>`).join("");
  $("#onboard-role").value = side === "seller" ? "SELLER" : "BUYER";
  $("#onboard-name").value = prefillName || "";
  $("#onboard-side").value = side;
  $("#onboard-revenue").value = "";
  $("#onboard-years").value = "";
  $("#onboard-parent").value = "";
  $("#onboard-title").textContent = `Onboard new ${side}`;
  $("#onboard-modal").hidden = false;
}
function closeOnboardModal() { $("#onboard-modal").hidden = true; }

function setupOnboardModal() {
  $("#onboard-cancel").addEventListener("click", closeOnboardModal);
  $("#onboard-back").addEventListener("click", closeOnboardModal);
  $("#onboard-modal").addEventListener("click", (ev) => {
    if (ev.target.id === "onboard-modal") closeOnboardModal();
  });
  $("#onboard-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const fd = new FormData(ev.target);
    const side = fd.get("side");
    const payload = {
      name: fd.get("name").trim(),
      role: fd.get("role"),
      country: fd.get("country"),
      industry: fd.get("industry") || "Industrial",
      annual_revenue_usd: parseFloat(fd.get("annual_revenue_usd")),
      years_operated: parseInt(fd.get("years_operated"), 10),
      parent_name: (fd.get("parent_name") || "").trim() || null,
    };
    const btn = $("#onboard-submit");
    btn.disabled = true;
    btn.textContent = "Running agents…";
    try {
      const resp = await fetch("/api/companies", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await resp.json();
      if (!resp.ok) {
        alert(`Onboarding failed: ${data.detail || data.message || "Unknown error"}`);
        return;
      }
      closeOnboardModal();
      populateInvoiceAfterOnboard(side, data);
    } finally {
      btn.disabled = false;
      btn.textContent = "Run Underwriter Agent";
    }
  });
}

function populateInvoiceAfterOnboard(side, data) {
  const c = data.company;
  const rp = c.risk_profile;
  const input = $(side === "seller" ? "#seller-input" : "#buyer-input");
  input.value = c.name;
  input.dispatchEvent(new Event("input", { bubbles: true }));

  const primary = (c.credit_limits || []).find((x) => x.product === "GLOBAL");
  const fact = (c.credit_limits || []).find((x) => x.product === "FACTORING");
  const rev = (c.credit_limits || []).find((x) => x.product === "REVERSE_FACTORING");
  const yrs = c.founded_year ? (new Date().getFullYear() - c.founded_year) : null;

  $("#new-company-title").innerHTML =
    `New ${side} onboarded — <strong>${c.name}</strong> ${rp ? renderRatingPill(rp.rating) : ""}`;
  $("#new-company-body").innerHTML = `
    <div class="summary-banner">
      Underwriter Agent assigned <strong>${rp ? rp.rating : "—"}</strong>
      with credit spread <strong>${rp ? fmtPct(rp.credit_spread) : "—"}</strong>.
      Submit the invoice to continue the flow through the rest of the graph.
    </div>
    <div class="kv">
      <div class="k">Role</div><div>${c.role}</div>
      <div class="k">Country</div><div>${c.country || "—"}</div>
      <div class="k">Industry</div><div>${c.industry || "—"}</div>
      <div class="k">Annual revenue</div><div>${fmtUsd(c.annual_revenue_usd)}</div>
      <div class="k">Years operated</div><div>${yrs === null ? "—" : yrs}</div>
      <div class="k">Rating</div><div>${rp ? renderRatingPill(rp.rating) : "—"}</div>
      <div class="k">PD (1y)</div><div>${rp ? fmtPct(rp.pd_1y) : "—"}</div>
      <div class="k">Credit spread</div><div>${rp ? fmtPct(rp.credit_spread) : "—"}</div>
      <div class="k">Global limit</div><div>${primary ? fmtUsd(primary.limit_usd) : "—"}</div>
      <div class="k">Factoring sub-limit</div><div>${fact ? fmtUsd(fact.limit_usd) : "—"}</div>
      <div class="k">Reverse-factoring sub-limit</div><div>${rev ? fmtUsd(rev.limit_usd) : "—"}</div>
      ${rp && rp.notes ? `<div class="k">Underwriter notes</div><div>${rp.notes}</div>` : ""}
    </div>
    <div class="section-title">Agent Events</div>
    <div class="event-feed">${(data.events || []).map(renderEvent).join("")}</div>`;
  $("#new-company-card").hidden = false;
  $("#result-card").hidden = true;
  $("#new-company-card").scrollIntoView({ behavior: "smooth", block: "start" });

  const otherInput = $(side === "seller" ? "#buyer-input" : "#seller-input");
  const amountInput = document.querySelector('#invoice-form input[name="amount"]');
  if (!otherInput.value) otherInput.focus();
  else if (amountInput && !amountInput.value) amountInput.focus();
}

function renderToolCall(tc) {
  return `
    <div class="tool-call">
      <div class="tc-head">
        <div><span class="trace-node">${tc.node}</span>
          &nbsp;→&nbsp;<span class="tc-tool">${tc.tool}()</span></div>
        <div class="muted">${fmtTs(tc.timestamp)}</div>
      </div>
      <div class="muted">args:</div>
      <pre>${JSON.stringify(tc.args, null, 2)}</pre>
      <div class="muted">result:</div>
      <pre>${JSON.stringify(tc.result, null, 2)}</pre>
    </div>`;
}
function renderTraceEntry(t) {
  return `
    <div class="trace-item">
      <div class="trace-head">
        <span><span class="trace-node">${t.node}</span> · ${fmtTs(t.timestamp)}</span>
      </div>
      <div>${t.message}</div>
      ${t.payload ? `<pre style="margin-top:6px;">${JSON.stringify(t.payload, null, 2)}</pre>` : ""}
    </div>`;
}

function setupForm() {
  setupAutocomplete("#seller-input", "#seller-suggestions", "SELLER");
  setupAutocomplete("#buyer-input", "#buyer-suggestions", "BUYER");

  $("#invoice-form").addEventListener("reset", () => {
    $("#new-company-card").hidden = true;
    $("#result-card").hidden = true;
    $("#seller-status").textContent = "";
    $("#buyer-status").textContent = "";
  });

  $$(".inner-tab").forEach((btn) => {
    btn.addEventListener("click", () => {
      $$(".inner-tab").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      $$(".inner-panel").forEach((p) => p.hidden = true);
      $(`#panel-${btn.dataset.inner}`).hidden = false;
    });
  });

  $("#invoice-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const fd = new FormData(ev.target);
    const payload = {
      seller_name: fd.get("seller_name"),
      buyer_name: fd.get("buyer_name"),
      product: fd.get("product"),
      amount: parseFloat(fd.get("amount")),
      currency: fd.get("currency"),
      tenor_days: parseInt(fd.get("tenor_days"), 10),
      grace_period_days: parseInt(fd.get("grace_period_days") || "0", 10),
    };
    const submitBtn = ev.target.querySelector("button[type='submit']");
    submitBtn.disabled = true;
    submitBtn.textContent = "Running graph…";
    try {
      const resp = await fetch("/api/invoices", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await resp.json();
      const card = $("#result-card");

      if (resp.status === 422) {
        // Determine which side is missing and open the modal.
        const msg = (data.message || "").toLowerCase();
        if (msg.includes("seller")) {
          openOnboardModal("seller", payload.seller_name);
        } else if (msg.includes("buyer")) {
          openOnboardModal("buyer", payload.buyer_name);
        } else {
          alert(`Invoice could not be created: ${data.message}`);
        }
        return;
      }

      card.hidden = false;
      if (!resp.ok) {
        $("#decision-summary").innerHTML = `<div class="summary-banner error">Error: ${data.detail || data.message || "Unknown error"}</div>`;
        $("#panel-trace").innerHTML = "";
        $("#panel-tool-calls").innerHTML = "";
        $("#panel-events").innerHTML = "";
        return;
      }

      const inv = data.invoice;
      const bannerClass = inv.status === "REJECTED" ? "summary-banner error" : "summary-banner";
      // On reject, show the full multi-line decision_reason; on approve, the
      // one-line summary is enough.
      const bannerHeader = `${inv.invoice_number} · ${inv.status}`;
      const bannerBody = inv.status === "REJECTED" && inv.decision_reason
        ? inv.decision_reason
        : (inv.decision_reason || data.summary || "");
      $("#decision-summary").innerHTML = `
        <div class="${bannerClass}"><strong>${bannerHeader}</strong>${bannerBody ? "\n" + bannerBody : ""}</div>
        <div class="kv">
          <div class="k">Invoice</div><div>${inv.invoice_number} <span class="badge ${inv.status}">${inv.status}</span></div>
          <div class="k">Seller → Buyer</div><div>${inv.seller.name} → ${inv.buyer.name}</div>
          <div class="k">Product</div><div>${inv.product}</div>
          <div class="k">Amount</div><div>${inv.amount.toLocaleString()} ${inv.currency} (${fmtUsd(inv.amount_usd)})</div>
          <div class="k">Tenor / Grace</div><div>${inv.tenor_days}d / ${inv.grace_period_days}d</div>
          <div class="k">Base rate</div><div>${fmtPct(inv.base_rate)}</div>
          <div class="k">Credit spread</div><div>${fmtPct(inv.credit_spread)}</div>
          <div class="k">All-in fee</div><div>${fmtUsd(inv.fee_usd)}</div>
          <div class="k">Funded amount</div><div>${fmtUsd(inv.funded_amount_usd)}</div>
        </div>`;
      $("#panel-trace").innerHTML = (data.trace || []).map(renderTraceEntry).join("")
        || `<div class="muted">No trace entries.</div>`;
      $("#panel-tool-calls").innerHTML = (data.tool_calls || []).map(renderToolCall).join("")
        || `<div class="muted">No tool calls recorded.</div>`;
      $("#panel-events").innerHTML = (data.events || []).map(renderEvent).join("")
        || `<div class="muted">No events persisted.</div>`;
      loadDashboard();
    } finally {
      submitBtn.disabled = false;
      submitBtn.textContent = "Run Agent Graph";
    }
  });
}

// ---------- bootstrap ----------
window.addEventListener("DOMContentLoaded", () => {
  setupTabs();
  loadMeta().then(loadDashboard);
  setupForm();
  setupOnboardModal();
  $("#refresh-events").addEventListener("click", loadEvents);
  $("#filter-agent").addEventListener("change", loadEvents);
  $("#filter-severity").addEventListener("change", loadEvents);
  $("#company-search").addEventListener("input", loadCompanies);
  $("#company-role").addEventListener("change", loadCompanies);
  $("#company-rating").addEventListener("change", loadCompanies);
  $("#company-sort").addEventListener("change", loadCompanies);
});
