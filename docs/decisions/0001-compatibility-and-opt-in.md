# UniMem names, compatibility and opt-in controls

The product name is UniMem. Keep the `commontrace` import path and executable so
existing deployments and generated agent configurations continue to load.

New units use typed identities such as `skill:<digest>` and `writer:<agent>`.
They reuse canonical assignment, schedule, integrity, e-process and promotion
code. New metadata belongs to versioned records with distinct domain tags;
empty additions to existing record types are omitted from their canonical bodies.

New admission, lifecycle, isolation and robot-action controls require explicit
configuration or invocation. Existing stores do not acquire new withholding,
eviction, quarantine, hibernation or blocking behavior on upgrade. Protected
hazards remain outside discretionary trials and lifecycle actions.
