from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import youtube_shorts_bot as legacy
from .core import (VERSION, AuditCoverageError, Cache, MovieConfig, accepted_evidence, digest, file_digest, job_lock, read_json,
                   rights_manifest, validate_audit, validate_global, validate_scene, validate_script,
                   validate_audit_coverage, validate_verdicts, write_json)
from .audit import complete_review_audit
from .media import build_scenes, detect_scenes, enable_local_tools, inspect_source, render_movie
from .provider import GeminiVideoClient
from .live_provider import GeminiLiveVideoClient
from .narration import PROMPT_VERSION, load_context
from .characters import identity_frames, recognize_characters, recognize_character_chunks
from .chunks import CHUNK_VERSION, analyze_chunks, session_seconds, scene_windows
from .review import (plan_prompt, review_audit_prompt, review_writer_prompt, review_repair_prompt, validate_plan,
                     validate_review_audit, validate_review_script)
from .openai_writer import OpenAIWriterClient
from .timing import NarrationTimingError
from .timing_repair import compact_allowed_claims, repair_narration_timing

LOG = logging.getLogger("movie")


def payload(data):
    return json.dumps(data, ensure_ascii=False)


def movie_settings() -> legacy.Settings:
    """Reuse TTS/mux settings without requiring the documentary OpenAI credentials."""
    logo = Path(os.getenv("MOVIE_LOGO_FILE", "").strip() or os.getenv("OVERLAY_LOGO_FILE", "overlay-logo.png"))
    if not logo.is_absolute():
        logo = legacy.ROOT / logo
    return legacy.Settings(
        google_tts_service_account=legacy.DATA_DIR / os.getenv("GOOGLE_TTS_SERVICE_ACCOUNT_FILE", "google_tts_service_account.json"),
        google_tts_vietnamese_voice=os.getenv("GOOGLE_TTS_VI_VOICE", "vi-VN-Chirp3-HD-Aoede"),
        google_tts_vietnamese_speaking_rate=float(os.getenv("GOOGLE_TTS_VI_SPEAKING_RATE", "1.0")),
        overlay_logo=logo,
        overlay_logo_short_width=int(os.getenv("OVERLAY_LOGO_SHORT_WIDTH", "220")),
        overlay_logo_margin=int(os.getenv("OVERLAY_LOGO_MARGIN", "36")),
        overlay_logo_short_top_margin=int(os.getenv("OVERLAY_LOGO_SHORT_TOP_MARGIN", "72")),
    )


def run_movie(source: Path, directory: Path, config: MovieConfig, rights_status="unknown", rights_basis="",
              regenerate_script=False, regenerate_voice=False, analyze_only=False, provider=None, tts=None,
              regeneration_id=None, movie_title=None, movie_context=None, narration_style=None,
              context_file=None, script_only=False, text_provider=None) -> Path:
    source, directory = source.resolve(), directory.resolve()
    # Lock before state writes, quota accounting, cache invalidation or regeneration.
    with job_lock(directory):
        state = read_json(directory / "job.json", {})
        state.update(status="processing", source=str(source), error=None, updated_at=datetime.now(UTC).isoformat())
        write_json(directory / "job.json", state)
        try:
            enable_local_tools()
            legacy.require_tools()
            info = inspect_source(source, config)
            settings = movie_settings()
            if not analyze_only and not script_only and tts is None:
                legacy.materialize_railway_credentials()
                if not settings.google_tts_service_account.is_file():
                    raise legacy.BotError("Thiếu Google TTS service-account JSON; có thể chạy --analyze-only trước.")
            if not analyze_only and not script_only and not settings.overlay_logo.is_file():
                raise legacy.BotError("Thiếu logo; kiểm tra MOVIE_LOGO_FILE hoặc OVERLAY_LOGO_FILE.")
            source_hash = file_digest(source)
            if state.get("source_sha256") and state["source_sha256"] != source_hash:
                raise legacy.BotError("Thư mục job thuộc video khác; dùng thư mục mới để giữ evidence/usage chính xác.")
            state["source_sha256"] = source_hash
            context = load_context(directory, movie_title, movie_context, narration_style, context_file)
            rights = rights_manifest(rights_status, rights_basis, source_hash)
            write_json(directory / "rights.json", rights)
            cache = Cache(directory)
            provider_type = GeminiLiveVideoClient if config.video_provider == "gemini_live" else GeminiVideoClient
            client = provider or provider_type(os.getenv("GEMINI_API_KEY", "").strip(), config, directory)
            state.update(video_provider=config.video_provider, analysis_model=config.model)
            remote = None
            writer = text_provider
            writer_model = config.writer_model or (os.getenv("OPENAI_TEXT_MODEL", "gpt-5.4-mini")
                          if config.writer_provider == "openai" else config.model)
            audit_model = writer_model if config.writer_provider == "openai" else (config.writer_model or config.verifier_model)

            def ask_text(stage, prompt, timing_word_limits=None, timing_claim_ids=None):
                nonlocal writer
                if config.writer_provider == "openai":
                    if writer is None:
                        writer = OpenAIWriterClient(os.getenv("OPENAI_API_KEY", "").strip(), config, client, writer_model)
                    if isinstance(writer, OpenAIWriterClient):
                        return writer.generate(stage, prompt,
                            allowed_claim_ids=[c["id"] for c in evidence["accepted"]] + [f["id"] for f in context["facts"]],
                            allowed_visual_ids=[c["id"] for c in evidence["accepted"] if c["modality"] in {"visual", "both"}],
                            timing_word_limits=timing_word_limits, timing_claim_ids=timing_claim_ids)
                    return writer.generate(stage, prompt)
                if provider is not None:
                    return ask(stage, prompt, False, verify=stage == "script_audit")
                return client.generate(stage, prompt, verify=stage == "script_audit",
                                       model_override=audit_model if stage == "script_audit" else writer_model)

            def ask(stage, prompt, scene=None, fps=None, verify=False, model_override=None):
                nonlocal remote
                if scene is not False and remote is None:
                    remote = client.file(source, source_hash)
                span = None if not scene else (scene["start"], scene["end"])
                extra = {"model_override": model_override} if model_override else {}
                return client.generate(stage, prompt, remote=remote if scene is not False else None,
                                       span=span, fps=fps or config.global_fps, verify=verify, **extra)

            identity_writer = None
            identity_images = None

            def ask_identity(stage, prompt, **kwargs):
                nonlocal identity_writer, identity_images
                character_ids = kwargs.pop("character_ids", None)
                if config.identity_provider == "gemini":
                    return ask(stage, prompt, model_override=config.identity_model or None, **kwargs)
                if identity_writer is None:
                    identity_writer = text_provider or OpenAIWriterClient(
                        os.getenv("OPENAI_API_KEY", "").strip(), config, client,
                        config.identity_model or config.writer_model or os.getenv("OPENAI_TEXT_MODEL", "gpt-5.4-mini"))
                if identity_images is None:
                    identity_images = identity_frames(source, directory, evidence)
                return identity_writer.generate(stage, prompt, image_paths=identity_images,
                                                allowed_claim_ids=[c["id"] for c in evidence["accepted"]],
                                                allowed_character_ids=character_ids or [c["id"] for c in context["characters"]])

            window_images = {}

            def ask_window_identity(stage, prompt, window, **kwargs):
                nonlocal identity_writer
                character_ids = kwargs.pop("character_ids", None)
                if config.identity_provider == "gemini":
                    return ask(stage, prompt, window, **kwargs)
                if identity_writer is None:
                    identity_writer = text_provider or OpenAIWriterClient(
                        os.getenv("OPENAI_API_KEY", "").strip(), config, client,
                        config.identity_model or config.writer_model or os.getenv("OPENAI_TEXT_MODEL", "gpt-5.4-mini"))
                scoped = [c for c in evidence["accepted"] if c["start"] >= window["start"] and c["end"] <= window["end"]]
                if window["id"] not in window_images:
                    window_images[window["id"]] = identity_frames(source, directory / "identity_chunks" / window["id"],
                                                                {"accepted": scoped})
                return identity_writer.generate(stage, prompt, image_paths=window_images[window["id"]],
                    allowed_claim_ids=[c["id"] for c in scoped],
                    allowed_character_ids=character_ids or [c["id"] for c in context["characters"]])

            def checkpoint(stage):
                state.update(stage=stage, updated_at=datetime.now(UTC).isoformat())
                write_json(directory / "job.json", state)

            checkpoint("global")
            scene_config = config
            if config.video_provider == "gemini_live":
                # Leave room for the verifier's one-second overlap on each side.
                scene_config = replace(config, max_scene_seconds=min(config.max_scene_seconds, session_seconds(config) - 2))
            detected = cache.stage("shots", [source_hash, scene_config.max_scene_seconds, config.max_scenes, "group-v2"],
                                   lambda: detect_scenes(source, info, scene_config))
            global_dependencies = [source_hash, config.video_provider, config.model, config.global_fps, client.cache_version if isinstance(client, GeminiLiveVideoClient) else "rest-v1"]
            if config.video_provider == "gemini_live":
                global_dependencies += [CHUNK_VERSION, session_seconds(config)]
            global_prompt = (f"Watch the entire {info['duration']:.3f}s video including its sound. "
                            "Describe chronology, setting, character labels, relationships actually demonstrated, "
                            "audible dialogue (do not invent unintelligible speech), music/sound cues and camera choices. "
                            "Identify promising analysis angles grounded in the clip, not a full-film recap. "
                            'JSON: {summary:string, characters:[{label:string,description:string,name_evidence:string}], '
                            'events:[{start:number,end:number,description:string}], dialogue:[{start:number,end:number,'
                            'speaker:string,text:string,uncertain:boolean}], angles:[string], uncertainties:[string]}. '
                            f"Audio stream present: {info['has_audio']}. "
                            f"The source starts at 0.000 seconds and ends at {info['duration']:.6f} seconds. "
                            "Every event/dialogue start and end MUST be numeric seconds within these bounds. "
                            "Do not use original full-film timestamps, milliseconds, or describe beyond the clip.")
            tolerance = min(0.5, 0.5 / config.global_fps)

            def analyze_global():
                feedback = None
                for attempt in range(config.attempts):
                    prompt = global_prompt
                    if feedback:
                        prompt += ("\nThe previous response failed timeline validation: " + payload(feedback)
                                   + "\nRewatch the entire supplied clip and return a corrected complete analysis. "
                                   "Do not merely clamp or rescale invalid timestamps or invent missing events.")
                    raw = cache.stage(f"global_attempt_{attempt + 1}", [global_dependencies, feedback],
                                      lambda: ask("global", prompt))
                    try:
                        return validate_global(raw, info["duration"], tolerance)
                    except legacy.BotError as exc:
                        LOG.warning("Global timeline validation failed: %s", exc)
                        feedback = {"error": str(exc), "previous_response": raw}
                        if attempt + 1 == config.attempts:
                            raise

            global_data = cache.stage(
                "global", global_dependencies,
                (lambda: analyze_chunks(cache, directory, info, config, global_dependencies, ask, checkpoint))
                if config.video_provider == "gemini_live" else analyze_global,
                lambda data: validate_global(data, info["duration"], tolerance),
            )
            # Add semantic event boundaries to shot cuts. Fast cuts stay grouped,
            # while the deep passes still cover every second of the source.
            boundaries = [s["end"] for s in detected[:-1]]
            boundaries += [t for s in detected for t in s["shot_cuts"]]
            boundaries += [e["end"] for e in global_data["events"]]
            detected = build_scenes(info["duration"], boundaries, scene_config.max_scene_seconds, config.max_scenes)
            write_json(directory / "scenes.json", detected)
            claims, verdicts, rechecks = [], [], 0
            for scene in detected:
                sid = scene["id"]
                checkpoint("scene:" + sid)
                scene_context = {"characters": global_data.get("characters", []), "events": [e for e in global_data["events"]
                           if e["start"] < scene["end"] and e["end"] > scene["start"]]}
                scene_context["dialogue"] = [d for d in global_data.get("dialogue", [])
                                             if d["start"] < scene["end"] + 1 and d["end"] > scene["start"] - 1]
                fps = config.recheck_fps if scene["fast_action"] else config.scene_fps
                deep_prompt = (
                        f"Analyze every meaningful event and audio in {scene['start']}–{scene['end']}s. "
                        "Track distinct characters, action order, spatial advantages, camera/editing and dialogue. "
                        "Global context is a hypothesis to challenge, not truth: " + payload(scene_context) + "\n"
                        "Return at most 16 atomic, useful claims; keep each text and evidence under 240 characters. "
                        "Split fact from causal interpretation. Evidence must "
                        "describe a directly visible/audible detail, not repeat the claim. Do not create external facts. "
                        'JSON: {claims:[{id:string,kind:"observed"|"inference"|"external",modality:"visual"|"audio"|"both",'
                        'start:number,end:number,text:string,evidence:string,confidence:number}], '
                        'dialogue:[{start:number,end:number,speaker:string,text:string,uncertain:boolean}], uncertainties:[string]}. '
                        f"IDs must start {sid}_; timestamps ABSOLUTE original-video seconds. "
                        f"Only source seconds {scene['start']:.6f}–{scene['end']:.6f} are supplied. "
                        "Every claim and dialogue interval must satisfy scene_start <= start < end <= scene_end. "
                        "Do not describe events outside this interval, even if global context mentions them.")

                def repair_deep(previous, error):
                    LOG.warning("Scene %s validation failed; re-analyzing the same interval: %s", sid, error)
                    return ask("deep:" + sid, deep_prompt + "\nPrevious analysis failed validation: "
                               + payload(error) + "\nRewatch the supplied interval and return a complete corrected analysis. "
                               "Re-establish timestamps from the media. Omit events not present in this interval. "
                               "Do not simply clamp/rescale timestamps or invent supporting evidence.", scene, fps)

                deep = cache.stage(
                    sid + "_deep", [global_dependencies, config.model, scene, fps, scene_context],
                    lambda: ask("deep:" + sid, deep_prompt, scene, fps),
                    lambda data: validate_scene(data, scene, min(0.5, 0.5 / fps)),
                    repair=repair_deep, attempts=config.attempts,
                )
                scene_claims = deep["claims"]
                if not scene_claims:
                    continue
                checkpoint("verify:" + sid)
                verification_prompt = (
                    "Independently rewatch the supplied video/audio, then evaluate EACH candidate claim. "
                    "Compare to global chronology and character identity; flag contradictions. Neither global "
                    "context nor candidate assertions are ground truth. Reject external canon, guessed words, "
                    "motives and off-screen causes; interpretation is not an observed fact. "
                    "supported means directly visible/audible at the stated source timestamp; uncertain includes "
                    "details not resolvable from this clip. Never increase certainty to satisfy a request. "
                    'JSON: {verdicts:[{id:string,status:"supported"|"uncertain"|"contradicted",confidence:number,reason:string}]}. '
                    "Relevant chronology (hypotheses, not proof): " + payload(scene_context) + "\nCandidates: " + payload(scene_claims)
                )
                # A short overlap prevents treating a cut or clipped sentence as independent evidence.
                span = {"start": max(0, scene["start"] - 1), "end": min(info["duration"], scene["end"] + 1)}
                verified = cache.stage(
                    sid + "_verify", [global_dependencies, config.verifier_model, scene_claims, scene_context, fps],
                    lambda: ask("verify:" + sid, verification_prompt, span, fps, True),
                    lambda data: validate_verdicts(data, scene_claims),
                )
                uncertain = [v["id"] for v in verified["verdicts"] if v["status"] == "uncertain"
                             or (v["status"] == "supported" and v["confidence"] < config.min_confidence)]
                if uncertain and rechecks < config.max_rechecks:
                    rechecks += 1
                    checkpoint("recheck:" + sid)
                    checked = cache.stage(
                        sid + "_recheck", [global_dependencies, config.verifier_model, verified, scene_claims, config.recheck_fps],
                        lambda: ask("recheck:" + sid, verification_prompt + "\nPreviously unclear IDs: " + payload(uncertain)
                                    + (". Rewatch at the same 1 FPS; Live cannot increase FPS. " if config.video_provider == "gemini_live" else ". Watch at higher FPS. ")
                                    + "Leave unresolved details uncertain.", span, config.recheck_fps, True),
                        lambda data: validate_verdicts(data, scene_claims),
                    )
                    replacements = {v["id"]: v for v in checked["verdicts"]}
                    verified = {"verdicts": [replacements[v["id"]] if v["id"] in uncertain else v for v in verified["verdicts"]]}
                claims.extend(scene_claims)
                verdicts.extend(verified["verdicts"])
            evidence = accepted_evidence(claims, verdicts, config.min_confidence)
            write_json(directory / "evidence.json", evidence)
            if not evidence["accepted"]:
                raise legacy.BotError("Không đủ bằng chứng được kiểm chứng; dừng, không bịa kịch bản.")
            if analyze_only:
                state.update(status="analyzed", stage="evidence")
                write_json(directory / "job.json", state)
                return directory / "evidence.json"
            checkpoint("characters")
            if config.video_provider == "gemini_live" or (config.identity_provider == "openai" and info["duration"] > 45):
                context = recognize_character_chunks(directory, source_hash, config, evidence, context,
                    scene_windows(detected, session_seconds(config) if config.video_provider == "gemini_live" else 45),
                    ask_window_identity, checkpoint)
            else:
                context = recognize_characters(cache, directory, source_hash, config, evidence, context, ask_identity)
            checkpoint("script")
            if regeneration_id and state.get("regeneration_id") == regeneration_id:
                regenerate_script = regenerate_voice = False
            if regenerate_script:
                state["script_revision"] = int(state.get("script_revision", 0)) + 1
            if regenerate_voice:
                # Keep analysis/script caches; remove only paid audio receipts downstream.
                for receipt in directory.glob("b*_voice.json"):
                    receipt.unlink()
                (directory / "render.json").unlink(missing_ok=True)
            if regeneration_id:
                state["regeneration_id"] = regeneration_id
            write_json(directory / "job.json", state)
            timed_source = info["duration"] if config.visual_mode == "continuous" and rights["allow_source_visuals"] else None
            review_seconds = max(20, math.ceil(timed_source)) if timed_source is not None else config.target_seconds
            script_deps = [evidence, config.writer_provider, writer_model, audit_model, review_seconds, timed_source,
                           state.get("script_revision", 0), PROMPT_VERSION, context]
            checkpoint("review_plan")
            plan = cache.stage("review_plan", script_deps,
                               lambda: ask_text("review_plan", plan_prompt(review_seconds, evidence, context, timed_source)),
                               lambda data: validate_plan(data, evidence, context))
            approved = read_json(directory / "script_approved.json")
            script_key = digest(script_deps)
            if approved and approved.get("key") == script_key:
                script = validate_review_script(approved["script"], evidence, context, review_seconds, timed_source)
                validate_review_audit(approved["audit"], script)
            repair = None
            saved_repair = read_json(directory / "script_repair.json")
            if saved_repair and saved_repair.get("key") == script_key:
                repair = saved_repair["feedback"]
            else:
                previous_script = read_json(directory / "script.json")
                previous_audit = read_json(directory / "script_audit.json")
                if previous_script and previous_audit:
                    previous = previous_script["data"]
                    expected = digest({"version": VERSION, "dependencies": [previous, evidence, context,
                                      config.writer_provider, audit_model, PROMPT_VERSION, timed_source]})
                    if previous_audit["key"] == expected:
                        try:
                            validate_review_audit(previous_audit["data"], previous)
                        except legacy.BotError:
                            repair = {"script": previous, "audit": previous_audit["data"]}
            resume_script = None
            if repair and "audit" in repair:
                try:
                    validate_audit_coverage(repair["audit"], repair["script"])
                except AuditCoverageError:
                    # A malformed reviewer response is not evidence that the
                    # already validated draft needs another paid writer call.
                    resume_script = validate_review_script(repair["script"], evidence, context, review_seconds, timed_source)
                    repair = None
            for attempt in range(0 if approved and approved.get("key") == script_key else 3):
                prompt = review_writer_prompt(review_seconds, evidence, context, global_data, plan, timed_source)
                if repair:
                    prompt = review_repair_prompt(repair, evidence, context, review_seconds, timed_source)
                    state["editorial_attempt"] = int(state.get("editorial_attempt", 0)) + 1
                    write_json(directory / "job.json", state)
                    repair = {**repair, "repair_mode": "targeted-v1", "editorial_attempt": state["editorial_attempt"]}
                try:
                    if resume_script is not None:
                        script, resume_script = resume_script, None
                    else:
                        script = cache.stage("script", [script_deps, repair],
                                             lambda: ask_text("script", prompt),
                                             lambda d: validate_review_script(d, evidence, context, review_seconds, timed_source))
                except legacy.BotError as exc:
                    response = read_json(directory / "script.response.json")
                    # Do not hide transport/quota failures as editorial retries.
                    if not response or response.get("key") != digest({"version": VERSION, "dependencies": [script_deps, repair]}):
                        raise
                    previous_audit = (repair or {}).get("audit") or (repair or {}).get("previous_audit")
                    repair = {"script": response["data"], "validation_error": str(exc)}
                    if previous_audit:
                        # Structural repairs must not forget why the prior draft
                        # failed its factual/editorial audit.
                        repair["previous_audit"] = previous_audit
                    write_json(directory / "script_repair.json", {"key": script_key, "feedback": repair})
                    if attempt == 2:
                        raise
                    LOG.warning("Review cần biên tập lại: %s", exc)
                    continue
                checkpoint("script_audit")
                audit = complete_review_audit(cache,
                    "script_audit", [script, evidence, context, config.writer_provider, audit_model, PROMPT_VERSION, timed_source],
                    script, review_audit_prompt(script, evidence, context, plan, timed_source), ask_text,
                    on_incomplete=lambda incomplete: write_json(directory / "script_repair.json",
                        {"key": script_key, "feedback": {"script": script, "audit": incomplete}}))
                try:
                    validate_review_audit(audit, script)
                    write_json(directory / "script_approved.json", {"key": script_key, "script": script, "audit": audit})
                    (directory / "script_repair.json").unlink(missing_ok=True)
                    break
                except legacy.BotError as exc:
                    repair = {"script": script, "audit": audit}
                    write_json(directory / "script_repair.json", {"key": script_key, "feedback": repair})
                    LOG.warning("Review cần biên tập lại: %s", exc)
                    if attempt == 2:
                        raise
            narration_path = directory / "narration_vi.txt"
            narration_path.write_text("\n\n".join(b["voiceover"] for b in script["beats"]) + "\n", encoding="utf-8")
            if script_only:
                state.update(status="script_ready", stage="script", title=script["title"], error=None)
                write_json(directory / "job.json", state)
                return narration_path
            checkpoint("render")
            legacy.materialize_railway_credentials()
            if not settings.overlay_logo.is_file():
                raise legacy.BotError("Thiếu logo; kiểm tra MOVIE_LOGO_FILE hoặc OVERLAY_LOGO_FILE.")
            legacy.ensure_dejavu_font()
            voice = tts or legacy.GoogleCloudVietnameseTTS(settings)
            for timing_attempt in range(3):
                checkpoint("render")
                try:
                    output = render_movie(source, directory, script, evidence, rights, settings, voice, client, config,
                                          movie_title=context["title"])
                    (directory / "narration_timing_error.json").unlink(missing_ok=True)
                    break
                except NarrationTimingError as exc:
                    write_json(directory / "narration_timing_error.json", {"key": script_key, "timing": exc.feedback})
                    if timing_attempt == 2:
                        raise
                    LOG.warning("Đang rút gọn lời bình theo audio đo được: %s", exc)
                    script = repair_narration_timing(directory, cache, script_key, script, exc.feedback,
                        evidence, context, review_seconds, timed_source, plan, ask_text, checkpoint,
                        ask_compact=lambda stage, prompt, limits: ask_text(stage, prompt, timing_word_limits=limits,
                            timing_claim_ids=compact_allowed_claims(script, exc.feedback, context)))
                    narration_path.write_text("\n\n".join(b["voiceover"] for b in script["beats"]) + "\n", encoding="utf-8")
            state.update(status="review", stage="complete", title=script["title"], output=str(output),
                         updated_at=datetime.now(UTC).isoformat(), error=None)
            write_json(directory / "job.json", state)
            LOG.info("Movie draft ready for human review: %s", output)
            return output
        except Exception as exc:
            state.update(status="failed", error=str(exc)[:2000], updated_at=datetime.now(UTC).isoformat())
            write_json(directory / "job.json", state)
            raise


def run_queued_movie(archive: legacy.Archive, row: dict) -> int:
    """The existing queue remains authoritative; movie jobs never auto-publish."""
    idea_id = int(row["id"])
    try:
        data = json.loads(row["idea"])
        directory = Path(data["directory"])
        if not Path(data["source"]).is_file():
            raise legacy.BotError("Không còn video gốc tại đường dẫn đã lưu. Kiểm tra Volume của service dashboard và BOT_DATA_DIR; tải lại đúng video vào job nếu dữ liệu đối chiếu còn, hoặc tạo job mới.")
        cfg = MovieConfig.from_env(int(data.get("target_seconds", 60)))
        visual_mode = data.get("visual_mode", cfg.visual_mode)
        if visual_mode not in {"continuous", "evidence"}:
            raise legacy.BotError("Chế độ dựng không hợp lệ.")
        cfg = replace(cfg, visual_mode=visual_mode)
        run_movie(Path(data["source"]), directory, cfg,
                  data.get("rights_status", "unknown"), data.get("rights_basis", ""),
                  regenerate_script=data.get("regenerate") == "script", regenerate_voice=data.get("regenerate") == "voice",
                  regeneration_id=str(idea_id), movie_title=data.get("movie_title"),
                  movie_context=data.get("movie_context"), narration_style=data.get("narration_style"),
                  context_file=Path(data["context_file"]) if data.get("context_file") else None)
        state = read_json(directory / "job.json")
        fresh = archive.get_idea(idea_id)
        if fresh and fresh["status"] == "processing":
            archive.update_idea(idea_id, "done", output_title=state["title"])
        return 0
    except Exception as exc:
        fresh = archive.get_idea(idea_id)
        if fresh and fresh["status"] == "processing":
            archive.update_idea(idea_id, "failed", error=str(exc)[:2000])
        raise
