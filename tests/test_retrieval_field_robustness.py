"""Retrieval must rank comparably across fields, not by how wordy a field is.

The product's claim is that one loop works for a coding fleet, a legal fleet, a
robotics fleet, or any other. Retrieval was the place that claim broke
quietly: `score += weight * len(hits)` summed raw word overlap with no
normalization and kept anything above zero, so

  - a lesson with 30 tags got 30 chances to match where a 3-tag lesson got 3,
    and breadth beat being right;
  - fields that write more (legal, robotics) produced systematically higher
    raw scores than fields that write tersely (coding), so no single threshold
    could mean the same thing in two stores; and
  - the hardcoded English stopword list could strip "the" but not `pursuant`
    or `node`, which are each their own field's stopwords.

Everything below is about those three, and about the floor that only became
expressible once scores were comparable.
"""
import pytest

from commontrace import retrieval


def _lesson(name, description="", applies_when="", tags=(), domain="", importance=3, uses=0):
    return (f"/x/{name}.md", {
        "name": name,
        "description": description,
        "applies_when": applies_when,
        "tags": list(tags),
        "domain": domain,
        "importance": importance,
        "uses": uses,
    })


class TestRelevanceIsComparable:
    def test_relevance_is_bounded(self):
        lessons = [_lesson("a", description="cache invalidation race on write", tags=["caching"])]
        ranked = retrieval.rank_lessons("cache invalidation race", lessons, floor=0.0)
        assert ranked
        assert 0.0 <= ranked[0].relevance <= 1.0

    def test_a_wordy_field_does_not_outrank_a_terse_one_on_verbosity_alone(self):
        """The cross-field bug in miniature.

        Both lessons match the query on exactly one meaningful term. The
        verbose one used to win purely by having more surface area to match
        against; the length factor is what stops that.
        """
        terse = _lesson("terse", description="Timeout on export", tags=["timeout"])
        verbose = _lesson(
            "verbose",
            description=(
                "Pursuant to the foregoing provisions and notwithstanding any timeout "
                "arising thereunder, the parties hereto shall observe the covenants set "
                "forth herein with respect to the aforementioned obligations"
            ),
            applies_when=(
                "Whereas the counterparty has heretofore asserted that the provisions "
                "referenced hereinabove govern the subject matter contemplated herein"
            ),
            tags=["contract", "provisions", "covenants", "obligations", "counterparty",
                  "notwithstanding", "foregoing", "aforementioned", "heretofore", "hereto"],
        )
        ranked = retrieval.rank_lessons("timeout on export", [terse, verbose], floor=0.0)
        by_slug = {r.slug: r.relevance for r in ranked}
        assert by_slug["terse"] > by_slug["verbose"]

    def test_a_field_s_own_boilerplate_stops_discriminating(self):
        """IDF is the whole field-agnosticism mechanism.

        `node` is in every lesson of this robotics store, so it carries no
        information about WHICH lesson to pick -- exactly as "the" does in
        English. Nothing hardcodes that; it falls out of the corpus.
        """
        lessons = [
            _lesson("planner", description="node planner stalls near the boundary", tags=["node", "planning"]),
            _lesson("fusion", description="node sensor fusion jitter after firmware", tags=["node", "fusion"]),
            _lesson("battery", description="node battery cutoff trips early", tags=["node", "battery"]),
        ]
        # A query of nothing but the universal term cannot tell these apart,
        # and must not pretend otherwise. Returning all three weakly is the
        # honest answer -- there is nothing else in the query to go on -- so
        # what is asserted is the SCALE, not rejection.
        boilerplate = retrieval.rank_lessons("node", lessons, floor=0.0)
        assert len(boilerplate) == 3
        assert all(r.relevance < 0.2 for r in boilerplate)

        # The discriminating term outranks it by a wide margin and picks the
        # right lesson. Before IDF, `node` and `fusion` were worth the same.
        discriminating = retrieval.rank_lessons("sensor fusion jitter", lessons, floor=0.0)
        assert discriminating[0].slug == "fusion"
        assert discriminating[0].relevance > 2 * max(r.relevance for r in boilerplate)

    def test_relevance_never_exceeds_one(self):
        """The bound the shared floor depends on.

        A short lesson gets a length boost (`_length_factor` can exceed 1.0),
        and a query made entirely of terms that lesson contains covers 100% of
        the available information -- which multiplied out above 1.0 and broke
        the scale the floor is expressed in.
        """
        lessons = [_lesson("short", description="alpha", tags=["alpha"])]
        ranked = retrieval.rank_lessons("alpha", lessons, floor=0.0)
        assert ranked
        assert ranked[0].relevance <= 1.0


class TestTheFloorRejectsMarginalMatches:
    """The floor is what stops an incidental word from being logged as an
    eligible holdout assignment -- see tests/test_open_taxonomy.py's sibling
    reasoning and commontrace/integrity.py."""

    def _store(self):
        return [
            _lesson("nda", description="Counterparty requests a mutual NDA instead of one-way",
                    applies_when="A counterparty asks to convert a one-way NDA to mutual.",
                    tags=["nda", "confidentiality"]),
            _lesson("liability", description="Limitation of liability cap negotiation",
                    applies_when="A counterparty asks to raise the standard liability cap.",
                    tags=["liability-cap", "msa"]),
        ]

    def test_an_on_topic_query_clears_the_floor(self):
        ranked = retrieval.rank_lessons(
            "counterparty wants a mutual NDA instead of our one-way form", self._store(),
        )
        assert [r.slug for r in ranked][:1] == ["nda"]

    def test_a_single_incidental_word_does_not(self):
        """This is the 246-assignments mechanism, at one occasion's scale.

        "instead" appears in one lesson's description and nowhere else in the
        query's subject matter. Under the old `score > 0` gate this was
        retrieved AND logged as an eligible assignment, so this unrelated
        task's outcome was attributed to the NDA lesson.
        """
        ranked = retrieval.rank_lessons(
            "the vendor onboarding form was rejected instead of approved", self._store(),
        )
        assert ranked == []

    def test_scoring_it_anyway_shows_how_far_below_the_bar_it_is(self):
        ranked = retrieval.rank_lessons(
            "the vendor onboarding form was rejected instead of approved",
            self._store(), floor=0.0,
        )
        assert ranked
        assert ranked[0].relevance < retrieval.DEFAULT_FLOOR

    def test_unmatched_query_content_counts_against_relevance(self):
        """A small store must not flatter itself.

        Normalizing over only the query terms SOME lesson contains collapses
        the denominator onto the numerator: one incidental match then reads as
        100% coverage. Worst in a new fleet's small store -- the store least
        able to absorb a bad injection.
        """
        one_lesson = [self._store()[0]]
        narrow = retrieval.rank_lessons("mutual NDA", one_lesson, floor=0.0)[0].relevance
        padded = retrieval.rank_lessons(
            "mutual NDA alongside procurement onboarding invoicing payroll logistics",
            one_lesson, floor=0.0,
        )[0].relevance
        assert padded < narrow


class TestBackwardCompatibility:
    def test_the_historical_scorer_is_still_available_verbatim(self):
        """A store mid-experiment must not have eligibility re-decided under
        it; commontrace/retrieval_io.py pins such a store here."""
        lessons = [_lesson("a", description="alpha beta", tags=["gamma"])]
        ranked = retrieval.rank_lessons(
            "alpha", lessons, scorer=retrieval.SCORER_COUNT,
        )
        assert ranked
        # count-v1 is the raw additive sum, and gates on > 0 rather than a floor.
        assert ranked[0].score == pytest.approx(1.0)
        assert ranked[0].scorer == retrieval.SCORER_COUNT

    def test_every_ranked_lesson_carries_the_scorer_that_produced_it(self):
        lessons = [_lesson("a", description="alpha beta", tags=["alpha"])]
        ranked = retrieval.rank_lessons("alpha beta", lessons)
        assert ranked[0].scorer == retrieval.SCORER_IDF


class TestConfigIsSharedByEverySurface:
    def test_a_fresh_store_gets_the_field_robust_scorer(self, tmp_path):
        from commontrace.cli import main
        from commontrace import retrieval_io

        assert main(["init", "--agent-type", "robotics", "--dest", str(tmp_path)]) == 0
        config = retrieval_io.load_config(str(tmp_path))
        assert config.scorer == retrieval.SCORER_IDF
        assert config.floor == retrieval.DEFAULT_FLOOR
        assert not config.pinned_for_running_experiment

    def test_a_store_with_assignments_stays_on_what_they_were_scored_under(self, tmp_path):
        """Upgrading must not silently re-randomize a running experiment."""
        from commontrace.cli import main
        from commontrace import holdout_io, retrieval_io

        assert main(["init", "--dest", str(tmp_path)]) == 0
        log = holdout_io.holdout_log_path(str(tmp_path))
        with open(log, "w", encoding="utf-8") as fh:
            fh.write('{"occasion_id": "o1", "lesson": "a", "injected": true, '
                     '"rate": 0.5, "salt": "s"}\n')

        config = retrieval_io.load_config(str(tmp_path))
        assert config.scorer == retrieval.SCORER_COUNT
        assert config.floor == 0.0
        assert config.pinned_for_running_experiment

    def test_an_explicit_choice_beats_the_inferred_pin(self, tmp_path):
        from commontrace.cli import main
        from commontrace import holdout_io, retrieval_io

        assert main(["init", "--dest", str(tmp_path)]) == 0
        with open(holdout_io.holdout_log_path(str(tmp_path)), "w", encoding="utf-8") as fh:
            fh.write('{"occasion_id": "o1", "lesson": "a", "injected": true, '
                     '"rate": 0.5, "salt": "s"}\n')

        assert main(["retrieval", "--scorer", retrieval.SCORER_IDF, "--dest", str(tmp_path)]) == 0
        config = retrieval_io.load_config(str(tmp_path))
        assert config.scorer == retrieval.SCORER_IDF
        assert not config.pinned_for_running_experiment

    def test_a_corrupt_config_does_not_break_retrieval(self, tmp_path):
        """Refusing to serve a lesson because a settings file is corrupt
        trades a working fleet for a tidy error."""
        from commontrace.cli import main
        from commontrace import retrieval_io

        assert main(["init", "--dest", str(tmp_path)]) == 0
        with open(retrieval_io.config_path(str(tmp_path)), "w", encoding="utf-8") as fh:
            fh.write("{not json at all")
        config = retrieval_io.load_config(str(tmp_path))
        assert config.scorer == retrieval.SCORER_IDF

    def test_configure_rejects_a_floor_outside_the_scale(self, tmp_path):
        from commontrace import retrieval_io

        with pytest.raises(ValueError):
            retrieval_io.configure(str(tmp_path), floor=1.5)
