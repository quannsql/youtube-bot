"""Recognize a sourced film roster in video, then independently check identities."""
from __future__ import annotations

import copy
import json
import os

from youtube_shorts_bot import BotError
from .core import number, validate_verdicts, write_json

IDENTITY_VERSION = "identity-v6"
CUES = {"face", "voice", "explicit_name", "distinctive_costume", "setting", "relationship"}


def identity_frames(source, directory, evidence):
    """Bounded still-image samples; never claim they include audio/all frames."""
    import youtube_shorts_bot as legacy
    times = set()
    for claim in evidence["accepted"]:
        if claim.get("modality") != "visual":
            continue
        start, end = claim["start"], claim["end"]
        times.update((round(start, 3), round((start + end) / 2, 3), round(max(start, end - .15), 3)))
    times = sorted(times)
    if len(times) > 24:
        times = [times[round(i * (len(times) - 1) / 23)] for i in range(24)]
    if not times:
        raise BotError("Không có bằng chứng hình ảnh để nhận diện bằng OpenAI.")
    frames = directory / "identity_frames"
    frames.mkdir(exist_ok=True)
    paths = []
    for timestamp in times:
        path = frames / f"frame_{timestamp:09.3f}s.jpg"
        if not path.is_file():
            legacy.run(["ffmpeg", "-y", "-ss", str(timestamp), "-i", str(source), "-frames:v", "1",
                        "-vf", "scale=768:768:force_original_aspect_ratio=decrease", "-q:v", "3", str(path)], timeout_seconds=30)
        if not path.is_file() or path.stat().st_size == 0:
            raise BotError("Không trích được khung hình để nhận diện nhân vật.")
        paths.append(path)
    return paths


def identity_schema(stage):
    obj = lambda props: {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}
    string = {"type": "string"}
    if stage == "character_identity":
        item = obj({"character_id": string, "claim_ids": {"type": "array", "items": string},
                    "confidence": {"type": "number"}, "cues": {"type": "array", "items": {"type": "string", "enum": sorted(CUES)}},
                    "reason": string})
        return obj({"matches": {"type": "array", "items": item}})
    if stage == "character_identity_verify":
        item = obj({"id": string, "status": {"type": "string", "enum": ["supported", "uncertain", "contradicted"]},
                    "confidence": {"type": "number"}, "reason": string})
        return obj({"verdicts": {"type": "array", "items": item}})
    raise BotError("Stage nhận diện không hợp lệ.")


def validate_matches(data, evidence, context):
    matches = data.get("matches") if isinstance(data, dict) else None
    profiles = {p["id"] for p in context["characters"]}
    observed = {c["id"] for c in evidence["accepted"]}
    if not isinstance(matches, list) or len(matches) > len(profiles):
        raise BotError("Nhận diện nhân vật cần matches hợp lệ.")
    ids = set()
    for match in matches:
        if not isinstance(match, dict) or match.get("character_id") not in profiles or match["character_id"] in ids:
            raise BotError("Nhận diện nhân vật trả ID thiếu nguồn hoặc trùng.")
        ids.add(match["character_id"])
        refs = match.get("claim_ids")
        if not isinstance(refs, list) or not refs or any(ref not in observed for ref in refs):
            raise BotError("Nhận diện nhân vật cần bằng chứng trong clip.")
        cues = match.get("cues")
        if not isinstance(cues, list) or any(c not in CUES for c in cues):
            raise BotError("Dấu hiệu nhận diện nhân vật không hợp lệ.")
        match["confidence"] = number(match.get("confidence"), "identity confidence")
        if not 0 <= match["confidence"] <= 1 or not isinstance(match.get("reason"), str) or not match["reason"].strip():
            raise BotError("Nhận diện nhân vật thiếu độ tin cậy/lý do.")
    return data


def recognize_characters(cache, directory, source_hash, config, evidence, context, ask):
    result = copy.deepcopy(context)
    result["recognized_characters"] = []
    if not context["characters"]:
        return result
    identity_model = config.identity_model or ((config.writer_model or os.getenv("OPENAI_TEXT_MODEL", "gpt-5.4-mini"))
                     if config.identity_provider == "openai" else config.model)
    # Delivery aliases must not bias recognition or invalidate paid identity passes when edited.
    roster = [{key: p[key] for key in ("id", "name", "recognition_notes", "context_ids")} for p in context["characters"]]
    dependencies = [source_hash, config.video_provider, config.identity_provider, identity_model, config.verifier_model, config.scene_fps,
                    evidence, roster, context["facts"], IDENTITY_VERSION]
    prompt = (
        "Independently inspect the video/audio and match any clearly recognizable characters to the "
        "supplied sourced roster. Film/actor recognition may help, but corroborate with visible/audible "
        "identity cues in this clip; do not require a name to be spoken. If citing face, describe the "
        "specific recognizable facial features or actor likeness; a partially obscured bald profile "
        "with a wheelchair/plaque must not be relabelled as face recognition. A bald man/wheelchair alone, a cast list, generic "
        "armour alone, or a name on a nearby school cannot. Consider lookalikes and fan edits. "
        "Evaluate identity using all available views across the clip, not one shadowed frame. A known "
        "film character can be recognized without hearing their name. Clearly distinctive costume/design "
        "with consistent visible features and context can support a match even if the face is hidden. "
        "If the face/mask/voice remains ambiguous, do not force a match. Roster membership is not presence. "
        "Use only OBSERVATIONS' accepted sNNN_ clip IDs for claim_ids, NEVER ctx_ context IDs. Report confidence, concrete cues and alternatives "
        "in reason. Do not infer plot or speaker attribution from an off-screen voice alone. "
        'JSON: {matches:[{character_id:string,claim_ids:[string],confidence:number,cues:['
        '"face"|"voice"|"explicit_name"|"distinctive_costume"|"setting"|"relationship"],reason:string}]}. '
        + "\nROSTER: " + json.dumps(roster, ensure_ascii=False)
        + "\nCONTEXT: " + json.dumps(context["facts"], ensure_ascii=False)
        + "\nOBSERVATIONS: " + json.dumps(evidence["accepted"], ensure_ascii=False)
    )
    if config.identity_provider == "openai":
        prompt += "\nYou receive sampled timestamped still images, not video/audio. Do not use voice cues. Inspect visible identity and mark uncertainty where samples are insufficient."
    candidates = cache.stage("character_candidates", dependencies,
                             lambda: ask("character_identity", prompt, fps=config.scene_fps),
                             lambda data: validate_matches(data, evidence, context))
    matches = candidates["matches"]
    verdicts = {"verdicts": []}
    if matches:
        verification_prompt = (
            "Rewatch the video independently to verify EACH proposed character identity. Reject a "
            "match based only on roster membership, a bald head/wheelchair, a nearby plaque or generic "
            "armour. Evaluate face/voice/distinctive design, scene consistency and plausible alternatives. "
            "Do not repeat the candidate's confidence automatically. supported needs strong direct identity "
            "cues, uncertain means naming is not justified. Identity does not establish guilt, motives or "
            "whose voiceover is speaking. Verdict id must equal character_id. "
            'JSON: {verdicts:[{id:string,status:"supported"|"uncertain"|"contradicted",confidence:number,reason:string}]}. '
            + "\nROSTER: " + json.dumps(roster, ensure_ascii=False)
            + "\nOBSERVATIONS: " + json.dumps(evidence["accepted"], ensure_ascii=False)
            + "\nCANDIDATES: " + json.dumps(matches, ensure_ascii=False)
        )
        if config.identity_provider == "openai":
            verification_prompt += "\nYou receive sampled timestamped stills only. They contain no audio. Reject voice-only or unsampled assertions."
        verdicts = cache.stage("character_verification", [dependencies, candidates],
                              lambda: ask("character_identity_verify", verification_prompt, fps=config.scene_fps, verify=True),
                              lambda data: validate_verdicts(data, [{"id": m["character_id"]} for m in matches]))
    by_id = {v["id"]: v for v in verdicts["verdicts"]}
    profiles = {p["id"]: p for p in context["characters"]}
    accepted, rejected = [], []
    for match in matches:
        v = by_id[match["character_id"]]
        cues = set(match["cues"])
        distinctive = ("explicit_name" in cues or
                       (len(cues) >= 2 and bool(cues & {"face", "voice", "distinctive_costume"})))
        valid = distinctive and v["status"] == "supported" and min(match["confidence"], v["confidence"]) >= 0.92
        if config.identity_provider == "openai" and "voice" in cues:
            valid = False
        item = {**match, "verification": v, "profile": profiles[match["character_id"]]}
        (accepted if valid else rejected).append(item)
        if valid:
            profile = item["profile"]
            cid = "ctx_identity_" + profile["id"].removeprefix("char_")
            result["recognized_characters"].append({"id": profile["id"], "name": profile["name"],
                                                    "spoken_name": profile["spoken_name"], "identity_claim_id": cid,
                                                    "playful_names": profile.get("playful_names", []),
                                                    "claim_ids": match["claim_ids"]})
            result["facts"].append({"id": cid,
                "text": f'Nhân vật trong các bằng chứng {", ".join(match["claim_ids"])} được nhận diện là {profile["name"]} ({profile["spoken_name"]}). {v["reason"]}',
                "source": "video_identity_verified", "claim_ids": match["claim_ids"], "context_ids": profile["context_ids"]})
    write_json(directory / "characters.json", {"accepted": accepted, "rejected": rejected,
                                               "provider": config.identity_provider, "model": identity_model,
                                               "limitation": "AI identity estimates; independent agreement does not guarantee correctness"})
    return result
