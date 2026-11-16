// Mattermost Implement architecture wizard. Runs entirely client-side: nothing
// here is ever sent anywhere. The estate-plan schema this emits is documented in
// estate.example.yml and consumed by tools/estate_plan.py on each server.
(() => {
  "use strict";

  const DATA = JSON.parse(document.getElementById("wizard-data").textContent);
  const SPECS = DATA.components;

  const ROLE_LABEL = {
    core: "Core (PostgreSQL + Mattermost + Nginx)",
    app: "App server (Mattermost + Nginx)",
    database: "Database (PostgreSQL)",
    calls: "Calls (rtcd)",
    search: "Search (OpenSearch)",
    storage: "File storage (NFS)",
    monitoring: "Monitoring (Prometheus + Loki + Grafana)",
    metrics: "Metrics and logs (Prometheus + Loki)",
    dashboards: "Dashboards (Grafana)",
  };
  const NAME_PREFIX = { core: "core", app: "app", database: "db", calls: "calls",
                        search: "search", storage: "files", monitoring: "mon",
                        metrics: "metrics", dashboards: "graf" };

  let nodes = [];     // [{id, role, replica, primaryId, name, address}]
  let nextId = 1;
  let state = {};

  // ---------------------------------------------------------------- helpers

  function el(id) { return document.getElementById(id); }
  function escapeHtml(s) { return String(s).replace(/[&<>]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c])); }
  function qs(sel, root) { return (root || document).querySelector(sel); }
  function qsa(sel, root) { return Array.from((root || document).querySelectorAll(sel)); }
  function shortLabel(role) { return ROLE_LABEL[role].split(" (")[0]; }
  function setRadio(name, value) {
    const input = qs('input[name="' + name + '"][value="' + value + '"]');
    if (input) input.checked = true;
  }
  function roleHas(node, component) { return (DATA.profile_components[node.role] || []).includes(component); }

  function showStep(n) {
    for (let i = 1; i <= 4; i++) {
      el("step-" + i).style.display = (i === n) ? "" : "none";
      const chip = qs('.step[data-step="' + i + '"]');
      chip.classList.toggle("active", i === n);
      chip.classList.toggle("done", i < n);
    }
    el("step2-error").style.display = "none";
    el("step3-error").style.display = "none";
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  function pickBand(seats) {
    for (const band of DATA.bands) {
      if (band.max_seats === null || seats <= band.max_seats) return band;
    }
    return DATA.bands[DATA.bands.length - 1];
  }

  // Mattermost marks high availability as required from its 2,000-user tier. A band is a
  // range, so a 500-seat estate sits in the "up to 2,000" band without reaching that tier.
  function haRequired(band, seats) {
    return Boolean(band && band.ha_required) && seats >= DATA.ha_required_from_seats;
  }

  function readState() {
    return {
      name: el("f-name").value.trim() || "mattermost",
      siteUrl: el("f-site").value.trim(),
      tier: el("f-tier").value,
      seats: Math.max(1, parseInt(el("f-seats").value, 10) || 1),
      observability: qs('input[name="observability"]:checked').value,
      monitoringLayout: qs('input[name="grafana"]:checked').value,
      scrapeSources: el("f-scrape").value.split(",").map(s => s.trim()).filter(Boolean),
      lokiUrl: el("f-loki").value.trim(),
      callsMode: qs('input[name="calls"]:checked').value,
      callsServers: Math.max(1, parseInt(el("f-calls-servers").value, 10) || 1),
      search: qs('input[name="search"]:checked').value,
      ha: el("f-ha").checked,
      appServers: el("f-ha").checked ? Math.max(2, parseInt(el("f-app-servers").value, 10) || 2) : 1,
      dbReplicas: el("f-ha").checked ? Math.max(0, parseInt(el("f-db-replicas").value, 10) || 0) : 0,
      tlsMode: el("f-tls").value,
      fileStorage: el("f-storage").value,
    };
  }

  // ---------------------------------------------------------------- recommendation

  // What the band would pick, before anyone overrides it on the Availability group.
  function bandDefaults(band) {
    let app = 0, replicas = 0;
    for (const n of band.nodes) {
      if (n.role === "app") app += n.count;
      if (n.role === "database" && n.replica) replicas += n.count;
    }
    return { app: Math.max(1, app), replicas };
  }

  // The answers that change the shape of the estate. Anything else (a URL, a
  // name) can be edited without throwing away the nodes already set up.
  function shapeSignature(s) {
    return [s.seats, s.tier, s.callsMode, s.callsServers, s.observability,
            s.monitoringLayout, s.ha, s.appServers, s.dbReplicas, s.fileStorage, s.search].join("|");
  }

  function buildRecommendation(s) {
    const band = pickBand(s.seats);
    // The database always gets its own server, at every size.
    const roles = [{ role: "database" }];
    for (let i = 0; i < s.dbReplicas; i++) roles.push({ role: "database", replica: true });
    for (let i = 0; i < s.appServers; i++) roles.push({ role: "app" });
    // Enterprise search, high availability and rtcd are all Enterprise features.
    if (s.tier === "enterprise" && s.search === "opensearch") roles.push({ role: "search" });
    if (s.fileStorage === "nfs") roles.push({ role: "storage" });
    // Up to 50 people, Calls runs inside the app servers and needs no node of its
    // own. Past that Mattermost expects rtcd, so messaging doesn't pay for media.
    if (s.callsMode === "rtcd") {
      for (let i = 0; i < s.callsServers; i++) roles.push({ role: "calls" });
    }
    if (s.observability === "managed") {
      // Grafana is the only part of the stack people log in to, so by default it
      // sits apart from the metrics and logs it reads.
      if (s.monitoringLayout === "combined") {
        roles.push({ role: "monitoring" });
      } else {
        roles.push({ role: "metrics" });
        roles.push({ role: "dashboards" });
      }
    }
    return { band, roles };
  }

  function renameAndRenumber() {
    const counts = {};
    for (const n of nodes) {
      const prefix = NAME_PREFIX[n.role] || n.role;
      counts[prefix] = (counts[prefix] || 0) + 1;
      if (!n.nameTouched) n.name = prefix + String(counts[prefix]).padStart(2, "0");
    }
  }

  function recomputePrimaries() {
    const primaries = nodes.filter(n => n.role === "database" && !n.replica);
    for (const n of nodes) {
      if (n.role === "database" && n.replica) {
        if (!primaries.some(p => p.id === n.primaryId)) {
          n.primaryId = primaries.length ? primaries[0].id : null;
        }
      } else {
        n.primaryId = null;
      }
    }
  }

  function setNodesFromRoles(roles) {
    const addresses = new Map(nodes.map(n => [n.name, n.address]));
    nodes = roles.map(r => ({
      id: nextId++, role: r.role, replica: !!r.replica, primaryId: null,
      name: "", address: "", nameTouched: false,
    }));
    recomputePrimaries();
    renameAndRenumber();
    // Keep what was already typed for a node that survived the change.
    for (const n of nodes) {
      if (addresses.has(n.name)) n.address = addresses.get(n.name);
    }
  }

  // ---------------------------------------------------------------- what runs where

  // Everything installed on a node: the profile's own components, plus the
  // exporters and log collector the toolkit adds next to what they watch.
  function nodeComponents(n) {
    const out = (DATA.profile_components[n.role] || []).slice();
    const auto = DATA.auto_components;
    const scraped = state.s.observability === "managed" || state.s.scrapeSources.length > 0;
    const shipsLogs = state.s.observability === "managed" || state.s.lokiUrl.length > 0;
    if (scraped) {
      for (const c of out.slice()) {
        const extra = auto.alongside[c];
        if (extra && !out.includes(extra)) out.push(extra);
      }
      if (!out.includes("node_exporter")) out.push("node_exporter");
    }
    if (shipsLogs && !out.includes("otel_collector")) out.push("otel_collector");
    if (out.includes("prometheus") && (state.s.siteUrl || nodes.some(x => x.role === "calls"))) {
      for (const c of auto.monitoring_server) if (!out.includes(c)) out.push(c);
    }
    return out;
  }

  // ---------------------------------------------------------------- install order

  // Database (and anything with no upstream dependency) first, a replica once its
  // primary exists, then app servers once what they talk to is up, monitoring last
  // since it needs the whole estate. Matches README's own install-order guidance.
  function installRank(n) {
    if (n.role === "database" && n.replica) return 2;
    if (n.role === "app") return 3;
    if (n.role === "monitoring" || n.role === "metrics") return 4;
    if (n.role === "dashboards") return 5;   // needs the Prometheus it queries
    return 1;
  }

  function buildInstallOrder() {
    const waves = {};
    for (const n of nodes) {
      const r = installRank(n);
      (waves[r] = waves[r] || []).push(n);
    }
    return Object.keys(waves).map(Number).sort((a, b) => a - b).map(r => waves[r]);
  }

  function renderInstallOrder(targetId) {
    const list = el(targetId);
    list.innerHTML = "";
    for (const wave of buildInstallOrder()) {
      const li = document.createElement("li");
      li.textContent = wave.map(n => `${n.name} (${shortLabel(n.role)})`).join(", ");
      if (wave.length > 1) {
        const span = document.createElement("span");
        span.className = "sametime";
        span.textContent = "Any order between these.";
        li.appendChild(span);
      }
      list.appendChild(li);
    }
  }

  // ---------------------------------------------------------------- step 2: node table + diagram

  function renderStep2() {
    const band = state.band;
    el("rationale").textContent = band.label + " users, sized from " + band.reference + ", " +
      (nodes.length === 1 ? "one server" : nodes.length + " servers");
    const ha = haRequired(band, state.s.seats)
      ? "Mattermost marks high availability as required at this size. The Availability group on the "
        + "previous page is where you decide."
      : "";
    const text = [band.note, ha].filter(Boolean).join(" ");
    el("scale-note").style.display = text ? "" : "none";
    el("scale-note").textContent = text;
    renderEstateSummary();
    renderDiagram();
  }

  function roleOptions(selected) {
    return Object.keys(ROLE_LABEL).map(r =>
      `<option value="${r}" ${r === selected ? "selected" : ""}>${ROLE_LABEL[r]}</option>`).join("");
  }

  // The estate grouped by what each tier is for, rather than a row of dropdowns.
  // What a server is gets decided on the first page; this is where you see it.
  const TIERS = [
    { key: "app", roles: ["app", "core"], title: "Application" },
    { key: "database", roles: ["database"], title: "Database" },
    { key: "search", roles: ["search"], title: "Search" },
    { key: "calls", roles: ["calls"], title: "Calls" },
    { key: "storage", roles: ["storage"], title: "File storage" },
    { key: "monitoring", roles: ["monitoring", "metrics", "dashboards"], title: "Monitoring" },
  ];

  // What a tier actually installs, taken from the profiles rather than written out.
  function tierComponents(members) {
    const out = [];
    for (const n of members) {
      for (const component of DATA.profile_components[n.role] || []) {
        if (!out.includes(component)) out.push(component);
      }
    }
    return out.map(c => (SPECS[c] || {}).title || c).join(", ");
  }

  function describeNode(n) {
    if (n.role !== "database") return ROLE_LABEL[n.role].replace(/ \(.*\)$/, "");
    if (!n.replica) return "primary";
    const primary = nodes.find(p => p.id === n.primaryId);
    return primary ? "read replica of " + primary.name : "read replica";
  }

  function renderEstateSummary() {
    recomputePrimaries();
    renameAndRenumber();
    const host = el("estate-summary");
    host.innerHTML = "";
    for (const tier of TIERS) {
      const members = nodes.filter(n => tier.roles.includes(n.role));
      if (!members.length) continue;
      const block = document.createElement("div");
      block.className = "tier";
      const count = members.length === 1 ? "1 server" : members.length + " servers";
      block.innerHTML = `
        <div class="tier-head">
          <h4>${escapeHtml(tier.title)}</h4>
          <span class="shape">${count}</span>
        </div>
        <p class="hint" style="margin:4px 0 0;">${escapeHtml(tierComponents(members))}</p>
        <ul>${members.map(n =>
          `<li><strong>${escapeHtml(n.name)}</strong> <span class="role-note">${escapeHtml(describeNode(n))}</span>` +
          `<button class="link small remove-node" data-id="${n.id}">remove</button></li>`).join("")}</ul>`;
      host.appendChild(block);
    }
    if (!nodes.length) {
      host.innerHTML = '<p class="hint">No servers yet. Go back and answer the questions, or add one below.</p>';
    }
    qsa(".remove-node", host).forEach(btn => btn.addEventListener("click", (e) => {
      nodes = nodes.filter(x => x.id != e.target.dataset.id);
      renderEstateSummary();
      renderDiagram();
    }));
  }

  el("add-node").addEventListener("click", () => {
    const role = el("f-add-role").value;
    nodes.push({ id: nextId++, role, replica: false, primaryId: null,
                 name: "", address: "", nameTouched: false });
    renderEstateSummary();
    renderDiagram();
  });

  function buildMermaid() {
    const idFor = n => "n" + n.id;
    const lines = ["flowchart LR"];
    for (const n of nodes) {
      lines.push(`  ${idFor(n)}["${n.name}<br/>${shortLabel(n.role)}"]`);
    }
    const byRole = r => nodes.filter(n => n.role === r);
    const clients = byRole("app").concat(byRole("core"));
    const primaryDbs = nodes.filter(n => n.role === "database" && !n.replica);

    // Where the users come in. Call media is drawn thick because it goes straight
    // to whatever runs rtcd, never through the proxy, and that catches people out.
    if (clients.length) {
      lines.push('  users(["Users"])');
      for (const c of clients) lines.push(`  users -->|"HTTPS 443"| ${idFor(c)}`);
      const callsNodes = byRole("calls");
      if (callsNodes.length) {
        for (const x of callsNodes) lines.push(`  users ==>|"Calls media 8443 UDP and TCP"| ${idFor(x)}`);
      } else if (state.s.callsMode === "integrated") {
        for (const c of clients) lines.push(`  users ==>|"Calls media 8443 UDP and TCP"| ${idFor(c)}`);
      }
    }

    for (const c of clients) {
      for (const d of primaryDbs) lines.push(`  ${idFor(c)} -->|SQL| ${idFor(d)}`);
      for (const x of byRole("calls")) lines.push(`  ${idFor(c)} -->|Calls| ${idFor(x)}`);
      for (const x of byRole("search")) lines.push(`  ${idFor(c)} -->|Search| ${idFor(x)}`);
      for (const x of byRole("storage")) lines.push(`  ${idFor(c)} -->|Files| ${idFor(x)}`);
    }
    for (const d of nodes.filter(n => n.role === "database" && n.replica)) {
      const primary = nodes.find(n => n.id === d.primaryId);
      if (primary) lines.push(`  ${idFor(primary)} -.->|replicates to| ${idFor(d)}`);
    }
    const store = nodes.find(n => roleHas(n, "prometheus")) || nodes.find(n => roleHas(n, "loki"));
    if (store) {
      for (const n of nodes) if (n.id !== store.id) lines.push(`  ${idFor(n)} -.->|metrics/logs| ${idFor(store)}`);
      const grafana = nodes.find(n => roleHas(n, "grafana") && !roleHas(n, "prometheus"));
      if (grafana) lines.push(`  ${idFor(grafana)} -->|queries| ${idFor(store)}`);
    }
    return lines.join("\n");
  }

  // Editing a node redraws the diagram, and drawing is asynchronous, so a slow
  // render can land after a newer one. Each render carries a token and only the
  // newest is allowed to reach the page; the rest are dropped.
  let mermaidReady = false;
  let diagramToken = 0;
  function renderDiagram() {
    renderInstallOrder("install-order");
    const host = el("mermaid-out");
    const token = ++diagramToken;
    if (!nodes.length) {
      host.textContent = "Add a node above to see the diagram.";
      return;
    }
    const src = buildMermaid();
    if (!window.mermaid) { host.textContent = src; return; }
    if (!mermaidReady) {
      window.mermaid.initialize({ startOnLoad: false, theme: "neutral" });
      mermaidReady = true;
    }
    window.mermaid.render("estate-diagram-" + token, src)
      .then(result => { if (token === diagramToken) host.innerHTML = result.svg; })
      .catch(() => { if (token === diagramToken) host.textContent = src; });
  }

  // ---------------------------------------------------------------- step 3: addresses

  function renderStep3() {
    const host = el("address-list");
    host.innerHTML = "";
    for (const n of nodes) {
      const row = document.createElement("div");
      row.className = "address-row";
      row.innerHTML = `
        <div class="field" style="margin:0;">
          <label for="name-${n.id}">Name<span>${escapeHtml(describeRoleFully(n))}</span></label>
          <input type="text" id="name-${n.id}" class="name-in" data-id="${n.id}" value="${escapeHtml(n.name)}">
        </div>
        <div class="field" style="margin:0;">
          <label for="addr-${n.id}">Address or hostname</label>
          <input type="text" id="addr-${n.id}" class="addr-in" data-id="${n.id}"
                 value="${escapeHtml(n.address)}" placeholder="10.0.0.11" autocomplete="off">
        </div>`;
      host.appendChild(row);
    }
    qsa(".name-in", host).forEach(inp => inp.addEventListener("input", (e) => {
      const n = nodes.find(x => x.id == e.target.dataset.id);
      n.name = e.target.value.trim(); n.nameTouched = true;
    }));
    qsa(".addr-in", host).forEach(inp => inp.addEventListener("input", (e) => {
      const n = nodes.find(x => x.id == e.target.dataset.id);
      n.address = e.target.value.trim();
    }));
  }

  function describeRoleFully(n) {
    const base = shortLabel(n.role);
    if (n.role !== "database") return base;
    if (!n.replica) return base + ", primary";
    const primary = nodes.find(p => p.id === n.primaryId);
    return base + ", read replica" + (primary ? " of " + primary.name : "");
  }

  // ---------------------------------------------------------------- checking what was typed

  const HOSTNAME = /^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$/i;
  const NODE_NAME = /^[a-z0-9][a-z0-9._-]*$/i;

  // Mark the field itself rather than only listing the problem at the bottom.
  function markField(fieldId, message) {
    const field = el(fieldId);
    if (!field) return;
    field.classList.toggle("invalid", Boolean(message));
    let note = qs(".inline-error", field);
    if (!message) { if (note) note.remove(); return; }
    if (!note) {
      note = document.createElement("span");
      note.className = "inline-error";
      field.appendChild(note);
    }
    note.textContent = message;
  }

  // What has to be filled in before a recommendation means anything. Everything
  // here marks its own field, so the summary underneath only has to point at them.
  function step1Problems() {
    const problems = [];
    const s = readState();
    const add = (fieldId, message) => { markField(fieldId, message); if (message) problems.push(message); };

    // readState falls back to a default name, so check what was actually typed.
    add("field-name", el("f-name").value.trim() ? "" : "Give the deployment a name.");

    if (!s.siteUrl) {
      add("field-site", "The address people will use. Several settings are built from it.");
    } else if (!/^https?:\/\/[^\s/]+/.test(s.siteUrl)) {
      add("field-site", "Start with https:// or http://");
    } else {
      add("field-site", "");
    }

    add("field-seats", parseInt(el("f-seats").value, 10) > 0 ? "" : "A number above zero.");

    // More than one app server and local disk means each one holds different files.
    add("field-storage", s.appServers > 1 && s.fileStorage === "local"
      ? "More than one app server can't keep files on local disk. Use NFS or S3." : "");

    add("field-scrape", s.observability === "external" && !s.scrapeSources.length
      ? "At least one address, or let this toolkit build monitoring." : "");
    return problems;
  }

  function looksLikeAddress(value) {
    if (/^[0-9.]+$/.test(value)) {
      const parts = value.split(".");
      return parts.length === 4 && parts.every(p => p !== "" && Number(p) <= 255 && !/^0\d/.test(p));
    }
    if (value.includes(":")) return /^[0-9a-f:]+$/i.test(value);   // IPv6
    return HOSTNAME.test(value);
  }

  // Two nodes on one address means one box, and install.sh can't then tell which
  // node it is meant to be, so it's worth catching here rather than there.
  function nodeProblems() {
    const problems = [];
    const seenNames = new Map();
    const seenAddresses = new Map();

    for (const n of nodes) {
      if (!n.name) { problems.push("Every node needs a name."); continue; }
      if (!NODE_NAME.test(n.name)) {
        problems.push(`"${n.name}" isn't a usable name: letters, digits, dots, dashes and underscores only.`);
      }
      const key = n.name.toLowerCase();
      seenNames.set(key, (seenNames.get(key) || 0) + 1);
    }
    for (const [name, count] of seenNames) {
      if (count > 1) problems.push(`${count} nodes are called ${name}. Names have to be different.`);
    }

    for (const n of nodes) {
      if (!n.address) { problems.push(`${n.name || "A node"} needs an address.`); continue; }
      if (!looksLikeAddress(n.address)) {
        problems.push(`"${n.address}" on ${n.name} isn't an address or a hostname.`);
        continue;
      }
      seenAddresses.set(n.address, (seenAddresses.get(n.address) || []).concat(n.name));
    }
    for (const [address, names] of seenAddresses) {
      if (names.length > 1) {
        problems.push(`${andList(names)} share the address ${address}. Each node is a separate server, ` +
                      `so give each one its own, or put the roles on one node instead.`);
      }
    }
    return problems;
  }

  const NUMBERS = ["", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"];
  function countWord(n) { return NUMBERS[n] || String(n); }

  function andList(items) {
    if (items.length < 2) return items.join("");
    return items.slice(0, -1).join(", ") + " and " + items[items.length - 1];
  }

  // Put a red edge on the rows the problems above are about.
  function markBadRows() {
    const counts = {};
    for (const n of nodes) counts[n.address] = (counts[n.address] || 0) + 1;
    for (const input of qsa(".addr-in")) {
      const node = nodes.find(n => n.id == input.dataset.id);
      const bad = !node.address || !looksLikeAddress(node.address) || counts[node.address] > 1;
      input.classList.toggle("invalid", bad);
    }
    for (const input of qsa(".name-in")) {
      const node = nodes.find(n => n.id == input.dataset.id);
      const twin = nodes.filter(n => n.name.toLowerCase() === node.name.toLowerCase()).length > 1;
      input.classList.toggle("invalid", !node.name || !NODE_NAME.test(node.name) || twin);
    }
  }

  // ---------------------------------------------------------------- firewall table

  function appServerCount() {
    return nodes.filter(n => nodeComponents(n).includes("mattermost")).length;
  }

  // Who connects to a listening port. An empty list means nothing outside this
  // server needs it, so there's no rule to ask the network team for.
  function resolveSources(from, node) {
    switch (from) {
      case "internet":
        return ["Internet / users"];
      case "admins":
        return ["Your Mattermost admins"];
      case "app":
        return nodes.filter(n => n.id !== node.id && nodeComponents(n).includes("mattermost")).map(n => n.name);
      // Gossip goes both ways between every app server, so one row naming all of
      // them beats one row per pair. Including this node keeps the source list
      // identical on each, which is what merges them into that single row.
      case "app_peers": {
        const apps = nodes.filter(n => nodeComponents(n).includes("mattermost")).map(n => n.name);
        return apps.length > 1 ? apps : [];
      }
      case "monitoring": {
        const scrapers = nodes.filter(n => n.id !== node.id && roleHas(n, "prometheus")).map(n => n.name);
        return state.s.observability === "external" ? state.s.scrapeSources.slice() : scrapers;
      }
      case "db_replica":
        return nodes.filter(n => n.role === "database" && n.replica && n.primaryId === node.id).map(n => n.name);
      case "estate":
        return nodes.filter(n => n.id !== node.id).map(n => n.name);
      default:
        return [];
    }
  }

  function buildFirewallRows() {
    const groups = new Map();
    const add = (froms, to, port, proto, purpose, rank) => {
      if (!froms.length) return;
      const key = [to, port, proto, purpose].join("|");
      if (!groups.has(key)) groups.set(key, { froms: [], tos: [to], port, proto, purpose, rank });
      const g = groups.get(key);
      for (const f of froms) if (!g.froms.includes(f)) g.froms.push(f);
    };

    for (const node of nodes) {
      for (const comp of nodeComponents(node)) {
        for (const listen of (SPECS[comp] || {}).listens || []) {
          if (listen.when === "has_replica" &&
              !nodes.some(n => n.role === "database" && n.replica && n.primaryId === node.id)) continue;
          if (listen.when === "calls_integrated" && state.s.callsMode !== "integrated") continue;
          // Mattermost's metrics endpoint needs an Enterprise licence, so on any
          // other licence there is nothing listening on 8067 to ask for.
          if (listen.when === "mm_metrics" && state.s.tier !== "enterprise") continue;
          // Gossip only matters once there is a second app server to gossip with.
          if (listen.when === "clustering" && appServerCount() < 2) continue;
          const rank = listen.from === "internet" ? 0 : (listen.from === "monitoring" || listen.from === "estate" ? 2 : 1);
          add(resolveSources(listen.from, node), node.name, listen.port, listen.proto, listen.purpose, rank);
        }
      }
    }

    // Logs leave each server for Loki, which may be outside the estate entirely.
    if (state.s.observability === "external" && state.s.lokiUrl) {
      const host = state.s.lokiUrl.replace(/^https?:\/\//, "").replace(/\/.*$/, "");
      add(nodes.map(n => n.name), host, host.includes(":") ? host.split(":")[1] : 3100, "tcp",
          "Logs shipped to your own Loki", 2);
    }

    // One rule per source and port: everything Prometheus scrapes on 9100 is one
    // line for the network team, not one line per server.
    const merged = new Map();
    for (const g of groups.values()) {
      const key = [g.froms.join(", "), g.port, g.proto, g.purpose].join("|");
      if (!merged.has(key)) merged.set(key, { ...g, tos: [] });
      const m = merged.get(key);
      for (const t of g.tos) if (!m.tos.includes(t)) m.tos.push(t);
    }

    return Array.from(merged.values())
      .sort((a, b) => a.rank - b.rank)
      .map(g => ({ from: g.froms.join(", "), to: g.tos.join(", "), port: String(g.port), proto: g.proto, purpose: g.purpose }));
  }

  // ---------------------------------------------------------------- outbound

  // Which nodes have to make a given outbound connection. A null component means
  // every server in the estate, because every server installs something.
  function outboundSources(component) {
    if (component === null) return nodes.map(n => n.name);
    if (component === "calls") {
      const rtcd = nodes.filter(n => n.role === "calls");
      if (rtcd.length) return rtcd.map(n => n.name);
      return state.s.callsMode === "integrated"
        ? nodes.filter(n => nodeComponents(n).includes("mattermost")).map(n => n.name) : [];
    }
    return nodes.filter(n => nodeComponents(n).includes(component)).map(n => n.name);
  }

  function buildOutboundRows() {
    const out = [];
    const add = (entry, when) => {
      if (entry.when === "s3" && state.s.fileStorage !== "amazons3") return;
      const froms = outboundSources(entry.component);
      if (!froms.length) return;
      out.push({ from: froms.join(", "), to: entry.to, port: String(entry.port),
                 proto: entry.proto, purpose: entry.purpose, when: entry.when_label || when });
    };
    const spec = DATA.outbound || { runtime: [], install: [] };
    for (const entry of spec.runtime || []) add(entry, "Always");
    for (const entry of spec.install || []) add(entry, "Install and upgrade only");
    return out;
  }

  function renderOutboundTable() {
    const rows = buildOutboundRows();
    const tbody = qs("#outbound-table tbody");
    tbody.innerHTML = "";
    for (const r of rows) {
      const tr = document.createElement("tr");
      for (const cell of [r.from, r.to, r.port, r.proto, r.purpose, r.when]) {
        const td = document.createElement("td");
        td.textContent = cell;
        tr.appendChild(td);
      }
      tbody.appendChild(tr);
    }
    el("outbound-note").innerHTML = (DATA.outbound || {}).offline_note || "";
    return rows;
  }

  function renderFirewallTable() {
    const rows = buildFirewallRows();
    const tbody = qs("#firewall-table tbody");
    tbody.innerHTML = "";
    for (const r of rows) {
      const tr = document.createElement("tr");
      for (const cell of [r.from, r.to, r.port, r.proto, r.purpose]) {
        const td = document.createElement("td");
        td.textContent = cell;
        tr.appendChild(td);
      }
      tbody.appendChild(tr);
    }
    const note = el("calls-note");
    const where = nodes.some(n => n.role === "calls") ? "Calls server"
      : (state.s.callsMode === "integrated" ? "app server" : "");
    if (where) {
      note.style.display = "";
      note.textContent = "Calls media does not go through the proxy. Clients reach the " + where +
        " directly on 8443, UDP and TCP, so behind NAT or a load balancer that port has to map " +
        "through unchanged and the ICE host override has to name the address clients use. Without " +
        "it, calls connect and carry no audio. If clients can't reach 8443 at all, Calls needs a " +
        "TURN server, which this toolkit doesn't install.";
    } else {
      note.style.display = "none";
    }
    return rows;
  }

  // ---------------------------------------------------------------- infrastructure table

  // Sizing comes from the seat band, not from the components. A component's
  // floor in the catalogue says what it takes to start, which is the same at
  // 200 seats and 20,000, and is no use to anyone buying servers.
  function buildInfraRows() {
    const sizing = (state.band && state.band.sizing) || {};
    const notes = DATA.sizing_notes || {};
    return nodes.map(n => {
      const spec = sizing[n.role] || {};
      const components = nodeComponents(n);
      const named = (DATA.profile_components[n.role] || []).map(c => (SPECS[c] || {}).title || c);
      const extras = components.length - named.length;
      return {
        name: n.name,
        runs: named.join(", ") + (extras > 0 ? `, plus ${extras} exporter${extras === 1 ? "" : "s"}` : ""),
        cpus: spec.vcpu ? String(spec.vcpu) : "",
        ram: spec.ram_gb ? spec.ram_gb + " GB" : "",
        disk: spec.disk_gb ? spec.disk_gb + " GB" : "your own data",
        note: notes[n.role] || "",
      };
    });
  }

  function renderInfraTable() {
    const rows = buildInfraRows();
    const tbody = qs("#infra-table tbody");
    tbody.innerHTML = "";
    for (const r of rows) {
      const tr = document.createElement("tr");
      for (const cell of [r.name, r.runs, r.cpus, r.ram, r.disk, r.note]) {
        const td = document.createElement("td");
        td.textContent = cell;
        tr.appendChild(td);
      }
      tbody.appendChild(tr);
    }
    el("sizing-source").textContent = DATA.sizing_source || "";
    el("storage-formula").textContent = DATA.storage_formula || "";
    return rows;
  }

  // ---------------------------------------------------------------- design decisions

  function buildDecisions() {
    const rows = [];
    const count = role => nodes.filter(n => n.role === role).length;
    const apps = count("app") + count("core");
    const replicas = nodes.filter(n => n.role === "database" && n.replica).length;
    const primaries = nodes.filter(n => n.role === "database" && !n.replica).length + count("core");

    // A core node is the database and the app on one machine. Two separate rows
    // would each tell half the truth and never say they share a box.
    if (count("core")) {
      rows.push(["Database and app on one server",
                 "Restarting or restoring either takes both down, and they can't be sized apart. "
                 + "Split them before this carries real traffic."]);
    } else {
      rows.push(apps > 1
        ? [apps + " app servers",
           "You supply the load balancer and turn on cluster mode. Neither is installed here. "
           + "Open TCP and UDP 8074 between them for cluster gossip, keep every node on the same "
           + "Mattermost version and the same config.json, and apply the licence to each."]
        : ["One app server", "Restarts and upgrades are outages."]);
    }

    if (count("core")) {
      // Already covered by the row above.
    } else if (primaries && replicas) {
      rows.push([replicas === 1 ? "Database with a read replica" : "Database with " + replicas + " read replicas",
                 "The app servers are pointed at " + (replicas === 1 ? "it" : "them")
                 + " as DataSourceReplicas, so reads go there. Failover is manual."]);
    } else if (primaries) {
      rows.push(["One database", "Single point of failure, and every read lands on it."]);
    }

    const storage = state.s.fileStorage;
    if (apps > 1) {
      rows.push(storage === "local"
        ? ["Uploaded files on local disk", "Broken with more than one app server: each holds its own. Move to NFS or S3."]
        : ["Uploaded files on " + (storage === "nfs" ? "shared NFS" : "S3"), "Every app server sees the same files."]);
    }

    if (count("search")) {
      const want = (state.band && state.band.search_nodes) || 0;
      rows.push(["Search on OpenSearch",
                 "Search leaves the database. One node, so search is down if it is."
                 + (want > 1 ? " Mattermost's reference at this size uses " + want + "." : "")]);
    } else {
      rows.push(["Search on the database",
                 "Database search starts to slow down at about 2 million posts, and Mattermost wants you "
                 + "on OpenSearch before 3 million. That is a post count, not a seat count."]);
    }

    if (count("calls")) {
      rows.push([count("calls") > 1 ? count("calls") + " Calls servers" : "One Calls server",
                 "Media stays off the app servers. " + (count("calls") > 1
                   ? "Mattermost points at one rtcd URL, so these need a load balancer."
                   : "Calls stop if it does.")]);
    } else if (state.s.callsMode === "integrated") {
      rows.push(["Calls inside the app servers",
                 "Calls and messaging share the same CPU. Mattermost expects rtcd past about 50 people."]);
    }

    if (count("monitoring") || count("metrics")) {
      rows.push([count("dashboards") ? "Grafana on its own server" : "Monitoring on one server",
                 count("dashboards")
                   ? "Dashboards stay up when the metrics server is busy."
                   : "Losing it loses dashboards, alerting and log history at once."]);
    } else {
      rows.push(["Monitoring handled elsewhere", "Alerting and retention are yours."]);
    }

    if (state.s.tlsMode === "later") {
      rows.push(["TLS not configured yet", "Plain HTTP until a certificate is in place."]);
    }
    return rows;
  }

  function renderDecisions() {
    const rows = buildDecisions();
    const tbody = qs("#decisions-table tbody");
    tbody.innerHTML = "";
    for (const r of rows) {
      const tr = document.createElement("tr");
      for (const cell of r) {
        const td = document.createElement("td");
        td.textContent = cell;
        tr.appendChild(td);
      }
      tbody.appendChild(tr);
    }
    return rows;
  }

  function toMarkdown(headers, rows) {
    const lines = ["| " + headers.join(" | ") + " |", "| " + headers.map(() => "---").join(" | ") + " |"];
    for (const r of rows) lines.push("| " + r.join(" | ") + " |");
    return lines.join("\n");
  }

  // ---------------------------------------------------------------- step 4: plan + encoding

  function esc(v) { return JSON.stringify(String(v)); }

  function buildPlanObject() {
    return {
      name: state.s.name,
      seats: state.s.seats,
      licence: state.s.tier,
      // Professional runs the Enterprise binary; only Team Edition is different.
      mattermost_edition: state.s.tier === "team" ? "team" : "enterprise",
      mattermost_site_url: state.s.siteUrl,
      tls_mode: state.s.tlsMode,
      file_storage: state.s.fileStorage,
      calls: state.s.callsMode,
      observability: state.s.observability,
      monitoring_scrape_sources: state.s.scrapeSources,
      logging_loki_url: state.s.lokiUrl,
      nodes: nodes.map(n => {
        const primary = n.replica ? nodes.find(p => p.id === n.primaryId) : null;
        return { name: n.name, address: n.address, role: n.role, primary: primary ? primary.name : null };
      }),
    };
  }

  function emitYaml(plan) {
    const order = buildInstallOrder().map(wave => wave.map(n => n.name).join("/")).join(", then ");
    const lines = [
      "# Suggested install order: " + order,
      "estate_version: 1",
      "name: " + esc(plan.name),
      "seats: " + plan.seats,
      "licence: " + plan.licence,
      "mattermost_edition: " + plan.mattermost_edition,
      "mattermost_site_url: " + esc(plan.mattermost_site_url),
      "tls_mode: " + plan.tls_mode,
      "file_storage: " + plan.file_storage,
      "calls: " + plan.calls,
      "observability: " + plan.observability,
    ];
    if (plan.observability === "external") {
      lines.push("monitoring_scrape_sources: [" + plan.monitoring_scrape_sources.map(esc).join(", ") + "]");
      lines.push("logging_loki_url: " + esc(plan.logging_loki_url || ""));
    }
    lines.push("nodes:");
    for (const n of plan.nodes) {
      lines.push("  - name: " + esc(n.name));
      lines.push("    address: " + esc(n.address));
      lines.push("    profiles: [" + esc(n.role) + "]");
      if (n.primary) lines.push("    primary: " + esc(n.primary));
    }
    return lines.join("\n") + "\n";
  }

  function encodePlan(yamlText) {
    const bytes = new TextEncoder().encode(yamlText);
    const gz = window.pako.gzip(bytes);
    let binary = "";
    for (const b of gz) binary += String.fromCharCode(b);
    return "MME1:" + btoa(binary);
  }

  function buildFollowups(plan) {
    const out = [];
    const hasRole = r => plan.nodes.some(n => n.role === r);
    if (hasRole("app")) {
      if (hasRole("database")) out.push("Copy the database password from the database server's secrets.yml into mattermost_db_password on each app server, or use an estate secrets file (see the README).");
      if (hasRole("search")) {
        out.push("Copy the OpenSearch 'mattermost' user's password from the search server's secrets.yml into mattermost_opensearch_password on each app server.");
        out.push("Copy /etc/opensearch/certs/root-ca.pem from the search server to each app server, then point mattermost_opensearch_ca_file at it.");
      }
      if (plan.tls_mode === "provided") out.push("Put the certificate and key on each app server, then run install.sh --edit to add their paths.");
      if (plan.file_storage === "nfs" && hasRole("storage")) out.push("Mount the storage server's NFS share before installing Mattermost on each app server.");
      if (plan.file_storage === "amazons3") out.push("Fill in the S3 bucket, region and keys on each app server. They're not part of the estate plan.");
    }
    if (plan.nodes.some(n => n.primary)) out.push("Copy the db_password and replication_password from the primary's secrets.yml into the replica's answers.");
    if (plan.nodes.filter(n => n.role === "app" || n.role === "core").length > 1) {
      out.push("Put a load balancer in front of the app servers and health-check /api/v4/system/ping on each.");
      out.push("Turn on cluster mode yourself (ClusterSettings.Enable, ClusterName, GossipPort 8074) and open TCP and UDP 8074 between the app servers. This toolkit doesn't configure clustering.");
      out.push("Apply the Enterprise licence on every app server and keep them on the same Mattermost version and the same config.json.");
    }
    if (hasRole("calls")) out.push("If clients reach the Calls server through NAT or a different address, fill in rtcd_ice_host_override on that server.");
    out.push("None of that is in the estate plan on purpose. Secrets and certificate files never leave your servers.");
    return out;
  }

  function renderStep4() {
    const plan = buildPlanObject();
    const yamlText = emitYaml(plan);
    el("yaml-out").value = yamlText;
    el("blob-out").textContent = encodePlan(yamlText);
    renderInstallOrder("install-order-4");
    const decisions = renderDecisions();
    const firewall = renderFirewallTable();
    const outbound = renderOutboundTable();
    el("count-decisions").textContent = decisions.length + (decisions.length === 1 ? " decision" : " decisions");
    el("count-firewall").textContent = (firewall.length + outbound.length) + " rules";
    el("count-infra").textContent = nodes.length + " servers";
    renderInfraTable();
    const ul = el("followups");
    ul.innerHTML = "";
    const followups = buildFollowups(plan);
    for (const f of followups) {
      const li = document.createElement("li");
      li.textContent = f;
      ul.appendChild(li);
    }
    el("count-followups").textContent = followups.length + (followups.length === 1 ? " item" : " items");
  }

  function copyFrom(text, btn) {
    const done = () => { const old = btn.textContent; btn.textContent = "Copied!"; setTimeout(() => { btn.textContent = old; }, 1200); };
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(done).catch(() => fallbackCopy(text, done));
    } else {
      fallbackCopy(text, done);
    }
  }
  // Losing the plan means doing the whole thing again, so it can be saved as well
  // as copied. install.sh --estate takes either form.
  function planFilename(extension) {
    const name = (state.s.name || "estate").replace(/[^a-z0-9._-]/gi, "-");
    return name + "-estate." + extension;
  }

  function downloadFile(filename, text) {
    const url = URL.createObjectURL(new Blob([text], { type: "text/plain;charset=utf-8" }));
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  function fallbackCopy(text, done) {
    const ta = document.createElement("textarea");
    ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
    document.body.appendChild(ta); ta.focus(); ta.select();
    try { document.execCommand("copy"); done(); } catch (e) { /* best effort */ }
    document.body.removeChild(ta);
  }

  // ---------------------------------------------------------------- loading a plan back in

  function decodePlanText(text) {
    text = text.trim();
    if (text.startsWith("MME1:")) {
      const binary = atob(text.slice(5));
      const bytes = Uint8Array.from(binary, c => c.charCodeAt(0));
      text = new TextDecoder().decode(window.pako.ungzip(bytes));
    }
    return window.jsyaml.load(text);
  }

  function applyPlan(plan) {
    if (!plan || typeof plan !== "object") throw new Error("That isn't an estate plan.");
    if (plan.estate_version !== 1) throw new Error("This plan says estate_version " + plan.estate_version + ", and this wizard writes version 1.");
    if (!Array.isArray(plan.nodes) || !plan.nodes.length) throw new Error("That plan has no nodes in it.");

    el("f-name").value = plan.name || "mattermost";
    el("f-site").value = plan.mattermost_site_url || "";
    el("f-tier").value = ["team", "professional", "enterprise"].includes(plan.licence)
      ? plan.licence : (plan.mattermost_edition === "team" ? "team" : "enterprise");
    el("f-tls").value = plan.tls_mode === "provided" ? "provided" : "later";
    el("f-storage").value = ["local", "nfs", "amazons3"].includes(plan.file_storage) ? plan.file_storage : "local";
    if (plan.seats) el("f-seats").value = plan.seats;
    const obs = plan.observability === "external" ? "external" : "managed";
    qs('input[name="observability"][value="' + obs + '"]').checked = true;
    el("observability-external").style.display = obs === "external" ? "" : "none";
    el("monitoring-layout-row").style.display = obs === "external" ? "none" : "";
    setRadio("grafana", plan.nodes.some(n => (n.profiles || []).includes("monitoring"))
      ? "combined" : "split");
    el("f-scrape").value = (plan.monitoring_scrape_sources || []).join(", ");
    el("f-loki").value = plan.logging_loki_url || "";
    const appCount = plan.nodes.filter(n => (n.profiles || []).includes("app")).length;
    setRadio("search", plan.nodes.some(n => (n.profiles || []).includes("search"))
      ? "opensearch" : "database");
    el("f-ha").checked = appCount > 1 || plan.nodes.some(n => n.primary);
    el("f-app-servers").value = Math.max(2, appCount);
    el("f-db-replicas").value = plan.nodes.filter(n => n.primary).length;
    const callsNodes = plan.nodes.filter(n => (n.profiles || []).includes("calls"));
    setRadio("calls", callsNodes.length ? "rtcd"
      : (["integrated", "none"].includes(plan.calls) ? plan.calls : "none"));
    el("f-calls-servers").value = Math.max(1, callsNodes.length);
    updateCallsNote();

    nodes = plan.nodes.map(n => ({
      id: nextId++,
      role: (n.profiles || ["app"])[0],
      replica: Boolean(n.primary),
      primaryId: null,
      name: String(n.name || ""),
      address: String(n.address || ""),
      nameTouched: true,
    }));
    plan.nodes.forEach((src, i) => {
      if (!src.primary) return;
      const primary = nodes.find(x => x.name === src.primary);
      if (primary) nodes[i].primaryId = primary.id;
    });

    const unknown = nodes.filter(n => !ROLE_LABEL[n.role]).map(n => n.name);
    if (unknown.length) throw new Error("This wizard doesn't know the profile on " + unknown.join(", ") + ".");

    updateHaNote();
    updateSearchNote();
    updateMonitoringNote();
    state.s = readState();
    state.band = pickBand(state.s.seats);
    state.signature = shapeSignature(state.s);
    showStep(2);
    renderStep2();
  }

  // ---------------------------------------------------------------- wiring

  function showError(id, message) {
    const box = el(id);
    if (message) { box.textContent = message; box.style.display = ""; }
    else { box.style.display = "none"; }
  }

  el("do-import").addEventListener("click", () => {
    const text = el("f-import").value;
    if (!text.trim()) { showError("import-error", "Paste a plan first."); return; }
    try {
      applyPlan(decodePlanText(text));
      showError("import-error", "");
    } catch (err) {
      showError("import-error", err.message + " Paste the encoded line exactly as the wizard gave it, or the plain YAML.");
    }
  });

  // Mattermost's own guidance: integrated Calls up to 50 people, rtcd past that,
  // and rtcd is Enterprise only.
  function updateCallsNote() {
    const tier = el("f-tier").value;
    const enterprise = tier === "enterprise";
    const rtcd = el("f-calls-rtcd");
    rtcd.disabled = !enterprise;
    if (!enterprise && rtcd.checked) setRadio("calls", "integrated");

    // The 50 is Mattermost's integrated-versus-rtcd threshold, and it only means
    // anything on a licence that has group calls at all. On Team Edition the
    // labels describe where Calls runs and nothing more.
    const groupCalls = tier !== "team";
    el("calls-integrated-label").textContent = groupCalls
      ? "Up to 50 people in calls, built into the app servers"
      : "Built into the app servers";
    el("calls-rtcd-label").textContent = groupCalls
      ? "More than 50, on dedicated Calls servers"
      : "On dedicated Calls servers";

    const mode = qs('input[name="calls"]:checked').value;
    const note = el("calls-mode-note");
    el("field-calls-servers").style.display = mode === "rtcd" ? "" : "none";
    note.style.display = mode === "none" ? "none" : "";
    if (mode === "integrated") {
      const media = " UDP and TCP 8443 have to reach the app servers directly, not through the proxy.";
      if (tier === "team") {
        // Team Edition calls are 1-to-1 and time-limited, so "up to 50 people"
        // is about the deployment model, not what this licence can actually do.
        note.textContent = "Team Edition is 1-to-1 calls and screen sharing only, capped at 40 minutes. "
          + "Group calls need Professional, and dedicated Calls servers need Enterprise." + media;
      } else if (tier === "professional") {
        note.textContent = "Professional gives group calls and screen sharing with no time limit, inside "
          + "each app server. Dedicated Calls servers need Enterprise." + media;
      } else {
        note.textContent = "Group calls and screen sharing with no time limit, plus recording, transcription "
          + "and live captions, inside each app server." + media;
      }
    } else if (mode === "rtcd") {
      note.textContent = "Mattermost points at one rtcd URL, so more than one needs a load balancer in front.";
    }
  }
  qsa('input[name="calls"]').forEach(r => r.addEventListener("change", updateCallsNote));
  el("f-tier").addEventListener("change", updateCallsNote);

  // The seat band proposes a shape; the Availability group is where it's decided.
  function applyBandDefaults() {
    const defaults = bandDefaults(pickBand(Math.max(1, parseInt(el("f-seats").value, 10) || 1)));
    el("f-app-servers").value = Math.max(2, defaults.app);
    el("f-db-replicas").value = Math.max(1, defaults.replicas);
    updateHaNote();
  }

  // Clustering is an Enterprise feature, so the option is only open on Enterprise.
  function updateHaNote() {
    const enterprise = el("f-tier").value === "enterprise";
    const seats = Math.max(1, parseInt(el("f-seats").value, 10) || 1);
    const band = pickBand(seats);
    const required = haRequired(band, seats);
    const box = el("f-ha");
    box.disabled = !enterprise;
    if (!enterprise) box.checked = false;
    el("ha-options").style.display = box.checked ? "" : "none";

    // Mattermost marks high availability as required from its 2,000-user tier
    // upwards. We say so and leave the choice to you.
    const badge = el("ha-badge");
    badge.textContent = required ? "Required by Mattermost at this size" : "Optional at this size";
    badge.classList.remove("recommended");
    badge.classList.toggle("optional", !required);
    badge.classList.toggle("required", Boolean(required));

    // More than one app server cannot keep uploaded files on its own disk.
    const storage = el("f-storage");
    storage.querySelector('option[value="local"]').disabled = box.checked;
    if (box.checked && storage.value === "local") storage.value = "nfs";

    const note = el("ha-note");
    if (!enterprise) {
      note.textContent = "Clustering needs Enterprise.";
      return;
    }
    if (!box.checked) {
      note.textContent = "";
      return;
    }
    const replicas = Math.max(0, parseInt(el("f-db-replicas").value, 10) || 0);
    note.textContent = "You supply the load balancer and turn on cluster mode; neither is installed here. "
      + "Cluster gossip needs TCP and UDP 8074 open between the app servers, and it is in the firewall table. "
      + (replicas > 0 ? "Replica failover is manual." : "No replica, so the database is still a single point of failure.");
  }

  // Mattermost measures this in posts, not seats: database search starts to
  // degrade at about 2 million posts, and they want you moved over before 3
  // million. A small estate that has been running for years can be past it.
  function updateSearchNote() {
    const enterprise = el("f-tier").value === "enterprise";
    const opensearch = el("f-search-opensearch");
    opensearch.disabled = !enterprise;
    if (!enterprise && opensearch.checked) setRadio("search", "database");
    el("search-note").textContent = !enterprise
      ? "OpenSearch needs Enterprise. Recommended from 2 million posts."
      : "Recommended from 2 million posts.";
  }
  qsa('input[name="search"]').forEach(r => r.addEventListener("change", updateSearchNote));
  el("f-tier").addEventListener("change", updateSearchNote);

  // Mattermost's own metrics need an Enterprise licence. The rest of the stack
  // (host, PostgreSQL, Nginx, OpenSearch, logs) works on any licence, so the
  // monitoring servers are still worth building; say what you do and don't get.
  function updateMonitoringNote() {
    const enterprise = el("f-tier").value === "enterprise";
    const managed = qs('input[name="observability"]:checked').value === "managed";
    const note = el("monitoring-note");
    if (!managed) { note.textContent = ""; return; }
    note.textContent = enterprise
      ? "Mattermost's own metrics on port 8067 are collected too. They need the Enterprise licence in place."
      : "Host, PostgreSQL, Nginx and log collection work on any licence. Mattermost's own metrics on "
        + "port 8067 need Enterprise, so the Mattermost dashboards stay empty on this licence.";
  }

  el("f-seats").addEventListener("change", applyBandDefaults);
  el("f-seats").addEventListener("change", updateSearchNote);
  el("f-tier").addEventListener("change", updateHaNote);
  el("f-tier").addEventListener("change", updateMonitoringNote);
  qsa('input[name="observability"]').forEach(r => r.addEventListener("change", updateMonitoringNote));
  el("f-ha").addEventListener("change", updateHaNote);
  el("f-app-servers").addEventListener("input", updateHaNote);
  el("f-db-replicas").addEventListener("input", updateHaNote);

  qs('input[name="observability"][value="external"]').addEventListener("change", () => {
    el("observability-external").style.display = "";
    el("monitoring-layout-row").style.display = "none";
  });
  qs('input[name="observability"][value="managed"]').addEventListener("change", () => {
    el("observability-external").style.display = "none";
    el("monitoring-layout-row").style.display = "";
  });

  el("to-step-2").addEventListener("click", () => {
    const problems = step1Problems();
    if (problems.length) {
      // Each one is marked on its own field, so this just points at them.
      showError("step1-error", problems.length === 1
        ? "Check the answer marked above."
        : "Check the " + countWord(problems.length) + " answers marked above.");
      return;
    }
    showError("step1-error", "");
    state.s = readState();
    const rec = buildRecommendation(state.s);
    state.band = rec.band;
    // Only rebuild the nodes when an answer that shapes them changed, so coming
    // back here to fix a URL doesn't throw away the estate you've edited.
    const signature = shapeSignature(state.s);
    if (signature !== state.signature || !nodes.length) {
      setNodesFromRoles(rec.roles);
      state.signature = signature;
    }
    showStep(2);
    renderStep2();
  });
  el("back-to-1").addEventListener("click", () => showStep(1));

  el("to-step-3").addEventListener("click", () => {
    if (!nodes.length) { showError("step2-error", "Add at least one node before continuing."); return; }
    showError("step2-error", "");
    renderStep3();
    showStep(3);
  });
  el("back-to-2").addEventListener("click", () => { showStep(2); renderEstateSummary(); renderDiagram(); });

  el("to-step-4").addEventListener("click", () => {
    const problems = nodeProblems();
    markBadRows();
    if (problems.length) {
      showError("step3-error", problems.join(" "));
      return;
    }
    showError("step3-error", "");
    renderStep4();
    showStep(4);
  });
  el("back-to-3").addEventListener("click", () => showStep(3));

  el("copy-blob").addEventListener("click", (e) => copyFrom(el("blob-out").textContent, e.target));
  el("copy-yaml").addEventListener("click", (e) => copyFrom(el("yaml-out").value, e.target));
  el("download-blob").addEventListener("click", () =>
    downloadFile(planFilename("txt"), el("blob-out").textContent + "\n"));
  el("download-yaml").addEventListener("click", () =>
    downloadFile(planFilename("yml"), el("yaml-out").value));
  el("copy-decisions").addEventListener("click", (e) => copyFrom(
    toMarkdown(["Decision", "What it means"], buildDecisions()), e.target));
  el("copy-firewall").addEventListener("click", (e) => copyFrom(
    toMarkdown(["Source", "Destination", "Port", "Protocol", "What it carries"],
      buildFirewallRows().map(r => [r.from, r.to, r.port, r.proto, r.purpose])), e.target));
  el("copy-outbound").addEventListener("click", (e) => copyFrom(
    toMarkdown(["Source", "Destination", "Port", "Protocol", "What it carries", "When"],
      buildOutboundRows().map(r => [r.from, r.to, r.port, r.proto, r.purpose, r.when])), e.target));
  el("copy-infra").addEventListener("click", (e) => copyFrom(
    toMarkdown(["Server", "Runs", "vCPU", "RAM", "Disk", "Notes"],
      buildInfraRows().map(r => [r.name, r.runs, r.cpus, r.ram, r.disk, r.note]))
    + "\n\n" + (DATA.sizing_source || "") + "\n\n" + (DATA.storage_formula || ""), e.target));

  el("start-over").addEventListener("click", () => {
    nodes = []; nextId = 1; state = {};
    showStep(1);
  });

  // The step chips are navigation. Going back is free; going forward runs the
  // same checks the buttons do, and stops on the first one that complains.
  function currentStep() {
    for (let i = 1; i <= 4; i++) {
      if (el("step-" + i).style.display !== "none") return i;
    }
    return 1;
  }

  function gotoStep(target) {
    let here = currentStep();
    if (target === here) return;
    if (target < here) {
      showStep(target);
      if (target === 3) renderStep3();
      if (target === 2) { renderEstateSummary(); renderDiagram(); }
      return;
    }
    while (here < target) {
      const button = el("to-step-" + (here + 1));
      if (!button) return;
      button.click();
      const moved = currentStep();
      if (moved === here) return;  // a check stopped us, and it has said why
      here = moved;
    }
  }

  qsa(".step").forEach(chip => chip.addEventListener("click", () => gotoStep(Number(chip.dataset.step))));

  el("f-add-role").innerHTML = roleOptions("app");
  applyBandDefaults();
  updateSearchNote();
  updateCallsNote();
  updateMonitoringNote();
  showStep(1);
})();
