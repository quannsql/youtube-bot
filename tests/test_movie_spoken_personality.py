"""No technical IDs reach speech; delivery updates reuse verified video evidence."""
import copy
import re

import pytest

import youtube_shorts_bot as legacy
from movie_analysis.core import BARE_CITATION, Cache, MovieConfig, digest, read_json, spoken_text, validate_script, write_json
from movie_analysis.narration import DEFAULT_STYLE, spoken_style_guidance, validate_context
from movie_analysis.review import review_audit_prompt, review_writer_prompt, review_repair_prompt
from test_movie_analysis import FakeVideoAI, SyntheticTTS, claim, script, synthetic_clip


@pytest.mark.parametrize("text,expected", [
    ("Batman đứng vững. CTX_title C001_1 C001_2", "Batman đứng vững."),
    ("C001_1 Superman phản công. c001_2", "Superman phản công."),
    ("Ông chú sắp toang? [CTX_title, C001_1; S001_c2]", "Ông chú sắp toang?"),
    ("Thì ra là đòn phản công! (CTX_title; C001_1)", "Thì ra là đòn phản công!"),
    ("Màu đỏ. `ctx_title` **S001_c1** 'c001_2'", "Màu đỏ."),
    ("Đỏ thế này? char_auto_123abc b01:0 CTX_IDENTITY_auto_ab", "Đỏ thế này?"),
    ("Định làm gì nữa đây? [c001_1\nctx_title]", "Định làm gì nữa đây?"),
    ("[tiếng cười] Toang! [ctx_doom]", "[tiếng cười] Toang!"),
    ("C-3PO và R2-D2 trong 2001: A Space Odyssey.", "C-3PO và R2-D2 trong 2001: A Space Odyssey."),
    ("Batman hỏi: 'Liệu Superman có chịu nổi không?'", "Batman hỏi: 'Liệu Superman có chịu nổi không?'"),
])
def test_reserved_ids_removed_without_damaging_names_dialogue_or_vietnamese(text, expected):
    assert spoken_text(text) == expected
    assert spoken_text(expected) == expected


def test_cleaning_keeps_provenance_and_the_audit_sees_only_spoken_language():
    data = script()
    data["beats"][0]["sentences"][0]["text"] = "Khung hình có nền đỏ. CTX_title C001_1 [S001_c1]"
    metadata = copy.deepcopy(data["beats"][0]["sentences"][0]["claim_ids"])
    result = validate_script(data, {"accepted": [claim()]})
    assert result["beats"][0]["voiceover"] == "Khung hình có nền đỏ."
    assert result["beats"][0]["sentences"][0]["claim_ids"] == metadata
    assert not BARE_CITATION.search(result["beats"][0]["sentences"][0]["text"])


@pytest.mark.parametrize("text", ["CTX_title", "C001_1 C001_2", "[CTX_title].", "**s001_c1**;", "char_auto_a b01:0"])
def test_metadata_only_sentences_cannot_be_approved_or_read(text):
    data = script()
    data["beats"][0]["sentences"][0]["text"] = text
    with pytest.raises(legacy.BotError, match="chỉ chứa mã"):
        validate_script(data, {"accepted": [claim()]})


def test_personality_is_shared_by_writer_audit_repair_and_respects_serious_style():
    context = validate_context({"title": "Synthetic Film", "style": "Tấu hài, tò mò, bạn thân"})
    evidence = {"accepted": [claim()]}
    plan = FakeVideoAI().generate("review_plan")
    data = FakeVideoAI().generate("script")
    prompts = [review_writer_prompt(60, evidence, context, {}, plan),
               review_audit_prompt(data, evidence, context, plan),
               review_repair_prompt({"script": data}, evidence, context, 60)]
    for prompt in prompts:
        assert "ông chú" in prompt and "chị gái này" in prompt and "thanh niên này" in prompt
        assert "Liệu Batman có chịu nổi đòn này không?" in prompt
        assert "verified Batman AND an observed attack" in prompt
        assert "NOT invented character dialogue" in prompt and "never paste the same stock" in prompt
    assert "factual presuppositions" in prompts[0] and "question's presuppositions" in prompts[1]
    serious = spoken_style_guidance(validate_context({"style": "Nghiêm túc, không đùa"}))
    assert "Keep serious or restrained styles serious" in serious and "Nghiêm túc, không đùa" in serious
    assert len(DEFAULT_STYLE) <= 1000


def test_cached_old_narration_with_bare_ids_is_rewritten_without_reanalyzing_video(synthetic_clip, tmp_path, monkeypatch):
    import movie_analysis.core as core
    import movie_analysis.pipeline as pipeline
    directory = tmp_path / "job"
    new_clean, new_prompt = core.spoken_text, pipeline.PROMPT_VERSION

    class Writer(FakeVideoAI):
        def generate(self, stage, *args, **kwargs):
            result = super().generate(stage, *args, **kwargs)
            if stage == "script":
                result["beats"][0]["sentences"][0]["text"] += " CTX_title C001_1 C001_2"
                if "curious, playful personality" in args[0]:
                    result["beats"][1]["sentences"][0]["text"] = "Màu đỏ này gợi cảm giác gì nhỉ?"
                    result["beats"][1]["sentences"][0]["kind"] = "inference"
            return result

    monkeypatch.setattr(core, "spoken_text", lambda text: re.sub(r"\s+", " ", text).strip())
    monkeypatch.setattr(pipeline, "PROMPT_VERSION", "old-spoken-story-fixture")
    pipeline.run_movie(synthetic_clip, directory, MovieConfig(), provider=Writer(), script_only=True)
    assert "CTX_title" in (directory / "narration_vi.txt").read_text(encoding="utf-8")
    before = {name: (directory / name).read_bytes() for name in ("global.json", "s001_deep.json", "s001_verify.json")}
    monkeypatch.setattr(core, "spoken_text", new_clean)
    monkeypatch.setattr(pipeline, "PROMPT_VERSION", new_prompt)
    resumed = Writer()
    pipeline.run_movie(synthetic_clip, directory, MovieConfig(), provider=resumed, script_only=True)
    assert [s for s, _ in resumed.calls] == ["review_plan", "script", "script_audit"] and resumed.uploads == 0
    assert all((directory / name).read_bytes() == contents for name, contents in before.items())
    text = (directory / "narration_vi.txt").read_text(encoding="utf-8")
    assert not BARE_CITATION.search(text) and "gợi cảm giác gì nhỉ?" in text
    cached = Writer()
    pipeline.run_movie(synthetic_clip, directory, MovieConfig(), provider=cached, script_only=True)
    assert cached.calls == []


def test_render_boundary_cleans_legacy_audio_payload_and_only_recreates_changed_voice(synthetic_clip, tmp_path):
    from movie_analysis.media import render_movie
    from movie_analysis.pipeline import movie_settings
    from movie_analysis.core import rights_manifest, file_digest
    directory = tmp_path / "job"
    directory.mkdir()
    data = validate_script(script(), {"accepted": [claim()]})
    cfg, settings, provider = MovieConfig(), movie_settings(), FakeVideoAI()

    class TTS(SyntheticTTS):
        def __init__(self):
            super().__init__()
            self.texts = []
        def speech(self, text, path):
            self.texts.append(text)
            assert not BARE_CITATION.search(text)
            super().speech(text, path)

    tts = TTS()
    rights = rights_manifest("owned", "Synthetic source", file_digest(synthetic_clip))
    render_movie(synthetic_clip, directory, data, {"accepted": [claim()]}, rights, settings, tts, provider, cfg)
    assert tts.calls == 2
    unaffected = read_json(directory / "b02_voice.json")
    dirty = copy.deepcopy(data)
    dirty["beats"][0]["voiceover"] += " CTX_title C001_1"
    dirty["beats"][0]["sentences"][0]["text"] += " CTX_title C001_1"
    # Simulate the old audio receipt: it was generated from an unfiltered payload.
    receipt = read_json(directory / "b01_voice.json")
    receipt["key"] = digest([dirty["beats"][0]["voiceover"], settings.google_tts_vietnamese_voice,
                              settings.google_tts_vietnamese_speaking_rate])
    write_json(directory / "b01_voice.json", receipt)
    rendered = read_json(directory / "render.json")
    rendered["key"] = "old-unfiltered-output-fixture"
    write_json(directory / "render.json", rendered)
    render_movie(synthetic_clip, directory, dirty, {"accepted": [claim()]}, rights, settings, tts, provider, cfg)
    assert tts.calls == 3 and read_json(directory / "b02_voice.json") == unaffected
    assert "CTX_title" in dirty["beats"][0]["voiceover"]  # renderer didn't mutate caller/cache metadata
    captions = (directory / "captions_vi.ass").read_text(encoding="utf-8")
    assert not BARE_CITATION.search(captions)
    render_movie(synthetic_clip, directory, dirty, {"accepted": [claim()]}, rights, settings, tts, provider, cfg)
    assert tts.calls == 3


def test_render_rejects_metadata_only_voice_before_paid_tts(tmp_path):
    from movie_analysis.media import render_movie
    data = {"beats": [{"voiceover": "CTX_title C001_1"}]}
    with pytest.raises(legacy.BotError, match="dừng trước TTS"):
        render_movie(tmp_path / "missing.mp4", tmp_path, data, {}, {}, None, None, None, MovieConfig())
