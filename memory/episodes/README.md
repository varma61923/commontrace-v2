# `memory/episodes/` — Trace de chaque run `/justdoit`

## Rôle

Un fichier épisode `YYYY-MM-DD_<slug>.md` est écrit par le sous-agent **Omega** à la **Phase 10** de chaque run `/justdoit` (sauf si `--skip-omega`). L'écriture est **systématique** (traçabilité), indépendamment du verdict final ou de la création éventuelle de nouvelles leçons.

## Workflow (v2.2)

1. Run `/justdoit` se déroule (Phases 0-8).
2. Phase 9 : orchestrateur produit la mini-rétro.
3. Phase 10 : Omega reçoit tout (tâche, Alpha, A, B, verdict, rétro) et écrit l'épisode ICI.
4. Phase 11 : Lambda audit les propositions Omega (ACCEPTÉ | REJETÉ | À RAFFINER). L'orchestrateur applique les ACCEPTÉ et met à jour le champ `lessons_validated_by_lambda` du frontmatter de cet épisode avec les slugs effectivement validés.

Note v2.2 : le champ frontmatter `lessons_validated_by_user` a été renommé en `lessons_validated_by_lambda` pour refléter le passage à la validation Lambda automatique (workflow 100% automatisé, plus de dépendance utilisateur).

## Format

Frontmatter YAML strict (parsable par `yaml.safe_load`), suivi d'un corps markdown avec sections fixes.

Voir `episode_template.md` pour un squelette vide. Champs obligatoires :

| Champ | Type | Description |
|---|---|---|
| `name` | string | `YYYY-MM-DD_<slug>`, identique au nom de fichier (sans .md) |
| `description` | string | Résumé 1 ligne du run |
| `task_invocation` | string | Verbatim de l'invocation `/justdoit ...` |
| `tags` | list[string] | Tags libres pour pré-filtrage Alpha (e.g. `[cuda, refactor]`) |
| `project` | string | Projet détecté depuis le cwd (e.g. `<votre_projet>`) — utilisé pour métrique `transfer_gap` |
| `verdict` | enum | `CONFORME` \| `ARBITRAGE` \| `ABANDON` |
| `importance` | int | Entier 1-5 OBLIGATOIRE — voir rubrique complète dans `SKILL.md` section "Rubrique d'importance" (5=showstopper, 4=critique, 3=utile, 2=mineur, 1=anecdotique). Calé par Omega lors de l'écriture de l'épisode. |
| `importance_rationale` | string | 1-phrase concrète OBLIGATOIRE justifiant le score (pas générique). |
| `n_iterations` | int | Nombre d'itérations A+B effectuées |
| `commit_sha` | string | SHA du commit final |
| `duration_minutes` | int | Durée totale du run |
| `lessons_retrieved_by_alpha` | list[string] | Slugs des leçons formellement sélectionnées par Alpha dans son bloc "Lessons applicables". N'inclut PAS les counter-examples mentionnés en "Rappel mandat" — pour cela voir bloc "Lessons consultées" du rapport Alpha. |
| `lessons_hit` | list[string] | Slugs des leçons effectivement utiles (selon rapports A/B + rétro orchestrateur). **Non borné par `retrieved`** : peut inclure des lessons actives en arrière-plan (counter-examples, règles implicites). Benchmark calcule strict (hit ∩ retrieved / retrieved) et permissive (hit / retrieved). |
| `lessons_proposed_by_omega` | list[string] | Slugs des nouvelles leçons proposées par Omega (avant validation Lambda) |
| `lessons_validated_by_lambda` | list[string] | Slugs effectivement validés par Lambda en Phase 11 et appliqués par l'orchestrateur — rempli APRÈS coup. Renommé en v2.2 depuis `lessons_validated_by_user` (passage à la validation automatique). |

### Critère Omega pour proposer une nouvelle leçon depuis un épisode (v2.1)

Omega propose une leçon candidate si au moins UN des deux critères suivants est vrai (en plus de "non couvert par leçon existante" ET "généralisable hors-projet") :

- **(A)** Importance épisode source ≥ 3 ET généralisable hors-projet.
- **(B)** Importance 4-5 même sur 1 seule occurrence — un showstopper / critique mérite d'être capturé tout de suite, sans attendre une seconde occurrence.

Remplace l'ancien critère "≥ 2 épisodes le montrent" (trop strict pour les showstoppers rares mais critiques).

## Pourquoi ces champs

Les champs `lessons_*` servent à :
- Tracer ce qui a été retrouvé par Alpha et ce qui a réellement aidé (audit `lessons_retrieved_by_alpha` vs `lessons_hit`).
- Tracer la chaîne proposition Omega → validation Lambda (`lessons_proposed_by_omega` vs `lessons_validated_by_lambda`).
- Permettre des analyses cross-project ultérieures via le champ `project`.

Un script de benchmark mesurant `lesson_quality` / `implicit_retrieval` / `transfer_gap` sera ajouté dans une étape séparée et s'appuiera sur ces champs.

## Sections du corps

- **What happened** : 5-10 lignes factuelles (ce qu'on a fait, comment, résultat).
- **What surprised me** : extrait verbatim de la rétro orchestrateur (Phase 9).
- **What worked well** : 0-N items.
- **What worked less well** : 0-N items.

## Édition manuelle

Sauf cas exceptionnel (correction de champ erroné, ajout post-mortem), **ne pas éditer** un épisode après sa création. C'est une **archive**, pas un document vivant. Les corrections doivent passer par un nouvel épisode ou un commit dédié documenté.
