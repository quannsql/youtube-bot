# Tạo token YouTube mới và nạp vào Railway

Từ khi bật tính năng playlist, bot cần token mang **2 scope**:

| Scope | Dùng để |
|---|---|
| `https://www.googleapis.com/auth/youtube.upload` | upload video (đã có từ trước) |
| `https://www.googleapis.com/auth/youtube` | tạo playlist + thêm video vào playlist (**mới**) |

Token cũ chỉ có scope thứ nhất. Nó **vẫn upload bình thường** — bot sẽ log cảnh báo và bỏ qua bước playlist — nhưng muốn dùng playlist thì phải tạo lại token theo các bước dưới.

> ⚠️ Toàn bộ quá trình phải chạy **ở máy local có trình duyệt**. Railway không mở được cửa sổ đăng nhập Google.

---

## Bước 0 — Bật quyền trong Google Cloud Console

Làm một lần, bỏ qua nếu đã có sẵn.

1. Vào [Google Cloud Console](https://console.cloud.google.com/) → chọn đúng project chứa OAuth client đang dùng.
2. **APIs & Services → Library** → tìm **YouTube Data API v3** → **Enable**.
3. **APIs & Services → OAuth consent screen**:
   - Nếu app đang ở chế độ **Testing**: đảm bảo email của bạn nằm trong **Test users**.
   - Ở mục **Scopes**, không cần khai báo gì thêm — scope được xin lúc chạy lệnh authorize.
4. **APIs & Services → Credentials** → OAuth 2.0 Client ID phải là loại **Desktop app**. Nếu chưa có thì tạo mới rồi tải file JSON về.

> 📌 App ở chế độ **Testing** thì refresh token **hết hạn sau 7 ngày**. Nếu bot đang chạy dài hạn trên Railway, nên chuyển sang **Production** (Publish app) để token không bị revoke liên tục. Với scope `youtube` thì Google có thể yêu cầu verification — trường hợp kênh cá nhân, cứ để Testing và chấp nhận làm lại token định kỳ cũng được, nhưng phải biết trước.

---

## Bước 1 — Chuẩn bị máy local

```bash
git clone <repo-cua-ban> youtube-bot
cd youtube-bot
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Đặt file OAuth client tải ở Bước 0 vào **thư mục gốc repo**, đổi tên thành `client_secrets.json`:

```bash
cp ~/Downloads/client_secret_xxxxx.json ./client_secrets.json
```

> Bot đọc credentials từ `DATA_DIR`. Ở local, `DATA_DIR` mặc định chính là **thư mục gốc repo** — chỉ khác đi khi bạn đặt `BOT_DATA_DIR`, hoặc trên Railway khi có `RAILWAY_VOLUME_MOUNT_PATH` (lúc đó là đường dẫn Volume). Tên file đổi được qua `YOUTUBE_CLIENT_SECRETS` / `YOUTUBE_TOKEN_FILE`.
>
> ⚠️ Nếu file `.env` ở local của bạn có đặt `BOT_DATA_DIR`, thì `client_secrets.json` và `youtube_token.json` sẽ nằm trong thư mục đó thay vì gốc repo — chỉnh đường dẫn ở các bước sau cho khớp.

---

## Bước 2 — Chạy lệnh authorize

```bash
python youtube_shorts_bot.py --authorize-youtube
```

Trình duyệt sẽ mở ra. Lưu ý ở màn hình đồng ý:

- Đăng nhập bằng **đúng tài khoản Google sở hữu kênh YouTube** cần đăng bài.
- Màn hình giờ sẽ xin **2 quyền** — có thêm dòng đại ý *"Xem, chỉnh sửa và xoá vĩnh viễn video, xếp hạng, bình luận và danh sách phát trên YouTube của bạn"*. Đó chính là scope `youtube` cần cho playlist. **Phải tick/đồng ý cả hai**, bỏ sót một cái là playlist không chạy.
- Nếu hiện cảnh báo *"Google hasn't verified this app"* → **Advanced** → **Go to ... (unsafe)**. Bình thường với app Testing của chính mình.

Xong sẽ thấy dòng log:

```
Saved YouTube authorization token to youtube_token.json
```

**Kiểm tra nhanh xem token có đủ scope chưa:**

```bash
python -c "import json; print(json.load(open('youtube_token.json'))['scopes'])"
```

Kết quả **phải có đủ 2 dòng**:

```
['https://www.googleapis.com/auth/youtube.upload', 'https://www.googleapis.com/auth/youtube']
```

Nếu chỉ thấy 1 scope → bạn đã bỏ tick một quyền, xoá `youtube_token.json` rồi chạy lại Bước 2.

---

## Bước 3 — Chuyển token thành Base64

Railway nhận credentials qua biến môi trường dạng Base64 (`materialize_railway_credentials()` sẽ giải mã ngược ra file lúc khởi động).

Quan trọng: chuỗi Base64 phải **nằm trên đúng 1 dòng**, không xuống dòng. Lệnh dưới đã xử lý sẵn việc đó.

### macOS / Linux

```bash
base64 -w 0 youtube_token.json > token_b64.txt
```

macOS không có `-w`, dùng:

```bash
base64 -i youtube_token.json | tr -d '\n' > token_b64.txt
```

### Windows (PowerShell)

```powershell
[Convert]::ToBase64String([IO.File]::ReadAllBytes("youtube_token.json")) | Set-Content -NoNewline token_b64.txt
```

### Cách chắc ăn nhất (chạy được ở mọi hệ điều hành)

```bash
python -c "import base64,pathlib; pathlib.Path('token_b64.txt').write_text(base64.b64encode(pathlib.Path('youtube_token.json').read_bytes()).decode())"
```

**Tự kiểm tra trước khi dán lên Railway** — giải mã ngược lại xem có ra JSON đúng không:

```bash
python -c "import base64,json,pathlib; d=json.loads(base64.b64decode(pathlib.Path('token_b64.txt').read_text())); print('OK, scopes =', d['scopes'])"
```

Phải in ra đủ 2 scope. Nếu lỗi ở đây thì Railway cũng sẽ lỗi y hệt.

---

## Bước 4 — Nạp vào Railway

1. Mở Railway → chọn service của bot → tab **Variables**.
2. Tìm biến `YOUTUBE_TOKEN_JSON_B64` (đã có sẵn từ lần cài trước) → **Edit**.
3. Xoá sạch giá trị cũ, dán toàn bộ nội dung `token_b64.txt` vào.
   - Dán bằng cách mở file rồi copy toàn bộ, **đừng copy từ output terminal** — dễ dính ký tự xuống dòng hoặc bị cắt.
   - Giá trị đúng là một chuỗi dài liền mạch, chỉ gồm `A–Z a–z 0–9 + / =`, không có khoảng trắng.
4. Thêm 2 biến mới:

   | Biến | Giá trị |
   |---|---|
   | `YOUTUBE_PLAYLIST_ENABLED` | `true` |
   | `YOUTUBE_PLAYLIST_PRIVACY` | `public` |

5. **Deploy** lại service (Railway thường tự redeploy khi đổi biến; nếu không thì bấm tay).

---

## Bước 5 — Chạy thử

Cách an toàn nhất là chạy một video long-form ở chế độ private:

```bash
YOUTUBE_PRIVACY_STATUS=private LONG_FORM_FORCE_NEW=true python youtube_shorts_bot.py --long-form --publish
```

Trên Railway thì đặt tạm `LONG_FORM_FORCE_NEW=true` để bỏ qua cửa chặn 2 ngày, chạy xong nhớ gỡ ra.

**Log cần thấy:**

```
Uploading long.mp4 to YouTube as private…
YouTube upload completed.
Creating the YouTube playlist 'Origins: How Everyday Things Began'…
Created YouTube playlist 'Origins: How Everyday Things Began' (PLxxxxxxxx).
Added video <video_id> to the playlist 'Origins: How Everyday Things Began'.
```

Rồi vào **YouTube Studio → Content → Playlists** kiểm tra.

> Playlist tạo ở bước test sẽ là `public` nhưng chứa 1 video `private`, nên người ngoài thấy playlist rỗng. Xoá tay playlist test đó nếu không muốn giữ — lần chạy thật sẽ tự tạo lại.

---

## Xử lý sự cố

| Log / triệu chứng | Nguyên nhân | Cách xử lý |
|---|---|---|
| `YouTube token thieu scope '...youtube' nen bo qua buoc playlist` | Token vẫn là bản cũ chỉ có `youtube.upload` | Làm lại Bước 2 → 4. Kiểm tra kỹ output ở Bước 3 |
| `YOUTUBE_TOKEN_JSON_B64 không phải Base64 hợp lệ` | Chuỗi bị xuống dòng, thiếu ký tự, hoặc dính khoảng trắng | Tạo lại bằng lệnh Python ở Bước 3, dán từ file |
| `YouTube OAuth token da het han hoac bi revoke` | Refresh token hết hạn (app Testing = 7 ngày), hoặc bạn vừa đổi mật khẩu Google | Làm lại Bước 2 → 4. Cân nhắc Publish app lên Production |
| `YouTube Data API v3 chưa được bật...` | Quên Bước 0.2 | Enable API, chờ vài phút rồi upload lại MP4 đã render |
| Upload xong nhưng **không có log playlist nào** | `YOUTUBE_PLAYLIST_ENABLED` chưa bật, hoặc đây là Short | Shorts không vào playlist — đúng thiết kế. Nếu là long-form thì kiểm tra biến môi trường |
| `it could not be added to the playlist` | Lỗi API lúc thêm video | Video **vẫn live bình thường**, chỉ thiếu playlist. Thêm tay trên Studio, xem chi tiết lỗi trong log |
| Playlist bị tạo trùng tên | Bạn đã đổi tên playlist thủ công trên YouTube | Bot dò playlist theo **tên chính xác**. Đổi tên lại cho khớp `playlist_title` trong code, hoặc sửa code rồi đổi cả hai |

---

## Danh sách 7 playlist bot sẽ tạo

Playlist được tạo **lazy** — chỉ sinh ra khi có video đầu tiên thuộc chủ đề đó, nên sẽ không bao giờ có playlist rỗng.

| Chủ đề (slug) | Tên playlist |
|---|---|
| `civilizations` | Civilizations Explained: Empires, Dynasties & Eras |
| `prehistory` | Before History: How Early Humans Survived |
| `cultures` | Peoples, Cultures & Religions Explained |
| `wars` | Wars & Revolutions: The Turning Points Explained |
| `figures` | People Who Shaped the World |
| `events` | Moments That Changed Everything |
| `origins` | Origins: How Everyday Things Began |

Muốn đổi tên hoặc mô tả: sửa `LONG_FORM_CATEGORIES` trong `youtube_shorts_bot.py`. **Đừng đổi `slug`** — đó là khoá đã được lưu trong `plan.json` của các video cũ.
