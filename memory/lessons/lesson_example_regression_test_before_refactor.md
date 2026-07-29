---
name: lesson_example_regression_test_before_refactor
description: Écrire un test de non-régression capturant le comportement actuel AVANT de lancer le refactor, pas après
tags: [testing, refactor, regression, example]
domain: testing
importance: 4
importance_rationale: "Sans test de capture préalable, B ne peut pas distinguer un changement de comportement voulu d'une régression silencieuse."
importance_history: []
applies_when: le brief A demande un refactor / réécriture d'un module dont le comportement observable doit rester identique
do_not_apply_when: création d'une fonctionnalité neuve sans comportement antérieur à préserver
uses: 1
last_hit: 2026-07-01
source_episodes: [2026-07-01_example-api-pagination]
status: active
---

> ⚠️ **Exemple illustratif (donnée fictive).** Ce fichier montre le FORMAT d'une leçon
> capitalisée ; ce n'est pas une leçon issue d'un vrai run. À supprimer une fois vos
> propres leçons accumulées.

## Rule
Avant tout refactor à comportement constant, faire écrire par A un test qui capture le comportement actuel (golden/caractérisation) et le faire passer sur le code AVANT modification.

## Why
Sur le run `2026-07-01_example-api-pagination`, le refactor de la pagination aurait pu changer l'ordre des résultats sans que rien ne le signale. Le test de caractérisation écrit d'abord a servi d'oracle : B a pu vérifier que le comportement observable était préservé plutôt que de relire ligne à ligne.

## How to apply
Dans le brief A : « Étape 1 — écris un test qui fige le comportement actuel de `<module>` et vérifie qu'il PASSE sur le code non modifié. Étape 2 seulement — refactore. Le test doit rester vert. » Dans le brief B : exiger la preuve que le test existait et passait avant le diff.

## Counter-examples
Ne s'applique pas quand il n'y a pas de comportement antérieur (feature neuve), ni quand le refactor CHANGE volontairement le comportement — dans ce cas, le test doit être mis à jour explicitement et le diff du test fait partie de la revue.
