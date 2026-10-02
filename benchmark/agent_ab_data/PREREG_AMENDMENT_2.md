# Amendment 2 (written before any EVAL agent run; no outcome had been observed)

Mechanics discovered while preparing occasions: CommonTrace's holdout is per LESSON per occasion (each eligible
lesson independently withheld at 50%), not per occasion. With several eligible lessons almost every occasion has
some memory injected, so "any memory vs none" has almost no control group.

Therefore the primary contrast is the one CommonTrace's own `experiment` estimates: for each occasion, was the
RELEVANT lesson (the one written for that task's gotcha class) injected or withheld? Other eligible lessons are
injected or withheld independently and equally in both groups, so the contrast stays unbiased for the relevant
lesson; their presence is part of the realistic injected context. Occasions where the relevant lesson was not
eligible at all are reported separately (a retrieval miss) and excluded from the contrast.
"Any memory injected vs none" is reported descriptively only.

Parsing fix: the first preparation script counted `[WITHHELD - holdout]` lines as injected. All occasions were
re-prepared after the fix and the randomization was reset; no agent had run on the wrong data.
Two probe queries leaked stray assignments; they were removed by resetting the salt before the real plan.

Everything else (arms, prompt, graders, analysis, model) unchanged.
