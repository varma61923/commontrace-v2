# Third-party notices

UniMem is licensed under AGPL-3.0-only. Parts of it are adapted from the permissively licensed projects listed below. Apache-2.0 and MIT code may be included in an AGPL-3.0 work. Each license's conditions are met by keeping the notices below. Files containing adapted code also say so in their headers.

Techniques that are only cited in docstrings (re-implemented from papers or public descriptions, with no code carried over) are not listed here.

The optional `unimem[crdt]` extra depends on the upstream Automerge Python
bindings (MIT, pinned development release) and `cryptography`; neither project's
implementation code is copied into this repository. No Automerge wire format or
cross-language interoperability claim is made without matching fixture tests.

---

## Mem0

- **Project:** https://github.com/mem0ai/mem0
- **License:** Apache License 2.0 (full text in [`LICENSES/Apache-2.0.txt`](LICENSES/Apache-2.0.txt))
- **Copyright:** Copyright 2023 Taranjeet Singh (as stated in the Mem0 LICENSE)
- **Used in:** `unimem/hierarchical.py`. The entity-candidate extraction heuristics are adapted and modified from `mem0/utils/entity_extraction.py`.

## Hindsight

- **Project:** https://github.com/vectorize-io/hindsight
- **License:** MIT
- **Used in:** `unimem/defense.py`. The memory-defense pattern catalogue, the policy actions and the fingerprinted previews are adapted and modified from `hindsight_api/extensions/memory_defense.py`.

```
MIT License

Copyright (c) 2025 Vectorize AI, Inc.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Cognee

- **Project:** https://github.com/topoteretes/cognee
- **License:** Apache License 2.0 (full text in [`LICENSES/Apache-2.0.txt`](LICENSES/Apache-2.0.txt))
- **Used in:** `unimem/sql_guard.py`. The single-statement and read-only checks for model-written SQL are adapted and modified from `cognee/modules/tools/text_to_sql/sql_guard.py`.
- **NOTICE (reproduced as Apache-2.0 §4(d) requires):**

  > topoteretes - cognee
  > Copyright © 2024 Topoteretes UG. (haftungsbeschränkt), Schonehauser Allee 163, Berlin.
  >
  > This project includes software developed at Topoteretes UG. (https://www.cognee.ai/).

## EverOS

- **Project:** https://github.com/EverMind-AI/EverOS
- **License:** Apache License 2.0 (full text in [`LICENSES/Apache-2.0.txt`](LICENSES/Apache-2.0.txt))
- **Used in:** `benchmarks/judges/locomo.py`. This is the binary LoCoMo judge prompt used in the Mem0 and EverOS published evaluations, kept verbatim so that scores stay comparable.
- **NOTICE (reproduced as Apache-2.0 §4(d) requires):**

  > EverOS
  > Copyright 2025 EverMind AI
  >
  > This product includes software developed by EverMind AI.

## Benchmark prompts

`benchmarks/judges/beam.py` and `benchmarks/judges/longmemeval.py` reproduce evaluation prompts verbatim from the benchmarks' own repositories so that scores stay comparable. Each file header names the source repository and the pinned commit.

No benchmark *datasets* are distributed with UniMem. LoCoMo (snap-research/locomo) is licensed CC BY-NC 4.0 and must not be bundled into a commercial distribution.
