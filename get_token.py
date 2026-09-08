import os
import requests
import streamlit as st
from dotenv import load_dotenv

load_dotenv()

def get_env_variable(var_name):
    val = os.getenv(var_name)
    if not val:
        try:
            val = st.secrets.get(var_name)
        except Exception:
            val = None
    return val

client_id = get_env_variable("KAKAO_CLIENT_ID")
client_secret = get_env_variable("KAKAO_CLIENT_SECRET")
redirect_uri = get_env_variable("KAKAO_REDIRECT_URI") or "http://localhost:8501"

# 인가 코드는 1회성이므로 카카오 로그인 리다이렉트 URL에서 추출한 코드를 여기에 입력하세요.
authorization_code = "여기에_인가코드_입력"

url = "https://kauth.kakao.com/oauth/token"
data = {
    "grant_type": "authorization_code",
    "client_id": client_id,
    "redirect_uri": redirect_uri,
    "code": authorization_code,
}

if client_secret:
    data["client_secret"] = client_secret

response = requests.post(url, data=data)
print(response.json())