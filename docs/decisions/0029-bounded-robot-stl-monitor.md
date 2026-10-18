# Decision 0029: bounded runtime STL monitor

Use an explicit JSON formula tree and discrete sampled robustness for bounded
`always`, `eventually`, and `until`. Tie each spec to memory provenance. Unknown
or unsafe outcomes hold and expose recovery lineage. Keep sensor acquisition,
motor interlocks, certification, and safety-rated controls outside this module.
