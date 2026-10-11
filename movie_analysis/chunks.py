"""Bounded Live sessions; source timestamps and cached progress survive retries."""
from __future__ import annotations

import json
import math

from youtube_shorts_bot import BotError
from .core import validate_global, write_json

CHUNK_VERSION = "chunks-v1"


def session_seconds(config):
    # Reserve space for instructions/schema/output as well as media. A lower
    # project TPM automatically produces shorter sessions, never higher FPS.
    available = int(config.live_tpm * .8) - config.max_output_tokens - 4096 - 12000
    seconds = min(60, config.live_chunk_seconds, math.floor(available / 562))
    if seconds < 5:
        raise BotError("MOVIE_LIVE_TPM quá thấp cho phiên phân tích; kiểm tra quota/cấu hình.")
    return float(seconds)


def windows(seconds, limit):
    count = math.ceil(seconds / limit)
    return [{"id": f"c{i + 1:03d}", "start": round(i * limit, 6),
             "end": round(min(seconds, (i + 1) * limit), 6)} for i in range(count)]


def scene_windows(scenes, limit):
    """Group full scenes, so an identity never cites unseen halves of a claim."""
    result = []
    for scene in scenes:
        if result and scene["end"] - result[-1]["start"] <= limit:
            result[-1]["end"] = scene["end"]
        else:
            result.append({"id": f"c{len(result) + 1:03d}", "start": scene["start"], "end": scene["end"]})
    return result


def compact_claims(claims):
    # Independent verification already lives in the evidence ledger. Repeating
    # its prose in each prompt wastes TPM without adding observable information.
    return [{k: c[k] for k in ("id", "start", "end", "text", "evidence", "modality")} for c in claims]


def validate_chunk(data, part):
    data = validate_global(data, part["end"], .5, part["start"])
    for character in data.get("characters", []):
        if not isinstance(character, dict) or not isinstance(character.get("label"), str) or not character["label"].strip():
            raise BotError("Global chunk trả nhãn nhân vật không hợp lệ.")
    for key in ("angles", "uncertainties"):
        values = data.get(key, [])
        if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
            raise BotError(f"Global chunk {key} phải là danh sách chuỗi.")
    return data


def analyze_chunks(cache, directory, info, config, dependencies, ask, checkpoint):
    parts = windows(info["duration"], session_seconds(config))
    write_json(directory / "analysis_chunks.json", {"version": CHUNK_VERSION, "chunks": parts})
    results, labels = [], []
    for part in parts:
        checkpoint("global:" + part["id"])
        prompt = (
            f"Analyze ONLY source seconds {part['start']}–{part['end']}, including sound. "
            "Give chronology, setting, neutral character labels, directly demonstrated relationships, "
            "intelligible dialogue and grounded analysis angles. Do not infer unseen plot. "
            "Return at most 12 events, 12 dialogue lines, 12 characters and 6 angles. "
            "Keep each description under 240 characters, each character label/description under 160 characters "
            "and summary under 900 characters. Timestamps are ABSOLUTE original-video seconds, never reset to zero. "
            'JSON: {summary:string,characters:[{label:string,description:string,name_evidence:string}],'
            'events:[{start:number,end:number,description:string}],dialogue:[{start:number,end:number,'
            'speaker:string,text:string,uncertain:boolean}],angles:[string],uncertainties:[string]}. '
            f"Audio present: {info['has_audio']}. Every event/dialogue MUST stay within "
            f"{part['start']}–{part['end']}s. Earlier labels are hypotheses; reuse a label only if "
            "the same visible person is identifiable, otherwise create a distinct label: "
            + json.dumps(labels, ensure_ascii=False)
        )
        deps = [dependencies, CHUNK_VERSION, part, labels]

        def produce():
            feedback = None
            for attempt in range(config.attempts):
                corrected = prompt + ("\nCorrect the previous analysis error: " + feedback if feedback else "")
                raw = cache.stage(f"global_{part['id']}_attempt_{attempt + 1}", [deps, feedback],
                                  lambda: ask("global:" + part["id"], corrected, part))
                try:
                    return validate_chunk(raw, part)
                except BotError as exc:
                    feedback = str(exc)
                    if attempt + 1 == config.attempts:
                        raise

        result = cache.stage("global_" + part["id"], deps, produce,
                             lambda data: validate_chunk(data, part))
        results.append(result)
        for character in result.get("characters", []):
            if character not in labels:
                labels.append(character)
        # Only the recent label descriptions accompany the next media window;
        # the merged chronology retains all observations separately.
        labels = labels[-24:]
    merged = {"summary": "\n".join(f"{p['start']:g}–{p['end']:g}s: {r['summary']}" for p, r in zip(parts, results))}
    for key in ("characters", "events", "dialogue", "angles", "uncertainties"):
        merged[key] = []
        for result in results:
            for item in result.get(key, []):
                if item not in merged[key]:
                    merged[key].append(item)
    for key in ("events", "dialogue"):
        merged[key].sort(key=lambda item: (item["start"], item["end"]))
    return validate_global(merged, info["duration"])
