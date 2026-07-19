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

from youtube_shorts_bot import ROOT, Archive

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
    idea = (request.form.get("idea") or "").strip()
    mode = (request.form.get("mode") or "short").strip().lower()
    privacy = (request.form.get("privacy") or DEFAULT_PRIVACY).strip().lower()
    publish = request.form.get("publish", "on") in ("on", "true", "1")

    if not idea:
        return jsonify({"ok": False, "error": "Ý tưởng đang trống."}), 400
    if mode not in ("short", "long"):
        mode = "short"
    if privacy not in PRIVACY_CHOICES:
        privacy = DEFAULT_PRIVACY

    # Video luôn khớp theo độ dài giọng đọc, nên không thu "thời lượng" từ form.
    # Short dùng SHORT_DURATION_SECONDS mặc định cho ngân sách từ (xử lý ở bot).
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
:root{
  --bg:#0a0c12; --surface:rgba(255,255,255,.035); --border:rgba(255,255,255,.09);
  --border2:rgba(255,255,255,.06); --text:#e9edf5; --muted:#8b93a7; --faint:#5b6377;
  --accent:#7c6cff; --grad:linear-gradient(135deg,#8a6bff,#4d8dff);
  --green:#34d399; --red:#f97066; --blue:#60a5fa; --slate:#94a3b8;
  --r:16px; --r-sm:11px; color-scheme:dark;
}
*{box-sizing:border-box}
html,body{margin:0}
body{background:var(--bg);color:var(--text);min-height:100vh;line-height:1.55;
  font-family:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;
  -webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}
.bg{position:fixed;inset:0;z-index:-1;background:
  radial-gradient(1100px 520px at 12% -12%,rgba(124,108,255,.22),transparent 60%),
  radial-gradient(900px 520px at 105% 0%,rgba(77,141,255,.15),transparent 55%),var(--bg)}
a{color:inherit}
.wrap{max-width:760px;margin:0 auto;padding:30px 18px 72px}
.hd{display:flex;justify-content:space-between;align-items:center;margin-bottom:26px}
.brand{display:flex;gap:13px;align-items:center}
.logo{width:44px;height:44px;border-radius:13px;display:grid;place-items:center;font-size:22px;
  background:var(--grad);box-shadow:0 10px 26px -8px rgba(124,108,255,.75)}
.brand-name{font-weight:750;font-size:19px;letter-spacing:-.01em}
.brand-sub{color:var(--muted);font-size:13px}
.ghost{color:var(--muted);text-decoration:none;font-size:13px;border:1px solid var(--border);
  padding:8px 14px;border-radius:10px;transition:.15s}
.ghost:hover{color:var(--text);border-color:var(--accent)}
.card{background:var(--surface);border:1px solid var(--border);border-radius:var(--r);
  padding:22px;margin-bottom:18px;backdrop-filter:blur(12px);
  box-shadow:0 1px 0 rgba(255,255,255,.04) inset,0 22px 44px -26px rgba(0,0,0,.75)}
.card-title{font-size:15px;font-weight:700;margin:0 0 16px;letter-spacing:-.01em}
.lbl{display:block;font-weight:600;font-size:13px;color:var(--muted);margin:18px 0 8px}
form .lbl:first-child{margin-top:0}
textarea{width:100%;background:rgba(0,0,0,.26);border:1px solid var(--border);color:var(--text);
  border-radius:var(--r-sm);padding:14px;font-size:15px;font-family:inherit;min-height:120px;resize:vertical;transition:.15s}
textarea::placeholder{color:var(--faint)}
textarea:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px rgba(124,108,255,.18)}
.seg{display:grid;grid-template-columns:1fr 1fr;gap:11px}
.seg-opt{position:relative;display:flex;flex-direction:column;gap:2px;padding:14px 16px;cursor:pointer;
  border:1px solid var(--border);border-radius:var(--r-sm);background:rgba(0,0,0,.2);transition:.16s}
.seg-opt:hover{border-color:rgba(255,255,255,.18)}
.seg-opt input{position:absolute;opacity:0;pointer-events:none}
.seg-ico{font-size:20px}
.seg-tt{font-weight:650;font-size:14.5px}
.seg-sb{color:var(--muted);font-size:12px}
.seg-opt.is-active{border-color:transparent;
  background:linear-gradient(rgba(10,12,18,.62),rgba(10,12,18,.62)) padding-box,var(--grad) border-box;
  box-shadow:0 0 0 1px rgba(124,108,255,.35),0 12px 28px -16px rgba(124,108,255,.9)}
.seg-opt.is-active .seg-tt{color:#fff}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:14px;align-items:end}
.sel{position:relative}
.sel::after{content:"";position:absolute;right:15px;top:50%;width:7px;height:7px;pointer-events:none;
  border-right:2px solid var(--muted);border-bottom:2px solid var(--muted);transform:translateY(-70%) rotate(45deg)}
select{width:100%;appearance:none;-webkit-appearance:none;background:rgba(0,0,0,.26);border:1px solid var(--border);
  color:var(--text);border-radius:var(--r-sm);padding:0 34px 0 14px;height:48px;font-size:14.5px;
  font-family:inherit;cursor:pointer;text-transform:capitalize}
select:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px rgba(124,108,255,.18)}
.pub{display:flex;align-items:center;justify-content:space-between;height:48px;padding:0 15px;
  border:1px solid var(--border);border-radius:var(--r-sm);background:rgba(0,0,0,.26);font-size:14px;color:var(--text);cursor:pointer}
.switch{position:relative;display:inline-block;width:46px;height:27px;flex:0 0 auto}
.switch input{opacity:0;width:0;height:0}
.track{position:absolute;inset:0;background:#2a3140;border-radius:99px;transition:.2s}
.track::before{content:"";position:absolute;left:3px;top:3px;width:21px;height:21px;background:#fff;border-radius:50%;transition:.2s}
.switch input:checked+.track{background:var(--grad)}
.switch input:checked+.track::before{transform:translateX(19px)}
.cta{margin-top:22px;width:100%;border:0;border-radius:var(--r-sm);padding:15px;cursor:pointer;color:#fff;
  font-size:15.5px;font-weight:700;font-family:inherit;background:var(--grad);
  box-shadow:0 14px 30px -14px rgba(124,108,255,.9);transition:.16s}
.cta:hover{transform:translateY(-1px);box-shadow:0 18px 36px -14px rgba(124,108,255,1)}
.cta:active{transform:translateY(0)}
.cta:disabled{opacity:.72;cursor:default;transform:none}
.hint{color:var(--faint);font-size:12.5px;margin:13px 2px 0;text-align:center}
.jobs-head{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:12px;flex-wrap:wrap}
.jobs-head .card-title{margin:0}
.stats{display:flex;gap:6px;flex-wrap:wrap}
.chip{font-size:11.5px;font-weight:600;padding:3px 10px;border-radius:99px;border:1px solid var(--border)}
.chip.c-processing{color:var(--blue)}
.chip.c-pending{color:var(--slate)}
.chip.c-done{color:var(--green)}
.chip.c-failed{color:var(--red)}
.job{display:flex;gap:13px;align-items:flex-start;padding:15px 4px;border-top:1px solid var(--border2)}
.job:first-child{border-top:0}
.job-ico{width:38px;height:38px;border-radius:11px;display:grid;place-items:center;font-size:18px;
  background:rgba(255,255,255,.05);flex:0 0 auto}
.job-main{flex:1;min-width:0}
.job-idea{font-size:14px;line-height:1.45;word-break:break-word}
.jt{color:var(--muted);font-size:12.5px;margin-top:3px}
.job-meta{color:var(--faint);font-size:12px;margin-top:5px}
.job-right{display:flex;flex-direction:column;align-items:flex-end;gap:7px;flex:0 0 auto}
.pill{display:inline-flex;align-items:center;gap:6px;font-size:11.5px;font-weight:650;padding:4px 11px;border-radius:99px;white-space:nowrap}
.p-pending{background:rgba(148,163,184,.14);color:var(--slate)}
.p-processing{background:rgba(96,165,250,.16);color:var(--blue)}
.p-done{background:rgba(52,211,153,.15);color:var(--green)}
.p-failed{background:rgba(249,112,102,.15);color:var(--red)}
.livedot{width:6px;height:6px;border-radius:50%;background:currentColor;animation:pulse 1.4s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
.link{color:#fff;text-decoration:none;font-size:12.5px;font-weight:650;background:rgba(255,255,255,.08);
  padding:6px 12px;border-radius:9px;transition:.15s}
.link:hover{background:var(--grad)}
.muted{color:var(--muted);font-size:12.5px}
.err{color:var(--red);font-size:12px;max-width:190px;text-align:right;display:inline-block}
.empty{text-align:center;color:var(--muted);padding:38px 12px}
.empty-ico{font-size:30px;margin-bottom:8px;opacity:.7}
.skeleton{height:60px;border-radius:12px;margin:9px 0;
  background:linear-gradient(90deg,rgba(255,255,255,.03),rgba(255,255,255,.07),rgba(255,255,255,.03));
  background-size:200% 100%;animation:sh 1.3s infinite}
@keyframes sh{0%{background-position:200% 0}100%{background-position:-200% 0}}
.ft{text-align:center;color:var(--faint);font-size:12px;margin-top:22px}
@media(max-width:520px){
  .grid2{grid-template-columns:1fr}
  .job-right{flex-direction:row;align-items:center}
}
"""

LOGIN_HTML = """<!doctype html><html lang=vi><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>Đăng nhập · Idea Studio</title>
<style>""" + BASE_CSS + """
.login-wrap{max-width:400px;margin:0 auto;min-height:100vh;display:flex;flex-direction:column;justify-content:center;padding:24px}
.pw{width:100%;background:rgba(0,0,0,.26);border:1px solid var(--border);color:var(--text);
  border-radius:var(--r-sm);padding:14px;font-size:15px;font-family:inherit}
.pw:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px rgba(124,108,255,.18)}
</style></head><body><div class="bg"></div>
<div class="login-wrap">
  <div class="brand" style="justify-content:center;margin-bottom:22px">
    <span class="logo">💡</span>
    <div><div class="brand-name">Idea Studio</div><div class="brand-sub">Đăng nhập để tiếp tục</div></div>
  </div>
  <div class="card">
    <form method=post>
      <label class="lbl" for=token>Mật khẩu</label>
      <input class="pw" type=password id=token name=token autofocus autocomplete=current-password>
      {% if error %}<p style="color:var(--red);font-size:13px;margin:10px 2px 0">{{ error }}</p>{% endif %}
      <button class="cta" type=submit style="margin-top:18px">Đăng nhập</button>
    </form>
  </div>
</div></body></html>"""

INDEX_HTML = """<!doctype html><html lang=vi><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>Idea Studio · YouTube bot</title>
<style>""" + BASE_CSS + """</style></head><body><div class="bg"></div>
<div class="wrap">
  <header class="hd">
    <div class="brand">
      <span class="logo">💡</span>
      <div><div class="brand-name">Idea Studio</div><div class="brand-sub">Biến ý tưởng thành video, tự động</div></div>
    </div>
    {% if require_auth %}<a class="ghost" href="{{ url_for('logout') }}">Đăng xuất</a>{% endif %}
  </header>

  <section class="card">
    <h2 class="card-title">Tạo video mới</h2>
    <form id="ideaForm">
      <label class="lbl" for="idea">Ý tưởng của bạn</label>
      <textarea id="idea" name="idea" required placeholder="VD: Sự cạnh tranh khốc liệt giữa các công ty AI…&#10;Gõ tiếng Việt hay tiếng Anh đều được — video xuất ra tiếng Anh."></textarea>

      <label class="lbl">Loại video</label>
      <div class="seg">
        <label class="seg-opt is-active" data-mode="short">
          <input type="radio" name="mode" value="short" checked>
          <span class="seg-ico">📱</span><span class="seg-tt">Short</span><span class="seg-sb">Dọc · video ngắn</span>
        </label>
        <label class="seg-opt" data-mode="long">
          <input type="radio" name="mode" value="long">
          <span class="seg-ico">🎬</span><span class="seg-tt">Long-form</span><span class="seg-sb">Ngang · 5–7 phút</span>
        </label>
      </div>

      <div class="grid2">
        <div>
          <label class="lbl" for="privacy">Chế độ đăng YouTube</label>
          <div class="sel"><select id="privacy" name="privacy">
            {% for p in privacy_choices %}<option value="{{ p }}" {% if p == default_privacy %}selected{% endif %}>{{ p }}</option>{% endfor %}
          </select></div>
        </div>
        <div>
          <label class="lbl">Tự đăng lên YouTube</label>
          <label class="pub"><span>Đăng sau khi render</span>
            <span class="switch"><input type="checkbox" name="publish" checked><span class="track"></span></span>
          </label>
        </div>
      </div>

      <button class="cta" id="submitBtn" type="submit">Tạo video</button>
      <p class="hint">⏱️ Độ dài video tự khớp theo giọng đọc — không cần chỉnh thời lượng.</p>
    </form>
  </section>

  <section class="card">
    <div class="jobs-head"><h2 class="card-title">Hàng đợi &amp; lịch sử</h2><div class="stats" id="stats"></div></div>
    <div id="jobs"><div class="skeleton"></div><div class="skeleton"></div></div>
  </section>

  <footer class="ft">Tự làm mới mỗi 5 giây · mỗi lần render một video</footer>
</div>

<script>
const $ = (s, r=document) => r.querySelector(s);
const form = $('#ideaForm'), btn = $('#submitBtn');
const segOpts = document.querySelectorAll('.seg-opt');
segOpts.forEach(o => o.addEventListener('click', () => {
  segOpts.forEach(x => x.classList.remove('is-active'));
  o.classList.add('is-active');
  o.querySelector('input').checked = true;
}));

const BADGE = {pending:['pending','Chờ'], processing:['processing','Đang tạo…'], done:['done','Xong'], failed:['failed','Lỗi']};
function esc(s){ return (s||'').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
function fmt(ts){ if(!ts) return ''; const d = new Date(ts); return isNaN(d) ? ts : d.toLocaleString('vi-VN',{hour:'2-digit',minute:'2-digit',day:'2-digit',month:'2-digit'}); }

function renderJobs(jobs){
  const box = $('#jobs'), stats = $('#stats');
  const c = {pending:0, processing:0, done:0, failed:0};
  jobs.forEach(j => { c[j.status] = (c[j.status]||0) + 1; });
  stats.innerHTML =
    (c.processing ? '<span class="chip c-processing">'+c.processing+' đang tạo</span>' : '') +
    (c.pending ? '<span class="chip c-pending">'+c.pending+' chờ</span>' : '') +
    (c.done ? '<span class="chip c-done">'+c.done+' xong</span>' : '') +
    (c.failed ? '<span class="chip c-failed">'+c.failed+' lỗi</span>' : '');
  if(!jobs.length){ box.innerHTML = '<div class="empty"><div class="empty-ico">🗂️</div>Chưa có video nào. Nhập ý tưởng đầu tiên phía trên nhé!</div>'; return; }
  box.innerHTML = jobs.map(j => {
    const b = BADGE[j.status] || ['pending', j.status];
    const mi = j.mode === 'long' ? '🎬' : '📱';
    const mt = j.mode === 'long' ? 'Long-form' : 'Short';
    let res = '';
    if(j.status === 'done' && j.youtube_id) res = '<a class="link" href="https://youtube.com/watch?v='+esc(j.youtube_id)+'" target="_blank">▶ YouTube</a>';
    else if(j.status === 'done') res = '<span class="muted">đã render</span>';
    else if(j.status === 'failed') res = '<span class="err" title="'+esc(j.error)+'">'+esc((j.error||'lỗi').slice(0,48))+'</span>';
    else if(j.status === 'processing') res = '<span class="muted">đang xử lý…</span>';
    const title = j.output_title ? '<div class="jt">'+esc(j.output_title)+'</div>' : '';
    const dot = j.status === 'processing' ? '<span class="livedot"></span>' : '';
    return '<div class="job"><div class="job-ico">'+mi+'</div>'
      + '<div class="job-main"><div class="job-idea">'+esc((j.idea||'').slice(0,120))+'</div>'+title
      + '<div class="job-meta">'+mt+' · '+fmt(j.created_at)+'</div></div>'
      + '<div class="job-right"><span class="pill p-'+b[0]+'">'+dot+esc(b[1])+'</span><div>'+res+'</div></div></div>';
  }).join('');
}

async function poll(){ try{ const r = await fetch('/api/jobs',{cache:'no-store'}); if(r.ok) renderJobs(await r.json()); }catch(e){} }

form.addEventListener('submit', async e => {
  e.preventDefault();
  if(!$('#idea').value.trim()) return;
  btn.disabled = true; const old = btn.textContent; btn.textContent = 'Đang gửi…';
  try{
    const r = await fetch('/submit', {method:'POST', body:new FormData(form)});
    const j = await r.json().catch(() => ({}));
    if(r.ok && j.ok){ $('#idea').value = ''; btn.textContent = '✓ Đã thêm vào hàng đợi'; poll(); }
    else { btn.textContent = (j && j.error) ? j.error : 'Lỗi, thử lại'; }
  }catch(e){ btn.textContent = 'Lỗi mạng'; }
  setTimeout(() => { btn.disabled = false; btn.textContent = old; }, 1600);
});

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
