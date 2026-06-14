# `/justdoit` — skill Claude Code

Pattern de **double-review A+B par sous-agents** avec loop jusqu'à conformité
(max 3 itérations, puis arbitrage de l'orchestrateur), augmenté d'un mécanisme
d'**apprentissage long-terme** (mémoire de leçons + épisodes).

Délègue une tâche à un sous-agent **A (implementer)** — commit immédiat — puis à un
sous-agent **B (reviewer indépendant)**, et itère si écart. Idéal pour code
architectural, refactor lourd, ports CUDA/GPU, et fixes critiques où une review
indépendante apporte de la valeur.

## Installation

Copier ce dossier dans `~/.claude/skills/justdoit/`, puis invoquer :

```
/justdoit <description tâche + critères de succès inline>
```

## Contenu

| Chemin | Rôle |
|---|---|
| `SKILL.md` | Spécification complète du skill (pipeline, phases, sous-agents). |
| `assets/` | Diagrammes du pipeline et des sous-agents (`.dot` + `.png`). |
| `benchmark/measure_performance.py` | Script de mesure de performance du mécanisme mémoire. |
| `memory/` | Scaffold de la base mémoire long-terme (vide au départ). |
| `memory/INDEX.md` | Index hiérarchique des leçons/épisodes (rempli au fil des runs). |
| `memory/lessons/` · `memory/episodes/` | Leçons capitalisées et épisodes (templates + README). |
| `memory/attention/` | Couche d'attention sémantique (embeddings locaux) — pré-filtre du retrieval. |

## Architecture

Sous-agents : **A** (implementer), **B** (reviewer), **Alpha** (retrieval mémoire,
Phase 0), **Omega** (synthèse + propositions de leçons, Phase 10), **Lambda**
(validation indépendante du backlog mémoire, Phase 11). Voir `SKILL.md` et
`assets/justdoit_overall.png`.

> Base mémoire livrée vide : les leçons et épisodes s'accumulent localement au fil
> de vos propres runs `/justdoit`.
