"""Create an evidence-checked Vietnamese movie commentary draft from an MP4."""
from __future__ import annotations

import argparse
from dataclasses import replace
from pathlib import Path

import youtube_shorts_bot as legacy
from movie_analysis.core import MovieConfig, file_digest
from movie_analysis.pipeline import run_movie


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--target-seconds", type=int, default=60)
    parser.add_argument("--rights-status", choices=("unknown", "restricted", "owned", "licensed", "legal_basis"), default="unknown")
    parser.add_argument("--rights-basis", default="", help="Ghi căn cứ/giấy phép của chính clip đầu vào")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--analyze-only", action="store_true")
    mode.add_argument("--script-only", action="store_true", help="Viết/kiểm tra thoại rồi dừng trước TTS và render")
    parser.add_argument("--movie-title", help="Tên phim/trailer do bạn xác nhận")
    parser.add_argument("--movie-context", help="Bối cảnh/nhân vật; mỗi dòng một thông tin, tối đa 1500 ký tự/dòng")
    parser.add_argument("--context-file", type=Path, help="JSON bối cảnh có nguồn; xem docs/examples")
    parser.add_argument("--narration-style", help="Giọng bình luận mong muốn, ví dụ hài hước hơi lầy")
    parser.add_argument("--video-provider", choices=("gemini", "gemini_live"), help="Phân tích MP4 qua Files API hoặc Gemini Live")
    parser.add_argument("--video-model", help="Model phân tích và kiểm chứng video")
    parser.add_argument("--writer-provider", choices=("gemini", "openai"), help="AI viết và kiểm tra thoại; phân tích video vẫn dùng Gemini")
    parser.add_argument("--writer-model", help="Model viết thoại; mặc định theo provider hoặc OPENAI_TEXT_MODEL của bot cũ")
    parser.add_argument("--identity-provider", choices=("gemini", "openai"), help="Nhận diện bằng video Gemini hoặc khung hình OpenAI; mặc định Gemini")
    parser.add_argument("--identity-model", help="Model nhận diện riêng, ví dụ gemini-3.1-pro-preview; không đổi model phân tích cảnh")
    parser.add_argument("--regenerate-script", action="store_true")
    parser.add_argument("--visual-mode", choices=("continuous", "evidence"), help="Giữ nguyên clip hoặc chọn/lặp cảnh theo lời bình")
    parser.add_argument("--regenerate-voice", action="store_true")
    args = parser.parse_args()
    legacy.configure_logging("INFO")
    cfg = MovieConfig.from_env(args.target_seconds)
    if args.visual_mode:
        cfg = replace(cfg, visual_mode=args.visual_mode)
    if args.video_provider:
        cfg = replace(cfg, video_provider=args.video_provider)
    if args.video_model:
        cfg = replace(cfg, model=args.video_model, verifier_model=args.video_model)
    if cfg.video_provider == "gemini_live":
        cfg = replace(cfg, global_fps=min(cfg.global_fps, 1), scene_fps=min(cfg.scene_fps, 1), recheck_fps=min(cfg.recheck_fps, 1))
    if args.writer_provider:
        cfg = replace(cfg, writer_provider=args.writer_provider)
    if args.writer_model:
        cfg = replace(cfg, writer_model=args.writer_model)
    if args.identity_provider:
        cfg = replace(cfg, identity_provider=args.identity_provider)
    if args.identity_model:
        cfg = replace(cfg, identity_model=args.identity_model)
    if not args.video.is_file():
        parser.error("Không tìm thấy video đầu vào.")
    if args.context_file and not args.context_file.is_file():
        parser.error("Không tìm thấy file bối cảnh.")
    directory = args.output_dir or legacy.DATA_DIR / "generated" / "movies" / file_digest(args.video)[:20]
    result = run_movie(args.video, directory, cfg, args.rights_status, args.rights_basis,
                       args.regenerate_script, args.regenerate_voice, args.analyze_only,
                       movie_title=args.movie_title, movie_context=args.movie_context,
                       context_file=args.context_file, narration_style=args.narration_style,
                       script_only=args.script_only)
    print(f"Kết quả để kiểm tra: {result}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except legacy.BotError as exc:
        print(f"Movie job dừng: {exc}")
        raise SystemExit(1)
