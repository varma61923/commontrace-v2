
from benchmarks.adapters import (
    BenchmarkItem,
    get_adapter,
    get_snapshot_cache_key,
    load_cached_snapshot,
    save_cached_snapshot,
)
from benchmarks.judges import get_judge


def test_synthetic_adapter_seeded_determinism():
    ad1 = get_adapter("synthetic")
    items1 = ad1.load(limit=5, seed=123)

    ad2 = get_adapter("synthetic")
    items2 = ad2.load(limit=5, seed=123)

    assert len(items1) == 5
    assert len(items2) == 5
    for a, b in zip(items1, items2):
        assert a.id == b.id
        assert a.question == b.question
        assert a.answer == b.answer


def test_hotpotqa_and_musique_adapters():
    hotpot = get_adapter("hotpotqa")
    hp_items = hotpot.load(limit=3, seed=42)
    assert len(hp_items) == 3
    assert "hop1" in hp_items[0].evidence
    assert "hop2" in hp_items[0].evidence

    musique = get_adapter("musique")
    mq_items = musique.load(limit=3, seed=42)
    assert len(mq_items) == 3
    assert "step1" in mq_items[0].evidence


def test_snapshot_cache_save_and_load(tmp_path):
    cache_dir = str(tmp_path / "cache")
    key = get_snapshot_cache_key("hotpotqa", 42, 5)

    assert load_cached_snapshot(cache_dir, key) is None

    items = [
        BenchmarkItem(
            id="item1",
            question="What is X?",
            answer="X is 10",
            evidence={"fact": "X = 10"},
        )
    ]
    saved_path = save_cached_snapshot(cache_dir, key, items)
    assert saved_path.endswith(f"snapshot_{key}.json")

    loaded = load_cached_snapshot(cache_dir, key)
    assert loaded is not None
    assert len(loaded) == 1
    assert loaded[0].id == "item1"
    assert loaded[0].question == "What is X?"


def test_dolphin_judge_formatting():
    judge = get_judge("dolphin")
    assert judge.name == "dolphin"
    assert judge.model == "gpt-4o"

    prompt = judge.format_prompt(
        question="What is Morgan Chen's project?",
        gold="Project Chimera",
        answer="Morgan Chen leads Project Chimera.",
    )
    assert "DolphinBench" in prompt
    assert "Project Chimera" in prompt
    assert "Morgan Chen" in prompt
