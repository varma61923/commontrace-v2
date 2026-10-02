# Amendment 1 to the pre-registration (written before the first EVAL occasion)

Original: sha256 ea03bc827abac6c883fce0e05526ca60f74fc4491c7a9bc35d877eea9d7b85a0

What happened in the baseline period (6 TRAIN tasks, no memory, real subagent runs):
- 6/6 passed the hidden suite. The model reads the library's docstrings and avoids every gotcha.
  Consequence: on this bench the no-memory arm has ~no correctness headroom, so the primary outcome (pass rate)
  cannot show a benefit. It remains the primary outcome (a loss would still show); tokens, time and tool calls
  are the outcomes with room to move. This is a finding about the bench, not an adjustment of it.
- Lessons were therefore written from the team's documented conventions, not from observed failures.

Changes, made before any EVAL run:
1. Replications: each EVAL task runs 4 times (48 occasions) instead of 2, because a run costs ~10 s and ~52k tokens.
2. The poisoned-lesson (prompt injection) arm was dropped: the environment's permission policy blocked writing a
   lesson containing a shell-pipe payload. Security is instead measured by (a) hidden injection/traversal checks
   on the agents' code, (b) a static scan of produced solutions, (c) the repository's existing content-screen and
   tenant-isolation test suites, not by planting a payload.
3. Retrieval was characterised before the run (no agents): the right lesson was returned for 12/12 EVAL queries;
   63 lessons were returned across the 12 queries (12 of the matching class), i.e. injected context contains
   unrelated lessons. This is reported as-is.

Unchanged: arms, prompts, graders, analysis plan, exclusion rule, model, tools.
A second bench with rules that exist ONLY in memory (not in code or spec) is a separate experiment with its own
registration; it measures the mechanism (retrieve, inject, apply), not how often real work needs such rules.
