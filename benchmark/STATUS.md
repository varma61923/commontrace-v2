# Benchmark de la base mémoire /justdoit — État au 2026-05-27

> Doc d'état mise à jour à chaque évolution majeure du benchmark ou de ses métriques.
> Version /justdoit couverte : **v2.3** (Alpha + A + B + Omega + Lambda + attention layer).

---

## 1. Vue d'ensemble

Le benchmark mesure la qualité du système de mémoire long-terme intégré au skill `/justdoit`. Il analyse les épisodes (un par run, écrits par Omega en Phase 10) et les leçons (un fichier par règle procédurale, validées par Lambda en Phase 11) stockés sous `~/.claude/skills/justdoit/memory/`, et calcule des métriques sur 3 axes complémentaires :

- **lesson_quality** — qualité de la génération Omega (validation Lambda)
- **implicit_retrieval** — qualité du retrieval Alpha (précision + richesse)
- **transfer_gap** — capacité de transfert cross-projet (généralisation)

Position dans l'écosystème /justdoit : **outil hors hot path**, invocable manuellement à tout moment. Il ne bloque jamais un run et ne consomme aucun token LLM (parsing local des frontmatters YAML). Pas de dépendance à l'orchestrateur, aux sous-agents, ni à l'attention layer. Lecture seule sur `memory/`.

À quoi il sert concrètement :
- Donner à G. un signal quantitatif sur la santé de la base mémoire (au-delà du ressenti qualitatif des runs)
- Détecter les dérives (qualité Omega qui chute, retrieval Alpha qui sous-performe, lessons jamais hit qui s'accumulent)
- Fournir un input à des futurs mécanismes (Dreamer v2.4, decay temporel, archivage automatique)
- Documenter l'évolution dans le temps (snapshot reproductible)

Invocation :
```bash
~/git/project-x/.venv/bin/python ~/.claude/skills/justdoit/benchmark/measure_performance.py
```

Voir §6 pour les flags et la fréquence recommandée.

---

## 2. Ce qui existe

### 2.1 Script `measure_performance.py`

Source canonique unique : `~/.claude/skills/justdoit/benchmark/measure_performance.py` (~430 lignes, dont ~100 de rendu markdown / HTML). Standalone, pas de package, pas de tests, pas de config externe.

**Architecture** (modules logiques dans le même fichier) :

1. **Parse frontmatter** (`parse_frontmatter`, `parse_yaml_minimal`) : extrait le bloc YAML entre les `---` au début de chaque fichier `.md`. Utilise PyYAML si disponible (présent dans `~/git/project-x/.venv/`), sinon retombe sur un parser regex minimal (suffit pour le format épisode/leçon actuel).

2. **Load episodes/lessons** (`load_episodes`, `load_lessons`) : `glob` sur `memory/episodes/2*.md` et `memory/lessons/lesson_*.md`. Ignore les templates (`*_template`). Le slug d'une leçon est le `basename` sans `.md`. Pas de cache, relecture intégrale à chaque invocation.

3. **Compute metrics** (`compute_lesson_quality`, `compute_implicit_retrieval`, `compute_transfer_gap`, `compute_extras`) : un calcul par axe + un calcul agrégé pour les annexes (top hits, never-hit, propositions non validées, distribution importance, couverture domain).

4. **Render** (`render_markdown`, `render_html`) : sortie textuelle (stdout) ou HTML (fichier sous `memory/benchmark_reports/YYYY-MM-DD_HHMMSS.html`). Le JSON est rendu inline (`json.dumps`) sans helper dédié.

**CLI flags** :

| Flag | Effet |
|---|---|
| (default) | Markdown stdout, tous les épisodes |
| `--n=N` | Garde les N derniers épisodes (tri par nom = par date) |
| `--html` | Écrit un rapport HTML dans `memory/benchmark_reports/` |
| `--json` | JSON brut stdout (pour pipeline downstream) |

**Formats de sortie** :
- **Markdown** : sections fixes (Métriques principales 3 axes, Détail par épisode en table, Top 5 hits, Never-hit, Proposed-not-validated, Distribution importance, Couverture domain). Citable directement dans une discussion ou un commit message.
- **HTML** : enrobe le markdown dans un template HTML simple avec styling inline. Pas de chart, pas d'interactivité — juste lisible dans un navigateur.
- **JSON** : structure complète (timestamp, n_episodes, n_lessons, 3 axes, episodes verbatim, extras) — payload prêt pour comparaison run-à-run ou ingestion par un futur Dreamer.

Pas de hook automatique : le benchmark n'est ni lancé en Phase 11 ni en hook git. Invocation manuelle par l'utilisateur ou un agent qui le décide.

### 2.2 Métrique 1 — lesson_quality

**Définition** : moyenne sur les épisodes de la fraction de propositions Omega validées par Lambda.

**Formule** :
```
lesson_quality = mean(  |validated_by_lambda| / |proposed_by_omega|  )
                 sur épisodes avec proposed_by_omega non vide
```

Le champ source est `lessons_proposed_by_omega` (propositions Omega Phase 10, avant audit Lambda) et `lessons_validated_by_lambda` (verdicts ACCEPTÉ appliqués par l'orchestrateur en Phase 11). Fallback rétrocompat : si `lessons_validated_by_lambda` absent (v2.1 ou avant), on lit `lessons_validated_by_user` — c'est la fonction `get_validated()` qui gère le pivot v2.2.

**Interprétation** :
- ~100% : Omega calibre bien ses propositions, Lambda les accepte
- < 80% : signal qualité Omega dégradée (propositions hors critères, doublons, importance mal calibrée) — voir aussi métrique annexe "proposed-not-validated"
- > 100% (théorique) : peut arriver si une proposition Omega d'un run précédent est validée a posteriori par un Lambda d'un run ultérieur (rétro-validation du backlog) — observé empiriquement sur le snapshot actuel

**Exclusions** : épisodes avec aucune proposition Omega (`lessons_proposed_by_omega == []`) sont exclus du calcul — n'aurait aucun sens de compter 0/0. Le compte effectif est rapporté (`sur N épisodes valides`).

**Ce que ça mesure** : alignement Omega ↔ Lambda. Plus précisément, le taux de propositions qui passent les 4 critères Lambda (qualité formelle + non-doublon + généralisation + calibration importance) sans modification.

**Ce que ça ne mesure pas** : la qualité absolue d'une proposition (Lambda peut valider une lesson qui finit jamais utilisée). Distinction importante : `lesson_quality` mesure la conformité au critère, pas l'utilité réelle. Pour l'utilité, voir métrique annexe "never-hit" (§2.5).

### 2.3 Métrique 2 — implicit_retrieval (strict + permissive)

Le retrieval Alpha est évalué via deux angles complémentaires. Les deux sont calculés et reportés systématiquement.

**Définitions** :

```
strict     = mean(  |hit ∩ retrieved| / |retrieved|  )
permissive = mean(  |hit|             / |retrieved|  )
             sur épisodes avec retrieved non vide
```

Les champs source sont `lessons_retrieved_by_alpha` (sélections formelles Alpha dans son rapport, bloc "Lessons applicables") et `lessons_hit` (lessons effectivement utiles selon les rapports A/B + rétro orchestrateur).

**Pourquoi deux angles** : RETEX du run 3 (`2026-05-27_formalize-lambda`). Le débat structurel : faut-il `hit ⊂ retrieved` (strict, hit ne peut excéder ce qu'Alpha a remonté) ou `hit` peut-il inclure des lessons en arrière-plan (counter-examples, règles méthodologiques implicites jamais formellement sélectionnées) ? La décision design figée : `hit` n'est pas borné, ce qui rend `permissive` significatif. Le ratio strict mesure la **précision** d'Alpha (sélections utilisées), le ratio permissive mesure la **richesse** (peut excéder 100% si l'application Omega compte des hits hors retrieved).

**Interprétation diagnostique** :
- strict ≈ permissive ≈ 100% : Alpha est précis ET complet (cas idéal)
- strict élevé, permissive >> strict : Alpha est précis mais sous-retrieve (Omega complète avec des lessons non sélectionnées)
- strict bas, permissive bas : Alpha retrieve mais peu de hits (lessons non applicables ou rétro orchestrateur sévère)
- strict bas, permissive élevé : pattern atypique — investiguer

**Exclusions** : épisodes avec `retrieved == []` exclus (typiquement le premier run, base mémoire vide). Le compte effectif est rapporté.

**Ce que ça mesure** : qualité fonctionnelle du retrieval Alpha — est-ce que ce qu'Alpha remonte sert effectivement à A/B ? Mesure plus fiable que la confidence auto-déclarée d'Alpha (qui est subjective).

**Ce que ça ne mesure pas** : le faux négatif (lessons pertinentes que Alpha aurait dû remonter mais n'a pas remontées). Pour détecter ça il faudrait un ground truth annoté — pas disponible aujourd'hui.

### 2.4 Métrique 3 — transfer_gap

**Définition** : fraction des hits dont la lesson source provient d'un projet différent du projet courant.

**Formule** :
```
transfer_gap = cross_hits / total_hits
où total_hits  = hits dont les source_episodes sont traçables (projet identifiable)
   cross_hits  = hits dont aucun source_episode n'appartient au projet courant
```

Algorithme : pour chaque épisode, pour chaque `hit_slug` dans `lessons_hit`, on résout la lesson, on lit ses `source_episodes`, on collecte les `project:` distincts, et on incrémente `cross_hits` si le projet courant n'est pas dans le set. Si `source_episodes == []` (lesson seedée sans épisode source), le hit est marqué `untraceable` et exclu du calcul.

**Interprétation** :
- 0% : tous les hits viennent de lessons seedées sur le même projet (pas de transfert)
- > 0% : preuve empirique qu'une lesson sur le projet X aide effectivement sur le projet Y
- N/A : aucun hit traçable (base mono-projet ou lessons sans source_episodes)

**État actuel** : mono-projet — tous les épisodes ont `project: project-x`, donc la métrique vaut mécaniquement 0% (aucun hit ne peut être cross-projet). Le `transfer_gap` n'est pas significatif tant que la base reste mono-projet.

**Ce qu'il faut pour que la métrique devienne mesurable** :
- Au moins un projet distinct dans la base mémoire (e.g. `project: module-a` séparé de `project: project-x`)
- Plusieurs runs sur chaque projet pour avoir un corpus statistiquement comparable
- Des lessons seedées dans un projet qui se font retrieve et hit sur un autre

Aujourd'hui les épisodes module-a ont `project: project-x` parce qu'module-a est un sous-projet de project-x. La distinction project mériterait peut-être un découpage plus fin (e.g. `project: module-a`, `project: module-b`, `project: module-c-v2`) pour rendre la métrique exploitable même au sein de project-x.

### 2.5 Métriques annexes

Le benchmark calcule en plus 5 indicateurs annexes utiles pour l'audit qualitatif :

- **Top 5 lessons par uses** : ranking par compteur `uses` (incrémenté en Phase 11 ACCEPTÉ). Révèle quelles lessons sont les plus mobilisées en pratique. Identifie aussi les lessons qui pourraient bénéficier d'une promotion en importance (corrélation empirique uses ↔ utilité).

- **Lessons jamais hit** : `uses == 0` ET non templates. Candidates archivage à terme (status `active → archived`). Permet d'identifier les lessons seedées mais sans traction, qui polluent le retrieval Alpha sans bénéfice.

- **Lessons proposées par Omega mais jamais validées par Lambda** : `(proposed ∪ all_episodes) - (validated ∪ all_episodes)`. Signal qualité Omega dégradée : si une proposition n'a JAMAIS été acceptée par Lambda dans aucun run, c'est probablement un faux positif systématique.

- **Distribution importance** : nombre de lessons et d'épisodes par niveau d'importance 1-5. Permet de détecter le biais de calibration (cf. RETEX 7.5 doc : pas de cas 1-2 dans les seeds).

- **Couverture par domain** : nombre de lessons par domaine (`git-safety`, `cuda-gpu`, `refactor`, `testing`, `subagents`, `performance`, `other`). Révèle les domaines sur-représentés (`subagents` aujourd'hui) et sous-représentés (`git-safety`, `performance`).

Ces annexes sont peu coûteuses à calculer et fournissent un contexte qualitatif aux 3 métriques principales. Particulièrement utiles pour répondre à "ma base mémoire est-elle saine ?" au-delà des chiffres agrégés.

---

## 3. Ce qui fonctionne bien (validation empirique sur la base actuelle)

### Stats actuelles (snapshot 2026-05-27)

- **n_episodes** = 12 (run de bootstrap v2 + 4 méta-runs /justdoit + 7 runs module-a réels)
- **n_lessons** = 26 (5 cuda-gpu→1, refactor→6, subagents→10, testing→5, other→4 + lessons seedées)
- **lesson_quality** = **104.5%** sur 11 épisodes valides
- **implicit_retrieval** : strict **88.2%**, permissive **97.3%** sur 11 épisodes valides
- **transfer_gap** = 0.0% sur 55 hits traceables (mono-projet)

Le détail verbatim est en §7.

### Patterns détectés

**Émergence d'une hiérarchie d'usage naturelle**. Le top 5 by uses montre une distribution non triviale :
- `lesson_semantic_check_not_just_syntactic` : 7 uses (refactor transverse)
- `lesson_no_tmp_results` : 5 uses (paths/results)
- `lesson_serialize_subagents_same_files` : 4 uses (subagents)
- `lesson_subagent_double_review_pattern` : 4 uses (subagents)
- `lesson_alpha_brief_quality_drives_a_quality` : 3 uses (subagents)

Cette hiérarchie reflète bien le pattern observé empiriquement : les lessons "subagents" et "refactor" dominent parce que `/justdoit` est précisément un skill pour les refactors via subagents — c'est attendu et auditable.

**Distinction Omega/Lambda structurellement révélée**. Le ratio strict (88.2%) vs permissive (97.3%) montre qu'Alpha est précis : ~88% des sélections Alpha servent effectivement. L'écart d'environ 9 points entre strict et permissive correspond aux lessons hit en arrière-plan (counter-examples, règles méthodologiques) que Omega compte mais qu'Alpha n'a pas formellement sélectionnées. Pattern attendu et cohérent avec le design — voir §2.3.

**lesson_quality > 100% empirique**. Le 104.5% observé n'est pas une anomalie de calcul mais un effet de rétro-validation. Un Lambda d'un run récent peut valider une proposition restée en attente d'un run antérieur (le backlog v2.1 qui s'était accumulé). Quand on divise validated par proposed sur l'épisode courant, on peut donc avoir un ratio > 1 si validated inclut des slugs proposés ailleurs. Pattern à clarifier sémantiquement : "% de propositions validées par Lambda" est ambigu si la validation est asynchrone.

**Lessons module-a empiriquement utiles**. La lesson `lesson_audit_cascade_may_reveal_noop_scope` a été créée le 2026-05-27 sur l'épisode `module-a-v04-pipeline-multi-tf`, hit 3 fois ensuite (`module-a-v04-g2-eventref-tf-qualifiable`, `module-a-v04-g3-nested-outer-level-persistent`, `module-a-task4-setup-12-runnable-g4-candidate`) — preuve empirique que le mécanisme capture des règles transférables au sein d'une même classe de runs (refactor multi-dimensionnel module-a).

### Insights opérationnels permis par le benchmark

- **Identifier lessons jamais hit** : 4 lessons à `uses: 0` actuellement (`lesson_e2e_test_xfail_when_dependency_bugs_identified`, `lesson_gp_gpu_non_deterministic`, `lesson_signal_absence_confirmed_by_regularization_convergence`, `lesson_triangulate_before_architectural_report`). Aucune n'est candidate archivage immédiat (toutes < 1 mois), mais à surveiller. La présence de `lesson_gp_gpu_non_deterministic` (seedée le 2026-05-26) sans hit traduit le fait qu'aucun run /justdoit n'a touché à du code GPU depuis — attendu.

- **Identifier propositions Omega rejetées par Lambda** : 2 propositions à ce jour (`lesson_couple_skip_flags_for_coherent_state`, `lesson_horizon_dominates_context_at_long_h`). Premier signal qualité Omega — proposées par un Omega mais rejetées par Lambda comme non généralisables ou trop spécifiques. À auditer pour comprendre le critère de rejet.

- **Distribution importance non triviale** : 16 lessons en importance 3, 9 en importance 4, 1 en importance 5. Pas de monoculture (pas tout à 5), ce qui suggère que la calibration est défendable — mais aussi confirme le biais identifié dans la RETEX 7.5 (pas de 1-2). Le scénario "tout devient 5 par drift" n'est pas observé sur ce snapshot.

- **Couverture domain auditable** : `subagents` domine (10 lessons) parce que c'est le cœur du skill ; `git-safety` et `performance` sont à 0 lessons (zone aveugle empirique — pas encore de run a généré ces patterns). `cuda-gpu` à 1 seul (la seed `lesson_gp_gpu_non_deterministic`) traduit le fait que les runs récents sont sur module-a (CPU/algo) pas sur module-b/module-c (GPU).

### Cohérence cross-fichiers

Le benchmark relit à chaque invocation toutes les sources (épisodes + lessons individuels) sans dépendre de l'INDEX.md. C'est intentionnel : `lesson_<slug>.md` est la source de vérité, INDEX.md n'est qu'un reflet. Si l'INDEX.md dérive (édition manuelle non synchronisée), le benchmark continue de retourner les vraies valeurs, ce qui permet aussi de **détecter** la dérive (comparer manuellement INDEX.md vs sortie benchmark).

---

## 4. Limitations actuelles

### 4.1 Rapport Lambda non persisté dans l'épisode

L'épisode courant capture `lessons_proposed_by_omega` et `lessons_validated_by_lambda` mais **pas le verdict Lambda complet** (ACCEPTÉ / REJETÉ / À RAFFINER + justification 2-3 phrases). Conséquence : le benchmark ne peut pas calculer **le taux de rejet Lambda effectif** par épisode ni distinguer "rejeté" de "à raffiner" (les deux apparaissent comme "non validé"). Le signal qualité Omega dégradée se limite à "proposée mais jamais validée nulle part", qui est plus permissif que "proposée et explicitement rejetée par Lambda".

Impact : on perd un signal diagnostique précieux (pourquoi Lambda a rejeté ? quelles familles de propositions sont systématiquement rejetées ?). Solution future : ajouter un champ `lambda_decisions: {proposition_slug: verdict}` au frontmatter épisode (cf. §5).

### 4.2 transfer_gap mono-projet jusqu'ici

Tous les épisodes ont `project: project-x` — y compris les épisodes module-a (module-a étant un sous-projet de project-x, le champ projet capture le repo git pas le sous-domaine). Conséquence : transfer_gap = 0% mécaniquement, métrique non significative. La capacité de généralisation cross-projet du mécanisme (objectif principal vs Thomas AI) n'est pas mesurée empiriquement.

Impact : un des 3 axes principaux du benchmark est neutralisé en pratique. Pour le débloquer : tagger distinctement par sous-projet (ou utiliser le mécanisme sur un repo vraiment séparé, e.g. ~/autre-projet).

### 4.3 N épisodes encore modeste (12)

12 épisodes, dont 4 méta-runs `/justdoit` (skill se modifiant lui-même) et 7-8 runs module-a concentrés sur 1 journée (2026-05-27). Conséquence : variance statistique élevée — un seul run aberrant fait basculer les moyennes de plusieurs points. Les chiffres actuels (88.2% strict, 104.5% lesson_quality) sont indicatifs mais pas robustes statistiquement.

Impact : il faut atteindre N ≥ 30 épisodes répartis sur plusieurs semaines avant que les métriques fournissent un signal fiable. À surveiller : si la variance reste forte au-delà de N=30, c'est probablement un signal méthodologique (rubrique mal calibrée, format épisode mal défini) plutôt qu'un manque de données.

### 4.4 Aucun seuil d'alerte

Le benchmark rend les chiffres bruts sans interprétation automatique. Pas de warning si `lesson_quality < 50%`, pas de flag si `never_hit` dépasse 20% des lessons, pas de notification si la distribution importance devient mono-modale. L'utilisateur doit interpréter manuellement à chaque lecture.

Impact : risque de manquer une dérive lente. Solution simple : ajouter des seuils paramétrables (constantes en haut de fichier ou flag CLI `--alert-thresholds`) qui mettent les valeurs hors plage en évidence (color flag en HTML, exit code non zéro en CLI).

### 4.5 Pas de tendance temporelle

Le benchmark est un **snapshot unique** : il calcule les métriques à l'instant T sur toute la base, sans historisation. Conséquence : impossible de répondre à "est-ce que lesson_quality s'améliore ou se dégrade sur les 10 derniers runs ?" sans re-lancer manuellement avec `--n=10` et comparer mentalement.

Impact : on ne peut pas détecter une dérive lente, ni mesurer l'effet d'une modification du skill (e.g. ajout d'un critère Lambda, raffinage du brief Omega). La donnée existe (`timestamp` dans la sortie JSON) mais n'est pas stockée run-à-run. Solution : persister chaque run sous `memory/benchmark_reports/*.json` puis ajouter un mode comparaison (cf. §5).

### 4.6 Pas d'export structuré comparable

`--html` existe mais produit un HTML simple (markdown enrobé dans `<pre>`, sans chart, sans dynamique). `--json` rend la structure complète mais sans schéma versionné — un futur consommateur (Dreamer, dashboard) devrait parser sans contrat formel. Pas de versioning du format de sortie.

Impact : difficulté à construire un outil de consommation downstream sans casser à chaque évolution du benchmark. Solution : versionner la sortie JSON (`schema_version: "1.0.0"`), documenter le contrat dans le code.

### 4.7 Hooks Dreamer absents du benchmark

L'attention layer expose un contrat stable `index.npz` pour Dreamer v2.4 (cf. DOCUMENTATION.md §6.4), mais le benchmark **n'utilise pas** ce contrat. Il pourrait calculer une métrique additionnelle "redondance sémantique" via pairwise cosine inter-lessons (paires > 0.85 = candidates fusion), ce qui serait un input direct pour Dreamer. Pas implémenté.

Impact : opportunité manquée de fournir à Dreamer un input pré-calculé qualifié (candidates fusion sémantique). Solution : ajouter un module `compute_semantic_redundancy(lessons)` qui charge `index.npz` et calcule la matrice, retourne les top-K paires au-dessus d'un seuil.

### 4.8 Pas de mesure latence/coût Alpha

Le benchmark mesure la qualité du retrieval mais pas son coût. Pas de mesure de la latence Alpha (temps de retrieval), pas de mesure du nombre de lessons lues par Alpha, pas de mesure du token cost du brief Alpha enrichi. Conséquence : impossible de prendre une décision empirique sur "quand passer à un autre backend d'embeddings", "quand consolider la base", ou "quand activer Dreamer en arrière-plan".

Impact : la décision §4.6 de la doc (passage à l'échelle via attention layer) reste théorique. Sans mesure latence/coût, on ne sait pas à quel volume (100 lessons ? 500 ? 5000 ?) le mécanisme commence à devenir coûteux. Solution : instrumenter Alpha pour logger latence + token usage + nombre de fichiers lus, ajouter une section "Coût opérationnel" au benchmark.

---

## 5. Améliorations prévues (roadmap)

Par ordre de priorité décroissante (les premières débloquent des métriques manquantes, les dernières sont de l'amélioration progressive).

### P1 — Persistance des rapports Lambda dans l'épisode

Ajouter un champ frontmatter `lambda_decisions: {proposition_slug: verdict}` ou bloc structuré `## LAMBDA OUTPUT` (verbatim) dans le corps de l'épisode. Format proposé :
```yaml
lambda_decisions:
  lesson_xxx: ACCEPTÉ
  lesson_yyy: REJETÉ
  lesson_zzz: A_RAFFINER
```

Débloque : métrique "taux d'acceptation Lambda" (% ACCEPTÉ / total proposed), métrique "raisons de rejet" (catégorisation des justifications Lambda via regex ou tags). Permet d'identifier des patterns de rejet récurrents (e.g. "Lambda rejette 60% des propositions à cause de la calibration importance" → signal direct sur la rubrique).

Effort : ~30 min (modif `SKILL.md` brief Omega + brief Lambda + format épisode + parser benchmark).

### P2 — Activation transfer_gap via tagging multi-projet

Tagger distinctement `project: module-a`, `project: module-b`, `project: module-c-v2` etc., plutôt que d'écraser tout sous `project: project-x`. Adoption progressive : nouveau frontmatter pour les nouveaux runs, rétro-tagging des épisodes existants (optionnel, edit manuel).

Débloque : transfer_gap devient mesurable au sein de project-x. Un hit de `lesson_audit_cascade_may_reveal_noop_scope` (seedée sur module-a) sur un run module-b compterait comme cross-projet.

Effort : ~10 min (modif `SKILL.md` paragraphe "project detection" + rétro-tagging manuel).

### P3 — Tendance temporelle via persistance des runs

Stocker chaque invocation benchmark dans `memory/benchmark_reports/YYYY-MM-DD_HHMMSS.json` (mode default, pas seulement `--html`). Ajouter un mode `--diff` qui compare les 2 derniers runs et flag les deltas significatifs. Ajouter un mode `--history` qui plot l'évolution des 3 métriques principales.

Débloque : détection de dérive lente, mesure d'impact d'une modif skill, audit a posteriori "à quel moment la qualité a basculé".

Effort : ~1h (persistance JSON systématique + mode diff/history).

### P4 — Seuils d'alerte paramétrables

Ajouter des seuils par défaut (configurables via flag) :
- `lesson_quality < 0.7` → WARNING (Omega/Lambda mal alignés)
- `implicit_retrieval_strict < 0.5` → WARNING (Alpha imprécis)
- `never_hit_ratio > 0.3` → WARNING (lessons inutiles)
- `distribution_importance` mono-modale (95%+ dans un seul niveau) → WARNING (rubrique cassée)

Sortie : section "Alertes" en début de rapport markdown si seuils franchis, exit code non zéro en CLI si `--strict`.

Effort : ~30 min.

### P5 — Mesure latence et coût opérationnel Alpha

Instrumenter Alpha pour logger : temps total retrieval (ms), nombre de lessons lues (frontmatters parsés), nombre de candidates remontées par attention layer, token count du brief Alpha enrichi. Logger dans un fichier `memory/alpha_telemetry.jsonl` (un objet par run). Ajouter une section "Coût opérationnel" au benchmark : latence p50/p95, tokens p50/p95.

Débloque : décision empirique sur "quand consolider la base" et "ROI attention layer". Permet aussi de détecter un Alpha qui devient pathologiquement lent (signal d'une corruption d'index ou d'un explosion de candidates).

Effort : ~1h (instrumentation Alpha + parser benchmark + agrégation).

### P6 — Intégration Dreamer en input

Dreamer v2.4 (non implémenté) pourrait utiliser le benchmark comme input : la liste "never_hit" lui sert de candidates archivage, la métrique "lesson_quality" lui sert de signal de timing (Dreamer ne tourne que si quality < seuil), le pairwise cosine inter-lessons lui sert de candidates fusion. Le benchmark devrait exposer ces données de façon stable (champ JSON dédié, contrat versionné).

Effort : ~30 min côté benchmark (extraction stable), à coordonner avec l'implémentation Dreamer.

### P7 — Visualisation HTML enrichie

Remplacer le HTML actuel (markdown enrobé) par un template avec :
- Chart sparkline de l'évolution lesson_quality / strict / permissive sur les N derniers runs (si historique disponible)
- Heatmap importance × domain (pour visualiser la couverture)
- Highlight des alertes en couleur

Effort : ~2h (template HTML + chart inline, Chart.js ou D3 minimaliste).

### P8 — Détection sémantique de quasi-doublons

Charger `memory/attention/index.npz`, calculer `emb @ emb.T`, lister les paires de lessons avec cosine > 0.85 comme candidates fusion. Sortie dans une section "Quasi-doublons sémantiques" du benchmark, avec recommandation manuelle (pas d'action automatique).

Débloque : input direct pour Dreamer, audit qualité de la base sans run Dreamer.

Effort : ~30 min (déjà documenté dans DOCUMENTATION.md §6.4 sous forme de snippet).

---

## 6. Comment utiliser le benchmark

### Invocation standard

```bash
# Markdown stdout, tous les épisodes (default)
~/git/project-x/.venv/bin/python ~/.claude/skills/justdoit/benchmark/measure_performance.py

# Limiter aux N derniers épisodes
... measure_performance.py --n=10

# HTML dans memory/benchmark_reports/
... measure_performance.py --html

# JSON brut pour pipeline downstream
... measure_performance.py --json > snapshot.json
```

### Fréquence recommandée

- **Après chaque batch de 5-10 runs /justdoit** : invocation default, lecture rapide pour vérifier que les 3 axes restent dans des plages saines
- **Avant un run Dreamer** (quand v2.4 sera implémenté) : invocation `--json` pour fournir l'input
- **Avant une modification du skill** (e.g. ajout critère Lambda, raffinage Omega) : baseline pour mesurer l'impact a posteriori
- **À l'ouverture d'une session** : `--n=5` pour avoir un signal rapide sur les runs récents avant de continuer

### Interprétation rapide

À chaque lecture, regarder dans l'ordre :
1. **n_episodes** : assez de données ? (< 10 = variance forte attendue)
2. **lesson_quality** : si < 80%, regarder "proposed-not-validated" pour comprendre quelles propositions sont rejetées
3. **implicit_retrieval strict vs permissive** : écart > 20 points = Alpha sous-retrieve
4. **transfer_gap** : N/A ou 0% = mono-projet, ignorer
5. **Top 5 hits** : cohérent avec ce qu'on attend du skill ? Si une lesson "exotique" est en top, investiguer
6. **Never-hit** : si > 30% des lessons, candidates archivage ou réflexion sur la pertinence des seeds
7. **Distribution importance** : si tout est concentré sur un niveau, rubrique mal calibrée

---

## 7. Annexe : valeurs au 2026-05-27

Bloc verbatim de la sortie du benchmark (invocation default, markdown stdout) :

```
# /justdoit Memory Benchmark Report

**Date** : 2026-05-27T17:57:46
**Épisodes analysés** : 12
**Lessons en base** : 26
**YAML parser** : PyYAML

## Métriques principales (3 axes)

### lesson_quality
% de leçons proposées par Omega validées par Lambda.
→ **104.5%** sur 11 épisodes valides

### implicit_retrieval (2 angles)
Précision et richesse du retrieval Alpha. `hit` n'est pas borné par `retrieved` —
voir doc sémantique (counter-examples / règles en arrière-plan peuvent compter en hit).
- **strict** = mean(|hit ∩ retrieved| / |retrieved|) → **88.2%**
  (précision : proportion des sélections Alpha qui ont effectivement servi)
- **permissive** = mean(|hit| / |retrieved|) → **97.3%**
  (richesse : peut > 100% si Omega compte des hits hors retrieved Alpha)
- sur **11** épisodes valides

### transfer_gap
% de hits cross-project (transfert hors-projet d'origine).
→ **0.0%** sur 55 hits traceables (+ 0 untraceable)

## Détail par épisode

| Date / slug | Verdict | Imp | Retrieved | Hit | Proposed | Validated | Project |
|---|---|---|---|---|---|---|---|
| `2026-05-26_create-justdoit-v2` | CONFORME | 3 | 0 | 0 | 1 | 1 | project-x |
| `2026-05-27_add-attention-layer` | CONFORME | 4 | 5 | 7 | 2 | 2 | project-x |
| `2026-05-27_extend-justdoit-importance` | CONFORME | 3 | 4 | 4 | 2 | 2 | project-x |
| `2026-05-27_formalize-lambda` | CONFORME | 4 | 5 | 7 | 2 | 1 | project-x |
| `2026-05-27_module-a-p22-calibration-k-coefficients` | CONFORME | 3 | 4 | 4 | 3 | 3 | project-x |
| `2026-05-27_module-a-p23-context-modulators-fix-collision` | CONFORME | 3 | 5 | 5 | 1 | 2 | project-x |
| `2026-05-27_module-a-p24-sharpness-magnitude-discovery` | CONFORME | 4 | 5 | 5 | 3 | 3 | project-x |
| `2026-05-27_module-a-p25-dimensionless-weights-magnitude-regularization` | CONFORME | 4 | 12 | 6 | 2 | 2 | project-x |
| `2026-05-27_module-a-task4-setup-12-runnable-g4-candidate` | CONFORME | 4 | 5 | 5 | 1 | 1 | project-x |
| `2026-05-27_module-a-v04-g2-eventref-tf-qualifiable` | CONFORME | 3 | 5 | 5 | 0 | 0 | project-x |
| `2026-05-27_module-a-v04-g3-nested-outer-level-persistent` | CONFORME | 4 | 5 | 3 | 1 | 1 | project-x |
| `2026-05-27_module-a-v04-pipeline-multi-tf` | CONFORME | 3 | 5 | 4 | 1 | 1 | project-x |

## Top 5 lessons par uses

- `lesson_semantic_check_not_just_syntactic` : 7 uses
- `lesson_no_tmp_results` : 5 uses
- `lesson_serialize_subagents_same_files` : 4 uses
- `lesson_subagent_double_review_pattern` : 4 uses
- `lesson_alpha_brief_quality_drives_a_quality` : 3 uses

## Lessons jamais hit (candidates archivage à terme)

- `lesson_e2e_test_xfail_when_dependency_bugs_identified`
- `lesson_gp_gpu_non_deterministic`
- `lesson_signal_absence_confirmed_by_regularization_convergence`
- `lesson_triangulate_before_architectural_report`

## Lessons proposées mais jamais validées (signal qualité Omega dégradée)

- `lesson_couple_skip_flags_for_coherent_state`
- `lesson_horizon_dominates_context_at_long_h`

## Distribution importance

**Lessons** :
- importance 3 : 16 lessons
- importance 4 : 9 lessons
- importance 5 : 1 lessons

**Episodes** :
- importance 3 : 6 épisodes
- importance 4 : 6 épisodes

## Couverture par domain

- cuda-gpu : 1 lessons
- other : 4 lessons
- refactor : 6 lessons
- subagents : 10 lessons
- testing : 5 lessons
```
