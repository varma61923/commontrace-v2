# Validated targeting

`frontier uplift-fit` accepts pre-treatment feature dictionaries and randomized
binary outcomes, with a fixed `training_size`. Features include context, tool,
model version, embodiment, gripper, site and object class. A categorical shrunk
DR learner uses the training slice; a disjoint fixed validation slice tests a
prespecified top-half TOC (a two-point RATE weighting) with a conservative lower
bound. This specialization is not the full continuous AUTOC estimator.

`Targeting.decide` returns the empirical-Bayes fallback when validation fails.
When validated, it retains a nonzero untargeted randomized exploration slice.
Class-specific simultaneous positive lower bounds prevent transfer into a
measured harmful gripper/site class. Unknown classes receive low randomized
injection probability, rather than an unsupported positive claim.

`uplift.activate` writes a signed map only for validated positive classes. The
normal `allocate plan` path incorporates this map in its next signed schedule;
retrieval never changes propensities mid-era. Map context labels to the same
pre-treatment labels supplied at retrieval. A Hub deployment can transport the
signed class map with its tenant-owned lesson; this package does not bypass Hub
lesson admission or automatically publish lessons to other tenants.

Server-side `PostgresJournal.put_targeted` verifies the signed policy through a
trusted coordinator's pinned verifier, binds the exact memory and environment,
and refuses unmeasured feature classes. It retains site/gripper/embodiment labels
in the tenant-owned record and writes through the existing audit/record path.
An ordinary `put` is a storage operation, not authorization for fleet rollout.
