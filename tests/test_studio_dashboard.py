"""Authenticated upload, review boundaries and legacy queue controls."""
import io
import json
import shutil
import subprocess
from pathlib import Path

import pytest

import web_app
import youtube_shorts_bot as legacy
from movie_analysis.core import file_digest, read_json, write_json


@pytest.fixture
def studio(monkeypatch, tmp_path):
    web_app.reset_archive()
    monkeypatch.setattr(web_app, "Archive", lambda: legacy.Archive(tmp_path / "queue.db"))
    monkeypatch.setattr(web_app, "ACCESS_TOKEN", "studio-test-password")
    monkeypatch.setattr(legacy, "DATA_DIR", tmp_path)
    monkeypatch.setattr(legacy, "require_tools", lambda: None)
    monkeypatch.setattr("movie_analysis.web.inspect_source", lambda *_: {"duration": 4})
    client = web_app.app.test_client()
    client.post("/login", data={"token": "studio-test-password"})
    client.get("/movies")
    with client.session_transaction() as session:
        csrf = session["movie_csrf"]
        studio_csrf = session["studio_csrf"]
    yield client, csrf, studio_csrf
    web_app.reset_archive()


def upload(studio, **fields):
    client, csrf, _ = studio
    response = client.post("/movies/submit", data={"csrf": csrf, "video": (io.BytesIO(b"test-video"), "clip.mp4"), **fields})
    return response


def test_profile_upload_is_validated_saved_and_passed_to_worker(studio):
    response = upload(studio, context_preset="avengers-doomsday", movie_title="Avengers: Doomsday")
    assert response.status_code == 302
    client, _, _ = studio
    job_id = int(response.location.rsplit("/", 1)[1])
    row = web_app.get_archive().get_idea(job_id)
    data = json.loads(row["idea"])
    context = read_json(Path(data["context_file"]))
    assert len(context["characters"]) == 3
    assert context["review_scope"] == "trailer"
    assert row["publish"] == 0
    assert client.get(response.location).status_code == 200
    assert client.get(response.location + "/status").json["stage"] == "queued"


def test_auto_identity_ui_needs_no_profile_and_shows_unconfirmed_candidates(studio):
    client, _, _ = studio
    assert "Tự nhận diện từ video" in client.get("/movies").get_data(as_text=True)
    response = upload(studio, movie_title="Synthetic Film")
    job_id = int(response.location.rsplit("/", 1)[1])
    data = json.loads(web_app.get_archive().get_idea(job_id)["idea"])
    directory = Path(data["directory"])
    assert read_json(Path(data["context_file"]))["characters"] == []
    write_json(directory / "characters.json", {"mode": "automatic", "model": "gpt-5.4-mini", "accepted": [],
        "status": "no_verified_matches", "rejected": [{"profile": {"spoken_name": "Batman"},
        "verification": {"reason": "Cảnh quá mờ"}, "rejection_reason": "Cảnh quá mờ, chưa xác nhận được"}]})
    page = client.get(response.location).get_data(as_text=True)
    assert "Tự nhận diện từ video" in page and "Nhân vật chưa được xác nhận" in page
    assert "Batman" in page and "Cảnh quá mờ, chưa xác nhận được" in page


def test_shared_retry_state_shows_current_timing_error_instead_of_old_audit_error(studio):
    client, _, _ = studio
    response = upload(studio)
    job_id = int(response.location.rsplit("/", 1)[1])
    row = web_app.get_archive().get_idea(job_id)
    directory = Path(json.loads(row["idea"])["directory"])
    web_app.get_archive().update_idea(job_id, "failed", error="Script audit phải kiểm tra mọi câu.")
    current_error = "Beat b02 phải còn tối đa 47 từ theo giọng đọc thực tế."
    write_json(directory / "job.json", {"status": "failed", "stage": "timing_script", "error": current_error})
    page = client.get(response.location).get_data(as_text=True)
    assert current_error in page and "Script audit phải kiểm tra mọi câu." not in page
    assert "Rút gọn lời bình theo thời lượng" in page
    status = client.get(response.location + "/status").json
    assert status["error"] == current_error and status["step"] == 3


@pytest.mark.parametrize("fields", [
    {"context_preset": "avengers-doomsday", "movie_title": "A different film"},
    {"context_file": (io.BytesIO(b"[]"), "bad.json")},
    {"context_file": (io.BytesIO(b"x" * 65537), "large.json")},
    {"rights_status": "owned", "rights_basis": ""},
    {"visual_mode": "loop-everything"},
])
def test_bad_profile_or_rights_never_enqueues(studio, fields):
    assert upload(studio, **fields).status_code == 400
    assert web_app.get_archive().recent_ideas() == []


def test_review_keeps_unapproved_candidate_separate_from_old_approved(studio):
    response = upload(studio)
    client, _, _ = studio
    job_id = int(response.location.rsplit("/", 1)[1])
    row = web_app.get_archive().get_idea(job_id)
    directory = Path(json.loads(row["idea"])["directory"])
    write_json(directory / "script.response.json", {"data": {"beats": [{"voiceover": "Latest candidate"}]}})
    write_json(directory / "script_approved.json", {"script": {"beats": [{"voiceover": "Older approved"}]}})
    write_json(directory / "evidence.json", {"test": True})
    (directory / "movie_review.mp4").write_bytes(b"old-output")
    web_app.get_archive().update_idea(job_id, "failed", error="Audit failed")
    page = client.get(response.location).get_data(as_text=True)
    assert "Latest candidate" in page and "Older approved" in page and "CHƯA DUYỆT" in page
    assert client.get(response.location + "/media/movie_review.mp4").status_code == 404
    assert client.get(response.location + "/media/evidence.json").status_code == 200
    assert client.get(response.location + "/media/source.mp4").status_code == 404
    with client.session_transaction() as session:
        session.clear()
    assert client.get(response.location + "/status").status_code == 302


def test_legacy_controls_require_csrf_and_respect_unchecked_publish(studio):
    client, _, csrf = studio
    assert client.get("/").status_code == 200
    assert client.post("/submit", data={"mode": "long", "idea": "A topic"}).status_code == 400
    response = client.post("/submit", data={"csrf": csrf, "mode": "long", "idea": "A topic"})
    job_id = response.json["id"]
    assert not web_app.get_archive().get_idea(job_id)["publish"]
    assert client.post(f"/cancel/{job_id}").status_code == 400
    assert client.post(f"/cancel/{job_id}", data={"csrf": csrf}).status_code == 200
    assert client.get("/api/jobs").json[0]["status"] == "failed"


def test_status_endpoint_refuses_queue_paths_outside_movie_root(studio, tmp_path):
    client, _, _ = studio
    job_id = web_app.get_archive().enqueue_idea("movie", json.dumps({"directory": str(tmp_path), "filename": "a.mp4"}), 60, False, "private")
    assert client.get(f"/movies/{job_id}/status").status_code == 404


def test_large_requests_return_actionable_json(studio, monkeypatch):
    client, _, _ = studio
    monkeypatch.setitem(web_app.app.config, "MAX_CONTENT_LENGTH", 100)
    response = upload(studio)
    assert response.status_code == 413 and response.json["error"]


@pytest.mark.parametrize("visual_mode", ["continuous", "evidence"])
def test_worker_receives_uploaded_profile_and_completed_media_is_accessible(studio, monkeypatch, visual_mode):
    from movie_analysis.pipeline import run_queued_movie
    response = upload(studio, context_preset="avengers-doomsday", visual_mode=visual_mode)
    client, _, _ = studio
    job_id = int(response.location.rsplit("/", 1)[1])
    archive = web_app.get_archive()
    archive.update_idea(job_id, "processing")
    row = archive.get_idea(job_id)
    data = json.loads(row["idea"])
    directory = Path(data["directory"])
    received = {}
    def render(_source, target, _config, *_args, **kwargs):
        received.update(kwargs)
        received["visual_mode"] = _config.visual_mode
        write_json(target / "job.json", {"title": "Completed review", "stage": "complete"})
        write_json(target / "script_approved.json", {"script": {"beats": [{"voiceover": "Approved narration"}]}})
        (target / "movie_review.mp4").write_bytes(b"completed-media")
    monkeypatch.setattr("movie_analysis.pipeline.run_movie", render)
    assert run_queued_movie(archive, row) == 0
    assert received["context_file"] == Path(data["context_file"])
    assert received["visual_mode"] == visual_mode == data["visual_mode"]
    assert client.get(response.location + "/media/movie_review.mp4?download=1").data == b"completed-media"
    page = client.get(response.location).get_data(as_text=True)
    assert "Approved narration" in page and "ĐÃ DUYỆT" in page
    assert "BẢN NHÁP · CHƯA DUYỆT" not in page


def test_retry_preserves_source_identity_and_paid_cache_revision(studio):
    response = upload(studio)
    client, csrf, _ = studio
    job_id = int(response.location.rsplit("/", 1)[1])
    row = web_app.get_archive().get_idea(job_id)
    directory = Path(json.loads(row["idea"])["directory"])
    write_json(directory / "job.json", {"status": "failed", "stage": "script_audit", "source_sha256": "stable-hash",
               "script_revision": 3, "regeneration_id": "previous", "error": "old error"})
    web_app.get_archive().update_idea(job_id, "failed")
    assert client.post(response.location + "/retry", data={"csrf": csrf, "action": "resume"}).status_code == 302
    state = read_json(directory / "job.json")
    assert state["stage"] == "queued" and state["error"] is None
    assert state["source_sha256"] == "stable-hash" and state["script_revision"] == 3


def test_retry_frontend_submits_missing_error_element_and_handles_timeout():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required to execute the dashboard JavaScript")
    result = subprocess.run([node, str(Path(__file__).with_name("test_studio_frontend.js"))],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert "11 dashboard retry behaviours passed" in result.stdout


def test_retry_browser_uses_route_attribute_with_named_action_input(studio, tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required to execute the dashboard JavaScript")
    response = upload(studio)
    client, _, _ = studio
    job_id = int(response.location.rsplit("/", 1)[1])
    data = json.loads(web_app.get_archive().get_idea(job_id)["idea"])
    web_app.get_archive().update_idea(job_id, "failed")
    html = client.get(response.location).get_data(as_text=True)
    Path(data["source"]).unlink()
    restore_html = client.get(response.location).get_data(as_text=True)
    fixture = tmp_path / "retry-browser.json"
    fixture.write_text(json.dumps({"review_path": response.location, "review_html": html,
                                   "restore_html": restore_html}), encoding="utf-8")
    result = subprocess.run([node, str(Path(__file__).with_name("test_studio_frontend_browser.js")), str(fixture)],
                            capture_output=True, text=True, timeout=45)
    if result.returncode == 77:
        pytest.skip("Playwright is optional for the real DOM regression test")
    assert result.returncode == 0, result.stderr
    assert "4 real DOM retry behaviours passed" in result.stdout


def test_retry_legacy_redirect_lands_on_new_review_without_enqueuing_twice(studio):
    response = upload(studio)
    client, csrf, _ = studio
    job_id = int(response.location.rsplit("/", 1)[1])
    web_app.get_archive().update_idea(job_id, "failed")
    # Browsers following the legacy redirect receive HTML, not a JSON ack.
    resumed = client.post(response.location + "/retry", data={"csrf": csrf, "action": "resume"},
                          follow_redirects=True)
    assert resumed.status_code == 200 and resumed.mimetype == "text/html"
    assert len(resumed.history) == 1 and resumed.history[0].status_code == 302
    new_path = resumed.history[0].location
    assert resumed.request.path == new_path and new_path != response.location
    new_id = int(new_path.rsplit("/", 1)[1])
    assert web_app.get_archive().get_idea(new_id)["status"] == "pending"
    assert len(web_app.get_archive().recent_ideas()) == 2
    assert "studio.js?v=timed-style-4" in resumed.get_data(as_text=True)


def test_retry_json_acknowledges_committed_job_before_loading_review(studio, caplog):
    response = upload(studio)
    client, csrf, _ = studio
    job_id = int(response.location.rsplit("/", 1)[1])
    web_app.get_archive().update_idea(job_id, "failed")
    with caplog.at_level("INFO", logger="movie.web"):
        resumed = client.post(response.location + "/retry", data={"csrf": csrf, "action": "resume"},
                              headers={"Accept": "application/json"})
    assert resumed.status_code == 200 and resumed.json["ok"] is True
    new_id = resumed.json["job_id"]
    assert resumed.json["redirect_url"] == f"/movies/{new_id}"
    assert web_app.get_archive().get_idea(new_id)["status"] == "pending"
    assert f"queued new_id={new_id}" in caplog.text
    assert len(web_app.get_archive().recent_ideas()) == 2


def test_retry_server_error_returns_json_without_leaking_connection_details(studio, monkeypatch, caplog):
    client, csrf, _ = studio
    def broken():
        raise RuntimeError("secret-database-connection-information")
    monkeypatch.setattr(web_app, "Archive", broken)
    web_app.reset_archive()
    response = client.post("/movies/18/retry", data={"csrf": csrf, "action": "resume"},
                           headers={"Accept": "application/json"})
    assert response.status_code == 503 and response.json["ok"] is False
    assert "secret-database" not in response.get_data(as_text=True) + caplog.text


@pytest.mark.parametrize("mounted", [False, True])
def test_railway_rejects_uploads_without_storage_on_its_volume(studio, monkeypatch, tmp_path, mounted):
    monkeypatch.setenv("RAILWAY_SERVICE_ID", "dashboard-test")
    if mounted:
        monkeypatch.setenv("RAILWAY_VOLUME_MOUNT_PATH", str(tmp_path / "other-volume"))
    else:
        monkeypatch.delenv("RAILWAY_VOLUME_MOUNT_PATH", raising=False)
    response = upload(studio)
    assert response.status_code == 409 and "Volume" in response.json["error"]
    assert web_app.get_archive().recent_ideas() == []
    page = studio[0].get("/movies").get_data(as_text=True)
    assert "Cần cấu hình nơi lưu video" in page


def test_railway_upload_uses_mounted_volume_and_records_recovery_data(studio, monkeypatch):
    monkeypatch.setenv("RAILWAY_SERVICE_ID", "dashboard-test")
    monkeypatch.setenv("RAILWAY_VOLUME_MOUNT_PATH", str(legacy.DATA_DIR))
    response = upload(studio, context_preset="avengers-doomsday")
    assert response.status_code == 302
    row = web_app.get_archive().get_idea(int(response.location.rsplit("/", 1)[1]))
    data = json.loads(row["idea"])
    assert data["source_sha256"] == file_digest(Path(data["source"]))
    assert data["uploaded_context"] == read_json(Path(data["context_file"]))


def test_missing_source_blocks_retry_until_matching_file_restores_saved_job(studio):
    response = upload(studio, context_preset="avengers-doomsday")
    client, csrf, _ = studio
    archive = web_app.get_archive()
    job_id = int(response.location.rsplit("/", 1)[1])
    data = json.loads(archive.get_idea(job_id)["idea"])
    directory = Path(data["directory"])
    source = Path(data["source"])
    source.unlink()
    Path(data["context_file"]).unlink()
    write_json(directory / "global.json", {"preserved": True})
    write_json(directory / "job.json", {"source_sha256": data["source_sha256"], "script_revision": 2})
    archive.update_idea(job_id, "failed")
    page = client.get(response.location).get_data(as_text=True)
    assert "Khôi phục video và tiếp tục" in page and "Tiếp tục từ bước đã lưu" not in page
    post = {"csrf": csrf, "action": "resume"}
    assert client.post(response.location + "/retry", data=post).status_code == 409
    wrong = client.post(response.location + "/retry", data={**post, "video": (io.BytesIO(b"different-file"), "clip.mp4")})
    assert wrong.status_code == 400 and "khác file gốc" in wrong.json["error"]
    assert not source.exists() and not list(directory.glob("restore-*.mp4"))
    assert len(web_app.get_archive().recent_ideas()) == 1
    restored = client.post(response.location + "/retry", data={**post, "video": (io.BytesIO(b"test-video"), "clip.mp4")})
    assert restored.status_code == 302 and source.read_bytes() == b"test-video"
    assert read_json(directory / "global.json") == {"preserved": True}
    assert read_json(directory / "job.json")["script_revision"] == 2
    assert read_json(Path(data["context_file"])) == data["uploaded_context"]
    fresh = json.loads(web_app.get_archive().get_idea(int(restored.location.rsplit("/", 1)[1]))["idea"])
    assert fresh["source_sha256"] == data["source_sha256"] and fresh["directory"] == data["directory"]


def test_old_job_without_source_or_identity_requires_fresh_upload(studio):
    response = upload(studio)
    client, csrf, _ = studio
    archive = web_app.get_archive()
    first_id = int(response.location.rsplit("/", 1)[1])
    data = json.loads(archive.get_idea(first_id)["idea"])
    del data["source_sha256"]
    del data["uploaded_context"]
    Path(data["source"]).unlink()
    archive.update_idea(first_id, "failed")
    old_id = archive.enqueue_idea("movie", json.dumps(data), 60, False, "private")
    archive.update_idea(old_id, "failed")
    page = client.get(f"/movies/{old_id}").get_data(as_text=True)
    assert "Upload video và tạo job mới" in page and "Khôi phục video và tiếp tục" not in page
    retried = client.post(f"/movies/{old_id}/retry", data={"csrf": csrf, "action": "resume",
                          "video": (io.BytesIO(b"test-video"), "clip.mp4")})
    assert retried.status_code == 409 and "mã đối chiếu" in retried.json["error"]
    assert len(web_app.get_archive().recent_ideas()) == 2


def test_worker_missing_source_fails_before_any_ai_or_tts(studio, monkeypatch):
    from movie_analysis.pipeline import run_queued_movie
    response = upload(studio)
    job_id = int(response.location.rsplit("/", 1)[1])
    archive = web_app.get_archive()
    data = json.loads(archive.get_idea(job_id)["idea"])
    Path(data["source"]).unlink()
    archive.update_idea(job_id, "processing")
    def forbidden(*args, **kwargs):
        raise AssertionError("No AI/TTS may run with a missing source")
    monkeypatch.setattr("movie_analysis.pipeline.run_movie", forbidden)
    with pytest.raises(legacy.BotError, match="Không còn video gốc"):
        run_queued_movie(archive, archive.get_idea(job_id))
    assert archive.get_idea(job_id)["status"] == "failed"


def test_script_retry_can_change_tone_without_changing_input_or_render_mode(studio, monkeypatch):
    from movie_analysis.pipeline import run_queued_movie
    response = upload(studio, visual_mode="continuous", context_preset="avengers-doomsday")
    client, csrf, _ = studio
    job_id = int(response.location.rsplit("/", 1)[1])
    archive = web_app.get_archive()
    original = json.loads(archive.get_idea(job_id)["idea"])
    archive.update_idea(job_id, "done")
    new_style = "Tấu hài hơi lầy, dùng sắp toang đúng tình huống."
    retried = client.post(response.location + "/retry", data={"csrf": csrf, "action": "script", "narration_style": new_style})
    assert retried.status_code == 302
    new_id = int(retried.location.rsplit("/", 1)[1])
    archive = web_app.get_archive()
    fresh = json.loads(archive.get_idea(new_id)["idea"])
    assert fresh["narration_style"] == new_style and fresh["regenerate"] == "script"
    assert all(fresh[key] == original[key] for key in ("source", "directory", "context_file", "visual_mode"))
    def render(_source, target, _config, *_args, **kwargs):
        assert kwargs["narration_style"] == new_style and kwargs["regenerate_script"]
        write_json(target / "job.json", {"title": "Changed tone"})
    monkeypatch.setattr("movie_analysis.pipeline.run_movie", render)
    archive.update_idea(new_id, "processing")
    assert run_queued_movie(archive, archive.get_idea(new_id)) == 0
    page = client.get(retried.location).get_data(as_text=True)
    assert "Đổi phong cách lời bình" in page and new_style in page


def test_invalid_retry_tone_does_not_enqueue_another_paid_job(studio):
    response = upload(studio)
    client, csrf, _ = studio
    job_id = int(response.location.rsplit("/", 1)[1])
    archive = web_app.get_archive()
    archive.update_idea(job_id, "done")
    retried = client.post(response.location + "/retry", data={"csrf": csrf, "action": "script", "narration_style": "x" * 1001})
    assert retried.status_code == 400 and len(web_app.get_archive().recent_ideas()) == 1
