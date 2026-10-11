"""Movie upload/review pages registered on the existing Flask dashboard."""
from __future__ import annotations

import json
import secrets
import uuid
from pathlib import Path

from flask import abort, jsonify, redirect, render_template, request, send_file, session, url_for

import youtube_shorts_bot as legacy
from .core import MovieConfig, RIGHTS_STATES, read_json, rights_manifest, write_json
from .media import enable_local_tools, inspect_source
from .narration import DEFAULT_STYLE, validate_context


STEPS = ["Phân tích nội dung", "Kiểm chứng chi tiết", "Nhận diện nhân vật", "Viết lời bình", "Duyệt kịch bản", "Giọng đọc & dựng video"]


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
                                      default_visual_mode=cfg.visual_mode)

    @app.route("/movies/submit", methods=["POST"])
    @login_required
    def movie_submit():
        check_csrf()
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
        total = 0
        try:
            with source.open("wb") as output:
                for chunk in iter(lambda: video.stream.read(1024 * 1024), b""):
                    total += len(chunk)
                    if total > cfg.max_input_mb * 1024 * 1024:
                        raise legacy.BotError("MP4 quá giới hạn dung lượng.")
                    output.write(chunk)
            enable_local_tools()
            legacy.require_tools()
            source_info = inspect_source(source, cfg)
            if cfg.video_provider == "gemini_live" and source_info["duration"] > cfg.live_max_seconds:
                raise legacy.BotError(f"Video vượt giới hạn Live {cfg.live_max_seconds:g} giây.")
        except (legacy.BotError, ValueError, KeyError) as exc:
            source.unlink(missing_ok=True)
            return jsonify(ok=False, error=str(exc)), 400
        data = {"source": str(source.resolve()), "directory": str(directory.resolve()), "filename": video.filename,
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
            ready=row["status"] == "done" and (directory / "movie_review.mp4").is_file())

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
                data["regenerate"] = action
                new_id = get_archive().enqueue_idea("movie", json.dumps(data, ensure_ascii=False),
                                                   data["target_seconds"], False, "private")
                state = read_json(directory / "job.json", {})
                state.update(status="pending", stage="queued", error=None)
                write_json(directory / "job.json", state)
        except legacy.BotError as exc:
            return jsonify(ok=False, error=str(exc)), 409
        return redirect(url_for("movie_review", idea_id=new_id))
