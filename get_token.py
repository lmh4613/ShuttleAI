import requests

url = "https://kauth.kakao.com/oauth/token"
data = {
    "grant_type": "authorization_code",
    "client_id": "ec1adf1a1782c97f4a2b428e7f279544",  # 사용 중이신 REST API 키
    "redirect_uri": "http://localhost:8501",
    "code": "VyqGQnz9lgb3eaffi7uW3XZTzJwmZGgs_V5wmTIPVE-CYdc-4t4bvAAAAAQKFwtrAAABoGgWx_C2xj-RG-1vuA",
    "client_secret": "DGqsuWaNzK9qom7UosdxWH3rFqQ7G7gS"
}

# 만약 카카오 앱 설정에서 Client Secret을 사용 중이라면 아래 주석을 풀고 입력하세요
# data["client_secret"] = "여기에_클라이언트_시크릿_입력"

response = requests.post(url, data=data)
print(response.json())