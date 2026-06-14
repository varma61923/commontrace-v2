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

*(Base mémoire vide — scaffold de départ. Les domaines et leurs entrées sont créés
automatiquement par l'orchestrateur en Phase 11 au fil des runs `/justdoit`, ou
manuellement. Domaines prévus : Git/Safety, CUDA/GPU, Refactor, Testing, Subagents,
Performance, Other.)*
