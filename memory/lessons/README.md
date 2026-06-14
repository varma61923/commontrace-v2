# `memory/lessons/` — Leçons capitalisées entre runs `/justdoit`

## Rôle

Une leçon `lesson_<slug>.md` représente une règle apprise d'au moins un épisode passé, formulée en langage naturel pour pouvoir être :
1. **Retrouvée** par le sous-agent **Alpha** (Phase 0) à partir du `name` / `description` / `tags` / `domain` / `applies_when`
2. **Appliquée** dans le brief A du nouveau run (recommandations, anti-patterns, docs à lire)

## Workflow d'écriture

Les leçons NE sont JAMAIS écrites directement par Omega. Pipeline (v2.2) :

1. **Phase 10 (Omega)** propose 0-N leçons candidates (output verbatim).
2. **Phase 11 (Lambda)** audit les propositions selon 4 critères (qualité formelle, non-doublon, généralisation, calibration importance) et rend un verdict ACCEPTÉ | REJETÉ | À RAFFINER par proposition.
3. Si Lambda marque ACCEPTÉ → l'**orchestrateur** crée le fichier `lesson_<slug>.md` ICI avec frontmatter YAML strict, puis met à jour `memory/INDEX.md` (section domaine correspondante).

Workflow 100% automatisé : pas de validation user dans la boucle, exploitable par un agent sans humain.

## Workflow d'update

Quand une leçon existante est hit (utile dans un run) :

1. Omega propose en Phase 10 : `UPDATE LEÇON "lesson_xxx" : +1 uses`.
2. Lambda audit Phase 11 (cohérence : source_episode pas déjà présent, dates cohérentes, uses cohérent, lesson_hit confirmé).
3. Si ACCEPTÉ → orchestrateur incrémente `uses` dans le frontmatter, met `last_hit: YYYY-MM-DD`, append l'épisode courant à `source_episodes`.

## Workflow de révision

Si une leçon a été retrieve par Alpha mais s'est révélée non applicable (mauvaise formulation, applies_when trop large) :

1. Omega flag en Phase 10 : `RÉVISION LEÇON "lesson_xxx" : À RÉVISER — raison`.
2. Lambda audit Phase 11 (motif documenté concrètement avec citation rapport A/B ou rétro orchestrateur).
3. Si ACCEPTÉ → orchestrateur change `status: active → review` dans le frontmatter et append un commentaire `## Revision note` dans le corps.

L'utilisateur peut ensuite éditer manuellement les leçons en statut `review`.

## Format

Frontmatter YAML strict (parsable par `yaml.safe_load`), suivi d'un corps markdown avec sections fixes. Voir `lesson_template.md` pour un squelette vide.

### Champs frontmatter obligatoires

| Champ | Type | Description |
|---|---|---|
| `name` | string | slug unique de la leçon (e.g. `lesson_subagent_double_review_pattern`) |
| `description` | string | Résumé 1 ligne — utilisé par Alpha pour le filtrage sémantique |
| `tags` | list[string] | Tags libres (e.g. `[subagents, pattern, double-review]`) |
| `domain` | enum | `git-safety` \| `cuda-gpu` \| `refactor` \| `testing` \| `subagents` \| `performance` \| `other` |
| `importance` | int | Entier 1-5 — voir rubrique complète dans `SKILL.md` section "Rubrique d'importance" (5=showstopper, 4=critique, 3=utile, 2=mineur, 1=anecdotique). Single source of truth ; `INDEX.md` reflète cette valeur. |
| `importance_rationale` | string | 1-phrase concrète OBLIGATOIRE justifiant le score (pas générique). Exemple bon : "Sans cette règle, écrasement silencieux par sous-agents parallèles" ; mauvais : "important parce qu'utile". |
| `importance_history` | list[dict] | Log des changements d'importance, format `[{date: YYYY-MM-DD, old: N, new: M, reason: "..."}]`. Initialisé `[]`. Utile pour audit + détection de drift de calibration. |
| `applies_when` | string | Condition d'activation sémantique précise (≥ 1 phrase concrète) — Alpha utilise ça pour décider d'appliquer |
| `do_not_apply_when` | string | Contre-condition explicite — évite sur-généralisation |
| `uses` | int | Compteur d'usages (incrémenté en Phase 11) |
| `last_hit` | string | `YYYY-MM-DD` du dernier hit, ou `NEVER` |
| `source_episodes` | list[string] | Slugs des épisodes qui ont contribué à cette leçon |
| `status` | enum | `active` \| `review` (flaggée pour révision) \| `archived` (manuellement) |

### Critère Omega pour proposer une nouvelle leçon (v2.1)

Omega propose une leçon candidate si au moins UN des deux critères suivants est vrai (en plus de "non couvert par leçon existante" ET "généralisable hors-projet") :

- **(A)** Importance épisode source ≥ 3 ET généralisable hors-projet.
- **(B)** Importance 4-5 même sur 1 seule occurrence — showstopper / critique mérite d'être capturé immédiatement.

Remplace l'ancien critère "≥ 2 épisodes le montrent" qui filtrait trop strict les showstoppers rares mais critiques.

L'importance de la leçon candidate est dérivée des `source_episodes` (max ou moyenne), ajustable +/- 1 par Omega au moment de proposer (justifier dans la proposition).

### Sections du corps

- **## Rule** — 1 phrase actionnable
- **## Why** — observation factuelle ou incident source, ancré dans réalité d'un projet
- **## How to apply** — quand l'invoquer, comment l'utiliser concrètement dans un brief A ou B
- **## Counter-examples** — cas où la règle ne s'applique PAS

## Bonnes pratiques pour rédiger une bonne leçon

- **applies_when** doit être précis : pas "quand on refactor" mais "quand on fait un refactor architectural touchant ≥ 3 fichiers d'un module"
- **do_not_apply_when** doit lister explicitement les exceptions connues : pas "sauf cas spéciaux" mais "ne s'applique pas aux scripts jetables R&D" ou "ne s'applique pas si testé en TDD"
- **Why** doit citer un incident ou observation concrète d'un projet, pas une généralité
- **How to apply** doit être actionnable : "ajouter ligne X dans le brief A" ou "vérifier Y avant lancer le code", pas "être prudent"

## Hiérarchie par domaine

Les leçons sont rangées sémantiquement dans `INDEX.md` par `domain`. Domaines actuels :

- `git-safety` — opérations git, commit, recovery, stash/clean/reset
- `cuda-gpu` — kernels CUDA, full-GPU, syncs host, atomics, déterminisme
- `refactor` — refactor architectural, rename strict, copy vs reimplement
- `testing` — tests existants, pytest, parité empirique, skip/xfail
- `subagents` — patterns sous-agents, double-review, parallélisation, indépendance
- `performance` — bench, mesure, sustained, isolation compute/memory
- `other` — divers (rapports, output paths, transparence, etc.)

Pour ajouter un nouveau domaine, éditer `INDEX.md` (ajouter une section) ET ce README.

## Édition manuelle

Autorisée (et même encouragée) pour :
- Affiner `applies_when` / `do_not_apply_when` après un retrieval Alpha raté
- Archiver une leçon obsolète (`status: archived`)
- Fusionner deux leçons doublonnes

Garder une trace dans le commit message.
