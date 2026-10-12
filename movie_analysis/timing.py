"""Place measured TTS beats on the uncut source timeline without extreme slowing."""
from __future__ import annotations

import math

from youtube_shorts_bot import BotError


def narration_schedule(beats, claims, durations, source_seconds):
    if (not beats or len(beats) != len(durations) or not math.isfinite(source_seconds)
            or source_seconds <= 0 or any(not math.isfinite(d) or d <= 0 for d in durations)):
        raise BotError("Không lập được timeline lời bình từ thời lượng audio.")
    anchors = [0.0] + [float(claims[b["visual_claim_id"]]["start"]) for b in beats[1:]]
    # A closing take belongs near the end, rather than immediately after a short
    # concatenated summary. Leave room for the actual measured final sentence.
    anchors[-1] = max(anchors[-1], source_seconds - durations[-1] / .95)
    if (any(not math.isfinite(a) or not 0 <= a < source_seconds for a in anchors)
            or any(b < a for a, b in zip(anchors, anchors[1:]))):
        raise BotError("Timeline lời bình không theo thứ tự clip; viết lại lời bình với bằng chứng đúng thời điểm.")
    result = []
    first = 0
    while first < len(beats):
        stop = first + 1
        while stop < len(beats) and anchors[stop] == anchors[first]:
            stop += 1
        end = anchors[stop] if stop < len(beats) else source_seconds
        available = end - anchors[first]
        raw = sum(durations[first:stop])
        if raw > available * 1.2 + .03:
            raise BotError(f"Lời bình {beats[first]['id']} cần {raw:.1f}s nhưng cảnh chỉ có {available:.1f}s. "
                           "Viết lại đoạn này ngắn hơn; giọng đọc đã tạo được giữ để tiếp tục.")
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
