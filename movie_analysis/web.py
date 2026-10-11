"""Movie upload/review pages registered on the existing Flask dashboard."""
from __future__ import annotations

import json
import os
import re
import secrets
import uuid
from pathlib import Path

from flask import abort, jsonify, redirect, render_template, request, send_file, session, url_for

import youtube_shorts_bot as legacy
from .core import MovieConfig, RIGHTS_STATES, file_digest, read_json, rights_manifest, write_json
from .media import enable_local_tools, inspect_source
from .narration import DEFAULT_STYLE, validate_context


STEPS = ["Phân tích nội dung", "Kiểm chứng chi tiết", "Nhận diện nhân vật", "Viết lời bình", "Duyệt kịch bản", "Giọng đọc & dựng video"]

MISSING_SOURCE = "Không còn video gốc tại đường dẫn đã lưu. Kiểm tra Volume của service dashboard và BOT_DATA_DIR; tải lại đúng video vào job nếu dữ liệu đối chiếu còn, hoặc tạo job mới."


def storage_warning():
    if not any(os.getenv(key) for key in ("RAILWAY_PROJECT_ID", "RAILWAY_SERVICE_ID", "RAILWAY_ENVIRONMENT_ID")):
        return None
    mount = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "").strip()
    if not mount:
        return "Service dashboard chưa có Volume được Railway khai báo. Gắn Volume tại /data và đặt BOT_DATA_DIR=/data trước khi upload để giữ video/cache qua redeploy."
    if not legacy.DATA_DIR.resolve().is_relative_to(Path(mount).resolve()):
        return "BOT_DATA_DIR đang nằm ngoài Volume của service dashboard. Đặt BOT_DATA_DIR trùng Mount Path của Volume để giữ video/cache qua redeploy."
    return None


def source_recovery_hash(data, directory):
    value = data.get("source_sha256") or read_json(directory / "job.json", {}).get("source_sha256", "")
    return value if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) else None


def save_upload(video, destination, max_mb):
    total = 0
    with destination.open("wb") as output:
        for chunk in iter(lambda: video.stream.read(1024 * 1024), b""):
            total += len(chunk)
            if total > max_mb * 1024 * 1024:
                raise legacy.BotError("MP4 quá giới hạn dung lượng.")
            output.write(chunk)


def stage_index(stage):
    if stage == "complete":
        return 6
    if stage == "render":
        return 5
    if stage == "script_audit":
        return 4
    if stage in {"review_plan", "script"}:
        return 3
    if str(stage).startswith("character"):
        return 2
    if str(stage).startswith(("scene:", "verify:", "recheck:")) or stage == "evidence":
        return 1
    return 0


def stage_label(stage):
    return "Đang chờ trong hàng đợi" if stage in {None, "queued"} else "Hoàn tất" if stage == "complete" else STEPS[stage_index(stage)]


def register_movie_routes(app, login_required, get_archive):
    def csrf():
        token = session.get("movie_csrf")
        if not token:
            token = secrets.token_urlsafe(32)
            session["movie_csrf"] = token
        return token

    def check_csrf():
        value = request.form.get("csrf", "") or request.headers.get("X-CSRF-Token", "")
        if not value or not secrets.compare_digest(value, csrf()):
            abort(400, "Phiên form đã hết hạn; tải lại trang.")

    def job_directory(idea_id):
        row = get_archive().get_idea(idea_id)
        if not row or row.get("mode") != "movie":
            abort(404)
        data = json.loads(row["idea"])
        directory = Path(data["directory"]).resolve()
        root = (legacy.DATA_DIR / "generated" / "movies").resolve()
        if not directory.is_relative_to(root) or directory == root:
            abort(404)
        return row, data, directory

    @app.route("/movies")
    @login_required
    def movies():
        cfg = MovieConfig.from_env()
        return render_template("studio/movies.html", csrf=csrf(), states=RIGHTS_STATES,
                                      max_mb=cfg.max_input_mb, default_style=DEFAULT_STYLE,
                                      max_seconds=min(cfg.max_input_seconds, cfg.live_max_seconds)
                                      if cfg.video_provider == "gemini_live" else cfg.max_input_seconds,
                                      default_visual_mode=cfg.visual_mode, storage_warning=storage_warning())

    @app.route("/movies/submit", methods=["POST"])
    @login_required
    def movie_submit():
        check_csrf()
        warning = storage_warning()
        if warning:
            return jsonify(ok=False, error=warning), 409
        video = request.files.get("video")
        if not video or not video.filename or Path(video.filename).suffix.lower() != ".mp4":
            return jsonify(ok=False, error="Cần chọn file MP4."), 400
        status, basis = request.form.get("rights_status", "unknown"), request.form.get("rights_basis", "")
        try:
            cfg = MovieConfig.from_env(int(request.form.get("target_seconds", "60")))
            visual_mode = request.form.get("visual_mode", cfg.visual_mode)
            if visual_mode not in {"continuous", "evidence"}:
                raise legacy.BotError("Chế độ dựng không hợp lệ.")
            rights_manifest(status, basis, "pending")
            profile = request.files.get("context_file")
            if profile and profile.filename:
                raw = profile.stream.read(65537)
                if len(raw) > 65536:
                    raise legacy.BotError("Hồ sơ JSON tối đa 64 KB.")
                context = validate_context(json.loads(raw))
            elif request.form.get("context_preset") == "avengers-doomsday":
                context = validate_context(read_json(legacy.ROOT / "docs/examples/avengers-doomsday-context.json"))
            else:
                context = validate_context({})
            title = request.form.get("movie_title", "").strip()
            if title and context["title"] and title != context["title"]:
                raise legacy.BotError("Tên phim khác hồ sơ đã chọn. Chọn đúng hồ sơ hoặc bỏ hồ sơ mẫu.")
            context["title"] = title or context["title"]
            context["style"] = request.form.get("narration_style", "").strip() or DEFAULT_STYLE
            background = request.form.get("movie_context", "").strip()
            if background:
                context["facts"] = [f for f in context["facts"] if f["id"] != "ctx_user_1"]
                context["facts"].append({"id": "ctx_user_1", "text": background, "source": "user_provided"})
            context = validate_context(context)
        except (legacy.BotError, ValueError, UnicodeError) as exc:
            return jsonify(ok=False, error=str(exc)), 400
        directory = legacy.DATA_DIR / "generated" / "movies" / uuid.uuid4().hex
        directory.mkdir(parents=True)
        source = directory / "source.mp4"
        # Stream to disk with a second limit for chunked uploads; don't read MP4 into RAM.
        try:
            save_upload(video, source, cfg.max_input_mb)
            enable_local_tools()
            legacy.require_tools()
            source_info = inspect_source(source, cfg)
            if cfg.video_provider == "gemini_live" and source_info["duration"] > cfg.live_max_seconds:
                raise legacy.BotError(f"Video vượt giới hạn Live {cfg.live_max_seconds:g} giây.")
        except (legacy.BotError, ValueError, KeyError) as exc:
            source.unlink(missing_ok=True)
            return jsonify(ok=False, error=str(exc)), 400
        data = {"source": str(source.resolve()), "directory": str(directory.resolve()), "filename": video.filename,
                "source_sha256": file_digest(source), "uploaded_context": context,
                "target_seconds": cfg.target_seconds, "visual_mode": visual_mode, "rights_status": status, "rights_basis": basis,
                "movie_title": context["title"],
                "context_file": str((directory / "upload_context.json").resolve()),
                "narration_style": context["style"]}
        write_json(directory / "upload_context.json", context)
        idea_id = get_archive().enqueue_idea("movie", json.dumps(data, ensure_ascii=False), cfg.target_seconds, False, "private")
        write_json(directory / "job.json", {"status": "pending", "stage": "queued"})
        return redirect(url_for("movie_review", idea_id=idea_id))

    @app.route("/movies/<int:idea_id>")
    @login_required
    def movie_review(idea_id):
        row, data, directory = job_directory(idea_id)
        approved = read_json(directory / "script_approved.json", {})
        candidate = read_json(directory / "script.response.json", {}).get("data", {})
        script = approved.get("script", {}) if row["status"] == "done" else candidate
        return render_template("studio/review.html", row=row, data=data, csrf=csrf(),
            state=read_json(directory / "job.json", {}), evidence=read_json(directory / "evidence.json", {}),
            script=script, previous_approved=approved.get("script", {}) if row["status"] != "done" else {},
            editorial=approved.get("audit", {}).get("quality", {}) if row["status"] == "done" else
                read_json(directory / "script_audit.response.json", {}).get("data", {}).get("quality", {}),
            characters=read_json(directory / "characters.json", {}),
            plan=read_json(directory / "review_plan.json", {}).get("data", {}),
            usage=read_json(directory / "usage.json", {}), quality=read_json(directory / "quality.json", {}),
            steps=STEPS, current_step=stage_index(read_json(directory / "job.json", {}).get("stage")),
            stage_label=stage_label(read_json(directory / "job.json", {}).get("stage")),
            snapshot=snapshot(row, directory),
            ready=row["status"] == "done" and (directory / "movie_review.mp4").is_file(),
            source_available=Path(data["source"]).is_file(),
            can_restore_source=bool(source_recovery_hash(data, directory)) and
                (Path(data["context_file"]).is_file() or bool(data.get("uploaded_context"))),
            storage_warning=storage_warning())

    def snapshot(row, directory):
        state = read_json(directory / "job.json", {})
        stage = state.get("stage", "queued")
        artifacts = [directory / name for name in ("script.response.json", "script_approved.json", "characters.json")]
        return {"status": row["status"], "stage": stage, "step": stage_index(stage),
                "error": row.get("error") or state.get("error"),
                "revision": ":".join(str(p.stat().st_mtime_ns) if p.exists() else "0" for p in artifacts)}

    @app.route("/movies/<int:idea_id>/status")
    @login_required
    def movie_status(idea_id):
        row, _, directory = job_directory(idea_id)
        return jsonify(snapshot(row, directory))

    @app.route("/movies/<int:idea_id>/media/<name>")
    @login_required
    def movie_media(idea_id, name):
        row, _, directory = job_directory(idea_id)
        if name not in {"movie_review.mp4", "preview.jpg", "captions_vi.ass", "evidence.json", "timeline.json", "narration_vi.txt"}:
            abort(404)
        if (row["status"] != "done" and name not in {"evidence.json", "timeline.json"}) or not (directory / name).is_file():
            abort(404)
        return send_file(directory / name, conditional=True, as_attachment=request.args.get("download") == "1")

    @app.route("/movies/<int:idea_id>/retry", methods=["POST"])
    @login_required
    def movie_retry(idea_id):
        check_csrf()
        row, data, directory = job_directory(idea_id)
        action = request.form.get("action", "resume")
        if row["status"] not in {"done", "failed"} or action not in {"resume", "script", "voice"}:
            return jsonify(ok=False, error="Job đang chạy hoặc thao tác không hợp lệ."), 409
        warning = storage_warning()
        if warning:
            return jsonify(ok=False, error=warning), 409
        if action == "script" and "narration_style" in request.form:
            style = request.form["narration_style"].strip()
            if len(style) > 1000:
                return jsonify(ok=False, error="Phong cách lời bình tối đa 1000 ký tự."), 400
            data["narration_style"] = style or DEFAULT_STYLE
        # Atomic OS lock also rules out a CLI rendering this same directory.
        from .core import job_lock
        try:
            with job_lock(directory):
                jobs = get_archive().recent_ideas(limit=1000)
                if any(j["mode"] == "movie" and j["status"] in {"pending", "processing"}
                       and json.loads(j["idea"]).get("directory") == data["directory"] for j in jobs):
                    return jsonify(ok=False, error="Job đã được đưa vào hàng đợi."), 409
                source = Path(data["source"]).resolve()
                if source != directory / "source.mp4":
                    return jsonify(ok=False, error="Đường dẫn video của job không hợp lệ."), 409
                if not source.is_file():
                    expected = source_recovery_hash(data, directory)
                    video = request.files.get("video")
                    if not expected:
                        return jsonify(ok=False, error="Không còn mã đối chiếu video cũ. Hãy tạo job mới; không thể dùng cache cho video chưa xác định."), 409
                    if not video or not video.filename or Path(video.filename).suffix.lower() != ".mp4":
                        return jsonify(ok=False, error=MISSING_SOURCE), 409
                    cfg = MovieConfig.from_env(int(data.get("target_seconds", 60)))
                    temporary = directory / ("restore-" + uuid.uuid4().hex + ".mp4")
                    try:
                        save_upload(video, temporary, cfg.max_input_mb)
                        if file_digest(temporary) != expected:
                            return jsonify(ok=False, error="Video tải lại khác file gốc. Chọn đúng file chưa cắt/chỉnh sửa, hoặc tạo job mới."), 400
                        enable_local_tools()
                        legacy.require_tools()
                        inspect_source(temporary, cfg)
                        context_path = directory / "upload_context.json"
                        if not context_path.is_file():
                            if not data.get("uploaded_context"):
                                return jsonify(ok=False, error="Hồ sơ phim của job đã mất. Hãy tạo job mới với đúng bối cảnh."), 409
                            write_json(context_path, validate_context(data["uploaded_context"]))
                        data["context_file"] = str(context_path)
                        data["source_sha256"] = expected
                        os.replace(temporary, source)
                    finally:
                        temporary.unlink(missing_ok=True)
                data["regenerate"] = action
                new_id = get_archive().enqueue_idea("movie", json.dumps(data, ensure_ascii=False),
                                                   data["target_seconds"], False, "private")
                state = read_json(directory / "job.json", {})
                state.update(status="pending", stage="queued", error=None)
                write_json(directory / "job.json", state)
        except (legacy.BotError, ValueError) as exc:
            return jsonify(ok=False, error=str(exc)), 409
        return redirect(url_for("movie_review", idea_id=new_id))
