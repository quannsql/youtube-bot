# YouTube Shorts documentary bot

Python bot tạo và đăng YouTube Shorts tiếng Anh 10–25 giây. Mỗi lượt chạy hoàn thành toàn bộ chu trình: Grok nghiên cứu/kịch bản → LTX-2 tạo video → Google Chirp 3 HD tạo narration → FFmpeg ghép → YouTube upload.

Grok dùng `POLLINATIONS_GROK_API_KEY`; LTX-2 dùng `POLLINATIONS_VIDEO_API_KEY`. Hai key độc lập giúp chi phí reasoning không ăn vào quota video.

Visual prompt mặc định đi theo hướng animated documentary explainer: minh họa 2D/3D, cel-shaded/paper texture, bản đồ, diagram, cutaway bảo tàng và cận cảnh hiện vật/fossil; tránh photorealistic/live-action để giảm cảm giác video AI giả thật.

Caption được burn-in bằng FFmpeg/ASS sau khi TTS tạo audio, không nhờ model video vẽ chữ. Bot chia narration thành cụm 3-6 từ, canh timing theo duration thực tế của MP3 sau bước chỉnh tempo, rồi lưu `captions_en.ass` và `captions_vi.ass` trong thư mục `generated/...` để dễ kiểm tra.

Topic prompt mặc định ưu tiên nội dung có sức tò mò đại chúng: bí ẩn, thảm họa, biến mất, sụp đổ, thành phố/đế chế thất lạc, câu hỏi "what if", giới hạn khoa học, hoặc một câu chuyện quen thuộc có cú đảo dựa trên bằng chứng. Đây là pattern định hướng để Grok tự suy luận topic mới, không phải danh sách chủ đề cố định.

## Local setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

Điền hai key Pollinations và các file Google credential. Xem `.env.example` để biết biến cần thiết. Các JSON credential và `.env` không được commit Git.

Chạy một video ngay:

```powershell
python youtube_shorts_bot.py --publish --duration 25
```

`--scheduled` bật chốt an toàn theo `SCHEDULED_DAILY_LIMIT` trong một ngày UTC. Mặc định là `3`; đặt `0` nếu muốn tắt giới hạn này.

```powershell
python youtube_shorts_bot.py --publish --scheduled
```

## Railway: nhiều video/ngày theo Cron

File `railway.toml` đã đặt start command cho Railway:

```text
python youtube_shorts_bot.py --publish --scheduled
```

`nixpacks.toml` cài `ffmpeg` và `ffprobe` vào container Railway. Hai công cụ này là bắt buộc để nối các cảnh LTX-2 và ghép voice-over.

Tại Railway, vào Service → **Settings → Cron Schedule** và đặt:

```text
0 0,6,12 * * *
```

Railway chạy Cron theo UTC. Lịch này chạy vào 00:00, 06:00 và 12:00 UTC, tương ứng 07:00, 13:00 và 19:00 giờ Việt Nam (UTC+7). Mỗi lần chạy tạo/upload một video ngay, miễn là chưa chạm `SCHEDULED_DAILY_LIMIT`. Muốn nhiều hơn 3 video/ngày thì tăng cả Cron Schedule và `SCHEDULED_DAILY_LIMIT`. Video LTX-2 dài 25 giây tốn khoảng `0.125` Pollen trên key video, vẫn có thể tận dụng `LTX_FALLBACK_TO_GROK_KEY=true` khi key video hết quota hoặc bị rate-limit.

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
POLLINATIONS_GROK_API_KEY=sk_key_for_grok
POLLINATIONS_VIDEO_API_KEY=sk_key_for_ltx2
POLLINATIONS_BASE_URL=https://gen.pollinations.ai
POLLINATIONS_CONNECT_TIMEOUT_SECONDS=30
POLLINATIONS_READ_TIMEOUT_SECONDS=180
LTX_SCENE_ATTEMPTS=3
LTX_SCENE_RETRY_BACKOFF_SECONDS=20
LTX_FALLBACK_TO_GROK_KEY=true
SHORT_DURATION_SECONDS=25
SCHEDULED_DAILY_LIMIT=3
GOOGLE_TTS_SERVICE_ACCOUNT_FILE=google_tts_service_account.json
GOOGLE_TTS_VOICE=en-US-Chirp3-HD-Achernar
GOOGLE_TTS_SPEAKING_RATE=1.05
SOCIAL_TTS_VOICE=vi-VN-Standard-A
SOCIAL_TTS_SPEAKING_RATE=1.05
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

`LTX_SCENE_ATTEMPTS` và `LTX_SCENE_RETRY_BACKOFF_SECONDS` giúp cron chịu được lỗi tạm thời từ Pollinations/LTX-2 khi render từng scene. `LTX_FALLBACK_TO_GROK_KEY=true` cho phép LTX-2 thử lại bằng `POLLINATIONS_GROK_API_KEY` khi `POLLINATIONS_VIDEO_API_KEY` hết quota hoặc bị rate-limit. Bot ghi scene vào file `.part` trước, chỉ giữ lại MP4 khi tải xong.

Khi bật `PUBLISH_FACEBOOK=true` hoặc `PUBLISH_TIKTOK=true`, bot vẫn upload `short.mp4` tiếng Anh lên YouTube, sau đó tạo `social_vi.json` và `short_vi.mp4` tiếng Việt từ cùng `visuals.mp4` để publish lên Facebook/TikTok. Facebook dùng Meta Graph Video API cho Page, cần `FACEBOOK_PAGE_ID` và Page access token có quyền publish video. Nếu Page token hết hạn hoặc bạn muốn bot tự lấy token Page mỗi lần chạy, đặt thêm `FACEBOOK_USER_ACCESS_TOKEN` là long-lived user token có quyền quản lý Page; bot sẽ gọi `/me/accounts` để lấy Page token khớp `FACEBOOK_PAGE_ID`. TikTok dùng Content Posting API Direct Post, cần app có Content Posting API, scope `video.publish`, và `TIKTOK_ACCESS_TOKEN` của tài khoản đã authorize; app chưa audit thường chỉ post được ở chế độ private/`SELF_ONLY`.

Không thêm `BOT_DATA_DIR=.` vào Railway Variables: điều đó sẽ làm bot ghi dữ liệu vào filesystem tạm thay vì Volume. Để kiểm tra file trên Volume, Railway CLI hỗ trợ `railway volume files list /`.

Railway Cron yêu cầu process hoàn tất và thoát; bot đã là one-shot process. Nếu một lần render/upload chưa xong khi lượt Cron kế tiếp đến, Railway sẽ bỏ qua lượt mới. [Railway Cron Jobs](https://docs.railway.com/cron-jobs) · [Railway Volumes](https://docs.railway.com/volumes)
