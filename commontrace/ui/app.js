/* CommonTrace console. No build step, no dependencies, no inline script.
 * Everything dynamic is rendered with textContent / DOM nodes, never innerHTML: agent ids,
 * memory ids and occasion ids are untrusted input from whatever is calling the gateway. */
(function () {
  "use strict";

  var POLL_MS = 5000;
  var main = document.getElementById("main");
  var nav = document.getElementById("nav");
  var conn = document.getElementById("conn");
  var envChip = document.getElementById("env");
  var token = "";
  var timer = null;
  var lastOk = 0;
  var state = { status: null, memories: null, agents: null, events: null };
  var connState = "";

  // ---- helpers -------------------------------------------------------------------------

  function h(tag, attrs) {
    var el = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (k) {
        var v = attrs[k];
        if (v === null || v === undefined || v === false) return;
        if (k === "class") el.className = v;
        else if (k === "text") el.textContent = v;
        else el.setAttribute(k, v === true ? "" : String(v));
      });
    }
    for (var i = 2; i < arguments.length; i++) {
      var c = arguments[i];
      if (c === null || c === undefined || c === false) continue;
      el.appendChild(typeof c === "string" || typeof c === "number" ? document.createTextNode(String(c)) : c);
    }
    return el;
  }
  function svg(tag, attrs) {
    var el = document.createElementNS("http://www.w3.org/2000/svg", tag);
    Object.keys(attrs || {}).forEach(function (k) { el.setAttribute(k, String(attrs[k])); });
    for (var i = 2; i < arguments.length; i++) {
      var c = arguments[i];
      if (c) el.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    }
    return el;
  }
  function pct(x, digits) {
    if (x === null || x === undefined || isNaN(x)) return "–";
    var v = (x * 100).toFixed(digits === undefined ? 1 : digits);
    return (x > 0 ? "+" : "") + v + "%";
  }
  function plain(x, digits) { return (x * 100).toFixed(digits === undefined ? 0 : digits) + "%"; }
  function num(x) { return x === null || x === undefined ? "–" : Number(x).toLocaleString(); }
  function ago(iso) {
    var t = Date.parse(iso);
    if (isNaN(t)) return "";
    var s = Math.max(0, Math.round((Date.now() - t) / 1000));
    if (s < 5) return "just now";
    if (s < 60) return s + " s ago";
    if (s < 3600) return Math.round(s / 60) + " min ago";
    if (s < 86400) return Math.round(s / 3600) + " h ago";
    return Math.round(s / 86400) + " d ago";
  }
  function clock(iso) {
    var d = new Date(iso);
    return isNaN(d) ? "" : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  }
  function store(key, val) { try { if (val === null) localStorage.removeItem(key); else localStorage.setItem(key, val); } catch (e) { /* private mode */ } }
  function load(key) { try { return localStorage.getItem(key); } catch (e) { return null; } }

  // ---- verdict / validity chips: icon + label + colour, never colour alone ---------------

  var VERDICTS = {
    HELPS: { icon: "▲", cls: "pos", label: "Helps" },
    HURTS: { icon: "▼", cls: "neg", label: "Hurts" },
    NO_MEASURABLE_EFFECT: { icon: "●", cls: "zero", label: "No measurable effect" },
    UNDERPOWERED: { icon: "○", cls: "zero", label: "Not yet decided" }
  };
  function verdictChip(v) {
    var d = VERDICTS[v] || { icon: "?", cls: "zero", label: String(v) };
    return h("span", { class: "chip" }, h("span", { class: "ic " + d.cls, "aria-hidden": "true", text: d.icon }), d.label);
  }
  var VALIDITY = {
    SOUND: { icon: "✓", cls: "good", label: "Sound" },
    WEAKENED: { icon: "!", cls: "warn", label: "Weakened" },
    COMPROMISED: { icon: "✕", cls: "crit", label: "Compromised" }
  };
  function validityChip(v) {
    var d = VALIDITY[v] || { icon: "?", cls: "warn", label: String(v || "Unknown") };
    return h("span", { class: "chip " + d.cls }, h("span", { class: "ic " + d.cls, "aria-hidden": "true", text: d.icon }), "Validity: " + d.label);
  }

  // Server messages quote commands in backticks; show them as code, as text nodes only.
  function rich(text) {
    var out = h("span");
    String(text).split("`").forEach(function (part, i) {
      out.appendChild(i % 2 ? h("code", { text: part }) : document.createTextNode(part));
    });
    return out;
  }

  // ---- API ------------------------------------------------------------------------------------

  function api(path) {
    return fetch(path, { headers: { Authorization: "Bearer " + token }, cache: "no-store" }).then(function (r) {
      if (r.status === 401) { var e = new Error("auth"); e.auth = true; throw e; }
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    });
  }
  function setConn(kind, text) {
    if (connState === kind + text) return;
    connState = kind + text;
    conn.textContent = text;
    conn.setAttribute("data-state", kind);
  }

  // ---- routing -----------------------------------------------------------------------------------

  var ROUTES = [
    { id: "overview", label: "Overview", title: "Overview" },
    { id: "memories", label: "Memories", title: "What each memory did" },
    { id: "live", label: "Live", title: "Live activity" },
    { id: "fleet", label: "Fleet", title: "Robots and agents" },
    { id: "safety", label: "Safety", title: "Safety and policy" }
  ];
  function currentRoute() {
    var id = (location.hash.replace(/^#\/?/, "").split("?")[0]) || "overview";
    return ROUTES.filter(function (r) { return r.id === id; })[0] || ROUTES[0];
  }
  function renderNav() {
    var cur = currentRoute().id;
    nav.textContent = "";
    ROUTES.forEach(function (r) {
      nav.appendChild(h("a", { href: "#/" + r.id, "aria-current": r.id === cur ? "page" : false, text: r.label }));
    });
  }

  // ---- views ---------------------------------------------------------------------------------------

  function empty(title, body, command) {
    return h("div", { class: "card" }, h("h2", { text: title }), h("p", { class: "muted", text: body }),
      command ? h("pre", null, h("code", { text: command })) : null);
  }

  function viewOverview() {
    var s = state.status, m = state.memories, a = state.agents;
    var root = h("div");
    root.appendChild(h("h1", { text: "Overview" }));
    root.appendChild(h("p", { class: "lede", text: "Is the memory helping, can the measurement be trusted, and is anything hurting?" }));
    if (!s) return root.appendChild(h("p", { class: "muted", text: "Loading…" })), root;

    var attention = h("div");
    var hurting = ((m && m.memories) || []).filter(function (x) { return x.verdict === "HURTS"; });
    hurting.forEach(function (x) {
      var withdrawn = x.withdrawn;
      attention.appendChild(h("div", { class: "alert " + (withdrawn ? "warn" : "crit"), role: "group", "aria-label": "Harmful memory" },
        h("strong", { text: (withdrawn ? "Withdrawn: " : "Still being delivered: ") + x.lesson_slug }),
        h("span", null, rich("Measured to make outcomes worse (" + pct(x.effect) + ", 95% interval " + pct(x.ci_low) + " to " + pct(x.ci_high) + ")." +
          (withdrawn ? " It is no longer delivered." : " The store's harm policy is " + (s.gateway.harm_policy || "inform") + "; restart the gateway with `--on-harm withdraw` to stop it.")))));
    });
    if (m && m.integrity && m.integrity.verdict !== "SOUND") {
      var bad = m.integrity.verdict === "COMPROMISED";
      attention.appendChild(h("div", { class: "alert " + (bad ? "crit" : "warn") },
        h("strong", { text: bad ? "The experiment cannot be trusted as it stands" : "The experiment is weakened" }),
        h("span", { text: bad ? "No effect or value should be quoted until the findings are fixed (see the report)." : "Read the verdicts below with that in mind." })));
    }
    if (attention.childNodes.length) root.appendChild(attention);

    var grid = h("div", { class: "grid" });
    var exp = s.experiment || {};
    grid.appendChild(h("section", { class: "card", "aria-labelledby": "c-exp" },
      h("h2", { id: "c-exp", text: "Experiment" }),
      exp.running
        ? h("p", { class: "big", text: plain(exp.rate) })
        : h("p", { class: "big", text: "Not started" }),
      h("p", { class: "muted", text: exp.running
        ? "of eligible memories are withheld at random, to measure what they do."
        : "Nothing is withheld until an experiment is started on purpose." }),
      exp.running ? null : h("pre", null, h("code", { text: "commontrace proof start <function> --label NAME --daily N" }))));

    var p = s.proof;
    var proofCard = h("section", { class: "card", "aria-labelledby": "c-proof" }, h("h2", { id: "c-proof", text: "Proof" }));
    if (p) {
      var pctDone = p.planned_occasions ? Math.min(100, Math.round(100 * p.resolved_occasions / p.planned_occasions)) : 0;
      proofCard.appendChild(h("p", { class: "muted", text: p.label + (p.synthetic ? " · synthetic demo data" : "") }));
      var fill = h("div", { class: "bar-fill" });
      fill.style.width = pctDone + "%"; // CSSOM, not a style attribute: the CSP forbids inline styles
      proofCard.appendChild(h("div", { class: "bar-track", role: "progressbar", "aria-valuemin": "0", "aria-valuemax": "100",
        "aria-valuenow": pctDone, "aria-label": "Planned occasions resolved" }, fill));
      proofCard.appendChild(h("p", { text: num(p.resolved_occasions) + " of " + num(p.planned_occasions) + " planned occasions resolved (" + pctDone + "%), day " + p.days_elapsed + "." }));
      if (p.integrity) proofCard.appendChild(h("p", null, validityChip(p.integrity)));
      proofCard.appendChild(h("p", { class: "muted small" }, rich(p.next_step)));
    } else {
      proofCard.appendChild(h("p", { class: "muted", text: "No proof has been started in this store." }));
    }
    grid.appendChild(proofCard);

    var act = s.activity || {};
    grid.appendChild(h("section", { class: "card", "aria-labelledby": "c-act" },
      h("h2", { id: "c-act", text: "Activity" }),
      h("dl", { class: "kv" },
        h("dt", { text: "Recalls" }), h("dd", { text: num(act.recalls) }),
        h("dt", { text: "Outcomes" }), h("dd", { text: num(act.outcomes) }),
        h("dt", { text: "Agents" }), h("dd", { text: a ? num((a.agents || []).length) : "–" }),
        h("dt", { text: "Occasions measured" }), h("dd", { text: m && m.occasions !== undefined ? num(m.occasions) : "–" })),
      h("p", { class: "muted small", text: "Counts cover the gateway's recent event window." })));
    root.appendChild(grid);

    if (m && m.memories && m.memories.length) root.appendChild(memoriesPanel(m, true));
    else root.appendChild(empty("No measurements yet", "Verdicts appear once recalls are made with an occasion id and outcomes are reported under the same id.",
      "curl -H \"Authorization: Bearer $TOKEN\" -d '{\"occasion_id\":\"ep-1\",\"items\":[{\"id\":\"m1\",\"text\":\"…\"}]}' \\\n  -H 'Content-Type: application/json' http://localhost:8787/v1/recall"));
    return root;
  }

  function niceExtent(rows) {
    var m = 0.1;
    rows.forEach(function (r) { m = Math.max(m, Math.abs(r.ci_low), Math.abs(r.ci_high), Math.abs(r.effect)); });
    var steps = [0.1, 0.2, 0.25, 0.3, 0.4, 0.5, 0.75, 1.0];
    for (var i = 0; i < steps.length; i++) if (m * 1.08 <= steps[i]) return steps[i];
    return 1.0;
  }
  function forestRow(r, extent) {
    var W = 600, H = 36, pad = 16, mid = H / 2;
    var x = function (v) { return pad + ((v + extent) / (2 * extent)) * (W - 2 * pad); };
    var cls = r.verdict === "HELPS" ? "pos-c" : r.verdict === "HURTS" ? "neg-c" : "zero-c";
    var d = svg("svg", { class: "f-svg", viewBox: "0 0 " + W + " " + H, role: "img",
      "aria-label": r.lesson_slug + ": effect " + pct(r.effect) + ", 95% interval " + pct(r.ci_low) + " to " + pct(r.ci_high) });
    d.appendChild(svg("line", { class: "grid", x1: x(-extent / 2), x2: x(-extent / 2), y1: 4, y2: H - 4 }));
    d.appendChild(svg("line", { class: "grid", x1: x(extent / 2), x2: x(extent / 2), y1: 4, y2: H - 4 }));
    d.appendChild(svg("line", { class: "zero", x1: x(0), x2: x(0), y1: 2, y2: H - 2 }));
    d.appendChild(svg("line", { class: "ci " + cls, x1: x(Math.max(-extent, r.ci_low)), x2: x(Math.min(extent, r.ci_high)), y1: mid, y2: mid }));
    var hollow = r.verdict === "UNDERPOWERED";
    d.appendChild(svg("circle", { class: "dot " + cls + (hollow ? " hollow" : ""), cx: x(Math.max(-extent, Math.min(extent, r.effect))), cy: mid, r: 6.5 }));
    return d;
  }
  // An interval of exactly 0 to 0 is the estimator declining to answer, not a measured zero.
  function measured(r) { return !(r.ci_low === 0 && r.ci_high === 0 && r.verdict === "UNDERPOWERED"); }
  function axisRow(extent) {
    // HTML, not SVG text: SVG text scales with the viewBox and is unreadable on a phone.
    var row = h("div", { class: "f-axis", "aria-hidden": "true" });
    [-extent, -extent / 2, 0, extent / 2, extent].forEach(function (v) {
      row.appendChild(h("span", { text: (v > 0 ? "+" : "") + Math.round(v * 100) + "%" }));
    });
    return row;
  }
  function memoriesPanel(m, compact) {
    var rows = m.memories.slice().sort(function (a, b) { return a.effect - b.effect; });
    var extent = niceExtent(rows);
    var card = h("section", { class: "card wide", "aria-labelledby": "c-mem" }, h("h2", { id: "c-mem", text: "What each memory did" }));
    if (m.integrity) card.appendChild(h("p", null, validityChip(m.integrity.verdict)));
    var list = h("ul", { class: "forest" });
    rows.forEach(function (r) {
      list.appendChild(h("li", null,
        h("div", { class: "f-head" },
          h("span", { class: "f-name mono", text: r.lesson_slug }), verdictChip(r.verdict),
          r.withdrawn ? h("span", { class: "chip" }, h("span", { class: "ic crit", "aria-hidden": "true", text: "✕" }), "Withdrawn") : null,
          h("span", { class: "f-num", text: measured(r) ? pct(r.effect) + " (" + pct(r.ci_low) + " to " + pct(r.ci_high) + ")"
            : "Not enough data yet: " + num(r.n_injected) + " with, " + num(r.n_withheld) + " without" })),
        forestRow(r, extent)));
    });
    card.appendChild(list);
    card.appendChild(axisRow(extent));
    card.appendChild(h("ul", { class: "legend" },
      h("li", null, h("span", { class: "ic pos", "aria-hidden": "true", text: "▲ " }), "right of zero: outcomes better with the memory"),
      h("li", null, h("span", { class: "ic neg", "aria-hidden": "true", text: "▼ " }), "left of zero: outcomes worse"),
      h("li", null, "Bars are 95% intervals" + (m.mode === "sequential" ? ", valid however often the run is looked at" : ""))));
    if (!compact) card.appendChild(memoryTable(rows));
    return card;
  }
  function memoryTable(rows) {
    var t = h("table", null,
      h("caption", { text: "The same figures as a table" }),
      h("thead", null, h("tr", null, h("th", { scope: "col", text: "Memory" }), h("th", { scope: "col", text: "Verdict" }),
        h("th", { scope: "col", class: "num", text: "Effect" }), h("th", { scope: "col", class: "num", text: "95% interval" }),
        h("th", { scope: "col", class: "num", text: "Injected" }), h("th", { scope: "col", class: "num", text: "Withheld" }))));
    var tb = h("tbody");
    rows.forEach(function (r) {
      tb.appendChild(h("tr", null, h("th", { scope: "row", class: "mono wrap", text: r.lesson_slug }),
        h("td", null, verdictChip(r.verdict)), h("td", { class: "num", text: pct(r.effect) }),
        h("td", { class: "num", text: pct(r.ci_low) + " to " + pct(r.ci_high) }),
        h("td", { class: "num", text: num(r.n_injected) }), h("td", { class: "num", text: num(r.n_withheld) })));
    });
    t.appendChild(tb);
    return h("div", { class: "table-wrap", role: "region", "aria-label": "Memory results table", tabindex: "0" }, t);
  }

  function viewMemories() {
    var root = h("div"), m = state.memories;
    root.appendChild(h("h1", { text: "What each memory did" }));
    root.appendChild(h("p", { class: "lede", text: "Each memory is withheld at random on a share of occasions. The difference in outcomes between the two groups is its causal effect." }));
    if (!m) root.appendChild(h("p", { class: "muted", text: "Loading…" }));
    else if (!m.memories || !m.memories.length) root.appendChild(empty("No measurements yet", m.note || "Nothing has been recalled and resolved under an experiment."));
    else root.appendChild(memoriesPanel(m, false));
    return root;
  }

  function viewLive() {
    var root = h("div");
    root.appendChild(h("h1", { text: "Live activity" }));
    root.appendChild(h("p", { class: "lede", text: "The most recent recalls and outcomes, newest first." }));
    var ev = state.events && state.events.events;
    if (!ev) { root.appendChild(h("p", { class: "muted", text: "Loading…" })); return root; }
    if (!ev.length) { root.appendChild(empty("Nothing yet", "Events appear as agents call the gateway.")); return root; }
    var ul = h("ul", { class: "events", "aria-label": "Recent events" });
    ev.forEach(function (e) {
      var what;
      if (e.kind === "recall") what = "recall: delivered " + e.delivered + ", withheld " + e.withheld +
        (e.withdrawn ? ", withdrawn " + e.withdrawn : "") + (e.protected ? ", protected " + e.protected : "") + (e.quarantined ? ", quarantined " + e.quarantined : "");
      else if (e.kind === "outcome") what = "outcome: " + (e.succeeded ? "succeeded" : "failed");
      else what = "outcome undecided";
      ul.appendChild(h("li", null, h("time", { datetime: e.at, text: clock(e.at) }),
        h("span", { class: "mono", text: e.agent_id || "—" }),
        h("span", null, h("span", { class: "mono", text: e.occasion_id }), " — " + what)));
    });
    root.appendChild(h("div", { class: "card" }, ul));
    return root;
  }

  function viewFleet() {
    var root = h("div");
    root.appendChild(h("h1", { text: "Robots and agents" }));
    root.appendChild(h("p", { class: "lede", text: "Who is calling, how often, and how their episodes are going." }));
    var a = state.agents && state.agents.agents;
    if (!a) { root.appendChild(h("p", { class: "muted", text: "Loading…" })); return root; }
    if (!a.length) { root.appendChild(empty("No agents yet", "Pass an agent_id on each call to see them here.")); return root; }
    var t = h("table", null, h("caption", { text: "Activity per agent over the recent event window" }),
      h("thead", null, h("tr", null, h("th", { scope: "col", text: "Agent" }), h("th", { scope: "col", text: "Seen" }),
        h("th", { scope: "col", class: "num", text: "Recalls" }), h("th", { scope: "col", class: "num", text: "Outcomes" }),
        h("th", { scope: "col", class: "num", text: "Success" }), h("th", { scope: "col", class: "num", text: "Protected" }),
        h("th", { scope: "col", class: "num", text: "Quarantined" }))));
    var tb = h("tbody");
    a.forEach(function (r) {
      var idle = r.seconds_since_seen !== null && r.seconds_since_seen > 120;
      tb.appendChild(h("tr", null, h("th", { scope: "row", class: "mono wrap", text: r.agent_id }),
        h("td", null, h("span", { class: "chip" }, h("span", { class: "ic " + (idle ? "warn" : "good"), "aria-hidden": "true", text: idle ? "◌" : "●" }),
          (idle ? "Idle · " : "Active · ") + ago(r.last_seen))),
        h("td", { class: "num", text: num(r.recalls) }), h("td", { class: "num", text: num(r.outcomes) }),
        h("td", { class: "num", text: r.success_rate === null ? "–" : plain(r.success_rate) }),
        h("td", { class: "num", text: num(r.protected) }), h("td", { class: "num", text: num(r.quarantined || 0) })));
    });
    t.appendChild(tb);
    root.appendChild(h("div", { class: "table-wrap", role: "region", "aria-label": "Agents table", tabindex: "0" }, t));
    if (state.agents.window) root.appendChild(h("p", { class: "muted small", text: "Window: " + state.agents.window + "." }));
    return root;
  }

  function viewSafety() {
    var root = h("div"), s = state.status;
    root.appendChild(h("h1", { text: "Safety and policy" }));
    root.appendChild(h("p", { class: "lede", text: "What this store guarantees, set by the operator and enforced for every client." }));
    if (!s) { root.appendChild(h("p", { class: "muted", text: "Loading…" })); return root; }
    var g = s.gateway;
    var grid = h("div", { class: "grid" });
    var prefixes = g.protected_prefixes || [];
    grid.appendChild(h("section", { class: "card", "aria-labelledby": "s-prot" }, h("h2", { id: "s-prot", text: "Protected memories" }),
      h("p", { text: "Safety constraints are always delivered. They are never withheld as a control, never withdrawn and never counted in an experiment." }),
      prefixes.length
        ? h("ul", null, prefixes.map(function (p) { return h("li", null, "ids starting ", h("code", { text: p })); }))
        : h("p", { class: "muted", text: "No protected id prefix is set. Items can still be marked protected on each call." }),
      prefixes.length ? null : h("pre", null, h("code", { text: "commontrace gateway --protect safety/" }))));
    grid.appendChild(h("section", { class: "card", "aria-labelledby": "s-env" }, h("h2", { id: "s-env", text: "Environment" }),
      g.env ? h("p", { class: "big", text: g.env }) : h("p", { class: "big", text: "Not set" }),
      h("p", { class: "muted", text: g.env
        ? "This store measures only this environment. A request naming another is refused, so simulation and reality are never pooled."
        : "Set one with --env sim|real so simulation and reality can never share a store." })));
    grid.appendChild(h("section", { class: "card", "aria-labelledby": "s-pol" }, h("h2", { id: "s-pol", text: "Policy" }),
      h("dl", { class: "kv" },
        h("dt", { text: "Harm policy" }), h("dd", { text: g.harm_policy === "withdraw" ? "Withdraw: a memory measured to hurt is no longer delivered" : "Inform: harmful memories are reported but still delivered" }),
        h("dt", { text: "Log durability" }), h("dd", { text: g.durable ? "Strict: every line is flushed to disk" : "Relaxed: a power loss can drop the last few lines" }),
        h("dt", { text: "Withholding" }), h("dd", { text: s.experiment && s.experiment.running ? "On, at " + plain(s.experiment.rate) : "Off until an experiment is started" }),
        h("dt", { text: "API" }), h("dd", { text: "v" + g.api + " · commontrace " + g.version }))));
    root.appendChild(grid);
    var a = state.agents && state.agents.agents;
    if (a && a.length) {
      var quarantined = a.reduce(function (n, r) { return n + (r.quarantined || 0); }, 0);
      var protectedN = a.reduce(function (n, r) { return n + (r.protected || 0); }, 0);
      root.appendChild(h("div", { class: "card" }, h("h2", { text: "Recent protection activity" }),
        h("p", { text: num(protectedN) + " protected deliveries and " + num(quarantined) + " items quarantined by the injection screen in the recent window." })));
    }
    return root;
  }

  var VIEWS = { overview: viewOverview, memories: viewMemories, live: viewLive, fleet: viewFleet, safety: viewSafety };

  function viewAuth(message) {
    var input = h("input", { id: "tok", type: "password", autocomplete: "off", spellcheck: "false", "aria-describedby": "tok-help" });
    var form = h("form", null,
      h("label", { for: "tok" }, "Access token"), input,
      h("p", { id: "tok-help", class: "muted small", text: "Printed when the gateway started, and kept in memory/gateway.token. It is stored only for this browser tab." }),
      h("button", { class: "btn", type: "submit", text: "Connect" }));
    form.addEventListener("submit", function (ev) {
      ev.preventDefault();
      token = input.value.trim();
      try { sessionStorage.setItem("ct-token", token); } catch (e) { /* ignore */ }
      refresh();
    });
    return h("div", { class: "auth card" }, h("h1", { text: "Connect to the gateway" }),
      message ? h("p", { class: "alert crit", role: "alert", text: message }) : null, form);
  }

  // ---- loop ---------------------------------------------------------------------------------------------

  function render() {
    var route = currentRoute();
    renderNav();
    document.title = route.title + " · CommonTrace";
    main.textContent = "";
    main.appendChild(VIEWS[route.id]());
  }
  function paintHeader() {
    var s = state.status;
    if (s && s.gateway && s.gateway.env) { envChip.hidden = false; envChip.textContent = "Environment: " + s.gateway.env; }
    else envChip.hidden = true;
  }

  function refresh() {
    if (!token) { main.textContent = ""; main.appendChild(viewAuth()); setConn("bad", "Not connected"); return Promise.resolve(); }
    var route = currentRoute().id;
    var wants = ["status", "memories", "agents"];
    if (route === "live") wants.push("occasions");
    return Promise.all(wants.map(function (w) { return api("/v1/" + w).then(function (d) { return [w, d]; }); }))
      .then(function (pairs) {
        pairs.forEach(function (p) { state[p[0] === "occasions" ? "events" : p[0]] = p[1]; });
        lastOk = Date.now();
        setConn("ok", "Live · updated " + ago(new Date(lastOk).toISOString()));
        paintHeader();
        render();
      })
      .catch(function (e) {
        if (e && e.auth) { token = ""; try { sessionStorage.removeItem("ct-token"); } catch (x) { /* ignore */ } main.textContent = ""; main.appendChild(viewAuth("That token was not accepted.")); setConn("bad", "Not connected"); return; }
        setConn("bad", lastOk ? "Disconnected · last update " + ago(new Date(lastOk).toISOString()) : "Cannot reach the gateway");
      });
  }
  function schedule() {
    clearTimeout(timer);
    timer = setTimeout(function () { (document.hidden ? Promise.resolve() : refresh()).then(schedule); }, POLL_MS);
  }

  // ---- theme, token bootstrap -----------------------------------------------------------------------------

  var themeSel = document.getElementById("theme");
  function applyTheme(v) {
    if (v === "light" || v === "dark") document.documentElement.setAttribute("data-theme", v);
    else document.documentElement.removeAttribute("data-theme");
  }
  var saved = load("ct-theme") || "system";
  themeSel.value = saved; applyTheme(saved);
  themeSel.addEventListener("change", function () { store("ct-theme", themeSel.value); applyTheme(themeSel.value); });

  // The token arrives in the URL FRAGMENT (#token=...), which browsers never send to a server or
  // a referrer. It is moved to sessionStorage and removed from the address bar.
  (function bootstrapToken() {
    var m = /(?:^|[#&])token=([^&]+)/.exec(location.hash);
    if (m) {
      token = decodeURIComponent(m[1]);
      try { sessionStorage.setItem("ct-token", token); } catch (e) { /* ignore */ }
      history.replaceState(null, "", location.pathname + location.search + "#/overview");
    } else {
      try { token = sessionStorage.getItem("ct-token") || ""; } catch (e) { token = ""; }
    }
  })();

  window.addEventListener("hashchange", function () { render(); main.focus(); refresh(); });
  document.addEventListener("visibilitychange", function () { if (!document.hidden) refresh(); });
  setInterval(function () { if (lastOk && connState.indexOf("ok") === 0) setConn("ok", "Live · updated " + ago(new Date(lastOk).toISOString())); }, 1000);

  renderNav();
  refresh().then(schedule);
})();
