"""Self-contained interactive graph viz export (offline, no CDN)."""
from __future__ import annotations

import html
import json
import math
from typing import Any

from commontrace import graph as _graph_mod
from commontrace import lesson_cache as _lesson_cache

ENTITY_COLORS: dict[str, str] = {
    "service": "#4e79a7",
    "tool": "#f28e2b",
    "error": "#e15759",
    "concept": "#76b7b2",
    "lesson": "#59a14f",
    "scope": "#edc948",
    "user": "#b07aa1",
    "file": "#ff9da7",
    "memory": "#9c755f",
    "document": "#bab0ac",
}
DEFAULT_COLOR = "#888888"

LABELED_RELATIONS = ("updates", "extends", "derives", "supersedes")


def _color_for(entity_type: str) -> str:
    return ENTITY_COLORS.get(str(entity_type or "").strip().lower(), DEFAULT_COLOR)


def _esc(text: Any) -> str:
    return html.escape(str(text if text is not None else ""), quote=True)


def _node_field(node: Any, name: str, default: Any = None) -> Any:
    if isinstance(node, dict):
        return node.get(name, default)
    return getattr(node, name, default)


def _edge_field(edge: Any, name: str, default: Any = None) -> Any:
    if isinstance(edge, dict):
        if name == "valid_at" and "valid_at" not in edge and "valid_from" in edge:
            return edge.get("valid_from", default)
        if name == "valid_from" and "valid_from" not in edge and "valid_at" in edge:
            return edge.get("valid_at", default)
        return edge.get(name, default)
    if name == "valid_at" and not hasattr(edge, "valid_at") and hasattr(edge, "valid_from"):
        return getattr(edge, "valid_from", default)
    if name == "valid_from" and not hasattr(edge, "valid_from") and hasattr(edge, "valid_at"):
        return getattr(edge, "valid_at", default)
    return getattr(edge, name, default)


def _chain_key(node: Any) -> str:
    root_id = _node_field(node, "root_id", None)
    nid = str(_node_field(node, "id", ""))
    if root_id:
        return str(root_id)
    return nid


def _node_is_superseded(node: Any) -> bool:
    is_latest = _node_field(node, "is_latest", True)
    return is_latest is False


def _node_is_deleted(node: Any) -> bool:
    if bool(_node_field(node, "is_forgotten", False)) is True:
        return True
    props = _node_field(node, "properties", {}) or {}
    if isinstance(props, dict):
        for k in ("deleted", "is_deleted", "forgotten"):
            if props.get(k) is True:
                return True
        if str(props.get("status", "")).strip().lower() == "deleted":
            return True
    return False


def _edge_is_inactive(edge: Any, moment: Any) -> bool:
    try:
        if not isinstance(edge, dict) and hasattr(edge, "valid_at"):
            return not _graph_mod._is_active_edge(edge, moment)
    except Exception:
        pass
    valid_raw = _edge_field(edge, "valid_at", None)
    if valid_raw is None:
        valid_raw = _edge_field(edge, "valid_from", None)
    invalid_raw = _edge_field(edge, "invalid_at", None)
    expired_raw = _edge_field(edge, "expired_at", None)
    if moment is None:
        if invalid_raw is not None:
            return True
        if expired_raw is not None:
            try:
                exp = _lesson_cache.parse_moment(expired_raw)
                now = _lesson_cache.parse_moment(_graph_mod._now())
                if exp <= now:
                    return True
            except Exception:
                pass
        return False
    try:
        if valid_raw:
            if _lesson_cache.parse_moment(valid_raw) > moment:
                return True
        if invalid_raw:
            if _lesson_cache.parse_moment(invalid_raw) <= moment:
                return True
        if expired_raw:
            if _lesson_cache.parse_moment(expired_raw) <= moment:
                return True
    except Exception:
        pass
    return False


def render_html(root: str, as_of: str | None = None) -> str:
    """Render the graph at ``root`` as a self-contained HTML string."""
    try:
        nodes_map = _graph_mod.load_nodes(root)
    except Exception:
        nodes_map = {}
    try:
        all_edges = _graph_mod.load_edges(root)
    except Exception:
        all_edges = []

    if isinstance(nodes_map, dict):
        node_list = list(nodes_map.values())
    elif isinstance(nodes_map, list):
        node_list = list(nodes_map)
    else:
        node_list = []
    if not isinstance(all_edges, list):
        all_edges = []

    moment = _lesson_cache.parse_moment(as_of) if as_of else None

    active_edges: list[Any] = []
    inactive_edges: list[Any] = []
    for e in all_edges:
        if _edge_is_inactive(e, moment):
            inactive_edges.append(e)
        else:
            active_edges.append(e)

    shown_edges = active_edges if moment is not None else (active_edges + inactive_edges)

    def _node_sort(n: Any) -> str:
        return str(_node_field(n, "id", ""))

    def _edge_sort(e: Any) -> tuple:
        return (
            str(_edge_field(e, "source", "")),
            str(_edge_field(e, "target", "")),
            str(_edge_field(e, "relation", "")),
        )

    node_list = sorted(node_list, key=_node_sort)
    shown_edges = sorted(shown_edges, key=_edge_sort)

    chains: dict[str, list[Any]] = {}
    for n in node_list:
        chains.setdefault(_chain_key(n), []).append(n)
    for members in chains.values():
        try:
            members.sort(key=lambda n: int(_node_field(n, "version", 1) or 1))
        except Exception:
            pass

    n_superseded = sum(1 for n in node_list if _node_is_superseded(n))
    n_deleted = sum(1 for n in node_list if _node_is_deleted(n))

    W, H = 800, 600
    pos: list[tuple[float, float]] = []
    n_count = max(1, len(node_list))
    for i in range(len(node_list)):
        a = (2 * math.pi * i) / n_count
        pos.append((W / 2 + 220 * math.cos(a), H / 2 + 220 * math.sin(a)))

    svg_nodes: list[str] = []
    for i, n in enumerate(node_list):
        nid = str(_node_field(n, "id", ""))
        etype = str(_node_field(n, "entity_type", "concept"))
        name = str(_node_field(n, "name", nid))
        color = _color_for(etype)
        classes = ["node"]
        if _node_is_superseded(n):
            classes.append("superseded")
            classes.append("dimmed")
        if _node_is_deleted(n):
            classes.append("deleted")
            classes.append("dimmed")
        cls = " ".join(classes)
        nx, ny = pos[i]
        svg_nodes.append(
            f'<g class="{cls}" data-id="{_esc(nid)}" data-idx="{i}">'
            f'<circle cx="{nx:.1f}" cy="{ny:.1f}" r="14" fill="{_esc(color)}" '
            f'stroke="#222" stroke-width="1.5" data-id="{_esc(nid)}"></circle>'
            f'<text class="node-label" x="{nx:.1f}" y="{ny + 28:.1f}" text-anchor="middle">{_esc(name)}</text>'
            f"</g>"
        )

    id_to_idx = {str(_node_field(n, "id", "")): i for i, n in enumerate(node_list)}
    svg_edges: list[str] = []
    for e in shown_edges:
        src = str(_edge_field(e, "source", ""))
        dst = str(_edge_field(e, "target", ""))
        rel = str(_edge_field(e, "relation", "relates_to"))
        inactive = e in inactive_edges
        classes = ["edge"]
        if inactive:
            classes.append("superseded")
            classes.append("dimmed")
        cls = " ".join(classes)
        si = id_to_idx.get(src, -1)
        ti = id_to_idx.get(dst, -1)
        if si >= 0 and ti >= 0:
            x1, y1 = pos[si]
            x2, y2 = pos[ti]
            mx, my = (x1 + x2) / 2, (y1 + y2) / 2
        else:
            x1, y1, x2, y2, mx, my = 0.0, 0.0, 0.0, 0.0, 0.0, 0.0

        svg_edges.append(
            f'<g class="{cls}" data-source="{_esc(src)}" data-target="{_esc(dst)}" '
            f'data-relation="{_esc(rel)}" data-active="{str(not inactive).lower()}">'
            f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
            f'stroke="#999" stroke-width="1.5" data-si="{si}" data-ti="{ti}"></line>'
            f'<text class="edge-label" x="{mx:.1f}" y="{my:.1f}">{_esc(rel)}</text>'
            f"</g>"
        )

    present_types = sorted({str(_node_field(n, "entity_type", "concept")) for n in node_list})
    if not present_types:
        present_types = ["concept"]
    legend_items = "".join(
        f'<span class="legend-item"><span class="swatch" '
        f'style="background:{_esc(_color_for(t))}"></span>{_esc(t)}</span>'
        for t in present_types
    )
    vocab_items = "".join(f"<code>{_esc(r)}</code>" for r in LABELED_RELATIONS)

    chain_blocks: list[str] = []
    for key in sorted(chains):
        members = chains[key]
        lis = "".join(
            f"<li>{_esc(str(_node_field(m, 'id', '')))} (v{_esc(str(_node_field(m, 'version', 1)))})"
            + (" <em>superseded</em>" if _node_is_superseded(m) else "")
            + (" <em>deleted</em>" if _node_is_deleted(m) else "")
            + "</li>"
            for m in members
        )
        chain_blocks.append(
            f'<div class="chain" data-root="{_esc(key)}"><strong>{_esc(key)}</strong><ul>{lis}</ul></div>'
        )
    chains_html = "\n".join(chain_blocks) if chain_blocks else "<p>No version chains.</p>"

    js_nodes = []
    for n in node_list:
        js_nodes.append(
            {
                "id": str(_node_field(n, "id", "")),
                "entity_type": str(_node_field(n, "entity_type", "concept")),
                "name": str(_node_field(n, "name", "")),
                "properties": _node_field(n, "properties", {}) or {},
                "version": _node_field(n, "version", 1),
                "is_latest": bool(_node_field(n, "is_latest", True)),
                "is_forgotten": bool(_node_field(n, "is_forgotten", False)),
                "parent_id": _node_field(n, "parent_id", None),
                "root_id": _node_field(n, "root_id", None),
                "color": _color_for(str(_node_field(n, "entity_type", "concept"))),
                "superseded": _node_is_superseded(n),
                "deleted": _node_is_deleted(n),
                "chain": _chain_key(n),
            }
        )
    js_edges = []
    for e in shown_edges:
        js_edges.append(
            {
                "source": str(_edge_field(e, "source", "")),
                "target": str(_edge_field(e, "target", "")),
                "relation": str(_edge_field(e, "relation", "")),
                "weight": _edge_field(e, "weight", 1.0),
                "valid_at": _edge_field(e, "valid_at", None),
                "valid_from": _edge_field(e, "valid_from", None),
                "invalid_at": _edge_field(e, "invalid_at", None),
                "expired_at": _edge_field(e, "expired_at", None),
                "properties": _edge_field(e, "properties", {}) or {},
                "active": e not in inactive_edges,
            }
        )
    data_json = json.dumps({"nodes": js_nodes, "edges": js_edges})
    data_json = data_json.replace("</", "<\\/")

    counts_text = (
        f"{len(node_list)} nodes, {len(shown_edges)} edges "
        f"({len(active_edges)} active, {len(inactive_edges)} superseded), "
        f"{len(chains)} chains, {n_superseded} superseded, {n_deleted} deleted"
    )

    as_of_label = _esc(as_of) if as_of else "now"
    static_svg = "".join(svg_edges) + "".join(svg_nodes)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CommonTrace Graph</title>
<style>
body {{ font-family: sans-serif; margin: 0; padding: 16px; background: #fafafa; color: #222; }}
header {{ margin-bottom: 12px; }}
#counts {{ margin: 8px 0; font-size: 14px; }}
#legend {{ margin: 8px 0; font-size: 13px; }}
.legend-item {{ margin-right: 12px; }}
.swatch {{ display: inline-block; width: 12px; height: 12px; border-radius: 3px; margin-right: 4px; vertical-align:
    baseline; border: 1px solid #333; }}
#vocab {{ margin: 8px 0; font-size: 13px; }}
#vocab code {{ background: #eee; padding: 1px 5px; margin-right: 6px; border-radius: 3px; }}
#layout {{ display: flex; gap: 16px; }}
#graph {{ border: 1px solid #ccc; background: #fff; flex: 1 1 auto; }}
#inspect {{ width: 300px; border: 1px solid #ccc; background: #fff; padding: 8px; font-size: 13px; white-space:
    pre-wrap; }}
.node {{ cursor: pointer; }}
.node-label {{ font-size: 10px; fill: #333; }}
.edge-label {{ font-size: 9px; fill: #555; }}
.dimmed {{ opacity: 0.35; }}
.superseded circle {{ stroke-dasharray: 4 2; }}
#chains {{ margin-top: 12px; }}
.chain {{ border: 1px solid #ddd; background: #fff; padding: 6px 10px; margin: 6px 0; }}
</style>
</head>
<body>
<header><h1>CommonTrace Graph</h1><div>as_of: {as_of_label}</div></header>
<div id="counts">{_esc(counts_text)}</div>
<div id="legend">Legend: {legend_items}</div>
<div id="vocab">Edge vocabulary: {vocab_items} relates_to depends_on causes resolves affects scoped_to uses
    violates</div>
<div id="layout">
<svg id="graph" width="800" height="600" role="img" aria-label="knowledge graph">{static_svg}</svg>
<div id="inspect"><h3>Inspect</h3><p>Click a node to inspect its properties.</p><div id="props"></div></div>
</div>
<h2>Version chains grouped by entity</h2>
<div id="chains">{chains_html}</div>
<script type="application/json" id="graph-data">{data_json}</script>
<script>
(function() {{
  var data = JSON.parse(document.getElementById('graph-data').textContent);
  var svg = document.getElementById('graph');
  var NS = 'http://www.w3.org/2000/svg';
  var W = 800, H = 600;
  var nodes = data.nodes.map(function(n, i) {{
    var a = (2 * Math.PI * i) / Math.max(1, data.nodes.length);
    return {{ id: n.id, x: W/2 + 220*Math.cos(a), y: H/2 + 220*Math.sin(a), vx: 0, vy: 0, ref: n }};
  }});
  var byId = {{}};
  nodes.forEach(function(n) {{ byId[n.id] = n; }});
  var edges = data.edges.map(function(e) {{ return {{ s: byId[e.source], t: byId[e.target], rel: e.relation }}; }})
    .filter(function(e) {{ return e.s && e.t; }});
  // render edges
  var edgeLayer = document.createElementNS(NS, 'g');
  var edgeLines = [];
  var edgeLabels = [];
  edges.forEach(function(e) {{
    var line = document.createElementNS(NS, 'line');
    line.setAttribute('stroke', '#999');
    line.setAttribute('stroke-width', '1.5');
    edgeLayer.appendChild(line);
    edgeLines.push(line);
    var lab = document.createElementNS(NS, 'text');
    lab.setAttribute('class', 'edge-label');
    lab.textContent = e.rel;
    edgeLayer.appendChild(lab);
    edgeLabels.push(lab);
  }});
  svg.appendChild(edgeLayer);
  var nodeLayer = document.createElementNS(NS, 'g');
  var nodeEls = [];
  nodes.forEach(function(n, i) {{
    var g = document.createElementNS(NS, 'g');
    g.setAttribute('class', 'node' + (n.ref.superseded ? ' superseded dimmed' : '') + (n.ref.deleted ? ' deleted
        dimmed' : ''));
    var c = document.createElementNS(NS, 'circle');
    c.setAttribute('r', '14');
    c.setAttribute('fill', n.ref.color);
    c.setAttribute('stroke', '#222');
    c.setAttribute('cx', n.x);
    c.setAttribute('cy', n.y);
    c.style.cursor = 'pointer';
    c.addEventListener('click', function() {{
      var box = document.getElementById('props');
      box.textContent = JSON.stringify(n.ref, null, 2);
    }});
    g.appendChild(c);
    var t = document.createElementNS(NS, 'text');
    t.setAttribute('class', 'node-label');
    t.setAttribute('text-anchor', 'middle');
    t.textContent = n.ref.name || n.id;
    g.appendChild(t);
    nodeLayer.appendChild(g);
    nodeEls.push({{ g: g, c: c, t: t }});
    // drag
    (function(pt) {{
      var dragging = false;
      c.addEventListener('mousedown', function() {{ dragging = true; pt.vx = 0; pt.vy = 0; }});
      window.addEventListener('mouseup', function() {{ dragging = false; }});
      svg.addEventListener('mousemove', function(ev) {{
        if (!dragging) return;
        var r = svg.getBoundingClientRect();
        pt.x = ev.clientX - r.left;
        pt.y = ev.clientY - r.top;
      }});
    }})(n);
  }});
  svg.appendChild(nodeLayer);
  function tick() {{
    // repulsion
    for (var i = 0; i < nodes.length; i++) {{
      for (var j = i + 1; j < nodes.length; j++) {{
        var a = nodes[i], b = nodes[j];
        var dx = a.x - b.x, dy = a.y - b.y;
        var d2 = dx*dx + dy*dy + 0.01;
        var f = 4000 / d2;
        var d = Math.sqrt(d2);
        var fx = (dx / d) * f, fy = (dy / d) * f;
        a.vx += fx * 0.02; a.vy += fy * 0.02;
        b.vx -= fx * 0.02; b.vy -= fy * 0.02;
      }}
    }}
    // springs
    edges.forEach(function(e) {{
      var dx = e.t.x - e.s.x, dy = e.t.y - e.s.y;
      var d = Math.sqrt(dx*dx + dy*dy) || 1;
      var want = 140;
      var f = (d - want) * 0.02;
      var fx = (dx / d) * f, fy = (dy / d) * f;
      e.s.vx += fx; e.s.vy += fy;
      e.t.vx -= fx; e.t.vy -= fy;
    }});
    // center + integrate
    nodes.forEach(function(n) {{
      n.vx += (W/2 - n.x) * 0.002;
      n.vy += (H/2 - n.y) * 0.002;
      n.vx *= 0.9; n.vy *= 0.9;
      n.x += n.vx; n.y += n.vy;
      n.x = Math.max(20, Math.min(W - 20, n.x));
      n.y = Math.max(20, Math.min(H - 20, n.y));
    }});
    // write positions
    nodes.forEach(function(n, i) {{
      nodeEls[i].c.setAttribute('cx', n.x.toFixed(1));
      nodeEls[i].c.setAttribute('cy', n.y.toFixed(1));
      nodeEls[i].t.setAttribute('x', n.x.toFixed(1));
      nodeEls[i].t.setAttribute('y', (n.y + 28).toFixed(1));
    }});
    edges.forEach(function(e, i) {{
      edgeLines[i].setAttribute('x1', e.s.x.toFixed(1));
      edgeLines[i].setAttribute('y1', e.s.y.toFixed(1));
      edgeLines[i].setAttribute('x2', e.t.x.toFixed(1));
      edgeLines[i].setAttribute('y2', e.t.y.toFixed(1));
      edgeLabels[i].setAttribute('x', ((e.s.x + e.t.x) / 2).toFixed(1));
      edgeLabels[i].setAttribute('y', ((e.s.y + e.t.y) / 2).toFixed(1));
    }});
  }}
  var steps = 0;
  var timer = setInterval(function() {{ tick(); if (++steps > 400) clearInterval(timer); }}, 16);
  // static fallback: run some steps synchronously so SVG has positions offline
  for (var k = 0; k < 60; k++) tick();
  nodes.forEach(function(n, i) {{
    nodeEls[i].c.setAttribute('cx', n.x.toFixed(1));
    nodeEls[i].c.setAttribute('cy', n.y.toFixed(1));
  }});
}})();
</script>
</body>
</html>"""
