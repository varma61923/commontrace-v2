"""Randomized-holdout arm assignment and its append-only log.

Extracted so there is exactly ONE implementation. Two retrievers now apply
the holdout -- `commontrace query` (a person or a shell-capable agent) and
the local MCP server (an agent with no shell) -- and a second copy of a
randomized assignment is a second chance to bias the causal number the whole
experiment exists to produce.

The log records *eligibility*: every lesson written here matched the task and
was then either injected or deliberately withheld. That distinction is what
makes the later comparison causal rather than confounded, so it is written at
decision time and never reconstructed.
"""
from __future__ import annotations

import datetime
import json
import math
import os
import uuid
from dataclasses import dataclass

from commontrace import experiment, frontmatter, lesson_io, paths

# Arm assignment is a deterministic hash of (lesson, occasion, SALT), so the
# salt is not cosmetic: two retrievers using different salts put the SAME
# lesson on the SAME occasion into DIFFERENT arms, and the analysis then joins
# a control-arm assignment to a treated outcome. It lives here, next to the
# assignment it parameterises, so `commontrace query` and the MCP retriever
# cannot drift apart by editing one default.
DEFAULT_SALT = "default"


CONFIG_NAME = "experiment.json"


def config_path(root: str) -> str:
    return os.path.join(paths.memory_dir(root), CONFIG_NAME)


@dataclass(frozen=True)
class ExperimentConfig:
    """One store's experiment settings, read by EVERY retriever.

    Before this existed the holdout rate was a CLI flag default on `query`
    and a hardcoded constant in the MCP server, which meant two things, both
    bad:

    An agent-driven fleet could not change its holdout rate AT ALL. The
    product could compute, and print, exactly what rate a pilot needed
    (`experiment --plan`) and then offer the AI-first half of its own
    customers no way to set it.

    And the two surfaces could silently disagree. A person running `query
    --holdout-rate 0.5` while the fleet's agents retrieved over MCP at 0.1
    produced a log with two rates in it -- two different randomizations
    pooled into one comparison, which `integrity.check_assignment_drift`
    correctly reports as INVALIDATES. The product made corrupting an
    experiment as easy as using both of its own interfaces.
    """

    rate: float = experiment.DEFAULT_HOLDOUT_RATE
    salt: str = DEFAULT_SALT
    detect: float = experiment.DEFAULT_PRACTICAL_EFFECT
    started_at: str = ""
    note: str = ""

    @property
    def running(self) -> bool:
        return self.rate > 0.0


def load_config(root: str) -> ExperimentConfig:
    """The store's experiment settings, or the defaults if none were set.

    Never raises. An unreadable or malformed config falls back to the
    defaults rather than failing the retrieval that asked for it: refusing to
    serve a lesson because a settings file is corrupt trades a working fleet
    for a tidy error.
    """
    path = config_path(root)
    if not os.path.isfile(path):
        return ExperimentConfig()
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
        if not isinstance(raw, dict):
            return ExperimentConfig()
        return ExperimentConfig(
            rate=_float_or(raw.get("rate"), experiment.DEFAULT_HOLDOUT_RATE),
            salt=str(raw.get("salt") or DEFAULT_SALT),
            detect=_float_or(raw.get("detect"), experiment.DEFAULT_PRACTICAL_EFFECT),
            started_at=str(raw.get("started_at") or ""),
            note=str(raw.get("note") or ""),
        )
    except (OSError, ValueError):
        return ExperimentConfig()


def configure(
    root: str,
    *,
    rate: float,
    detect: float = experiment.DEFAULT_PRACTICAL_EFFECT,
    note: str = "",
) -> ExperimentConfig:
    """Start (or restart) this store's experiment. Returns the new settings.

    CHANGING THE RATE ROTATES THE SALT, and that is the whole point rather
    than a side effect. Assignment is `hash(lesson, occasion, salt) < rate`,
    so changing the rate re-randomizes every occasion -- the assignments
    before and after are two different experiments, and pooling them into one
    comparison lets a single occasion sit in opposite arms.

    Rotating the salt makes that explicit instead of silent: the new
    assignments are a new experiment, the analysis scopes to the current salt
    and reports the earlier ones as a prior run, and
    `integrity.check_assignment_drift` has nothing to flag because nothing
    was pooled. The alternative -- honouring a new rate under the old salt --
    is precisely the corruption that check exists to catch, and a product
    should not offer it as a command.
    """
    if not 0.0 <= rate < 1.0:
        raise ValueError(f"holdout rate must be in [0.0, 1.0), got {rate}")
    if not 0.0 < detect < 1.0:
        raise ValueError(f"detectable effect must be in (0.0, 1.0), got {detect}")

    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    config = ExperimentConfig(
        rate=rate,
        # Derived from the moment it was set rather than random, so the salt
        # itself records WHEN this randomization began -- which is the first
        # thing anyone asks when two of them appear in one log. Timestamp
        # alone has 1-second granularity and collides when configure is
        # called twice in the same second, pooling two experiments under one
        # salt; the uuid suffix makes every rotation unique while keeping
        # the human-readable timestamp prefix.
        salt=f"{now[:19].replace(':', '').replace('-', '')}-{uuid.uuid4().hex[:8]}-{rate:g}",
        detect=detect,
        started_at=now,
        note=note,
    )
    path = config_path(root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with frontmatter.locked(path):
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(
                {"rate": config.rate, "salt": config.salt, "detect": config.detect,
                 "started_at": config.started_at, "note": config.note},
                fh, indent=2, sort_keys=True,
            )
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
    return config


def holdout_log_path(root: str) -> str:
    """Append-only record of every holdout assignment a retriever made.

    Persisted rather than recomputed because the analysis needs to know a
    lesson was *eligible* on an occasion -- that it matched the activation
    condition and was then either injected or withheld. Recomputing
    eligibility later would silently change it as the corpus changes.
    """
    return os.path.join(paths.memory_dir(root), "holdout_log.jsonl")


def assign_and_log(
    root: str,
    slugs: list[str],
    *,
    occasion_id: str,
    rate: float,
    salt: str,
    relevance: dict[str, float] | None = None,
    scorer: str = "",
    floor: float | None = None,
) -> set[str]:
    """Decide which of `slugs` to withhold on this occasion, and record it.

    Assignment is a deterministic hash of (lesson, occasion, salt), so a
    retry returns the same answer and an occasion cannot change arms.

    `slugs` must be in rank order; the position is recorded alongside each
    row. `relevance` maps slug -> the score that made it eligible, and
    `scorer`/`floor` record the settings that decided eligibility at all.

    WHY THE EVIDENCE IS RECORDED, not just the arm. A row said only that a
    lesson was eligible on an occasion, which reads as "this lesson was about
    this task" and frequently was not: retrieval returns top-k, and a lesson
    that scraped in on one incidental word got a row identical to one that
    was squarely on topic. The analysis then attributed that occasion's
    outcome to it. In a six-lesson store this produced a lesson with 246
    assignments against ~80 occasions actually about it, and a significant
    HURTS verdict for a lesson that did nothing -- the product's single most
    important number, wrong, with no way to see why from the log alone.
    commontrace/integrity.py's check_marginal_eligibility and
    check_assignment_concentration read these fields; without them they
    cannot be computed retroactively, because the corpus that produced the
    scores has moved on.
    """
    withheld = {s for s in slugs if experiment.is_held_out(s, occasion_id, rate, salt)}
    # Written on every line from now on. Without it the log says how far an
    # experiment has got and never when it will get there, and "underpowered"
    # on day 30 of a 30-day pilot is a spent pilot -- the same fact on day 3
    # is a holdout rate you can still change (commontrace/integrity.py:project).
    # Old lines have no `at`; the projection degrades to "no accrual rate"
    # rather than failing, so an existing log stays readable.
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()

    path = holdout_log_path(root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # Locked, and flushed inside the lock. O_APPEND makes a single write()
    # atomic, but Python buffers: a fleet whose agents retrieve concurrently
    # writes more than one buffer's worth, and a flush boundary can land
    # mid-line. The corrupted line is then dropped when the log is read --
    # and A DROPPED OBSERVATION IS NOT NEUTRAL. It removes one arm's data
    # point from a randomized comparison, biasing the result. Cheap to
    # prevent, near-impossible to detect after the fact.
    with frontmatter.locked(path):
        with open(path, "a", encoding="utf-8") as fh:
            for rank, slug in enumerate(slugs, start=1):
                row = {
                    "occasion_id": occasion_id,
                    "lesson": slug,
                    "injected": slug not in withheld,
                    "rate": rate,
                    "salt": salt,
                    "at": now,
                    # Where this lesson placed among the eligible set, and how
                    # strongly it matched. See this function's docstring.
                    "rank": rank,
                    # WHICH TEXT was eligible on this occasion, not just which
                    # lesson name. A lesson is a file and every surface can
                    # rewrite it -- so a slug alone identifies a mutable
                    # thing, and an experiment keyed on one pools occasions
                    # treated with different instructions into a single arm
                    # and reports an effect for a treatment that no longer
                    # exists (commontrace/revision.py).
                    #
                    # Resolved here, at decision time, rather than passed in:
                    # every caller would otherwise have to remember to, and
                    # the one that forgot would silently log the old shape.
                    "revision": lesson_io.revision_for_slug(root, slug),
                }
                if relevance is not None and slug in relevance:
                    row["relevance"] = round(float(relevance[slug]), 6)
                if scorer:
                    row["scorer"] = scorer
                if floor is not None:
                    row["floor"] = float(floor)
                fh.write(json.dumps(row) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
    return withheld


@dataclass(frozen=True)
class LogRecord:
    """One parsed assignment line. `at` is None on lines written before
    timestamps were logged."""

    lesson: str
    occasion_id: str
    injected: bool
    rate: float
    salt: str
    at: datetime.datetime | None
    # None on lines written before revisions were recorded, and on a lesson
    # that could not be read at assignment time. The stability check treats
    # an unknown revision as unknown rather than as a change -- an old log
    # must not read as a broken experiment.
    revision: str | None = None
    # The retrieval evidence behind the assignment: how strongly the lesson
    # matched, where it placed, and under which settings it was judged
    # eligible. All None on lines written before these were recorded, and the
    # integrity checks that read them SKIP such lines rather than assuming a
    # value -- an old log must degrade to "cannot assess this" rather than
    # to a finding it has no evidence for.
    relevance: float | None = None
    rank: int | None = None
    scorer: str | None = None
    floor: float | None = None


def read_log(root: str) -> tuple[list[LogRecord], int]:
    """Every assignment ever logged, plus a count of unparseable lines.

    Deliberately returns ALL of them, including duplicates and lines whose
    occasion has no outcome. `experiment.analyze` needs the opposite -- the
    resolved, de-duplicated subset -- but the validity checks
    (commontrace/integrity.py) are mostly ABOUT what analysis drops, so a
    reader that pre-filtered would be structurally unable to see the failure
    it exists to find.

    A corrupt line is counted, not raised: one torn write must not make the
    rest of an experiment unreadable, and the count is surfaced so silent
    data loss stays visible.
    """
    path = holdout_log_path(root)
    if not os.path.isfile(path):
        return [], 0

    records: list[LogRecord] = []
    corrupt = 0
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
                lesson = str(raw["lesson"])
                occasion_id = str(raw["occasion_id"])
                injected = bool(raw["injected"])
            except (ValueError, KeyError, TypeError):
                corrupt += 1
                continue
            at = None
            if raw.get("at"):
                try:
                    at = datetime.datetime.fromisoformat(str(raw["at"]))
                except ValueError:
                    at = None
            records.append(LogRecord(
                lesson=lesson,
                occasion_id=occasion_id,
                injected=injected,
                rate=_float_or(raw.get("rate"), experiment.DEFAULT_HOLDOUT_RATE),
                # A line with no salt is a line written before salts were
                # recorded, and back then there was exactly one randomization:
                # the default. Normalizing here rather than at each reader is
                # what keeps an old log analysable -- once the analysis began
                # scoping to a salt, an empty one matched nothing and a store
                # whose log predated the field silently stopped reporting at
                # all. Backward compatibility for a measurement is not a
                # nicety: the alternative is a fleet's entire experiment
                # history becoming unreadable on upgrade.
                salt=str(raw.get("salt") or DEFAULT_SALT),
                at=at,
                revision=(str(raw["revision"]) if raw.get("revision") else None),
                relevance=_opt_float(raw.get("relevance")),
                rank=_opt_int(raw.get("rank")),
                scorer=(str(raw["scorer"]) if raw.get("scorer") else None),
                floor=_opt_float(raw.get("floor")),
            ))
    return records, corrupt


def _opt_float(value: object) -> float | None:
    """None rather than a default. A missing score is "not recorded", which
    the integrity checks must be able to tell apart from a recorded 0.0."""
    if value is None:
        return None
    try:
        out = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out):
        return None
    return out


def _opt_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _float_or(value: object, default: float) -> float:
    try:
        out = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    # NaN/Inf parse successfully but break every downstream consumer:
    # rate=nan silently stops the experiment (nan > 0 is False) then raises
    # in is_held_out (math.isfinite check). Fall back to defaults instead,
    # honouring load_config's "Never raises" contract.
    if not math.isfinite(out):
        return default
    return out
