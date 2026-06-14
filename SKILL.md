---
name: justdoit
description: "Pattern A+B double-review sous-agents avec loop jusqu'à conformité (max 3 itérations, puis arbitrage Claude). Lance un sous-agent A implementer + commit immédiat + sous-agent B reviewer indépendant. Itère si écart. Idéal pour code architectural, refactor lourd, ports CUDA/GPU, fixes critiques où une review indépendante apporte de la valeur. v2 ajoute un mécanisme d'apprentissage long-terme : Alpha (retrieval mémoire) en Phase 0, Omega (synthèse + propositions de leçons) en Phase 10, validation Lambda automatique du backlog mémoire en Phase 11. v2.1 ajoute l'importance scalaire 1-5 sur épisodes et leçons. v2.2 remplace la validation user par un sous-agent Lambda reviewer indépendant (workflow 100% automatisé). v2.3 ajoute une couche d'attention sémantique (embeddings locaux) en pré-filtre Alpha pour permettre le passage à l'échelle (100+ leçons) sans dégrader latence ni qualité. Invocation : `/justdoit <description tâche + critères de succès inline>`. Triggers : `/justdoit`, `fais le double review`, `justdoit cette tâche`, `lance A+B sur ...`."
---

# Skill `/justdoit` — Pattern double-review A+B avec loop + apprentissage long-terme

Délègue une tâche à un sous-agent **A implementer** puis un sous-agent **B reviewer indépendant**, avec **commit immédiat après A** et **loop d'itération si écart** (max 3 itérations, puis arbitrage Claude).

**v2 (2026-05-26)** : ajout d'un mécanisme d'apprentissage long-terme via sous-agents **Alpha (retrieval)** et **Omega (synthèse + proposition d'enrichissement mémoire)** et une base mémoire structurée hiérarchiquement (`memory/`). Ce mécanisme est un terrain d'expérimentation pour le futur projet "Thomas AI" (cf. `project_thomas_ai_long_term_learning.md`).

**v2.2 (2026-05-27)** : Phase 11 entièrement automatisée via le sous-agent **Lambda** (reviewer indépendant du backlog mémoire). Lambda audit chaque proposition Omega selon 4 critères (qualité formelle, non-doublon, généralisation, calibration importance) et rend un verdict ACCEPTÉ / REJETÉ / À RAFFINER. L'orchestrateur applique les ACCEPTÉ sans humain dans la boucle. Permet à un agent d'exécuter le pipeline complet sans dépendance utilisateur.

## Schéma pipeline (v2.2)

```
Phase 0  → Alpha (retrieval mémoire) ────────────┐
                                                  ↓
Phase 1  → Parser invocation + complétion        (brief A enrichi par Alpha)
Phase 2  → Créer tâche TaskList
Phase 3  → Sous-agent A (implementer)             ──→ rapport A
Phase 4  → Commit immédiat après A
Phase 5  → Sous-agent B (reviewer indépendant)    ──→ verdict B
Phase 6  → Décision selon verdict B
            ├─ CONFORME → continue Phase 9
            └─ ÉCART
                ├─ it < max → Phase 7 (itération A2..An, retour Phase 4)
                └─ it ≥ max → Phase 8 (arbitrage Claude)
Phase 7  → Itération (relance A avec brief enrichi des écarts B)
Phase 8  → Arbitrage Claude (max_iterations atteint)
                                                  ↓
Phase 9  → Mini-rétro orchestrateur (3 questions)
Phase 10 → Omega (synthèse + propositions de leçons)  ──→ écrit episode/, propose lessons
Phase 11 → Lambda (validation automatique du backlog) ──→ verdicts ACCEPTÉ/REJETÉ/À RAFFINER, orchestrateur applique ACCEPTÉ
```

## Quand utiliser

- **Code architectural** : refactor, port, redesign (e.g. CUDA port, scan algorithmique)
- **Tâche risquée** où une review indépendante apporte de la valeur (e.g. modification d'un détecteur, fix bug critique)
- **Tâche avec critères de succès mesurables** : tests, parité empirique, perf, anti-patterns

**NE PAS utiliser pour** :
- Tâches triviales (1-2 fichiers, < 20 lignes modifiées) — voir "Bypass session principale" ci-dessous
- Tâches exploratoires sans critères clairs
- Tâches purement de lecture/recherche (utiliser `Explore` ou `general-purpose`)
- Refactor multi-fichiers en conflit (cf. `feedback_parallel_subagents_file_overlap`)

## Bypass session principale (tâches triviales < 20 lignes)

Pour les tâches dont le scope mesurable est **< 20 lignes modifiées et < 3 fichiers touchés**, la session principale (Claude orchestrateur) peut traiter directement **SANS** lancer le pattern A+B (Phase 0 Alpha → Phase 10/11 Omega/Lambda). Évite l'overhead injustifié sur des tâches simples.

**Critères d'éligibilité au bypass** :
- Modification simple : rename, fix typo, ajout literal Pydantic, bump version, fix bug 5-lignes, ajout test ciblé
- Tests existants couvrent le changement (pas besoin de nouveau test, ou ajout d'un seul test trivial)
- Pas de logique métier nouvelle
- Pas de refactor d'API publique
- Pas de modification d'invariants

**Workflow bypass (session principale directe)** :
1. Orchestrateur lit les fichiers concernés
2. Présente le diff à l'utilisateur (chat ouvert, validation interactive section par section — cf. `lesson_show_changes_before_editing`)
3. Applique l'Edit après OK utilisateur
4. Run les tests existants pour confirmer non-régression
5. Commit Git ciblé avec message explicite

**À ne PAS bypasser (utiliser /justdoit complet)** :
- Refactor architectural (e.g. changement d'API, nouveau pattern)
- Port CUDA / GPU
- Modification d'API publique
- Fix critique sur code production
- N'importe quoi qui touche > 20 lignes ou > 3 fichiers
- Tâche avec critères de succès mesurables multi-dimensionnels

**Note** : cette section ajoutée 2026-05-28 suite à la dreamer session OGHAM §4.4 (over-engineering observé sur fixes simples comme `midnight_ny` 5 lignes ou neutralisation T4 magic numbers). Le pattern empirique récent du bypass (e.g. midnight_ny Fix D 2026-05-28 traité en session principale sans /justdoit) a démontré qu'il est efficace.

## Workflow

### Phase 0 — Alpha (retrieval mémoire)

**Objectif** : avant de coder, lire la base mémoire `memory/` pour identifier les leçons et épisodes passés applicables à la tâche entrante. Injecter le résultat dans le brief A.

Lance un sous-agent **Alpha** (`Agent` avec `subagent_type=general-purpose`, `run_in_background=true`). Alpha est en **lecture seule** sur la mémoire — il ne touche à rien d'autre.

**Brief Alpha (template autonome, copiable verbatim)** :

```
Tu es Alpha, sous-agent retrieval mémoire pour /justdoit. Tu travailles en lecture seule.

## Mandat strict
- LIRE la base mémoire `~/.claude/skills/justdoit/memory/`
- IDENTIFIER les lessons et episodes pertinents pour la tâche entrante
- RETOURNER un brief structuré qui sera injecté verbatim dans le brief A

Tu NE touches à rien d'autre que la lecture de la mémoire. Pas de Write, pas de Edit, pas de git.

## Tâche entrante (verbatim utilisateur)
[INVOCATION /justdoit verbatim ici]

## Workflow obligatoire

### Étape 0 — Pré-filtre attention sémantique (v2.3)
LANCER en Bash :
```
python3 ~/.claude/skills/justdoit/memory/attention/query.py "[invocation /justdoit verbatim, tâche + critères]" --top-k=10 --include-importance-floor=4
```
Lire la sortie (une ligne par leçon, format `slug | cosine=0.XXX | importance=N`). Ce sont tes candidats prioritaires pour les étapes 1-2 ci-dessous. L'override `--include-importance-floor=4` garantit que toutes les leçons `importance >= 4` sont présentes dans la sortie, même si absentes du top-K cosine — tu DOIS les conserver comme candidats à considérer (cf. étape 7).

Le score cosine est un COMPLÉMENT au tri qualitatif (importance × tag_match), pas un remplacement. Tu juges toujours `applies_when` / `do_not_apply_when` à l'étape 4. Si le script échoue (fichier index.npz absent, erreur Python), continue avec le workflow classique (étapes 1-8) en signalant l'échec dans le rapport.

### Étapes 1-8 (workflow qualitatif classique)
1. LIRE `memory/INDEX.md` pour vérifier la pertinence des candidats du pré-filtre et compléter par domaine si nécessaire
2. Sélectionner 3-7 lessons/episodes candidats — priorité au top-K cosine de l'étape 0, complétés des candidats `importance >= 4` non couverts
3. LIRE le fichier de chaque candidat (frontmatter YAML + corps)
4. Vérifier `applies_when` et `do_not_apply_when` de chacun contre la tâche entrante
5. Retenir UNIQUEMENT ceux qui passent ce filtre sémantique
6. **Trier les leçons retenues par score décroissant** : `score = importance × tag_match`
   - `importance` = champ frontmatter (entier 1-5 ; défaut 3 si absent — flagger "à caler" dans le rapport)
   - `tag_match` = nombre de tags lesson présents dans les mots-clés de la tâche entrante (proxy simple, entier ≥ 0)
7. **Sécurité importance** : toute leçon `importance >= 4` doit être considérée même si `tag_match == 0`, parce qu'elle représente un risque critique/showstopper potentiellement transversal — ne PAS la filtrer faute de tag match, la mentionner avec mention "importance haute, applicabilité à valider"
8. Synthétiser au format de sortie ci-dessous

## Format de sortie EXACT (à respecter scrupuleusement)

## RETRIEVED MEMORY (Alpha)

### Lessons applicables (3-5 max, triées par importance × tag_match décroissant)
1. **[lesson_slug]** (cosine: 0.XX, importance: N) — rule en 1 phrase
   - Why this applies here: [1 phrase ancrée dans la tâche entrante, pas générique]
   - How to apply: [action concrète dans le code à produire — ce que A doit faire/éviter]

2. **[lesson_slug]** (cosine: 0.XX, importance: N) — ...

Note format : `cosine: 0.XX` est le score retourné par `query.py` à l'étape 0 (mettre `N/A` si la leçon a été remontée uniquement via l'override sécurité importance ≥ 4 sans présence dans le top-K cosine). L'exigence "How to apply spécifique par lesson" est CONSERVÉE — pas un copy-paste de la lesson source, mais une action concrète ancrée dans la tâche entrante.

### Episodes précédents similaires (0-2)
- [episode_slug] (importance: N) — résumé 1 phrase + ce qui en avait été retenu

### Recommandations pour le brief A
- À ajouter dans la section "Contraintes anti-patterns" : ...
- À ajouter dans la section "Documents à lire" : ...

### Confidence
HAUTE | MOYENNE | FAIBLE | AUCUNE

### Lessons consultées (pour traçabilité)
[Liste complète des slugs consultés, même non sélectionnés — utile pour audit du retrieval. Mentionner importance entre parenthèses, cosine si disponible, et flag "à caler" si importance absente.]

## Si AUCUN précédent applicable
Retourne le bloc ci-dessous EXACTEMENT :

## RETRIEVED MEMORY (Alpha)

### Confidence
AUCUNE

### Lessons consultées
[liste des slugs lus quand même, avec cosine si disponible]

### Message
Pas de précédent applicable. Tâche en territoire neuf — orchestrateur, prudence accrue.

GO.
```

**Échec ou confidence AUCUNE = NON BLOQUANT** :
- Si Alpha échoue (timeout, erreur), l'orchestrateur continue sans bloquer.
- Si Alpha retourne `Confidence: AUCUNE`, l'orchestrateur continue avec un signal explicite dans le brief A : `"## RETRIEVED MEMORY (Alpha)\nno memory used — tâche en territoire neuf"`.

**Skippable** via `--skip-alpha` (cf. paramètres). Si `--alpha-only`, Alpha tourne seul et l'orchestrateur s'arrête après affichage du rapport (utile pour tester le retrieval sans coder).

### Phase 1 — Parser l'invocation et compléter si besoin

L'utilisateur invoque `/justdoit <tâche>`. La tâche peut inclure :
- Description de la tâche
- Critères de succès explicites
- Fichiers à toucher / pas toucher
- Contraintes spécifiques

**Si la tâche est ambiguë** ou les critères de succès manquent : pose 1-2 questions complémentaires via `AskUserQuestion` AVANT de lancer A. Critères standards à valider :
- Tests à faire passer (lesquels ?)
- Sémantique à préserver (laquelle ?)
- Fichiers à ne PAS toucher (territoires d'autres sous-agents en parallèle ?)
- Performance / parité empirique requise ?

**Si la tâche est suffisamment claire** : passe directement à Phase 2.

### Phase 2 — Créer la tâche dans `TaskList`

`TaskCreate` avec subject = résumé de la tâche, description = critères de succès, status = `in_progress`.

### Phase 3 — Lancer le sous-agent A (implementer)

`Agent` avec `subagent_type=general-purpose`, `run_in_background=true`.

**Brief A (template, enrichi par sortie Alpha si Phase 0 a tourné)** :

```
Tu es A, sous-agent implementer pour [TÂCHE]. Un sous-agent B reviewer indépendant auditera ton output. Pattern : feedback_subagent_double_review.

[INSERTION VERBATIM DE LA SORTIE ALPHA ICI — bloc "## RETRIEVED MEMORY (Alpha)" complet]

## Mission
[Description précise de la tâche]

## Critères de succès (verbatim utilisateur)
- [Critère 1]
- [Critère 2]
- ...

## Documents à lire avant de coder
- [Liste docs canoniques projet]
- [Spec / CDC si applicable]
- [+ Docs recommandés par Alpha si applicable]

## Contraintes anti-patterns (standards)
- Pas de @property d'alias / back-compat (strict rename si refactor)
- Pas de magic number sans justification empirique
- Pas de mock / simulation séparée
- Pas de output /tmp (utiliser results/<run_name>/)
- Pas de Python loop évitable si vectorisable
- Pas de tests skip / xfail pour masquer un bug
- Sémantique projet préservée (tests existants doivent passer)
- [+ Anti-patterns recommandés par Alpha si applicable]

## NE PAS faire
- NE PAS commit (l'orchestrateur le fera)
- NE PAS faire de git operations (stash, clean, restore) — risque de perte de code
- NE PAS toucher aux fichiers : [liste exclusions]

## Format rapport final
[Structure attendue : fichiers modifiés, tests, mesures, surprises]

## CWD / Python
[Standard projet]

GO. Vise CONFORME en 1 itération.
```

Attends la notification de complétion. **Pas de polling.**

### Phase 4 — Commit immédiat après A

**Dès qu'A termine**, sans attendre B :
1. Lis le rapport A (fichiers modifiés)
2. `git add` les fichiers spécifiés par A
3. `git status` pour vérifier
4. `git commit` avec message clair incluant :
   - Tâche initiale
   - Verdict A (rapport synthétique)
   - Tests passants
   - Itération courante (si > 1)

**Justification** : évite la perte de code en cas d'opération git involontaire (incident vécu : sous-agent B0a v3 a fait git clean qui a effacé cuda_v3/*.py).

Si A signale un blocage / abandon : ne pas commit, retour à l'utilisateur.

### Phase 5 — Lancer le sous-agent B (reviewer indépendant)

`Agent` avec `subagent_type=general-purpose`, `run_in_background=true`.

**Brief B (template)** :

```
Tu es B, sous-agent reviewer indépendant pour [TÂCHE]. Tu N'ES PAS l'auteur du code. Tu juges contre les critères de succès. Pattern : feedback_subagent_double_review.

## Mandat strict
1. LIRE le code à reviewer (diff du commit [SHA])
2. LIRE le CDC / spec / critères de succès
3. Auditer méthodologiquement (sémantique, tests, anti-patterns, critères user)
4. Vérifier empiriquement quand possible (lancer pytest, mini-bench)
5. Émettre verdict : CONFORME ou ÉCART
6. NE PAS modifier le code (lecture seule)
7. NE PAS faire de git operations (stash, clean, restore, reset, checkout)

## Fichiers à reviewer
[Liste précise : modifs A + nouveaux fichiers, depuis git diff HEAD~1]

## Critères de succès (verbatim utilisateur)
- [Critère 1]
- [Critère 2]
- ...

## Grille d'audit
### Sémantique préservée (CRITIQUE)
- Tests existants passent ? Lance pytest et confirme
- Sémantique projet : vérifier par lecture diff + exécution

### Anti-patterns
- @property d'alias présent ?
- Magic number sans justification ?
- Output /tmp ?
- Python loop évitable ?
- Tests skip / xfail ?

### Critères user
- Pour chaque critère : PASS / FAIL avec citation précise

### Tests empiriques
- Lance les commandes pertinentes (pytest, bench, smoke)
- Reporte les résultats chiffrés

## Format verdict (CRUCIAL)

```
## VERDICT B

**Décision** : [CONFORME | ÉCART]

**Si CONFORME** :
- Points marquants validés (3-5)
- Recommandations non-bloquantes optionnelles

**Si ÉCART** :
- Liste numérotée d'écarts :
  1. [section] [fichier:ligne] [exigence] [observé] [correction]
  2. ...
- Sévérité : BLOQUANT / MAJEUR / MINEUR

**Tests qui ont tourné** : commandes + résultats
```

## CWD / Python
[Standard projet]

GO. Audit rigoureux, verdict factuel. Ne réécris pas le code.
```

Attends la notification de complétion. **Pas de polling.**

### Phase 6 — Décision selon verdict B

**Si CONFORME** :
1. Mark `TaskUpdate` status=completed
2. Output un résumé à l'utilisateur (3-5 lignes) :
   - Tâche complétée
   - Verdict B
   - Commit SHA
   - Tests / mesures clés
3. **Continue Phase 9** (mini-rétro orchestrateur, sauf si `--skip-omega`).

**Si ÉCART** :
- Si it. < `max_iterations` (default 3) → **Phase 7 : itérer**
- Si it. ≥ `max_iterations` → **Phase 8 : arbitrage Claude**

### Phase 7 — Itération (relance A avec brief enrichi)

Génère un **nouveau brief A** (A2, A3, ...) avec :
- Brief original (incluant la sortie Alpha de la Phase 0 si elle a tourné)
- Section additionnelle : **"Écarts B itération précédente à corriger"**
  - Liste numérotée verbatim de B
  - Sévérité conservée
- Instruction : "Corrige les écarts listés ci-dessus tout en préservant les critères de succès initiaux."

Lance le sous-agent A2 (background).

**Loop** : retour à Phase 4 (commit immédiat) → Phase 5 (nouveau B) → Phase 6 (verdict).

### Phase 8 — Arbitrage Claude (max_iterations atteint)

Si après 3 itérations toujours ÉCART :
1. **Lis les écarts persistants** identifiés par B
2. **Lecture ciblée** du code (Read sur les fichiers concernés)
3. **Décide** :
   - Soit fix toi-même les écarts résiduels (si minor) puis commit
   - Soit présente à l'utilisateur : "Après 3 itérations A+B, écarts résiduels : [liste]. Recommandation : [option A / option B / abandon]."
4. Mark TaskUpdate status=completed avec note "arbitrage Claude"

**Continue Phase 9** (mini-rétro orchestrateur).

### Phase 9 — Mini-rétro orchestrateur (avant Omega)

**Objectif** : capturer 30 secondes de recul méta sur le run qui vient de se terminer, pour alimenter Omega avec un signal qualitatif que les rapports A et B ne contiennent pas (ressenti orchestrateur, surprises, utilité réelle d'Alpha).

L'orchestrateur produit cette rétro **lui-même par défaut** (auto-réflexion en sortie). Pour runs sensibles (e.g. tâches très ambiguës, conflits A↔B persistants, user présent), l'orchestrateur **peut** demander confirmation/correction à l'user via `AskUserQuestion` (max 3 questions).

**Bloc rétro EXACT** (à produire verbatim) :

```
## Rétro orchestrateur (avant Omega)

1. Ce qui a bien marché : [phrase courte]
2. Ce qui m'a surpris / dérangé : [phrase courte]
3. Le brief Alpha a-t-il été utile ? [OUI / PARTIELLEMENT / NON — pourquoi]
4. Qu'avons-nous appris sur LE MARCHÉ / domaine projet (vs workflow méta) ? [phrase courte ou "rien de notable côté domaine ce run"]
```

Ce bloc est injecté verbatim dans le brief Omega à la Phase 10.

**Note sur la 4e question (ajoutée 2026-05-28)** : contre-mesure à l'effet de loupe méta-workflow observé sur les premiers runs OGHAM (cf. dreamer session 2026-05-27_first-dreamer-ogham §1.6) — la majorité des lessons générées étaient méta (`amend_brief_a`, `orchestrator_fix_residual`, etc.) au détriment du domaine (`signal_absence`, `prefer_scalar`). Forcer la réflexion explicite sur le domaine équilibre la base mémoire long-terme. Réponse "rien de notable" est OK et explicite — pas de hallucination.

**Skippable conjointement avec Phase 10/11** via `--skip-omega`.

### Phase 10 — Omega (synthèse + proposition d'enrichissement mémoire)

**Objectif** : écrire systématiquement l'épisode (traçabilité du run) et **proposer** (sans écrire) 0-N leçons candidates et 0-N updates de leçons existantes.

Lance un sous-agent **Omega** (`Agent` avec `subagent_type=general-purpose`, `run_in_background=true`).

**Brief Omega (template autonome, copiable verbatim)** :

```
Tu es Omega, sous-agent synthèse + enrichissement mémoire pour /justdoit.

## Mandat
1. ÉCRIRE l'ÉPISODE du run qui vient de se terminer (TOUJOURS, traçabilité obligatoire) directement dans `memory/episodes/YYYY-MM-DD_slug.md` ; CALER l'importance de l'épisode (1-5 + rationale 1-phrase) selon la rubrique SKILL.md
2. PROPOSER (NE PAS ÉCRIRE) 0-N leçons candidates nouvelles, avec importance + rationale 1-phrase pour chacune
3. PROPOSER (NE PAS ÉCRIRE) 0-N updates de leçons existantes
4. Optionnellement : flag "À RÉVISER" si une leçon retrieve par Alpha s'est révélée non applicable

Tu N'AS PAS l'autorisation d'écrire dans `memory/lessons/`. Les fichiers leçons sont écrits par l'orchestrateur APRÈS validation Lambda automatique (Phase 11).

## Rubrique d'importance (1-5, à appliquer pour épisode ET leçons)

- **5 (showstopper)** : sans cette leçon, la classe de tâche entière échoue ou cause perte de données / sécurité.
- **4 (critique)** : ignorer cette leçon → forte probabilité de rework majeur ou de bug subtil difficile à détecter.
- **3 (utile)** : la leçon évite un anti-pattern courant ou un piège méthodologique. Économise du temps significatif.
- **2 (mineur)** : leçon valable mais d'impact limité, applicable à un sous-cas spécifique.
- **1 (anecdotique)** : observation intéressante mais peu actionnable → préférer documenter comme note d'épisode.

Le rationale est OBLIGATOIRE et doit être 1 phrase concrète (pas "important parce qu'utile").

## Inputs (verbatim)

### Tâche initiale (invocation /justdoit)
[INSERTION VERBATIM ICI]

### Rapport Alpha (Phase 0)
[INSERTION VERBATIM ICI — ou "AUCUN (--skip-alpha)" si skip]

### Brief A initial
[INSERTION VERBATIM ICI]

### Rapports A1..An (toutes les itérations)
[INSERTION VERBATIM ICI, séparés par "--- ITÉRATION N ---"]

### Rapports B1..Bn (toutes les itérations)
[INSERTION VERBATIM ICI, séparés par "--- ITÉRATION N ---"]

### Verdict final
[CONFORME après N itérations | ARBITRAGE Claude après 3 itérations | ABANDON]

### Rétro orchestrateur (Phase 9)
[INSERTION VERBATIM DU BLOC]

### Métadonnées run
- task_invocation: [verbatim]
- project: [détecté depuis cwd, e.g. "<votre_projet>"]
- commit_sha: [SHA final]
- duration_minutes: [N]
- n_iterations: [N]

## Mission 1 — Écrire l'épisode (TOUJOURS)

Fichier : `memory/episodes/YYYY-MM-DD_slug.md` où :
- YYYY-MM-DD = date du run
- slug = 3-5 mots dérivés de la tâche (lowercase, separator `-`)

Frontmatter YAML STRICT (parsable par yaml.safe_load) :

---
name: YYYY-MM-DD_slug
description: one-line summary du run
task_invocation: verbatim invocation /justdoit ...
tags: [tag1, tag2]
project: nom-du-projet
verdict: CONFORME | ARBITRAGE | ABANDON
importance: N          # entier 1-5, voir rubrique ci-dessus
importance_rationale: "1-phrase concrète, justifie le score"
n_iterations: N
commit_sha: xxx
duration_minutes: N
lessons_retrieved_by_alpha: [list of lesson slugs retournés par Alpha]
lessons_hit: [list of lesson slugs effectivement utiles d'après le run + rétro]
lessons_proposed_by_omega: [list of proposed lesson slugs nouvelles ci-dessous]
lessons_validated_by_lambda: []  # à compléter par orchestrateur en Phase 11 (post-Lambda)
---

## What happened
[5-10 lignes factuelles : ce qu'on a fait, comment, résultat]

## What surprised me
[Extrait rétro orchestrateur — verbatim de la Phase 9]

## What worked well
[Liste 0-N items, basée sur rapports A/B + rétro]

## What worked less well
[Liste 0-N items]

## Mission 2 — Proposer leçons nouvelles (0-N)

Critères pour PROPOSER une nouvelle leçon (au moins UN des deux doit être VRAI, ET la leçon doit être non couverte par une existante ET généralisable hors de ce projet précis) :
- **(A)** Importance épisode source ≥ 3 ET généralisable hors de ce projet précis (chercher dans memory/INDEX.md + memory/lessons/ pour vérifier non-doublon)
- **(B)** Importance 4-5 même sur 1 seule occurrence — un showstopper / critique mérite d'être capturé tout de suite, pas besoin d'attendre une seconde occurrence

L'importance de la leçon candidate est dérivée de ses `source_episodes` (max ou moyenne des importances source). Tu peux l'ajuster de +/- 1 au moment de proposer (justifier dans la proposition).

Si rien de notable : dire franchement "Rien de neuf à apprendre, épisode archivé pour traçabilité".

## Mission 3 — Proposer updates de leçons existantes (0-N)

Pour chaque leçon retrieve par Alpha qui a réellement aidé :
- Proposer : `uses += 1`, `last_hit = today`, append `source_episodes`

Pour chaque leçon retrieve qui s'est révélée non applicable / mal formulée :
- Proposer : flag "À RÉVISER" avec raison

## Format de sortie EXACT (verbatim)

## OMEGA OUTPUT

### Episode written
- Path: memory/episodes/YYYY-MM-DD_slug.md
- Status: created
- Importance: N — "[rationale 1 phrase]"

### Lessons candidates (à valider par Lambda AVANT write)
1. **[lesson_slug_proposé]**
   - Rule: ...
   - Why: ... (cite l'épisode source)
   - How to apply: ...
   - applies_when: ...
   - do_not_apply_when: ...
   - Importance: N — "[rationale 1 phrase concrète]" (dérivée des source_episodes, ajustée si pertinent)
   - Justification "pourquoi nouvelle vs existante" : ...

2. ...

### Lessons updates (existantes à incrémenter)
1. [lesson_slug existant] : +1 uses (a aidé sur cet épisode), append source_episode YYYY-MM-DD_slug
2. ...

### Lessons revisions (existantes à flagger)
1. [lesson_slug existant] : À RÉVISER — raison concrète
2. ...

### Aucune leçon nouvelle ?
[Si rien de notable, le dire franchement et expliquer pourquoi le run n'a pas généré d'apprentissage transférable]

GO.
```

### Phase 11 — Lambda (validation automatique du backlog mémoire)

**Objectif** : auditer automatiquement chaque proposition Omega (nouvelles leçons, updates, révisions) via un sous-agent **Lambda reviewer indépendant**, puis l'orchestrateur applique uniquement les propositions ACCEPTÉ. Workflow 100% automatisé, utilisable par un agent sans humain dans la boucle.

**Lambda est à Omega ce que B est à A** : un reviewer indépendant qui juge contre des critères explicites, pas l'auteur des propositions.

**Workflow** :
1. L'orchestrateur lance Lambda en background (`Agent` avec `subagent_type=general-purpose`, `run_in_background=true`) avec le brief verbatim ci-dessous, en injectant la sortie Omega + accès lecture à la base mémoire.
2. Lambda audit chaque proposition selon 4 critères (qualité formelle, non-doublon, généralisation, calibration importance) et retourne un verdict ACCEPTÉ / REJETÉ / À RAFFINER par proposition.
3. L'orchestrateur applique les propositions ACCEPTÉ (écriture leçons / updates / révisions).
4. Les REJETÉ et À RAFFINER sont logués dans le rapport final pour traçabilité (pas appliqués).

**Brief Lambda (template autonome, copiable verbatim)** :

```
Tu es Lambda, sous-agent reviewer du backlog mémoire pour /justdoit. Indépendant des choix Omega.

## Mandat strict
- LIRE la base mémoire `~/.claude/skills/justdoit/memory/` (lessons existantes, INDEX.md, épisodes récents si besoin)
- LIRE la rubrique d'importance dans `SKILL.md` section "Rubrique d'importance"
- LIRE le rapport Omega (verbatim injecté ci-dessous)
- AUDITER chaque proposition Omega (nouvelles leçons + updates + révisions) selon 4 critères
- RETOURNER un verdict ACCEPTÉ | REJETÉ | À RAFFINER par proposition avec justification 2-3 phrases

Tu N'AS PAS l'autorisation d'écrire dans `memory/`. Tu es strictement en lecture seule. Pas de Write, pas de Edit, pas de git. C'est l'orchestrateur qui applique tes verdicts ACCEPTÉ.

## Inputs (verbatim)

### Rapport Omega
[INSERTION VERBATIM ICI — bloc "## OMEGA OUTPUT" complet]

### Format leçon attendu (rappel)
- Frontmatter YAML strict : name, description, tags, domain, importance (1-5), importance_rationale, importance_history ([]), applies_when, do_not_apply_when, uses, last_hit, source_episodes, status (active|review|archived)
- Corps : ## Rule, ## Why, ## How to apply, ## Counter-examples
- Référence : `memory/lessons/README.md` et `memory/lessons/lesson_template.md`

### Rubrique importance (rappel verbatim)
- 5 (showstopper) : sans cette leçon, la classe de tâche entière échoue ou cause perte de données / sécurité.
- 4 (critique)    : ignorer cette leçon → forte probabilité de rework majeur ou de bug subtil difficile à détecter.
- 3 (utile)       : la leçon évite un anti-pattern courant ou un piège méthodologique. Économise du temps significatif.
- 2 (mineur)      : leçon valable mais d'impact limité, applicable à un sous-cas spécifique.
- 1 (anecdotique) : observation intéressante mais peu actionnable → préférer documenter comme note d'épisode.

## Workflow audit (par proposition)

### Pour chaque NOUVELLE LEÇON proposée

Vérifier les 4 critères suivants. Verdict ACCEPTÉ uniquement si les 4 passent.

1. **Qualité formelle** :
   - `applies_when` concret et précis (pas "quand on refactor" mais "quand on fait un refactor architectural touchant ≥ 3 fichiers")
   - `do_not_apply_when` explicite (pas "sauf cas spéciaux")
   - `importance` (1-5) ET `importance_rationale` (1-phrase concrète, actionnable, pas "important parce qu'utile")
   - YAML proposé valide (champs requis présents, types corrects)

2. **Non-doublon** :
   - LIRE `memory/INDEX.md` pour le domaine concerné
   - Grep sémantique : la Rule proposée chevauche-t-elle une leçon existante (même domain + tags similaires) ?
   - Si chevauchement → proposer UPDATE de la leçon existante au lieu d'une nouvelle. Verdict : REJETÉ ou À RAFFINER avec mention "remplacer par UPDATE leçon X"

3. **Généralisation** :
   - Peut-on imaginer ≥ 3 contextes d'application hors du projet/run courant ? (e.g. autre projet, autre stack, autre type de tâche)
   - Si trop spécifique → REJETÉ avec mention "trop spécifique au run X, mérite plutôt note d'épisode"

4. **Calibration importance** :
   - Le score est-il défendable contre la rubrique 1-5 ?
   - Si A et B du run ont divergé sur la calibration (cf. rapports A/B injectés dans le brief Omega), juger l'écart : ±1 acceptable, ≥2 → À RAFFINER avec mention "écart calibration à arbitrer"

### Pour chaque UPDATE proposé (uses += 1, last_hit, etc.)

1. **Cohérence** :
   - `source_episode` proposé non déjà présent dans `source_episodes` de la leçon (sinon double-comptage → REJETÉ)
   - `last_hit` proposé ≤ date du jour (pas de date future → REJETÉ)
   - `uses` proposé cohérent avec `uses` actuel + 1 (sinon REJETÉ)

2. **Justification** :
   - La leçon a-t-elle effectivement été utile dans le run ? (vérifier dans le rapport Omega que le slug est dans `lessons_hit` de l'épisode)
   - Si non confirmable → REJETÉ avec mention "lesson_hit non confirmé par rapport"

### Pour chaque RÉVISION proposée (status active → review)

1. **Motif documenté** :
   - Le motif (raison du flag À RÉVISER) est-il documenté concrètement (citation rapport A/B ou rétro orchestrateur) ?
   - Si motif générique ou non sourcé → REJETÉ

## Format de sortie EXACT (verbatim)

## LAMBDA OUTPUT

### Decisions par proposition

#### Nouvelle lesson [lesson_slug_proposé]
- **Décision** : ACCEPTÉ | REJETÉ | À RAFFINER
- **Justification** : [2-3 phrases couvrant qualité formelle, non-doublon, généralisation, calibration importance]
- **Si À RAFFINER** : champs précis à corriger

#### Update [lesson_slug existant]
- **Décision** : ACCEPTÉ | REJETÉ
- **Justification** : [vérifier source_episode non déjà présent, dates cohérentes, uses cohérent, lesson_hit confirmé]

#### Révision [lesson_slug existant]
- **Décision** : ACCEPTÉ | REJETÉ
- **Justification** : [motif documenté dans épisode source ?]

### Synthèse
- Total propositions : N
- ACCEPTÉ : N
- REJETÉ : N (raisons synthétiques)
- À RAFFINER : N

GO.
```

**Après le rapport Lambda, l'orchestrateur** :
1. Pour chaque NOUVELLE LEÇON marquée ACCEPTÉ :
   - Crée le fichier `memory/lessons/lesson_<slug>.md` avec frontmatter YAML strict (cf. template `memory/lessons/lesson_template.md`)
   - Champs initiaux : `uses: 0`, `last_hit: NEVER`, `source_episodes: [YYYY-MM-DD_slug_episode_courant]`, `status: active`, `importance_history: []`
2. Pour chaque UPDATE marqué ACCEPTÉ :
   - Met à jour le frontmatter du fichier leçon existant : `uses += 1`, `last_hit = today`, append episode courant à `source_episodes`
3. Pour chaque RÉVISION marquée ACCEPTÉ :
   - Change `status: active → review` dans le frontmatter
   - Append un commentaire (## Revision note) dans le corps avec la justification Lambda
4. Met à jour `memory/INDEX.md` : ajoute les nouvelles lessons dans leurs sections de domaine respectives ; reflète les updates (uses, last_hit) et révisions (status).
5. Met à jour le frontmatter de l'épisode courant : remplit `lessons_validated_by_lambda` avec la liste effective des slugs validés (champ renommé en v2.2 depuis `lessons_validated_by_user`).
6. **Trigger rebuild attention layer (v2.3)** : si au moins une création / update (qui modifie le corps ou les champs encodés) / révision a été appliquée aux étapes 1-3, l'orchestrateur lance automatiquement :
   ```
   python3 ~/.claude/skills/justdoit/memory/attention/build_index.py
   ```
   Sortie attendue : `Index built: N lessons, model=multi-qa-mpnet-base-dot-v1, dim=768`. Si le script échoue (sentence-transformers indisponible, etc.), l'orchestrateur le signale dans le rapport final mais ne bloque pas le run — l'index reste consultable en l'état pour les runs futurs. Manuel possible aussi : `python build_index.py --force` après édition manuelle.
7. Inclut dans le rapport final user :
   - Liste des propositions ACCEPTÉ appliquées
   - Liste des propositions REJETÉ avec raison Lambda (traçabilité)
   - Liste des propositions À RAFFINER avec champs à corriger (l'utilisateur peut décider de les retravailler manuellement)
   - Statut du rebuild attention layer (OK / KO / non déclenché si rien appliqué)

**Skippable** via `--skip-omega` (skip 9 + 10 + 11 ensemble — le rebuild attention est skippé aussi puisqu'il est conditionné à l'application Lambda).

## Mémoire

### Chemin de la base

Base mémoire du skill : `memory/` (chemin relatif à ce `SKILL.md`, soit `~/.claude/skills/justdoit/memory/`).

### Rubrique d'importance (v2.1, 1-5)

Chaque épisode ET chaque leçon porte un champ scalaire `importance` (entier 1-5) accompagné d'un `importance_rationale` (string 1-phrase concrète obligatoire). La rubrique :

```
Importance 5 (showstopper) : sans cette leçon, la classe de tâche entière échoue
                              ou cause perte de données / sécurité.
Importance 4 (critique)    : ignorer cette leçon → forte probabilité de rework
                              majeur ou de bug subtil difficile à détecter.
Importance 3 (utile)        : la leçon évite un anti-pattern courant ou un piège
                              méthodologique. Économise du temps significatif.
Importance 2 (mineur)       : leçon valable mais d'impact limité, applicable à
                              un sous-cas spécifique.
Importance 1 (anecdotique) : observation intéressante mais peu actionnable
                              → préférer documenter comme note d'épisode.
```

**Inspiration** : Park et al. 2023 "Generative Agents: Interactive Simulacra of Human Behavior" (memory stream — eux utilisent 1-10, ici on choisit 1-5 pour calibration plus simple).

**Usage** :
- **Alpha (Phase 0)** trie les leçons retrouvées par `score = importance × tag_match` décroissant et **considère TOUJOURS les leçons `importance >= 4`** même si `tag_match == 0` (sécurité : un showstopper est probablement transversal).
- **Omega (Phase 10)** cale l'importance de l'épisode produit et propose l'importance des leçons candidates (dérivée des `source_episodes`, ajustable +/- 1 par Omega au moment de proposer).
- **Critère Omega pour proposer une leçon nouvelle** : (A) importance épisode source ≥ 3 ET généralisable hors-projet, OU (B) importance 4-5 même sur 1 seule occurrence. Remplace l'ancien critère "≥ 2 épisodes le montrent" (trop strict pour les showstoppers).

**Rétrocompatibilité** : pour les épisodes/lessons écrits avant v2.1 sans `importance` :
- Alpha traite l'absence comme `importance = 3` par défaut (médian) et flag "à caler" dans le bloc "Lessons consultées".
- Omega flag "importance absente — à caler" dans ses propositions d'update.
- Aucun script existant ne casse : les champs sont additifs.

### Évolutions futures (non implémentées v2.1)

Documentées ici pour mémoire — NE PAS implémenter sans validation utilisateur explicite et sans terrain empirique :

- **Decay temporel** : pondérer `importance` par `exp(-(today - last_hit) / tau)` pour faire émerger les leçons récemment hit. Risque : faire oublier des leçons rares mais critiques. Probable couplage avec un facteur `recency` séparé.
- **Recency séparée** : tenir un champ `recency` distinct de `importance` (Park et al. utilisent cette décomposition). À discuter quand on aura plus de données empiriques sur le retrieval Alpha.
- **Bump auto uses → importance** : si une leçon dépasse N hits sur M runs, auto-incrémenter son importance (signal empirique fort d'utilité). À discuter : risque de drift vers tout en importance 5.
- **Salience composite** : `salience = α·importance + β·recency + γ·log(uses+1)`. Style Park et al. Demande tuning α/β/γ.
- **Multi-dimensionnel** : passer de scalaire à vecteur (e.g. `importance = (severity, frequency, generalizability)`). Plus expressif mais demande UI de retrieval plus sophistiqué.

Ces évolutions sont des pistes pour Thomas AI (cf. `project_thomas_ai_long_term_learning.md`). En v2.1, on reste sur scalaire 1-5 + rationale, point.

### Architecture mémoire

```
memory/
├── INDEX.md                  # index hiérarchique par domaine (édité à chaque Phase 11)
├── episodes/
│   ├── README.md             # format épisode + workflow
│   ├── episode_template.md   # template vide
│   └── YYYY-MM-DD_<slug>.md  # un fichier par run /justdoit (écrit par Omega Phase 10)
└── lessons/
    ├── README.md             # format leçon + workflow
    ├── lesson_template.md    # template vide
    └── lesson_<slug>.md      # un fichier par leçon (créé par orchestrateur Phase 11 après validation)
```

### Quand chaque agent intervient

- **Alpha** (Phase 0, par défaut) : retrieval mémoire avant code. Skippable via `--skip-alpha`.
- **Omega** (Phase 10, par défaut) : synthèse + propositions après verdict B. Skippable via `--skip-omega`.
- **Lambda** (Phase 11, par défaut) : validation automatique du backlog mémoire après propositions Omega. Couplé à `--skip-omega` (skippé en même temps). La mémoire n'est jamais enrichie sans verdict ACCEPTÉ de Lambda — workflow 100% automatisé, pas de dépendance humaine.

### Lien avec Thomas AI

Ce mécanisme (Alpha + Omega + base mémoire) est un terrain d'expérimentation pour le futur projet "Thomas AI" (cf. `project_thomas_ai_long_term_learning.md`), dont l'objectif est d'évaluer l'apprentissage long-terme des agents (qualité leçon, retrieval contextuel implicite, transfert situation isomorphe).

### Choix de design retenus (v2 + v2.2)

- **Base hiérarchique par domaine** (Option B) : INDEX.md sectionné par domaine (git-safety, cuda-gpu, refactor, testing, subagents, performance, other) pour faciliter pré-filtrage Alpha.
- **Validation Lambda automatique** (Phase 11, v2.2) : la mémoire n'est jamais enrichie sans verdict ACCEPTÉ d'un reviewer indépendant des choix Omega. Lambda audit qualité formelle, non-doublon, généralisation, calibration importance. Workflow 100% automatisé, exploitable par un agent sans humain dans la boucle (overrides la validation user de v2-v2.1).
- **Scope global avec tag projet** : la mémoire est partagée entre projets, le champ `project:` dans frontmatter permet filtrage cross-project.
- **Échec Alpha non bloquant** : si retrieval échoue, on continue avec un signal explicite, on ne bloque pas le run.
- **Mémoire séparée du MEMORY.md global** : la base `~/.claude/skills/justdoit/memory/` est indépendante du MEMORY.md projet sous `~/.claude/projects/...`. Évolutions découplées.
- **Omega ne write JAMAIS les leçons** : seul l'épisode est écrit automatiquement (traçabilité), les leçons passent par Phase 11 (audit Lambda + application orchestrateur).
- **Lambda est à Omega ce que B est à A** : reviewer indépendant qui juge contre des critères explicites, pas l'auteur des propositions. Reproduit le pattern double-review au niveau backlog mémoire.

### Note benchmark (hors scope v2)

Un script de benchmark mesurant `lesson_quality` / `implicit_retrieval` / `transfer_gap` sera ajouté dans une étape séparée.

## Paramètres optionnels (inline)

L'utilisateur peut spécifier dans l'invocation :
- `max_iterations=N` (default 3)
- `tests=path/to/tests` (default : projet entier)
- `skip-commit-after-a` (utiliser si conflit avec autres workflows)
- `no-loop` (1 cycle A+B sans loop)
- `--skip-alpha` (skip Phase 0 retrieval mémoire)
- `--skip-omega` (skip Phases 9 + 10 + 11 — pas de mini-rétro, pas d'Omega, pas de write mémoire)
- `--alpha-only` (run Alpha seul, afficher son rapport et s'arrêter — utile pour tester le retrieval)

Exemple : `/justdoit refactor xxx --max_iterations=2 --tests=OGHAM/tests/`
Exemple : `/justdoit fix bug yyy --skip-omega` (urgence : skip apprentissage)
Exemple : `/justdoit --alpha-only "port CUDA module xxx"` (juste pour voir ce que la mémoire dit)

**Rétrocompatibilité** : toutes les invocations historiques continuent de fonctionner. Phases Alpha/Omega sont activées par défaut mais skippables.

## Bonnes pratiques

### Génération des briefs
- **Brief A doit être autonome** : le sous-agent ne voit pas la conversation. Inclus tout le contexte nécessaire (paths, conventions, exclusions, sortie Alpha si applicable).
- **Brief B doit être indépendant** : ne pas dire "valide les claims A". Donner les critères + le code, B juge contre les critères.
- **Brief Alpha / Brief Omega** : copiés verbatim depuis ce SKILL.md, pas reformulés.
- **Pas de subagent réutilisé** : nouveau Agent à chaque cycle pour garantir indépendance (SendMessage non recommandé).

### Parallélisation
- A et B sont **séquentiels** (B reviewe ce que A a produit). Pas de parallélisation A↔B.
- **Plusieurs `/justdoit` en parallèle** OK si fichiers disjoints (cf. `feedback_parallel_subagents_file_overlap`). Vérifier explicitement avant de lancer.
- **Alpha tourne avant** Phase 1 (séquentiel). Pas parallélisable avec A.
- **Omega tourne après** Phase 9 (séquentiel). Pas parallélisable avec B.

### Commit messages
Format pour les commits intermédiaires :
```
[TÂCHE] : it N (A produit, B pas encore review)

[Résumé court rapport A]
[Tests passants : N/M]

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
```

Pour le commit final (CONFORME) :
```
[TÂCHE] : CONFORME après N itération(s)

[Résumé final : sémantique, perf, tests]

Verdict B reviewer CONFORME.
[Liens fichiers / résultats]

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
```

### Anti-patterns du skill lui-même
- Ne pas skip Phase 4 (commit immédiat) — risque de perte
- Ne pas relancer le MÊME Agent via SendMessage (perd l'indépendance B↔A)
- Ne pas itérer > 3 fois sans arbitrage utilisateur explicite
- Ne pas lancer plusieurs `/justdoit` sur les mêmes fichiers en parallèle
- Ne pas laisser Omega écrire les leçons sans validation Lambda (Phase 11 obligatoire si Omega tourne)
- Ne pas bloquer le run sur échec Alpha (non bloquant par design)

## Mémoires utilisateur de référence

- `feedback_subagent_double_review` — pattern A+B implementer + reviewer indépendant
- `feedback_no_subagents_for_architectural_code` — exception : OK avec double-review (ce skill)
- `feedback_parallel_subagents_file_overlap` — sérialiser sur mêmes fichiers
- `feedback_show_changes_before_editing` — A doit montrer son plan dans son rapport
- `feedback_transparency_when_deviating` — A doit signaler si dévie de la spec
- `project_thomas_ai_long_term_learning` — projet lié, ce mécanisme est un terrain d'expérimentation

## Exemples d'invocation

```
/justdoit refactor InferenceEngine.step() pour accepter step_runtime_kwargs en plus de step_default_kwargs (init). Critères : 33 tests inference existants passent, pas de @property d'alias, default = comportement V0.3 inchangé.
```

```
/justdoit fix bug parité multi-instrument Pression dans cuda_v3 (diff 0.16 actuellement). Cause hypothèse : ordering L2-supersede filter ou couplage Exhaustion. Critères : diff < 1e-5 sur 10 inst × H1 × 1 mois, 533 tests passent, ne pas toucher latents/force_relative.py (B0b territory).
```

```
/justdoit port CUDA du module liquidite_gpu.py vers Triton custom kernel. Critères : parité bit-exact CPU/GPU < 1e-4, speedup ≥ ×3 sur smoke 3m, tests existants 526 passent. --max_iterations=2
```

```
/justdoit --alpha-only "refactor module XYZ pour vectoriser update()"   # juste voir ce que la mémoire dit
```

```
/justdoit fix urgence prod --skip-omega   # urgence : pas le temps pour mini-rétro + apprentissage
```
