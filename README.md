# YouTube Shorts documentary bot

Python bot tạo và đăng YouTube Shorts tiếng Anh cùng video dài 5–7 phút. Mỗi lượt chạy hoàn thành toàn bộ chu trình: GPT-5.4 mini nghiên cứu/kịch bản → GPT Image 2 và Brave Images tạo visual → Google Cloud Chirp 3 HD tạo narration → FFmpeg ghép → YouTube upload.

Phần text gọi OpenAI Responses API bằng `gpt-5.4-mini`: reasoning `low` cho Shorts và `medium` cho video dài. Khi bật chia sẻ input/output cho đúng OpenAI Project, lưu lượng đủ điều kiện sẽ tự dùng hạn mức token miễn phí hằng ngày; không đưa bí mật hoặc dữ liệu riêng tư vào prompt. Phần tạo ảnh vẫn tính phí riêng và giữ `gpt-image-2` chất lượng `low`.

Ảnh AI được gọi trực tiếp từ OpenAI Platform bằng `OPENAI_API_KEY`, model `gpt-image-2`. Bot khóa cứng `quality=low` và dùng `1024x1536` cho dọc hoặc `1536x1024` cho ngang — hai cấu hình có giá đầu ra khoảng `$0.005/ảnh` theo bảng giá chính thức. Bot không còn dùng Pollinations.

Mỗi Short dùng đúng 6 visual. Mỗi video dài dùng 15 visual: tối đa 5 ảnh Brave và 10 ảnh GPT Image 2. Nếu Brave thiếu ảnh cho video dài, bot lặp lại visual đã có thay vì gọi quá 10 ảnh OpenAI. Các trang nguồn đã sử dụng được lưu trong `web_sources.json` và thêm vào mô tả video.

Brave chỉ giúp tìm ảnh, không tự cấp bản quyền sử dụng. Bot giới hạn kết quả vào Wikimedia, Unsplash, Pexels, Pixabay, NASA, Library of Congress và Smithsonian, đồng thời lưu nguồn; người vận hành vẫn cần kiểm tra giấy phép cụ thể nếu kênh có yêu cầu thương mại nghiêm ngặt.

Caption được burn-in bằng FFmpeg/ASS sau khi TTS tạo audio, không nhờ model video vẽ chữ. Bot chia narration thành cụm 3-6 từ, canh timing theo duration thực tế của MP3 sau bước chỉnh tempo, rồi lưu `captions_en.ass` và `captions_vi.ass` trong thư mục `generated/...` để dễ kiểm tra.

Logo thương hiệu nền trong suốt từ `overlay-logo.png` được chèn xuyên suốt ở góc trên bên phải của Shorts, video dài và bản social. Mặc định logo rộng 220 px ở cả hai định dạng, cách mép phải 36 px; logo Short cách mép trên 72 px và logo video dài cách mép trên 36 px. Có thể chỉnh bằng các biến `OVERLAY_LOGO_*`.

Topic Short giữ đúng phong cách documentary về nhân vật và sự kiện lịch sử, nền văn minh, chiến tranh, đế chế, kỳ quan thiên nhiên, kiến trúc xưa–nay, công trình nổi tiếng, thảm họa và lịch sử văn hóa. Bot không chọn chủ đề độc lập về phát minh/kỹ thuật, động vật, khảo cổ, khám phá khoa học hoặc thám hiểm không gian; chi tiết xây dựng chỉ được dùng để kể về một công trình hay kiến trúc cụ thể. Mỗi video phải chọn một câu chuyện như quyết định, cách xây dựng, trở ngại, sai lầm, bước ngoặt, kỷ lục hoặc chi tiết ít người biết để người xem dễ hiểu và kể lại. Planner loại góc lý thuyết, hành tinh giả định và ẩn dụ học thuật; không chuyển nội dung thành mẹo tiêu dùng. Video dài chỉ tập trung tin thế giới, chính trị, quân sự, kinh tế, công nghệ và thể thao; không lấy chủ đề khoa học.

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
BRAVE_WEB_IMAGES_PER_LONG_FORM=5
OPENAI_IMAGE_ATTEMPTS=3
OPENAI_IMAGE_RETRY_BACKOFF_SECONDS=10
OVERLAY_LOGO_FILE=overlay-logo.png
OVERLAY_LOGO_SHORT_WIDTH=220
OVERLAY_LOGO_LONG_FORM_WIDTH=220
OVERLAY_LOGO_MARGIN=36
OVERLAY_LOGO_SHORT_TOP_MARGIN=72
OVERLAY_LOGO_LONG_FORM_TOP_MARGIN=36
SHORT_DURATION_SECONDS=60
SCHEDULED_DAILY_LIMIT=2
GOOGLE_TTS_SERVICE_ACCOUNT_FILE=google_tts_service_account.json
GOOGLE_TTS_VOICE=en-US-Chirp3-HD-Enceladus
GOOGLE_TTS_SPEAKING_RATE=1.05
# Vietnamese Short narration for Facebook/TikTok via OpenAI gpt-4o-mini-tts
SOCIAL_OPENAI_TTS_VOICE=ash
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

Khi bật `PUBLISH_FACEBOOK=true` hoặc `PUBLISH_TIKTOK=true`, bot vẫn upload `short.mp4` tiếng Anh lên YouTube, sau đó tạo `social_vi.json` và `short_vi.mp4` tiếng Việt từ cùng `visuals.mp4` để publish lên Facebook/TikTok. Voice tiếng Việt dùng OpenAI `gpt-4o-mini-tts` với mặc định `ash` và phong cách “patient teacher”; có thể đổi qua `SOCIAL_OPENAI_TTS_VOICE` và `SOCIAL_OPENAI_TTS_SPEED`. Trước khi gọi Brave hoặc GPT Image 2, bot luôn tạo audio tiếng Anh và lấy thời lượng thật làm timeline: các scene sẽ được co/giãn theo audio, nên không cần tạo lại voice chỉ để khớp thời lượng. Bản tiếng Việt cũng tự co/giãn từ visual đã có, không tạo thêm ảnh. Facebook dùng Meta Graph Video API cho Page, cần `FACEBOOK_PAGE_ID` và Page access token có quyền publish video. Nếu Page token hết hạn hoặc bạn muốn bot tự lấy token Page mỗi lần chạy, đặt thêm `FACEBOOK_USER_ACCESS_TOKEN` là long-lived user token có quyền quản lý Page; bot sẽ gọi `/me/accounts` để lấy Page token khớp `FACEBOOK_PAGE_ID`. TikTok dùng Content Posting API Direct Post, cần app có Content Posting API, scope `video.publish`, và `TIKTOK_ACCESS_TOKEN` của tài khoản đã authorize; app chưa audit thường chỉ post được ở chế độ private/`SELF_ONLY`.

Không thêm `BOT_DATA_DIR=.` vào Railway Variables: điều đó sẽ làm bot ghi dữ liệu vào filesystem tạm thay vì Volume. Để kiểm tra file trên Volume, Railway CLI hỗ trợ `railway volume files list /`.

Railway Cron yêu cầu process hoàn tất và thoát; bot đã là one-shot process. Nếu một lần render/upload chưa xong khi lượt Cron kế tiếp đến, Railway sẽ bỏ qua lượt mới. [Railway Cron Jobs](https://docs.railway.com/cron-jobs) · [Railway Volumes](https://docs.railway.com/volumes)

## Long-form horizontal videos

Video dài chạy end-to-end trong một lượt: lập kế hoạch → tạo audio → co/giãn scene theo audio → chuẩn bị 15 visual (5 Brave + 10 OpenAI) → render → tạo thumbnail 16:9 → upload lên YouTube. Thumbnail lấy visual hook đã có, phủ chữ lớn từ `thumbnail_text` (tên nhân vật/địa danh/sự kiện chính), nên không gọi GPT Image 2 thêm lần nào và không tốn thêm image credit. Bot upload `thumbnail.jpg` qua YouTube API sau khi video Long hoàn tất. Voice được dùng đúng một lần; video tự khớp với thời lượng voice nên không có đoạn im lặng cuối và không cần tiêu voice/ảnh lần hai chỉ để sửa thời lượng. Nếu Railway bị restart sau khi audio và đủ 15 ảnh đã tạo, lượt chạy Long kế tiếp sẽ tự tiếp tục job dở từ Volume, tái dùng audio/ảnh và các scene clip hợp lệ; không dùng `--long-form-force-new`. Render ghi log từng scene, dùng scale trung gian 2304px đủ cho 1080p. Nếu một visual web không render được trong 2 phút, bot thay bằng visual scene trước và render lại, không gọi thêm TTS hoặc image API. Video dài không được đăng sang Facebook/TikTok và không tạo bản tiếng Việt. Không còn chế độ `prepare/finalize`, không cần Volume để giữ ảnh tạo dần:

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
BRAVE_WEB_IMAGES_PER_LONG_FORM=5
```

Nội dung video dài dùng Google News RSS làm lead cho tin thế giới, chính trị/quân sự, kinh tế, công nghệ và thể thao. Feed khoa học đã bị loại; planner cũng từ chối nghiên cứu khoa học, khí hậu, không gian, y khoa, khảo cổ và tin học thuật lọt vào mục tin tổng hợp. Kịch bản ưu tiên thay đổi cụ thể, người bị ảnh hưởng, hệ quả thực tế và diễn biến cần theo dõi; đồng thời tiếp tục loại nội dung liên quan Việt Nam. Mười ảnh OpenAI `low` có chi phí đầu ra khoảng `$0.05/video dài`; Brave không tìm đủ thì visual được lặp lại nên mức OpenAI này không tăng. PostgreSQL lưu lịch và archive, còn toàn bộ media được tạo và hoàn tất trong cùng một lượt chạy.
