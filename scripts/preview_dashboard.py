"""Loopback-only UI preview with separate data and NO API/worker execution."""
import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["BOT_DATA_DIR"] = str(ROOT / "generated" / "dashboard-preview")
os.environ["DATABASE_URL"] = ""
os.environ["WEB_ACCESS_TOKEN"] = ""

import web_app
import youtube_shorts_bot as legacy
from movie_analysis.core import write_json

web_app.app.config["STUDIO_PREVIEW"] = True
archive = web_app.get_archive()
if not archive.recent_ideas():
    directory = legacy.DATA_DIR / "generated" / "movies" / "demo-review"
    directory.mkdir(parents=True, exist_ok=True)
    fixture = ROOT / "generated" / "movie-test-short"
    for name in ("script.response.json", "script_approved.json", "script_audit.response.json", "characters.json", "evidence.json", "review_plan.json", "usage.json"):
        if (fixture / name).is_file():
            shutil.copyfile(fixture / name, directory / name)
    data = {"filename": "test-short.mp4", "directory": str(directory), "source": str(directory / "source.mp4"),
            "movie_title": "Avengers: Doomsday · Demo", "target_seconds": 60, "rights_status": "unknown", "rights_basis": ""}
    job_id = archive.enqueue_idea("movie", json.dumps(data, ensure_ascii=False), 60, False, "private")
    archive.update_idea(job_id, "failed", error="Dữ liệu demo: lượt thử trước dừng ở bước duyệt thoại; chưa có bản video mới.")
    write_json(directory / "job.json", {"stage": "script_audit", "status": "failed", "title": "Avengers: Doomsday · Demo"})
    job_id = archive.enqueue_idea("short", legacy.encode_comparison_idea("Coca-Cola", "Pepsi", "Lịch sử thương hiệu"), None, False, "private")
    archive.update_idea(job_id, "done", output_title="Coca-Cola và Pepsi: cuộc đua trăm năm · Demo")
    archive.enqueue_idea("long", "Vì sao tiền giấy thay đổi thế giới? · Demo", None, False, "private")
web_app.reset_archive()
if __name__ == "__main__":
    from waitress import serve
    print("Dashboard preview: http://127.0.0.1:8085 (worker disabled)", flush=True)
    serve(web_app.app, host="127.0.0.1", port=8085, threads=4,
          max_request_body_size=web_app.app.config["MAX_CONTENT_LENGTH"])
