import importlib.util
import json
import sys
from pathlib import Path

SPEC = importlib.util.spec_from_file_location("bot", Path(__file__).parents[1] / "youtube_shorts_bot.py")
bot = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
sys.modules[SPEC.name] = bot
SPEC.loader.exec_module(bot)


def test_similarity_normalizes_vietnamese_accents():
    assert bot.similarity("Đế chế La Mã biến mất", "De che La Ma bien mat") == 1


def test_plan_parser_accepts_valid_plan():
    plan = bot.ShortPlan.from_dict({
        "topic": "T", "angle": "A", "title": "Title", "description": "#Shorts", "tags": ["shorts"],
        "hook": "A short script.", "narration": "A short script.", "closing_line": "A short script.",
        "fact_note": "No exaggeration", "source_hints": ["Smithsonian"],
        "scenes": [{"duration": 4, "visual_prompt": "A scene", "on_screen_text": "A mystery"}],
    })
    assert plan.scenes[0].duration == 4


def test_archive_blocks_exactly_repeated_plan(tmp_path):
    plan = bot.ShortPlan.from_dict({
        "topic": "Sự kiện Tunguska", "angle": "Vụ nổ năm 1908", "title": "Điều gì đã làm rung chuyển Siberia?",
        "description": "#Shorts", "tags": ["shorts"], "narration": "Một kịch bản ngắn.",
        "fact_note": "Không cường điệu", "source_hints": ["NASA"],
        "scenes": [{"duration": 4, "visual_prompt": "A scene", "on_screen_text": "Bí ẩn"}],
    })
    archive = bot.Archive(tmp_path / "shorts.db")
    archive.reserve(plan, tmp_path / "output")
    assert archive.duplicate_of(plan) is not None


def test_archive_counts_jobs_created_today(tmp_path):
    plan = bot.ShortPlan.from_dict({
        "topic": "Topic", "angle": "Angle", "title": "Title", "description": "#Shorts", "tags": ["shorts"],
        "narration": "A short script.", "fact_note": "No exaggeration", "source_hints": ["Museum"],
        "scenes": [{"duration": 4, "visual_prompt": "A scene"}],
    })
    archive = bot.Archive(tmp_path / "shorts.db")
    for number in range(3):
        current = bot.ShortPlan.from_dict({**plan.to_dict(), "title": f"Title {number}"})
        archive.reserve(current, tmp_path / f"output-{number}")
    assert archive.jobs_created_today() == 3


def test_three_pass_planner_returns_a_20_second_story(tmp_path):
    plan = {
        "topic": "The Green Sahara", "angle": "A desert shaped by monsoon shifts",
        "title": "When the Sahara Was Green", "description": "A desert was once a lake country. #Shorts",
        "tags": ["shorts", "science", "history"],
        "hook": "Twelve thousand years ago, the Sahara was green.",
        "narration": "Twelve thousand years ago, the Sahara was green. Monsoon rains fed lakes across North Africa, supporting grasslands and animals. Sediment and archaeological evidence reveal this African Humid Period, though its timing varied by region. Then Earth's orbital shifts weakened the monsoons. A desert can be a climate snapshot, not a permanent identity.",
        "closing_line": "A desert can be a climate snapshot, not a permanent identity.",
        "scenes": [{"duration": 5, "visual_prompt": "Scene one"}, {"duration": 5, "visual_prompt": "Scene two"}, {"duration": 5, "visual_prompt": "Scene three"}, {"duration": 5, "visual_prompt": "Scene four"}],
        "fact_note": "Regional timing varied.", "source_hints": ["NOAA", "peer-reviewed paleoclimate research"],
    }

    class FakeClient:
        def __init__(self):
            self.responses = [
                {"central_claim": "The Sahara had wet periods.", "evidence_points": ["lakes", "sediment", "monsoons"], "uncertainty": "regional timing", "fresh_angle": "climate snapshot", "source_leads": ["NOAA"], "avoid": ["exact dates"]},
                plan,
                {"quality_check": {"hook_score": 9, "clarity_score": 9}, "plan": plan},
            ]

        def chat(self, _prompt, temperature=0.55):
            return json.dumps(self.responses.pop(0))

    result = bot.plan_short(FakeClient(), bot.Archive(tmp_path / "shorts.db"), "green deserts", 20)
    assert result.hook == plan["hook"]
    assert sum(scene.duration for scene in result.scenes) == 20
