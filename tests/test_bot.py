import importlib.util
import json
import base64
from io import BytesIO
import re
import sys
from pathlib import Path
import requests
import pytest

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
        "thumbnail_text": "Named Subject",
        "hook": "A short script.", "narration": "A short script.", "closing_line": "A short script.",
        "fact_note": "No exaggeration", "source_hints": ["Smithsonian"],
        "scenes": [{"duration": 4, "visual_prompt": "A scene"}],
    })
    assert plan.scenes[0].duration == 4
    assert plan.thumbnail_text == "Named Subject"


def test_plan_parser_ignores_extra_scene_fields_and_accepts_visual_prompt_alt():
    plan = bot.ShortPlan.from_dict({
        "topic": "Major Event", "angle": "Decisive turning point", "title": "A Major Event",
        "description": "#History #Shorts", "tags": ["history", "shorts"],
        "hook": "A major event changed history.", "narration": "A major event changed history.",
        "closing_line": "Its consequences lasted for generations.",
        "fact_note": "Avoids unsupported claims", "source_hints": ["National archive"],
        "scenes": [
            {"duration": "5.5", "visual_prompt": "A wide historical scene.", "visual_prompt_alt": "Unused alternate scene.", "camera_move": "slow push"},
            {"duration": 5.5, "visual_prompt_alt": "A detailed archival object on a table."},
        ],
    })

    assert plan.scenes[0] == bot.Scene(duration=5.5, visual_prompt="A wide historical scene.")
    assert plan.scenes[1] == bot.Scene(duration=5.5, visual_prompt="A detailed archival object on a table.")


def test_social_vietnamese_plan_repairs_an_overlong_narration():
    plan = bot.ShortPlan.from_dict({
        "topic": "Panama Canal Locks", "angle": "How water lifts ships", "title": "Panama Canal Locks",
        "description": "#History #Engineering", "tags": ["History", "Engineering"],
        "hook": "Ships climb a water staircase.", "narration": "Ships climb a water staircase.",
        "closing_line": "The locks changed global trade.", "fact_note": "No unsupported figures",
        "source_hints": ["Panama Canal Authority"],
        "scenes": [{"duration": 10, "visual_prompt": "Canal locks from above."}],
    })
    overlong = " ".join(["từ"] * 249)
    repaired = " ".join(["từ"] * 207)
    prompts = []

    class FakeClient:
        def __init__(self):
            self.responses = [
                {"title": "Kênh đào Panama", "description": "Mô tả #LichSu #CongTrinh", "tags": ["LichSu", "CongTrinh"], "narration": overlong},
                {"title": "Kênh đào Panama", "description": "Mô tả #LichSu #CongTrinh", "tags": ["LichSu", "CongTrinh"], "narration": repaired},
            ]

        def chat(self, prompt, temperature=0.55):
            prompts.append(prompt)
            return json.dumps(self.responses.pop(0))

    social = bot.plan_social_vietnamese(FakeClient(), plan, 60)

    assert len(re.findall(r"\b\w+\b", social.narration, flags=re.UNICODE)) == 207
    assert "Plan to rewrite" in prompts[1]


def test_prepare_social_video_uses_actual_english_narration_duration(tmp_path, monkeypatch):
    plan = bot.ShortPlan.from_dict({
        "topic": "Major Event", "angle": "Turning point", "title": "Major Event",
        "description": "#History #Shorts", "tags": ["History", "Shorts"],
        "hook": "A major event changed history.", "narration": "A major event changed history.",
        "closing_line": "Its consequences lasted.", "fact_note": "No unsupported figures",
        "source_hints": ["Archive"], "scenes": [{"duration": 10, "visual_prompt": "Historic scene."}],
    })
    (tmp_path / "narration.mp3").write_bytes(b"audio")
    captured = []
    social = bot.SocialPlan(title="Tiêu đề", description="Mô tả #A #B", tags=["A", "B"], narration="Lời đọc")

    monkeypatch.setattr(bot, "measured_narration_duration", lambda *_args: 65.448)
    monkeypatch.setattr(bot, "plan_social_vietnamese", lambda _llm, _plan, duration: captured.append(duration) or social)
    monkeypatch.setattr(bot, "render_social_video", lambda *_args: tmp_path / "short_vi.mp4")

    bot.prepare_social_video(plan, object(), object(), tmp_path, bot.Settings(duration=60))

    assert captured == [65]


def test_vietnamese_comparison_uses_scene_audio_to_retime_pointer_visuals(tmp_path, monkeypatch):
    english_segments = [
        "Alpha and Beta begin.",
        "Alpha point one.",
        "Beta point two.",
        "Alpha point three.",
        "Beta point four.",
        "Alpha and Beta finish.",
    ]
    vietnamese_segments = [
        "Alpha và Beta bắt đầu.",
        "Alpha có điểm một.",
        "Beta có điểm hai.",
        "Alpha có điểm ba.",
        "Beta có điểm bốn.",
        "Alpha và Beta kết thúc.",
    ]
    plan = bot.ShortPlan.from_dict({
        "topic": "Alpha vs Beta", "angle": "Differences", "title": "Alpha vs Beta",
        "subject_a": "Alpha", "subject_b": "Beta",
        "description": "Description #A #B", "tags": ["A", "B"],
        "hook": english_segments[0], "narration": " ".join(english_segments),
        "closing_line": english_segments[-1],
        "scenes": [
            {"duration": 5, "focus": focus, "voiceover": text, "visual_prompt": text}
            for focus, text in zip(["both", "a", "b", "a", "b", "both"], english_segments)
        ],
        "fact_note": "Note", "source_hints": ["Source"],
    })
    social = bot.SocialPlan(
        title="Alpha và Beta",
        description="Mô tả #A #B",
        tags=["A", "B"],
        narration=" ".join(vietnamese_segments),
        scene_voiceovers=vietnamese_segments,
    )
    (tmp_path / "visuals.mp4").write_bytes(b"v" * 2048)
    tts_calls = []
    captured_durations = []
    part_seconds = [3.0, 4.0, 2.0, 5.0, 3.0, 2.0]

    class FakeTTS:
        def speech(self, text, destination):
            tts_calls.append(text)
            destination.write_bytes(b"a" * 2048)

    def fake_duration(path, _label):
        path = Path(path)
        if path.name == "narration_vi.mp3":
            return sum(part_seconds)
        index = int(path.stem.rsplit("_", 1)[1]) - 1
        return part_seconds[index]

    def fake_run(command, timeout_seconds=None):
        Path(command[-1]).write_bytes(b"m" * 2048)

    def fake_visuals(_plan, durations, _output_dir, destination):
        captured_durations.extend(durations)
        destination.write_bytes(b"v" * 2048)
        return destination

    monkeypatch.setattr(bot, "require_tools", lambda: None)
    monkeypatch.setattr(bot, "measured_narration_duration", fake_duration)
    monkeypatch.setattr(bot, "run", fake_run)
    monkeypatch.setattr(bot, "render_comparison_social_visuals", fake_visuals)
    monkeypatch.setattr(bot, "write_ass_captions", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        bot,
        "mux_video_audio_with_captions",
        lambda _visuals, _narration, _captions, output, *_args, **_kwargs: output.write_bytes(b"f" * 2048),
    )

    result = bot.render_social_video(social, FakeTTS(), tmp_path, bot.Settings(), plan)

    assert result == tmp_path / "short_vi.mp4"
    assert tts_calls == vietnamese_segments
    assert captured_durations == part_seconds


def test_thumbnail_headline_uses_named_subject_and_wraps_to_two_lines():
    plan = bot.ShortPlan.from_dict({
        "topic": "The Strait of Hormuz", "angle": "Oil risk", "title": "The Trade Crisis",
        "thumbnail_text": "Strait of Hormuz", "description": "#News", "tags": ["News"],
        "hook": "Hook.", "narration": "Hook.", "closing_line": "Hook.",
        "fact_note": "Note", "source_hints": ["News"],
        "scenes": [{"duration": 4, "visual_prompt": "A ship"}],
    })

    headline = bot.thumbnail_headline(plan)

    assert headline == "STRAIT OF HORMUZ"
    assert bot.thumbnail_headline_lines("THE STRAIT OF HORMUZ AND GLOBAL OIL PRICES RISE") == "THE STRAIT OF HORMUZ\nAND GLOBAL OIL PRICES…"


def test_title_is_prefixed_with_the_named_subject_when_the_planner_returns_a_vague_headline():
    plan = bot.ShortPlan.from_dict({
        "topic": "The Strait of Hormuz", "angle": "Oil risk", "title": "The Trade Crisis",
        "thumbnail_text": "Strait of Hormuz", "description": "#News", "tags": ["News"],
        "hook": "Hook.", "narration": "Hook.", "closing_line": "Hook.",
        "fact_note": "Note", "source_hints": ["News"],
        "scenes": [{"duration": 4, "visual_prompt": "A ship"}],
    })

    bot.ensure_title_names_main_subject(plan)

    assert plan.title == "Strait of Hormuz: The Trade Crisis"


def test_archive_blocks_exactly_repeated_plan(tmp_path):
    plan = bot.ShortPlan.from_dict({
        "topic": "Sự kiện Tunguska", "angle": "Vụ nổ năm 1908", "title": "Điều gì đã làm rung chuyển Siberia?",
        "description": "#Shorts", "tags": ["shorts"], "narration": "Một kịch bản ngắn.",
        "fact_note": "Không cường điệu", "source_hints": ["NASA"],
        "scenes": [{"duration": 4, "visual_prompt": "A scene"}],
    })
    archive = bot.Archive(tmp_path / "shorts.db")
    archive.reserve(plan, tmp_path / "output")
    assert archive.duplicate_of(plan) is not None


def test_long_form_subject_match_blocks_same_route_with_a_different_angle():
    first = "Trump Iran strikes and Strait of Hormuz security risk"
    second = "Strait of Hormuz economic risk in the US Iran clash"

    assert bot.same_long_form_subject(first, second)
    assert not bot.same_long_form_subject(first, "OpenAI changes the consumer AI market")


def test_long_form_subject_match_catches_rephrased_headline_with_partial_overlap():
    assert bot.same_long_form_subject(
        "US Iran escalation and the Strait of Hormuz",
        "Strait of Hormuz blockade and global oil prices",
    )


def test_archive_blocks_repeated_long_form_subject_but_ignores_short_subject(tmp_path):
    archived = bot.ShortPlan.from_dict({
        "topic": "Trump Iran strikes and Strait of Hormuz security risk",
        "angle": "Military escalation", "title": "Iran Strikes Put Hormuz Back at Risk",
        "description": "#News", "tags": ["News"], "narration": "A script.",
        "fact_note": "No exaggeration", "source_hints": ["News"],
        "scenes": [{"duration": 4, "visual_prompt": "A tanker"}],
    })
    candidate = bot.ShortPlan.from_dict({
        "topic": "Strait of Hormuz economic risk in the US Iran clash",
        "angle": "Consumer and oil costs", "title": "Hormuz Is the Biggest Economic Risk",
        "description": "#News", "tags": ["News"], "narration": "A different script.",
        "fact_note": "No exaggeration", "source_hints": ["News"],
        "scenes": [{"duration": 4, "visual_prompt": "A tanker"}],
    })
    archive = bot.Archive(tmp_path / "shorts.db")
    archive.reserve(archived, tmp_path / "short-output")
    assert archive.same_long_form_subject_as(candidate) is None

    archived_long = bot.ShortPlan.from_dict({
        **archived.to_dict(),
        "angle": "Security consequences for shipping",
        "title": "The Strait of Hormuz Security Shock",
    })
    archive.reserve(archived_long, tmp_path / "long-20260714-hormuz")
    assert archive.same_long_form_subject_as(candidate) is not None


def test_archive_blocks_rephrased_long_form_subject(tmp_path):
    archived = bot.ShortPlan.from_dict({
        "topic": "US Iran escalation and the Strait of Hormuz",
        "angle": "Military escalation", "title": "Iran Strikes Put Hormuz Back at Risk",
        "description": "#News", "tags": ["News"], "narration": "A script.",
        "fact_note": "No exaggeration", "source_hints": ["News"],
        "scenes": [{"duration": 4, "visual_prompt": "A tanker"}],
    })
    candidate = bot.ShortPlan.from_dict({
        "topic": "Strait of Hormuz blockade and global oil prices",
        "angle": "Oil markets", "title": "Hormuz Blockade Sends Oil Prices Soaring",
        "description": "#News", "tags": ["News"], "narration": "A different script.",
        "fact_note": "No exaggeration", "source_hints": ["News"],
        "scenes": [{"duration": 4, "visual_prompt": "A tanker"}],
    })
    archive = bot.Archive(tmp_path / "shorts.db")
    archive.reserve(archived, tmp_path / "long-20260714-hormuz")
    assert archive.same_long_form_subject_as(candidate) is not None


def test_long_form_explainer_category_avoids_repeats_within_run():
    first = bot.choose_long_form_explainer_category()
    assert first in bot.LONG_FORM_CATEGORIES

    # Exclusion works on slugs: with every category but the last ruled out, the
    # helper must pick that last one.
    excluded = {category.slug for category in bot.LONG_FORM_CATEGORIES[:-1]}
    assert bot.choose_long_form_explainer_category(excluded) == bot.LONG_FORM_CATEGORIES[-1]

    # If all are excluded it still returns a valid category rather than crashing.
    all_slugs = {category.slug for category in bot.LONG_FORM_CATEGORIES}
    assert bot.choose_long_form_explainer_category(all_slugs) in bot.LONG_FORM_CATEGORIES

    # Hard science, space, and cosmology were removed for being too abstract/long-winded.
    categories = " ".join(bot.LONG_FORM_EXPLAINER_CATEGORIES).lower()
    for banned in ("space", "cosmos", "universe", "astronomy", "planet", "science explainer"):
        assert banned not in categories


def test_every_long_form_category_maps_to_a_distinct_playlist():
    slugs = [category.slug for category in bot.LONG_FORM_CATEGORIES]
    titles = [category.playlist_title for category in bot.LONG_FORM_CATEGORIES]

    assert len(set(slugs)) == len(slugs), "slugs key the playlist lookup, so they must be unique"
    # ensure_playlist matches an existing playlist by exact title, so two topics
    # sharing a title would silently file into the same playlist.
    assert len(set(titles)) == len(titles)
    assert bot.LONG_FORM_CATEGORY_BY_SLUG.keys() == set(slugs)

    for category in bot.LONG_FORM_CATEGORIES:
        assert category.prompt and category.playlist_description
        # YouTube truncates longer titles in the sidebar and playlist cards.
        assert len(category.playlist_title) <= 60


def test_long_form_topic_category_survives_the_plan_json_round_trip():
    # A long-form job is resumable: the plan is written to plan.json and reloaded,
    # so a slug that does not round-trip would be lost before the upload step.
    plan = bot.ShortPlan.from_dict({
        "topic": "How money was invented", "angle": "From barter to coins",
        "title": "How Money Was Invented", "description": "An explainer.",
        "tags": ["History"], "narration": "A short script. It ends here.",
        "fact_note": "Qualitative", "source_hints": ["General history"],
        "scenes": [{"duration": 4, "visual_prompt": "A scene"}],
    })
    assert plan.topic_category == ""  # absent from the model's schema

    plan.topic_category = "origins"
    reloaded = bot.ShortPlan.from_dict(json.loads(json.dumps(plan.to_dict())))

    assert reloaded.topic_category == "origins"
    assert bot.LONG_FORM_CATEGORY_BY_SLUG[reloaded.topic_category].playlist_title


def test_research_prompt_receives_archive_and_rejected_candidates(tmp_path):
    archived = bot.ShortPlan.from_dict({
        "topic": "Antikythera mechanism", "angle": "Ancient gears predicted eclipses",
        "title": "Ancient Gears Predicted Eclipses", "description": "#Shorts",
        "tags": ["shorts", "history"], "narration": "A short script.",
        "fact_note": "Avoids overclaiming precision", "source_hints": ["Museum"],
        "scenes": [{"duration": 4, "visual_prompt": "A scene"}],
    })
    fresh_plan = {
        "topic": "The Green Sahara", "angle": "A desert shaped by monsoon shifts",
        "title": "When the Sahara Was Green", "description": "A desert was once a lake country. #Shorts",
        "tags": ["shorts", "science", "history"],
        "hook": "Twelve thousand years ago, the Sahara was green.",
        "narration": "Twelve thousand years ago, the Sahara was green. Monsoon rains fed lakes across North Africa, supporting grasslands and animals. Sediment evidence shows this humid period varied by region. Then Earth's orbit helped weaken the rains. A desert can be a climate snapshot, not a permanent identity.",
        "closing_line": "A desert can be a climate snapshot, not a permanent identity.",
        "scenes": [{"duration": 5, "visual_prompt": "Scene one"}, {"duration": 5, "visual_prompt": "Scene two"}, {"duration": 5, "visual_prompt": "Scene three"}, {"duration": 5, "visual_prompt": "Scene four"}],
        "fact_note": "Regional timing varied.", "source_hints": ["NOAA"],
    }
    archive = bot.Archive(tmp_path / "shorts.db")
    archive.reserve(archived, tmp_path / "old")
    prompts = []

    class FakeClient:
        def __init__(self):
            self.responses = [
                {"central_claim": "The Sahara had wet periods.", "evidence_points": ["lakes", "sediment", "monsoons"], "uncertainty": "regional timing", "fresh_angle": "climate snapshot", "source_leads": ["NOAA"], "avoid": ["exact dates"]},
                fresh_plan,
                {"quality_check": {"hook_score": 9, "clarity_score": 9}, "plan": fresh_plan},
            ]

        def chat(self, prompt, temperature=0.55):
            prompts.append(prompt)
            return json.dumps(self.responses.pop(0))

    bot.plan_short(
        FakeClient(),
        archive,
        "little-known discoveries",
        20,
        [{"topic": "Antikythera mechanism", "angle": "eclipses", "title": "Ancient Greek Gears That Predicted Eclipses"}],
    )

    assert "Existing archive" in prompts[0]
    assert "Ancient Gears Predicted Eclipses" in prompts[0]
    assert "Rejected candidates from this run" in prompts[0]
    assert "Ancient Greek Gears That Predicted Eclipses" in prompts[0]
    assert "COMPARISON YouTube Short" in prompts[0]
    assert "Fame & clarity gate" in prompts[0]
    assert "subject_fame_score" in prompts[0]
    assert "Neutrality" in prompts[0]
    assert "OPPOSING or rival" in prompts[0]
    assert "COMPARISON YouTube Short" in prompts[1]
    assert "subject_a" in prompts[1]
    assert "TITLE REQUIREMENT" in prompts[1]
    assert "thumbnail_text" in prompts[1]
    assert '"focus"' in prompts[1]
    assert "NEUTRALITY" in prompts[1]
    assert "fairness_score" in prompts[2]
    assert "NEUTRALITY" in prompts[2]
    assert '"focus"' in prompts[2]
    assert "subject_a_image_query" in prompts[2]


def test_plan_short_repairs_too_short_quality_pass_instead_of_aborting(tmp_path):
    def segment(prefix, count):
        words = prefix.split()
        return " ".join(words + [f"detail{index}" for index in range(count - len(words))])

    initial_segments = [
        segment("Alpha and Beta begin this comparison", 12),
        segment("Alpha takes the first contrast", 14),
        segment("Beta takes the second contrast", 14),
        segment("Alpha adds another established difference", 14),
        segment("Beta adds the final established difference", 14),
        segment("Alpha and Beta end without a winner", 12),
    ]
    repaired_segments = [
        segment("Alpha and Beta begin this comparison", 20),
        segment("Alpha takes the first contrast", 24),
        segment("Beta takes the second contrast", 24),
        segment("Alpha adds another established difference", 24),
        segment("Beta adds the final established difference", 24),
        segment("Alpha and Beta end without a winner", 24),
    ]
    short_plan = {
        "topic": "Alpha vs Beta",
        "angle": "Their established differences",
        "title": "Alpha vs Beta: The Differences",
        "description": "Alpha and Beta compared. #Shorts",
        "tags": ["comparison", "shorts"],
        "thumbnail_text": "Alpha vs Beta",
        "hook": initial_segments[0],
        "narration": " ".join(initial_segments),
        "closing_line": initial_segments[-1],
        "subject_a": "Alpha",
        "subject_b": "Beta",
        "subject_a_image_query": "Alpha portrait",
        "subject_b_image_query": "Beta portrait",
        "subject_fame_score": 9,
        "fascination_score": 8,
        "clarity_score": 9,
        "scenes": [
            {
                "duration": 10,
                "focus": focus,
                "visual_prompt": f"Comparison scene {index}",
                "voiceover": voiceover,
            }
            for index, (focus, voiceover) in enumerate(
                zip(["both", "a", "b", "a", "b", "both"], initial_segments),
                start=1,
            )
        ],
        "fact_note": "No unsupported facts.",
        "source_hints": ["Official sources"],
    }
    repaired_copy = {
        "hook": repaired_segments[0],
        "narration": " ".join(repaired_segments),
        "closing_line": repaired_segments[-1],
        "scene_voiceovers": repaired_segments,
    }
    prompts = []

    class FakeClient:
        def __init__(self):
            self.responses = [
                {
                    "subject_a": "Alpha",
                    "subject_b": "Beta",
                    "matchup_hook": "Alpha versus Beta",
                    "key_contrasts": ["one", "two"],
                    "surprise": "Neither is declared the winner.",
                },
                short_plan,
                {"quality_check": {"hook_score": 9, "clarity_score": 9}, "plan": short_plan},
                repaired_copy,
            ]

        def chat(self, prompt, temperature=0.55):
            prompts.append(prompt)
            return json.dumps(self.responses.pop(0))

    client = FakeClient()
    result = bot.plan_short(client, bot.Archive(tmp_path / "shorts.db"), "comparisons", 60)

    assert bot.spoken_word_count(result.narration) == 140
    assert "Repair only the spoken English copy" in prompts[3]
    assert result.narration == " ".join(scene.voiceover for scene in result.scenes)
    assert client.responses == []


def test_choose_novel_plan_passes_duplicate_candidate_to_retry(tmp_path, monkeypatch):
    duplicate_plan = bot.ShortPlan.from_dict({
        "topic": "Antikythera mechanism", "angle": "Ancient gears predicted eclipses",
        "title": "Ancient Gears Predicted Eclipses", "description": "#Shorts",
        "tags": ["shorts"], "narration": "A short script.",
        "fact_note": "Avoids overclaiming precision", "source_hints": ["Museum"],
        "scenes": [{"duration": 4, "visual_prompt": "A scene"}],
    })
    fresh_plan = bot.ShortPlan.from_dict({
        "topic": "The Green Sahara", "angle": "A desert shaped by monsoon shifts",
        "title": "When the Sahara Was Green", "description": "#Shorts",
        "tags": ["shorts"], "narration": "A short script.",
        "fact_note": "Regional timing varied", "source_hints": ["NOAA"],
        "scenes": [{"duration": 4, "visual_prompt": "A scene"}],
    })
    archive = bot.Archive(tmp_path / "shorts.db")
    archive.reserve(duplicate_plan, tmp_path / "old")
    rejected_seen = []

    def fake_plan_short(_client, _archive, _theme, _duration, rejected=None):
        rejected_seen.append(list(rejected or []))
        return duplicate_plan if len(rejected_seen) == 1 else fresh_plan

    monkeypatch.setattr(bot, "plan_short", fake_plan_short)

    result = bot.choose_novel_plan(object(), archive, "little-known discoveries", 20, max_attempts=2)

    assert result == fresh_plan
    assert rejected_seen[0] == []
    assert rejected_seen[1][0]["title"] == "Ancient Gears Predicted Eclipses"
    assert rejected_seen[1][0]["matched_existing_title"] == "Ancient Gears Predicted Eclipses"


@pytest.mark.parametrize(
    "angle",
    [
        "Newton as a human processing engine, not magic",
        "A planet can lock itself into a self-feeding climate trap",
        "The unintended hacking of Earth's biological carrying capacity",
    ],
)
def test_short_editorial_filter_rejects_abstract_theory_angles(angle):
    plan = bot.ShortPlan.from_dict({
        "topic": angle,
        "angle": angle,
        "title": angle,
        "description": "Description #A #B",
        "tags": ["A", "B"],
        "hook": angle,
        "narration": angle,
        "closing_line": angle,
        "fact_note": "Note",
        "source_hints": ["Source"],
        "scenes": [{"duration": 10, "visual_prompt": "Concrete scene"}],
    })

    assert bot.short_editorial_rejection_reason(plan) is not None


def test_short_editorial_filter_rejects_minor_local_disaster_story():
    plan = bot.ShortPlan.from_dict({
        "topic": "Boston's Great Molasses Flood",
        "angle": "How one underbuilt tank turned millions of gallons into a city disaster",
        "title": "One Tank Failed, and a City Paid",
        "description": "Description #A #B", "tags": ["A", "B"],
        "hook": "One tank failed, and a city paid.",
        "narration": "One tank failed, and a city paid. An underbuilt tank caused a city disaster.",
        "closing_line": "The underbuilt tank changed local rules.",
        "fact_note": "Note", "source_hints": ["Source"],
        "scenes": [{"duration": 10, "visual_prompt": "Concrete scene"}],
        "subject_fame_score": 5,
        "fascination_score": 4,
        "clarity_score": 5,
        "appeal_reason": "A notable local industrial accident.",
    })

    reason = bot.short_editorial_rejection_reason(plan)

    assert reason is not None
    assert "minor or local incident" in reason


def test_short_editorial_filter_rejects_obscure_subject_by_fame_score():
    plan = bot.ShortPlan.from_dict({
        "topic": "A little-known regional event", "angle": "A surprising anecdote", "title": "A Quiet Mystery",
        "description": "Description #A #B", "tags": ["A", "B"],
        "hook": "A quiet mystery began here.", "narration": "A quiet mystery began here.",
        "closing_line": "Few people ever heard of it.", "fact_note": "Note", "source_hints": ["Source"],
        "scenes": [{"duration": 10, "visual_prompt": "Concrete scene"}],
        "subject_fame_score": 4,
        "fascination_score": 7,
        "clarity_score": 8,
    })

    assert "insufficient fame or clarity" in bot.short_editorial_rejection_reason(plan)


def test_short_editorial_filter_accepts_famous_present_day_subject():
    plan = bot.ShortPlan.from_dict({
        "topic": "A globally famous tech founder", "angle": "The decision that almost sank the company",
        "title": "The Bet That Nearly Ended a Famous Company",
        "description": "Description #A #B", "tags": ["A", "B"],
        "hook": "One decision nearly ended a company everyone now knows.",
        "narration": "One decision nearly ended a company everyone now knows. It became a global giant instead.",
        "closing_line": "It became a global giant instead.", "fact_note": "Note", "source_hints": ["Source"],
        "scenes": [{"duration": 10, "visual_prompt": "Concrete scene"}],
        "subject_fame_score": 9,
        "fascination_score": 8,
        "clarity_score": 9,
    })

    assert bot.short_editorial_rejection_reason(plan) is None


def test_short_categories_are_diverse_comparison_matchups():
    categories = " ".join(bot.COMPARISON_TOPIC_CATEGORIES).lower()

    # Every category is a two-subject comparison ("vs" / rival / opposing).
    assert len(bot.COMPARISON_TOPIC_CATEGORIES) >= 5
    assert " vs " in categories
    for keyword in ("rival", "brands", "political", "places", "technolog", "everyday"):
        assert keyword in categories
    # The user's example matchups appear as guidance.
    assert "coca-cola vs pepsi" in categories
    assert "republicans vs democrats" in categories
    assert "north pole vs south pole" in categories


def test_get_random_topic_rule_requests_two_neutral_subjects():
    rule = bot.get_random_topic_rule()
    assert "COMPARISON" in rule
    assert "TWO" in rule
    assert "neutral" in rule.lower()
    assert "winner" in rule.lower()  # instructs NOT to declare a winner


def test_choose_novel_plan_retries_abstract_candidate(tmp_path, monkeypatch):
    abstract = bot.ShortPlan.from_dict({
        "topic": "Planetary feedback",
        "angle": "A planet can lock itself into a climate trap",
        "title": "The Planet That Locked Itself",
        "description": "Description #A #B", "tags": ["A", "B"],
        "hook": "A planet can lock itself into a climate trap.",
        "narration": "A planet can lock itself into a climate trap.",
        "closing_line": "The trap feeds itself.", "fact_note": "Note", "source_hints": ["Source"],
        "scenes": [{"duration": 10, "visual_prompt": "A planet"}],
    })
    concrete = bot.ShortPlan.from_dict({
        "topic": "Airplane window holes",
        "angle": "The tiny hole prevents dangerous pressure stress",
        "title": "That Tiny Airplane Window Hole Has a Safety Job",
        "description": "Description #A #B", "tags": ["A", "B"],
        "hook": "That tiny window hole protects the outer pane.",
        "narration": "That tiny window hole protects the outer pane.",
        "closing_line": "A tiny detail manages a huge pressure difference.",
        "fact_note": "Note", "source_hints": ["FAA"],
        "scenes": [{"duration": 10, "visual_prompt": "An airplane window"}],
    })
    rejected_seen = []

    def fake_plan_short(_llm, _archive, _theme, _duration, rejected=None):
        rejected_seen.append(list(rejected or []))
        return abstract if len(rejected_seen) == 1 else concrete

    monkeypatch.setattr(bot, "plan_short", fake_plan_short)

    result = bot.choose_novel_plan(object(), bot.Archive(tmp_path / "shorts.db"), "documentary", 60, 2)

    assert result == concrete
    assert "editorial_rejection" in rejected_seen[1][0]


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


def test_long_form_schedule_waits_two_local_calendar_days(tmp_path):
    plan = bot.ShortPlan.from_dict({
        "topic": "Global shipping", "angle": "Trade routes", "title": "Trade Routes",
        "description": "Description #A #B", "tags": ["A", "B"],
        "narration": "A short script.", "fact_note": "No exaggeration", "source_hints": ["News"],
        "scenes": [{"duration": 50, "visual_prompt": "A ship"}],
    })
    archive = bot.Archive(tmp_path / "shorts.db")
    record_id = archive.reserve(plan, tmp_path / "long-20260713-trade-routes")
    archive.mark(record_id, "rendered")
    archive.conn.execute(
        "UPDATE shorts SET created_at = ? WHERE id = ?",
        ("2026-07-13T01:00:00+00:00", record_id),
    )
    archive.conn.commit()
    settings = bot.Settings(long_form_interval_days=2, long_form_timezone="Asia/Bangkok")

    tuesday = bot.datetime.fromisoformat("2026-07-14T20:00:00+07:00")
    wednesday = bot.datetime.fromisoformat("2026-07-15T20:00:00+07:00")

    assert bot.long_form_is_due(archive, settings, tuesday) == (False, 1)
    assert bot.long_form_is_due(archive, settings, wednesday) == (True, 2)


def test_short_daily_limit_does_not_count_long_form_job(tmp_path):
    plan = bot.ShortPlan.from_dict({
        "topic": "Topic", "angle": "Angle", "title": "Title", "description": "#A #B",
        "tags": ["A", "B"], "narration": "A short script.", "fact_note": "Note",
        "source_hints": ["Source"], "scenes": [{"duration": 10, "visual_prompt": "Scene"}],
    })
    archive = bot.Archive(tmp_path / "shorts.db")
    archive.reserve(plan, tmp_path / "long-20260714-topic")

    assert archive.jobs_created_today() == 0


def test_materialize_credential_file_from_base64(tmp_path, monkeypatch):
    destination = tmp_path / "credential.json"
    monkeypatch.setenv("TEST_CREDENTIAL_B64", base64.b64encode(b'{"credential": true}').decode())
    bot.materialize_credential_file("TEST_CREDENTIAL_B64", destination)
    assert destination.read_bytes() == b'{"credential": true}'


def test_settings_accepts_48_second_duration(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "openai")
    monkeypatch.setenv("SHORT_DURATION_SECONDS", "48")
    monkeypatch.setenv("BRAVE_WEB_IMAGES_PER_LONG_FORM", "5")

    settings = bot.Settings.from_env()

    assert settings.duration == 48
    # Stick-figure long-form defaults to 15 AI images; an explicit Brave override adds web slots.
    assert settings.long_form_min_scenes == 20
    assert settings.long_form_max_scenes == 20
    assert settings.long_form_openai_images == 15
    assert settings.brave_web_images_per_long_form == 5
    assert settings.text_model == "gpt-5.4-mini"
    assert settings.text_reasoning_effort == "low"
    assert settings.text_long_form_reasoning_effort == "medium"


def test_settings_reads_long_form_ai_image_count(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "openai")
    monkeypatch.delenv("BRAVE_WEB_IMAGES_PER_LONG_FORM", raising=False)
    monkeypatch.setenv("LONG_FORM_AI_IMAGES", "18")

    settings = bot.Settings.from_env()

    # No forced Brave slots by default, so every scene is an AI cartoon stick-figure image.
    assert settings.long_form_openai_images == 18
    assert settings.brave_web_images_per_long_form == 0
    assert settings.long_form_min_scenes == 18
    assert settings.long_form_max_scenes == 18


def test_settings_reads_corner_overlay_video_options(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "openai")
    monkeypatch.setenv("OVERLAY_VIDEO_FILE", "assets/corner.mp4")
    monkeypatch.setenv("OVERLAY_VIDEO_SHORT_WIDTH", "176")
    monkeypatch.setenv("OVERLAY_VIDEO_LONG_FORM_WIDTH", "144")
    monkeypatch.setenv("OVERLAY_VIDEO_RIGHT_MARGIN", "84")
    monkeypatch.setenv("OVERLAY_VIDEO_BOTTOM_MARGIN", "28")
    monkeypatch.setenv("OVERLAY_VIDEO_CORNER_RADIUS", "16")
    monkeypatch.setenv("OVERLAY_VIDEO_LOOP_GAP_SECONDS", "4.5")

    settings = bot.Settings.from_env()

    assert settings.overlay_video == bot.ROOT / "assets/corner.mp4"
    assert settings.overlay_video_short_width == 176
    assert settings.overlay_video_long_form_width == 144
    assert settings.overlay_video_right_margin == 84
    assert settings.overlay_video_bottom_margin == 28
    assert settings.overlay_video_corner_radius == 16
    assert settings.overlay_video_loop_gap_seconds == 4.5


def test_audio_led_short_keeps_word_target_as_guidance_not_a_hard_gate():
    assert bot.target_narration_word_bounds(60) == (135, 153)
    assert bot.target_vietnamese_narration_word_bounds(60) == (180, 207)
    assert bot.narration_word_bounds(60) == (90, 244)


def test_settings_reads_scheduled_daily_limit(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "openai")
    monkeypatch.setenv("SCHEDULED_DAILY_LIMIT", "6")

    assert bot.Settings.from_env().scheduled_daily_limit == 6


def test_settings_defaults_english_to_google_leda_and_vietnamese_to_google_aoede(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "openai")
    monkeypatch.delenv("GOOGLE_TTS_VOICE", raising=False)
    monkeypatch.delenv("GOOGLE_TTS_SPEAKING_RATE", raising=False)
    monkeypatch.delenv("GOOGLE_TTS_VI_VOICE", raising=False)
    monkeypatch.delenv("GOOGLE_TTS_VI_SPEAKING_RATE", raising=False)

    settings = bot.Settings.from_env()

    assert settings.google_tts_voice == "en-US-Chirp3-HD-Leda"
    assert settings.google_tts_speaking_rate == 1.05
    assert settings.google_tts_vietnamese_voice == "vi-VN-Chirp3-HD-Aoede"
    assert settings.google_tts_vietnamese_speaking_rate == 1.0


def test_openai_text_uses_responses_api_and_extracts_output(monkeypatch):
    captured = {}

    class FakeResponse:
        ok = True
        status_code = 200
        text = ""

        def json(self):
            return {
                "status": "completed",
                "output": [
                    {"type": "reasoning", "content": []},
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": '{"topic":"test"}'}],
                    },
                ],
            }

    def fake_post(url, headers, json, timeout):
        captured.update(url=url, headers=headers, payload=json, timeout=timeout)
        return FakeResponse()

    monkeypatch.setattr(bot.requests, "post", fake_post)
    settings = bot.Settings(openai_api_key="openai", text_attempts=1)

    result = bot.OpenAITextClient(settings).chat("Return a topic", temperature=0.2)

    assert result == '{"topic":"test"}'
    assert captured["url"] == "https://api.openai.com/v1/responses"
    assert captured["headers"]["Authorization"] == "Bearer openai"
    assert captured["payload"]["model"] == "gpt-5.4-mini"
    assert captured["payload"]["reasoning"] == {"effort": "low"}
    assert captured["payload"]["max_output_tokens"] == 16000
    assert "temperature" not in captured["payload"]


def test_openai_text_retries_transient_error(monkeypatch):
    calls = {"count": 0}

    class FakeResponse:
        ok = True
        status_code = 200
        text = ""

        def json(self):
            return {"status": "completed", "output_text": '{"ok":true}'}

    def fake_post(*_args, **_kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise requests.Timeout("timed out")
        return FakeResponse()

    monkeypatch.setattr(bot.requests, "post", fake_post)
    settings = bot.Settings(
        openai_api_key="openai",
        text_attempts=2,
        text_retry_backoff_seconds=0,
    )

    assert bot.OpenAITextClient(settings).chat("Return JSON") == '{"ok":true}'
    assert calls["count"] == 2


def test_openai_text_does_not_retry_auth_error(monkeypatch):
    calls = {"count": 0}

    class FakeResponse:
        ok = False
        status_code = 401
        text = ""

        def json(self):
            return {"error": {"message": "invalid key"}}

    def fake_post(*_args, **_kwargs):
        calls["count"] += 1
        return FakeResponse()

    monkeypatch.setattr(bot.requests, "post", fake_post)
    settings = bot.Settings(openai_api_key="bad", text_attempts=3, text_retry_backoff_seconds=0)

    with pytest.raises(bot.BotError, match="401: invalid key"):
        bot.OpenAITextClient(settings).chat("Return JSON")
    assert calls["count"] == 1


def test_image_scene_prompt_adds_style_guardrails():
    prompt = bot.image_scene_prompt("A fossil leaf drifts across ancient continents.")

    assert prompt.startswith("A fossil leaf")
    assert "photorealistic" in prompt
    assert "cinematic" in prompt
    # Style suffix chỉ chứa token dương — không được nhồi phủ định vào prompt gửi model ảnh.
    assert "NO " not in prompt
    assert "must describe" not in prompt


def test_caption_chunks_are_short_and_readable():
    chunks = bot.caption_chunks(
        "Twelve thousand years ago, the Sahara was green. "
        "Monsoon rains fed lakes across North Africa."
    )

    assert chunks[0] == "Twelve thousand years ago,"
    assert all(bot.spoken_word_count(chunk) <= 7 for chunk in chunks)


def test_caption_cues_follow_narration_timeline():
    cues = bot.caption_cues_from_text(
        "A tiny bronze machine modeled the sky. Its gears tracked eclipses and calendars.",
        10.0,
    )

    assert cues[0].start == 0
    assert cues[-1].end == 10.0
    assert all(left.end <= right.start for left, right in zip(cues, cues[1:]))


def test_ass_caption_file_preserves_vietnamese_text(tmp_path):
    captions = tmp_path / "captions_vi.ass"
    bot.write_ass_captions([bot.CaptionCue(0, 2.5, "Hóa thạch nối lục địa")], captions)

    content = captions.read_text(encoding="utf-8")
    assert "DejaVu Sans" in content
    assert "Hóa thạch" in content
    assert "0:00:00.00,0:00:02.50" in content


def test_ass_filter_path_escapes_windows_drive():
    escaped = bot.ffmpeg_filter_path(Path("D:/youtube-bot/generated/captions_en.ass"))

    assert "D\\:" in escaped
    assert escaped.endswith("/captions_en.ass")


def test_create_long_form_thumbnail_reuses_scene_visual_without_openai_image_cost(tmp_path, monkeypatch):
    scene = bot.long_form_image_path(tmp_path, 1)
    scene.write_bytes(b"x" * 2048)
    font_file = tmp_path / "fonts" / "DejaVuSans.ttf"
    font_file.parent.mkdir()
    font_file.write_bytes(b"font")
    logo = tmp_path / "overlay-logo.png"
    logo.write_bytes(b"png")
    plan = bot.ShortPlan.from_dict({
        "topic": "Strait of Hormuz", "angle": "Oil risk", "title": "Strait of Hormuz Risk",
        "thumbnail_text": "Strait of Hormuz", "description": "#News", "tags": ["News"],
        "hook": "Hook.", "narration": "Hook.", "closing_line": "Hook.",
        "fact_note": "Note", "source_hints": ["News"],
        "scenes": [{"duration": 4, "visual_prompt": "A tanker"}],
    })
    commands = []

    def fake_run(command):
        commands.append(command)
        Path(command[-1]).write_bytes(b"j" * 4096)

    monkeypatch.setattr(bot, "DATA_DIR", tmp_path)
    monkeypatch.setattr(bot, "require_tools", lambda: None)
    monkeypatch.setattr(bot, "run", fake_run)

    thumbnail = bot.create_long_form_thumbnail(plan, tmp_path / "long.mp4", tmp_path, bot.Settings(overlay_logo=logo))

    assert thumbnail == tmp_path / "thumbnail.jpg"
    assert thumbnail.is_file()
    assert (tmp_path / "thumbnail_headline.txt").read_text(encoding="utf-8") == "STRAIT OF HORMUZ"
    command = commands[0]
    assert command[command.index("-i") + 1] == str(scene)
    assert "drawtext=" in command[command.index("-filter_complex") + 1]
    assert "scale=130:-1" in command[command.index("-filter_complex") + 1]


def test_publish_long_form_uploads_the_prepared_custom_thumbnail(tmp_path, monkeypatch):
    video = tmp_path / "long.mp4"
    thumbnail = tmp_path / "thumbnail.jpg"
    captured = {}
    plan = bot.ShortPlan.from_dict({
        "topic": "Strait of Hormuz", "angle": "Oil risk", "title": "Strait of Hormuz Risk",
        "description": "#News", "tags": ["News"], "hook": "Hook.",
        "narration": "Hook.", "closing_line": "Hook.", "fact_note": "Note",
        "source_hints": ["News"], "scenes": [{"duration": 4, "visual_prompt": "A tanker"}],
    })

    monkeypatch.setattr(bot, "create_long_form_thumbnail", lambda *_args: thumbnail)

    def fake_upload(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return "youtube123"

    monkeypatch.setattr(bot, "upload_to_youtube", fake_upload)

    assert bot.publish_long_form_video(video, plan, bot.Settings(), "public") == {"youtube": "youtube123"}
    assert captured["kwargs"]["thumbnail"] == thumbnail


def test_mux_short_adds_logo_and_drops_corner_video(tmp_path, monkeypatch):
    logo = tmp_path / "overlay-logo.png"
    logo.write_bytes(b"png")
    corner_video = tmp_path / "corner.mp4"
    corner_video.write_bytes(b"video")
    captured = {}
    monkeypatch.setattr(bot, "run", lambda command: captured.setdefault("command", command))
    settings = bot.Settings(
        openai_api_key="openai",
        overlay_logo=logo,
        overlay_video=corner_video,
    )

    bot.mux_video_audio_with_captions(
        tmp_path / "visuals.mp4",
        tmp_path / "narration.mp3",
        tmp_path / "captions.ass",
        tmp_path / "output.mp4",
        60,
        settings,
    )

    command = captured["command"]
    filter_complex = command[command.index("-filter_complex") + 1]
    logo_input = command.index(str(logo))
    assert command[logo_input - 3:logo_input + 1] == ["-loop", "1", "-i", str(logo)]
    assert "scale=220:-1" in filter_complex  # short logo width
    assert "overlay=x=W-w-36:y=72" in filter_complex  # short logo top margin
    assert "format=rgba[logo]" in filter_complex
    # The corner overlay video is gone for Shorts too — logo only.
    assert str(corner_video) not in command
    assert "-stream_loop" not in command
    assert "[3:v]" not in filter_complex
    assert "eof_action=repeat" not in filter_complex


def test_mux_requires_overlay_logo(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, "run", lambda _command: pytest.fail("ffmpeg must not run"))
    settings = bot.Settings(openai_api_key="openai", overlay_logo=tmp_path / "missing.png")

    with pytest.raises(bot.BotError, match="Không tìm thấy logo overlay"):
        bot.mux_video_audio_with_captions(
            tmp_path / "visuals.mp4",
            tmp_path / "narration.mp3",
            tmp_path / "captions.ass",
            tmp_path / "output.mp4",
            60,
            settings,
        )


def test_mux_long_form_uses_logo_without_corner_video(tmp_path, monkeypatch):
    logo = tmp_path / "overlay-logo.png"
    logo.write_bytes(b"png")
    captured = {}
    monkeypatch.setattr(bot, "run", lambda command: captured.setdefault("command", command))
    # A missing overlay video must NOT matter for long-form: it no longer uses one.
    settings = bot.Settings(
        openai_api_key="openai",
        overlay_logo=logo,
        overlay_video=tmp_path / "missing.mp4",
    )

    bot.mux_video_audio_with_captions(
        tmp_path / "visuals.mp4",
        tmp_path / "narration.mp3",
        tmp_path / "captions.ass",
        tmp_path / "output.mp4",
        300,
        settings,
        long_form=True,
    )

    command = captured["command"]
    filter_complex = command[command.index("-filter_complex") + 1]
    logo_input = command.index(str(logo))
    assert command[logo_input - 3:logo_input + 1] == ["-loop", "1", "-i", str(logo)]
    assert "format=rgba[logo]" in filter_complex
    assert "scale=220:-1" in filter_complex
    assert "overlay=x=W-w-36:y=36" in filter_complex
    # The corner overlay video is gone entirely.
    assert str(settings.overlay_video) not in command
    assert "-stream_loop" not in command
    assert "[3:v]" not in filter_complex
    assert "eof_action=repeat" not in filter_complex


def test_openai_image_retries_transient_failure(tmp_path, monkeypatch):
    calls = {"count": 0}
    encoded = base64.b64encode(b"x" * 2048).decode()

    class FakeResponse:
        ok = True
        status_code = 200
        text = ""

        def json(self):
            return {"data": [{"b64_json": encoded}]}

    def fake_post(*_args, **_kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise requests.Timeout("read timed out")
        return FakeResponse()

    monkeypatch.setattr(bot.requests, "post", fake_post)
    settings = bot.Settings(
        openai_api_key="openai",
        image_attempts=2,
        image_retry_backoff_seconds=0,
    )
    destination = tmp_path / "scene_1.jpg"

    bot.OpenAIImageClient(settings).image("a prompt", destination)

    assert calls["count"] == 2
    assert destination.read_bytes() == b"x" * 2048
    assert not (tmp_path / "scene_1.jpg.part").exists()


def test_openai_image_safety_block_does_not_retry(tmp_path, monkeypatch):
    calls = {"count": 0}

    class FakeResponse:
        ok = False
        status_code = 400
        text = '{"error":{"code":"moderation_blocked","message":"Rejected by the safety system"}}'

    def fake_post(*_args, **_kwargs):
        calls["count"] += 1
        return FakeResponse()

    monkeypatch.setattr(bot.requests, "post", fake_post)
    settings = bot.Settings(openai_api_key="openai", image_attempts=3, image_retry_backoff_seconds=0)

    with pytest.raises(bot.ImageGenerationSafetyError, match="safety block"):
        bot.OpenAIImageClient(settings).image("a prompt", tmp_path / "scene_1.jpg")

    assert calls["count"] == 1


def test_openai_image_locks_portrait_to_low_cost_payload(tmp_path, monkeypatch):
    captured = {}
    encoded = base64.b64encode(b"y" * 2048).decode()

    class FakeResponse:
        ok = True
        status_code = 200
        text = ""

        def json(self):
            return {"data": [{"b64_json": encoded}]}

    def fake_post(url, **kwargs):
        captured.update({"url": url, **kwargs})
        return FakeResponse()

    monkeypatch.setattr(bot.requests, "post", fake_post)
    settings = bot.Settings(openai_api_key="openai", image_attempts=1)
    destination = tmp_path / "scene_1.jpg"

    bot.OpenAIImageClient(settings).image("a prompt", destination, width=1080, height=1920)

    assert captured["url"] == "https://api.openai.com/v1/images/generations"
    assert captured["headers"]["Authorization"] == "Bearer openai"
    assert captured["json"]["model"] == "gpt-image-2"
    assert captured["json"]["quality"] == "low"
    assert captured["json"]["size"] == "1024x1536"
    assert destination.read_bytes() == b"y" * 2048


def test_openai_image_uses_low_cost_landscape_size(tmp_path, monkeypatch):
    payloads = []
    encoded = base64.b64encode(b"z" * 2048).decode()

    class FakeResponse:
        ok = True
        status_code = 200
        text = ""

        def json(self):
            return {"data": [{"b64_json": encoded}]}

    def fake_post(*_args, **kwargs):
        payloads.append(kwargs["json"])
        return FakeResponse()

    monkeypatch.setattr(bot.requests, "post", fake_post)
    settings = bot.Settings(openai_api_key="openai", image_attempts=1)
    destination = tmp_path / "scene_1.jpg"

    bot.OpenAIImageClient(settings).image("a prompt", destination, width=1920, height=1080)

    assert payloads[0]["quality"] == "low"
    assert payloads[0]["size"] == "1536x1024"
    assert destination.read_bytes() == b"z" * 2048


def test_openai_image_does_not_retry_permanent_auth_error(tmp_path, monkeypatch):
    calls = {"count": 0}

    class FakeResponse:
        ok = False
        status_code = 401
        text = "invalid API key"

    def fake_post(*_args, **_kwargs):
        calls["count"] += 1
        return FakeResponse()

    monkeypatch.setattr(bot.requests, "post", fake_post)
    settings = bot.Settings(openai_api_key="bad", image_attempts=3, image_retry_backoff_seconds=0)
    destination = tmp_path / "scene_1.jpg"

    with pytest.raises(bot.BotError, match="401"):
        bot.OpenAIImageClient(settings).image("a prompt", destination)

    assert calls["count"] == 1


def test_brave_image_search_downloads_trusted_portrait_and_records_source(tmp_path, monkeypatch):
    image_bytes = b"w" * 20_000

    class SearchResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "results": [{
                    "title": "Historic tower",
                    "url": "https://www.pexels.com/photo/historic-tower-123/",
                    "properties": {
                        "url": "https://images.pexels.com/photos/123/tower.jpg",
                        "width": 1000,
                        "height": 1500,
                    },
                }]
            }

    class ImageResponse:
        headers = {"Content-Type": "image/jpeg"}
        content = image_bytes

        def raise_for_status(self):
            return None

    def fake_get(url, **_kwargs):
        return SearchResponse() if "brave.com" in url else ImageResponse()

    monkeypatch.setattr(bot.requests, "get", fake_get)
    client = bot.BraveImageSearch(bot.Settings(brave_search_api_key="brave"))
    destination = tmp_path / "web.jpg"

    assert client.image("historic tower", destination, width=1080, height=1920)
    assert destination.read_bytes() == image_bytes
    assert client.sources[0]["source_page"].startswith("https://www.pexels.com/")


def test_brave_image_search_spaces_requests_using_rate_limit_headers(tmp_path, monkeypatch):
    clock = {"now": 100.0}
    waits = []

    class SearchResponse:
        status_code = 200
        headers = {"X-RateLimit-Remaining": "0, 999", "X-RateLimit-Reset": "1, 1000"}

        def raise_for_status(self):
            return None

        def json(self):
            return {"results": []}

    monkeypatch.setattr(bot.requests, "get", lambda *_args, **_kwargs: SearchResponse())
    monkeypatch.setattr(bot.time, "monotonic", lambda: clock["now"])

    def fake_sleep(seconds):
        waits.append(seconds)
        clock["now"] += seconds

    monkeypatch.setattr(bot.time, "sleep", fake_sleep)
    client = bot.BraveImageSearch(
        bot.Settings(brave_search_api_key="brave", brave_search_min_interval_seconds=1.1)
    )

    assert not client.image("first query", tmp_path / "first.jpg", width=1920, height=1080)
    assert not client.image("second query", tmp_path / "second.jpg", width=1920, height=1080)

    assert waits == [pytest.approx(1.1)]


def test_brave_image_search_retries_429_then_recovers(tmp_path, monkeypatch):
    clock = {"now": 100.0}
    waits = []
    calls = {"count": 0}

    class SearchResponse:
        def __init__(self, status_code):
            self.status_code = status_code
            self.headers = {"X-RateLimit-Remaining": "0, 999", "X-RateLimit-Reset": "1, 1000"}

        def raise_for_status(self):
            if self.status_code >= 400:
                raise bot.requests.HTTPError(f"HTTP {self.status_code}")

        def json(self):
            return {"results": []}

    def fake_get(*_args, **_kwargs):
        calls["count"] += 1
        return SearchResponse(429 if calls["count"] == 1 else 200)

    def fake_sleep(seconds):
        waits.append(seconds)
        clock["now"] += seconds

    monkeypatch.setattr(bot.requests, "get", fake_get)
    monkeypatch.setattr(bot.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(bot.time, "sleep", fake_sleep)
    client = bot.BraveImageSearch(
        bot.Settings(
            brave_search_api_key="brave",
            brave_search_attempts=3,
            brave_search_min_interval_seconds=1.1,
        )
    )

    assert not client.image("query", tmp_path / "image.jpg", width=1920, height=1080)

    assert calls["count"] == 2
    assert waits == [pytest.approx(1.1)]
    assert not client._disabled_for_run


def test_brave_image_search_stops_after_repeated_429(tmp_path, monkeypatch):
    clock = {"now": 100.0}
    calls = {"count": 0}

    class RateLimitedResponse:
        status_code = 429
        headers = {"Retry-After": "1"}

        def raise_for_status(self):
            raise bot.requests.HTTPError("HTTP 429")

    def fake_get(*_args, **_kwargs):
        calls["count"] += 1
        return RateLimitedResponse()

    monkeypatch.setattr(bot.requests, "get", fake_get)
    monkeypatch.setattr(bot.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(bot.time, "sleep", lambda seconds: clock.__setitem__("now", clock["now"] + seconds))
    client = bot.BraveImageSearch(
        bot.Settings(
            brave_search_api_key="brave",
            brave_search_attempts=3,
            brave_search_min_interval_seconds=1.1,
        )
    )

    assert not client.image("query", tmp_path / "first.jpg", width=1920, height=1080)
    assert not client.image("another query", tmp_path / "second.jpg", width=1920, height=1080)

    assert calls["count"] == 3
    assert client._disabled_for_run


def test_square_subject_search_accepts_portrait_images():
    assert bot.BraveImageSearch._matches_orientation(
        {"width": 800, "height": 1200}, width=1024, height=1024
    )
    assert not bot.BraveImageSearch._matches_orientation(
        {"width": 1200, "height": 800}, width=1080, height=1920
    )


def test_wikimedia_commons_downloads_public_figure_portrait(tmp_path, monkeypatch):
    from PIL import Image

    encoded = BytesIO()
    Image.new("RGB", (800, 1200), (60, 90, 140)).save(encoded, format="JPEG", quality=90)
    image_bytes = encoded.getvalue()

    class SearchResponse:
        headers = {}

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "query": {
                    "pages": [{
                        "title": "File:Lionel Messi portrait.jpg",
                        "canonicalurl": "https://commons.wikimedia.org/wiki/File:Lionel_Messi_portrait.jpg",
                        "imageinfo": [{
                            "thumburl": "https://upload.wikimedia.org/messi-portrait.jpg",
                            "thumbmime": "image/jpeg",
                            "thumbwidth": 800,
                            "thumbheight": 1200,
                            "url": "https://upload.wikimedia.org/messi-original.jpg",
                            "mime": "image/jpeg",
                            "width": 1600,
                            "height": 2400,
                        }],
                    }]
                }
            }

    class ImageResponse:
        headers = {"Content-Type": "image/jpeg"}
        content = image_bytes

        def raise_for_status(self):
            return None

    def fake_get(url, **_kwargs):
        return SearchResponse() if "w/api.php" in url else ImageResponse()

    monkeypatch.setattr(bot.requests, "get", fake_get)
    client = bot.WikimediaCommonsImageSearch(bot.Settings())
    destination = tmp_path / "subject_a.jpg"

    assert client.image("Lionel Messi portrait", destination, width=1024, height=1024)
    assert bot.comparison_subject_image_ready(destination)
    assert client.sources == [{
        "title": "File:Lionel Messi portrait.jpg",
        "source_page": "https://commons.wikimedia.org/wiki/File:Lionel_Messi_portrait.jpg",
        "image_url": "https://upload.wikimedia.org/messi-portrait.jpg",
    }]


def test_visual_provider_uses_wikimedia_before_openai(tmp_path):
    destination = tmp_path / "subject.jpg"
    calls = []
    provider = bot.VisualAssetProvider(bot.Settings())

    class MissingBrave:
        sources = []

        def image(self, *_args):
            calls.append("brave")
            return False

    class FoundWikimedia:
        sources = [{"source_page": "https://commons.wikimedia.org/wiki/File:Subject.jpg"}]

        def image(self, _query, target, _width, _height):
            calls.append("wikimedia")
            target.write_bytes(b"w" * 2048)
            return True

    class ForbiddenOpenAI:
        def image(self, *_args, **_kwargs):
            raise AssertionError("OpenAI must not run when Wikimedia found the subject")

    provider.brave = MissingBrave()
    provider.wikimedia = FoundWikimedia()
    provider.openai = ForbiddenOpenAI()

    assert provider.image("Lionel Messi", "", destination, 1024, 1024, prefer_web=True) == "web"
    assert calls == ["brave", "wikimedia"]
    assert provider.web_sources == FoundWikimedia.sources


def test_missing_comparison_subject_stops_render_instead_of_using_color_panel(tmp_path):
    plan = bot.ShortPlan.from_dict({
        "topic": "Messi vs Ronaldo", "angle": "Differences", "title": "Messi vs Ronaldo",
        "subject_a": "Lionel Messi", "subject_b": "Cristiano Ronaldo",
        "subject_a_image_query": "Lionel Messi portrait",
        "subject_b_image_query": "Cristiano Ronaldo portrait",
        "description": "Description #A #B", "tags": ["A", "B"],
        "hook": "Two legends.", "narration": "Two legends. Close.", "closing_line": "Close.",
        "scenes": [{"duration": 5, "focus": "both", "visual_prompt": "matchup"}],
        "fact_note": "Note", "source_hints": ["Source"],
    })

    class MissingImages:
        s = bot.Settings()

        def __init__(self):
            self.calls = []

        def image(self, search_query, generation_prompt, destination, width, height, prefer_web, web_only=False):
            self.calls.append((search_query, generation_prompt, prefer_web, web_only))
            return "missing"

    client = MissingImages()
    with pytest.raises(bot.BotError, match="Rendering and publishing were stopped"):
        bot.prepare_comparison_subject_images(plan, client, tmp_path)

    assert all(generation_prompt == "" and prefer_web and web_only for _, generation_prompt, prefer_web, web_only in client.calls)
    assert not (tmp_path / "subject_a.jpg").exists()
    assert not (tmp_path / "subject_b.jpg").exists()


def test_distributed_web_images_stay_within_six_visual_budget():
    assert bot.distributed_web_image_indexes(6, 2) == {2, 5}
    assert bot.distributed_web_image_indexes(6, 10) == {1, 2, 3, 4, 5, 6}


def test_create_fallback_scene_image_reuses_previous_image(tmp_path):
    previous = tmp_path / "scene_1.jpg"
    destination = tmp_path / "scene_2.jpg"
    previous.write_bytes(b"p" * 2048)

    bot.create_fallback_scene_image(destination, previous)

    assert destination.read_bytes() == previous.read_bytes()


def test_mentions_vietnam_blocks_related_text():
    assert bot.mentions_vietnam("A headline about Hanoi politics")
    assert not bot.mentions_vietnam("A headline about European energy markets")


def test_split_text_for_tts_chunks_long_script():
    text = " ".join(f"Sentence {index} ends here." for index in range(300))

    chunks = bot.split_text_for_tts(text, max_chars=500)

    assert len(chunks) > 1
    assert all(len(chunk) <= 560 for chunk in chunks)


def test_plan_long_form_builds_educational_explainer(tmp_path):
    narration = (
        "Before farming existed, early humans survived on skill alone. "
        + " ".join("They hunted large animals together and gathered wild plants, roots, and berries to stay alive." for _ in range(77))
        + " Every meal was earned by knowing the land better than any predator."
    )
    plan = {
        "topic": "How prehistoric humans survived",
        "angle": "The daily survival skills of early humans",
        "title": "How Prehistoric Humans Actually Survived",
        "description": "An educational explainer. AI-assisted production. #History #Prehistory",
        "tags": ["History", "Prehistory"],
        "hook": "Before farming existed, early humans survived on skill alone.",
        "narration": narration,
        "closing_line": "Every meal was earned by knowing the land better than any predator.",
        "scenes": [
            {"duration": 75, "visual_prompt": "Stick-figure hunters throwing spears at a stick-figure mammoth near a cave."},
            {"duration": 75, "visual_prompt": "A stick figure gathering berries into a woven basket beside bushes."},
            {"duration": 75, "visual_prompt": "Stick figures making fire with sticks around a small campfire."},
            {"duration": 75, "visual_prompt": "A stick-figure family walking across open grassland toward distant hills."},
        ],
        "fact_note": "Stays qualitative; avoids invented dates or numbers.",
        "source_hints": ["General prehistory knowledge"],
    }

    class FakeClient:
        def __init__(self):
            self.responses = [
                plan,
                {"quality_check": {"clarity_score": 9, "engagement_score": 8}, "plan": plan},
            ]

        def chat(self, _prompt, temperature=0.55, reasoning_effort=None):
            return json.dumps(self.responses.pop(0))

    result = bot.plan_long_form(
        FakeClient(),
        bot.Archive(tmp_path / "shorts.db"),
        "educational explainer channel",
        300,
        4,
        4,
        bot.LONG_FORM_CATEGORIES[1],
    )

    assert result.title == plan["title"]
    assert sum(scene.duration for scene in result.scenes) == 300
    # The assigned category is stamped on the plan even though the model never
    # returns it — the review pass only echoes the fields in the schema.
    assert result.topic_category == bot.LONG_FORM_CATEGORIES[1].slug


def test_plan_long_form_explainer_meets_word_budget(tmp_path):
    plan = {
        "topic": "How money was invented",
        "angle": "From trading goods directly to coins and paper money",
        "title": "How Money Was Invented",
        "description": "An educational explainer. AI-assisted production. #History #Money",
        "tags": ["History", "Money"],
        "hook": "For most of history, people lived with no money at all.",
        "narration": "For most of history, people lived with no money at all. " + "People traded goods directly, then slowly agreed to use coins so everyone could buy and sell more easily. " * 60 + "That simple agreement became the money we all use today.",
        "closing_line": "That simple agreement became the money we all use today.",
        "scenes": [
            {"duration": 75, "visual_prompt": "Two cartoon stick figures trading a chicken for a basket of grain at a market."},
            {"duration": 75, "visual_prompt": "A cartoon stick figure handing over round coins for a loaf of bread."},
            {"duration": 75, "visual_prompt": "A cartoon stick figure holding a piece of paper money and smiling."},
            {"duration": 75, "visual_prompt": "Cartoon stick figures buying and selling at a busy little town market."},
        ],
        "fact_note": "Stays qualitative; avoids invented dates or numbers.",
        "source_hints": ["General history knowledge"],
    }

    class FakeClient:
        def __init__(self):
            self.responses = [
                plan,
                {"quality_check": {"clarity_score": 9, "engagement_score": 8}, "plan": plan},
            ]

        def chat(self, _prompt, temperature=0.55, reasoning_effort=None):
            return json.dumps(self.responses.pop(0))

    result = bot.plan_long_form(
        FakeClient(),
        bot.Archive(tmp_path / "shorts.db"),
        "educational explainer channel",
        300,
        4,
        4,
        bot.LONG_FORM_CATEGORIES[6],  # Origins of the everyday world
    )

    assert bot.spoken_word_count(result.narration) >= bot.long_form_word_bounds(300)[0]


def test_long_form_word_bounds_keep_audio_led_safety_and_pacing_guidance_separate():
    minimum, maximum = bot.long_form_word_bounds(419)
    target_minimum, target_maximum = bot.target_long_form_word_bounds(419)

    assert (minimum, maximum) == (210, 2095)
    # Target pacing must match the measured TTS speed (~2.55 words/sec) so a
    # 300s plan renders near 5 minutes instead of 9.
    assert (target_minimum, target_maximum) == (1006, 1089)


def test_target_long_form_words_render_near_requested_duration():
    measured_tts_words_per_second = 2.57  # 1412 words -> 550.44s in production
    for duration in (240, 300):
        target_minimum, target_maximum = bot.target_long_form_word_bounds(duration)
        rendered_seconds = target_maximum / measured_tts_words_per_second
        assert rendered_seconds <= duration * 1.05
        assert target_minimum / measured_tts_words_per_second >= duration * 0.85


def test_prepare_long_form_images_creates_every_image_in_one_run(tmp_path):
    plan = bot.ShortPlan.from_dict({
        "topic": "Topic",
        "angle": "Angle",
        "title": "Title",
        "description": "Description #A #B",
        "tags": ["A", "B"],
        "hook": "Hook.",
        "narration": "Hook. Body. Close.",
        "closing_line": "Close.",
        "scenes": [
            {"duration": 10, "visual_prompt": "Scene one"},
            {"duration": 10, "visual_prompt": "Scene two"},
            {"duration": 10, "visual_prompt": "Scene three"},
        ],
        "fact_note": "Fact note",
        "source_hints": ["Source"],
    })

    class FakeImages:
        s = bot.Settings(brave_web_images_per_long_form=0)

        def __init__(self):
            self.calls = []

        def image(self, search_query, generation_prompt, destination, width, height, prefer_web, web_only=False):
            self.calls.append((generation_prompt, destination.name, width, height, prefer_web, web_only))
            destination.write_bytes(b"i" * 2048)

    client = FakeImages()

    generated = bot.prepare_long_form_images(plan, client, tmp_path)

    assert generated == 3
    assert len(client.calls) == 3
    assert client.calls[0][2:4] == (1920, 1080)
    assert (tmp_path / "long_scene_01.jpg").is_file()
    assert (tmp_path / "long_scene_03.jpg").is_file()


def test_prepare_long_form_images_fails_when_placeholder_fallback_disabled(tmp_path):
    plan = bot.ShortPlan.from_dict({
        "topic": "Topic",
        "angle": "Angle",
        "title": "Title",
        "description": "Description #A #B",
        "tags": ["A", "B"],
        "hook": "Hook.",
        "narration": "Hook. Body. Close.",
        "closing_line": "Close.",
        "scenes": [{"duration": 10, "visual_prompt": "Scene one"}],
        "fact_note": "Fact note",
        "source_hints": ["Source"],
    })

    class FakeImages:
        s = bot.Settings(brave_web_images_per_long_form=0, allow_image_fallback_placeholder=False)

        def image(self, *_args, **_kwargs):
            raise bot.ImageGenerationTransientError("quota exhausted")

    with pytest.raises(bot.BotError, match="placeholder fallback is disabled"):
        bot.prepare_long_form_images(plan, FakeImages(), tmp_path)


def test_prepare_long_form_images_falls_back_after_safety_rejection(tmp_path, monkeypatch):
    plan = bot.ShortPlan.from_dict({
        "topic": "Topic", "angle": "Angle", "title": "Title",
        "description": "Description #A #B", "tags": ["A", "B"],
        "hook": "Hook.", "narration": "Hook. Body. Close.", "closing_line": "Close.",
        "scenes": [{"duration": 10, "visual_prompt": "Scene one"}],
        "fact_note": "Fact note", "source_hints": ["Source"],
    })

    class FakeImages:
        s = bot.Settings(brave_web_images_per_long_form=0)

        def image(self, *_args, **_kwargs):
            raise bot.ImageGenerationSafetyError("provider blocked the image")

    monkeypatch.setattr(
        bot,
        "create_fallback_scene_image",
        lambda destination, *_args, **_kwargs: destination.write_bytes(b"i" * 2048),
    )

    assert bot.prepare_long_form_images(plan, FakeImages(), tmp_path) == 1
    assert bot.long_form_image_path(tmp_path, 1).is_file()


def test_scene_parser_keeps_real_subject_search_query():
    plan = bot.ShortPlan.from_dict({
        "topic": "Topic", "angle": "Angle", "title": "Title",
        "description": "Description #A #B", "tags": ["A", "B"],
        "hook": "Hook.", "narration": "Hook. Body. Close.", "closing_line": "Close.",
        "scenes": [
            {"duration": 10, "visual_prompt": "A government building in Kyiv.", "search_query": " Zelenskyy press conference "},
            {"duration": 10, "visual_prompt": "A symbolic map scene."},
        ],
        "fact_note": "Fact note", "source_hints": ["Source"],
    })

    assert plan.scenes[0].search_query == "Zelenskyy press conference"
    assert plan.scenes[1].search_query == ""


def test_plan_parser_keeps_comparison_fields_and_scene_focus():
    plan = bot.ShortPlan.from_dict({
        "topic": "Coca-Cola vs Pepsi", "angle": "The real differences", "title": "Coca-Cola vs Pepsi",
        "subject_a": "Coca-Cola", "subject_b": "Pepsi",
        "subject_a_image_query": "Coca-Cola logo can", "subject_b_image_query": "Pepsi logo can",
        "description": "Description #A #B", "tags": ["A", "B"],
        "hook": "Two colas, one endless rivalry.", "narration": "Two colas, one endless rivalry. Close.",
        "closing_line": "Close.",
        "scenes": [
            {"duration": 5, "focus": "both", "visual_prompt": "the matchup"},
            {"duration": 5, "focus": "A", "visual_prompt": "coke point"},
            {"duration": 5, "focus": "b", "visual_prompt": "pepsi point"},
            {"duration": 5, "focus": "sideways", "visual_prompt": "invalid focus falls back"},
        ],
        "fact_note": "Note", "source_hints": ["Source"],
    })

    assert bot.is_comparison_plan(plan)
    assert plan.subject_a == "Coca-Cola" and plan.subject_b == "Pepsi"
    assert plan.subject_a_image_query == "Coca-Cola logo can"
    assert [s.focus for s in plan.scenes] == ["both", "a", "b", ""]  # invalid focus dropped to ""


def test_ensure_comparison_fields_derives_subjects_and_assigns_focus():
    plan = bot.ShortPlan.from_dict({
        "topic": "Mercedes-Benz vs BMW: The Rivalry", "angle": "German car giants", "title": "Mercedes-Benz vs BMW",
        "description": "Description #A #B", "tags": ["A", "B"],
        "hook": "Two German legends.", "narration": "Two German legends. Close.", "closing_line": "Close.",
        "scenes": [{"duration": 4, "visual_prompt": f"point {i}"} for i in range(6)],
        "fact_note": "Note", "source_hints": ["Source"],
    })

    bot.ensure_comparison_fields(plan)

    # Hyphenated names survive the "A vs B" split.
    assert plan.subject_a == "Mercedes-Benz" and plan.subject_b == "BMW"
    # Missing image queries fall back to the subject names.
    assert plan.subject_a_image_query == "Mercedes-Benz"
    focuses = [s.focus for s in plan.scenes]
    assert focuses[0] == "both" and focuses[-1] == "both"
    assert focuses[1:-1] == ["a", "b", "a", "b"]  # middle beats alternate


def test_comparison_voiceovers_override_wrong_pointer_focus_from_spoken_subject():
    plan = bot.ShortPlan.from_dict({
        "topic": "Messi vs Ronaldo", "angle": "Playing styles", "title": "Messi vs Ronaldo",
        "subject_a": "Lionel Messi", "subject_b": "Cristiano Ronaldo",
        "description": "Description #A #B", "tags": ["A", "B"],
        "hook": "Messi and Ronaldo became football icons.",
        "narration": (
            "Messi and Ronaldo became football icons. "
            "Lionel Messi creates through close control. "
            "Cristiano Ronaldo attacks space with explosive movement. "
            "Lionel Messi often drops deeper to build play. "
            "Cristiano Ronaldo usually threatens closer to goal. "
            "Messi and Ronaldo reached greatness differently."
        ),
        "closing_line": "Messi and Ronaldo reached greatness differently.",
        "scenes": [
            {"duration": 5, "focus": "both", "voiceover": "Messi and Ronaldo became football icons.", "visual_prompt": "Hook"},
            {"duration": 5, "focus": "b", "voiceover": "Lionel Messi creates through close control.", "visual_prompt": "Messi"},
            {"duration": 5, "focus": "a", "voiceover": "Cristiano Ronaldo attacks space with explosive movement.", "visual_prompt": "Ronaldo"},
            {"duration": 5, "focus": "b", "voiceover": "Lionel Messi often drops deeper to build play.", "visual_prompt": "Messi"},
            {"duration": 5, "focus": "a", "voiceover": "Cristiano Ronaldo usually threatens closer to goal.", "visual_prompt": "Ronaldo"},
            {"duration": 5, "focus": "both", "voiceover": "Messi and Ronaldo reached greatness differently.", "visual_prompt": "Close"},
        ],
        "fact_note": "Note", "source_hints": ["Source"],
    })

    bot.synchronize_comparison_scene_voiceovers(plan)

    assert [scene.focus for scene in plan.scenes] == ["both", "a", "b", "a", "b", "both"]
    assert plan.narration == " ".join(scene.voiceover for scene in plan.scenes)


def test_comparison_narration_measures_each_scene_for_exact_pointer_timing(tmp_path, monkeypatch):
    voiceovers = [
        "Alpha and Beta begin.",
        "Alpha handles point one.",
        "Beta handles point two.",
        "Alpha handles point three.",
        "Beta handles point four.",
        "Alpha and Beta finish.",
    ]
    plan = bot.ShortPlan.from_dict({
        "topic": "Alpha vs Beta", "angle": "Differences", "title": "Alpha vs Beta",
        "subject_a": "Alpha", "subject_b": "Beta",
        "description": "Description #A #B", "tags": ["A", "B"],
        "hook": voiceovers[0], "narration": " ".join(voiceovers), "closing_line": voiceovers[-1],
        "scenes": [
            {"duration": 5, "focus": focus, "voiceover": text, "visual_prompt": text}
            for focus, text in zip(["both", "a", "b", "a", "b", "both"], voiceovers)
        ],
        "fact_note": "Note", "source_hints": ["Source"],
    })
    calls = []
    part_seconds = [2.0, 4.0, 3.0, 5.0, 6.0, 2.0]

    class FakeTTS:
        def speech(self, text, destination):
            calls.append(text)
            destination.write_bytes(b"a" * 2048)

    def fake_duration(path, _label):
        path = Path(path)
        if path.name == "narration.mp3":
            return sum(part_seconds)
        index = int(path.stem.rsplit("_", 1)[1]) - 1
        return part_seconds[index]

    def fake_run(command, timeout_seconds=None):
        Path(command[-1]).write_bytes(b"m" * 2048)

    monkeypatch.setattr(bot, "require_tools", lambda: None)
    monkeypatch.setattr(bot, "measured_narration_duration", fake_duration)
    monkeypatch.setattr(bot, "run", fake_run)

    narration, seconds = bot.prepare_short_english_narration(plan, FakeTTS(), tmp_path)

    assert narration == tmp_path / "narration.mp3"
    assert seconds == 22.0
    assert calls == voiceovers
    assert [scene.duration for scene in plan.scenes] == part_seconds


def test_build_comparison_scene_image_writes_valid_frames(tmp_path):
    from PIL import Image

    subject_a = tmp_path / "subject_a.jpg"
    subject_b = tmp_path / "subject_b.jpg"
    Image.new("RGB", (400, 400), (200, 30, 30)).save(subject_a)
    Image.new("RGB", (400, 400), (20, 90, 200)).save(subject_b)

    for focus in ("a", "b", "both"):
        out = tmp_path / f"scene_{focus}.jpg"
        bot.build_comparison_scene_image(
            subject_a, subject_b, "Coca-Cola", "Pepsi", focus,
            bot.ROOT / "assets" / "pointer_side.png",
            bot.ROOT / "assets" / "pointer_both.png",
            out,
        )
        assert out.is_file() and out.stat().st_size >= 1024
        assert Image.open(out).size == (1080, 1920)

    # A missing subject must fail closed so a blank/color panel cannot be published.
    out = tmp_path / "scene_fallback.jpg"
    with pytest.raises(bot.BotError, match="Refusing to render"):
        bot.build_comparison_scene_image(
            tmp_path / "missing.jpg", subject_b, "Nike", "Adidas", "a",
            bot.ROOT / "assets" / "pointer_side.png",
            bot.ROOT / "assets" / "pointer_both.png",
            out,
        )


def test_long_form_images_prefer_real_web_photos_for_named_subjects(tmp_path):
    plan = bot.ShortPlan.from_dict({
        "topic": "Topic", "angle": "Angle", "title": "Title",
        "description": "Description #A #B", "tags": ["A", "B"],
        "hook": "Hook.", "narration": "Hook. Body. Close.", "closing_line": "Close.",
        "scenes": [
            {"duration": 10, "visual_prompt": "A presidential office building.", "search_query": "Zelenskyy press conference"},
            {"duration": 10, "visual_prompt": "A defence ministry facade.", "search_query": "Ukraine defence ministry building"},
            {"duration": 10, "visual_prompt": "A symbolic map scene."},
        ],
        "fact_note": "Fact note", "source_hints": ["Source"],
    })

    class FakeImages:
        s = bot.Settings(brave_web_images_per_long_form=0)

        def __init__(self):
            self.web_queries = []
            self.openai_scenes = []

        def image(self, search_query, generation_prompt, destination, width, height, prefer_web, web_only=False):
            if prefer_web:
                self.web_queries.append(search_query)
                if "Zelenskyy" in search_query:
                    destination.write_bytes(b"w" * 2048)
                    return "web"
                return "missing"
            self.openai_scenes.append(destination.name)
            destination.write_bytes(b"o" * 2048)
            return "openai"

    client = FakeImages()
    prepared = bot.prepare_long_form_images(plan, client, tmp_path)

    assert prepared == 3
    assert client.web_queries == ["Zelenskyy press conference", "Ukraine defence ministry building"]
    assert client.openai_scenes == ["long_scene_02.jpg", "long_scene_03.jpg"]
    assert all((tmp_path / f"long_scene_{index:02d}.jpg").is_file() for index in range(1, 4))


def test_long_form_uses_five_brave_slots_and_only_ten_openai_slots(tmp_path):
    plan = bot.ShortPlan.from_dict({
        "topic": "Topic", "angle": "Angle", "title": "Title",
        "description": "Description #A #B", "tags": ["A", "B"],
        "hook": "Hook.", "narration": "Hook. Body. Close.", "closing_line": "Close.",
        "scenes": [
            {"duration": 20, "visual_prompt": f"Scene {index}"}
            for index in range(1, 16)
        ],
        "fact_note": "Fact note", "source_hints": ["Source"],
    })

    class FakeImages:
        s = bot.Settings(brave_web_images_per_long_form=5, long_form_openai_images=10)

        def __init__(self):
            self.openai_calls = 0
            self.brave_calls = 0

        def image(self, search_query, generation_prompt, destination, width, height, prefer_web, web_only=False):
            if prefer_web:
                self.brave_calls += 1
                return "missing"
            self.openai_calls += 1
            destination.write_bytes(b"o" * 2048)
            return "openai"

    client = FakeImages()
    prepared = bot.prepare_long_form_images(plan, client, tmp_path)

    assert prepared == 15
    assert client.brave_calls == 5
    assert client.openai_calls == 10
    assert all((tmp_path / f"long_scene_{index:02d}.jpg").is_file() for index in range(1, 16))


def test_long_form_tts_preflight_failure_spends_no_image_credits(tmp_path, monkeypatch):
    plan = bot.ShortPlan.from_dict({
        "topic": "Topic", "angle": "Angle", "title": "Title",
        "description": "Description #A #B", "tags": ["A", "B"],
        "hook": "Hook.", "narration": "Hook. Body. Close.", "closing_line": "Close.",
        "scenes": [{"duration": 60, "visual_prompt": "Scene one"}],
        "fact_note": "Fact note", "source_hints": ["Source"],
    })
    output_dir = tmp_path / "long-job"
    output_dir.mkdir()
    events = []

    class FakeArchive:
        def mark(self, *_args):
            pass

        def resumable_long_form_jobs(self):
            return []

    class FakeImages:
        web_sources = []

    monkeypatch.setattr(bot, "long_form_is_due", lambda *_args: (True, None))
    monkeypatch.setattr(bot, "create_long_form_job", lambda *_args: (plan, output_dir, 60, 1))

    def fail_audio(*_args):
        events.append("audio")
        raise bot.BotError("audio duration mismatch")

    def unexpected_images(*_args):
        events.append("images")
        raise AssertionError("images must not be requested after a failed audio preflight")

    monkeypatch.setattr(bot, "prepare_long_form_narration", fail_audio)
    monkeypatch.setattr(bot, "prepare_long_form_images", unexpected_images)

    with pytest.raises(bot.BotError, match="audio duration mismatch"):
        bot.run_long_form_flow(
            publish=False,
            privacy="private",
            theme="world news",
            settings=bot.Settings(),
            archive=FakeArchive(),
            llm=object(),
            images=FakeImages(),
            tts=object(),
        )

    assert events == ["audio"]


def test_archive_lists_interrupted_long_form_jobs(tmp_path):
    archive = bot.Archive(tmp_path / "shorts.db")
    plan = bot.ShortPlan.from_dict({
        "topic": "Strait of Hormuz", "angle": "Oil risk", "title": "Strait of Hormuz Risk",
        "description": "#News", "tags": ["News"], "hook": "Hook.", "narration": "Hook.",
        "closing_line": "Hook.", "fact_note": "Note", "source_hints": ["News"],
        "scenes": [{"duration": 60, "visual_prompt": "A tanker"}],
    })
    record_id = archive.reserve(plan, tmp_path / "long-interrupted")

    jobs = archive.resumable_long_form_jobs()

    assert jobs[0]["id"] == record_id
    assert jobs[0]["status"] == "rendering"


def test_load_resumable_long_form_job_reuses_saved_audio_and_images(tmp_path, monkeypatch):
    output_dir = tmp_path / "long-interrupted"
    output_dir.mkdir()
    plan = bot.ShortPlan.from_dict({
        "topic": "Strait of Hormuz", "angle": "Oil risk", "title": "Strait of Hormuz Risk",
        "description": "#News", "tags": ["News"], "hook": "Hook.", "narration": "Hook.",
        "closing_line": "Hook.", "fact_note": "Note", "source_hints": ["News"],
        "scenes": [
            {"duration": 30, "visual_prompt": "A tanker"},
            {"duration": 30, "visual_prompt": "A strait"},
        ],
    })
    (output_dir / "plan.json").write_text(json.dumps(plan.to_dict()), encoding="utf-8")
    (output_dir / "long_narration.mp3").write_bytes(b"a" * 2048)
    for index in (1, 2):
        bot.long_form_image_path(output_dir, index).write_bytes(b"i" * 2048)
    archive = bot.Archive(tmp_path / "shorts.db")
    record_id = archive.reserve(plan, output_dir)
    monkeypatch.setattr(bot, "media_duration", lambda _path: 60.0)

    resumed = bot.load_resumable_long_form_job(archive)

    assert resumed is not None
    resumed_plan, resumed_dir, duration, resumed_id = resumed
    assert resumed_plan.title == plan.title
    assert resumed_dir == output_dir
    assert duration == 60.0
    assert resumed_id == record_id


def test_run_long_form_resumes_saved_job_without_tts_or_image_calls(tmp_path, monkeypatch):
    output_dir = tmp_path / "long-interrupted"
    output_dir.mkdir()
    plan = bot.ShortPlan.from_dict({
        "topic": "Strait of Hormuz", "angle": "Oil risk", "title": "Strait of Hormuz Risk",
        "description": "#News", "tags": ["News"], "hook": "Hook.", "narration": "Hook.",
        "closing_line": "Hook.", "fact_note": "Note", "source_hints": ["News"],
        "scenes": [{"duration": 60, "visual_prompt": "A tanker"}],
    })
    (output_dir / "plan.json").write_text(json.dumps(plan.to_dict()), encoding="utf-8")
    (output_dir / "long_narration.mp3").write_bytes(b"a" * 2048)
    bot.long_form_image_path(output_dir, 1).write_bytes(b"i" * 2048)
    archive = bot.Archive(tmp_path / "shorts.db")
    record_id = archive.reserve(plan, output_dir)
    events = []

    monkeypatch.setattr(bot, "media_duration", lambda _path: 60.0)
    monkeypatch.setattr(bot, "prepare_long_form_narration", lambda *_args: pytest.fail("TTS must be reused"))
    monkeypatch.setattr(bot, "prepare_long_form_images", lambda *_args: pytest.fail("images must be reused"))
    monkeypatch.setattr(bot, "create_long_form_job", lambda *_args: pytest.fail("planner must not run"))
    monkeypatch.setattr(bot, "long_form_video_ready", lambda *_args: False)

    def fake_render(_plan, current_dir, *_args):
        events.append("render")
        video = current_dir / "long.mp4"
        video.write_bytes(b"v" * 2048)
        return video

    monkeypatch.setattr(bot, "render_long_form_from_assets", fake_render)

    assert bot.run_long_form_flow(
        publish=False,
        privacy="private",
        theme="world news",
        settings=bot.Settings(),
        archive=archive,
        llm=object(),
        images=type("Images", (), {"web_sources": []})(),
        tts=object(),
    ) == 0
    assert events == ["render"]
    assert archive.conn.execute("SELECT status FROM shorts WHERE id = ?", (record_id,)).fetchone()["status"] == "rendered"


def test_long_form_render_uses_lighter_scene_scale_and_timeout(tmp_path, monkeypatch):
    plan = bot.ShortPlan.from_dict({
        "topic": "Strait of Hormuz", "angle": "Oil risk", "title": "Strait of Hormuz Risk",
        "description": "#News", "tags": ["News"], "hook": "Hook.", "narration": "Hook.",
        "closing_line": "Hook.", "fact_note": "Note", "source_hints": ["News"],
        "scenes": [{"duration": 10, "visual_prompt": "A tanker"}],
    })
    bot.long_form_image_path(tmp_path, 1).write_bytes(b"i" * 2048)
    logo = tmp_path / "overlay-logo.png"
    logo.write_bytes(b"p" * 2048)
    corner_video = tmp_path / "corner.mp4"
    corner_video.write_bytes(b"v" * 2048)
    commands = []

    def fake_run(command, timeout_seconds=None):
        commands.append((command, timeout_seconds))
        Path(command[-1]).write_bytes(b"v" * 2048)

    monkeypatch.setattr(bot, "require_tools", lambda: None)
    monkeypatch.setattr(bot, "long_form_clip_ready", lambda *_args: False)
    monkeypatch.setattr(bot, "media_duration", lambda _path: 10.0)
    monkeypatch.setattr(bot, "run", fake_run)

    bot.render_long_form_from_assets(
        plan,
        tmp_path,
        10,
        bot.Settings(overlay_logo=logo, overlay_video=corner_video),
        tmp_path / "long_narration.mp3",
        10,
    )

    scene_command, scene_timeout = commands[0]
    assert f"scale={bot.LONG_FORM_INTERMEDIATE_WIDTH}:-1" in scene_command[scene_command.index("-vf") + 1]
    assert scene_timeout == bot.LONG_FORM_SCENE_RENDER_TIMEOUT_SECONDS
    assert commands[-1][1] == bot.LONG_FORM_FINAL_RENDER_TIMEOUT_SECONDS


def test_long_form_render_replaces_an_unrenderable_scene_with_previous_visual(tmp_path, monkeypatch):
    plan = bot.ShortPlan.from_dict({
        "topic": "Strait of Hormuz", "angle": "Oil risk", "title": "Strait of Hormuz Risk",
        "description": "#News", "tags": ["News"], "hook": "Hook.", "narration": "Hook.",
        "closing_line": "Hook.", "fact_note": "Note", "source_hints": ["News"],
        "scenes": [
            {"duration": 10, "visual_prompt": "A tanker"},
            {"duration": 10, "visual_prompt": "A strait"},
        ],
    })
    first_image = bot.long_form_image_path(tmp_path, 1)
    second_image = bot.long_form_image_path(tmp_path, 2)
    first_image.write_bytes(b"first" * 512)
    second_image.write_bytes(b"second" * 512)
    logo = tmp_path / "overlay-logo.png"
    logo.write_bytes(b"p" * 2048)
    corner_video = tmp_path / "corner.mp4"
    corner_video.write_bytes(b"v" * 2048)
    failed_once = False

    def fake_run(command, timeout_seconds=None):
        nonlocal failed_once
        destination = Path(command[-1])
        if destination.name == "long_scene_02.mp4" and not failed_once:
            failed_once = True
            raise bot.BotError("FFmpeg timed out after 120s")
        destination.write_bytes(b"v" * 2048)

    monkeypatch.setattr(bot, "require_tools", lambda: None)
    monkeypatch.setattr(bot, "long_form_clip_ready", lambda *_args: False)
    monkeypatch.setattr(bot, "media_duration", lambda _path: 10.0)
    monkeypatch.setattr(bot, "run", fake_run)

    bot.render_long_form_from_assets(
        plan,
        tmp_path,
        20,
        bot.Settings(overlay_logo=logo, overlay_video=corner_video),
        tmp_path / "long_narration.mp3",
        20,
    )

    assert failed_once
    assert second_image.read_bytes() == first_image.read_bytes()


def test_short_form_render_uses_lighter_scene_scale_and_timeout(tmp_path, monkeypatch):
    plan = bot.ShortPlan.from_dict({
        "topic": "Statue of Liberty", "angle": "Construction", "title": "How the Statue Was Built",
        "description": "#History", "tags": ["History"], "hook": "Hook.", "narration": "Hook.",
        "closing_line": "Hook.", "fact_note": "Note", "source_hints": ["History"],
        "scenes": [{"duration": 10, "visual_prompt": "The Statue of Liberty"}],
    })
    logo = tmp_path / "overlay-logo.png"
    logo.write_bytes(b"p" * 2048)
    corner_video = tmp_path / "corner.mp4"
    corner_video.write_bytes(b"v" * 2048)
    commands = []

    class FakeImages:
        s = bot.Settings(overlay_logo=logo, overlay_video=corner_video)

        def image(self, *_args, **kwargs):
            kwargs["destination"].write_bytes(b"i" * 2048)

    def fake_run(command, timeout_seconds=None):
        commands.append((command, timeout_seconds))
        Path(command[-1]).write_bytes(b"v" * 2048)

    monkeypatch.setattr(bot, "require_tools", lambda: None)
    monkeypatch.setattr(bot, "short_form_clip_ready", lambda *_args: False)
    monkeypatch.setattr(bot, "media_duration", lambda _path: 10.0)
    monkeypatch.setattr(bot, "run", fake_run)

    bot.render(plan, FakeImages(), tmp_path, 10, tmp_path / "narration.mp3", 10)

    scene_command, scene_timeout = commands[0]
    assert f"scale={bot.SHORT_FORM_INTERMEDIATE_WIDTH}:-1" in scene_command[scene_command.index("-vf") + 1]
    assert scene_timeout == bot.SHORT_FORM_SCENE_RENDER_TIMEOUT_SECONDS
    assert commands[-1][1] == bot.SHORT_FORM_FINAL_RENDER_TIMEOUT_SECONDS


def test_short_form_render_replaces_an_unrenderable_scene_with_previous_visual(tmp_path, monkeypatch):
    plan = bot.ShortPlan.from_dict({
        "topic": "Statue of Liberty", "angle": "Construction", "title": "How the Statue Was Built",
        "description": "#History", "tags": ["History"], "hook": "Hook.", "narration": "Hook.",
        "closing_line": "Hook.", "fact_note": "Note", "source_hints": ["History"],
        "scenes": [
            {"duration": 10, "visual_prompt": "The Statue of Liberty"},
            {"duration": 10, "visual_prompt": "The statue under construction"},
        ],
    })
    first_image = tmp_path / "scene_1.jpg"
    second_image = tmp_path / "scene_2.jpg"
    first_image.write_bytes(b"first" * 512)
    second_image.write_bytes(b"second" * 512)
    logo = tmp_path / "overlay-logo.png"
    logo.write_bytes(b"p" * 2048)
    corner_video = tmp_path / "corner.mp4"
    corner_video.write_bytes(b"v" * 2048)
    failed_once = False

    class FakeImages:
        s = bot.Settings(overlay_logo=logo, overlay_video=corner_video)

        def image(self, *_args, **_kwargs):
            pytest.fail("Existing images must be reused")

    def fake_run(command, timeout_seconds=None):
        nonlocal failed_once
        destination = Path(command[-1])
        if destination.name == "scene_2.mp4" and not failed_once:
            failed_once = True
            raise bot.BotError("FFmpeg timed out after 120s")
        destination.write_bytes(b"v" * 2048)

    monkeypatch.setattr(bot, "require_tools", lambda: None)
    monkeypatch.setattr(bot, "short_form_clip_ready", lambda *_args: False)
    monkeypatch.setattr(bot, "media_duration", lambda _path: 10.0)
    monkeypatch.setattr(bot, "run", fake_run)

    bot.render(plan, FakeImages(), tmp_path, 20, tmp_path / "narration.mp3", 20)

    assert failed_once
    assert second_image.read_bytes() == first_image.read_bytes()


def test_load_resumable_short_form_job_reuses_saved_narration_and_assets(tmp_path, monkeypatch):
    output_dir = tmp_path / "short-interrupted"
    output_dir.mkdir()
    plan = bot.ShortPlan.from_dict({
        "topic": "Statue of Liberty", "angle": "Construction", "title": "How the Statue Was Built",
        "description": "#History", "tags": ["History"], "hook": "Hook.", "narration": "Hook.",
        "closing_line": "Hook.", "fact_note": "Note", "source_hints": ["History"],
        "scenes": [{"duration": 10, "visual_prompt": "The Statue of Liberty"}],
    })
    (output_dir / "plan.json").write_text(json.dumps(plan.to_dict()), encoding="utf-8")
    (output_dir / "narration.mp3").write_bytes(b"a" * 2048)
    (output_dir / "scene_1.jpg").write_bytes(b"i" * 2048)
    (output_dir / "scene_1.mp4").write_bytes(b"v" * 2048)
    archive = bot.Archive(tmp_path / "shorts.db")
    record_id = archive.reserve(plan, output_dir)
    monkeypatch.setattr(bot, "media_duration", lambda _path: 10.0)

    resumed = bot.load_resumable_short_form_job(archive)

    assert resumed is not None
    resumed_plan, resumed_dir, duration, resumed_id = resumed
    assert resumed_plan.title == plan.title
    assert resumed_dir == output_dir
    assert duration == 10.0
    assert resumed_id == record_id


def test_audio_led_timeline_rescales_all_scenes_exactly():
    plan = bot.ShortPlan.from_dict({
        "topic": "Topic", "angle": "Angle", "title": "Title",
        "description": "Description #A #B", "tags": ["A", "B"],
        "hook": "Hook.", "narration": "Hook. Body. Close.", "closing_line": "Close.",
        "scenes": [
            {"duration": 10, "visual_prompt": "Scene one"},
            {"duration": 20, "visual_prompt": "Scene two"},
            {"duration": 30, "visual_prompt": "Scene three"},
        ],
        "fact_note": "Fact note", "source_hints": ["Source"],
    })

    effective_duration = bot.rescale_scene_durations(plan, 47.321, "Short English")

    assert effective_duration == 47.321
    assert sum(scene.duration for scene in plan.scenes) == pytest.approx(47.321)
    assert plan.scenes[1].duration == pytest.approx(plan.scenes[0].duration * 2, abs=0.002)


def test_main_bot_run_mode_env_forces_long_form(monkeypatch):
    called = {}

    monkeypatch.setenv("BOT_RUN_MODE", "long-form")
    monkeypatch.setenv("OPENAI_API_KEY", "openai")
    monkeypatch.delenv("LONG_FORM_FORCE_NEW", raising=False)
    monkeypatch.setattr(bot, "materialize_railway_credentials", lambda: None)
    monkeypatch.setattr(bot, "ensure_dejavu_font", lambda: None)
    class FakeArchive:
        def get_kv(self, _key):
            return None

    monkeypatch.setattr(bot, "Archive", lambda: FakeArchive())
    monkeypatch.setattr(bot, "VisualAssetProvider", lambda settings: object())
    monkeypatch.setattr(bot, "OpenAITextClient", lambda settings: object())
    monkeypatch.setattr(bot, "GoogleCloudTTS", lambda settings: object())

    def fake_run_long_form_flow(**kwargs):
        called.update(kwargs)
        return 0

    monkeypatch.setattr(bot, "run_long_form_flow", fake_run_long_form_flow)
    monkeypatch.setattr(sys, "argv", ["youtube_shorts_bot.py", "--publish", "--scheduled"])

    assert bot.main() == 0
    assert called["publish"] is True
    assert called["force_new"] is False


def test_main_long_form_force_new_env_bypasses_schedule_gate(monkeypatch):
    called = {}

    monkeypatch.setenv("BOT_RUN_MODE", "long-form")
    monkeypatch.setenv("OPENAI_API_KEY", "openai")
    monkeypatch.setenv("LONG_FORM_FORCE_NEW", "true")
    monkeypatch.setattr(bot, "materialize_railway_credentials", lambda: None)
    monkeypatch.setattr(bot, "ensure_dejavu_font", lambda: None)

    class FakeArchive:
        def get_kv(self, _key):
            return None

    monkeypatch.setattr(bot, "Archive", lambda: FakeArchive())
    monkeypatch.setattr(bot, "VisualAssetProvider", lambda settings: object())
    monkeypatch.setattr(bot, "OpenAITextClient", lambda settings: object())
    monkeypatch.setattr(bot, "GoogleCloudTTS", lambda settings: object())

    def fake_run_long_form_flow(**kwargs):
        called.update(kwargs)
        return 0

    monkeypatch.setattr(bot, "run_long_form_flow", fake_run_long_form_flow)
    monkeypatch.setattr(sys, "argv", ["youtube_shorts_bot.py", "--publish", "--scheduled"])

    assert bot.main() == 0
    assert called["force_new"] is True


def test_google_tts_language_code_supports_chirp_3_hd_leda():
    assert bot.GoogleCloudTTS.language_code_for_voice("en-US-Chirp3-HD-Leda") == "en-US"


def test_google_short_vietnamese_tts_uses_chirp_aoede(tmp_path, monkeypatch):
    captured = {}

    def fake_speech(_self, text, destination, voice=None, speaking_rate=None):
        captured.update(text=text, voice=voice, speaking_rate=speaking_rate)
        destination.write_bytes(b"mp3-bytes")

    monkeypatch.setattr(bot.GoogleCloudTTS, "speech", fake_speech)
    destination = tmp_path / "narration_vi.mp3"
    settings = bot.Settings()

    bot.GoogleCloudVietnameseTTS(settings).speech("Xin chào, đây là đoạn đọc tiếng Việt.", destination)

    assert destination.read_bytes() == b"mp3-bytes"
    assert captured["voice"] == "vi-VN-Chirp3-HD-Aoede"
    assert captured["speaking_rate"] == 1.0


def test_facebook_page_upload_posts_video(tmp_path, monkeypatch):
    video = tmp_path / "short_vi.mp4"
    video.write_bytes(b"video")
    calls = {}

    class FakeResponse:
        ok = True
        text = '{"id":"fb123"}'

        def json(self):
            return {"id": "fb123"}

    def fake_post(url, data=None, files=None, timeout=None):
        calls["url"] = url
        calls["data"] = data
        calls["files"] = files
        calls["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(bot.requests, "post", fake_post)
    settings = bot.Settings(facebook_page_id="page1", facebook_page_access_token="token")
    social = bot.SocialPlan(title="Tieu de", description="Mo ta", tags=["tag"], narration="Loi doc")

    assert bot.upload_to_facebook_page(video, social, settings) == "fb123"
    assert calls["url"].endswith("/page1/videos")
    assert calls["data"]["description"] == "Mo ta"


def test_facebook_upload_retries_with_user_token_when_page_token_expired(tmp_path, monkeypatch):
    video = tmp_path / "short_vi.mp4"
    video.write_bytes(b"video")
    posts = []
    gets = []

    class FakeResponse:
        def __init__(self, payload, status_code=200):
            self.payload = payload
            self.status_code = status_code
            self.ok = status_code < 400
            self.text = json.dumps(payload)

        def json(self):
            return self.payload

    def fake_post(url, data=None, files=None, timeout=None):
        posts.append({"url": url, "data": data, "files": files, "timeout": timeout})
        if len(posts) == 1:
            return FakeResponse(
                {
                    "error": {
                        "message": "Error validating access token: Session has expired.",
                        "type": "OAuthException",
                        "code": 190,
                        "error_subcode": 463,
                    }
                },
                status_code=400,
            )
        return FakeResponse({"id": "fb123"})

    def fake_get(url, params=None, timeout=None):
        gets.append({"url": url, "params": params, "timeout": timeout})
        return FakeResponse({"data": [{"id": "page1", "name": "Page", "access_token": "fresh-page-token"}]})

    monkeypatch.setattr(bot.requests, "post", fake_post)
    monkeypatch.setattr(bot.requests, "get", fake_get)
    settings = bot.Settings(
        facebook_page_id="page1",
        facebook_page_access_token="expired-page-token",
        facebook_user_access_token="user-token",
    )
    social = bot.SocialPlan(title="Tieu de", description="Mo ta", tags=["tag"], narration="Loi doc")

    assert bot.upload_to_facebook_page(video, social, settings) == "fb123"
    assert [post["data"]["access_token"] for post in posts] == ["expired-page-token", "fresh-page-token"]
    assert gets[0]["params"]["access_token"] == "user-token"


def test_facebook_expired_page_token_error_is_actionable(tmp_path, monkeypatch):
    video = tmp_path / "short_vi.mp4"
    video.write_bytes(b"video")

    class FakeResponse:
        ok = False
        status_code = 400
        text = '{"error":{"message":"Session has expired.","code":190,"error_subcode":463}}'

        def json(self):
            return {"error": {"message": "Session has expired.", "code": 190, "error_subcode": 463}}

    monkeypatch.setattr(bot.requests, "post", lambda *_args, **_kwargs: FakeResponse())
    settings = bot.Settings(
        facebook_page_id="page1",
        facebook_page_access_token="expired-page-token",
    )
    social = bot.SocialPlan(title="Tieu de", description="Mo ta", tags=["tag"], narration="Loi doc")

    with pytest.raises(bot.FacebookAPIError) as exc_info:
        bot.upload_to_facebook_page(video, social, settings)

    assert "access token da het han" in str(exc_info.value)
    assert "FACEBOOK_USER_ACCESS_TOKEN" in str(exc_info.value)


def test_tiktok_direct_post_uploads_whole_file(tmp_path, monkeypatch):
    video = tmp_path / "short_vi.mp4"
    video.write_bytes(b"z" * 2048)
    posts = []
    puts = []

    class FakeResponse:
        def __init__(self, payload, status_code=200):
            self.payload = payload
            self.status_code = status_code
            self.ok = status_code < 400
            self.text = json.dumps(payload)

        def json(self):
            return self.payload

    def fake_post(url, headers=None, json=None, timeout=None):
        posts.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        if url.endswith("/creator_info/query/"):
            return FakeResponse({"data": {"privacy_level_options": ["SELF_ONLY"]}, "error": {"code": "ok", "message": ""}})
        return FakeResponse({"data": {"publish_id": "tt123", "upload_url": "https://upload.example/video"}, "error": {"code": "ok", "message": ""}})

    def fake_put(url, headers=None, data=None, timeout=None):
        puts.append({"url": url, "headers": headers, "data": data, "timeout": timeout})
        return FakeResponse({}, status_code=201)

    monkeypatch.setattr(bot.requests, "post", fake_post)
    monkeypatch.setattr(bot.requests, "put", fake_put)
    settings = bot.Settings(tiktok_access_token="token")
    social = bot.SocialPlan(title="Tieu de", description="Mo ta #lichsu", tags=["lichsu"], narration="Loi doc")

    assert bot.upload_to_tiktok(video, social, settings) == "tt123"
    init_body = posts[1]["json"]
    assert init_body["source_info"]["source"] == "FILE_UPLOAD"
    assert init_body["source_info"]["total_chunk_count"] == 1
    assert puts[0]["headers"]["Content-Range"] == "bytes 0-2047/2048"


def test_twenty_second_story_accepts_shorter_narration(tmp_path):
    plan = {
        "topic": "Antikythera mechanism", "angle": "Ancient gears modeled the sky",
        "title": "Ancient Greek Gears Tracked the Sky", "description": "Ancient gears modeled the sky. #Shorts",
        "tags": ["shorts", "history", "science"],
        "hook": "A tiny bronze machine once modeled the sky.",
        "narration": "A tiny bronze machine once modeled the sky. Its gears tracked lunar cycles, eclipses, and festival calendars. Found in a shipwreck, it showed Greek engineers built portable astronomy long before telescopes changed time.",
        "closing_line": "Found in a shipwreck, it showed Greek engineers built portable astronomy long before telescopes changed time.",
        "scenes": [{"duration": 4, "visual_prompt": "Scene one"}, {"duration": 4, "visual_prompt": "Scene two"}, {"duration": 4, "visual_prompt": "Scene three"}, {"duration": 4, "visual_prompt": "Scene four"}, {"duration": 4, "visual_prompt": "Scene five"}],
        "fact_note": "Avoids claiming the mechanism predicted every event perfectly.", "source_hints": ["National Archaeological Museum, Athens"],
    }

    class FakeClient:
        def __init__(self):
            self.responses = [
                {"central_claim": "The mechanism modeled cycles.", "evidence_points": ["gears", "lunar cycles", "eclipses"], "uncertainty": "some functions remain debated", "fresh_angle": "portable astronomy", "source_leads": ["National Archaeological Museum, Athens"], "avoid": ["modern computer overclaim"]},
                plan,
                {"quality_check": {"hook_score": 9, "clarity_score": 9}, "plan": plan},
            ]

        def chat(self, _prompt, temperature=0.55):
            return json.dumps(self.responses.pop(0))

    result = bot.plan_short(FakeClient(), bot.Archive(tmp_path / "shorts.db"), "ancient technology", 20)
    assert len(result.narration.split()) < 40
    assert sum(scene.duration for scene in result.scenes) == 20


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


def test_plan_short_auto_corrects_hook_closing_line_mismatch(tmp_path):
    mismatched_plan = {
        "topic": "The Green Sahara", "angle": "A desert shaped by monsoon shifts",
        "title": "When the Sahara Was Green", "description": "A desert was once a lake country. #Shorts",
        "tags": ["shorts", "science", "history"],
        "hook": "Twelve thousand years ago, the Sahara was green.",
        "narration": "In the distant past, the Sahara was green. Monsoon rains fed lakes across North Africa, supporting grasslands and animals. Sediment and archaeological evidence reveal this African Humid Period, though its timing varied by region. A desert is just a temporary climate snapshot.",
        "closing_line": "A desert can be a climate snapshot, not a permanent identity.",
        "scenes": [{"duration": 5, "visual_prompt": "Scene one"}, {"duration": 5, "visual_prompt": "Scene two"}, {"duration": 5, "visual_prompt": "Scene three"}, {"duration": 5, "visual_prompt": "Scene four"}],
        "fact_note": "Regional timing varied.", "source_hints": ["NOAA"],
    }

    class FakeClient:
        def __init__(self):
            self.responses = [
                {"central_claim": "The Sahara had wet periods.", "evidence_points": ["lakes", "sediment", "monsoons"], "uncertainty": "regional timing", "fresh_angle": "climate snapshot", "source_leads": ["NOAA"], "avoid": ["exact dates"]},
                mismatched_plan,
                {"quality_check": {"hook_score": 9, "clarity_score": 9}, "plan": mismatched_plan},
            ]

        def chat(self, _prompt, temperature=0.55):
            return json.dumps(self.responses.pop(0))

    result = bot.plan_short(FakeClient(), bot.Archive(tmp_path / "shorts.db"), "green deserts", 20)
    
    # Narration should start with the hook and end with the closing line after auto-correct
    assert result.narration.startswith("Twelve thousand years ago, the Sahara was green.")
    assert result.narration.endswith("A desert can be a climate snapshot, not a permanent identity.")


def test_ensure_dejavu_font_creates_files(tmp_path, monkeypatch):
    import os
    monkeypatch.setattr(bot, "DATA_DIR", tmp_path)
    
    # Run the function
    bot.ensure_dejavu_font()
    
    font_file = tmp_path / "fonts" / "DejaVuSans.ttf"
    config_file = tmp_path / "fonts.conf"
    
    assert font_file.is_file()
    assert config_file.is_file()
    assert font_file.stat().st_size > 500000
    assert "FONTCONFIG_FILE" in os.environ


# --- Manual idea queue + manual planners ---------------------------------- #
def test_idea_queue_enqueue_claim_and_update(tmp_path):
    archive = bot.Archive(tmp_path / "shorts.db")
    i1 = archive.enqueue_idea("short", "Idea one", 55, True, "private")
    i2 = archive.enqueue_idea("long", "Idea two", None, True, "unlisted")
    assert i1 != i2

    first = archive.claim_next_idea()
    assert first["id"] == i1 and first["status"] == "processing" and first["duration"] == 55
    second = archive.claim_next_idea()
    assert second["id"] == i2 and second["duration"] is None and second["mode"] == "long"
    assert archive.claim_next_idea() is None  # nothing pending left

    archive.update_idea(i1, "done", youtube_id="abc123", output_title="Title A")
    row = archive.get_idea(i1)
    assert row["status"] == "done" and row["youtube_id"] == "abc123" and row["output_title"] == "Title A"
    assert len(archive.recent_ideas()) == 2


def test_idea_queue_recover_stuck_ideas(tmp_path):
    archive = bot.Archive(tmp_path / "shorts.db")
    i1 = archive.enqueue_idea("short", "stuck idea", 60, True, "private")
    archive.claim_next_idea()
    assert archive.get_idea(i1)["status"] == "processing"

    recovered = archive.recover_stuck_ideas()
    assert recovered == 1
    assert archive.get_idea(i1)["status"] == "pending"
    assert archive.claim_next_idea()["id"] == i1  # claimable again


def test_subject_detection_handles_word_forms_without_false_positives():
    # Narration rarely repeats the subject label verbatim.
    assert bot._subject_mentioned("Democrats favor broader federal programs.", "Democratic Party")
    assert bot._subject_mentioned("Republicans push for lower taxes.", "Republican Party")
    assert bot._subject_mentioned("The Olympic games rotate every four years.", "Olympics")
    assert bot._subject_mentioned("Android phones ship from dozens of makers.", "Android")
    # A shared category word must NOT count as naming one side.
    assert not bot._subject_mentioned("Two colas, one endless rivalry.", "Coca-Cola")
    assert not bot._subject_mentioned("Two colas, one endless rivalry.", "Pepsi")
    assert not bot._subject_mentioned("Both still split blind taste tests.", "Coca-Cola")


def test_scene_focus_is_corrected_from_the_named_subject():
    """The pointer must follow who the voiceover names, not the model's guess."""
    segments = [
        "Two parties, one divided nation.",
        "Republicans push for lower taxes and less regulation.",
        "Democrats favor broader federal programs.",
        "Republicans lean on rural turnout.",
        "Democrats dominate dense urban centers.",
        "Both claim the same middle ground.",
    ]
    plan = bot.ShortPlan.from_dict({
        "topic": "t", "subject_a": "Republican Party", "subject_b": "Democratic Party",
        "subject_a_image_query": "q", "subject_b_image_query": "q", "angle": "x",
        "title": "Republican Party vs Democratic Party", "thumbnail_text": "GOP vs Dems",
        "description": "d #A #B", "tags": ["A", "B"],
        "hook": segments[0], "narration": " ".join(segments), "closing_line": segments[-1],
        "fact_note": "n", "source_hints": ["s"],
        # The writer handed back a reversed focus track; sync must repair it.
        "scenes": [
            {"duration": 5, "focus": f, "visual_prompt": "v", "voiceover": s}
            for f, s in zip(["both", "b", "a", "b", "a", "both"], segments)
        ],
    })

    bot.synchronize_comparison_scene_voiceovers(plan)

    assert [s.focus for s in plan.scenes] == ["both", "a", "b", "a", "b", "both"]


def test_scene_focus_holds_the_subject_through_a_pronoun_beat():
    segments = ["Intro line.", "Coca-Cola leans on heritage.", "It never changed that formula.", "Closing line."]
    plan = bot.ShortPlan.from_dict({
        "topic": "t", "subject_a": "Coca-Cola", "subject_b": "Pepsi",
        "subject_a_image_query": "q", "subject_b_image_query": "q", "angle": "x",
        "title": "Coca-Cola vs Pepsi", "thumbnail_text": "t", "description": "d #A #B", "tags": ["A", "B"],
        "hook": segments[0], "narration": " ".join(segments), "closing_line": segments[-1],
        "fact_note": "n", "source_hints": ["s"],
        # No usable focus on the pronoun beat -> it must hold "a", not flip to "b".
        "scenes": [
            {"duration": 5, "focus": f, "visual_prompt": "v", "voiceover": s}
            for f, s in zip(["both", "a", "", "both"], segments)
        ],
    })

    bot.synchronize_comparison_scene_voiceovers(plan)

    assert [s.focus for s in plan.scenes] == ["both", "a", "a", "both"]


def test_comparison_idea_round_trips_and_ignores_plain_text():
    encoded = bot.encode_comparison_idea("Coca-Cola", "Pepsi", "hương vị")

    assert bot.decode_comparison_idea(encoded) == {
        "subject_a": "Coca-Cola", "subject_b": "Pepsi", "angle": "hương vị",
    }
    assert bot.describe_manual_idea(encoded) == "Coca-Cola vs Pepsi — hương vị"
    assert bot.describe_manual_idea(bot.encode_comparison_idea("A", "B")) == "A vs B"
    # Plain-text ideas (long-form, or the CLI) must pass through untouched.
    assert bot.decode_comparison_idea("Trận Điện Biên Phủ 1954") is None
    assert bot.describe_manual_idea("Trận Điện Biên Phủ 1954") == "Trận Điện Biên Phủ 1954"
    # Malformed / incomplete payloads degrade to plain text rather than raising.
    assert bot.decode_comparison_idea('{"subject_a": "only one"}') is None
    assert bot.decode_comparison_idea("{not json") is None


def _manual_comparison_plan_json():
    return {
        "topic": "Coke vs Pepsi", "subject_a": "Coke", "subject_b": "PEPSI CO",
        "subject_a_image_query": "unrelated mountain", "subject_b_image_query": "unrelated beach",
        "angle": "taste and marketing", "title": "Coca-Cola vs Pepsi: Real Differences",
        "thumbnail_text": "Coca vs Pepsi", "description": "d #A #B", "tags": ["A", "B"],
        "hook": "Two colas, one endless rivalry.",
        "narration": (
            "Two colas, one endless rivalry. Coca-Cola leans on heritage branding. "
            "Pepsi chased a younger audience. Coca-Cola keeps a smoother vanilla note. "
            "Pepsi hits sweeter up front. Both still split taste tests worldwide."
        ),
        "closing_line": "Both still split taste tests worldwide.",
        "scenes": [
            {"duration": 5, "focus": "both", "visual_prompt": "matchup", "voiceover": "Two colas, one endless rivalry."},
            {"duration": 5, "focus": "a", "visual_prompt": "coke", "voiceover": "Coca-Cola leans on heritage branding."},
            {"duration": 5, "focus": "b", "visual_prompt": "pepsi", "voiceover": "Pepsi chased a younger audience."},
            {"duration": 5, "focus": "a", "visual_prompt": "coke2", "voiceover": "Coca-Cola keeps a smoother vanilla note."},
            {"duration": 5, "focus": "b", "visual_prompt": "pepsi2", "voiceover": "Pepsi hits sweeter up front."},
            {"duration": 5, "focus": "both", "visual_prompt": "close", "voiceover": "Both still split taste tests worldwide."},
        ],
        "fact_note": "n", "source_hints": ["s"],
    }


def test_manual_short_pins_the_users_two_subjects():
    prompts = []

    class FakeClient:
        def chat(self, prompt, **kwargs):
            prompts.append(prompt)
            return json.dumps(_manual_comparison_plan_json())

    idea = bot.encode_comparison_idea("Coca-Cola", "Pepsi", "hương vị và marketing")
    plan = bot.plan_short_from_idea(FakeClient(), 30, idea)

    # The model answered "Coke"/"PEPSI CO"; the user's pinned names must win.
    assert (plan.subject_a, plan.subject_b) == ("Coca-Cola", "Pepsi")
    assert bot.is_comparison_plan(plan)
    # Image searches that drifted to another subject are repaired to the pinned name.
    assert plan.subject_a_image_query == "Coca-Cola"
    assert plan.subject_b_image_query == "Pepsi"
    assert [s.focus for s in plan.scenes] == ["both", "a", "b", "a", "b", "both"]
    assert abs(sum(s.duration for s in plan.scenes) - 30) < 0.1
    assert 'MUST be exactly "Coca-Cola"' in prompts[0]
    assert "hương vị và marketing" in prompts[0]


def test_manual_short_derives_subjects_from_free_text_versus():
    class FakeClient:
        def chat(self, prompt, **kwargs):
            payload = _manual_comparison_plan_json()
            payload["subject_a_image_query"] = "Coca-Cola logo can"
            payload["subject_b_image_query"] = "Pepsi logo can"
            return json.dumps(payload)

    plan = bot.plan_short_from_idea(FakeClient(), 30, "Coca-Cola vs Pepsi")

    assert (plan.subject_a, plan.subject_b) == ("Coca-Cola", "Pepsi")
    # On-topic image queries from the model are kept as-is.
    assert plan.subject_a_image_query == "Coca-Cola logo can"


def test_plan_long_form_from_idea_seeds_idea_and_has_no_vietnam_hard_block(monkeypatch):
    monkeypatch.setattr(bot, "fetch_news_for_idea", lambda *a, **k: [])  # no network in tests
    idea = "Trận Điện Biên Phủ 1954 và ý nghĩa lịch sử"
    plan_json = {
        "topic": "Dien Bien Phu 1954",
        "angle": "A siege that ended a colonial war",
        "title": "Dien Bien Phu: The Siege That Changed History",
        "thumbnail_text": "Dien Bien Phu",
        "description": "The 1954 siege explained. #history #documentary",
        "tags": ["history", "documentary"],
        "hook": "One valley decided a war.",
        "narration": "One valley decided a war. Troops dug into the hills above a remote basin. Supplies came only by air, and the air was contested. Week by week the ring tightened. When it ended, a colonial era ended with it.",
        "closing_line": "When it ended, a colonial era ended with it.",
        "scenes": [
            {"duration": 75, "visual_prompt": "Wide misty valley basin", "search_query": "Dien Bien Phu valley"},
            {"duration": 75, "visual_prompt": "Soldiers digging hillside trenches", "search_query": "1954 trench warfare"},
            {"duration": 75, "visual_prompt": "Cargo plane over mountains", "search_query": "1950s transport plane"},
            {"duration": 75, "visual_prompt": "A battlefield at dawn", "search_query": "battlefield aftermath 1954"},
        ],
        "fact_note": "Kept casualty numbers general.",
        "source_hints": ["History archive"],
    }
    prompts = []

    class FakeClient:
        long_form_reasoning_effort = "medium"

        def chat(self, prompt, **kwargs):
            prompts.append(prompt)
            return json.dumps(plan_json)

    # A Vietnam-related idea must NOT be rejected by the manual planner.
    plan = bot.plan_long_form_from_idea(FakeClient(), 300, 4, 4, idea)
    assert len(plan.scenes) == 4
    assert abs(sum(scene.duration for scene in plan.scenes) - 300) < 0.1
    assert idea in prompts[0]
    assert "explicitly requested THIS exact idea" in prompts[0]
    assert "Fresh related news headlines" in prompts[0]  # news grounding is wired in
    # The auto-flow Vietnam hard rule must be absent from the manual prompt.
    assert "public controversy related to Vietnam" not in prompts[0]


def test_fetch_news_for_idea_parses_and_dedups(monkeypatch):
    rss = (
        b'<?xml version="1.0"?><rss><channel>'
        b"<item><title>AI firms race to ship models</title><description>Big labs compete</description>"
        b"<link>http://x/1</link><pubDate>Mon, 01 Jul 2026</pubDate></item>"
        b"<item><title>AI firms race to ship models</title><description>dup</description><link>http://x/2</link></item>"
        b"<item><title>Chip supply tightens</title><description>demand up</description><link>http://x/3</link></item>"
        b"</channel></rss>"
    )

    class FakeResp:
        content = rss

        def raise_for_status(self):
            pass

    monkeypatch.setattr(bot.requests, "get", lambda *a, **k: FakeResp())
    items = bot.fetch_news_for_idea("AI competition", limit=5)
    assert len(items) == 2  # duplicate title collapsed
    assert items[0]["title"].startswith("AI firms")
    assert items[0]["link"] == "http://x/1"


class FakePlaylistService:
    """Minimal stand-in for the YouTube Data API playlist endpoints."""

    def __init__(self, existing=(), pages=None, fail_on=""):
        self.existing = list(existing)
        self.pages = pages
        self.fail_on = fail_on
        self.created = []
        self.added = []

    class _Call:
        def __init__(self, result):
            self._result = result

        def execute(self):
            return self._result() if callable(self._result) else self._result

    def _boom(self, name):
        from googleapiclient.errors import HttpError

        class Resp:
            status = 403
            reason = "insufficientPermissions"

        raise HttpError(Resp(), b'{"error": {"message": "no"}}', uri=name)

    def playlists(self):
        service = self

        class Playlists:
            def list(self, part, mine, maxResults, pageToken=None):
                if service.fail_on == "list":
                    return service._Call(lambda: service._boom("list"))
                if service.pages is not None:
                    return service._Call(service.pages[pageToken])
                return service._Call({"items": [
                    {"id": pid, "snippet": {"title": title}} for pid, title in service.existing
                ]})

            def insert(self, part, body):
                if service.fail_on == "insert":
                    return service._Call(lambda: service._boom("insert"))
                service.created.append(body)
                return service._Call({"id": "PL_new"})

        return Playlists()

    def playlistItems(self):
        service = self

        class PlaylistItems:
            def insert(self, part, body):
                if service.fail_on == "add":
                    return service._Call(lambda: service._boom("add"))
                service.added.append(body["snippet"])
                return service._Call({"id": "PLI_1"})

        return PlaylistItems()


def _playlist_settings(**overrides):
    return bot.Settings(
        youtube_playlist_enabled=True,
        youtube_playlist_privacy="public",
        **overrides,
    )


def test_ensure_playlist_reuses_an_existing_playlist_instead_of_creating_a_duplicate():
    category = bot.LONG_FORM_CATEGORY_BY_SLUG["wars"]
    service = FakePlaylistService(existing=[("PL_other", "Something else"), ("PL_wars", category.playlist_title)])

    assert bot.ensure_playlist(service, category, _playlist_settings()) == "PL_wars"
    assert service.created == []


def test_ensure_playlist_creates_the_playlist_on_first_use():
    category = bot.LONG_FORM_CATEGORY_BY_SLUG["origins"]
    service = FakePlaylistService(existing=[])

    assert bot.ensure_playlist(service, category, _playlist_settings()) == "PL_new"
    body = service.created[0]
    assert body["snippet"]["title"] == category.playlist_title
    assert body["snippet"]["description"] == category.playlist_description
    assert body["status"]["privacyStatus"] == "public"


def test_ensure_playlist_pages_past_the_first_fifty_playlists():
    category = bot.LONG_FORM_CATEGORY_BY_SLUG["figures"]
    service = FakePlaylistService(pages={
        None: {"items": [{"id": "PL_a", "snippet": {"title": "Unrelated"}}], "nextPageToken": "p2"},
        "p2": {"items": [{"id": "PL_figures", "snippet": {"title": category.playlist_title}}]},
    })

    assert bot.ensure_playlist(service, category, _playlist_settings()) == "PL_figures"
    assert service.created == []


def test_add_video_to_playlist_files_the_video_under_its_topic():
    category = bot.LONG_FORM_CATEGORY_BY_SLUG["cultures"]
    service = FakePlaylistService(existing=[("PL_cultures", category.playlist_title)])

    bot.add_video_to_playlist(service, "vid123", category, _playlist_settings())

    assert service.added == [{
        "playlistId": "PL_cultures",
        "resourceId": {"kind": "youtube#video", "videoId": "vid123"},
    }]


@pytest.mark.parametrize("fail_on", ["list", "insert", "add"])
def test_playlist_failures_never_raise_because_the_video_is_already_live(fail_on):
    category = bot.LONG_FORM_CATEGORY_BY_SLUG["events"]
    service = FakePlaylistService(existing=[], fail_on=fail_on)

    # Must not raise: upload_to_youtube calls this after the video is published,
    # so a playlist error has to stay a logged warning, like a failed thumbnail.
    bot.add_video_to_playlist(service, "vid123", category, _playlist_settings())

    assert service.added == []


def test_shorts_carry_no_topic_category_so_they_never_join_a_playlist():
    short = bot.ShortPlan.from_dict({
        "topic": "Coca-Cola vs Pepsi", "angle": "Two rival colas",
        "title": "Coke vs Pepsi", "description": "#Shorts", "tags": ["shorts"],
        "narration": "A short script. It ends here.", "fact_note": "Qualitative",
        "source_hints": ["General knowledge"],
        "scenes": [{"duration": 4, "visual_prompt": "A scene"}],
    })

    assert short.topic_category == ""
    # This lookup is the gate in upload_to_youtube; a Short must never match.
    assert bot.LONG_FORM_CATEGORY_BY_SLUG.get(short.topic_category) is None


def test_saved_token_reports_the_scopes_it_was_actually_granted(tmp_path):
    # Regression guard. Credentials.from_authorized_user_file echoes back whatever
    # scope list it is handed, so passing YOUTUBE_SCOPES here would make an
    # upload-only token look playlist-capable and turn the graceful skip into an
    # opaque 403 at playlistItems.insert.
    from google.oauth2.credentials import Credentials

    token = tmp_path / "youtube_token.json"
    token.write_text(json.dumps({
        "token": "x", "refresh_token": "y", "client_id": "a", "client_secret": "b",
        "token_uri": "https://oauth2.googleapis.com/token",
        "scopes": ["https://www.googleapis.com/auth/youtube.upload"],
    }))

    granted = set(Credentials.from_authorized_user_file(str(token)).scopes or ())
    assert bot.YOUTUBE_PLAYLIST_SCOPE not in granted

    widened = set(Credentials.from_authorized_user_file(str(token), bot.YOUTUBE_SCOPES).scopes or ())
    assert bot.YOUTUBE_PLAYLIST_SCOPE in widened, "the trap this test exists to prevent"
