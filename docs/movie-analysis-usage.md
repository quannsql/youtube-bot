# Movie Analysis Bot — chạy thử

Luồng Gemini Live mới: [cấu hình và test hoàn chỉnh](gemini-live.md). Các hướng dẫn Files API/FPS 4–8 phía dưới áp dụng khi chọn provider `gemini`.

Luồng movie đã có CLI và tích hợp dashboard hiện tại. Short/Long/cron cũ vẫn dùng lệnh cũ. Movie luôn tạo **bản nháp**, không tự đăng. Mặc định minh họa là đồ họa tự tạo bằng Pillow; chỉ dùng cảnh đầu vào khi người vận hành khai báo quyền và ghi căn cứ cụ thể.

## Local Windows

Khi được phép dùng cảnh nguồn, mặc định `MOVIE_VISUAL_MODE=continuous`: giữ toàn bộ clip từ đầu đến cuối ở tốc độ gốc, thêm lời review, phụ đề và logo; âm thanh gốc giảm còn 12%. Khung hình được đặt trọn trong video dọc, không crop nội dung. Nếu lời đọc dài hơn clip, khung hình cuối được giữ đến hết lời đọc; nếu clip dài hơn lời đọc, vẫn phát hết clip. Thời lượng chọn là mục tiêu viết thoại, không ép cắt hoặc lặp clip để đủ thời gian.

CLI có `--visual-mode continuous` và `--visual-mode evidence`. Chế độ `evidence` chọn cảnh theo dẫn chứng từng ý và có thể lặp cảnh cho đủ lời đọc. Dashboard có lựa chọn **Cách dựng hình ảnh** và lưu lựa chọn này cho từng job. Đổi chế độ dựng với cùng thư mục và cấu hình lời bình sẽ dùng lại phân tích, kịch bản và giọng đọc đã lưu.

Phong cách mặc định là tiếng Việt đời thường, tấu hài hơi lầy, đùa theo tình huống và vẫn kể rõ nội dung. Khoảng hai câu đùa ngắn cho 60 giây là gợi ý nhịp kể, không phải chỉ tiêu bắt buộc; clip ít thông tin sẽ ngắn hơn. Có thể chỉnh ô **Phong cách lời bình** trước khi upload, hoặc mở **Đổi phong cách lời bình** trên trang kết quả rồi bấm **Viết lại lời bình**. Viết lại sẽ gọi API viết/duyệt và TTS cho phần thoại thay đổi, nhưng dùng lại phân tích video khi cache còn hợp lệ. Video đã hoàn tất không tự đổi lời thoại chỉ vì cập nhật code.

GPT có thể dùng các khung hình để tự đề xuất nhân vật hoặc đối chiếu hồ sơ phim: đặt `MOVIE_IDENTITY_PROVIDER=openai` và `MOVIE_IDENTITY_MODEL=gpt-5.4-mini`. Khi chọn OpenAI, bot gửi tối đa 24 khung hình mỗi lượt nhận diện; các khung hình không có âm thanh. Gemini Live vẫn phụ trách phân tích nội dung video/audio. Giữ `MOVIE_IDENTITY_PROVIDER=gemini` nếu muốn nhận diện qua Live như bản test đã hoàn tất. Nhập tên phim; hồ sơ nhân vật là tùy chọn. Tên phim, danh sách diễn viên, bối cảnh hoặc đồ vật tương tự không đủ để gán tên người trong clip. Không có bảo đảm nhận diện đúng mọi clip.

Hồ sơ nhân vật hỗ trợ `playful_names`, ví dụ `"playful_names": ["con khỉ"]` cho nhân vật Kong. Tên đầy đủ và tên nói trong lời bình vẫn giữ ở `name` / `spoken_name`; biệt danh chỉ là cách xưng hô sau khi nhân vật được kiểm chứng và giới thiệu. Biệt danh không tham gia lượt nhận diện. Đổi biệt danh không gọi lại nhận diện khi cache còn hợp lệ. Mẫu hồ sơ nằm tại `static/context-example.json` và có nút tải trên form upload. Nguồn facts/context_ids và gợi ý nhận diện vẫn cần đầy đủ như mẫu.

Môi trường `.venv` và FFmpeg trong `.tools` đã được chuẩn bị trên máy này. Luồng movie tự tìm `.tools/**/ffmpeg.exe` nếu PATH chưa có FFmpeg. Trên máy khác, cài Python 3.11+, dependency trong `requirements.txt`, và FFmpeg/ffprobe (có libass, libx264).

```powershell
Copy-Item .env.example .env
```

Điền `GEMINI_API_KEY`, đặt Google TTS service-account JSON tại `google_tts_service_account.json` hoặc cấu hình `GOOGLE_TTS_SERVICE_ACCOUNT_FILE`. Bật Cloud Text-to-Speech cho project và quyền của service account. Giọng tiếng Việt dùng cấu hình cũ `GOOGLE_TTS_VI_VOICE` / `GOOGLE_TTS_VI_SPEAKING_RATE`. Movie không yêu cầu OpenAI, Brave, YouTube hoặc Facebook key. Không commit secret.

```powershell
# Chỉ phân tích/kiểm chứng, chưa cần TTS credential; áp dụng quota/giá theo tier Gemini của project.
.\.venv\Scripts\python.exe movie_analysis_bot.py "D:\clips\scene.mp4" --analyze-only

# Tạo MP4 với đồ họa tự tạo và thuyết minh tiếng Việt.
.\.venv\Scripts\python.exe movie_analysis_bot.py "D:\clips\scene.mp4" --target-seconds 60

# Chỉ dùng cảnh phim nếu có căn cứ áp dụng cho chính clip này.
.\.venv\Scripts\python.exe movie_analysis_bot.py "D:\clips\scene.mp4" --rights-status licensed --rights-basis "Giấy phép X, chủ sở hữu Y, cho phép bình luận và phân phối trên Facebook"

# Chạy lại cùng lệnh để tiếp tục job. Không phân tích lại nếu cache hợp lệ.
.\.venv\Scripts\python.exe movie_analysis_bot.py "D:\clips\scene.mp4" --regenerate-script
.\.venv\Scripts\python.exe movie_analysis_bot.py "D:\clips\scene.mp4" --regenerate-voice

# Dashboard: vào http://127.0.0.1:8080/movies (PORT có thể đổi).
.\.venv\Scripts\python.exe web_app.py
```

Nút tạo lại lời bình/giọng đọc có thể phát sinh phí ở bước tương ứng. Tham số quyền sử dụng phải được lặp lại khi chạy CLI với cảnh được cấp phép; nếu bỏ sẽ chuyển về đồ họa tự tạo. Có thể dùng `--output-dir` để đặt thư mục job riêng. Mỗi thư mục chỉ nhận một video theo SHA-256. Chế độ movie từ dashboard được xử lý bằng worker subprocess hiện tại, `publish=False` cố định.

## Kết quả và review

Trong `generated/movies/<hash hoặc uuid>/`:

- `global.json`, `scenes.json`, `sNNN_deep/verify/recheck.json`: ngữ cảnh, timeline, các pass AI.
- `evidence.json`: observed claims được chấp nhận và chi tiết bị loại, timestamp, modality, confidence và lý do verifier.
- `script_approved.json`: lời bình cuối đã audit từng câu; inference/joke có phân loại và citation IDs.
- `rights.json`: khai báo quyền gắn với mã băm nguồn; `timeline.json`: mapping lời đọc → visual → timestamp nguồn.
- `usage.json`: Gemini calls/retries, input/output/total tokens, dự trữ token cho request bị gián đoạn và TTS characters. Đây không phải hóa đơn USD.
- `movie_review.mp4`, `preview.jpg`, `captions_vi.ass`, `quality.json`: bản dọc 1080×1920 và kết quả QA.
- `job.json`: bước hiện tại, trạng thái và lỗi. Cache ghi atomic; OS lock ngăn chạy cùng job đồng thời.

Phản hồi AI được lưu trước validation trong `*.response.json`, kể cả khi timestamp bị từ chối. Đây là dữ liệu chẩn đoán chưa được chấp nhận, không phải evidence để viết lời bình. Mốc thời gian dạng số giây hoặc MM:SS/HH:MM:SS được chuẩn hóa; chỉ sửa sai số làm tròn ở mép clip (tối đa nửa khoảng lấy mẫu, không quá 0,5s). Mốc vượt xa clip vẫn bị từ chối. Global analysis sai timeline sẽ được yêu cầu xem lại với số lượt giới hạn bởi `MOVIE_ATTEMPTS` và quota chung. Các lỗi HTTP tạm thời như 503 được ghi rõ trước khi retry.

Nếu gặp HTTP 429, xem `last_api_error.json` và trường `error` trong `usage.json`: bot lưu message, quota ID/metric và RetryInfo của Google, có che key và URL. Quota theo ngày hoặc quota bằng 0 dừng ngay; quota theo phút có thể retry sau thời gian Google đề nghị (ít nhất 30s, tối đa 60s). Thời gian chờ dài hơn hoặc 429 không có thông tin rõ sẽ dừng và giữ cache. Kiểm tra đúng project/model ở Google AI Studio → Usage → Rate limits. Quota theo ngày reset lúc 00:00 Pacific; ở Việt Nam là 14:00 khi Pacific dùng giờ mùa hè, 15:00 khi dùng giờ tiêu chuẩn. Chạy lại cùng lệnh/cùng output-dir sau khi quota khả dụng để tiếp tục; tăng `MOVIE_MAX_CALLS` không tăng quota của Google.

Subtitle text lấy trực tiếp từ lời TTS; timing bám duration đo từng beat, nhưng cụm từ vẫn chia theo số từ. QA kiểm tra decode toàn video, audio không gần im lặng, resolution, thời lượng và file subtitle. Chưa có forced alignment hoặc đánh giá chất lượng phát âm tự động. Cần xem hình/phụ đề bằng mắt.

## Lời bình tự nhiên và bối cảnh phim

Movie writer hiện có bối cảnh riêng và phong cách nói chuyện: hook → mạch chuyện/nhân vật/xung đột → câu chốt, dùng chi tiết hình ảnh để hỗ trợ thay vì kể từng khung hình. Tên phim và thông tin ngoài clip do người dùng cung cấp hoặc nạp từ JSON có nguồn; bot không tự xác nhận toàn bộ cốt truyện từ vài giây trailer. Context facts có ID `ctx_...` và nguồn riêng, không được coi là cảnh đã nhìn thấy. Nhận diện nhân vật/quan hệ/động cơ trong clip vẫn cần bằng chứng phù hợp. Form `/movies` cũng có tên phim, bối cảnh và giọng bình luận.

Theo góp ý về lời thoại lan man, prompt v6 ưu tiên **tình huống trong phim**: ai gặp nguy cơ gì, chuyện gì đang xảy ra, ảnh hưởng đến ai. Mở đầu nói thẳng tên phim và vấn đề chính; thân chỉ giữ chi tiết cần thiết; kết nêu ý nghĩa hoặc tình trạng còn chưa rõ. Không lấy cách trailer cắt cảnh, giấu mặt, dấu ba chấm hoặc động tác vụn làm chủ đề. Hài nhẹ là tùy tình huống, không bắt buộc hai câu đùa hay callback. Thời lượng là ngân sách ước lượng, không có số từ tối thiểu: clip ít thông tin có thể có thoại ngắn hơn 60 giây. Không bịa thêm cốt truyện để kéo dài. Mẫu biên tập thủ công tại [avengers-doomsday-narration.md](examples/avengers-doomsday-narration.md); v6 chưa chạy API thật.

Ví dụ với clip Avengers: Doomsday đã phân tích, tạo riêng lời bình để đọc thử trước khi dùng TTS:

```powershell
.\.venv\Scripts\python.exe movie_analysis_bot.py "D:\video-bot\clips\test-short.mp4" --output-dir "D:\video-bot\generated\movie-test-short" --target-seconds 30 --context-file "D:\video-bot\docs\examples\avengers-doomsday-context.json" --script-only
```

JSON mẫu dùng bối cảnh từ trang Disney đã đối chiếu lúc chuẩn bị mẫu. URL là nguồn để kiểm tra lại, bot không tự tải URL hay tự xác nhận clip tải lên là trailer chính thức. Với phim khác, tạo file riêng gồm `title`, `style`, `facts:[{id:"ctx_...",text:"...",source:"..."}]`. Có thể dùng `--movie-title`, `--movie-context` (mỗi dòng một thông tin, ≤1500 ký tự/dòng) và `--narration-style` thay cho file. Bối cảnh được lưu trong `movie_context.json`, dùng lại khi resume; thay bối cảnh/style/provider/model chỉ tạo lại phần thoại, giữ cache phân tích video.

Để lên dàn ý, viết và audit thoại bằng OpenAI giống bot cũ, thêm `--writer-provider openai`. Dùng lại `OPENAI_API_KEY`; model ưu tiên `MOVIE_WRITER_MODEL`, rồi `OPENAI_TEXT_MODEL`. Cấu hình thử 60 giây trên máy này dùng `MOVIE_WRITER_PROVIDER=openai`, `MOVIE_WRITER_MODEL=gpt-5.4`, `MOVIE_IDENTITY_PROVIDER=gemini`, `MOVIE_TEXT_REASONING_EFFORT=medium`. Các thiết lập riêng này không đổi model của bot short/long cũ. Phân tích video/audio và xác minh từng cảnh vẫn dùng Gemini. GPT-5.4 nhận text evidence/context cho phần dàn ý, viết và audit; không nhận MP4 hoặc âm thanh trực tiếp. Tùy chọn `--identity-provider openai` còn gửi các khung hình lấy từ video để nhận diện.

`--script-only` dừng sau viết + audit, xuất `narration_vi.txt` và `script_approved.json`, không cần credential TTS/logo. Khi đã đọc thoại, chạy lại cùng lệnh với cùng provider/model/target-seconds nhưng bỏ `--script-only` để dựng video từ cache thoại. Số lượt/token OpenAI ghi chung trong `usage.json`, áp dụng cùng `MOVIE_MAX_CALLS/MOVIE_MAX_TOKENS`. Dự trữ input OpenAI dùng số byte UTF-8 cộng overhead để bảo vệ ngân sách, không phải token count chính xác; response usage thay thế sau khi hoàn tất. Retry có giới hạn, insufficient_quota dừng ngay; output bị cắt/refusal không được chấp nhận. `narration_vi.txt` chỉ để đọc, sửa file này không thay đổi kịch bản JSON đã audit.

## Chi phí và giới hạn

### OpenAI data sharing trên tài khoản này

Theo thông báo tài khoản do người dùng cung cấp, hai nhóm được hưởng tối đa 250.000 và 2.500.000 token mỗi ngày. GPT-5.4 thuộc nhóm 250.000; GPT-5.4-mini thuộc nhóm 2.500.000. Input và output cùng tính vào hạn mức nhóm, không phải mỗi model có một hạn mức riêng. Đảm bảo API key thuộc project đã bật chia sẻ và tài khoản hiển thị enrolled, có số dư dương. Token ưu đãi chỉ áp dụng traffic đủ điều kiện; tool use và các model ngoài danh sách không thuộc phạm vi thử này. Khi một request làm vượt hạn mức ngày, cả request đó có thể bị tính phí. Ngày ưu đãi reset 00:00 UTC, tức 07:00 Việt Nam. Xem [điều kiện chính thức](https://help.openai.com/en/articles/10306912-sharing-feedback-evaluation-and-fine-tuning-data-and-api-inputs-and-outputs-with-openai).

Bot có bộ đếm riêng dùng chung giữa các movie job:

```dotenv
MOVIE_OPENAI_MAIN_DAILY_TOKEN_LIMIT=200000
MOVIE_OPENAI_MINI_DAILY_TOKEN_LIMIT=2000000
```

Giữ mức thấp hơn ưu đãi để chừa khoảng trống. Đây là ngân sách **cục bộ**, không truy vấn số token miễn phí còn lại trên tài khoản. Sổ bắt đầu từ khi bật, không nhập lại các lượt thử trước đó, không biết lượt bot short/long cũ hoặc ứng dụng khác. Nếu có usage ngoài movie bot, giảm trần tương ứng trước khi chạy. `0` tắt ngân sách nhóm; nếu ít nhất một nhóm đang bật thì model ở nhóm chưa được cấp ngân sách sẽ bị chặn. Tắt cả hai sẽ chỉ còn trần từng job, không còn bộ chặn theo ngày.

Sổ nằm ở `generated/openai_movie_budget/usage.json` dưới `BOT_DATA_DIR`, khóa khi dự trữ để nhiều job không cùng chiếm một ngân sách. Trước HTTP, bot dự trữ input cộng trần output; response có usage sẽ thay bằng số thực tế. Request lỗi không có usage giữ dự trữ thận trọng. Khi hết trần, bot dừng và giữ cache. Bộ đếm không bảo đảm hóa đơn bằng 0: đối chiếu Usage theo service tier `data sharing incentive tier` và trang Costs trên dashboard. Gemini và Google TTS có hạn mức/billing riêng, không hưởng ưu đãi OpenAI.

Mặc định clip ≤5 phút, ≤500 MB, ≤30 scene, ≤100 lượt AI (gồm OpenAI/recheck/retry) và ≤2 triệu token cho toàn job. REST dùng countTokens; Live ước lượng prompt + khung hình + audio + đầu ra, dự trữ trước khi gửi và chờ quota theo phút. Failed/time-out requests giữ dự trữ khi không có usage thật vì có thể đã được tính phí. Live chia cả phân tích tổng thể và nhận diện thành phiên tối đa 45 giây, giữ timestamp gốc và lưu cache riêng từng đoạn. Quota persist giữa các lượt chạy, bao gồm tạo lại script. Thay `MOVIE_MAX_CALLS`/`MOVIE_MAX_TOKENS` nếu muốn tiếp tục sau khi chạm trần; thay trần không làm mất cache phân tích. Xem [cấu hình và cách tiếp tục Live](gemini-live.md) khi deploy video 2–5 phút.

Mọi cảnh được phân tích và kiểm chứng; cảnh chưa rõ có thêm recheck, tối đa `MOVIE_MAX_RECHECKS`. Quota và scene cap dừng job, không bỏ qua phần video rồi giả vờ đã xem toàn bộ. Output dùng audio thực tế, tối đa 180s; target là chỉ dẫn cho writer, không ép tăng tốc để đạt đúng số giây. Google TTS mỗi beat có retry giới hạn; usage ghi mỗi attempt.

Không gửi API key qua URL hoặc ghi key vào log. Video được tải lên Google Files API cho phân tích; xem chính sách dữ liệu/tier của tài khoản trước khi gửi clip nhạy cảm. Cache file reference có thời hạn; file hết hạn được tải lại, kết quả phân tích hợp lệ vẫn giữ.

## Railway

Dashboard đã nâng cấp thành Video Studio, có upload/xem trước MP4, hồ sơ nhân vật, tiến trình và trang kết quả phân biệt thoại nháp với thoại đã duyệt. Xem [hướng dẫn dashboard và Railway](dashboard-railway.md) cho cấu hình service Web, Volume, đăng nhập và cách chạy preview không gọi API.

Chạy `python web_app.py` như service hiện tại, đặt `WEB_ACCESS_TOKEN` và `FLASK_SECRET_KEY` ổn định. Dùng Volume với `BOT_DATA_DIR` hoặc `RAILWAY_VOLUME_MOUNT_PATH` cho `generated/movies`; PostgreSQL chỉ lưu queue, không giữ video. Không dùng filesystem ephemeral nếu muốn review/resume sau restart. Một worker/service truy cập cùng kho job; tránh chạy nhiều web replica với recovery tự động của queue cũ. `nixpacks.toml` hiện tại đã cài FFmpeg/ffprobe.

Upload qua proxy/platform có thể bị giới hạn thấp hơn 500 MB; giảm `MOVIE_MAX_INPUT_MB` theo deployment. Route movie dùng login hiện có và CSRF token. MP4 được kiểm tra bằng ffprobe trước khi enqueue. Chỉ các artifact review được phép tải, không route tùy ý đọc file nguồn hoặc đường dẫn do người dùng nhập.

## Kiểm thử

### Luồng review có dàn ý và nhận diện nhân vật

Luồng mới: bằng chứng đã kiểm chứng → tự đề xuất nhân vật từ video (hoặc đối chiếu hồ sơ có sẵn) → kiểm chứng tên lần hai → dàn ý có một luận điểm → viết thoại → audit dữ kiện và chất lượng → TTS/render. Không có `characters` trong context vẫn chạy tự nhận diện, không cần JSON. Nhận diện thường cần hai request mỗi cửa sổ media (không có ứng viên thì chỉ một); phản hồi đề xuất sai định dạng có thêm tối đa hai lượt sửa, chưa tính retry mạng. Tất cả dùng chung trần `MOVIE_MAX_CALLS` và cache của job. Với Gemini Live và GPT trên video dài hơn 45 giây, chia cửa sổ tối đa 45 giây; cửa sổ Live có thể ngắn hơn theo TPM. GPT nhận tối đa 24 ảnh có timestamp mỗi cửa sổ, gồm bằng chứng `visual` và `both`, không nhận âm thanh. Dàn ý/viết/audit cần ba request text ở lượt thành công đầu; biên tập lại có thể thêm request trong trần chung. `--analyze-only` vẫn chỉ tạo bằng chứng.

Nếu job cũ dừng với “Ứng viên nhân vật cần tên ngắn gọn”, chọn **Tiếp tục từ bước đã lưu** sau khi cập nhật code. Bot đọc lại phản hồi đã lưu và sửa riêng cửa sổ lỗi; các cửa sổ nhận diện đã thành công vẫn được dùng lại. Thiếu `spoken_name` thì dùng `name` chuẩn đã hợp lệ, không thêm request sửa. Thiếu/sai `name` phải sửa qua AI, không suy tên từ tiêu đề phim; ứng viên sau sửa vẫn phải qua kiểm chứng độc lập. Sau tối đa hai lượt sửa vẫn sai, job dừng và giữ phản hồi để kiểm tra; tiếp tục cùng cấu hình không gọi lại những bản sửa đã lưu. Lỗi mạng/quota dừng ngay và có thể tiếp tục khi API hoạt động lại.

Muốn dùng GPT để tự nhận diện, đặt `MOVIE_IDENTITY_PROVIDER=openai` và `MOVIE_IDENTITY_MODEL=gpt-5.4-mini`, dùng `OPENAI_API_KEY` hiện có. Thay cả model nếu trước đó đặt `gemini-3.8-live`; phần đọc video vẫn có thể giữ Gemini Live. Tích hợp dùng ảnh và JSON Schema của [OpenAI Responses API / Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs). Nhận diện Gemini vẫn hoạt động theo cấu hình cũ, không bắt buộc đổi model. Job cũ có video/evidence trên Volume có thể **Tiếp tục từ bước đã lưu**: bước nhận diện mới chạy từ evidence đã lưu, và lời bình được viết lại nếu có tên mới được xác nhận. Không cần upload lại lên dashboard; các bước nhận diện/viết/giọng đọc mới có thể dùng API.

Kịch bản cần 3–8 phần theo thứ tự mở đầu, phát triển, kết thúc. Mở đầu giới thiệu tên phim, phần thân nối chi tiết với ý nghĩa, phần kết có câu chốt/câu hỏi cụ thể. Audit chấm mạch kể, tiếng Việt tự nhiên, hài hước phù hợp, mở đầu và kết thúc; mỗi tiêu chí phải đạt ít nhất 4/5. Bot có tối đa ba bản viết để sửa lỗi, sau đó dừng để kiểm tra thay vì duyệt bài kém. Điểm là đánh giá của AI, vẫn cần đọc lời thoại thực tế.

Lỗi “Script audit phải kiểm tra mọi câu” nghĩa là phản hồi duyệt thiếu, trùng hoặc sai mã câu, chưa kết luận kịch bản sai nội dung. Bot gửi danh sách từng câu và mã chính xác `b01:0`, `b01:1`… (đánh số từ 0 trong mỗi beat), rồi sửa riêng bản duyệt tối đa hai lượt. Không tự điền `supported=true`; mọi câu phải được AI kiểm tra, gồm câu bối cảnh, suy luận và câu đùa. Các đánh giá đã loại câu, tiêu đề/phong cách hoặc chất lượng thấp không được nâng thành đạt chỉ nhờ sửa mã câu; bản duyệt sửa vẫn có thể phát hiện thêm lỗi. Câu thiếu bằng chứng phải qua bước viết lại như trước. Cơ chế này áp dụng cả duyệt bản viết ban đầu và bản rút gọn sau TTS. Nếu job cũ dừng ở lỗi này, **Tiếp tục từ bước đã lưu** sẽ dùng lại bản kịch bản hợp lệ và sửa bản duyệt, giữ cache phân tích/nhận diện; có thể gọi thêm API duyệt. Lỗi mạng/quota giữ trạng thái này để tiếp tục mà không viết lại. Sau hai lượt sửa vẫn thiếu câu, job dừng trước TTS/render và giữ các phản hồi đã nhận, tránh gọi lặp cùng bản lỗi khi tiếp tục.

Nếu bản biên tập bỏ tên phim ở mở đầu, bot bổ sung một câu giới thiệu từ tên bạn nhập và nguồn `ctx_title`, rồi gửi cả câu bổ sung qua audit. Không dùng tên phim để suy đoán nhân vật hay cốt truyện. Khi sửa lỗi cấu trúc, phản hồi audit trước đó vẫn được giữ để tránh đưa lại câu đã bị loại. Job cũ dừng với lỗi “Mở đầu review thiếu lời giới thiệu phim” có thể chọn **Tiếp tục từ bước đã lưu** sau khi cập nhật code; các bước phân tích đã lưu được dùng lại, bước sửa lời bình/audit chưa đạt vẫn có thể gọi API.

Ở chế độ giữ nguyên clip, nếu TTS của một beat dài hơn khoảng thời gian dành cho nó, bot tính giới hạn từ theo audio thực tế và tự rút gọn riêng beat đó. Các beat khác, thứ tự cảnh và dẫn chứng vẫn được giữ; bản sửa phải qua audit trước khi tạo giọng đọc mới. Audio của các beat không đổi được dùng lại. Tối đa hai lượt sửa theo thời lượng, mỗi lượt tối đa hai bản biên tập; lỗi mạng/quota dừng để tiếp tục từ cache. Job cũ dừng với lỗi “Lời bình b03 cần …s nhưng cảnh chỉ có …s” có thể **Tiếp tục từ bước đã lưu**, không cần upload lại hoặc chọn tạo lại toàn bộ giọng đọc. Bước sửa lời bình/audit và TTS cho phần thay đổi có thể gọi API.

Nếu cả hai bản biên tập vẫn quá dài, bot chuyển sang rút gọn riêng các beat lỗi, tối đa hai bản rút gọn. AI trả một câu ngắn cho mỗi beat dưới dạng danh sách từng đơn vị cách nhau bằng dấu cách; OpenAI dùng JSON Schema giới hạn `maxItems` riêng cho từng beat và cấm khoảng trắng bên trong từng mục. Ví dụ b02 có giới hạn 47 thì không được trả 48 mục hoặc gộp “sắp toang” vào một mục. Bot lắp phần rút gọn vào kịch bản gốc, giữ nguyên các beat khác/tiêu đề/mốc cảnh, kiểm tra lại số từ và dẫn chứng, rồi duyệt toàn bộ kịch bản trước TTS. Không tự cắt lời giữa câu hoặc tăng tốc quá mức để ép vừa cảnh. Request mới ghi stage `timing_rewrite`, dùng chung quota của job; bản rút gọn và audit được cache để tiếp tục khi mạng/quota gián đoạn. Job cũ dừng với “Beat b02 phải còn tối đa 47 từ” có thể tiếp tục từ hai bản sửa đã lưu, chỉ gọi bước rút gọn và audit mới. Dashboard hiển thị bước “Rút gọn lời bình theo thời lượng” và ưu tiên lỗi mới trong trạng thái job; khi mở job cũ dùng chung thư mục với lượt tiếp tục, tránh hiển thị lỗi audit cũ kèm bước xử lý mới.

Timeline mới kiểm tra **tất cả** khoảng đọc sau khi có TTS và gom các beat vượt thời lượng vào một bản rút gọn/audit, không sửa lần lượt rồi dừng ở beat kế tiếp. Các job dùng timeline này vào thẳng bước `timing_rewrite` có giới hạn số từ, không gọi hai lượt viết lại toàn kịch bản trước. Nếu lời kết bị neo vào khung hình cuối còn dưới 3 giây nhưng audio không vừa, bot dành một khoảng kết tối đa 10–20 giây (còn phụ thuộc thời lượng audio và phần trước), rồi yêu cầu viết lại lời kết thành nhận xét về các diễn biến đã xuất hiện. Bot chọn dẫn chứng hình ảnh trước khoảng kết, loại các dẫn chứng của cảnh cuối chưa xảy ra; chỉ beat kết này được đổi mốc minh họa, các beat khác giữ nguyên. Bản sửa vẫn phải duyệt, không được đọc kết quả trận đánh trước khi nó xuất hiện. Video giữ toàn bộ clip ở tốc độ gốc, không cắt lời đọc giữa câu hoặc giữ khung hình cuối để ép vừa. Job cũ lỗi “b05 cần 42.3s nhưng cảnh chỉ có 0.5s” có thể tiếp tục từ bản đã duyệt/audio đã lưu; chỉ các đoạn thực sự thay đổi phải tạo giọng mới.

File mẫu Avengers đã có `characters:[{id,name,spoken_name,recognition_notes,context_ids}]`, `style_examples`, `slang_bank` và `review_scope`. Hồ sơ có `context_ids` trỏ tới fact có nguồn; gợi ý nhận diện không tự chứng minh nhân vật xuất hiện. Chỉ nhận tên nếu đủ dấu hiệu trực tiếp và hai pass cùng xác nhận với confidence ≥0,92. Độ tin cậy của AI không phải xác suất bảo đảm đúng. Không tự tải ảnh mẫu hoặc nguồn web. Với phim mới, hồ sơ nhân vật là tùy chọn; tên phim chỉ là gợi ý cho nhận diện, không chứng minh ai xuất hiện. Ứng viên chưa được kiểm chứng không được biến thành fact bối cảnh, quan hệ nhân vật hoặc cốt truyện. Dashboard hiển thị tên đã xác nhận và lý do loại ứng viên chưa đủ chắc chắn.

`style_examples` tối đa bốn mẫu nhịp nói do bạn chọn, không dùng làm dữ kiện. `slang_bank` tối đa 30 câu/từ; writer dùng tối đa hai câu hợp tình huống. Đây là danh sách biên tập được, không phải bộ dò trend mạng xã hội tự cập nhật. Sửa danh sách khi muốn đổi phong cách hoặc bắt trend mới. `review_scope=trailer` giữ lời bình ở phạm vi đoạn giới thiệu; review toàn phim cần cung cấp phim/tóm tắt đầy đủ có nguồn.

Thêm `characters.json` để kiểm tra nhận diện, `review_plan.json` để xem dàn ý và `script_approved.json.audit.quality` để xem điểm/lý do. Mã dẫn chứng chỉ nằm trong metadata; lời đọc và phụ đề được làm sạch `[sNNN_...]`, `[ctx_...]`. Bản prompt mới tự tạo lại dàn ý/thoại, giữ phân tích cảnh cũ. Thay hồ sơ nhân vật hoặc fact nguồn còn tạo lại nhận diện; thay riêng style/slang không xem lại video.

Khi Gemini đang trả 503 ở nhận diện, dùng `--identity-provider openai` hoặc `MOVIE_IDENTITY_PROVIDER=openai`. Bot trích tối đa 24 JPEG (mỗi chiều ≤768px) ở mốc bằng chứng hình ảnh; gửi cùng timestamp và hồ sơ vào hai pass nhận diện. Đây là ảnh lấy mẫu, không có âm thanh, có thể bỏ sót gương mặt/cảnh. Không chấp nhận dấu hiệu voice từ ảnh. Ảnh được gửi tới OpenAI; chi phí có thêm image input tokens và cũng ghi vào usage. Vẫn cần Gemini cho phân tích video/audio ban đầu. Danh tính có thể chưa được nhận diện đủ chắc; không bảo đảm luôn gọi đúng tên mọi nhân vật.

Có thể nâng riêng bước nhận diện bằng `--identity-model gemini-3.1-pro-preview` (giữ `--identity-provider gemini`). Nếu chọn OpenAI cho nhận diện, đặt riêng `--identity-model gpt-5.4`. Thay model nhận diện không phân tích lại các cảnh đã có cache hợp lệ; Gemini Pro vẫn có quota/giá Google riêng.

Lệnh thử cấu hình hybrid 60 giây, chỉ tạo lời bình:

```powershell
.\.venv\Scripts\python.exe movie_analysis_bot.py "D:\video-bot\clips\test-short.mp4" --output-dir "D:\video-bot\generated\movie-test-short" --target-seconds 60 --context-file "D:\video-bot\docs\examples\avengers-doomsday-context.json" --writer-provider openai --writer-model gpt-5.4 --identity-provider gemini --script-only
```

Khi hài lòng, chạy lại cùng lệnh và bỏ `--script-only` để tạo giọng/video. Giữ cùng context/provider/model/thời lượng để dùng lại thoại đã duyệt.

Lượt đầu ngày 11/10/2026 trên test-short: sáu request OpenAI mini gồm hai đầu ra nhận diện sai schema (đã sửa), một đầu ra không nhận diện được nhân vật và ba bản thoại quá dài cho 30 giây. Không có script mới được duyệt; không chạy TTS/render. Lượt này dùng dàn ý biên tập cục bộ, chưa đánh giá dàn ý AI bằng API thật.

Lượt hybrid 60 giây sau đó chạy thật cả dàn ý, viết và audit bằng GPT-5.4, giữ cache phân tích clip 20,02 giây. Gemini đề xuất và xác minh Giáo sư X; bóng giáp cuối clip chưa được nhận diện. Tổng 10 request mới: 3 Gemini (gồm một lỗi 503), 7 GPT-5.4. OpenAI ghi 45.073 input + 30.536 output = 75.609 token. Giá tương đương thông thường khoảng 0,571 USD theo [bảng giá model](https://developers.openai.com/api/docs/models/gpt-5.4), không phải khoản đã thu; chưa truy cập hóa đơn hoặc số ưu đãi còn lại.

Ba bản thoại 60 giây đã nằm trong trần từ; bản cuối 204 từ, nhưng thiếu một citation và humour vẫn 3/5 nên **chưa có kịch bản mới được duyệt, chưa có TTS/render mới**. Nếu thấy MP4/thoại đã duyệt trong thư mục, đó là kết quả trước lượt này. Chi tiết lưu ở `generated/movie-test-short/trial_60s.json`. Prompt v5 sau đó đã sửa cách duyệt bản dịch tiếng Việt, hướng dẫn hai câu đùa theo tình huống và callback ở cuối, dùng năng lực/vai trò nhân vật để phân tích thay vì chỉ thêm tên vào mô tả. V5 đã qua 27 kiểm thử liên quan. Toàn bộ bộ kiểm thử trước sửa prompt đạt 261 test. Không coi test code hoặc điểm do AI tự chấm là chứng minh bài review đã tự nhiên.

Người dùng duyệt thêm 5 request để thử v5: chạy đúng 5 GPT-5.4 (dàn ý, viết, audit, viết lại, audit), 0 Gemini, không upload lại video. Lượt này dùng 30.703 input + 23.577 output = 54.280 token OpenAI; giá tương đương thông thường 0,430 USD, chưa xác nhận khoản thu thực tế. Bộ đếm movie cục bộ của ngày ghi tổng 129.889 token nhóm main qua hai lượt hybrid.

Bản cuối v5 có 224 từ; audit tự chấm mạch kể, tiếng Việt, mở đầu, kết thúc đều 4/5, hài 3/5. Hai câu còn bị chặn vì thiếu ID cảnh mở đầu và cách đổi ngôi trong lời thoại sang lời kể. Không có script mới được duyệt hoặc TTS/render mới. Toàn bộ kết quả ở `generated/movie-test-short/trial_60s_v5.json`; lời thoại thực tế và lý do chưa đạt ở `review_candidate_v5.md`. Giữ trần được người dùng duyệt, không gọi request thứ sáu. Lượt thử cho thấy tăng model chưa tự giải quyết giọng kể hài hước; cần đánh giá phần biên tập bằng mẫu giọng nói cụ thể, đồng thời kiểm tra độ nhất quán của bộ duyệt với diễn giải/lời kể gián tiếp.

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q
```

Nếu sandbox hạn chế thư mục temp Windows, đặt `TEMP`, `TMP`, `BOT_DATA_DIR` và `--basetemp` vào một thư mục thử mới trong `generated/`. FFmpeg smoke chạy clip đỏ + tiếng sine tổng hợp, AI/TTS giả lập rõ ràng. Smoke kiểm tra dựng/cache/rights/audit repair, không đánh giá chất lượng AI thật. Các lượt chạy clip thực tế lưu usage, phản hồi và narration trong thư mục job; đối chiếu kết quả tại đó để đánh giá AI.

Thiết kế và roadmap: [movie-analysis-architecture.md](movie-analysis-architecture.md). Tài liệu API chính thức: [video/audio & FPS](https://ai.google.dev/gemini-api/docs/generate-content/video-understanding), [Files](https://ai.google.dev/gemini-api/docs/files), [models](https://ai.google.dev/gemini-api/docs/models).
# Gemini Live

Cấu hình mới và lệnh test hoàn chỉnh: [gemini-live.md](gemini-live.md). Local dùng `MOVIE_VIDEO_PROVIDER=gemini_live` với `gemini-3.8-live`; nhánh Files API vẫn dùng được khi chọn `gemini`. Live gửi JPEG tối đa 1 FPS và PCM qua WebSocket, không có lượt xem lại FPS cao. Các mô tả Files API/FPS 4–8 phía dưới áp dụng cho nhánh REST.
