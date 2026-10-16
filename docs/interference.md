# Cluster and switchback experiments

Publish `frontier cluster-publish` with `identity`, `lesson`, and a fixed `rate`
matching the configured experiment. The design captures baseline memory ids and
salt before its occasions. Adaptive allocation is refused for this design.
`assign_cluster` uses the existing canonical assignment kernel once per cluster.
For switchbacks also preregister `block_length` and `burn_in`, then supply `period`
to assignment. Repeated steps share their block's action; burn-in is ineligible
for outcome aggregation. Record one bounded aggregate outcome per eligible
randomized block, not one independent observation per step.

Trusted coordinators wrap reads and derived writes in
`interference.reader_scope(root, design, cluster, period=...)`. Lineage tags writes
with experiment and arm. A holdout reader is refused exactly the memories that are,
or descend from, treated-arm derivations of that design (a summary of a treated
workaround included); everything else, including memories written outside the
design, stays visible to both arms, so diversion cannot itself create a contrast.
A design records its lineage position, never an enumeration of store ids. Hazard context is exempt. A caller that bypasses the governed store or
fails to establish the coordinator scope is outside this isolation boundary.

`cluster_effect` targets the equal-cluster ATE using HT contrasts and a
finite-sample time-uniform Hoeffding bound. Declare a propensity floor in advance.
Independent randomized clusters and no interference across their boundaries are
required. A switchback additionally assumes the washout removes carryover.
`symbiosis_bias` compares cluster and task estimates as a diagnostic, integrated
into `integrity.audit(cluster_comparison=...)`; it does not prove contamination.
