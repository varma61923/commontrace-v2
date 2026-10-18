# Decision 0024: tenant and model bound KV capsules

Capsule keys bind the source-memory digest and model fingerprint under the local tenant identity key. The reference implementation uses injectable memory and disk pools and compression, while external accelerator pools remain deployment adapters. Source admission is rechecked at serving time; all-tier eviction runs on canonical erasure and emits G20 change events. Erasure receipts include tier outcomes.

Promotion scores and thresholds are operator-supplied policy values, not certified trial results. Closed APIs cannot use capsules. CacheBlend-style partial recomputation and remote adapter deletion guarantees remain external responsibilities.
