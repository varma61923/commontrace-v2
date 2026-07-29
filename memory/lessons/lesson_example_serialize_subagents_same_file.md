---
name: lesson_example_serialize_subagents_same_file
description: Quand deux sous-agents doivent éditer le même fichier, les sérialiser (jamais en parallèle) pour éviter l'écrasement silencieux
tags: [subagents, orchestration, concurrency, example]
domain: subagents
importance: 5
importance_rationale: "Deux sous-agents parallèles sur le même fichier s'écrasent mutuellement sans erreur ; la perte de travail est silencieuse et coûteuse."
importance_history: []
applies_when: l'orchestrateur s'apprête à lancer ≥ 2 sous-agents dont les périmètres de fichiers se recoupent
do_not_apply_when: les sous-agents travaillent sur des fichiers strictement disjoints
uses: 0
last_hit: NEVER
source_episodes: [2026-07-01_example-api-pagination]
status: active
---

> ⚠️ **Exemple illustratif (donnée fictive).** Ce fichier montre le FORMAT d'une leçon
> capitalisée ; ce n'est pas une leçon issue d'un vrai run. À supprimer une fois vos
> propres leçons accumulées.

## Rule
Deux sous-agents qui touchent le même fichier doivent s'exécuter en série (A termine et commit, puis B), jamais concurremment. Si un fan-out parallèle est nécessaire, partitionner d'abord le travail par fichier.

## Why
Deux écritures concurrentes sur un même fichier via des workspaces séparés se terminent par un « last write wins » : le second commit écrase le premier sans conflit ni message d'erreur. Le pattern A+B de `/justdoit` sérialise déjà A→B pour cette raison ; la règle généralise à tout fan-out.

## How to apply
Dans l'orchestrateur : avant un `parallel(...)`, calculer l'intersection des périmètres de fichiers annoncés. Si non vide → basculer sur une exécution séquentielle (ou isolation par worktree + merge explicite). Sinon seulement, paralléliser.

## Counter-examples
Ne s'applique pas si chaque sous-agent a un périmètre de fichiers disjoint (ex. un agent par module indépendant), auquel cas le parallélisme est sûr et souhaitable.
