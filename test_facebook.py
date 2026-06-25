import os
import requests
from dotenv import load_dotenv

def test_facebook_token():
    load_dotenv()
    page_id = os.getenv("FACEBOOK_PAGE_ID")
    token = os.getenv("FACEBOOK_PAGE_ACCESS_TOKEN")
    
    if not page_id or not token:
        print("[Loi] Thieu FACEBOOK_PAGE_ID hoac FACEBOOK_PAGE_ACCESS_TOKEN trong file .env")
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
