# YouTube Shorts documentary bot

Python bot tạo và đăng YouTube Shorts tiếng Anh cùng video dài 5–7 phút. Mỗi lượt chạy hoàn thành toàn bộ chu trình: GPT-5.4 mini nghiên cứu/kịch bản → GPT Image 2 và Brave Images tạo visual → Google Cloud Chirp 3 HD tạo narration tiếng Anh → FFmpeg ghép → YouTube upload.

Phần text gọi OpenAI Responses API bằng `gpt-5.4-mini`: reasoning `low` cho Shorts và `medium` cho video dài. Khi bật chia sẻ input/output cho đúng OpenAI Project, lưu lượng đủ điều kiện sẽ tự dùng hạn mức token miễn phí hằng ngày; không đưa bí mật hoặc dữ liệu riêng tư vào prompt. Phần tạo ảnh vẫn tính phí riêng và giữ `gpt-image-2` chất lượng `low`.

Ảnh AI được gọi trực tiếp từ OpenAI Platform bằng `OPENAI_API_KEY`, model `gpt-image-2`. Bot khóa cứng `quality=low` và dùng `1024x1536` cho dọc hoặc `1536x1024` cho ngang — hai cấu hình có giá đầu ra khoảng `$0.005/ảnh` theo bảng giá chính thức. Bot không còn dùng Pollinations.

Mỗi Short dùng đúng 6 visual. Video dài (kênh giải thích, hình hoạt hình người que) mặc định dùng 15 visual, tất cả do GPT Image 2 tạo; số lượng chỉnh qua `LONG_FORM_AI_IMAGES` (mặc định 15). Mặc định `BRAVE_WEB_IMAGES_PER_LONG_FORM=0`, chỉ lấy ảnh thật từ Brave cho scene mà planner gắn `search_query` là địa danh/công trình/hiện vật thật sự nổi tiếng. Các trang nguồn đã dùng được lưu trong `web_sources.json` và thêm vào mô tả video.

Brave chỉ giúp tìm ảnh, không tự cấp bản quyền sử dụng. Bot giới hạn kết quả vào Wikimedia, Unsplash, Pexels, Pixabay, NASA, Library of Congress và Smithsonian, đồng thời lưu nguồn; người vận hành vẫn cần kiểm tra giấy phép cụ thể nếu kênh có yêu cầu thương mại nghiêm ngặt.

Caption được burn-in bằng FFmpeg/ASS sau khi TTS tạo audio, không nhờ model video vẽ chữ. Bot chia narration thành cụm 3-6 từ, canh timing theo duration thực tế của MP3 sau bước chỉnh tempo, rồi lưu `captions_en.ass` và `captions_vi.ass` trong thư mục `generated/...` để dễ kiểm tra.

Logo thương hiệu nền trong suốt từ `overlay-logo.png` được chèn xuyên suốt ở góc trên bên phải của Shorts, video dài và bản social. Video overlay nhỏ đã tối ưu `360×640`, không có audio, lưu tại `assets/overlay-video.mp4`, **chỉ** chèn ở góc dưới bên phải cho **Shorts và bản social** — **video dài KHÔNG còn chèn video overlay, chỉ giữ logo**. Mặc định video overlay rộng 180 px, cách mép phải 96 px và cách đáy 36 px, góc bo nhẹ. Asset được Git theo dõi và đóng gói cùng ứng dụng, nên cả Short, Long và bản thủ công đều dùng được mà không cần Railway Volume. Khi video Short/social dài hơn overlay quá 5 giây, FFmpeg tự lặp overlay đủ thời lượng; với chênh lệch nhỏ, khung hình cuối được giữ lại thay vì phát lại từ đầu. Có thể chỉnh bằng các biến `OVERLAY_LOGO_*` và `OVERLAY_VIDEO_*`.

Topic Short xoay quanh những chủ thể NỔI TIẾNG, dễ nhận biết ngay — từ quá khứ LẪN hiện tại: nhân vật huyền thoại và những cái tên lớn đương thời, sự kiện lớn xưa và nay, công ty/thương hiệu/sản phẩm biểu tượng, kỳ quan thiên nhiên nổi tiếng, kiến trúc biểu tượng, địa danh – công trình lớn, và các kỷ lục gây choáng. Mỗi video kể MỘT câu chuyện bất ngờ, dễ hiểu, nêu tên đối tượng cụ thể ngay từ đầu. Cổng biên tập yêu cầu chủ thể đủ nổi tiếng (subject_fame ≥ 6/10), góc kể đủ hấp dẫn để chia sẻ (fascination ≥ 6/10) và câu chuyện đủ dễ theo dõi trong một lần xem (clarity ≥ 7/10) trước khi render. Bot vẫn loại tai nạn/vụ việc địa phương vô danh, trivia không ai biết, góc lý thuyết trừu tượng, hành tinh giả định, ẩn dụ học thuật và mẹo tiêu dùng; nhưng nay cho phép phát minh/sản phẩm/công nghệ khi chúng là những cái tên quen thuộc được kể như một câu chuyện con người. Chi tiết xây dựng chỉ dùng để kể về một công trình/kiến trúc cụ thể. Video dài là kênh GIẢI THÍCH/giáo dục với hình minh hoạ NGƯỜI QUE: bot tự sinh chủ đề trải rộng — lịch sử & nền văn minh, người tiền sử, dân tộc & tôn giáo, hình thành Trái Đất và tự nhiên, vũ trụ, chiến tranh và bước ngoặt, nhân vật và sự kiện xưa & nay. Mỗi video giải thích rõ MỘT chủ thể theo trình tự dễ hiểu, hook mạnh ngay câu đầu, tiếng Anh giản dị mỗi câu một ý. Ảnh minh hoạ theo phong cách HOẠT HÌNH người que do AI vẽ đúng hành động/nội dung (săn bắt, hái lượm, dựng lửa…); mặc định 15 ảnh AI mỗi video, chỉnh qua `LONG_FORM_AI_IMAGES`. Cảnh đầu (scene 1) là ảnh người que thể hiện chủ thể chính và được dùng làm thumbnail. Chỉ xen ảnh thật cho địa danh/công trình/hiện vật thật sự nổi tiếng khi hợp. Video dài không chèn video overlay góc (chỉ giữ logo). Không còn cơ chế bản tin RSS/làn thời sự.

## Local setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

Điền `OPENAI_API_KEY`, tùy chọn `BRAVE_SEARCH_API_KEY`, và các file Google credential. Một OpenAI key dùng chung cho text và ảnh; bot không còn cần `GEMINI_API_KEY`. Xem `.env.example` để biết biến cần thiết. Các JSON credential và `.env` không được commit Git.

Chạy một video ngay:

```powershell
python youtube_shorts_bot.py --publish --duration 25
```

`--scheduled` bật chốt an toàn theo `SCHEDULED_DAILY_LIMIT` trong một ngày UTC. Mặc định là `2`; đặt `0` nếu muốn tắt giới hạn này.

```powershell
python youtube_shorts_bot.py --publish --scheduled
```

## Railway: nhiều video/ngày theo Cron

File `railway.toml` đã đặt start command cho Railway:

```text
python youtube_shorts_bot.py --publish --scheduled
```

`nixpacks.toml` cài `ffmpeg` và `ffprobe` vào container Railway. Hai công cụ này là bắt buộc để tạo chuyển động Ken Burns, nối cảnh và ghép voice-over.

Tại Railway, vào Service → **Settings → Cron Schedule** và đặt:

```text
0 0,12 * * *
```

Railway chạy Cron theo UTC. Lịch này chạy vào 00:00 và 12:00 UTC, tương ứng 07:00 và 19:00 giờ Việt Nam (UTC+7), tức 2 Shorts mỗi ngày. Với tối đa 6 ảnh GPT Image 2 `low`, chi phí đầu ra ảnh tối đa khoảng `$0.03/Short`; ảnh Brave thay thế được cảnh nào thì chi phí OpenAI giảm tương ứng.

## Chạy trên Railway KHÔNG dùng Volume (Khuyên dùng)

Để tránh tình trạng hết dung lượng (do các video cũ tồn đọng), bot hỗ trợ chạy hoàn toàn trên bộ nhớ tạm của container và lưu trữ dữ liệu vào **PostgreSQL Database**.

1. Deploy repository thành một Railway service.
2. Trong project canvas, nhấn `Ctrl/Cmd + K` → **Database** → **Add PostgreSQL**.
3. Kết nối Database vừa tạo vào Service của bot. Railway sẽ tự động tạo biến `DATABASE_URL`. Bot sẽ tự nhận diện và tự động tạo bảng dữ liệu, bạn không cần dùng Volume.
4. Vì không có Volume, mọi dữ liệu cục bộ sẽ mất khi container restart. Do đó, bạn cần nạp 3 file JSON cấu hình bằng biến môi trường Base64.

Trên máy local, chạy từng lệnh sau. Mỗi lệnh đưa Base64 vào clipboard, không in secret ra màn hình:

```powershell
[Convert]::ToBase64String([IO.File]::ReadAllBytes('.\google_tts_service_account.json')) | Set-Clipboard
[Convert]::ToBase64String([IO.File]::ReadAllBytes('.\client_secrets.json')) | Set-Clipboard
[Convert]::ToBase64String([IO.File]::ReadAllBytes('.\youtube_token.json')) | Set-Clipboard
```

Sau mỗi lệnh, trong Railway → **Variables** của service bot, tạo một variable rồi dán clipboard tương ứng:

```text
GOOGLE_TTS_SERVICE_ACCOUNT_JSON_B64
YOUTUBE_CLIENT_SECRETS_JSON_B64
YOUTUBE_TOKEN_JSON_B64
```

> **Lưu ý**: Lần đầu tiên chạy, bot sẽ load token từ `YOUTUBE_TOKEN_JSON_B64`. Khi token được YouTube tự động cấp lại, bot sẽ tự động cập nhật nó vào Database (bảng `kv_store`), nên bạn không cần cập nhật lại biến `YOUTUBE_TOKEN_JSON_B64` nữa! Các thư mục video `generated/` cũng sẽ tự động được xóa đi sau khi đăng thành công để tiết kiệm không gian đĩa.

Trong Railway → **Variables**, thêm:

```dotenv
OPENAI_API_KEY=your_openai_platform_key
OPENAI_TEXT_MODEL=gpt-5.4-mini
OPENAI_TEXT_REASONING_EFFORT=low
OPENAI_TEXT_LONG_FORM_REASONING_EFFORT=medium
OPENAI_TEXT_MAX_OUTPUT_TOKENS=16000
OPENAI_TEXT_ATTEMPTS=3
OPENAI_TEXT_RETRY_BACKOFF_SECONDS=5
OPENAI_TEXT_CONNECT_TIMEOUT_SECONDS=30
OPENAI_TEXT_READ_TIMEOUT_SECONDS=300
BRAVE_SEARCH_API_KEY=your_optional_brave_key
BRAVE_WEB_IMAGES_PER_SHORT=2
BRAVE_WEB_IMAGES_PER_LONG_FORM=0
BRAVE_SEARCH_MIN_INTERVAL_SECONDS=2.0
BRAVE_SEARCH_ATTEMPTS=3
OPENAI_IMAGE_ATTEMPTS=3
OPENAI_IMAGE_RETRY_BACKOFF_SECONDS=10
OVERLAY_LOGO_FILE=overlay-logo.png
OVERLAY_LOGO_SHORT_WIDTH=220
OVERLAY_LOGO_LONG_FORM_WIDTH=220
OVERLAY_LOGO_MARGIN=36
OVERLAY_LOGO_SHORT_TOP_MARGIN=72
OVERLAY_LOGO_LONG_FORM_TOP_MARGIN=36
OVERLAY_VIDEO_FILE=assets/overlay-video.mp4
OVERLAY_VIDEO_SHORT_WIDTH=180
OVERLAY_VIDEO_LONG_FORM_WIDTH=160
OVERLAY_VIDEO_RIGHT_MARGIN=96
OVERLAY_VIDEO_BOTTOM_MARGIN=36
OVERLAY_VIDEO_CORNER_RADIUS=18
OVERLAY_VIDEO_LOOP_GAP_SECONDS=5
SHORT_DURATION_SECONDS=60
SCHEDULED_DAILY_LIMIT=2
GOOGLE_TTS_SERVICE_ACCOUNT_FILE=google_tts_service_account.json
GOOGLE_TTS_VOICE=en-US-Chirp3-HD-Leda
GOOGLE_TTS_SPEAKING_RATE=1.05
# Vietnamese Short narration for Facebook/TikTok via OpenAI gpt-4o-mini-tts
SOCIAL_OPENAI_TTS_VOICE=nova
SOCIAL_OPENAI_TTS_SPEED=1.0
YOUTUBE_CLIENT_SECRETS=client_secrets.json
YOUTUBE_TOKEN_FILE=youtube_token.json
YOUTUBE_PRIVACY_STATUS=private
PUBLISH_FACEBOOK=false
FACEBOOK_GRAPH_VERSION=v25.0
FACEBOOK_PAGE_ID=
FACEBOOK_PAGE_ACCESS_TOKEN=
FACEBOOK_USER_ACCESS_TOKEN=
PUBLISH_TIKTOK=false
TIKTOK_ACCESS_TOKEN=
TIKTOK_PRIVACY_LEVEL=SELF_ONLY
TIKTOK_DISABLE_DUET=false
TIKTOK_DISABLE_COMMENT=false
TIKTOK_DISABLE_STITCH=false
```

`OPENAI_TEXT_ATTEMPTS`/`OPENAI_TEXT_RETRY_BACKOFF_SECONDS` và `OPENAI_IMAGE_ATTEMPTS`/`OPENAI_IMAGE_RETRY_BACKOFF_SECONDS` giúp cron chịu được lỗi mạng/rate-limit tạm thời. Chất lượng và kích thước GPT Image 2 không có biến môi trường để nâng lên, tránh vô tình tiêu credits ở mức `medium/high`.

Narration tiếng Anh của cả Short và Long dùng Google Cloud Chirp 3 HD, mặc định `en-US-Chirp3-HD-Leda` (giọng nữ); có thể đổi bằng `GOOGLE_TTS_VOICE` và `GOOGLE_TTS_SPEAKING_RATE`. Khi bật `PUBLISH_FACEBOOK=true` hoặc `PUBLISH_TIKTOK=true`, bot vẫn upload `short.mp4` tiếng Anh lên YouTube, sau đó tạo `social_vi.json` và `short_vi.mp4` tiếng Việt từ cùng `visuals.mp4` để publish lên Facebook/TikTok. Voice tiếng Việt dùng OpenAI `gpt-4o-mini-tts`, mặc định `nova` với phong cách “emo teenager”; có thể đổi qua `SOCIAL_OPENAI_TTS_VOICE` và `SOCIAL_OPENAI_TTS_SPEED`. Trước khi gọi Brave hoặc GPT Image 2, bot luôn tạo audio tiếng Anh và lấy thời lượng thật làm timeline: các scene sẽ được co/giãn theo audio, nên không cần tạo lại voice chỉ để khớp thời lượng. Bản tiếng Việt cũng tự co/giãn từ visual đã có, không tạo thêm ảnh. Facebook dùng Meta Graph Video API cho Page, cần `FACEBOOK_PAGE_ID` và Page access token có quyền publish video. Nếu Page token hết hạn hoặc bạn muốn bot tự lấy token Page mỗi lần chạy, đặt thêm `FACEBOOK_USER_ACCESS_TOKEN` là long-lived user token có quyền quản lý Page; bot sẽ gọi `/me/accounts` để lấy Page token khớp `FACEBOOK_PAGE_ID`. TikTok dùng Content Posting API Direct Post, cần app có Content Posting API, scope `video.publish`, và `TIKTOK_ACCESS_TOKEN` của tài khoản đã authorize; app chưa audit thường chỉ post được ở chế độ private/`SELF_ONLY`.

Không thêm `BOT_DATA_DIR=.` vào Railway Variables: điều đó sẽ làm bot ghi dữ liệu vào filesystem tạm thay vì Volume. Để kiểm tra file trên Volume, Railway CLI hỗ trợ `railway volume files list /`.

Railway Cron yêu cầu process hoàn tất và thoát; bot đã là one-shot process. Nếu một lần render/upload chưa xong khi lượt Cron kế tiếp đến, Railway sẽ bỏ qua lượt mới. [Railway Cron Jobs](https://docs.railway.com/cron-jobs) · [Railway Volumes](https://docs.railway.com/volumes)

## Long-form horizontal videos

Video dài chạy end-to-end trong một lượt: lập kế hoạch → tạo audio → co/giãn scene theo audio → chuẩn bị visual hoạt hình người que (mặc định 15 ảnh AI, chỉnh qua `LONG_FORM_AI_IMAGES`; `BRAVE_WEB_IMAGES_PER_LONG_FORM=0`, chỉ lấy ảnh Brave thật cho scene có `search_query` là địa danh/công trình/hiện vật nổi tiếng) → render (chỉ chèn logo, không chèn video overlay góc) → tạo thumbnail 16:9 → upload lên YouTube. Thumbnail lấy ảnh scene 1 (người que thể hiện chủ thể chính), phủ chữ lớn từ `thumbnail_text` (tên nhân vật/địa danh/sự kiện chính), nên không gọi GPT Image 2 thêm lần nào và không tốn thêm image credit. Bot upload `thumbnail.jpg` qua YouTube API sau khi video Long hoàn tất. Voice được dùng đúng một lần; video tự khớp với thời lượng voice nên không có đoạn im lặng cuối và không cần tiêu voice/ảnh lần hai chỉ để sửa thời lượng. Nếu Railway bị restart sau khi audio và đủ số ảnh đã tạo, lượt chạy Long kế tiếp sẽ tự tiếp tục job dở từ Volume, tái dùng audio/ảnh và các scene clip hợp lệ; không dùng `--long-form-force-new`. Render ghi log từng scene, dùng scale trung gian 2304px đủ cho 1080p. Nếu một visual web không render được trong 2 phút, bot thay bằng visual scene trước và render lại, không gọi thêm TTS hoặc image API. Video dài không được đăng sang Facebook/TikTok và không tạo bản tiếng Việt. Không còn chế độ `prepare/finalize`, không cần Volume để giữ ảnh tạo dần:

```powershell
python youtube_shorts_bot.py --long-form --publish
```

Cron khuyến nghị cho service Railway riêng:

```text
0 13 * * *
```

Cron gọi service mỗi ngày lúc 20:00 Việt Nam. Bot kiểm tra ngày local trong PostgreSQL và chỉ tạo khi video dài gần nhất đã cách ít nhất `LONG_FORM_INTERVAL_DAYS=2` ngày: ví dụ tạo thứ Hai thì thứ Tư mới tạo tiếp. Dùng `--long-form-force-new` chỉ khi muốn chạy thủ công và bỏ qua chốt này.

Long-form config:

```dotenv
LONG_FORM_MIN_DURATION_SECONDS=300
LONG_FORM_MAX_DURATION_SECONDS=420
LONG_FORM_TIMEZONE=Asia/Bangkok
LONG_FORM_INTERVAL_DAYS=2
BRAVE_WEB_IMAGES_PER_LONG_FORM=0
```

Nội dung video dài là kênh GIẢI THÍCH/giáo dục: bot tự sinh chủ đề (không dùng RSS/lane thời sự) từ 9 nhóm — lịch sử & nền văn minh, người tiền sử, dân tộc & tôn giáo, Trái Đất & tự nhiên, vũ trụ, chiến tranh & bước ngoặt, nhân vật, sự kiện, và nguồn gốc mọi thứ — cả xưa lẫn nay. Mỗi lượt chọn ngẫu nhiên một nhóm (tránh lặp trong cùng lần thử) và kiểm tra trùng chủ thể với archive; nếu trùng thì đổi nhóm và thử lại. Kịch bản giải thích rõ MỘT chủ thể theo trình tự dễ hiểu, dựa trên kiến thức phổ thông đã được xác lập (không bịa số liệu/ngày tháng/trích dẫn), đối xử tôn trọng và trung lập với dân tộc/tôn giáo, và tiếp tục loại nội dung liên quan Việt Nam. Mặc định 15 ảnh AI hoạt hình người que có chi phí đầu ra khoảng `$0.075/video dài` (chỉnh qua `LONG_FORM_AI_IMAGES`); nếu một scene được gắn `search_query` địa danh/công trình nổi tiếng thì lấy ảnh thật từ Brave trước. PostgreSQL lưu lịch và archive, còn toàn bộ media được tạo và hoàn tất trong cùng một lượt chạy.

## Nhập ý tưởng thủ công (frontend web)

Ngoài 2 luồng **auto** (Short Cron + Long Cron chạy như trên, không đổi), bot có thêm **luồng thủ công**: một trang web để bạn tự nhập ý tưởng, sau đó bot viết kịch bản, tạo ảnh, lồng tiếng, render và đăng YouTube y như luồng auto. Ý tưởng thủ công **tự do hoàn toàn** — bỏ qua bộ lọc tầm vóc, chống-trùng, ràng buộc lane tin tức và **cả bộ chặn Việt Nam**; chỉ giữ quy tắc không bịa số liệu/nguồn. Gõ ý tưởng bằng tiếng Việt hay tiếng Anh đều được, video xuất ra tiếng Anh (Short còn tự tạo bản tiếng Việt cho Facebook/TikTok nếu bạn đã bật).

Cách hoạt động: trang web ghi ý tưởng vào bảng `idea_queue` trong cùng PostgreSQL. Một worker chạy nền lần lượt nhặt từng ý tưởng và gọi `python youtube_shorts_bot.py --idea-id <id>` (render tuần tự 1 video một lúc để không quá tải). Trang có bảng theo dõi trạng thái (Chờ → Đang tạo → Xong/Lỗi) kèm link YouTube, tự làm mới mỗi 5 giây.

Chạy thử local:

```powershell
$env:WEB_ACCESS_TOKEN = "mat-khau-cua-ban"
python web_app.py
# Mở http://localhost:8080
```

Không đặt `WEB_ACCESS_TOKEN` thì trang mở tự do — chỉ nên vậy khi chạy local. Có thể chạy thử kế hoạch mà không tốn ảnh/voice/upload bằng:

```powershell
python youtube_shorts_bot.py --idea "Câu chuyện xây kênh đào Suez" --dry-run
python youtube_shorts_bot.py --idea "Trận Điện Biên Phủ 1954" --long-form --dry-run
```

### Triển khai Railway (service Web thứ 3)

Tạo thêm **một Railway service mới** từ chính repo này (2 service Cron auto giữ nguyên):

1. New Service → Deploy from repo (cùng repo).
2. **Settings → Custom Start Command**: `python web_app.py`. **KHÔNG** đặt Cron Schedule (service này luôn bật).
3. **Variables**: copy đúng các biến như service auto (`OPENAI_API_KEY`, `DATABASE_URL` trỏ về cùng PostgreSQL, và các credential Google/YouTube hoặc bản `*_JSON_B64`), rồi thêm:

```dotenv
WEB_ACCESS_TOKEN=mot-mat-khau-manh
FLASK_SECRET_KEY=mot-chuoi-ngau-nhien-dai
# YOUTUBE_PRIVACY_STATUS quyết định mặc định private/unlisted/public trên form.
```

Railway tự cấp `PORT`; không cần đặt thủ công. Vì dùng chung `DATABASE_URL` với luồng auto, video thủ công vẫn được lưu vào archive nên chống-trùng của luồng auto vẫn tính cả các video này. Container build từ cùng repo nên đã có sẵn `ffmpeg` để render.
