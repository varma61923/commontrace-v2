#!/usr/bin/env python3
"""measure_performance.py — benchmark de la base mémoire /justdoit.

Mesure les 3 axes manquants identifiés dans l'état de l'art (Park 2023 Generative
Agents, Evo-Memory, AgentErrorBench, ERL, LongMemEval, LoCoMo) :

- lesson_quality : % de propositions Omega validées par Lambda (qualité génération)
- implicit_retrieval : % de lessons retrieve par Alpha effectivement hit (qualité retrieval)
- transfer_gap : % de hits cross-project (transfert hors-projet d'origine)

Usage :
    python measure_performance.py                  # markdown stdout, tous épisodes
    python measure_performance.py --n=5            # derniers 5 épisodes
    python measure_performance.py --html           # HTML dans memory/benchmark_reports/
    python measure_performance.py --json           # JSON brut stdout

Si PyYAML manquant : tomber sur le fallback regex (parsing simplifié).
Idéalement lancer via venv : python3 ...
"""
import argparse
import datetime
import glob
import json
import os
import re
import sys

try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False


BASE_DIR = os.path.expanduser("~/.claude/skills/justdoit/memory")


def parse_frontmatter(content):
    if not content.startswith("---"):
        return None
    parts = content.split("---", 2)
    if len(parts) < 3:
        return None
    fm_text = parts[1]
    if HAS_YAML:
        try:
            return yaml.safe_load(fm_text)
        except yaml.YAMLError:
            return parse_yaml_minimal(fm_text)
    return parse_yaml_minimal(fm_text)


def parse_yaml_minimal(text):
    """Fallback regex-based parser for our frontmatter format."""
    data = {}
    for line in text.splitlines():
        line = line.rstrip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^([A-Za-z_]\w*):\s*(.*)$", line)
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip()
        if val.startswith('"') and val.endswith('"'):
            val = val[1:-1]
        elif val.startswith("'") and val.endswith("'"):
            val = val[1:-1]
        if val.startswith("[") and val.endswith("]"):
            inner = val[1:-1].strip()
            val = [x.strip().strip('"').strip("'") for x in inner.split(",")] if inner else []
        elif val.lstrip("-").isdigit():
            val = int(val)
        data[key] = val
    return data


def load_episodes(n=None):
    paths = sorted(glob.glob(os.path.join(BASE_DIR, "episodes", "2*.md")))
    if n is not None and n > 0:
        paths = paths[-n:]
    episodes = []
    for p in paths:
        with open(p) as fh:
            fm = parse_frontmatter(fh.read())
        if fm:
            fm["_path"] = p
            episodes.append(fm)
    return episodes


def load_lessons():
    paths = sorted(glob.glob(os.path.join(BASE_DIR, "lessons", "lesson_*.md")))
    lessons = {}
    for p in paths:
        name = os.path.basename(p).replace(".md", "")
        if name.endswith("_template"):
            continue
        with open(p) as fh:
            fm = parse_frontmatter(fh.read())
        if fm:
            fm["_path"] = p
            lessons[name] = fm
    return lessons


def get_validated(ep):
    """Get validated lessons, falling back to legacy field name for back-compat."""
    return ep.get("lessons_validated_by_lambda") or ep.get("lessons_validated_by_user") or []


def compute_lesson_quality(episodes):
    """Mean over episodes of (n_validated / n_proposed). Exclude episodes with no proposal."""
    ratios = []
    for ep in episodes:
        proposed = ep.get("lessons_proposed_by_omega") or []
        if not proposed:
            continue
        validated = get_validated(ep)
        ratios.append(len(validated) / len(proposed))
    if not ratios:
        return None, 0
    return sum(ratios) / len(ratios), len(ratios)


def compute_implicit_retrieval(episodes):
    """Two angles on retrieval quality:

    - strict     = mean(|hit ∩ retrieved| / |retrieved|)  → précision du retrieval Alpha
                   (proportion des sélections Alpha qui ont effectivement servi)
    - permissive = mean(|hit| / |retrieved|)               → richesse de l'application Omega
                   (peut excéder 100% si Omega compte des lessons influentes hors retrieved
                    Alpha — counter-examples, règles méthodologiques en arrière-plan, etc.)

    Exclude episodes with empty retrieval. Returns (strict, permissive, n_valid).
    """
    strict_ratios = []
    permissive_ratios = []
    for ep in episodes:
        retrieved = set(ep.get("lessons_retrieved_by_alpha") or [])
        if not retrieved:
            continue
        hit = set(ep.get("lessons_hit") or [])
        strict_ratios.append(len(hit & retrieved) / len(retrieved))
        permissive_ratios.append(len(hit) / len(retrieved))
    if not strict_ratios:
        return None, None, 0
    return (
        sum(strict_ratios) / len(strict_ratios),
        sum(permissive_ratios) / len(permissive_ratios),
        len(strict_ratios),
    )


def compute_transfer_gap(episodes, lessons):
    """% of hits whose source_episodes are from a different project than current."""
    episode_project = {ep["name"]: ep.get("project") for ep in episodes}

    def resolve_project(slug):
        if slug in episode_project:
            return episode_project[slug]
        path = os.path.join(BASE_DIR, "episodes", f"{slug}.md")
        if os.path.exists(path):
            with open(path) as fh:
                fm = parse_frontmatter(fh.read())
            return (fm or {}).get("project")
        return None

    total_hits = 0
    cross_hits = 0
    untraceable = 0
    for ep in episodes:
        current = ep.get("project")
        for hit_slug in ep.get("lessons_hit") or []:
            lesson = lessons.get(hit_slug)
            if not lesson:
                continue
            src_episodes = lesson.get("source_episodes") or []
            if not src_episodes:
                untraceable += 1
                continue
            src_projects = {resolve_project(s) for s in src_episodes}
            src_projects.discard(None)
            if not src_projects:
                untraceable += 1
                continue
            total_hits += 1
            if current not in src_projects:
                cross_hits += 1
    if total_hits == 0:
        return None, 0, untraceable
    return cross_hits / total_hits, total_hits, untraceable


def compute_extras(episodes, lessons):
    by_uses = sorted(lessons.items(), key=lambda kv: kv[1].get("uses", 0), reverse=True)
    top5 = [(n, l.get("uses", 0)) for n, l in by_uses[:5] if l.get("uses", 0) > 0]
    never_hit = sorted(n for n, l in lessons.items() if l.get("uses", 0) == 0)

    all_proposed = set()
    all_validated = set()
    for ep in episodes:
        all_proposed |= set(ep.get("lessons_proposed_by_omega") or [])
        all_validated |= set(get_validated(ep))
    proposed_not_validated = sorted(all_proposed - all_validated)

    # Distribution importance
    imp_lessons = {}
    for l in lessons.values():
        i = l.get("importance")
        imp_lessons[i] = imp_lessons.get(i, 0) + 1
    imp_episodes = {}
    for ep in episodes:
        i = ep.get("importance")
        imp_episodes[i] = imp_episodes.get(i, 0) + 1

    # Coverage par domain
    domain_coverage = {}
    for l in lessons.values():
        d = l.get("domain", "?")
        domain_coverage[d] = domain_coverage.get(d, 0) + 1

    return {
        "top5": top5,
        "never_hit": never_hit,
        "proposed_not_validated": proposed_not_validated,
        "importance_lessons": imp_lessons,
        "importance_episodes": imp_episodes,
        "domain_coverage": domain_coverage,
    }


def fmt_pct(v):
    return "N/A" if v is None else f"{v:.1%}"


def render_markdown(r):
    out = []
    out.append("# /justdoit Memory Benchmark Report")
    out.append("")
    out.append(f"**Date** : {r['timestamp']}")
    out.append(f"**Épisodes analysés** : {r['n_episodes']}")
    out.append(f"**Lessons en base** : {r['n_lessons']}")
    out.append(f"**YAML parser** : {'PyYAML' if HAS_YAML else 'regex fallback (PyYAML manquant)'}")
    out.append("")

    out.append("## Métriques principales (3 axes)")
    out.append("")
    out.append("### lesson_quality")
    out.append("% de leçons proposées par Omega validées par Lambda.")
    lq = r["lesson_quality"]
    if lq["value"] is None:
        out.append("→ **N/A** (aucun épisode avec proposition Omega)")
    else:
        out.append(f"→ **{fmt_pct(lq['value'])}** sur {lq['n']} épisodes valides")
    out.append("")

    out.append("### implicit_retrieval (2 angles)")
    out.append("Précision et richesse du retrieval Alpha. `hit` n'est pas borné par `retrieved` —")
    out.append("voir doc sémantique (counter-examples / règles en arrière-plan peuvent compter en hit).")
    ir = r["implicit_retrieval"]
    if ir["strict"] is None:
        out.append("→ **N/A** (aucun épisode avec retrieval Alpha non vide)")
    else:
        out.append(f"- **strict** = mean(|hit ∩ retrieved| / |retrieved|) → **{fmt_pct(ir['strict'])}**")
        out.append(f"  (précision : proportion des sélections Alpha qui ont effectivement servi)")
        out.append(f"- **permissive** = mean(|hit| / |retrieved|) → **{fmt_pct(ir['permissive'])}**")
        out.append(f"  (richesse : peut > 100% si Omega compte des hits hors retrieved Alpha)")
        out.append(f"- sur **{ir['n']}** épisodes valides")
    out.append("")

    out.append("### transfer_gap")
    out.append("% de hits cross-project (transfert hors-projet d'origine).")
    tg = r["transfer_gap"]
    if tg["value"] is None:
        msg = "→ **N/A**"
        if tg["untraceable"] > 0:
            msg += f" ({tg['untraceable']} hits untraceable — lessons seedées sans source_episode)"
        msg += ". Base mono-projet ou hits non traceables — seeder avec épisodes multi-projets pour mesurer."
        out.append(msg)
    else:
        out.append(f"→ **{fmt_pct(tg['value'])}** sur {tg['n']} hits traceables (+ {tg['untraceable']} untraceable)")
    out.append("")

    out.append("## Détail par épisode")
    out.append("")
    out.append("| Date / slug | Verdict | Imp | Retrieved | Hit | Proposed | Validated | Project |")
    out.append("|---|---|---|---|---|---|---|---|")
    for ep in r["episodes"]:
        out.append(
            f"| `{ep['name']}` | {ep.get('verdict', '?')} | "
            f"{ep.get('importance', '?')} | "
            f"{len(ep.get('lessons_retrieved_by_alpha') or [])} | "
            f"{len(ep.get('lessons_hit') or [])} | "
            f"{len(ep.get('lessons_proposed_by_omega') or [])} | "
            f"{len(get_validated(ep))} | "
            f"{ep.get('project', '?')} |"
        )
    out.append("")

    extras = r["extras"]

    out.append("## Top 5 lessons par uses")
    out.append("")
    if extras["top5"]:
        for n, u in extras["top5"]:
            out.append(f"- `{n}` : {u} uses")
    else:
        out.append("*(aucune lesson avec uses > 0)*")
    out.append("")

    out.append("## Lessons jamais hit (candidates archivage à terme)")
    out.append("")
    if extras["never_hit"]:
        for n in extras["never_hit"]:
            out.append(f"- `{n}`")
    else:
        out.append("*(toutes les lessons ont été hit au moins une fois)*")
    out.append("")

    out.append("## Lessons proposées mais jamais validées (signal qualité Omega dégradée)")
    out.append("")
    if extras["proposed_not_validated"]:
        for n in extras["proposed_not_validated"]:
            out.append(f"- `{n}`")
    else:
        out.append("*(toutes les propositions Omega ont été validées)*")
    out.append("")

    out.append("## Distribution importance")
    out.append("")
    out.append("**Lessons** :")
    for i in sorted(extras["importance_lessons"].keys(), key=lambda x: (x is None, x)):
        out.append(f"- importance {i} : {extras['importance_lessons'][i]} lessons")
    out.append("")
    out.append("**Episodes** :")
    for i in sorted(extras["importance_episodes"].keys(), key=lambda x: (x is None, x)):
        out.append(f"- importance {i} : {extras['importance_episodes'][i]} épisodes")
    out.append("")

    out.append("## Couverture par domain")
    out.append("")
    for d in sorted(extras["domain_coverage"].keys()):
        out.append(f"- {d} : {extras['domain_coverage'][d]} lessons")
    out.append("")

    return "\n".join(out)


def render_html(md_content, timestamp):
    return f"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<title>/justdoit Memory Benchmark — {timestamp}</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; max-width: 960px; margin: 2em auto; padding: 0 1.5em; line-height: 1.6; color: #2d2d2d; }}
h1, h2, h3 {{ color: #1a1a1a; }}
h1 {{ border-bottom: 2px solid #444; padding-bottom: 0.3em; }}
h2 {{ border-bottom: 1px solid #ccc; padding-bottom: 0.2em; margin-top: 2em; }}
h3 {{ margin-top: 1.5em; color: #444; }}
code {{ background: #f4f4f4; padding: 2px 6px; border-radius: 3px; font-size: 0.9em; font-family: "SF Mono", Menlo, Consolas, monospace; }}
table {{ border-collapse: collapse; margin: 1em 0; width: 100%; }}
th, td {{ border: 1px solid #ccc; padding: 8px 12px; text-align: left; }}
th {{ background: #f0f0f0; }}
strong {{ color: #0066cc; }}
em {{ color: #888; font-style: italic; }}
ul {{ padding-left: 1.5em; }}
li {{ margin-bottom: 0.3em; }}
pre {{ white-space: pre-wrap; font-family: inherit; }}
</style>
</head>
<body>
<pre>{md_content}</pre>
</body>
</html>"""


def main():
    parser = argparse.ArgumentParser(
        description="Benchmark de la base mémoire /justdoit (lesson_quality, implicit_retrieval, transfer_gap)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Usage :")[1] if "Usage :" in __doc__ else "",
    )
    parser.add_argument("--n", type=int, default=0, help="Nombre d'épisodes récents (default: tous)")
    parser.add_argument("--html", action="store_true", help="Output HTML dans memory/benchmark_reports/")
    parser.add_argument("--json", action="store_true", help="Output JSON brut stdout")
    args = parser.parse_args()

    episodes = load_episodes(args.n if args.n > 0 else None)
    lessons = load_lessons()

    if not episodes:
        print("Pas assez d'épisodes pour calculer, exécutez /justdoit N fois d'abord.")
        sys.exit(0)

    lq_value, lq_n = compute_lesson_quality(episodes)
    ir_strict, ir_permissive, ir_n = compute_implicit_retrieval(episodes)
    tg_value, tg_n, tg_untraceable = compute_transfer_gap(episodes, lessons)
    extras = compute_extras(episodes, lessons)

    report = {
        "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
        "n_episodes": len(episodes),
        "n_lessons": len(lessons),
        "lesson_quality": {"value": lq_value, "n": lq_n},
        "implicit_retrieval": {"strict": ir_strict, "permissive": ir_permissive, "n": ir_n},
        "transfer_gap": {"value": tg_value, "n": tg_n, "untraceable": tg_untraceable},
        "episodes": episodes,
        "extras": extras,
    }

    if args.json:
        clean = dict(report)
        clean["episodes"] = [{k: v for k, v in ep.items() if not k.startswith("_")} for ep in episodes]
        print(json.dumps(clean, indent=2, default=str))
    elif args.html:
        md = render_markdown(report)
        html = render_html(md, report["timestamp"])
        out_dir = os.path.join(BASE_DIR, "benchmark_reports")
        os.makedirs(out_dir, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")
        out_path = os.path.join(out_dir, f"{ts}.html")
        with open(out_path, "w") as fh:
            fh.write(html)
        print(f"Rapport HTML écrit : {out_path}")
    else:
        print(render_markdown(report))


if __name__ == "__main__":
    main()
