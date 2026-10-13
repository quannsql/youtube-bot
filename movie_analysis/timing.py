"""Place measured TTS beats on the uncut source timeline without extreme slowing."""
from __future__ import annotations

import math

from youtube_shorts_bot import BotError


class NarrationTimingError(BotError):
    """A measured speech window needs editing, rather than another TTS retry."""

    def __init__(self, message, feedback):
        super().__init__(message)
        self.feedback = feedback


def narration_schedule(beats, claims, durations, source_seconds):
    if (not beats or len(beats) != len(durations) or not math.isfinite(source_seconds)
            or source_seconds <= 0 or any(not math.isfinite(d) or d <= 0 for d in durations)):
        raise BotError("Không lập được timeline lời bình từ thời lượng audio.")
    anchors = [0.0] + [float(claims[b["visual_claim_id"]]["start"]) for b in beats[1:]]
    if (any(not math.isfinite(a) or not 0 <= a < source_seconds for a in anchors)
            or any(b < a for a, b in zip(anchors, anchors[1:]))):
        raise BotError("Timeline lời bình không theo thứ tự clip; viết lại lời bình với bằng chứng đúng thời điểm.")
    closing_replacement = None
    closing_allowed = None
    # A final half-second shot cannot host a full conclusion. Reserve an outro
    # window, but require an audited rewrite using earlier observations rather
    # than reading a future event before it appears.
    tail = source_seconds - anchors[-1]
    if (beats[-1].get("role") == "closing" and tail < 3 and durations[-1] > tail * 1.2 + .03):
        budget = min(20.0, max(10.0, source_seconds * .08))
        closing_start = max(anchors[-2] if len(anchors) > 1 else 0,
                            source_seconds - min(budget, durations[-1] / .95))
        used_refs = {ref for beat in beats for sentence in beat.get("sentences", [])
                     for ref in sentence.get("claim_ids", [])}
        earlier = [cid for cid in used_refs if cid in claims and claims[cid]["end"] <= closing_start]
        if not earlier:
            earlier = [cid for cid in claims if claims[cid]["end"] <= closing_start]
        if earlier and closing_start < anchors[-1]:
            visuals = [cid for cid in earlier if claims[cid].get("modality") in {"visual", "both"}]
            closing_replacement = max(visuals, key=lambda cid: (claims[cid]["start"], cid)) if visuals else None
            # Keep the replacement in chronological order with the last
            # development visual; narration itself still waits until the outro.
            if closing_replacement and claims[closing_replacement]["start"] >= (anchors[-2] if len(anchors) > 1 else 0):
                closing_allowed = sorted(earlier)
                anchors[-1] = closing_start
            else:
                closing_replacement = None
    # A closing take belongs near the end, rather than immediately after a short
    # concatenated summary. Leave room for the actual measured final sentence.
    if closing_replacement is None:
        anchors[-1] = max(anchors[-1], source_seconds - durations[-1] / .95)
    if (any(not math.isfinite(a) or not 0 <= a < source_seconds for a in anchors)
            or any(b < a for a, b in zip(anchors, anchors[1:]))):
        raise BotError("Timeline lời bình không theo thứ tự clip; viết lại lời bình với bằng chứng đúng thời điểm.")
    result = []
    failures = []
    first = 0
    while first < len(beats):
        stop = first + 1
        while stop < len(beats) and anchors[stop] == anchors[first]:
            stop += 1
        end = anchors[stop] if stop < len(beats) else source_seconds
        available = end - anchors[first]
        raw = sum(durations[first:stop])
        rewrite_closing = closing_replacement is not None and stop == len(beats)
        if raw > available * 1.2 + .03 or rewrite_closing:
            # Estimate a safe word cap from this voice's measured duration,
            # leaving space instead of relying on extreme audio acceleration.
            affected = []
            for beat, seconds in zip(beats[first:stop], durations[first:stop]):
                spoken = beat.get("voiceover") or " ".join(s["text"] for s in beat["sentences"])
                words = len(spoken.split())
                affected.append({"id": beat["id"], "visual_claim_id": beat["visual_claim_id"],
                                 "measured_voice_seconds": seconds, "word_count": words,
                                 "maximum_words": min(words, max(1, math.floor(words * available * .92 / raw)))})
                if rewrite_closing and beat["id"] == beats[-1]["id"]:
                    affected[-1].update(rewrite_as_closing_summary=True,
                        replacement_visual_claim_id=closing_replacement, allowed_claim_ids=closing_allowed)
            failures.append({"window_start": anchors[first], "window_end": end,
                             "available_seconds": available, "raw_voice_seconds": raw, "beats": affected})
        tempo = min(1.2, max(.9, raw / (available * .96)))
        gap = max(0.0, available - raw / tempo) / (stop - first)
        cursor = anchors[first]
        for i in range(first, stop):
            spoken = durations[i] / tempo
            result.append({"beat": beats[i]["id"], "start": cursor, "end": cursor + spoken,
                           "tempo": tempo, "raw_voice_seconds": durations[i],
                           "visual_claim_id": beats[i]["visual_claim_id"],
                           "anchor": anchors[first], "window_end": end})
            cursor += spoken + gap
        first = stop
    if failures:
        first_failure = failures[0]
        raise NarrationTimingError(
            f"Lời bình {first_failure['beats'][0]['id']} cần {first_failure['raw_voice_seconds']:.1f}s "
            f"nhưng cảnh chỉ có {first_failure['available_seconds']:.1f}s. "
            f"Cần sửa {sum(len(w['beats']) for w in failures)} đoạn theo thời lượng; giọng đọc đã tạo được giữ để tiếp tục.",
            {"source_seconds": source_seconds, "window_start": first_failure["window_start"],
             "window_end": first_failure["window_end"], "available_seconds": first_failure["available_seconds"],
             "beats": [b for window in failures for b in window["beats"]], "windows": failures,
             "timing_version": "batch-closing-v2"})
    return result


def align_narration(paths, schedule, output, seconds, run):
    command = ["ffmpeg", "-y"]
    filters = []
    for i, (path, slot) in enumerate(zip(paths, schedule)):
        command += ["-i", str(path)]
        filters.append(f"[{i}:a]asetpts=PTS-STARTPTS,atempo={slot['tempo']:.9f},"
                       f"adelay={round(slot['start'] * 1000)}:all=1,apad[v{i}]")
    filters.append("".join(f"[v{i}]" for i in range(len(paths)))
                   + f"amix=inputs={len(paths)}:normalize=0:duration=longest[a]")
    run(command + ["-filter_complex", ";".join(filters), "-map", "[a]", "-t", str(seconds),
                   "-c:a", "aac", "-b:a", "192k", str(output)], timeout_seconds=max(180, seconds * 2))
