# Movie Analysis Bot: khảo sát và kế hoạch nâng cấp

## Source hiện tại (baseline 6e66c6c)

`youtube_shorts_bot.py` là monolith khoảng 7.200 dòng: Settings/dataclass → Archive → client OpenAI/Brave/Wikimedia → planner/editor → TTS → assets → FFmpeg → publisher. Short là so sánh hai đối tượng, long là explainer 16:9. Kịch bản qua Responses API, ảnh qua GPT Image hoặc tìm kiếm. Google Cloud Chirp có adapter tiếng Việt riêng. Audio được tạo trước để lấy thời lượng thật. ASS chia câu theo số từ, chưa có word alignment. Render có cache audio/ảnh/clip và resume, logo, timeout; social có retime theo TTS tiếng Việt. Facebook/TikTok/YouTube upload tách khỏi render.

`web_app.py` dùng Flask với HTML inline, đăng nhập tùy chọn, hàng đợi idea_queue và worker subprocess tuần tự. Archive hỗ trợ SQLite/PostgreSQL, recovery job đang xử lý khi khởi động. Cron Railway chạy CLI cũ; storage có thể ephemeral. Test hiện tại dùng pytest và mock API/FFmpeg.

## Tái sử dụng và thay đổi

Giữ nguyên mode short/long, lịch cron, publisher và schema queue. Tái sử dụng Settings dạng constructor (không gọi from_env vì movie không cần OpenAI key), GoogleCloudVietnameseTTS, materialize credential, require_tools/run, caption cue/ASS, font và logo mux. Thêm module movie độc lập, mode movie trong queue và nhánh CLI riêng. Không ép clip phim qua ShortPlan với prompt ảnh tài liệu hoặc chính sách loại tiếng Việt.

## Kiến trúc mới

MP4 → ffprobe + FFmpeg shot cuts / cửa sổ giới hạn → Gemini global audio-video → deep analysis từng cảnh → verifier xem lại cảnh + đối chiếu global → recheck các claim bất định ở FPS cao → ledger bằng chứng → nhận diện hồ sơ nhân vật + kiểm tra lại bằng video → dàn ý review → kịch bản tiếng Việt → audit từng câu và năm tiêu chí chất lượng → TTS từng beat → timeline đo thực tế → hình/cảnh có quyền → ASS + FFmpeg 1080×1920 → QA → review.

Nhánh mới `MOVIE_VIDEO_PROVIDER=gemini_live` dùng SDK Google và WebSocket, gửi JPEG tối đa 1 FPS cùng âm thanh PCM theo thời gian thực. Không tăng FPS ở recheck. Local hiện chọn `gemini-3.8-live`; Files/generateContent và FPS 4–8 phía dưới thuộc nhánh REST `gemini`. Writer chỉ nhận ledger đã duyệt, không nhận giả thuyết toàn cục. [Cấu hình và kết quả test Live](gemini-live.md).

Model mặc định `gemini-3.8-flash`, có thể đổi model verifier qua biến môi trường. Theo [catalog Google](https://ai.google.dev/gemini-api/docs/models), đây là model stable hiện hành; dùng Flash làm baseline thay vì tự chuyển lên model đắt hơn. [Video API](https://ai.google.dev/gemini-api/docs/generate-content/video-understanding) hỗ trợ file upload, audio-video, clipping và FPS. Sử dụng generateContent REST để tránh thêm SDK; không gửi vài JPEG rồi gọi đó là hiểu toàn video. Global lấy mẫu 1 FPS, scene 4 FPS, recheck 8 FPS, cấu hình được. Đây vẫn là lấy mẫu, không cam kết xem mọi frame hay hiểu đúng tuyệt đối. Cảnh bị giới hạn độ dài và có overlap khi kiểm chứng để giữ ngữ cảnh.

Observed fact cần timestamp, modality, confidence và kiểm chứng video độc lập. Inference phải dựa trên observed claim đã được chấp nhận và dùng ngôn ngữ suy luận. Bối cảnh phim có ledger riêng trong `movie_context.json`: tên phim/tóm tắt do người dùng cung cấp hoặc JSON các facts có ID `ctx_...` và nguồn. Writer và audit đối chiếu cả hai ledger, phân biệt context với sự kiện trong clip; bối cảnh không tự xác nhận nhận diện người xuất hiện, nguyên nhân hay kết phim. Bot không tự tải nguồn ngoài hoặc tự biết toàn bộ nội dung từ trailer. Dialogue không nghe rõ phải ghi uncertain. Global summary phục vụ ngữ cảnh, không tự trở thành chứng cứ.

Writer/audit có thể chọn Gemini hoặc OpenAI Responses bằng `MOVIE_WRITER_PROVIDER`, dùng `OPENAI_API_KEY`/`OPENAI_TEXT_MODEL` hiện có. OpenAI nhận text evidence/context, không nhận MP4; usage và reservation dùng chung ledger/trần của job. Structured Outputs giới hạn schema và validation/audit giữ bước chặn trước TTS. Prompt tiếng Việt ưu tiên mạch kể, nhân vật/xung đột, humour theo tình huống và câu chốt, hạn chế bài giảng góc máy/mô tả từng shot. `--script-only` xuất thoại để đọc trước, không chạy TTS/render. Đổi provider/model/bối cảnh/style/prompt chỉ invalidates cache thoại trở đi.

`MOVIE_IDENTITY_PROVIDER=openai` còn dùng tối đa 24 khung hình JPEG theo mốc evidence với Responses image inputs cho hai pass nhận diện; không thay thế Gemini audio/video analysis. Kích thước ảnh ≤768px, reservation token ảnh thận trọng và actual usage ghi đè sau response. Tên nhân vật và claim IDs có enum riêng trong schema, không dùng chung enum. Mẫu ảnh không chứng minh voice; AI tự báo confidence cao không đồng nghĩa danh tính đúng.

`MOVIE_IDENTITY_MODEL`/`--identity-model` chọn model riêng cho hai pass nhận diện; không thay model global/deep/verify. Cấu hình thử trên máy này dùng Gemini cho video/nhận diện và GPT-5.4 với reasoning medium cho dàn ý/thoại/audit. `MOVIE_TEXT_REASONING_EFFORT` chỉ áp dụng movie, không đổi setting của bot cũ.

`openai_budget.py` bổ sung ngân sách token theo ngày UTC dùng chung các movie job dưới `BOT_DATA_DIR`. Hai nhóm main/mini theo danh sách ưu đãi người dùng cung cấp, gồm alias và snapshot ngày; model không được cấp ngân sách bị chặn khi bộ đếm đang bật. Ledger dự trữ trước HTTP dưới OS lock, ghi nhận actual total tokens khi có response; request lỗi giữ dự trữ. Bộ đếm chỉ biết các request movie từ khi bật, không kiểm tra project sharing, usage ứng dụng khác hoặc hóa đơn, không bảo đảm miễn phí. Chi tiết điều kiện và cách đối chiếu Usage/Costs trong tài liệu chạy thử.

Cache content-addressed theo SHA-256 nguồn, cấu hình model/prompt và dependency từng bước; ghi atomically, có lock job, trạng thái lỗi và resume. Regenerate script/voice chỉ bỏ cache downstream. API usage ghi cả retry, token, TTS characters; chặn vượt số request/token trước request tiếp theo. Lượt đánh giá API thật cần nằm trong phạm vi được người dùng cho phép và trần request/token cụ thể.

`characters.py` đối chiếu roster có nguồn với video, rồi yêu cầu verifier xem lại. Danh tính chỉ được cấp fact riêng `ctx_identity_...` khi có dấu hiệu trực tiếp và hai pass confidence ≥0,92; không suy từ bảng tên trường, xe lăn hay cast list. `review.py` tạo kế hoạch gồm câu hỏi trung tâm, luận điểm, hook/giới thiệu và closing; schema bắt buộc các vai trò opening/development/closing. Audit chấm từng tiêu chí 0–5, ngưỡng duyệt 4; vòng sửa tối đa ba bản. Bản viết sai cấu trúc/độ dài cũng được sửa từ phản hồi đã lưu, không che lỗi transport/quota. `style_examples` và `slang_bank` là cấu hình văn phong, không phải evidence hoặc nguồn trend tự cập nhật. Validator bỏ citation brackets khỏi lời đọc nhưng giữ claim_ids.

Quyền sử dụng là metadata do người vận hành khai báo, kèm căn cứ và mã băm source; không phải kết luận pháp lý của AI. Unknown/restricted → chỉ đồ họa phân tích tự tạo bằng Pillow. Licensed/owned/legal_basis với căn cứ → cho phép dùng excerpt đã chọn theo timestamp; giữ bản gốc theo tỷ lệ trong khung dọc, không chỉnh để né copyright. Không tự publish movie từ queue hoặc CLI.

## Giai đoạn

1. CLI hoàn chỉnh: MP4 → evidence → script → TTS → render → review, cache và quota.
2. Tích hợp form upload, queue hiện tại, status, retry/regenerate và preview/download.
3. Kiểm thử failure/retry/cache/evidence/rights, hồi quy mode cũ và smoke FFmpeg trên clip synthetic; tài liệu cấu hình/chạy.
4. Sau khi có API key và clip thực tế: đánh giá lỗi nhận diện, tên nhân vật, audio và chất lượng lời bình. Có thể nâng cấp verifier khác model, forced alignment, asset thư viện có giấy phép và tính năng duyệt trước publish nếu cần.

## Rủi ro và giới hạn cần kiểm tra

Các pass cùng model có thể cùng sai; verifier giảm rủi ro, không chứng minh tính đúng. Mật độ scene/FPS cao làm tăng token. Limits tránh chi phí vô hạn; bảng giá thay đổi nên ledger không coi giá ước lượng là hóa đơn. Phụ đề bám chính xác text TTS nhưng timing cụm từ vẫn ước lượng trong mỗi beat. Không tự tìm ảnh web chưa xác minh giấy phép. Đồ họa tự tạo ưu tiên chữ/luận điểm và timestamp, không giả làm ảnh thực tế. Preview, upload và resume cần volume bền vững khi chạy Railway; PostgreSQL chỉ giữ queue/metadata, không giữ MP4. Worker một tiến trình, lock ngăn chạy cùng job đồng thời. Upload cần giới hạn dung lượng và auth khi public.
