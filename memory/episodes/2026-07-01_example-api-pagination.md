---
name: 2026-07-01_example-api-pagination
description: Ajout d'une pagination par curseur à un endpoint REST de liste, via le pattern A+B
task_invocation: /justdoit ajoute une pagination par curseur à GET /items (limit + cursor opaque, ordre stable), critères — comportement existant préservé, test de non-régression vert, doc OpenAPI à jour
tags: [api, rest, pagination, refactor, example]
project: demo-api
verdict: CONFORME
importance: 3
importance_rationale: "Run de démonstration du pattern A+B sur une tâche de refactor à comportement partiellement constant ; sert d'ancre aux deux leçons exemples."
n_iterations: 1
commit_sha: 0000000
duration_minutes: 18
lessons_retrieved_by_alpha: [lesson_example_regression_test_before_refactor]
lessons_hit: [lesson_example_regression_test_before_refactor]
lessons_proposed_by_omega: [lesson_example_serialize_subagents_same_file]
lessons_validated_by_lambda: [lesson_example_serialize_subagents_same_file]
---

> ⚠️ **Exemple illustratif (donnée fictive).** Cet épisode montre le FORMAT ;
> ce n'est pas un vrai run. À supprimer une fois vos propres épisodes accumulés.

## What happened
A a d'abord écrit un test de caractérisation figeant la réponse actuelle de `GET /items` (ordre + payload), l'a fait passer sur le code non modifié, puis a introduit la pagination par curseur (paramètres `limit` et `cursor` opaque, ordre stable sur `(created_at, id)`). Commit immédiat. B, reviewer indépendant, a rejoué le test de caractérisation et vérifié que la première page sans curseur reproduisait exactement l'ancien comportement, puis a contrôlé la stabilité du curseur sur insertion concurrente. Verdict CONFORME en 1 itération.

## What surprised me
Alpha avait remonté `lesson_example_regression_test_before_refactor` avec confidence haute dès la Phase 0 ; le brief A l'a donc intégrée d'emblée, ce qui a évité l'aller-retour habituel « B réclame un test de non-régression manquant ».

## What worked well
- Test de caractérisation écrit AVANT le refactor → oracle net pour B (leçon `regression_test_before_refactor` effectivement hit).
- Curseur opaque (base64 de `(created_at, id)`) plutôt qu'offset → pas de saut/duplication sous insertion.

## What worked less well
- Omega a noté qu'A et B ont failli éditer `openapi.yaml` en parallèle (doc + exemple de réponse) → proposition de la leçon `lesson_example_serialize_subagents_same_file`, validée par Lambda.
