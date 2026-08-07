"""Queue-control behaviour of the Idea Studio dashboard.

The worker renders one video at a time, so anything that leaves a child process
running forever blocks every idea queued behind it until the next redeploy.
"""
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

pytest.importorskip("flask", reason="web_app needs Flask; it ships in requirements.txt")

# web_app builds its Archive from BOT_DATA_DIR at import time, so point it at a
# scratch directory before importing. start_worker() only runs under __main__.
os.environ.setdefault("BOT_DATA_DIR", tempfile.mkdtemp(prefix="idea-studio-tests-"))
sys.path.insert(0, str(Path(__file__).parents[1]))

import web_app  # noqa: E402


@pytest.fixture
def client():
    return web_app.app.test_client()


def archive():
    """A fresh handle: Flask tears the thread-local connection down per request."""
    return web_app.get_archive()


def test_cancelling_a_queued_idea_takes_it_out_of_the_worker_queue(client):
    idea_id = archive().enqueue_idea("long", "an idea waiting in line", None, True, "private")

    response = client.post(f"/cancel/{idea_id}")

    assert response.status_code == 200
    assert response.get_json() == {"ok": True, "stopped": False}
    row = archive().get_idea(idea_id)
    assert row["status"] == "failed"
    assert "hu" in (row["error"] or "").lower()
    # The point of cancelling: the worker must never pick it up again.
    assert archive().claim_next_idea() is None


def test_cancelling_a_running_idea_kills_the_render(client):
    idea_id = archive().enqueue_idea("long", "an idea being rendered", None, True, "private")
    archive().update_idea(idea_id, "processing")
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    with web_app._current_job_lock:
        web_app._current_job = (idea_id, child)
    try:
        response = client.post(f"/cancel/{idea_id}")

        assert response.status_code == 200
        assert response.get_json()["stopped"] is True
        deadline = time.monotonic() + 25
        while child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.1)
        assert child.poll() is not None
    finally:
        with web_app._current_job_lock:
            web_app._current_job = None
        if child.poll() is None:
            child.kill()


def test_cancel_rejects_jobs_that_are_missing_or_already_finished(client):
    assert client.post("/cancel/999999").status_code == 404

    idea_id = archive().enqueue_idea("short", "already finished", None, True, "private")
    archive().update_idea(idea_id, "done")

    response = client.post(f"/cancel/{idea_id}")
    assert response.status_code == 400
    assert response.get_json()["ok"] is False
    # A finished job keeps its result rather than being relabelled.
    assert archive().get_idea(idea_id)["status"] == "done"


def test_worker_kills_a_render_that_overruns_its_timeout(monkeypatch):
    idea_id = archive().enqueue_idea("long", "a render that hangs forever", None, True, "private")
    monkeypatch.setattr(web_app, "JOB_TIMEOUT_SECONDS", 2.0)
    real_popen = subprocess.Popen
    monkeypatch.setattr(
        web_app.subprocess,
        "Popen",
        lambda *_args, **kwargs: real_popen(
            [sys.executable, "-c", "import time; time.sleep(120)"], cwd=kwargs.get("cwd")
        ),
    )

    started = time.monotonic()
    assert web_app.process_one(archive()) is True
    elapsed = time.monotonic() - started

    # Without the timeout the worker sat in wait() forever and nothing else ran.
    assert elapsed < 20
    row = archive().get_idea(idea_id)
    assert row["status"] == "failed"
    assert "timeout" in row["error"]
