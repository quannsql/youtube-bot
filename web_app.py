"""Frontend nhập ý tưởng thủ công cho YouTube bot.

Chạy như một Railway service riêng, luôn bật:  python web_app.py

- Phục vụ form nhập ý tưởng + bảng theo dõi job (tiếng Việt).
- Ghi ý tưởng vào bảng idea_queue (dùng chung Postgres với các service auto).
- Một worker thread lần lượt nhặt ý tưởng pending và chạy:
      python youtube_shorts_bot.py --idea-id <id>
  Bot tự sinh kế hoạch từ ý tưởng, render và (mặc định) đăng thẳng YouTube,
  rồi cập nhật trạng thái row trong idea_queue.

Luồng auto (2 service Cron) không bị đụng tới.
"""

from __future__ import annotations

import logging
import json
import os
import secrets
import subprocess
import sys
import threading
import time
from functools import wraps

from flask import (
    Flask,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from youtube_shorts_bot import (
    ROOT,
    Archive,
    describe_manual_idea,
    encode_comparison_idea,
)

LOG = logging.getLogger("web_app")
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s", datefmt="%H:%M:%S")

BOT_SCRIPT = ROOT / "youtube_shorts_bot.py"
ACCESS_TOKEN = os.getenv("WEB_ACCESS_TOKEN", "").strip()
DEFAULT_PRIVACY = os.getenv("YOUTUBE_PRIVACY_STATUS", "private").strip() or "private"
WORKER_POLL_SECONDS = float(os.getenv("WEB_WORKER_POLL_SECONDS", "3"))
# The worker renders one video at a time, so a child that never exits blocks
# every idea queued behind it — the queue only recovers on a redeploy. A long
# video legitimately takes ~30 minutes, so the ceiling is generous.
JOB_TIMEOUT_SECONDS = float(os.getenv("WEB_JOB_TIMEOUT_SECONDS", "5400"))
PRIVACY_CHOICES = ("private", "unlisted", "public")

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY") or os.urandom(32).hex()
app.config["MAX_CONTENT_LENGTH"] = (int(os.getenv("MOVIE_MAX_INPUT_MB", "500")) + 2) * 1024 * 1024
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")
if os.getenv("RAILWAY_ENVIRONMENT_ID"):
    app.config["SESSION_COOKIE_SECURE"] = True


def studio_csrf():
    if not session.get("studio_csrf"):
        session["studio_csrf"] = secrets.token_urlsafe(32)
    return session["studio_csrf"]


@app.context_processor
def studio_context():
    return {"require_auth": bool(ACCESS_TOKEN), "studio_csrf": studio_csrf(),
            "preview_mode": app.config.get("STUDIO_PREVIEW", False)}


@app.before_request
def protect_manual_actions():
    if request.method == "POST" and request.endpoint in {"submit", "cancel"} and ACCESS_TOKEN and session.get("authed"):
        token = request.form.get("csrf", "") or request.headers.get("X-CSRF-Token", "")
        if not secrets.compare_digest(token, session.get("studio_csrf", "")) or not token:
            return jsonify(ok=False, error="Phiên đã hết hạn. Tải lại trang rồi thử lại."), 400

# Mỗi thread (worker + các request-handler của Flask) giữ 1 connection riêng.
_local = threading.local()


def get_archive() -> Archive:
    archive = getattr(_local, "archive", None)
    if archive is None:
        archive = Archive()
        _local.archive = archive
    return archive


def reset_archive() -> None:
    """Đóng connection của thread hiện tại (sau mỗi request, hoặc khi worker gặp lỗi)."""
    archive = getattr(_local, "archive", None)
    if archive is not None:
        try:
            archive.conn.close()
        except Exception:
            pass
        _local.archive = None


@app.teardown_request
def _teardown_request(_exc=None):
    # Werkzeug tạo thread mới mỗi request; đóng connection để không rò rỉ trên Postgres.
    reset_archive()


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #
def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not ACCESS_TOKEN:  # Không đặt token => mở (tiện chạy local).
            return view(*args, **kwargs)
        if session.get("authed"):
            return view(*args, **kwargs)
        return redirect(url_for("login"))

    return wrapper


@app.route("/login", methods=["GET", "POST"])
def login():
    if not ACCESS_TOKEN:
        return redirect(url_for("index"))
    error = None
    if request.method == "POST":
        if request.form.get("token", "") == ACCESS_TOKEN:
            session["authed"] = True
            session.permanent = True
            return redirect(url_for("index"))
        error = "Sai mật khẩu."
    return render_template("studio/login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.route("/")
@login_required
def index():
    return render_template(
        "studio/index.html",
        default_privacy=DEFAULT_PRIVACY,
        privacy_choices=PRIVACY_CHOICES,
    )


@app.route("/submit", methods=["POST"])
@login_required
def submit():
    mode = (request.form.get("mode") or "long").strip().lower()
    privacy = (request.form.get("privacy") or DEFAULT_PRIVACY).strip().lower()
    publish = request.form.get("publish", "off") in ("on", "true", "1")
    if mode not in ("short", "long"):
        mode = "long"
    if privacy not in PRIVACY_CHOICES:
        privacy = DEFAULT_PRIVACY

    if mode == "short":
        # Shorts are A-vs-B comparisons, so the two subjects are captured as separate
        # fields and stored structured — the bot never has to parse them back out.
        subject_a = (request.form.get("subject_a") or "").strip()
        subject_b = (request.form.get("subject_b") or "").strip()
        angle = (request.form.get("angle") or "").strip()
        if not subject_a or not subject_b:
            return jsonify({"ok": False, "error": "Cần nhập cả 2 đối tượng để so sánh."}), 400
        if subject_a.casefold() == subject_b.casefold():
            return jsonify({"ok": False, "error": "Hai đối tượng phải khác nhau."}), 400
        idea = encode_comparison_idea(subject_a, subject_b, angle)
    else:
        idea = (request.form.get("idea") or "").strip()
        if not idea:
            return jsonify({"ok": False, "error": "Ý tưởng đang trống."}), 400

    # Video luôn khớp theo độ dài giọng đọc, nên không thu "thời lượng" từ form.
    idea_id = get_archive().enqueue_idea(mode, idea, None, publish, privacy)
    LOG.info("Enqueued manual idea id=%s mode=%s publish=%s", idea_id, mode, publish)
    return jsonify({"ok": True, "id": idea_id})


@app.route("/api/jobs")
@login_required
def api_jobs():
    rows = get_archive().recent_ideas(limit=40)
    jobs = []
    for row in rows:
        jobs.append(
            {
                "id": row.get("id"),
                "created_at": row.get("created_at"),
                "updated_at": row.get("updated_at"),
                "mode": row.get("mode"),
                # Structured A-vs-B payloads are stored as JSON; show them readably.
                "idea": ("Phân tích MP4: " + json.loads(row["idea"]).get("filename", "video"))
                if row.get("mode") == "movie" else describe_manual_idea(str(row.get("idea") or "")),
                "review_url": url_for("movie_review", idea_id=row["id"]) if row.get("mode") == "movie" else None,
                "duration": row.get("duration"),
                "publish": bool(row.get("publish")),
                "privacy": row.get("privacy"),
                "status": row.get("status"),
                "youtube_id": row.get("youtube_id"),
                "output_title": row.get("output_title"),
                "error": row.get("error"),
            }
        )
    return jsonify(jobs)


from movie_analysis.web import register_movie_routes
register_movie_routes(app, login_required, get_archive)


@app.route("/cancel/<int:idea_id>", methods=["POST"])
@login_required
def cancel(idea_id: int):
    """Drop a queued or stuck job so the next idea can start straight away."""
    archive = get_archive()
    row = archive.get_idea(idea_id)
    if not row:
        return jsonify({"ok": False, "error": "Không tìm thấy job."}), 404
    status = str(row.get("status") or "")
    if status not in ("pending", "processing"):
        return jsonify({"ok": False, "error": f"Job đang ở trạng thái '{status}', không cần huỷ."}), 400
    # Mark it first: process_one only overwrites a row that is still
    # 'processing', so this is what stops the worker relabelling it on exit.
    archive.update_idea(idea_id, "failed", error="Đã huỷ từ dashboard.")
    stopped = stop_running_job(idea_id)
    LOG.info("Đã huỷ idea id=%s (tiến trình đang chạy: %s).", idea_id, stopped)
    return jsonify({"ok": True, "stopped": stopped})


@app.route("/healthz")
def healthz():
    return "ok", 200


@app.errorhandler(413)
def upload_too_large(_error):
    return jsonify(ok=False, error="File quá lớn. Chọn MP4 nhỏ hơn giới hạn hiển thị trên form."), 413


@app.errorhandler(400)
def bad_request(error):
    return jsonify(ok=False, error=error.description), 400


# --------------------------------------------------------------------------- #
# Worker: nhặt ý tưởng pending -> chạy bot subprocess (render tuần tự 1 luồng)
# --------------------------------------------------------------------------- #
# Handle on the render currently running, so a timeout or the dashboard's cancel
# button can actually stop it instead of waiting for the next redeploy.
_current_job_lock = threading.Lock()
_current_job: tuple[int, subprocess.Popen] | None = None


def stop_running_job(idea_id: int) -> bool:
    """Kill the child process if it is the one rendering ``idea_id``."""
    with _current_job_lock:
        running = _current_job
    if not running or running[0] != idea_id:
        return False
    process = running[1]
    process.terminate()
    try:
        process.wait(timeout=20)
    except subprocess.TimeoutExpired:
        process.kill()
    return True


def process_one(archive: Archive) -> bool:
    global _current_job

    row = archive.claim_next_idea()
    if not row:
        return False
    idea_id = int(row["id"])
    LOG.info("Worker bắt đầu idea id=%s (mode=%s)", idea_id, row.get("mode"))
    cmd = [sys.executable, "-u", str(BOT_SCRIPT), "--idea-id", str(idea_id)]
    try:
        process = subprocess.Popen(cmd, cwd=str(ROOT))
    except Exception as exc:  # không thể spawn được tiến trình
        LOG.exception("Không chạy được bot cho idea id=%s: %s", idea_id, exc)
        archive.update_idea(idea_id, "failed", error=f"spawn error: {exc}"[:2000])
        return True
    with _current_job_lock:
        _current_job = (idea_id, process)
    timed_out = False
    try:
        returncode = process.wait(timeout=JOB_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        timed_out = True
        LOG.error(
            "Idea id=%s chạy quá %.0fs; dừng tiến trình để giải phóng hàng đợi.",
            idea_id, JOB_TIMEOUT_SECONDS,
        )
        process.kill()
        returncode = process.wait()
    finally:
        with _current_job_lock:
            _current_job = None
    # Bot tự set done/failed. Nếu thoát lỗi mà row vẫn 'processing' -> đánh dấu failed.
    # Job đã huỷ từ dashboard không còn 'processing' nên không bị ghi đè ở đây.
    fresh = archive.get_idea(idea_id)
    if fresh and fresh.get("status") == "processing":
        if timed_out:
            archive.update_idea(idea_id, "failed", error=f"timeout sau {JOB_TIMEOUT_SECONDS:.0f}s")
        else:
            status = "failed" if returncode != 0 else "done"
            archive.update_idea(idea_id, status, error=(f"exit code {returncode}" if returncode != 0 else None))
    LOG.info("Worker xong idea id=%s (exit=%s)", idea_id, returncode)
    return True


def worker_loop() -> None:
    LOG.info("Idea worker đã khởi động (poll mỗi %.1fs).", WORKER_POLL_SECONDS)
    while True:
        worked = False
        try:
            worked = process_one(get_archive())
        except Exception as exc:
            LOG.exception("Lỗi vòng lặp worker: %s", exc)
            reset_archive()  # kết nối có thể đã hỏng; tạo lại ở vòng sau
        if not worked:
            time.sleep(WORKER_POLL_SECONDS)


def start_worker() -> None:
    try:
        recovered = get_archive().recover_stuck_ideas()
        if recovered:
            LOG.info("Đưa %d ý tưởng đang treo về pending sau khi khởi động lại.", recovered)
    except Exception as exc:
        LOG.warning("Không recover được idea treo: %s", exc)
    finally:
        reset_archive()  # worker thread sẽ mở connection riêng của nó
    thread = threading.Thread(target=worker_loop, name="idea-worker", daemon=True)
    thread.start()


if __name__ == "__main__":
    if os.getenv("RAILWAY_ENVIRONMENT_ID") and (not ACCESS_TOKEN or not os.getenv("FLASK_SECRET_KEY")):
        raise RuntimeError("Service Railway cần WEB_ACCESS_TOKEN và FLASK_SECRET_KEY ổn định.")
    if not ACCESS_TOKEN:
        LOG.warning("WEB_ACCESS_TOKEN chưa được đặt — trang web đang MỞ cho mọi người. Chỉ nên vậy khi chạy local.")
    start_worker()
    port = int(os.environ.get("PORT", "8080"))
    LOG.info("Idea Studio chạy tại http://0.0.0.0:%d", port)
    from waitress import serve
    serve(app, host="0.0.0.0", port=port, threads=8,
          max_request_body_size=app.config["MAX_CONTENT_LENGTH"], channel_timeout=300)
