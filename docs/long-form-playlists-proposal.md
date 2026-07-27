# Tự động gom video Long-form vào playlist theo chủ đề

> **Đã implement.** Tài liệu này giữ lại phần thiết kế và lý do; xem `docs/youtube-token-setup.md`
> cho hướng dẫn tạo token và nạp lên Railway.
>
> Một điểm đã đổi so với bản thiết kế ban đầu: `Credentials.from_authorized_user_file`
> được gọi **không kèm danh sách scope**. Truyền scope vào sẽ khiến `credentials.scopes`
> lặp lại đúng danh sách vừa truyền chứ không phải scope token thật sự mang, làm hỏng
> phép kiểm tra `playlist_ready`. Xem mục 3.6.

## Phạm vi

- ✅ **Chỉ áp dụng cho Long-form** (`long.mp4`).
- ❌ **Shorts không có playlist** — kể cả Short tự động lẫn Short thủ công.
- Playlist được **tạo tự động khi cần** (lazy): lần đầu có video thuộc chủ đề nào thì playlist của chủ đề đó mới sinh ra. Không bao giờ tồn tại playlist rỗng.

---

## 1. Ý tưởng cốt lõi

Bot **đã** chọn sẵn chủ đề *trước khi* sinh nội dung — `choose_long_form_explainer_category()` (`youtube_shorts_bot.py:1675`) bốc một trong 7 nhóm của `LONG_FORM_EXPLAINER_CATEGORIES` (`:1656`) rồi nhét vào prompt ở `:2455`.

Nghĩa là **không cần LLM phân loại lại, không thể phân loại sai**. Chủ đề là dữ liệu đầu vào đã biết chắc, chỉ đang bị vứt đi sau khi dùng xong.

Việc cần làm gọn trong 3 bước:

1. **Giữ lại** chủ đề đã chọn → gắn vào `ShortPlan`, đi cùng `plan.json`.
2. **Map** mỗi chủ đề → một playlist (tên + mô tả cố định trong code).
3. **Gọi thêm 2 API** sau khi `videos().insert` xong.

Shorts đi qua nhánh code khác (`plan_short`, `plan_short_from_idea`) nên không bao giờ được gán chủ đề → trường rỗng → bước 3 tự động bỏ qua. **Không cần cờ riêng để loại Shorts.**

---

## 2. Bộ playlist đề xuất: 7 playlist, 1:1 với 7 chủ đề

Giữ ánh xạ 1–1 thay vì gộp, vì: (a) chủ đề được *gán* chứ không *đoán* nên ranh giới sạch, (b) mỗi playlist có bộ từ khoá tìm kiếm riêng, (c) gộp thì vẫn phải viết bảng map, không tiết kiệm được gì.

Nhịp hiện tại là 1 video / 2 ngày (`long_form_interval_days = 2`, `:189`) ≈ **180 video/năm ≈ 26 video/playlist/năm**. Đủ dày để 7 playlist không bị loãng.

| # | Chủ đề (slug) | Tên playlist | Mô tả |
|---|---|---|---|
| 1 | `civilizations` | Civilizations Explained: Empires, Dynasties & Eras | How great civilizations rose, ruled, and fell — empires, dynasties, and the eras that shaped the world we live in. |
| 2 | `prehistory` | Before History: How Early Humans Survived | Life before writing: how prehistoric people found food, made fire and tools, built shelter, and spread across the planet. |
| 3 | `cultures` | Peoples, Cultures & Religions Explained | The origins, journeys, beliefs, and traditions of the world's peoples and faiths — explained factually and respectfully. |
| 4 | `wars` | Wars & Revolutions: The Turning Points Explained | The battles, revolutions, and turning points that redrew the map — what happened, why it happened, and what changed. |
| 5 | `figures` | People Who Shaped the World | Notable figures past and present: who they were, what they actually did, and why they still matter today. |
| 6 | `events` | Moments That Changed Everything | Major events, past and present — a clear explanation of what happened and why it mattered. |
| 7 | `origins` | Origins: How Everyday Things Began | Where the familiar world came from — money, food, cities, farming, trade, writing, holidays, jobs, and famous inventions. |

Tên đều **≤ 50 ký tự** để không bị cắt trên sidebar và trang playlist của YouTube.

---

## 3. Các thay đổi cụ thể

### 3.1 Cấu trúc lại hằng chủ đề (`~:1656`)

Hiện `LONG_FORM_EXPLAINER_CATEGORIES` là tuple câu tiếng Anh dài. Dùng chính câu đó làm khoá map là rất dễ vỡ — sửa một dấu phẩy trong prompt là mất playlist. Thay bằng slug ổn định:

```python
@dataclass(frozen=True)
class LongFormCategory:
    slug: str                  # khoá ổn định, lưu vào plan.json
    prompt: str                # câu đưa vào prompt LLM (giữ NGUYÊN VĂN bản cũ)
    playlist_title: str
    playlist_description: str


LONG_FORM_CATEGORIES: tuple[LongFormCategory, ...] = (
    LongFormCategory(
        slug="civilizations",
        prompt="Human history and civilizations, ancient to modern: how a people, empire, kingdom, dynasty, or era lived, rose, or fell.",
        playlist_title="Civilizations Explained: Empires, Dynasties & Eras",
        playlist_description="How great civilizations rose, ruled, and fell — empires, dynasties, and the eras that shaped the world we live in.",
    ),
    LongFormCategory(
        slug="prehistory",
        prompt="Prehistory and early humans: how prehistoric people and early humans survived — hunting, gathering, fire, tools, shelter, clothing, and migration.",
        playlist_title="Before History: How Early Humans Survived",
        playlist_description="Life before writing: how prehistoric people found food, made fire and tools, built shelter, and spread across the planet.",
    ),
    LongFormCategory(
        slug="cultures",
        prompt="Peoples, cultures, and religions: the origin, journey, beliefs, traditions, and defining moments of a people or faith, explained factually and respectfully.",
        playlist_title="Peoples, Cultures & Religions Explained",
        playlist_description="The origins, journeys, beliefs, and traditions of the world's peoples and faiths — explained factually and respectfully.",
    ),
    LongFormCategory(
        slug="wars",
        prompt="Wars, battles, revolutions, and turning points, past or present: what happened, why it happened, and what changed.",
        playlist_title="Wars & Revolutions: The Turning Points Explained",
        playlist_description="The battles, revolutions, and turning points that redrew the map — what happened, why it happened, and what changed.",
    ),
    LongFormCategory(
        slug="figures",
        prompt="Notable figures, past or present: who they were, what they did, and why they still matter.",
        playlist_title="People Who Shaped the World",
        playlist_description="Notable figures past and present: who they were, what they actually did, and why they still matter today.",
    ),
    LongFormCategory(
        slug="events",
        prompt="Major events, past or present: a clear explanation of what happened and why it mattered.",
        playlist_title="Moments That Changed Everything",
        playlist_description="Major events, past and present — a clear explanation of what happened and why it mattered.",
    ),
    LongFormCategory(
        slug="origins",
        prompt="Origins of the everyday world: how familiar things — money, food, cities, farming, trade, writing, holidays, everyday customs, jobs, and famous inventions — actually began and became part of daily life.",
        playlist_title="Origins: How Everyday Things Began",
        playlist_description="Where the familiar world came from — money, food, cities, farming, trade, writing, holidays, jobs, and famous inventions.",
    ),
)

LONG_FORM_CATEGORY_BY_SLUG = {c.slug: c for c in LONG_FORM_CATEGORIES}

# Giữ hằng cũ để code đang đọc nó (vd :2446) không phải sửa.
LONG_FORM_EXPLAINER_CATEGORIES = tuple(c.prompt for c in LONG_FORM_CATEGORIES)
```

> ⚠️ `prompt` phải **copy y nguyên** 7 câu hiện có. Đổi chữ trong đó là đổi hành vi sinh nội dung — không phải mục tiêu của thay đổi này.

### 3.2 `choose_long_form_explainer_category` (`:1675`) trả về object

```python
def choose_long_form_explainer_category(excluded: set[str] | None = None) -> LongFormCategory:
    """Pick an explainer topic area, avoiding the slugs already tried this run."""
    excluded = set(excluded or ())
    candidates = [c for c in LONG_FORM_CATEGORIES if c.slug not in excluded]
    return random.choice(candidates or list(LONG_FORM_CATEGORIES))
```

### 3.3 `ShortPlan` (`:353`) — thêm 1 trường

```python
    # Long-form only: slug chủ đề đã được gán trước khi sinh nội dung, dùng để
    # gom video vào playlist. Shorts luôn để rỗng -> không vào playlist nào.
    topic_category: str = ""
```

Trong `from_dict` (`:415`, phần `return cls(...)`) thêm:

```python
            topic_category=str(value.get("topic_category") or "").strip()[:40],
```

`to_dict()` dùng `asdict` nên tự động có. **Điểm quan trọng:** nhờ vậy trường này sống sót qua `plan.json` (`:4447`, `:4462`, `:4502`) — job long-form là resumable (prepare/finalize), nếu không đọc lại trong `from_dict` thì resume xong sẽ mất chủ đề.

### 3.4 `plan_long_form` (`:2430`) — gán slug bằng tay

Đổi kiểu tham số và cách dùng:

```python
def plan_long_form(
    ...
    topic_category: LongFormCategory | None = None,
    ...
) -> ShortPlan:
    ...
    category = topic_category or random.choice(list(LONG_FORM_CATEGORIES))
```

Trong prompt (`:2455`) đổi `{topic_category}` → `{category.prompt}`.

Sau `plan = ShortPlan.from_dict(reviewed["plan"])` (`:2522`) thêm **ngay**:

```python
    plan.topic_category = category.slug
```

> Phải gán tay ở đây. Không thể trông vào LLM trả về trường này: `LONG_FORM_PLAN_SCHEMA` không có nó, và bản plan cuối đi qua vòng review (`draft.to_dict()` → LLM → `reviewed["plan"]`) nên trường thêm vào draft sẽ bị rụng.

### 3.5 `choose_novel_long_form_plan` (`:2552`)

```python
        category = choose_long_form_explainer_category(attempted_categories)
        attempted_categories.add(category.slug)          # set giờ chứa slug
        plan = plan_long_form(
            llm, archive, theme, duration, min_scenes, max_scenes,
            category, rejected, covered_subjects,
        )
```

### 3.6 OAuth scope — **đây là chỗ dễ vỡ nhất**

Scope `youtube.upload` **không** cho phép ghi playlist. Hiện scope bị khai báo lặp ở 2 nơi (`:4049` và `:4076`); nếu chỉ sửa một chỗ thì `authorize_youtube` và `upload_to_youtube` lệch nhau và lỗi rất khó lần. Gom thành hằng chung:

```python
YOUTUBE_SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",
]
```

Thay cả `:4049` lẫn `:4076` bằng `YOUTUBE_SCOPES`.

**Token cũ sẽ không đủ quyền**, và chỗ này có một cái bẫy đã được kiểm chứng bằng thực nghiệm:

```python
# Token trong file chỉ được cấp youtube.upload
Credentials.from_authorized_user_file(p, YOUTUBE_SCOPES).scopes
# -> ['...youtube.upload', '...youtube']   ← lặp lại scope VỪA TRUYỀN VÀO
Credentials.from_authorized_user_file(p).scopes
# -> ['...youtube.upload']                 ← scope THẬT trong token
```

Nghĩa là nếu truyền `YOUTUBE_SCOPES` vào lúc load thì `credentials.scopes` luôn "đủ", phép kiểm tra thành vô dụng, và lỗi sẽ nổ muộn ở `playlists.insert` dưới dạng `403 insufficientPermissions` — đúng cái ta muốn tránh. Vì vậy load **không kèm scope**:

```python
        credentials = Credentials.from_authorized_user_file(str(settings.youtube_token))
```

rồi kiểm tra scope thật sau khi có `credentials`:

```python
    playlist_ready = YOUTUBE_PLAYLIST_SCOPE in set(credentials.scopes or ())
    if settings.youtube_playlist_enabled and not playlist_ready:
        LOG.warning(...)   # bỏ qua playlist, video vẫn upload bình thường
```

Có test `test_saved_token_reports_the_scopes_it_was_actually_granted` khoá lại hành vi này để tránh ai đó vô tình thêm tham số scope trở lại.

### 3.7 Hai hàm mới

Đặt ngay sau `upload_to_youtube`:

```python
def ensure_playlist(service, category: LongFormCategory, settings: Settings) -> str | None:
    """Trả playlistId của chủ đề, tạo mới nếu channel chưa có. None nếu thất bại."""
    from googleapiclient.errors import HttpError

    try:
        page_token = None
        while True:
            response = service.playlists().list(
                part="snippet", mine=True, maxResults=50, pageToken=page_token,
            ).execute()
            for item in response.get("items", []):
                if item["snippet"]["title"].strip() == category.playlist_title:
                    return str(item["id"])
            page_token = response.get("nextPageToken")
            if not page_token:
                break

        created = service.playlists().insert(
            part="snippet,status",
            body={
                "snippet": {
                    "title": category.playlist_title,
                    "description": category.playlist_description,
                    "defaultLanguage": "en",
                },
                "status": {"privacyStatus": settings.youtube_playlist_privacy},
            },
        ).execute()
    except HttpError as exc:
        LOG.error("Could not resolve the YouTube playlist for %s: %s", category.slug, exc)
        return None

    LOG.info("Created YouTube playlist %r (%s).", category.playlist_title, created["id"])
    return str(created["id"])


def add_video_to_playlist(service, video_id: str, category: LongFormCategory, settings: Settings) -> None:
    """Gom video vào playlist chủ đề.

    Không bao giờ raise: tới thời điểm này video đã live trên YouTube rồi, nên
    lỗi playlist chỉ được log lại — giống cách thumbnail được xử lý ở trên.
    """
    from googleapiclient.errors import HttpError

    playlist_id = ensure_playlist(service, category, settings)
    if not playlist_id:
        return
    try:
        service.playlistItems().insert(
            part="snippet",
            body={"snippet": {
                "playlistId": playlist_id,
                "resourceId": {"kind": "youtube#video", "videoId": video_id},
            }},
        ).execute()
        LOG.info("Added video %s to playlist %r.", video_id, category.playlist_title)
    except HttpError as exc:
        LOG.error(
            "Video %s uploaded, but adding it to playlist %r failed: %s",
            video_id, category.playlist_title, exc,
        )
```

> Muốn video mới nhất nằm **đầu** playlist thì thêm `"position": 0` vào `snippet`. Mặc định video được thêm vào cuối.

### 3.8 Nối vào `upload_to_youtube` (`:4133`)

Ngay trước `return video_id`:

```python
    category = LONG_FORM_CATEGORY_BY_SLUG.get(plan.topic_category)
    if settings.youtube_playlist_enabled and playlist_ready and category:
        add_video_to_playlist(service, video_id, category, settings)
    return video_id
```

**Không cần đổi chữ ký hàm và không cần sửa 4 chỗ gọi.** Cả 4 đường upload (`:4566` long-form, `:4705` Short thủ công, `:4939` `--upload-file`, `:5034` Short tự động) dùng chung hàm này, và điều kiện `category` chỉ đúng khi `plan.topic_category` khớp một slug hợp lệ:

| Đường upload | `plan.topic_category` | Vào playlist? |
|---|---|---|
| `publish_long_form_video` (`:4566`) | slug được gán ở 3.4 | ✅ |
| Short thủ công (`:4705`) | `""` | ❌ |
| Short tự động (`:5034`) | `""` | ❌ |
| `--upload-file` (`:4939`) | đọc từ `plan.json` cạnh file | ✅ nếu là `long.mp4`, ❌ nếu là Short |

Đúng yêu cầu "chỉ Long, không Short", và tự nhiên đúng chứ không phải nhờ điều kiện gán thêm.

### 3.9 `Settings` (`:215`) + `.env.example`

```python
    youtube_playlist_enabled: bool = False
    youtube_playlist_privacy: str = "public"
```

Trong `from_env` (`~:319`):

```python
            youtube_playlist_enabled=env_bool("YOUTUBE_PLAYLIST_ENABLED", False),
            youtube_playlist_privacy=os.getenv("YOUTUBE_PLAYLIST_PRIVACY", "public"),
```

Mặc định **`false`** là cố ý: deploy code mới lên Railway trong khi token cũ chưa được thay sẽ không sinh warning/lỗi nào. Bật lên sau khi đã đổi token xong (xem mục 5).

---

## 4. Quota

| Lệnh | Đơn vị | Số lần / 1 video long-form |
|---|---|---|
| `videos.insert` | **1600** | 1 |
| `playlists.list` | 1 | 1 |
| `playlists.insert` | 50 | chỉ lần đầu mỗi chủ đề (tối đa 7 lần trọn đời) |
| `playlistItems.insert` | 50 | 1 |

Phát sinh thêm **51 đơn vị/video** (101 ở lần đầu của mỗi chủ đề) trên nền 1600 sẵn có → **+3%**. Quota mặc định 10.000/ngày, nhịp 1 video/2 ngày. Hoàn toàn không phải lo.

---

## 5. Thứ tự triển khai

Scope đổi ⇒ **token cũ mất hiệu lực**, nên thứ tự quan trọng:

1. Merge code (`YOUTUBE_PLAYLIST_ENABLED` vẫn `false` → hành vi y hệt hiện tại).
2. Ở máy local: `python youtube_shorts_bot.py --authorize-youtube` → duyệt lại quyền, giờ màn hình consent sẽ xin thêm quyền quản lý playlist.
3. Base64 hoá `youtube_token.json` mới → cập nhật `YOUTUBE_TOKEN_JSON_B64` trên Railway.
4. Đặt `YOUTUBE_PLAYLIST_ENABLED=true` → redeploy.
5. Chạy thử 1 video: `--long-form --publish --privacy-status private --long-form-force-new`, rồi kiểm tra playlist trên YouTube Studio.

Bước 5 sẽ tạo playlist ở chế độ `public` (theo `YOUTUBE_PLAYLIST_PRIVACY`) nhưng chứa 1 video `private` — người ngoài thấy playlist rỗng. Xoá tay playlist test đó nếu không muốn giữ; lần chạy thật sẽ tự tạo lại.

---

## 6. Lưu ý & giới hạn

- **Đừng đổi tên playlist trên YouTube.** `ensure_playlist` dò theo *tên chính xác*; đổi tên thủ công sẽ khiến lần sau tạo playlist trùng. Muốn đổi tên thì sửa `playlist_title` trong code **và** đổi trên YouTube cho khớp. (Nếu thấy phiền: cache `slug → playlistId` vào `archive.set_kv`/`get_kv` (`:583`) — nhưng phải luồn thêm `archive` qua `upload_to_youtube` và `publish_long_form_video`, nên tôi bỏ qua ở phiên bản đầu.)
- `playlistItems.insert` **không chống trùng** — YouTube cho phép cùng một video nằm 2 lần trong playlist. Rủi ro thấp vì mỗi lần upload sinh videoId mới, nhưng đừng gọi lại hàm này cho một videoId đã thêm.
- Giới hạn 5.000 video/playlist — với nhịp hiện tại là ~190 năm, bỏ qua.
- Playlist chỉ được đặt privacy lúc **tạo**. Đổi `YOUTUBE_PLAYLIST_PRIVACY` sau đó không sửa các playlist đã tồn tại.
- Kiểm thử không cần gọi mạng: `plan.topic_category` là thuần dữ liệu, có thể test riêng phần round-trip `to_dict`/`from_dict` và phần map slug → `LongFormCategory` trong `tests/`.

---

## 7. Tóm tắt khối lượng

| File | Sửa gì | Ước tính |
|---|---|---|
| `youtube_shorts_bot.py` | hằng `LONG_FORM_CATEGORIES` (7 mục) | ~55 dòng |
| | `ShortPlan` + `from_dict` | 2 dòng |
| | `plan_long_form`, `choose_novel_long_form_plan`, `choose_long_form_explainer_category` | ~8 dòng |
| | `YOUTUBE_SCOPES` + kiểm tra scope | ~10 dòng |
| | `ensure_playlist`, `add_video_to_playlist` | ~50 dòng |
| | nối vào `upload_to_youtube` | 4 dòng |
| | `Settings` + `from_env` | 4 dòng |
| `.env.example` | 2 biến mới | 2 dòng |

Tổng ~135 dòng, trong đó 55 là dữ liệu tên playlist. Không đụng tới pipeline render, không đụng tới Shorts, không đụng tới social publishing.
