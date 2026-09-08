"""commontrace/lesson_cache.py -- the incremental store of parsed lesson
frontmatter and tokenized fields that replaced a full YAML re-parse (plus
re-tokenize) of the corpus on every `query`.

The non-negotiable property under test: a cached retrieval must be
BIT-IDENTICAL to the direct scan for the same corpus state. Retrieval
eligibility is the denominator of a causal estimate
(commontrace/integrity.py), so a cache that returns a different ranking than
the code it replaced -- even by a rounding hair -- would silently change
which lessons get randomized into an experiment.
"""
import os

from commontrace import frontmatter, lesson_cache, paths, retrieval


def _write_lesson(root, slug, description="", applies_when="", tags=None,
                  domain="", importance=3, uses=0, status="active", agent_type="code"):
    ldir = paths.lessons_dir(root)
    os.makedirs(ldir, exist_ok=True)
    path = os.path.join(ldir, f"{slug}.md")
    fm = {
        "name": slug, "description": description, "applies_when": applies_when,
        "tags": tags or [], "domain": domain, "importance": importance,
        "uses": uses, "status": status, "agent_type": agent_type,
        "last_hit": "2026-07-01",  # a bare YAML date -- the exact value that
                                   # breaks a naive JSON round-trip
    }
    frontmatter.write(path, fm, "## Rule\nSomething.\n")
    return path


def _direct_scan(root, agent_type=None):
    """The pre-cache behaviour, reproduced verbatim: glob + parse every file,
    no cache involved at all."""
    import glob

    out = []
    ldir = paths.lessons_dir(root)
    for path in sorted(glob.glob(os.path.join(ldir, "lesson_*.md"))):
        if os.path.basename(path) == "lesson_template.md":
            continue
        fm, _body = frontmatter.read(path)
        if fm.get("status") != "active":
            continue
        if agent_type and fm.get("agent_type") != agent_type:
            continue
        out.append((path, fm))
    return out


class TestCacheMatchesDirectScan:
    def test_a_fresh_store_ranks_identically_cached_and_direct(self, tmp_path):
        root = str(tmp_path)
        _write_lesson(root, "lesson_git_safety",
                      description="Never force-push a shared branch",
                      applies_when="force-pushing shared history", tags=["git", "safety"])
        _write_lesson(root, "lesson_unrelated",
                      description="How to write a changelog entry",
                      applies_when="documenting a release", tags=["docs"])
        _write_lesson(root, "lesson_archived",
                      description="Force-push guidance, but retired", status="archived")

        query = "I need to force-push a shared branch, is that safe"

        direct = _direct_scan(root)
        cached_lessons, term_cache = lesson_cache.load_active_with_terms(root)

        direct_ranked = retrieval.rank_lessons(query, direct, floor=0.0)
        cached_ranked = retrieval.rank_lessons(
            query, cached_lessons, floor=0.0, term_cache=term_cache)

        assert direct_ranked == cached_ranked
        assert [r.slug for r in cached_ranked][:1] == ["lesson_git_safety"]

    def test_cold_cache_and_warm_cache_rank_identically(self, tmp_path):
        """The first call (nothing cached yet, everything parsed) and a
        second call against the SAME unmodified files (everything served from
        cache) must be indistinguishable to a caller."""
        root = str(tmp_path)
        for i in range(12):
            _write_lesson(root, f"lesson_{i:03d}",
                          description=f"topic {i} handling and recovery",
                          applies_when=f"working with topic {i}", tags=[f"tag{i}", "shared"])

        query = "topic 5 handling and recovery"
        cold_lessons, cold_terms = lesson_cache.load_active_with_terms(root)
        cold = retrieval.rank_lessons(query, cold_lessons, floor=0.0,
                                      term_cache=cold_terms)

        assert os.path.isfile(lesson_cache.cache_path(root))

        warm_lessons, warm_terms = lesson_cache.load_active_with_terms(root)
        warm = retrieval.rank_lessons(query, warm_lessons, floor=0.0,
                                      term_cache=warm_terms)

        assert cold == warm

    def test_editing_one_lesson_reparses_only_that_one(self, tmp_path, monkeypatch):
        root = str(tmp_path)
        paths_written = [
            _write_lesson(root, f"lesson_{i:03d}", description=f"content {i}")
            for i in range(5)
        ]
        lesson_cache.load_active_with_terms(root)  # populate the cache

        parsed = []
        real_read = frontmatter.read

        def counting_read(p):
            parsed.append(p)
            return real_read(p)

        monkeypatch.setattr(frontmatter, "read", counting_read)
        # Touch only lesson_002's content (mtime AND size change).
        with open(paths_written[2], "a", encoding="utf-8") as fh:
            fh.write("\nExtra line.\n")

        lesson_cache.load_active_with_terms(
            root, reader=lambda p: counting_read(p))

        assert parsed == [paths_written[2]]

    def test_a_deleted_lesson_leaves_the_cache(self, tmp_path):
        root = str(tmp_path)
        keep = _write_lesson(root, "lesson_keep", description="keep me")
        gone = _write_lesson(root, "lesson_gone", description="delete me")
        lesson_cache.load_active_with_terms(root)

        os.remove(gone)
        lessons, terms = lesson_cache.load_active_with_terms(root)

        assert [p for p, _fm in lessons] == [keep]
        assert gone not in terms

    def test_a_corrupt_cache_file_falls_back_to_a_full_reparse(self, tmp_path):
        """Never raises, same posture as retrieval_io.load_config: a
        malformed cache costs speed, not correctness."""
        root = str(tmp_path)
        _write_lesson(root, "lesson_a", description="anything at all")
        os.makedirs(os.path.dirname(lesson_cache.cache_path(root)), exist_ok=True)
        with open(lesson_cache.cache_path(root), "w", encoding="utf-8") as fh:
            fh.write("{not valid json at all")

        lessons, terms = lesson_cache.load_active_with_terms(root)
        assert len(lessons) == 1
        assert lessons[0][1]["description"] == "anything at all"

    def test_a_corrupt_on_disk_terms_field_self_heals(self, tmp_path):
        """`terms` is a pure function of the cached `fm`, so `_stamps_differ`
        deliberately does not compare it -- but that means a cache entry
        whose `terms` alone is corrupt (unlike `mtime_ns`/`size`/`fm`, all of
        which stay unchanged) must still be recognized as needing a rewrite,
        or the corruption never leaves the on-disk file and every future
        call pays a full reparse for that lesson forever."""
        import json

        root = str(tmp_path)
        path = _write_lesson(root, "lesson_a", description="anything at all")
        lesson_cache.load_active_with_terms(root)  # populate the cache

        cpath = lesson_cache.cache_path(root)
        with open(cpath, encoding="utf-8") as fh:
            on_disk = json.load(fh)
        on_disk["entries"][path]["terms"] = [["only", "one", "field"]]  # wrong length
        with open(cpath, "w", encoding="utf-8") as fh:
            json.dump(on_disk, fh)

        lessons, terms = lesson_cache.load_active_with_terms(root)
        assert len(lessons) == 1  # self-heals in memory even without the fix

        with open(cpath, encoding="utf-8") as fh:
            healed = json.load(fh)
        assert len(healed["entries"][path]["terms"]) == 4, (
            "the repaired terms were never written back to disk -- every "
            "future call will reparse this lesson from scratch, forever"
        )

    def test_a_bare_yaml_date_field_survives_the_cache_round_trip(self, tmp_path):
        """last_hit: 2026-07-01 parses as datetime.date under PyYAML -- the
        exact value a naive JSON cache would crash on or silently corrupt."""
        root = str(tmp_path)
        _write_lesson(root, "lesson_a", description="anything")
        # Must not raise.
        lessons, _terms = lesson_cache.load_active_with_terms(root)
        assert len(lessons) == 1


class TestTermCacheIsOptional:
    def test_rank_lessons_without_term_cache_still_works(self, tmp_path):
        """A caller with no cache (or a caller that never adopts one) gets
        exactly today's behaviour -- `term_cache` can only skip work, never
        change what is required to produce a result."""
        root = str(tmp_path)
        _write_lesson(root, "lesson_a", description="force-push shared branch")
        lessons = _direct_scan(root)
        ranked = retrieval.rank_lessons("force-push a shared branch", lessons, floor=0.0)
        assert ranked[0].slug == "lesson_a"

    def test_a_path_missing_from_term_cache_still_ranks_correctly(self, tmp_path):
        """A partial cache (e.g. a lesson added after the cache dict was
        built by some other caller) tokenizes that one lesson fresh rather
        than skipping or mis-scoring it."""
        root = str(tmp_path)
        _write_lesson(root, "lesson_a", description="force-push shared branch")
        _write_lesson(root, "lesson_b", description="unrelated changelog entry")
        lessons = _direct_scan(root)

        full_cache = {p: lesson_cache.field_terms(fm) for p, fm in lessons}
        partial_cache = {k: v for k, v in full_cache.items() if k != lessons[0][0]}

        query = "force-push a shared branch"
        full = retrieval.rank_lessons(query, lessons, floor=0.0, term_cache=full_cache)
        partial = retrieval.rank_lessons(query, lessons, floor=0.0, term_cache=partial_cache)
        none = retrieval.rank_lessons(query, lessons, floor=0.0, term_cache=None)

        assert full == partial == none

    def test_a_malformed_term_cache_entry_falls_back_to_tokenizing_that_lesson(self, tmp_path):
        """A cache entry with the wrong number of fields (a hand-edited or
        format-drifted cache file) must not be trusted for that lesson."""
        root = str(tmp_path)
        _write_lesson(root, "lesson_a", description="force-push shared branch")
        lessons = _direct_scan(root)

        bad_cache = {lessons[0][0]: [["only", "one", "field"]]}  # wrong length
        query = "force-push a shared branch"
        with_bad_cache = retrieval.rank_lessons(query, lessons, floor=0.0, term_cache=bad_cache)
        direct = retrieval.rank_lessons(query, lessons, floor=0.0, term_cache=None)
        assert with_bad_cache == direct


class TestScoringIsProcessOrderIndependent:
    def test_relevance_does_not_depend_on_hash_seed(self, tmp_path):
        """The regression this pins: `covered` used to sum over a bare
        `set[str]` (`matched`), and str iteration order is
        PYTHONHASHSEED-randomized, so summing in an unspecified order could
        move `rel` by a few ULP between processes -- exactly at the boundary
        `rel >= floor` tests. Run the SAME corpus/query through several
        distinct hash seeds (via a subprocess, the only way to actually vary
        PYTHONHASHSEED) and assert bit-identical relevance.
        """
        import json
        import subprocess
        import sys

        root = str(tmp_path)
        _write_lesson(
            root, "lesson_a",
            description="Counterparty requests a mutual NDA instead of one-way",
            applies_when="converting a one-way NDA to mutual", tags=["nda", "legal"])
        _write_lesson(
            root, "lesson_b",
            description="Limitation of liability cap negotiation instead of a waiver",
            applies_when="raising the standard liability cap", tags=["liability", "legal"])

        script = (
            "import sys; sys.path.insert(0, %r)\n"
            "from commontrace import retrieval, lesson_cache\n"
            "lessons, terms = lesson_cache.load_active_with_terms(%r)\n"
            "ranked = retrieval.rank_lessons("
            "    'the vendor form was rejected instead of approved', lessons,"
            "    floor=0.0, term_cache=terms)\n"
            "import json\n"
            "print(json.dumps([[r.slug, r.relevance] for r in ranked]))\n"
        ) % (os.path.dirname(os.path.dirname(os.path.abspath(retrieval.__file__))), root)

        outputs = []
        for seed in ("0", "1", "2"):
            env = dict(os.environ, PYTHONHASHSEED=seed)
            proc = subprocess.run(
                [sys.executable, "-c", script], capture_output=True, text=True,
                env=env, check=True,
            )
            outputs.append(json.loads(proc.stdout))

        assert outputs[0] == outputs[1] == outputs[2]
