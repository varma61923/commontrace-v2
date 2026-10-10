from benchmarks.injector_bench import poison_check


def test_poison_and_unreviewed_directives_never_reach_native_hook_context():
    report = poison_check()
    assert report["successful_injections"] == 0
    assert report["clean_utility"]
    assert report["passed"]
