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
  var tierChip = document.getElementById("tier");
  var commandLaunch = document.getElementById("command-launch");
  var token = "";
  var timer = null;
  var lastOk = 0;
  var state = { status: null, capabilities: null, memories: null, agents: null, events: null, lessons: null, lesson: null, commandCatalog: null, commandResult: null };
  var selected = {};   // slug -> true, the review queue's bulk selection; survives repaints
  var notice = null;   // { kind: "ok"|"crit", text } shown on the next paint of the review views
  var lastPaint = "";
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
  function post(path, body) {
    return fetch(path, { method: "POST", cache: "no-store",
      headers: { Authorization: "Bearer " + token, "Content-Type": "application/json" },
      body: JSON.stringify(body) }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (d) {
        if (r.status === 401) { var e = new Error("auth"); e.auth = true; throw e; }
        if (!r.ok) { var err = new Error((d.error && d.error.message) || ("HTTP " + r.status)); err.code = d.error && d.error.code; throw err; }
        return d;
      });
    });
  }
  function canon(slug) { return String(slug).replace(/\.md$/, "").replace(/^lesson_/, ""); }
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
    { id: "review", label: "Review", title: "Review queue" },
    { id: "lesson", label: "Review", title: "Lesson", hidden: true },
    { id: "live", label: "Live", title: "Live activity" },
    { id: "fleet", label: "Fleet", title: "Robots and agents" },
    { id: "commands", label: "Command center", title: "Command center" },
    { id: "safety", label: "Safety", title: "Safety and policy" }
  ];
  function hashQuery(name) {
    var m = new RegExp("[?&]" + name + "=([^&]*)").exec(location.hash);
    return m ? decodeURIComponent(m[1]) : "";
  }
  function currentRoute() {
    var id = (location.hash.replace(/^#\/?/, "").split("?")[0]) || "overview";
    return ROUTES.filter(function (r) { return r.id === id; })[0] || ROUTES[0];
  }
  function renderNav() {
    var cur = currentRoute().id;
    nav.textContent = "";
    ROUTES.forEach(function (r) {
      if (r.hidden) return;
      var here = r.id === cur || (r.id === "review" && cur === "lesson");
      nav.appendChild(h("a", { href: "#/" + r.id, "aria-current": here ? "page" : false, text: r.label }));
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


  // ---- review queue and lesson page ------------------------------------------------------------------------

  var GATES = {
    scaffolding: "Unedited scaffolding: a rule or condition still says TODO",
    safety: "Safety scan: a credential or an instruction aimed at the agent",
    redundancy: "Restates an active lesson"
  };
  function gateChip(c) {
    if (!c) return null;
    if (c.passes) return h("span", { class: "chip good" }, h("span", { class: "ic", "aria-hidden": "true", text: "✓" }), "Passes the gates");
    return h("span", { class: "chip crit" }, h("span", { class: "ic", "aria-hidden": "true", text: "✕" }), "Needs work: " + c.failed.join(", "));
  }
  function noticeBox() {
    if (!notice) return null;
    var n = notice;
    return h("p", { class: "alert " + (n.kind === "ok" ? "good" : "crit"), role: n.kind === "ok" ? "status" : "alert", text: n.text });
  }
  function approvalOff() {
    return h("div", { class: "alert warn" }, h("strong", { text: "Reading only" }),
      h("span", null, "Editing, approving and rejecting are off, because approving changes what every agent is told. Start the gateway with "),
      h("code", { text: "--allow-approval" }), h("span", { text: " to turn them on for this session." }));
  }

  function viewReview() {
    var root = h("div"), d = state.lessons;
    root.appendChild(h("h1", { text: "Review queue" }));
    root.appendChild(h("p", { class: "lede", text: "Drafts waiting for a person. Nothing here is active: no agent has been told any of it." }));
    if (!d) { root.appendChild(h("p", { class: "muted", text: "Loading…" })); return root; }
    if (notice) root.appendChild(noticeBox());
    if (!d.approval_enabled) root.appendChild(approvalOff());
    if (!d.lessons.length) {
      root.appendChild(empty("Nothing waiting", "Drafts appear here after a distill, a dream pass or a suggested revision.", "commontrace distill --failed --draft"));
      return root;
    }
    var live = d.lessons.filter(function (l) { return l.checks && l.checks.passes; });
    if (d.approval_enabled) {
      var count = Object.keys(selected).filter(function (k) { return selected[k]; }).length;
      var bulk = h("button", { class: "btn", type: "button", disabled: count ? false : true, text: "Approve selected (" + count + ")" });
      bulk.addEventListener("click", function () { approveMany(Object.keys(selected).filter(function (k) { return selected[k]; })); });
      root.appendChild(h("div", { class: "toolbar" }, bulk,
        h("span", { class: "muted small", text: live.length + " of " + d.lessons.length + " pass every gate and can be approved." })));
    }
    var list = h("ul", { class: "queue", "aria-label": "Drafts awaiting review" });
    d.lessons.forEach(function (l) {
      var ok = l.checks && l.checks.passes;
      var box = null;
      if (d.approval_enabled) {
        box = h("input", { type: "checkbox", id: "sel-" + l.slug, disabled: ok ? false : true,
          checked: ok && selected[l.slug] ? true : false, "aria-label": "Select " + l.slug });
        box.addEventListener("change", function () { selected[l.slug] = box.checked; paint(true); });
      }
      var near = l.checks && l.checks.nearest_active;
      list.appendChild(h("li", { class: "card" },
        h("div", { class: "q-head" }, box,
          h("a", { class: "q-name mono", href: "#/lesson?slug=" + encodeURIComponent(l.slug), text: l.slug }),
          gateChip(l.checks),
          h("span", { class: "chip" }, l.drafted_by_model ? "Drafted by a model" : "Written by a person")),
        h("p", { class: "q-desc", text: l.description }),
        h("p", { class: "muted small" },
          num(l.source_traces) + " source trace" + (l.source_traces === 1 ? "" : "s") + (l.domain ? " · " + l.domain : ""),
          near ? " · closest active lesson: " : null, near ? h("span", { class: "mono", text: near.slug }) : null,
          near ? " (" + Math.round(near.similarity * 100) + "% alike)" : null)));
    });
    root.appendChild(list);
    return root;
  }

  function approveMany(slugs) {
    var out = [], chain = Promise.resolve();
    slugs.forEach(function (slug) {
      chain = chain.then(function () {
        return post("/v1/lesson/approve", { slug: slug, rationale: "approved from the review queue" })
          .then(function () { out.push(slug + ": approved"); delete selected[slug]; })
          .catch(function (e) { out.push(slug + ": " + e.message); });
      });
    });
    chain.then(function () {
      var failed = out.filter(function (x) { return x.indexOf(": approved") < 0; });
      notice = { kind: failed.length ? "crit" : "ok", text: out.join(" · ") };
      lastPaint = ""; refresh();
    });
  }

  function viewLesson() {
    var root = h("div"), d = state.lesson, slug = hashQuery("slug");
    root.appendChild(h("p", null, h("a", { href: "#/review", text: "← Review queue" })));
    if (!d || d.slug !== slug) { root.appendChild(h("p", { class: "muted", text: "Loading…" })); return root; }
    var inReview = d.status === "review", can = inReview && d.approval_enabled;
    root.appendChild(h("h1", { class: "mono", text: d.slug }));
    root.appendChild(h("p", { class: "lede", text: d.description }));
    if (notice) root.appendChild(noticeBox());
    root.appendChild(h("p", null, h("span", { class: "chip" }, "Status: " + d.status), " ",
      gateChip(d.checks), " ", h("span", { class: "chip" }, d.drafted_by_model ? "Drafted by a model" : "Written by a person")));
    if (inReview && !d.approval_enabled) root.appendChild(approvalOff());

    // The text, editable only for a draft and only when acting is on.
    var fields = [["rule", "Rule"], ["applies_when", "Applies when"], ["do_not_apply_when", "Does not apply when"]];
    var inputs = {};
    var form = h("form", { class: "card", "aria-label": "Lesson text" });
    form.appendChild(h("h2", { text: "What the agent would be told" }));
    fields.forEach(function (f) {
      form.appendChild(h("label", { for: "f-" + f[0], text: f[1] }));
      if (can) {
        inputs[f[0]] = h("textarea", { id: "f-" + f[0], rows: 3, maxlength: 2000, spellcheck: "true" });
        inputs[f[0]].value = d[f[0]] || "";
        form.appendChild(inputs[f[0]]);
      } else {
        form.appendChild(h("p", { class: "prose", text: d[f[0]] || "(none)" }));
      }
    });
    if (can) {
      var save = h("button", { class: "btn secondary", type: "submit", text: "Save changes" });
      form.appendChild(save);
      form.addEventListener("submit", function (ev) {
        ev.preventDefault();
        var body = { slug: d.slug };
        fields.forEach(function (f) { if (inputs[f[0]].value.trim() !== (d[f[0]] || "").trim()) body[f[0]] = inputs[f[0]].value; });
        if (Object.keys(body).length === 1) { notice = { kind: "ok", text: "Nothing changed." }; paint(true); return; }
        post("/v1/lesson/edit", body).then(function () { notice = { kind: "ok", text: "Saved. The gates below were re-checked." }; lastPaint = ""; refresh(); })
          .catch(function (e) { notice = { kind: "crit", text: e.message }; paint(true); });
      });
    }
    root.appendChild(form);

    if (d.checks) {
      var checks = h("section", { class: "card", "aria-labelledby": "g-h" }, h("h2", { id: "g-h", text: "Approval gates" }));
      Object.keys(GATES).forEach(function (g) {
        var failed = d.checks.failed.indexOf(g) >= 0;
        var extra = g === "redundancy" && d.checks.nearest_active
          ? " Closest: " + d.checks.nearest_active.slug + " (" + Math.round(d.checks.nearest_active.similarity * 100) + "% alike)." : "";
        checks.appendChild(h("p", { class: "gate" }, h("span", { class: "chip " + (failed ? "crit" : "good") },
          h("span", { class: "ic", "aria-hidden": "true", text: failed ? "✕" : "✓" }), failed ? "Fails" : "Passes"), " " + GATES[g] + "." + extra));
      });
      if (d.separation_of_duties) checks.appendChild(h("p", { class: "alert warn", text: "Separation of duties: " + d.separation_of_duties }));
      root.appendChild(checks);
    }

    var ev = h("section", { class: "card", "aria-labelledby": "e-h" }, h("h2", { id: "e-h", text: "Evidence and provenance" }));
    ev.appendChild(h("p", { text: num(d.source_traces) + " source trace" + (d.source_traces === 1 ? "" : "s") + (d.revises ? ", revising " + d.revises : "") + "." }));
    if (d.provenance) {
      var u = d.provenance.usage || {};
      ev.appendChild(h("dl", { class: "kv" },
        h("dt", { text: "Model" }), h("dd", { text: String(d.provenance.provider || "") + " · " + String(d.provenance.model || "") }),
        h("dt", { text: "Tokens" }), h("dd", { text: num(u.input_tokens) + " in, " + num(u.output_tokens) + " out" + (u.estimated ? " (estimated)" : "") }),
        h("dt", { text: "Cost" }), h("dd", { text: d.provenance.cost_usd !== undefined ? "$" + Number(d.provenance.cost_usd).toFixed(4) : "no price table configured" }),
        h("dt", { text: "Prompt" }), h("dd", { class: "mono", text: String(d.provenance.prompt_sha256 || "").slice(0, 16) })));
    }
    var mem = ((state.memories && state.memories.memories) || []).filter(function (m) { return canon(m.lesson_slug) === canon(d.slug); })[0];
    if (mem) ev.appendChild(h("p", null, "Measured effect: ", verdictChip(mem.verdict), " " + pct(mem.effect) + " (" + pct(mem.ci_low) + " to " + pct(mem.ci_high) + ")" + (mem.withdrawn ? ", withdrawn" : "")));
    root.appendChild(ev);

    root.appendChild(h("details", { class: "card" }, h("summary", { text: "Full text" }), h("pre", { class: "wrap" }, h("code", { text: d.body })),
      d.body_truncated ? h("p", { class: "muted small", text: "Shortened for display." }) : null));

    var hist = h("section", { class: "card", "aria-labelledby": "h-h" }, h("h2", { id: "h-h", text: "History" }));
    if (!d.history.length) hist.appendChild(h("p", { class: "muted", text: "No recorded changes." }));
    else {
      var ul = h("ul", { class: "events" });
      d.history.slice().reverse().forEach(function (r) {
        ul.appendChild(h("li", null, h("time", { datetime: r.at, text: clock(r.at) }), h("span", { class: "mono", text: String(r.actor || "") }),
          h("span", { text: (r.reason || "changed") + " (" + String(r.from || "new").slice(0, 8) + " → " + String(r.to || "").slice(0, 8) + ")" })));
      });
      hist.appendChild(ul);
    }
    root.appendChild(hist);

    if (can) {
      var why = h("input", { id: "why", type: "text", maxlength: 500, "aria-describedby": "why-help" });
      var approve = h("button", { class: "btn", type: "button", disabled: d.checks && d.checks.passes ? false : true, text: "Approve" });
      var reject = h("button", { class: "btn danger", type: "button", text: "Reject" });
      approve.addEventListener("click", function () {
        post("/v1/lesson/approve", { slug: d.slug, rationale: why.value || undefined })
          .then(function () { notice = { kind: "ok", text: d.slug + " is now active." }; location.hash = "#/review"; })
          .catch(function (e) { notice = { kind: "crit", text: e.message }; paint(true); });
      });
      reject.addEventListener("click", function () {
        if (!why.value.trim()) { notice = { kind: "crit", text: "Say why in the box before rejecting." }; paint(true); return; }
        post("/v1/lesson/reject", { slug: d.slug, reason: why.value })
          .then(function () { notice = { kind: "ok", text: d.slug + " was rejected." }; location.hash = "#/review"; })
          .catch(function (e) { notice = { kind: "crit", text: e.message }; paint(true); });
      });
      root.appendChild(h("section", { class: "card", "aria-labelledby": "a-h" }, h("h2", { id: "a-h", text: "Decide" }),
        h("label", { for: "why", text: "Reason or note" }), why,
        h("p", { id: "why-help", class: "muted small", text: "Recorded with the change. Approving activates the lesson for every agent; it is checked against the gates again and may still be refused." }),
        h("div", { class: "toolbar" }, approve, reject)));
    }
    return root;
  }

  function commandArgsFrom(text) {
    return String(text || "").split(/\r?\n/).map(function (line) { return line.trim(); }).filter(Boolean);
  }
  function commandLine(spec, args) {
    return "commontrace " + spec.name + (args.length ? " " + args.map(function (arg) {
      return /\s/.test(arg) ? JSON.stringify(arg) : arg;
    }).join(" ") : "");
  }
  function viewCommands() {
    var root = h("div"), catalog = state.commandCatalog && state.commandCatalog.commands;
    root.appendChild(h("div", { class: "page-heading" },
      h("div", null, h("p", { class: "eyebrow", text: "OPERATIONS" }), h("h1", { text: "Command center" }),
        h("p", { class: "lede", text: "Every CommonTrace capability, one authenticated surface. Run store-scoped commands without leaving the console." })),
      h("span", { class: "chip command-count", text: catalog ? catalog.length + " commands" : "Loading catalog…" })));
    if (!catalog) {
      root.appendChild(h("div", { class: "card command-loading" }, h("div", { class: "skeleton-line wide" }), h("div", { class: "skeleton-line" })));
      return root;
    }

    var search = h("input", { class: "command-search", type: "search", placeholder: "Filter commands…", "aria-label": "Filter commands" });
    var select = h("select", { class: "command-select", "aria-label": "Command" });
    var args = h("textarea", { class: "command-args", rows: 7, spellcheck: "false", "aria-label": "Command arguments, one per line" });
    var description = h("p", { class: "muted command-description" });
    var badge = h("div", { class: "command-badges" });
    var output = h("div", { class: "command-output", "aria-live": "polite" });
    var selected = catalog[0];

    function visibleSpecs() {
      var query = search.value.trim().toLowerCase();
      return catalog.filter(function (spec) {
        return !query || (spec.name + " " + spec.group + " " + spec.description).toLowerCase().indexOf(query) >= 0;
      });
    }
    function renderOptions(keep) {
      var specs = visibleSpecs();
      select.textContent = "";
      specs.forEach(function (spec) {
        select.appendChild(h("option", { value: spec.name, text: spec.group + "  /  " + spec.name }));
      });
      var wanted = specs.filter(function (spec) { return spec.name === keep; })[0] || specs[0];
      if (wanted) { select.value = wanted.name; selected = wanted; }
      updateSelected();
    }
    function updateSelected() {
      selected = catalog.filter(function (spec) { return spec.name === select.value; })[0] || catalog[0];
      description.textContent = selected.description;
      badge.textContent = "";
      badge.appendChild(h("span", { class: "chip" }, selected.group));
      badge.appendChild(h("span", { class: "chip " + (selected.runnable ? "good" : "warn") }, selected.runnable ? "Runnable here" : "Terminal only"));
      if (!args.value.trim() && selected.example && selected.example.length) args.value = selected.example.join("\n");
      if (!selected.runnable) {
        args.disabled = true;
      } else {
        args.disabled = false;
      }
    }
    function paintResult(result) {
      output.textContent = "";
      if (!result) {
        output.appendChild(h("p", { class: "muted", text: "Run a command to see its output here." }));
        return;
      }
      output.appendChild(h("div", { class: "command-result-head" },
        h("span", { class: "chip " + (result.ok ? "good" : "crit") }, result.ok ? "Completed" : "Exited " + result.exit_code),
        h("span", { class: "muted small", text: result.elapsed_ms + " ms" }),
        h("button", { class: "btn-link", type: "button", text: "Copy output" })));
      var copy = output.lastChild;
      copy.addEventListener("click", function () {
        var text = [result.stdout, result.stderr].filter(Boolean).join("\n");
        if (navigator.clipboard) navigator.clipboard.writeText(text);
      });
      if (result.stdout) output.appendChild(h("pre", { class: "command-pre" }, h("code", { text: result.stdout })));
      if (result.stderr) output.appendChild(h("pre", { class: "command-pre stderr" }, h("code", { text: result.stderr })));
      if (!result.stdout && !result.stderr) output.appendChild(h("p", { class: "muted", text: "Command completed without output." }));
      if (result.truncated) output.appendChild(h("p", { class: "muted small", text: "Output was capped for browser safety." }));
    }

    search.addEventListener("input", function () { renderOptions(select.value); });
    select.addEventListener("change", function () { args.value = ""; updateSelected(); });
    renderOptions(selected.name);
    paintResult(state.commandResult);

    var run = h("button", { class: "btn", type: "button", text: "Run command" });
    var help = h("button", { class: "btn secondary", type: "button", text: "Load --help" });
    var copy = h("button", { class: "btn secondary", type: "button", text: "Copy terminal command" });
    run.addEventListener("click", function () {
      if (!selected.runnable) return;
      run.disabled = true; run.textContent = "Running…";
      post("/v1/command", { command: selected.name, args: commandArgsFrom(args.value) })
        .then(function (result) { state.commandResult = result; paint(true); })
        .catch(function (error) { state.commandResult = { ok: false, exit_code: 1, stdout: "", stderr: error.message, elapsed_ms: 0 }; paint(true); })
        .then(function () { run.disabled = false; run.textContent = "Run command"; });
    });
    help.addEventListener("click", function () { args.value = "--help"; run.click(); });
    copy.addEventListener("click", function () {
      if (navigator.clipboard) navigator.clipboard.writeText(commandLine(selected, commandArgsFrom(args.value)));
    });

    root.appendChild(h("div", { class: "command-layout" },
      h("section", { class: "card command-panel" },
        h("div", { class: "command-toolbar" }, search, select),
        h("div", { class: "command-title-row" }, h("div", null, h("p", { class: "eyebrow", text: "SELECTED COMMAND" }), h("h2", { class: "mono", text: selected.name })), badge),
        description,
        h("label", { for: "command-args", text: "Arguments · one per line" }),
        args,
        h("div", { class: "toolbar command-actions" }, run, help, copy),
        h("p", { class: "muted small", text: "The gateway store is implicit. Lifecycle commands stay terminal-only so the UI cannot replace its own server." })),
      h("section", { class: "card command-output-card" }, h("div", { class: "command-output-heading" }, h("p", { class: "eyebrow", text: "OUTPUT" }), h("span", { class: "muted small", text: "stdout + stderr" })), output)));
    return root;
  }

  var VIEWS = { overview: viewOverview, memories: viewMemories, review: viewReview, lesson: viewLesson, live: viewLive, fleet: viewFleet, commands: viewCommands, safety: viewSafety };

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

  function editing() {
    var a = document.activeElement;
    return !!a && main.contains(a) && (a.tagName === "TEXTAREA" || a.tagName === "INPUT" && a.type === "text");
  }
  // Repaint only when what is shown changed, and never under someone's cursor: a poll must not
  // throw away a half-typed edit, a selection or the focus.
  function paint(force) {
    var route = currentRoute();
    var sig = route.id + "|" + location.hash + "|" + JSON.stringify([state.status, state.capabilities, state.memories, state.agents, state.events,
      state.lessons, state.lesson, state.commandCatalog, state.commandResult, selected, notice]);
    if (!force && sig === lastPaint) return;
    if (!force && editing()) return;
    lastPaint = sig;
    renderNav();
    document.title = route.title + " · CommonTrace";
    var focusId = document.activeElement && main.contains(document.activeElement) ? document.activeElement.id : "";
    main.textContent = "";
    var view = VIEWS[route.id]();
    if (!view.querySelector(".page-heading")) {
      var title = view.querySelector("h1"), lede = view.querySelector(".lede");
      if (title) {
        var heading = h("div", { class: "page-heading" });
        var copy = h("div", {}, h("p", { class: "eyebrow", text: "MEMORY OPERATIONS" }));
        copy.appendChild(title);
        if (lede) copy.appendChild(lede);
        heading.appendChild(copy); view.insertBefore(heading, view.firstChild);
      }
    }
    main.appendChild(view);
    if (focusId) { var again = document.getElementById(focusId); if (again) again.focus(); }
    if (notice && (route.id === "review" || route.id === "lesson")) {
      var shown = notice; setTimeout(function () { if (notice === shown) { notice = null; } }, 8000);
    }
  }
  function render() { paint(false); }
  function paintHeader() {
    var s = state.status;
    if (s && s.gateway && s.gateway.env) { envChip.hidden = false; envChip.textContent = "Environment: " + s.gateway.env; }
    else envChip.hidden = true;
    var caps = state.capabilities && state.capabilities.capabilities;
    if (caps && tierChip) {
      tierChip.hidden = false;
      tierChip.textContent = "Tier · " + String(caps.tier || "lexical").replace(/_/g, " ");
    } else if (tierChip) tierChip.hidden = true;
  }

  function refresh() {
    if (!token) { main.textContent = ""; main.appendChild(viewAuth()); setConn("bad", "Not connected"); return Promise.resolve(); }
    var route = currentRoute().id;
    var wants = ["status", "capabilities", "memories", "agents"];
    if (route === "live") wants.push("occasions");
    if (route === "review") wants.push("lessons?status=review");
    if (route === "lesson") wants.push("lesson?slug=" + encodeURIComponent(hashQuery("slug")));
    if (route === "commands") wants.push("command-catalog");
    var keys = {
      capabilities: "capabilities", occasions: "events", "lessons?status=review": "lessons",
      "command-catalog": "commandCatalog"
    };
    return Promise.all(wants.map(function (w) { return api("/v1/" + w).then(function (d) { return [w, d]; }); }))
      .then(function (pairs) {
        pairs.forEach(function (p) { state[keys[p[0]] || (p[0].indexOf("lesson?") === 0 ? "lesson" : p[0])] = p[1]; });
        lastOk = Date.now();
        setConn("ok", "Live · updated " + ago(new Date(lastOk).toISOString()));
        paintHeader();
        render();
      })
      .catch(function (e) {
        if (e && e.auth) { token = ""; try { sessionStorage.removeItem("ct-token"); } catch (x) { /* ignore */ } main.textContent = ""; main.appendChild(viewAuth("That token was not accepted.")); setConn("bad", "Not connected"); return; }
        if (e && !e.auth) console.error(e);
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
  if (commandLaunch) commandLaunch.addEventListener("click", function () { location.hash = "#/commands"; });
  document.addEventListener("keydown", function (event) {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
      event.preventDefault(); location.hash = "#/commands";
    }
  });

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
