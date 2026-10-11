# Video Studio: chạy local và triển khai Railway

Dashboard mở rộng từ Flask và hàng đợi của bot cũ. Có ba chế độ: Short so sánh, video giải thích, review phim. Review phim nhận MP4, có bối cảnh/hồ sơ nhân vật, phân tích → kiểm chứng → viết/duyệt thoại → Google TTS → dựng MP4 1080×1920 có phụ đề. Job dừng nếu kiểm chứng hoặc duyệt thoại không đạt; giao diện hiển thị lỗi và cho tiếp tục từ cache.

## Chạy local

Trong PowerShell tại `D:\video-bot`:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe web_app.py
```

Mở `http://localhost:8080`. Nếu đã đặt `WEB_ACCESS_TOKEN` trong `.env`, dùng giá trị đó làm mật khẩu. Server dùng Waitress; worker chạy tuần tự từng job. Chạy thật sẽ xử lý cả job đang chờ trong database đã cấu hình.

Muốn xem giao diện mà không gọi API hoặc xử lý hàng đợi thật:

```powershell
.\.venv\Scripts\python.exe scripts\preview_dashboard.py
```

Mở `http://127.0.0.1:8085`. Dữ liệu demo nằm trong `generated/dashboard-preview`; worker tắt. Preview chỉ bind loopback và không dùng database thật.

## Tạo review từ dashboard

1. Chọn **Review phim**, upload MP4. Giới hạn mặc định 500 MB và 300 giây; nên thử clip 15–30 giây trước.
2. Nhập tên phim. Chọn hồ sơ **Avengers: Doomsday** nếu đúng phim hoặc upload JSON riêng theo mẫu trên form. Hồ sơ giúp đối chiếu nhân vật; danh tính vẫn cần kiểm chứng từ clip.
3. Thêm bối cảnh đã kiểm tra và giọng bình luận mong muốn; mục tiêu mặc định 60 giây. Clip ít nội dung có thể cho lời bình ngắn hơn để tránh kéo dài.
4. Chọn **Giữ nguyên clip từ đầu đến cuối** để phát clip một lần ở tốc độ gốc, có lời review và phụ đề; hoặc chọn cảnh theo từng ý. Chọn quyền sử dụng hình ảnh. Nếu chưa xác định, bản dựng dùng đồ họa tự tạo. Khi dùng cảnh phim, điền căn cứ sử dụng.
5. Bấm **Bắt đầu tạo review**. Sau upload, trang kết quả theo dõi các bước. Lỗi API hoặc thoại chưa đạt được hiển thị rõ; bản nháp chưa duyệt không được đưa sang TTS.
6. Khi hoàn tất, xem video, tải MP4, lời bình và phụ đề. Review phim hiện giữ bản nháp, chưa tự đăng YouTube/Facebook. Short/Long giữ lựa chọn đăng YouTube của bot cũ; bỏ chọn sẽ không đăng.

## Railway: một service Web luôn bật

1. Đưa bản code mới lên nhánh GitHub mà Railway theo dõi; các thay đổi chỉ có trên máy local chưa được Railway triển khai. Tạo hoặc dùng service Web từ repo, chọn **Railway Config File** là `/railway.web.toml`. Không đặt Cron Schedule. File này chọn Nixpacks, chạy `python -u web_app.py`, kiểm tra `/healthz` và một replica. `nixpacks.toml` cài FFmpeg và font.
2. Gắn **Volume** vào service Web tại `/data`. Upload, nguồn MP4, cache và kết quả phải được lưu bền vững để còn xem lại sau restart. PostgreSQL chỉ lưu hàng đợi, không lưu MP4. Bot tự chọn `RAILWAY_VOLUME_MOUNT_PATH`; bỏ `BOT_DATA_DIR=.` hoặc đặt chính xác `/data`. Volume chỉ được mount khi service chạy. [Railway Volumes](https://docs.railway.com/volumes)
3. Thêm PostgreSQL và nối `DATABASE_URL`. Có thể dùng chung database với hai service Cron cũ; Volume chứa file review cần gắn vào chính service Web. Giữ **một replica**, vì worker và subprocess đang quản lý trong một tiến trình. [Config as Code](https://docs.railway.com/config-as-code)
4. Đặt Variables dưới đây, cùng các biến liên quan của bot cũ nếu dùng Short/Long:

```dotenv
WEB_ACCESS_TOKEN=<mật-khẩu-dài-riêng-của-bạn>
FLASK_SECRET_KEY=<chuỗi-ngẫu-nhiên-ổn-định>
DATABASE_URL=<reference-tới-PostgreSQL>
GEMINI_API_KEY=<key-phân-tích-video>
MOVIE_VIDEO_PROVIDER=gemini_live
MOVIE_VISUAL_MODE=continuous
MOVIE_MODEL=gemini-3.8-live
MOVIE_VERIFIER_MODEL=gemini-3.8-live
MOVIE_LIVE_TPM=65000
MOVIE_LIVE_MAX_SECONDS=300
MOVIE_LIVE_CHUNK_SECONDS=45
MOVIE_GLOBAL_FPS=1
MOVIE_SCENE_FPS=1
MOVIE_RECHECK_FPS=1
OPENAI_API_KEY=<key-OpenAI>
MOVIE_WRITER_PROVIDER=openai
MOVIE_WRITER_MODEL=gpt-5.4-mini
MOVIE_IDENTITY_PROVIDER=gemini
MOVIE_IDENTITY_MODEL=gemini-3.8-live
MOVIE_OPENAI_MAIN_DAILY_TOKEN_LIMIT=200000
MOVIE_OPENAI_MINI_DAILY_TOKEN_LIMIT=2000000
GOOGLE_TTS_SERVICE_ACCOUNT_JSON_B64=<JSON-service-account-đã-mã-hoá-base64>
GOOGLE_TTS_SERVICE_ACCOUNT_FILE=google_tts_service_account.json
GOOGLE_TTS_VI_VOICE=vi-VN-Chirp3-HD-Aoede
GOOGLE_TTS_VI_SPEAKING_RATE=1.0
MOVIE_MAX_INPUT_MB=500
MOVIE_MAX_INPUT_SECONDS=300
MOVIE_MAX_SCENES=30
MOVIE_MAX_CALLS=100
BOT_DATA_DIR=/data
WEB_JOB_TIMEOUT_SECONDS=7200
YOUTUBE_PRIVACY_STATUS=private
```

Không commit `.env`, key hoặc file credential. Với Railway, dùng `*_JSON_B64` theo hướng dẫn trong README; file JSON nằm ở máy Windows không tự xuất hiện trên server. YouTube OAuth chỉ cần nếu dùng chức năng đăng của bot cũ. Giới hạn token của movie là bộ đếm cục bộ, không thay thế quota toàn tài khoản hoặc bảo đảm mọi lượt gọi miễn phí.

`OPENAI_API_KEY` cũ dùng lại được nếu đã có trên chính service Web. Variables của service Cron khác không tự truyền sang Web; dùng shared/reference variable hoặc thêm giá trị vào Web. `GEMINI_API_KEY` cần thêm riêng cho bước phân tích/nhận diện video. Nguồn MP4 tối đa 300 giây; phân tích tổng thể và nhận diện đều chia thành phiên ngắn tối đa 45 giây, giữ timestamp gốc. Mục tiêu lời review trên dashboard độc lập với độ dài nguồn, vẫn tối đa 180 giây. Chế độ giữ nguyên clip phát toàn bộ nguồn; nếu lời bình kết thúc sớm, phần còn lại tiếp tục với âm thanh gốc đã hạ nhỏ.

Khi nâng cấp từ cấu hình cũ, sửa các biến đang đặt 90 giây / 60 lượt / 20 cảnh; biến Railway đã lưu sẽ ghi đè mặc định mới. Đặt `MOVIE_MAX_INPUT_SECONDS=300`, `MOVIE_LIVE_MAX_SECONDS=300`, `MOVIE_LIVE_CHUNK_SECONDS=45`, `MOVIE_MAX_SCENES=30`, `MOVIE_MAX_CALLS=100`, `WEB_JOB_TIMEOUT_SECONDS=7200`, rồi Deploy. Không cần đổi model nhận diện, API key hay JSON TTS.

Để cập nhật Google TTS từ file JSON mới, chạy PowerShell trên máy của bạn (đổi đường dẫn nếu file có tên khác):

```powershell
[Convert]::ToBase64String([IO.File]::ReadAllBytes('D:\video-bot\google_tts_service_account.json')) | Set-Clipboard
```

Lệnh đưa Base64 vào clipboard, không in credential ra màn hình. Trên Railway, mở **Service Web → Variables**, sửa giá trị `GOOGLE_TTS_SERVICE_ACCOUNT_JSON_B64` và dán clipboard vào đó. Không thêm dấu nháy hay chữ `GOOGLE_TTS_SERVICE_ACCOUNT_JSON_B64=` vào ô giá trị. Giữ `GOOGLE_TTS_SERVICE_ACCOUNT_FILE=google_tts_service_account.json` để bot ghi JSON vào `/data/google_tts_service_account.json`. Base64 mới thay thế credential cũ khi pipeline chạy; không cần xóa file trên Volume bằng tay. Nếu Cron Short/Long cũng dùng key đã thay, cập nhật B64 trên các service đó hoặc biến dùng chung.

Với `FLASK_SECRET_KEY`, có thể tạo chuỗi ngẫu nhiên trên máy local rồi lưu cố định vào Railway:

```powershell
.\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_hex(32))"
```

Variables thay đổi cần được **Deploy** để có hiệu lực. [Railway Variables](https://docs.railway.com/variables)

5. Generate Domain để có HTTPS, mở trang đăng nhập. Railway tự cấp `PORT`; không cần đặt. Service sẽ từ chối khởi động trên Railway nếu thiếu mật khẩu hoặc khóa session ổn định. `/healthz` phục vụ kiểm tra khởi động; không xác nhận API AI/TTS đã hoạt động. [Railway Healthchecks](https://docs.railway.com/deployments/healthchecks)
6. Test MP4 ngắn, theo dõi log và trang job. Xem video đã hoàn tất, restart service rồi kiểm tra file vẫn tải được. Đảm bảo Volume đủ chỗ cho video nguồn và các file trung gian; hiện chưa có nút tự dọn lịch sử/media.

## Mức độ xác minh hiện tại

Upload/hàng đợi, bảo vệ đăng nhập/CSRF, tiếp tục job, phân biệt thoại nháp với thoại đã duyệt, tải MP4 và dựng liên tục đã có kiểm thử. Bộ test hiện tại có 295 test đạt; các test API dùng giả lập. Clip Avengers thật 20,02 giây đã hoàn tất Live → OpenAI → Google TTS → MP4 29,33 giây; bản dựng liên tục dùng lại giọng đọc đã lưu, giữ khung hình cuối thêm 9,308 giây cho hết lời đọc. Chi tiết ở [Gemini Live](gemini-live.md). Lượt test này chạy trên máy local, chưa xác minh một deployment Railway thật.

Tài liệu này hướng dẫn triển khai; không có thao tác deploy Railway tự động trong thay đổi dashboard.
