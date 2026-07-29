# Memory Index — `/justdoit`

Index hiérarchique par domaine de la base mémoire du skill `/justdoit` (lessons + episodes). Édité par l'orchestrateur en Phase 11 (après validation Lambda automatique des propositions Omega).

## Conventions d'usage

- **Qui édite** : l'orchestrateur de `/justdoit` en Phase 11 (après validation Lambda automatique). Édition manuelle autorisée pour affiner / archiver / fusionner (garder trace dans le commit message).
- **Quand** : à chaque création / update / révision de leçon validée par Lambda (Phase 11 auto), et à chaque écriture d'épisode (mention dans la section Episodes du domaine).
- **Comment ajouter une nouvelle catégorie de domaine** : éditer ce fichier (ajouter une section `## <Domain>`), éditer `memory/lessons/README.md` (ajouter la mention dans la liste hiérarchique), et utiliser ce domaine dans le champ `domain:` du frontmatter des leçons concernées.

## Format ligne

```
- [slug](relative/path/to/file.md) — rule en 1 phrase | tags: [a,b,c] | importance: N | uses: N | last_hit: YYYY-MM-DD or NEVER
```

Le champ `importance` est un entier 1-5 (rubrique complète dans `SKILL.md` section "Rubrique d'importance"). Single source of truth = le fichier `lesson_<slug>.md` lui-même ; cet INDEX.md le reflète. Pour les lignes sans `importance` (épisodes/lessons antérieurs v2.1), Alpha applique défaut 3 et flag "à caler".

## Sections par domaine

> ℹ️ Les entrées ci-dessous sont des **exemples illustratifs fictifs** (préfixe
> `example`) livrés pour montrer le format en action. Supprimez-les dès que vos
> propres leçons/épisodes s'accumulent. Domaines prévus : Git/Safety, CUDA/GPU,
> Refactor, Testing, Subagents, Performance, Other.

### Testing

#### Lessons
- [lesson_example_regression_test_before_refactor](lessons/lesson_example_regression_test_before_refactor.md) — Écrire un test de non-régression capturant le comportement actuel AVANT le refactor, pas après | tags: [testing, refactor, regression, example] | importance: 4 | uses: 1 | last_hit: 2026-07-01

#### Episodes
- [2026-07-01_example-api-pagination](episodes/2026-07-01_example-api-pagination.md) — Ajout d'une pagination par curseur à GET /items via le pattern A+B (CONFORME / 1 itération)

---

### Subagents

#### Lessons
- [lesson_example_serialize_subagents_same_file](lessons/lesson_example_serialize_subagents_same_file.md) — Sérialiser deux sous-agents qui éditent le même fichier pour éviter l'écrasement silencieux | tags: [subagents, orchestration, concurrency, example] | importance: 5 | uses: 0 | last_hit: NEVER

#### Episodes
*(voir l'épisode `2026-07-01_example-api-pagination` en Testing — c'est lui qui a fait émerger cette leçon en Phase 10/11)*
