# YouTube Shorts documentary bot

Python bot tạo và đăng YouTube Shorts tiếng Anh 10–20 giây. Mỗi lượt chạy hoàn thành toàn bộ chu trình: Grok nghiên cứu/kịch bản → LTX-2 tạo video → Google Chirp 3 HD tạo narration → FFmpeg ghép → YouTube upload.

Grok dùng `POLLINATIONS_GROK_API_KEY`; LTX-2 dùng `POLLINATIONS_VIDEO_API_KEY`. Hai key độc lập giúp chi phí reasoning không ăn vào quota video.

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
python youtube_shorts_bot.py --publish --duration 20
```

`--scheduled` bật chốt an toàn: không tạo quá 3 job mới trong một ngày UTC.

```powershell
python youtube_shorts_bot.py --publish --scheduled
```

## Railway: 3 video/ngày, cách nhau 6 giờ

File `railway.toml` đã đặt start command cho Railway:

```text
python youtube_shorts_bot.py --publish --scheduled
```

Tại Railway, vào Service → **Settings → Cron Schedule** và đặt:

```text
0 0,6,12 * * *
```

Railway chạy Cron theo UTC. Lịch này chạy vào 00:00, 06:00 và 12:00 UTC, tương ứng 07:00, 13:00 và 19:00 giờ Việt Nam (UTC+7). Mỗi lần chạy tạo/upload một video ngay; video LTX-2 dài 20 giây tốn khoảng `0.1` Pollen trên key video, nằm dưới quota `0.15` Pollen/giờ mà bạn nêu.

## Tạo và dùng Railway Volume

Volume giữ `shorts.db` (chống trùng), `generated/`, YouTube refresh token và hai Google JSON. Nếu không có Volume, Railway có thể mất các file này sau redeploy hoặc giữa các lần Cron.

1. Deploy repository thành một Railway service.
2. Trong project canvas, nhấn `Ctrl/Cmd + K` → **New Volume** (hoặc chuột phải vào canvas → **New Volume**).
3. Chọn service của bot khi Railway hỏi service cần kết nối.
4. Đặt **Mount Path** là `/app/data`.
5. Railway tự đặt biến `RAILWAY_VOLUME_MOUNT_PATH`; bot tự phát hiện biến này, nên không cần đặt `BOT_DATA_DIR` trên Railway.
6. Từ máy local, dùng Railway CLI để upload ba file credential vào Volume:

```powershell
railway login
railway link
railway volume files upload .\google_tts_service_account.json /google_tts_service_account.json
railway volume files upload .\client_secrets.json /client_secrets.json
railway volume files upload .\youtube_token.json /youtube_token.json
```

`youtube_token.json` phải được tạo ở local trước bằng một lần chạy `--publish`, vì OAuth Desktop không thể mở trình duyệt cấp quyền trong Railway Cron headless.

Trong Railway → **Variables**, thêm:

```dotenv
POLLINATIONS_GROK_API_KEY=sk_key_for_grok
POLLINATIONS_VIDEO_API_KEY=sk_key_for_ltx2
POLLINATIONS_BASE_URL=https://gen.pollinations.ai
SHORT_DURATION_SECONDS=20
GOOGLE_TTS_SERVICE_ACCOUNT_FILE=google_tts_service_account.json
GOOGLE_TTS_VOICE=en-US-Chirp3-HD-Achernar
GOOGLE_TTS_SPEAKING_RATE=1.05
YOUTUBE_CLIENT_SECRETS=client_secrets.json
YOUTUBE_TOKEN_FILE=youtube_token.json
YOUTUBE_PRIVACY_STATUS=private
```

Không thêm `BOT_DATA_DIR=.` vào Railway Variables: điều đó sẽ làm bot ghi dữ liệu vào filesystem tạm thay vì Volume. Để kiểm tra file trên Volume, Railway CLI hỗ trợ `railway volume files list /`.

Railway Cron yêu cầu process hoàn tất và thoát; bot đã là one-shot process. Nếu một lần render/upload chưa xong khi lượt Cron kế tiếp đến, Railway sẽ bỏ qua lượt mới. [Railway Cron Jobs](https://docs.railway.com/cron-jobs) · [Railway Volumes](https://docs.railway.com/volumes)
