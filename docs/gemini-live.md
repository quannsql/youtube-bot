# Phân tích video bằng Gemini 3.8 Live

Luồng hiện tại: MP4 → JPEG tối đa 1 FPS + âm thanh PCM mono 16 kHz → Gemini Live → kiểm chứng bằng chứng và nhận diện nhân vật → OpenAI viết/duyệt thoại → Google Cloud TTS → FFmpeg dựng MP4 dọc, phụ đề và logo.

Writer chỉ nhận bằng chứng đã duyệt và bối cảnh có nguồn; không nhận lời thoại/diễn giải chưa kiểm chứng của lượt global. Schema OpenAI giới hạn ID dẫn chứng và hình minh họa theo ledger thật. Khi audit chặn một câu, bot biên tập bản đã có theo phản hồi thay vì luôn viết lại từ đầu. Tiến độ biên tập lưu trong `script_repair.json` để tiếp tục khi job lỗi.

Live sử dụng SDK `google-genai` và WebSocket; không upload qua Files API, không tự chuyển sang Gemini Flash khi lỗi. Kết quả phân tích được nhận qua hàm `submit_analysis(json_text)` và phải qua các bộ kiểm tra timestamp, claim và nhận diện hiện có. Nếu model chỉ nói, bot nhắc gọi hàm một lần trong cùng phiên; vẫn không có kết quả hợp lệ thì dừng, giữ cache. Lời nói của model không được tự coi là bằng chứng đã duyệt.

Nếu phân tích chi tiết trả timestamp ngoài cảnh, bot đọc lại chính cảnh đó kèm lỗi kiểm tra, tối đa `MOVIE_ATTEMPTS` lượt trả lời (mặc định 3, tính cả câu trả lời đầu). Không tự kéo timestamp về biên hoặc coi chi tiết ngoài cảnh là bằng chứng. Các lượt sửa vẫn tính vào giới hạn request/token và TPM. Câu trả lời lỗi và từng lượt sửa được lưu; tiếp tục job cũ sẽ giữ global/các cảnh đã hoàn tất, dùng lại câu trả lời đã nhận, chỉ gọi API cho lượt sửa còn thiếu. Nếu cả ba lượt vẫn sai, job dừng trước khi viết thoại/TTS.

Phần chia cảnh giữ thời lượng MP4 chính xác ở mốc cuối. Khi đọc kế hoạch cũ đã làm tròn mili giây, Live chỉ dung sai tối đa 0,5 ms ở biên media và gửi đúng phần còn trong nguồn; không áp dụng dung sai này để duyệt bằng chứng AI. Lỗi `Khoảng video Live không hợp lệ` ở cảnh cuối do làm tròn có thể tiếp tục sau cập nhật mà giữ global/các cảnh đã kiểm chứng.

## Cấu hình local và Railway

```dotenv
MOVIE_VIDEO_PROVIDER=gemini_live
MOVIE_MODEL=gemini-3.8-live
MOVIE_VERIFIER_MODEL=gemini-3.8-live
MOVIE_IDENTITY_PROVIDER=gemini
MOVIE_IDENTITY_MODEL=
MOVIE_GLOBAL_FPS=1
MOVIE_SCENE_FPS=1
MOVIE_RECHECK_FPS=1
MOVIE_LIVE_TPM=65000
MOVIE_LIVE_MAX_SECONDS=300
MOVIE_LIVE_CHUNK_SECONDS=45
MOVIE_MAX_INPUT_SECONDS=300
MOVIE_MAX_SCENES=30
MOVIE_MAX_CALLS=100
WEB_JOB_TIMEOUT_SECONDS=7200
MOVIE_WRITER_PROVIDER=openai
MOVIE_WRITER_MODEL=gpt-5.4
```

Giữ `GEMINI_API_KEY`, `OPENAI_API_KEY` và Google TTS credentials đã cấu hình. Cài lại `requirements.txt`, khởi động lại dashboard/worker sau khi đổi biến. Railway cũng cần các biến trên; `.env` local không tự đồng bộ lên Railway. Các job upload mới dùng cấu hình này, vẫn chạy qua hàng đợi một worker. Không tự đăng mạng xã hội.

Giới hạn RPM/RPD Unlimited do người dùng kiểm tra trong AI Studio không bỏ giới hạn TPM 65.000. Bot gửi media theo thời gian thực và dự trữ token đầu vào cùng đầu ra với trần nội bộ 80% TPM (52.000) trong cửa sổ 65 giây; job phải chờ nếu không đủ. Đây là ước lượng nội bộ, không biết usage từ ứng dụng khác và không bảo đảm không gặp 429. Model xử lý tối đa một khung hình/giây: không tăng được FPS ở lượt recheck. Không có bảo đảm nhận diện mọi gương mặt hay hành động nhanh.

Nguồn đầu vào tối đa 300 giây. Phân tích tổng thể chia thành cửa sổ tối đa 45 giây và hợp nhất timeline bằng code, không tốn một lượt AI tổng hợp bổ sung. Nhãn nhân vật trung tính gần đây được gửi sang cửa sổ kế tiếp dưới dạng giả thuyết. Nhận diện nhóm các cảnh trọn vẹn trong cửa sổ tối đa 45 giây; mỗi lượt chỉ được viện dẫn bằng chứng thực sự nằm trong cửa sổ đó. Chỉ tên đã qua nhận diện và kiểm chứng độc lập mới được hợp nhất vào hồ sơ; không áp tên đã thấy ở một cảnh cho mọi cảnh khác. Khi TPM cấu hình thấp hơn, cửa sổ và cảnh tự ngắn lại.

`MOVIE_LIVE_MAX_SECONDS` giới hạn nguồn, không phải một phiên. Mỗi phiên được giới hạn riêng bởi `MOVIE_LIVE_CHUNK_SECONDS` (có trần nội bộ 60 giây), không cần giữ kết nối Live xuyên suốt 5 phút. Timeout mỗi phiên là độ dài media + 120 giây, trong đó chờ kết quả tối đa 100 giây sau media. Lỗi truyền tải/429/503 được thử lại tối đa `MOVIE_ATTEMPTS`; hết lượt thì dừng và giữ cache. Cửa sổ/cảnh đã hoàn thành không bị gửi lại khi tiếp tục cùng job và cùng cấu hình. Khi đổi độ dài cửa sổ/model/provider, các cache phụ thuộc cấu hình đó được tính lại.

Giới hạn 100 lượt AI/job tính cả OpenAI, recheck và retry; không phải 100 lượt cố định sẽ luôn được sử dụng. Với 5 phút và 15 cảnh 20 giây, dự kiến 7 global + 30 deep/verify + tối đa 16 nhận diện/kiểm chứng + 3 viết/duyệt = khoảng 56 lượt trước recheck, retry hoặc sửa thoại. Cảnh thực tế có thể nhiều hơn. Ngân sách token/job mặc định 2 triệu; giới hạn OpenAI theo ngày vẫn áp dụng. Thời gian xử lý có thể hàng chục phút vì Live gửi theo thời gian thực và phải chờ TPM. Dashboard chạy job nền, không cần giữ tab mở. Railway nên giữ một worker cùng Volume để tiến độ tồn tại qua restart.

Độ dài nguồn độc lập với mục tiêu lời bình 20–180 giây. Chế độ continuous giữ toàn bộ clip 2–5 phút, dù lời bình có thể ngắn hơn; sau lời bình vẫn phát âm thanh gốc đã hạ nhỏ. Timeout FFmpeg tăng theo độ dài nguồn; worker mặc định tối đa 2 giờ. Kịch bản ngắn không bị kéo dài bằng câu thừa.

## Chạy thử và tiếp tục

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe movie_analysis_bot.py "D:\video-bot\clips\test-short.mp4" --output-dir "D:\video-bot\generated\movies\live-test-short" --target-seconds 60 --context-file "D:\video-bot\docs\examples\avengers-doomsday-context.json" --writer-model gpt-5.4-mini --narration-style "Kể thẳng nội dung chính bằng tiếng Việt tự nhiên, gần gũi; hài hước hơi lầy, có một câu chọc nhẹ bám tình huống. Mở đầu giới thiệu phim, kết ngắn. Không tả động tác vụn, không nhồi meme, không đùa về khuyết tật." --rights-status legal_basis --rights-basis "Người dùng xác nhận sử dụng trích đoạn để bình luận."
```

Chọn trạng thái quyền và căn cứ thực tế của từng clip. Dòng trên ghi lại căn cứ người dùng cung cấp cho bản test, không xác nhận tính pháp lý. Khi chưa xác định quyền, dùng mặc định `unknown`: bot dựng đồ họa thay cho cảnh phim.

Chạy lại cùng lệnh/thư mục sẽ dùng cache hợp lệ; đổi provider/model vô hiệu hóa cache phân tích liên quan. File đầu ra `movie_review.mp4`, thoại `narration_vi.txt`, báo cáo `quality.json`, chi tiết từng phiên/token/TTS `usage.json`. `charged_tokens` là usage Google trả về khi có; nếu không có thì giữ dự trữ thận trọng, không phải hóa đơn. Bộ đếm OpenAI theo ngày vẫn áp dụng riêng.

Muốn quay về Files API: `MOVIE_VIDEO_PROVIDER=gemini`, `MOVIE_MODEL=gemini-3.8-flash`, `MOVIE_VERIFIER_MODEL=gemini-3.8-flash`, rồi đặt FPS 1/4/8. Không đặt model REST vào nhánh Live.

## Kết quả test thật 11/10/2026

Clip `clips/test-short.mp4` dài 20,02 giây được phân tích bằng Live. Bản hoàn tất tại `generated/movies/live-test-short/movie_review.mp4`: 29,33 giây, 1080×1920, giọng đọc Google TTS tiếng Việt, phụ đề ASS, logo và cảnh trích từ clip theo căn cứ người dùng cung cấp. QA hình/tiếng đạt; audit thoại cuối đạt 4/5 cho cả năm tiêu chí. Đây vẫn là bản nháp để người dùng xem/nghe duyệt; không ép đủ 60 giây bằng lời thừa.

Toàn bộ quá trình phát triển/test có 30 lượt API: 8 phiên Live (7 thành công, 1 bị chặn vì không trả tool call), 5 GPT-5.4 và 17 GPT-5.4-mini. Token OpenAI API báo: 41.155 nhóm main + 130.251 nhóm mini cho lượt thử này; TTS 3 đoạn, tổng 462 ký tự. Số lượt gồm nhiều bản thoại bị audit từ chối trong quá trình sửa, không phải số lượt cố định cho mọi video. Không có lỗi 429 trong lượt Live này. Chạy lại lệnh hoàn tất dùng cache, không tăng số lượt AI/TTS. Usage không xác nhận khoản tiền bị thu hay phần ưu đãi còn lại trên tài khoản.

## Kiểm tra nâng cấp nguồn 2–5 phút

Kiểm tra toàn luồng bằng nguồn tổng hợp 300 giây và AI/TTS giả lập: 7 cửa sổ global, phân tích/kiểm chứng phủ đủ nguồn, MP4 đầu ra 300 giây 1080×1920, âm thanh/phụ đề đạt QA, cảnh cuối vẫn đúng màu nguồn và không giữ/lặp cảnh. Chạy lại job hoàn tất không phát sinh lượt AI/TTS giả lập. Tách media Live ở nguồn 270–300 giây cho đúng 30 JPEG và 30 giây PCM, chỉ giữ đoạn đang phân tích trong RAM.

Các test 120/180/300 giây xác nhận phủ timeline và ngân sách từng phiên; test lỗi giữa global/nhận diện xác nhận chỉ làm tiếp phần thiếu, giữ candidate đã nhận và không phân tích lại video. Test timeout xác nhận mỗi lần retry vẫn tính lượt và dự trữ token. Những kiểm tra nâng cấp này không gọi API thật; chúng kiểm tra code, media và khả năng phục hồi, không xác nhận chất lượng AI nhận diện một bộ phim dài cụ thể hoặc quota thực tế trên Railway.

Tài liệu Google: [Live API](https://ai.google.dev/gemini-api/docs/live-api), [gửi video/audio và nhận kết quả](https://ai.google.dev/gemini-api/docs/live-api/capabilities), [Gemini 3.8 Live](https://ai.google.dev/gemini-api/docs/models/gemini-3.8-live), [quota](https://ai.google.dev/gemini-api/docs/rate-limits).
