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
import os
import subprocess
import sys
import threading
import time
from functools import wraps

from flask import (
    Flask,
    jsonify,
    redirect,
    render_template_string,
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
PRIVACY_CHOICES = ("private", "unlisted", "public")

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY") or os.urandom(32).hex()

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
    return render_template_string(LOGIN_HTML, error=error)


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
    return render_template_string(
        INDEX_HTML,
        default_privacy=DEFAULT_PRIVACY,
        privacy_choices=PRIVACY_CHOICES,
        require_auth=bool(ACCESS_TOKEN),
    )


@app.route("/submit", methods=["POST"])
@login_required
def submit():
    mode = (request.form.get("mode") or "long").strip().lower()
    privacy = (request.form.get("privacy") or DEFAULT_PRIVACY).strip().lower()
    publish = request.form.get("publish", "on") in ("on", "true", "1")
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
                "idea": describe_manual_idea(str(row.get("idea") or "")),
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


@app.route("/healthz")
def healthz():
    return "ok", 200


# --------------------------------------------------------------------------- #
# Worker: nhặt ý tưởng pending -> chạy bot subprocess (render tuần tự 1 luồng)
# --------------------------------------------------------------------------- #
def process_one(archive: Archive) -> bool:
    row = archive.claim_next_idea()
    if not row:
        return False
    idea_id = int(row["id"])
    LOG.info("Worker bắt đầu idea id=%s (mode=%s)", idea_id, row.get("mode"))
    cmd = [sys.executable, "-u", str(BOT_SCRIPT), "--idea-id", str(idea_id)]
    try:
        proc = subprocess.run(cmd, cwd=str(ROOT))
        returncode = proc.returncode
    except Exception as exc:  # không thể spawn được tiến trình
        LOG.exception("Không chạy được bot cho idea id=%s: %s", idea_id, exc)
        archive.update_idea(idea_id, "failed", error=f"spawn error: {exc}"[:2000])
        return True
    # Bot tự set done/failed. Nếu thoát lỗi mà row vẫn 'processing' -> đánh dấu failed.
    fresh = archive.get_idea(idea_id)
    if fresh and fresh.get("status") == "processing":
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


# --------------------------------------------------------------------------- #
# HTML (inline, không CDN)
# --------------------------------------------------------------------------- #
BASE_CSS = """
:root{
  --bg:#000; --panel:#07070a; --line:rgba(255,255,255,.13); --line-soft:rgba(255,255,255,.07);
  --fg:#f2f4f8; --dim:#8a8f9c; --faint:#565b68;
  --ok:#4ade80; --warn:#fbbf24; --err:#f87171; --live:#67e8f9;
  --mono:ui-monospace,SFMono-Regular,'SF Mono',Menlo,Consolas,'Liberation Mono',monospace;
  --sans:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;
  color-scheme:dark;
}
*{box-sizing:border-box}
html,body{margin:0;padding:0}
body{background:var(--bg);color:var(--fg);min-height:100vh;font-family:var(--sans);
  font-size:13px;line-height:1.5;-webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}
/* faint technical grid so the black reads as a console, not an empty page */
.grid-bg{position:fixed;inset:0;z-index:-1;pointer-events:none;
  background-image:linear-gradient(rgba(255,255,255,.028) 1px,transparent 1px),
                   linear-gradient(90deg,rgba(255,255,255,.028) 1px,transparent 1px);
  background-size:56px 56px;
  mask-image:radial-gradient(ellipse 90% 65% at 50% 0%,#000 35%,transparent 100%);
  -webkit-mask-image:radial-gradient(ellipse 90% 65% at 50% 0%,#000 35%,transparent 100%)}
a{color:inherit}
.mono{font-family:var(--mono)}
.eyebrow{font-family:var(--mono);font-size:10px;letter-spacing:.16em;text-transform:uppercase;color:var(--faint)}

/* ---- top bar ---- */
.topbar{position:sticky;top:0;z-index:20;display:flex;align-items:center;justify-content:space-between;
  gap:16px;padding:0 20px;height:52px;background:rgba(0,0,0,.86);backdrop-filter:blur(10px);
  border-bottom:1px solid var(--line)}
.brand{display:flex;align-items:center;gap:10px;min-width:0}
.mark{width:22px;height:22px;border:1px solid var(--fg);border-radius:5px;display:grid;place-items:center;
  font-family:var(--mono);font-size:11px;font-weight:700;flex:0 0 auto}
.brand-name{font-weight:650;font-size:13px;letter-spacing:.01em;white-space:nowrap}
.brand-sub{color:var(--faint);font-size:11px;font-family:var(--mono);white-space:nowrap}
.sep{width:1px;height:14px;background:var(--line)}
.topbar-right{display:flex;align-items:center;gap:14px}
.status{display:flex;align-items:center;gap:7px;font-family:var(--mono);font-size:11px;color:var(--dim)}
.dot{width:6px;height:6px;border-radius:50%;background:var(--ok);box-shadow:0 0 8px var(--ok)}
.dot.busy{background:var(--live);box-shadow:0 0 8px var(--live);animation:pulse 1.3s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.25}}
.ghost{font-family:var(--mono);font-size:11px;color:var(--dim);text-decoration:none;
  border:1px solid var(--line);padding:5px 10px;border-radius:6px;transition:.15s}
.ghost:hover{color:var(--fg);border-color:rgba(255,255,255,.35)}

/* ---- page shell: full browser width ---- */
.shell{width:100%;padding:18px 20px 40px}
/* equal-height decks so both CTAs land on the same baseline */
.decks{display:grid;grid-template-columns:1fr 1fr;gap:16px;align-items:stretch}

/* ---- panel ---- */
.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;overflow:hidden;
  display:flex;flex-direction:column}
.decks .panel-bd{flex:1;display:flex}
.decks form{flex:1;display:flex;flex-direction:column}
.decks .cta{margin-top:auto}
.decks .field textarea{flex:1}
.panel-hd{display:flex;align-items:center;justify-content:space-between;gap:12px;
  padding:11px 16px;border-bottom:1px solid var(--line);background:rgba(255,255,255,.022)}
.panel-id{display:flex;align-items:center;gap:9px;min-width:0}
.panel-ico{width:24px;height:24px;border:1px solid var(--line);border-radius:6px;display:grid;
  place-items:center;font-size:12px;flex:0 0 auto}
.panel-name{font-size:13px;font-weight:650;letter-spacing:.01em}
.panel-note{font-family:var(--mono);font-size:10.5px;color:var(--faint);margin-top:1px}
.tag{font-family:var(--mono);font-size:9.5px;letter-spacing:.12em;text-transform:uppercase;
  color:var(--dim);border:1px solid var(--line);border-radius:99px;padding:3px 8px;white-space:nowrap}
.panel-bd{padding:16px}

/* ---- form controls ---- */
.lbl{display:block;font-family:var(--mono);font-size:10px;letter-spacing:.14em;text-transform:uppercase;
  color:var(--faint);margin:0 0 6px}
.field+.field{margin-top:14px}
input[type=text],textarea,select{width:100%;background:#000;border:1px solid var(--line);color:var(--fg);
  border-radius:7px;padding:9px 11px;font-size:13px;font-family:inherit;transition:.14s}
input[type=text]::placeholder,textarea::placeholder{color:var(--faint)}
input[type=text]:focus,textarea:focus,select:focus{outline:none;border-color:rgba(255,255,255,.45);
  box-shadow:0 0 0 3px rgba(255,255,255,.06)}
textarea{min-height:132px;resize:vertical;line-height:1.55}
select{appearance:none;-webkit-appearance:none;cursor:pointer;text-transform:capitalize;padding-right:30px;height:36px}
.sel{position:relative}
.sel::after{content:"";position:absolute;right:12px;top:50%;width:5px;height:5px;pointer-events:none;
  border-right:1.5px solid var(--dim);border-bottom:1.5px solid var(--dim);transform:translateY(-70%) rotate(45deg)}

/* A-vs-B row mirrors the video layout: left subject | VS | right subject */
.vs{display:grid;grid-template-columns:1fr auto 1fr;gap:10px;align-items:end}
.vs-badge{font-family:var(--mono);font-size:10px;font-weight:700;color:var(--faint);
  border:1px solid var(--line);border-radius:99px;padding:8px 7px;line-height:1;margin-bottom:7px}
.side{font-family:var(--mono);font-size:9px;color:var(--faint);letter-spacing:.1em}

.row2{display:grid;grid-template-columns:1fr auto;gap:10px;align-items:end;margin-top:14px}
.pub{display:flex;align-items:center;gap:9px;height:36px;padding:0 11px;border:1px solid var(--line);
  border-radius:7px;background:#000;cursor:pointer;white-space:nowrap;font-size:12px;color:var(--dim)}
.switch{position:relative;display:inline-block;width:32px;height:18px;flex:0 0 auto}
.switch input{opacity:0;width:0;height:0}
.track{position:absolute;inset:0;background:#22242b;border-radius:99px;transition:.18s}
.track::before{content:"";position:absolute;left:2px;top:2px;width:14px;height:14px;background:#8a8f9c;
  border-radius:50%;transition:.18s}
.switch input:checked+.track{background:#fff}
.switch input:checked+.track::before{transform:translateX(14px);background:#000}
.switch input:checked~*{color:var(--fg)}

.cta{margin-top:16px;width:100%;border:1px solid var(--fg);border-radius:7px;padding:10px;cursor:pointer;
  background:var(--fg);color:#000;font-size:12.5px;font-weight:700;font-family:var(--mono);
  letter-spacing:.06em;text-transform:uppercase;transition:.14s}
.cta:hover{background:#fff;box-shadow:0 0 0 3px rgba(255,255,255,.1)}
.cta:disabled{opacity:.45;cursor:default;box-shadow:none}
.hint{font-family:var(--mono);font-size:10.5px;color:var(--faint);margin:10px 0 0;line-height:1.5}

/* ---- queue table ---- */
.queue{margin-top:16px;background:var(--panel);border:1px solid var(--line);border-radius:10px;overflow:hidden}
.stats{display:flex;gap:6px;flex-wrap:wrap}
.chip{font-family:var(--mono);font-size:10px;letter-spacing:.06em;padding:3px 8px;border-radius:99px;
  border:1px solid var(--line);color:var(--dim)}
.chip.c-processing{color:var(--live);border-color:rgba(103,232,249,.35)}
.chip.c-done{color:var(--ok);border-color:rgba(74,222,128,.3)}
.chip.c-failed{color:var(--err);border-color:rgba(248,113,113,.35)}
.tbl-scroll{overflow-x:auto}
table{width:100%;border-collapse:collapse}
th{font-family:var(--mono);font-size:9.5px;letter-spacing:.14em;text-transform:uppercase;color:var(--faint);
  text-align:left;font-weight:500;padding:9px 16px;border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:10px 16px;border-bottom:1px solid var(--line-soft);vertical-align:middle;font-size:12.5px}
tr:last-child td{border-bottom:0}
tbody tr:hover{background:rgba(255,255,255,.022)}
.c-id{font-family:var(--mono);font-size:11px;color:var(--faint);width:52px}
.c-type{width:96px}
.c-status{width:132px}
.c-res{width:150px}
.c-time{width:104px;font-family:var(--mono);font-size:11px;color:var(--faint);white-space:nowrap}
.type{display:inline-flex;align-items:center;gap:6px;font-family:var(--mono);font-size:10px;
  letter-spacing:.08em;text-transform:uppercase;color:var(--dim)}
.idea{color:var(--fg);word-break:break-word;line-height:1.45}
.jt{color:var(--faint);font-size:11px;margin-top:2px}
.pill{display:inline-flex;align-items:center;gap:6px;font-family:var(--mono);font-size:10px;
  letter-spacing:.06em;text-transform:uppercase;padding:3px 9px;border-radius:99px;
  border:1px solid var(--line);color:var(--dim);white-space:nowrap}
.p-processing{color:var(--live);border-color:rgba(103,232,249,.35)}
.p-done{color:var(--ok);border-color:rgba(74,222,128,.3)}
.p-failed{color:var(--err);border-color:rgba(248,113,113,.35)}
.livedot{width:5px;height:5px;border-radius:50%;background:currentColor;animation:pulse 1.3s infinite}
.link{font-family:var(--mono);font-size:11px;text-decoration:none;color:var(--fg);
  border:1px solid var(--line);border-radius:6px;padding:4px 9px;transition:.14s;white-space:nowrap}
.link:hover{border-color:var(--fg);background:rgba(255,255,255,.06)}
.muted{color:var(--faint);font-family:var(--mono);font-size:11px}
.err{color:var(--err);font-size:11px;font-family:var(--mono);display:inline-block;max-width:220px;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.empty{text-align:center;color:var(--faint);padding:40px 16px;font-family:var(--mono);font-size:11.5px}
.skeleton{height:38px;margin:1px 0;background:linear-gradient(90deg,rgba(255,255,255,.02),
  rgba(255,255,255,.055),rgba(255,255,255,.02));background-size:200% 100%;animation:sh 1.3s infinite}
@keyframes sh{0%{background-position:200% 0}100%{background-position:-200% 0}}
.ft{font-family:var(--mono);font-size:10.5px;color:var(--faint);margin-top:14px;
  display:flex;gap:14px;flex-wrap:wrap}

@media(max-width:1080px){ .decks{grid-template-columns:1fr} }
@media(max-width:640px){
  .shell{padding:14px 12px 32px}
  .brand-sub,.sep{display:none}
  .c-time,.c-id,th.c-time,th.c-id{display:none}
  /* keep the remaining columns readable: scroll the table instead of crushing it */
  table{min-width:600px}
  .panel-hd{flex-wrap:wrap}
}
"""

LOGIN_HTML = """<!doctype html><html lang=vi><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>Auth · Idea Studio</title>
<style>""" + BASE_CSS + """
.login-wrap{max-width:340px;margin:0 auto;min-height:100vh;display:flex;flex-direction:column;
  justify-content:center;padding:24px}
.pw{width:100%;background:#000;border:1px solid var(--line);color:var(--fg);
  border-radius:7px;padding:9px 11px;font-size:13px;font-family:var(--mono)}
.pw:focus{outline:none;border-color:rgba(255,255,255,.45);box-shadow:0 0 0 3px rgba(255,255,255,.06)}
</style></head><body><div class="grid-bg"></div>
<div class="login-wrap">
  <div class="brand" style="margin-bottom:16px">
    <span class="mark">IS</span>
    <div><div class="brand-name">Idea Studio</div><div class="brand-sub">restricted access</div></div>
  </div>
  <div class="panel"><div class="panel-bd">
    <form method=post>
      <div class="field">
        <label class="lbl" for=token>Mật khẩu</label>
        <input class="pw" type=password id=token name=token autofocus autocomplete=current-password>
      </div>
      {% if error %}<p style="color:var(--err);font-family:var(--mono);font-size:11px;margin:9px 0 0">{{ error }}</p>{% endif %}
      <button class="cta" type=submit>Đăng nhập</button>
    </form>
  </div></div>
</div></body></html>"""

_PRIVACY_BLOCK = """
      <div class="row2">
        <div>
          <label class="lbl" for="{{id}}-privacy">Chế độ đăng</label>
          <div class="sel"><select id="{{id}}-privacy" name="privacy">
            {% for p in privacy_choices %}<option value="{{ p }}" {% if p == default_privacy %}selected{% endif %}>{{ p }}</option>{% endfor %}
          </select></div>
        </div>
        <div>
          <label class="lbl">Tự đăng</label>
          <label class="pub"><span class="switch"><input type="checkbox" name="publish" checked><span class="track"></span></span><span>YouTube</span></label>
        </div>
      </div>
"""

INDEX_HTML = """<!doctype html><html lang=vi><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>Idea Studio · YouTube bot</title>
<style>""" + BASE_CSS + """</style></head><body><div class="grid-bg"></div>

<header class="topbar">
  <div class="brand">
    <span class="mark">IS</span>
    <div style="min-width:0">
      <div class="brand-name">Idea Studio</div>
    </div>
    <span class="sep"></span>
    <span class="brand-sub">manual pipeline · vi → en</span>
  </div>
  <div class="topbar-right">
    <span class="status"><span class="dot" id="hdDot"></span><span id="hdStat">idle</span></span>
    {% if require_auth %}<a class="ghost" href="{{ url_for('logout') }}">Logout</a>{% endif %}
  </div>
</header>

<main class="shell">
  <div class="decks">

    <!-- ============ SHORT: A vs B comparison ============ -->
    <section class="panel">
      <div class="panel-hd">
        <div class="panel-id">
          <span class="panel-ico">📱</span>
          <div>
            <div class="panel-name">Short</div>
            <div class="panel-note">dọc 9:16 · so sánh 2 đối tượng</div>
          </div>
        </div>
        <span class="tag">A vs B</span>
      </div>
      <div class="panel-bd">
        <form id="shortForm">
          <input type="hidden" name="mode" value="short">
          <div class="field">
            <label class="lbl">Hai đối tượng cần so sánh</label>
            <div class="vs">
              <div>
                <input type="text" id="subjA" name="subject_a" maxlength="60" required
                       placeholder="Coca-Cola" autocomplete="off">
                <div class="side" style="margin-top:5px">◀ TRÁI</div>
              </div>
              <div class="vs-badge">VS</div>
              <div>
                <input type="text" id="subjB" name="subject_b" maxlength="60" required
                       placeholder="Pepsi" autocomplete="off">
                <div class="side" style="margin-top:5px;text-align:right">PHẢI ▶</div>
              </div>
            </div>
          </div>
          <div class="field">
            <label class="lbl" for="angle">Góc so sánh <span style="text-transform:none;letter-spacing:0">(tuỳ chọn)</span></label>
            <input type="text" id="angle" name="angle" maxlength="300" autocomplete="off"
                   placeholder="VD: hương vị, marketing, thị phần…">
          </div>
""" + _PRIVACY_BLOCK.replace("{{id}}", "s") + """
          <button class="cta" id="shortBtn" type="submit">Tạo Short</button>
          <p class="hint">Ảnh 2 đối tượng lấy tự động từ web · người que chỉ tay sang bên đang nói.</p>
        </form>
      </div>
    </section>

    <!-- ============ LONG-FORM: explainer ============ -->
    <section class="panel">
      <div class="panel-hd">
        <div class="panel-id">
          <span class="panel-ico">🎬</span>
          <div>
            <div class="panel-name">Long-form</div>
            <div class="panel-note">ngang 16:9 · 5–7 phút · giải thích</div>
          </div>
        </div>
        <span class="tag">Explainer</span>
      </div>
      <div class="panel-bd">
        <form id="longForm">
          <input type="hidden" name="mode" value="long">
          <div class="field">
            <label class="lbl" for="idea">Ý tưởng của bạn</label>
            <textarea id="idea" name="idea" required
              placeholder="VD: Người tiền sử đã sinh tồn như thế nào&#10;&#10;Gõ tiếng Việt hay tiếng Anh đều được — video xuất ra tiếng Anh."></textarea>
          </div>
""" + _PRIVACY_BLOCK.replace("{{id}}", "l") + """
          <button class="cta" id="longBtn" type="submit">Tạo Long-form</button>
          <p class="hint">Hình hoạt hình người que · cảnh 1 dùng làm thumbnail.</p>
        </form>
      </div>
    </section>

  </div>

  <!-- ============ QUEUE ============ -->
  <section class="queue">
    <div class="panel-hd">
      <div class="panel-id">
        <span class="panel-ico">▤</span>
        <div>
          <div class="panel-name">Hàng đợi &amp; lịch sử</div>
          <div class="panel-note">render tuần tự · tự làm mới mỗi 5s</div>
        </div>
      </div>
      <div class="stats" id="stats"></div>
    </div>
    <div class="tbl-scroll">
      <table>
        <thead><tr>
          <th class="c-id">#</th><th class="c-type">Loại</th><th>Nội dung</th>
          <th class="c-status">Trạng thái</th><th class="c-res">Kết quả</th><th class="c-time">Lúc</th>
        </tr></thead>
        <tbody id="jobs">
          <tr><td colspan="6" style="padding:0"><div class="skeleton"></div><div class="skeleton"></div><div class="skeleton"></div></td></tr>
        </tbody>
      </table>
    </div>
  </section>

  <footer class="ft">
    <span>▲ mỗi lần render một video</span>
    <span>▲ độ dài tự khớp theo giọng đọc</span>
  </footer>
</main>

<script>
const $ = (s, r=document) => r.querySelector(s);
const BADGE = {pending:['pending','Chờ'], processing:['processing','Đang tạo'], done:['done','Xong'], failed:['failed','Lỗi']};
function esc(s){ return (s==null?'':String(s)).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
function fmt(ts){ if(!ts) return ''; const d = new Date(ts); return isNaN(d) ? ts : d.toLocaleString('vi-VN',{hour:'2-digit',minute:'2-digit',day:'2-digit',month:'2-digit'}); }

function renderJobs(jobs){
  const box = $('#jobs'), stats = $('#stats');
  const c = {pending:0, processing:0, done:0, failed:0};
  jobs.forEach(j => { c[j.status] = (c[j.status]||0) + 1; });
  stats.innerHTML =
    (c.processing ? '<span class="chip c-processing">'+c.processing+' đang tạo</span>' : '') +
    (c.pending ? '<span class="chip">'+c.pending+' chờ</span>' : '') +
    (c.done ? '<span class="chip c-done">'+c.done+' xong</span>' : '') +
    (c.failed ? '<span class="chip c-failed">'+c.failed+' lỗi</span>' : '');
  const busy = c.processing > 0;
  $('#hdDot').className = 'dot' + (busy ? ' busy' : '');
  $('#hdStat').textContent = busy ? 'rendering' : (c.pending ? 'queued' : 'idle');

  if(!jobs.length){
    box.innerHTML = '<tr><td colspan="6"><div class="empty">chưa có job nào — nhập ý tưởng để bắt đầu</div></td></tr>';
    return;
  }
  box.innerHTML = jobs.map(j => {
    const b = BADGE[j.status] || ['pending', j.status];
    const isLong = j.mode === 'long';
    const type = '<span class="type">'+(isLong?'🎬':'📱')+' '+(isLong?'Long':'Short')+'</span>';
    let res = '<span class="muted">—</span>';
    if(j.status === 'done' && j.youtube_id) res = '<a class="link" href="https://youtube.com/watch?v='+esc(j.youtube_id)+'" target="_blank" rel="noopener">▶ YouTube</a>';
    else if(j.status === 'done') res = '<span class="muted">đã render</span>';
    else if(j.status === 'failed') res = '<span class="err" title="'+esc(j.error)+'">'+esc(j.error||'lỗi')+'</span>';
    else if(j.status === 'processing') res = '<span class="muted">đang xử lý…</span>';
    const title = j.output_title ? '<div class="jt">'+esc(j.output_title)+'</div>' : '';
    const dot = j.status === 'processing' ? '<span class="livedot"></span>' : '';
    return '<tr>'
      + '<td class="c-id">'+esc(j.id)+'</td>'
      + '<td class="c-type">'+type+'</td>'
      + '<td><div class="idea">'+esc((j.idea||'').slice(0,160))+'</div>'+title+'</td>'
      + '<td class="c-status"><span class="pill p-'+b[0]+'">'+dot+esc(b[1])+'</span></td>'
      + '<td class="c-res">'+res+'</td>'
      + '<td class="c-time">'+fmt(j.created_at)+'</td>'
      + '</tr>';
  }).join('');
}

async function poll(){ try{ const r = await fetch('/api/jobs',{cache:'no-store'}); if(r.ok) renderJobs(await r.json()); }catch(e){} }

function wire(formSel, btnSel, clearSel){
  const form = $(formSel), btn = $(btnSel);
  form.addEventListener('submit', async e => {
    e.preventDefault();
    if(!form.checkValidity()){ form.reportValidity(); return; }
    btn.disabled = true; const old = btn.textContent; btn.textContent = 'Đang gửi…';
    try{
      const r = await fetch('/submit', {method:'POST', body:new FormData(form)});
      const j = await r.json().catch(() => ({}));
      if(r.ok && j.ok){ clearSel.forEach(s => { const el = $(s); if(el) el.value = ''; }); btn.textContent = '✓ Đã vào hàng đợi'; poll(); }
      else { btn.textContent = (j && j.error) ? j.error : 'Lỗi, thử lại'; }
    }catch(e){ btn.textContent = 'Lỗi mạng'; }
    setTimeout(() => { btn.disabled = false; btn.textContent = old; }, 1800);
  });
}
wire('#shortForm', '#shortBtn', ['#subjA', '#subjB', '#angle']);
wire('#longForm', '#longBtn', ['#idea']);

poll(); setInterval(poll, 5000);
</script>
</body></html>"""


if __name__ == "__main__":
    if not ACCESS_TOKEN:
        LOG.warning("WEB_ACCESS_TOKEN chưa được đặt — trang web đang MỞ cho mọi người. Chỉ nên vậy khi chạy local.")
    start_worker()
    port = int(os.environ.get("PORT", "8080"))
    LOG.info("Idea Studio chạy tại http://0.0.0.0:%d", port)
    app.run(host="0.0.0.0", port=port, threaded=True)
