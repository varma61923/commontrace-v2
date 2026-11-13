import json

from unimem import cli


def test_demo_recovers_planted_effects_and_governs_them(tmp_path, capsys):
    store = tmp_path / "demo-store"
    assert cli.main(["demo", "--json", "--keep", str(store)]) == 0
    result = json.loads(capsys.readouterr().out)

    remember = result["remember"]
    assert "v2 payments API" in remember["today"]
    assert "v1 refunds endpoint" in remember["then"]

    measure = result["measure"]
    assert measure["integrity"] == "SOUND"
    verdicts = {row["memory"]: row["verdict"] for row in measure["memories"]}
    assert verdicts["verify-owner"] == "HELPS"
    assert verdicts["legacy-refund-api"] == "HURTS"
    assert verdicts["retry-429"] not in ("HELPS", "HURTS")

    assert result["govern"] == {
        "withdrawn": ["legacy-refund-api"],
        "graduated": ["verify-owner"],
        "under_test": ["retry-429"],
    }
    assert result["store"] == str(store)

    learn = result["learn"]
    assert learn["audit"] == "SOUND"
    effects = {row["memory"]: row["effects"] for row in learn["memories"]}
    assert effects["move-fast-skip-checks"]["robot"]["mean"] < -0.05
    assert abs(effects["move-fast-skip-checks"]["agent"]["mean"]) <= 0.08
    assert all(effects["confirm-before-acting"][kind]["mean"] > 0.03 for kind in ("agent", "llm", "robot"))


def test_demo_refuses_an_existing_directory_and_tiny_runs(tmp_path, capsys):
    assert cli.main(["demo", "--keep", str(tmp_path)]) == 2
    assert cli.main(["demo", "--occasions", "10"]) == 2
    assert "error" in capsys.readouterr().err
