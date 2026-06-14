# Attention layer (v2.3)

Pre-filter rapide pour Alpha (Phase 0) basé sur embeddings sémantiques locaux.
Permet à Alpha de passer à l'échelle (100+ leçons) sans dégrader la qualité du
retrieval ni la latence.

## Rôle dans le pipeline

Alpha (Phase 0) lit la base mémoire pour identifier les leçons pertinentes
pour la tâche entrante. Sans pré-filtre, Alpha doit lire le frontmatter de
toutes les leçons à volume croissant. Avec attention :

1. `query.py "tâche"` retourne ~10 candidats par similarité cosine + toutes
   les leçons `importance >= 4` (override sécurité)
2. Alpha lit ces candidats en profondeur (frontmatter + corps), applique le
   filtre sémantique `applies_when` / `do_not_apply_when` et le tri par
   `score = importance × tag_match`.

Le score cosine est un **complément** au tri qualitatif, pas un remplacement.
Alpha continue de juger applicabilité + qualité.

## Modèle utilisé

`multi-qa-mpnet-base-dot-v1` (sentence-transformers, ~420 MB, 768 dim).
Optimisé pour Q&A retrieval. Téléchargé une fois depuis HuggingFace dans
`~/.cache/huggingface/`, puis exécuté **strictement en local** — pas d'appel
API runtime, pas de télémétrie. Conforme `feedback_code_strictement_prive`.

## Format `index.npz`

Stocké sous `memory/attention/index.npz`. Contrat stable :

| Champ           | Type             | Sémantique                                         |
|-----------------|------------------|----------------------------------------------------|
| `slugs`         | `np.ndarray[str]`| Identifiants leçons, ordonnés                      |
| `embeddings`    | `np.ndarray[N,D]`| Embeddings L2-normalisés (cosine = dot product)    |
| `model_name`    | `str`            | `"multi-qa-mpnet-base-dot-v1"`                     |
| `encoded_field` | `str`            | Schéma lisible des champs encodés                  |
| `timestamp`     | `str`            | ISO-8601 build time                                |
| `n_lessons`     | `int`            | Nombre de leçons actives indexées                  |

Texte encodé par leçon : concaténation de `description + domain + tags +
applies_when + do_not_apply_when + rule` (séparateur ` | `, labels explicites).

## Override sécurité importance ≥ 4

Décision design figée v2.3 : toute leçon active avec `importance >= 4` est
**toujours incluse** dans le set retourné par `query.py`, même si absente du
top-K cosine. Garantit qu'une leçon critique / showstopper n'est jamais
silencieusement écartée par une query orthogonale. Floor configurable via
`--include-importance-floor=N`.

## Trigger rebuild

- **Automatique (Phase 11)** : l'orchestrateur lance `build_index.py` à la
  fin de Phase 11 si Lambda a appliqué au moins une création / update /
  révision de leçon (ajout, status change, ou rule modifiée).
- **Manuel** : `python build_index.py --force` (rebuild systématique, utile
  après édition manuelle, archivage, ou fusion de leçons).

Le mode `--force` ignore le check de fraîcheur (mtime).

## Hooks Dreamer (v2.4, NON implémenté ici)

Le format `index.npz` est intentionnellement exposé pour réutilisation par
un futur agent **Dreamer** (v2.4). Cas d'usage anticipé : Dreamer détecte
des candidats de fusion entre leçons existantes via similarité cosine
**inter-leçons** (sim > 0.85 = candidat fusion). Concrètement :

```python
import numpy as np
data = np.load("memory/attention/index.npz", allow_pickle=True)
emb = data["embeddings"]                    # déjà L2-normalisés
sim_matrix = emb @ emb.T                    # cosine pairwise (N x N)
candidates = np.argwhere((sim_matrix > 0.85) & (sim_matrix < 1.0))
# Dreamer propose ensuite les fusions à Lambda pour validation
```

Aucun code Dreamer n'est livré en v2.3 — seul le contrat `index.npz` est
stable et documenté pour permettre l'évolution.

## Anti-patterns à éviter

- Ne pas utiliser cosine comme remplacement du jugement Alpha (juste comme
  pré-filtre + complément de tri)
- Ne pas dégrader `query.py` en dump cosine sans le contexte importance
- Ne pas oublier le rebuild après édition manuelle d'une leçon (sinon
  Alpha utilise un index obsolète — `--force` à la rescousse)
- Ne pas substituer une dépendance externe (FAISS, hnswlib, chroma) à
  numpy : numpy suffit jusqu'à 10k+ leçons. KISS.
