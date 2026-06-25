import os
import requests
from dotenv import load_dotenv
from youtube_shorts_bot import BotError, Settings, resolve_facebook_page_access_token

def test_facebook_token():
    load_dotenv()
    page_id = os.getenv("FACEBOOK_PAGE_ID")
    page_token = os.getenv("FACEBOOK_PAGE_ACCESS_TOKEN", "")
    user_token = os.getenv("FACEBOOK_USER_ACCESS_TOKEN", "")
    
    if not page_id or not (page_token or user_token):
        print("[Loi] Thieu FACEBOOK_PAGE_ID va FACEBOOK_PAGE_ACCESS_TOKEN hoac FACEBOOK_USER_ACCESS_TOKEN trong file .env")
        return

    try:
        token = resolve_facebook_page_access_token(
            Settings(
                grok_api_key="unused",
                video_api_key="unused",
                facebook_page_id=page_id,
                facebook_page_access_token=page_token,
                facebook_user_access_token=user_token,
            )
        )
    except BotError as exc:
        print(f"[That bai] Khong lay duoc Facebook token: {exc}")
        return
        
    print(f"Dang kiem tra ket noi voi Page ID: {page_id}...")
    
    url = f"https://graph.facebook.com/v25.0/{page_id}"
    params = {
        "fields": "id,name,followers_count,is_published",
        "access_token": token
    }
    
    try:
        response = requests.get(url, params=params)
        data = response.json()
        
        if response.ok:
            print(f"[Thanh Cong] KET NOI THANH CONG!")
            print(f"Ten Trang: {data.get('name')}")
            print(f"ID Trang: {data.get('id')}")
            print("Token cua ban da hop le va san sang de dang bai.")
        else:
            print("[That bai] KET NOI THAT BAI!")
            print(f"Chi tiet loi: {data.get('error', {}).get('message')}")
            print(f"Ma loi: {data.get('error', {}).get('code')}")
            
    except Exception as e:
        print(f"[Loi] Loi khi gui request: {e}")

if __name__ == "__main__":
    test_facebook_token()
