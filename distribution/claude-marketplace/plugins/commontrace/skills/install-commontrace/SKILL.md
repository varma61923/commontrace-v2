---
name: install-commontrace
description: Install CommonTrace and set up a store for this project. Use when the user asks to install, set up or try CommonTrace, or to measure whether their agent's memory helps.
---

Install and check the CLI, then create a store for the function this agent performs.

```bash
python3 -m pip install commontrace
commontrace doctor
commontrace function list          # support, sales, hr, coding, marketing, robotics, legal, finance, clinical
commontrace init --function <the closest one> --dest .
```

Ask the user which function fits and how many tasks (tickets, pull requests, episodes) the agent handles per day.
Never pick a function or a volume for them. Then use the `prove-memory` skill.

Nothing is withheld from the agent until a proof is started on purpose. If `commontrace doctor` reports a problem,
show it to the user as printed.
