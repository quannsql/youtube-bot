import urllib.parse
import requests
import webbrowser

def main():
    print("=== TIKTOK TOKEN GENERATOR ===")
    client_key = "awipecneq8ifzx50"
    client_secret = "zLGHrDJ5TcwIsqG3M1zEairFn5ZjlU3T"
    redirect_uri = "https://www.google.com/"
    
    if not client_key or not client_secret:
        print("Lỗi: Client Key và Client Secret không được để trống!")
        return

    # Generate Auth URL
    auth_url = (
        f"https://www.tiktok.com/v2/auth/authorize/?"
        f"client_key={client_key}&"
        f"response_type=code&"
        f"scope=user.info.basic,video.publish&"
        f"redirect_uri={redirect_uri}&"
        f"state=123"
    )
    
    print("\n" + "="*50)
    print("Bước 1: Trình duyệt sẽ tự động mở đường link sau (nếu không mở, hãy tự copy và dán vào trình duyệt):")
    print(auth_url)
    print("="*50 + "\n")
    
    try:
        webbrowser.open(auth_url)
    except:
        pass
        
    print("Bước 2: Bấm nút Cho Phép (Authorize) trên trình duyệt.")
    print("Trình duyệt sẽ chuyển hướng bạn về trang Google.")
    print("Hãy nhìn lên thanh địa chỉ của trình duyệt, nó sẽ có dạng: https://www.google.com/?code=XXXXXXXX&state=123")
    print("Hãy COPY TOÀN BỘ đường link đó và dán vào đây.")
    
    redirected_url = input("\n👉 Dán đường link bạn vừa copy vào đây: ").strip()
    
    # Parse code from URL
    try:
        parsed_url = urllib.parse.urlparse(redirected_url)
        query_params = urllib.parse.parse_qs(parsed_url.query)
        code = query_params.get("code", [None])[0]
        
        if not code:
            print("❌ Lỗi: Không tìm thấy 'code' trong đường link bạn vừa dán.")
            return
            
        print(f"\nĐã trích xuất mã code thành công. Đang lấy Token...")
    except Exception as e:
        print("❌ Lỗi: Đường link không hợp lệ.")
        return

    # Exchange code for token
    token_url = "https://open.tiktokapis.com/v2/oauth/token/"
    payload = {
        "client_key": client_key,
        "client_secret": client_secret,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri
    }
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Cache-Control": "no-cache"
    }
    
    try:
        response = requests.post(token_url, headers=headers, data=payload)
        data = response.json()
        
        if response.ok and "access_token" in data:
            print("\n🎉 THÀNH CÔNG! Đây là Token của bạn:")
            print("-" * 50)
            print(data["access_token"])
            print("-" * 50)
            print("👉 Hãy copy đoạn token này và dán vào biến TIKTOK_ACCESS_TOKEN trong file .env")
        else:
            print("\n❌ Lấy Token thất bại!")
            print(f"Chi tiết lỗi: {data}")
    except Exception as e:
        print(f"\n❌ Lỗi khi gọi API: {e}")

if __name__ == "__main__":
    main()
