from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

import youtube_shorts_bot as legacy
from .core import MovieConfig, digest, file_digest, read_json, write_json


def enable_local_tools():
    if not shutil.which("ffmpeg"):
        for exe in (legacy.ROOT / ".tools").glob("**/ffmpeg.exe"):
            if exe.with_name("ffprobe.exe").is_file():
                os.environ["PATH"] = str(exe.parent) + os.pathsep + os.environ.get("PATH", "")
                break


def probe(path: Path) -> dict:
    result = subprocess.run(["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
                            capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise legacy.BotError(f"Không đọc được media: {path.name}")
    return json.loads(result.stdout)


def duration(path: Path) -> float:
    value = float(probe(path)["format"]["duration"])
    if not math.isfinite(value) or value <= 0:
        raise legacy.BotError(f"Media không có duration hợp lệ: {path.name}")
    return value


def inspect_source(source: Path, config: MovieConfig) -> dict:
    if source.suffix.lower() != ".mp4" or not source.is_file():
        raise legacy.BotError("Video đầu vào phải là file MP4 có thật.")
    if source.stat().st_size > config.max_input_mb * 1024 * 1024:
        raise legacy.BotError("MP4 vượt MOVIE_MAX_INPUT_MB.")
    data = probe(source)
    videos = [s for s in data["streams"] if s["codec_type"] == "video" and not s.get("disposition", {}).get("attached_pic")]
    if not videos or videos[0].get("width", 0) < 16:
        raise legacy.BotError("MP4 không có video stream hợp lệ.")
    seconds = duration(source)
    if seconds > config.max_input_seconds:
        raise legacy.BotError("Clip vượt MOVIE_MAX_INPUT_SECONDS; cắt ngắn hoặc cấu hình lại giới hạn.")
    return {"duration": seconds, "width": videos[0]["width"], "height": videos[0]["height"],
            "has_audio": any(s["codec_type"] == "audio" for s in data["streams"])}


def build_scenes(seconds: float, cuts: list[float], max_seconds: float, max_scenes: int) -> list[dict]:
    # Group rapid shot changes into analyzable events, while guaranteeing full coverage.
    cuts = sorted({float(t) for t in cuts if math.isfinite(float(t)) and 0 < float(t) < seconds})
    bounds, cursor = [0.0], 0.0
    while seconds - cursor > max_seconds:
        candidates = [c for c in cuts if cursor + max_seconds * 0.4 <= c <= cursor + max_seconds]
        cursor = max(candidates) if candidates else cursor + max_seconds
        bounds.append(cursor)
    bounds.append(seconds)
    if len(bounds) - 1 > max_scenes:
        raise legacy.BotError("Quá nhiều scene cho MOVIE_MAX_SCENES; dừng trước API để giữ ngân sách.")
    scenes = []
    for i, (start, end) in enumerate(zip(bounds, bounds[1:])):
        shots = [t for t in cuts if start < t < end]
        scenes.append({"id": f"s{i + 1:03d}", "start": round(start, 3), "end": round(end, 3),
                       "shot_cuts": shots, "fast_action": len(shots) >= 3})
    return scenes


def detect_scenes(source: Path, info: dict, config: MovieConfig) -> list[dict]:
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", str(source), "-an", "-vf",
         "scale=320:-2,select='gt(scene,0.32)',showinfo", "-f", "null", "-"],
        capture_output=True, text=True, timeout=300,
    )
    if result.returncode:
        raise legacy.BotError("FFmpeg không phân tích được shot cuts.")
    cuts = [float(t) for t in re.findall(r"pts_time:([\d.]+)", result.stderr)]
    return build_scenes(info["duration"], cuts, config.max_scene_seconds, config.max_scenes)


def graphic(beat: dict, claim: dict, title: str, destination: Path, source_allowed: bool):
    """Original typography/diagram; does not impersonate movie evidence."""
    canvas = Image.new("RGB", (1080, 1920), "#101828")
    draw = ImageDraw.Draw(canvas)
    font_path = legacy.ROOT / "fonts" / "DejaVuSans.ttf"
    font = lambda size: ImageFont.truetype(str(font_path), size)

    def paragraph(text, top, size, color, width=920, max_lines=5):
        face = font(size)
        lines, current = [], ""
        for word in text.split():
            candidate = (current + " " + word).strip()
            if current and draw.textlength(candidate, font=face) > width:
                lines.append(current)
                current = word
            else:
                current = candidate
        if current:
            lines.append(current)
        if len(lines) > max_lines and size > 28:
            return paragraph(text, top, size - 4, color, width, max_lines)
        for line in lines[:max_lines]:
            draw.text((80, top), line, font=face, fill=color)
            top += size * 1.35
        return top

    draw.rounded_rectangle((60, 70, 1020, 148), radius=25, fill="#284a66")
    draw.text((85, 85), "GÓC NHÌN ĐIỆN ẢNH", font=font(34), fill="white")
    paragraph(beat["headline"], 270, 54, "#ffffff", max_lines=2)
    stamp = f'Bằng chứng: {claim["start"]:.1f}s – {claim["end"]:.1f}s'
    draw.text((80, 1290), stamp, font=font(32), fill="#73d7ee")
    if not source_allowed:
        draw.rounded_rectangle((65, 480, 1015, 1200), radius=38, fill="#1d3146")
        draw.ellipse((460, 530, 620, 690), fill="#ffcc80")
        draw.line((540, 690, 540, 835), fill="#73d7ee", width=12)
        draw.line((540, 750, 390, 830), fill="#73d7ee", width=12)
        draw.line((540, 750, 690, 810), fill="#73d7ee", width=12)
        draw.line((540, 835, 435, 925), fill="#73d7ee", width=12)
        draw.line((540, 835, 645, 925), fill="#73d7ee", width=12)
        paragraph(claim["text"], 970, 38, "white", max_lines=4)
    paragraph("Phân tích & bình luận • " + title, 1400, 32, "#a6b8cc", max_lines=3)
    draw.text((80, 1810), "Minh họa tự tạo" if not source_allowed else "Cảnh có khai báo quyền sử dụng", font=font(28), fill="#a6b8cc")
    canvas.save(destination)


def audio_ready(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        return duration(path) > 0.15
    except (legacy.BotError, ValueError, KeyError):
        return False


def continuous_media(source: Path, directory: Path, narration: Path, seconds: float, source_seconds: float):
    """Play the entire source once at its original speed; hold its end if voice runs longer."""
    visuals = directory / "visuals.mp4"
    hold = max(0.0, seconds - source_seconds)
    # Preserve the complete frame in the portrait canvas; no crop, scene seek or replay.
    filters = ("setpts=PTS-STARTPTS,"
               "scale=1080:1920:force_original_aspect_ratio=decrease,"
               "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=0x101828,setsar=1,fps=30,"
               f"tpad=stop_mode=clone:stop_duration={hold}")
    legacy.run(["ffmpeg", "-y", "-i", str(source), "-map", "0:v:0", "-an", "-vf", filters,
                "-t", str(seconds), "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-pix_fmt", "yuv420p", str(visuals)], timeout_seconds=600)
    mixed = directory / "review_audio.m4a"
    has_audio = any(s["codec_type"] == "audio" for s in probe(source)["streams"])
    command = ["ffmpeg", "-y", "-i", str(narration)]
    if has_audio:
        command += ["-i", str(source), "-filter_complex",
                    f"[0:a]asetpts=PTS-STARTPTS,apad[voice];"
                    f"[1:a:0]asetpts=PTS-STARTPTS,atrim=duration={source_seconds},volume=0.12,apad[background];"
                    "[voice][background]amix=inputs=2:duration=longest:normalize=0,alimiter=limit=0.95[a]",
                    "-map", "[a]"]
    else:
        command += ["-map", "0:a:0", "-af", "apad"]
    legacy.run(command + ["-t", str(seconds), "-c:a", "aac", "-b:a", "192k", str(mixed)], timeout_seconds=180)
    return visuals, mixed


def render_movie(source: Path, directory: Path, script: dict, evidence: dict, rights: dict,
                 settings: legacy.Settings, tts, provider, config: MovieConfig) -> Path:
    claims = {c["id"]: c for c in evidence["accepted"]}
    key = digest({"script": script, "evidence": evidence, "rights": rights,
                  "voice": settings.google_tts_vietnamese_voice, "rate": settings.google_tts_vietnamese_speaking_rate,
                  "logo": file_digest(settings.overlay_logo), "source": file_digest(source),
                  "visual_mode": config.visual_mode, "render": 3})
    continuous = config.visual_mode == "continuous" and rights["allow_source_visuals"]
    source_seconds = duration(source) if continuous else 0.0
    output = directory / "movie_review.mp4"
    cached = read_json(directory / "render.json")
    if cached and cached.get("key") == key and output.is_file() and cached.get("sha256") == file_digest(output):
        quality_check(output, cached["duration"], directory / "captions_vi.ass")
        return output
    clips, audio_parts, cues, timeline = [], [], [], []
    cursor = 0.0
    for beat in script["beats"]:
        bid = beat["id"]
        narration = directory / f"{bid}_voice.mp3"
        audio_key = digest([beat["voiceover"], settings.google_tts_vietnamese_voice, settings.google_tts_vietnamese_speaking_rate])
        receipt = read_json(directory / f"{bid}_voice.json")
        if not (receipt and receipt.get("key") == audio_key and audio_ready(narration)
                and receipt.get("sha256") == file_digest(narration)):
            last_error = None
            for attempt in range(config.attempts):
                provider.record_tts(bid, len(beat["voiceover"]), "requested")
                temporary_audio = directory / f"{bid}_voice.tmp.mp3"
                try:
                    tts.speech(beat["voiceover"], temporary_audio)
                    if not audio_ready(temporary_audio):
                        raise legacy.BotError("TTS trả audio không hợp lệ.")
                    os.replace(temporary_audio, narration)
                    last_error = None
                    break
                except legacy.BotError as exc:
                    last_error = exc
                    # Retry transport failures; missing credentials/dependencies need user config.
                    if "Thiếu" in str(exc) or attempt + 1 == config.attempts:
                        raise
            if last_error:
                raise last_error
            write_json(directory / f"{bid}_voice.json", {"key": audio_key, "sha256": file_digest(narration)})
        seconds = duration(narration)
        if cursor + seconds > 180:
            raise legacy.BotError("Lời đọc vượt 180s; tạo lại kịch bản ngắn hơn, audio đã tạo được giữ.")
        audio_parts.append(narration)
        for cue in legacy.caption_cues_from_text(beat["voiceover"], seconds):
            cues.append(legacy.CaptionCue(start=cue.start + cursor, end=cue.end + cursor, text=cue.text))
        claim = claims[beat["visual_claim_id"]]
        if continuous:
            timeline.append({"beat": bid, "start": cursor, "end": cursor + seconds,
                             "visual_claim_id": claim["id"], "source_start": min(cursor, source_seconds),
                             "source_end": min(cursor + seconds, source_seconds),
                             "visual_type": "source_continuous" if cursor + seconds <= source_seconds else
                                            "held_frame" if cursor >= source_seconds else "source_continuous_then_hold"})
            cursor += seconds
            continue
        card, clip = directory / f"{bid}.png", directory / f"{bid}.mp4"
        graphic(beat, claim, script["title"], card, rights["allow_source_visuals"])
        clip_key = digest([key, bid, seconds])
        receipt = read_json(directory / f"{bid}_clip.json")
        if not (receipt and receipt.get("key") == clip_key and audio_ready(clip)
                and receipt.get("sha256") == file_digest(clip)):
            command = ["ffmpeg", "-y", "-loop", "1", "-framerate", "30", "-i", str(card)]
            if rights["allow_source_visuals"]:
                excerpt = directory / f"{bid}_excerpt.mp4"
                # Isolate the exact evidence window before replaying it; never loop the whole source.
                legacy.run(["ffmpeg", "-y", "-ss", str(claim["start"]), "-i", str(source),
                            "-t", str(claim["end"] - claim["start"]), "-map", "0:v:0", "-an",
                            "-vf", "scale=960:760:force_original_aspect_ratio=decrease,pad=960:760:(ow-iw)/2:(oh-ih)/2,fps=30,setsar=1",
                            "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", str(excerpt)], timeout_seconds=180)
                command += ["-stream_loop", "-1", "-i", str(excerpt), "-filter_complex", "[0:v][1:v]overlay=60:460:shortest=1[v]", "-map", "[v]"]
            else:
                command += ["-vf", "scale=1100:1956,crop=1080:1920:x='10+5*sin(t)':y=18"]
            command += ["-an", "-t", str(seconds), "-r", "30", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", str(clip)]
            legacy.run(command, timeout_seconds=300)
            write_json(directory / f"{bid}_clip.json", {"key": clip_key, "sha256": file_digest(clip)})
        clips.append(clip)
        timeline.append({"beat": bid, "start": cursor, "end": cursor + seconds,
                         "visual_claim_id": claim["id"], "source_start": claim["start"], "source_end": claim["end"],
                         "visual_type": "source_excerpt" if rights["allow_source_visuals"] else "original_graphic"})
        cursor += seconds
    # Use relative, controlled filenames: concat quoting remains safe for any user source path.
    for name, paths in (("visuals", clips), ("voice", audio_parts)):
        (directory / f"{name}.txt").write_text("".join(f"file '{p.name}'\n" for p in paths), encoding="utf-8")
    visuals, narration = directory / "visuals.mp4", directory / "narration_vi.mp3"
    if not continuous:
        legacy.run(["ffmpeg", "-y", "-f", "concat", "-safe", "1", "-i", str(directory / "visuals.txt"), "-c", "copy", str(visuals)], timeout_seconds=300)
    legacy.run(["ffmpeg", "-y", "-f", "concat", "-safe", "1", "-i", str(directory / "voice.txt"), "-c", "copy", str(narration)], timeout_seconds=120)
    voice_seconds = cursor
    if continuous:
        cursor = max(source_seconds, voice_seconds)
        visuals, narration = continuous_media(source, directory, narration, cursor, source_seconds)
    captions = directory / "captions_vi.ass"
    legacy.write_ass_captions(cues, captions)
    write_json(directory / "timeline.json", timeline)
    temporary = directory / "movie_review.tmp.mp4"
    legacy.mux_video_audio_with_captions(visuals, narration, captions, temporary, cursor, settings, timeout_seconds=600)
    report = quality_check(temporary, cursor, captions)
    os.replace(temporary, output)
    write_json(directory / "quality.json", report)
    write_json(directory / "render.json", {"key": key, "sha256": file_digest(output), "duration": cursor,
               "visual_mode": "continuous" if continuous else "evidence" if rights["allow_source_visuals"] else "graphics",
               "source_duration": source_seconds if continuous else None, "voice_duration": voice_seconds,
               "held_end_seconds": max(0, voice_seconds - source_seconds) if continuous else 0,
               "source_audio_volume": 0.12 if continuous else 0})
    legacy.run(["ffmpeg", "-y", "-ss", str(min(1, cursor / 2)), "-i", str(output), "-frames:v", "1", str(directory / "preview.jpg")], timeout_seconds=60)
    return output


def quality_check(video: Path, expected: float, captions: Path) -> dict:
    info = probe(video)
    streams = info["streams"]
    v = next((s for s in streams if s["codec_type"] == "video"), {})
    audio = next((s for s in streams if s["codec_type"] == "audio"), {})
    seconds = duration(video)
    if (v.get("width"), v.get("height")) != (1080, 1920) or not audio or abs(seconds - expected) > 0.4:
        raise legacy.BotError("QA không đạt: tỷ lệ/duration/audio.")
    if not captions.exists() or "Dialogue:" not in captions.read_text(encoding="utf-8"):
        raise legacy.BotError("QA không đạt: thiếu phụ đề.")
    decoded = subprocess.run(["ffmpeg", "-v", "info", "-xerror", "-i", str(video), "-af", "volumedetect", "-f", "null", "-"],
                             capture_output=True, text=True, timeout=300)
    volume = re.search(r"mean_volume:\s*(-?[\d.]+) dB", decoded.stderr)
    if decoded.returncode or not volume or float(volume.group(1)) < -55:
        raise legacy.BotError("QA không đạt: decode lỗi hoặc audio gần như im lặng.")
    return {"passed": True, "duration": seconds, "resolution": "1080x1920", "audio_mean_db": float(volume.group(1)),
            "caption_timing": "estimated within each measured TTS beat; human review required"}
