import json

from backend import run_store


def test_runs_survive_restart_and_unfinished_runs_are_failed():
    done = run_store.create_run({"error_log": "a"})
    run_store.update_run(done["id"], status="done")
    stuck = run_store.create_run({"error_log": "b"})
    run_store.update_run(stuck["id"], status="engineering")

    run_store._runs.clear()
    run_store._load()

    assert run_store.get_run(done["id"])["status"] == "done"
    assert run_store.get_run(stuck["id"])["status"] == "failed"
    assert "restarted" in run_store.get_run(stuck["id"])["error"]


def test_keeps_only_newest_runs(monkeypatch):
    monkeypatch.setattr(run_store, "MAX_RUNS", 3)
    ids = [run_store.create_run({"n": i})["id"] for i in range(5)]
    with open(run_store.STORE_PATH, encoding="utf-8") as f:
        assert len(json.load(f)) == 3
    assert run_store.get_run(ids[0]) is None and run_store.get_run(ids[-1]) is not None
