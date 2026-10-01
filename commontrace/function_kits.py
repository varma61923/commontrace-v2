"""The built-in function kits, as data. See commontrace/functions.py.

Baselines and effects are PLANNING ASSUMPTIONS used only to forecast how long a
verdict takes. They are not measurements and are shown as assumptions wherever
they appear. A function that needs different ones passes its own at the call.
"""
from __future__ import annotations

SPECS: tuple[dict, ...] = (
    {
        "key": "support", "title": "Customer support", "agent_type": "support",
        "occasion": {"label": "ticket", "example": "ZD-48213"},
        "outcome": {
            "success": "resolved without reopen within 7 days, CSAT of at least 4/5, "
                       "and no human takeover",
            "window_days": 7,
            "signals": ["from_no_reversal", "from_csat", "from_human_takeover"],
            "combine": "all",
        },
        "planning": {"baseline": 0.70, "effect": 0.05, "holdout_rate": 0.5},
        "domains": ["escalation", "refunds", "troubleshooting", "tone", "known-issues"],
    },
    {
        "key": "sales", "title": "Sales outreach", "agent_type": "sales",
        "occasion": {"label": "deal or contact", "example": "deal-90311"},
        "outcome": {
            "success": "a reply received, a meeting booked, or the stage advanced within 14 days",
            "window_days": 14,
            "signals": ["from_event_within_window"],
            "combine": "single",
        },
        "planning": {"baseline": 0.20, "effect": 0.05, "holdout_rate": 0.5},
        "domains": ["objection-handling", "pricing", "qualification", "competitor", "follow-up"],
    },
    {
        "key": "hr", "title": "HR and recruiting", "agent_type": "hr",
        "occasion": {"label": "candidate or requisition", "example": "cand-7741"},
        "outcome": {
            "success": "the candidate responded, an interview was scheduled or an offer accepted "
                       "within 14 days; or an HR helpdesk question was resolved without escalation",
            "window_days": 14,
            "signals": ["from_event_within_window", "from_human_takeover"],
            "combine": "any",
        },
        "planning": {"baseline": 0.35, "effect": 0.08, "holdout_rate": 0.5},
        "domains": ["screening", "compliance", "onboarding", "policy"],
    },
    {
        "key": "coding", "title": "Coding agents", "agent_type": "code",
        "occasion": {"label": "pull request", "example": "PR-1207"},
        "outcome": {
            "success": "CI passes, the PR merges, and it is not reverted within 14 days",
            "window_days": 14,
            "signals": ["from_test_exit_code", "from_no_reversal"],
            "combine": "all",
        },
        "planning": {"baseline": 0.60, "effect": 0.10, "holdout_rate": 0.5},
        "domains": ["git-safety", "refactor", "testing", "subagents", "performance", "other"],
        "packs": ["concurrency-resilience", "databases", "kubernetes-deployment", "security"],
    },
    {
        "key": "marketing", "title": "Marketing", "agent_type": "marketing",
        "occasion": {"label": "campaign or asset", "example": "cmp-2026-q4-17"},
        "outcome": {
            "success": "the asset met its conversion target within 7 days and was not pulled "
                       "for a compliance or brand flag",
            "window_days": 7,
            "signals": ["from_threshold", "from_no_reversal"],
            "combine": "all",
        },
        "planning": {"baseline": 0.30, "effect": 0.06, "holdout_rate": 0.5},
        "domains": ["messaging", "compliance", "channel", "brand-voice"],
    },
    {
        "key": "robotics", "title": "Robotics", "agent_type": "robotics",
        "occasion": {"label": "task episode", "example": "ep-0042917"},
        "outcome": {
            "success": "the task finished within tolerance with no safety stop and no human "
                       "intervention",
            "window_days": 0,
            "signals": ["from_threshold", "from_safety_stop", "from_human_takeover"],
            "combine": "all",
        },
        "planning": {"baseline": 0.80, "effect": 0.05, "holdout_rate": 0.5},
        "domains": ["perception", "manipulation", "navigation", "safety", "calibration"],
    },
    {
        "key": "legal", "title": "Legal", "agent_type": "legal", "regulated": True,
        "occasion": {"label": "matter or document", "example": "matter-5521/doc-3"},
        "outcome": {
            "success": "the reviewing attorney accepted the draft with edits below a set share "
                       "of its text",
            "window_days": 3,
            "signals": ["from_threshold"],
            "combine": "single",
        },
        "planning": {"baseline": 0.50, "effect": 0.08, "holdout_rate": 0.5},
        "domains": ["contract-review", "citations", "privilege", "jurisdiction", "redlining"],
    },
    {
        "key": "finance", "title": "Finance operations", "agent_type": "finance", "regulated": True,
        "occasion": {"label": "case or transaction", "example": "case-88213"},
        "outcome": {
            "success": "the analyst accepted the recommendation without override and it was not "
                       "reversed within 30 days",
            "window_days": 30,
            "signals": ["from_human_takeover", "from_no_reversal"],
            "combine": "all",
        },
        "planning": {"baseline": 0.75, "effect": 0.05, "holdout_rate": 0.5},
        "domains": ["reconciliation", "controls", "fraud", "reporting", "compliance"],
    },
    {
        "key": "clinical", "title": "Clinical documentation", "agent_type": "clinical",
        "regulated": True,
        "occasion": {"label": "encounter or task", "example": "enc-3390177"},
        "outcome": {
            "success": "the clinician accepted the draft with edits below a set share of its text",
            "window_days": 1,
            "signals": ["from_threshold"],
            "combine": "single",
        },
        "planning": {"baseline": 0.60, "effect": 0.08, "holdout_rate": 0.5},
        "domains": ["documentation", "coding", "triage", "scheduling", "patient-communication"],
    },
)
