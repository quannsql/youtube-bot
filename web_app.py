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
    MAX_SHORT_DURATION_SECONDS,
    MIN_SHORT_DURATION_SECONDS,
    ROOT,
    Archive,
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
        min_dur=MIN_SHORT_DURATION_SECONDS,
        max_dur=MAX_SHORT_DURATION_SECONDS,
        default_privacy=DEFAULT_PRIVACY,
        privacy_choices=PRIVACY_CHOICES,
        require_auth=bool(ACCESS_TOKEN),
    )


@app.route("/submit", methods=["POST"])
@login_required
def submit():
    idea = (request.form.get("idea") or "").strip()
    mode = (request.form.get("mode") or "short").strip().lower()
    privacy = (request.form.get("privacy") or DEFAULT_PRIVACY).strip().lower()
    publish = request.form.get("publish", "on") == "on"

    if not idea:
        return redirect(url_for("index"))
    if mode not in ("short", "long"):
        mode = "short"
    if privacy not in PRIVACY_CHOICES:
        privacy = DEFAULT_PRIVACY

    duration = None
    if mode == "short":
        try:
            duration = int(request.form.get("duration") or MAX_SHORT_DURATION_SECONDS)
        except ValueError:
            duration = MAX_SHORT_DURATION_SECONDS
        duration = max(MIN_SHORT_DURATION_SECONDS, min(MAX_SHORT_DURATION_SECONDS, duration))

    idea_id = get_archive().enqueue_idea(mode, idea, duration, publish, privacy)
    LOG.info("Enqueued manual idea id=%s mode=%s publish=%s", idea_id, mode, publish)
    return redirect(url_for("index"))


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
                "idea": row.get("idea"),
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
:root{color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;background:#0e1117;color:#e6edf3;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;line-height:1.5}
.wrap{max-width:820px;margin:0 auto;padding:24px 16px 64px}
h1{font-size:22px;margin:8px 0 4px}
.sub{color:#8b949e;font-size:14px;margin:0 0 24px}
.card{background:#161b22;border:1px solid #30363d;border-radius:14px;padding:20px;margin-bottom:20px}
label{display:block;font-weight:600;font-size:14px;margin:14px 0 6px}
textarea,input[type=number],select{width:100%;background:#0d1117;border:1px solid #30363d;color:#e6edf3;border-radius:10px;padding:12px;font-size:15px;font-family:inherit}
textarea{min-height:120px;resize:vertical}
.row{display:flex;gap:12px;flex-wrap:wrap}
.row>.col{flex:1;min-width:150px}
.modes{display:flex;gap:10px;margin-top:6px}
.mode-opt{flex:1;border:1px solid #30363d;border-radius:10px;padding:12px;cursor:pointer;text-align:center;background:#0d1117;transition:.15s}
.mode-opt.active{border-color:#2f81f7;background:#132033}
.mode-opt small{display:block;color:#8b949e;font-weight:400;margin-top:2px}
.mode-opt input{display:none}
.check{display:flex;align-items:center;gap:10px;margin-top:16px;font-size:14px}
.check input{width:18px;height:18px}
button.primary{margin-top:20px;width:100%;background:#238636;border:0;color:#fff;font-size:16px;font-weight:600;padding:14px;border-radius:10px;cursor:pointer}
button.primary:hover{background:#2ea043}
table{width:100%;border-collapse:collapse;font-size:13.5px}
th,td{text-align:left;padding:10px 8px;border-bottom:1px solid #21262d;vertical-align:top}
th{color:#8b949e;font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.03em}
.badge{display:inline-block;padding:2px 9px;border-radius:999px;font-size:12px;font-weight:600;white-space:nowrap}
.b-pending{background:#30363d;color:#c9d1d9}
.b-processing{background:#1f3a5f;color:#79c0ff}
.b-done{background:#1a3a24;color:#56d364}
.b-failed{background:#4d1f22;color:#ff7b72}
.tag{display:inline-block;background:#21262d;color:#adbac7;border-radius:6px;padding:1px 7px;font-size:11.5px;margin-right:4px}
a{color:#58a6ff}
.muted{color:#8b949e}
.topbar{display:flex;justify-content:space-between;align-items:center}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:#3fb950;margin-right:6px;animation:pulse 1.6s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.35}}
.empty{color:#8b949e;text-align:center;padding:24px}
"""

LOGIN_HTML = """<!doctype html><html lang=vi><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>Đăng nhập · Idea Studio</title>
<style>""" + BASE_CSS + """</style></head><body><div class=wrap style="max-width:400px;padding-top:80px">
<h1>🔒 Idea Studio</h1><p class=sub>Nhập mật khẩu để tiếp tục.</p>
<div class=card><form method=post>
<label for=token>Mật khẩu</label>
<input type=password id=token name=token autofocus autocomplete=current-password
 style="width:100%;background:#0d1117;border:1px solid #30363d;color:#e6edf3;border-radius:10px;padding:12px;font-size:15px">
{% if error %}<p style="color:#ff7b72;font-size:13px;margin:10px 0 0">{{ error }}</p>{% endif %}
<button class=primary type=submit>Đăng nhập</button>
</form></div></div></body></html>"""

INDEX_HTML = """<!doctype html><html lang=vi><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>Idea Studio · YouTube bot</title>
<style>""" + BASE_CSS + """</style></head><body><div class=wrap>
<div class=topbar>
  <div><h1>💡 Idea Studio</h1><p class=sub>Nhập ý tưởng của bạn — bot sẽ tự viết kịch bản, tạo ảnh, lồng tiếng, render và đăng YouTube.</p></div>
  {% if require_auth %}<a href="{{ url_for('logout') }}" class=muted style="font-size:13px">Đăng xuất</a>{% endif %}
</div>

<div class=card>
<form method=post action="{{ url_for('submit') }}">
  <label for=idea>Ý tưởng của bạn</label>
  <textarea id=idea name=idea required placeholder="VD: Câu chuyện về việc xây dựng kênh đào Suez và vì sao nó thay đổi thương mại thế giới. (Gõ tiếng Việt hay tiếng Anh đều được — video xuất ra tiếng Anh.)"></textarea>

  <label>Loại video</label>
  <div class=modes>
    <label class="mode-opt active" id=opt-short>
      <input type=radio name=mode value=short checked>📱 Short (dọc)<small>video ngắn 45–60s</small>
    </label>
    <label class="mode-opt" id=opt-long>
      <input type=radio name=mode value=long>🎬 Long-form (ngang)<small>video dài 5–7 phút</small>
    </label>
  </div>

  <div class=row>
    <div class=col id=dur-wrap>
      <label for=duration>Thời lượng Short (giây)</label>
      <input type=number id=duration name=duration min="{{ min_dur }}" max="{{ max_dur }}" value="{{ max_dur }}">
    </div>
    <div class=col>
      <label for=privacy>Chế độ đăng</label>
      <select id=privacy name=privacy>
        {% for p in privacy_choices %}
        <option value="{{ p }}" {% if p == default_privacy %}selected{% endif %}>{{ p }}</option>
        {% endfor %}
      </select>
    </div>
  </div>

  <label class=check><input type=checkbox name=publish checked> Đăng thẳng lên YouTube sau khi render</label>
  <button class=primary type=submit>🚀 Tạo video</button>
</form>
</div>

<div class=card>
  <div class=topbar><h1 style="font-size:17px;margin:0"><span class=dot></span>Hàng đợi &amp; lịch sử</h1>
  <span class=muted style="font-size:12px">tự làm mới mỗi 5s</span></div>
  <div id=jobs><p class=empty>Đang tải…</p></div>
</div>
</div>

<script>
// Chuyển đổi giao diện chọn mode + ẩn/hiện ô thời lượng Short.
const optShort=document.getElementById('opt-short'), optLong=document.getElementById('opt-long'), durWrap=document.getElementById('dur-wrap');
function syncMode(){
  const isShort=document.querySelector('input[name=mode]:checked').value==='short';
  optShort.classList.toggle('active',isShort); optLong.classList.toggle('active',!isShort);
  durWrap.style.display=isShort?'block':'none';
}
document.querySelectorAll('input[name=mode]').forEach(el=>el.addEventListener('change',syncMode)); syncMode();

const BADGE={pending:['b-pending','Chờ'],processing:['b-processing','Đang tạo…'],done:['b-done','Xong'],failed:['b-failed','Lỗi']};
function esc(s){return (s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
function fmt(ts){ if(!ts) return ''; const d=new Date(ts); return isNaN(d)?ts:d.toLocaleString('vi-VN',{hour:'2-digit',minute:'2-digit',day:'2-digit',month:'2-digit'}); }
function render(jobs){
  const box=document.getElementById('jobs');
  if(!jobs.length){box.innerHTML='<p class=empty>Chưa có ý tưởng nào. Nhập ý tưởng đầu tiên ở trên nhé!</p>';return;}
  let h='<table><thead><tr><th>Thời gian</th><th>Loại</th><th>Ý tưởng</th><th>Trạng thái</th><th>Kết quả</th></tr></thead><tbody>';
  for(const j of jobs){
    const [cls,txt]=BADGE[j.status]||['b-pending',j.status];
    const modeTag=j.mode==='long'?'🎬 Long':'📱 Short';
    let result='';
    if(j.status==='done'&&j.youtube_id){result='<a href="https://youtube.com/watch?v='+esc(j.youtube_id)+'" target=_blank>▶ Xem trên YouTube</a>';}
    else if(j.status==='done'){result='<span class=muted>đã render</span>';}
    else if(j.status==='failed'){result='<span style="color:#ff7b72" title="'+esc(j.error)+'">'+esc((j.error||'lỗi').slice(0,60))+'</span>';}
    else if(j.status==='processing'){result='<span class=muted>đang xử lý…</span>';}
    const title=j.output_title?'<div class=muted style="margin-top:3px;font-size:12px">'+esc(j.output_title)+'</div>':'';
    h+='<tr><td class=muted style="white-space:nowrap">'+fmt(j.created_at)+'</td><td><span class=tag>'+modeTag+'</span></td>'+
       '<td>'+esc((j.idea||'').slice(0,110))+title+'</td>'+
       '<td><span class="badge '+cls+'">'+esc(txt)+'</span></td><td>'+result+'</td></tr>';
  }
  box.innerHTML=h+'</tbody></table>';
}
async function poll(){ try{const r=await fetch('/api/jobs',{cache:'no-store'}); if(r.ok) render(await r.json());}catch(e){} }
poll(); setInterval(poll,5000);
</script>
</body></html>"""


if __name__ == "__main__":
    if not ACCESS_TOKEN:
        LOG.warning("WEB_ACCESS_TOKEN chưa được đặt — trang web đang MỞ cho mọi người. Chỉ nên vậy khi chạy local.")
    start_worker()
    port = int(os.environ.get("PORT", "8080"))
    LOG.info("Idea Studio chạy tại http://0.0.0.0:%d", port)
    app.run(host="0.0.0.0", port=port, threaded=True)
