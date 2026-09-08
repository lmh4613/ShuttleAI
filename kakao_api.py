import os
import json
import requests
import streamlit as st
from dotenv import load_dotenv

load_dotenv()

def get_env_variable(var_name):
    """환경 변수를 os.getenv에서 먼저 찾고, 없으면 st.secrets에서 가져옵니다."""
    val = os.getenv(var_name)
    if not val:
        try:
            val = st.secrets.get(var_name)
        except Exception:
            val = None
    return val

def refresh_access_token():
    """리프레시 토큰을 이용해 새로운 액세스 토큰을 발급받습니다."""
    refresh_token = get_env_variable("KAKAO_REFRESH_TOKEN")
    client_id = get_env_variable("KAKAO_CLIENT_ID")
    client_secret = get_env_variable("KAKAO_CLIENT_SECRET")
    
    if not refresh_token or not client_id:
        return None, "필수 환경변수(KAKAO_CLIENT_ID 또는 KAKAO_REFRESH_TOKEN)가 누락되었습니다."
        
    url = "https://kauth.kakao.com/oauth/token"
    data = {
        "grant_type": "refresh_token",
        "client_id": client_id,
        "refresh_token": refresh_token
    }
    if client_secret:
        data["client_secret"] = client_secret
        
    try:
        response = requests.post(url, data=data, timeout=5)
        res_data = response.json()
        
        if response.status_code == 200:
            return res_data.get("access_token"), "성공"
        else:
            error_msg = res_data.get("error_description", res_data.get("error", "알 수 없는 오류"))
            return None, f"카카오 인증 실패: {error_msg} (상태코드: {response.status_code})"
    except Exception as e:
        return None, f"통신 예외 발생: {e}"

def send_kakao_memo(message_text):
    """
    카카오톡 '나에게 보내기' API를 통해 메시지를 전송합니다.
    리프레시 토큰을 사용하여 매번 안전하게 새로운 액세스 토큰을 발급받아 전송합니다.
    """
    access_token, error_reason = refresh_access_token()
    
    if not access_token:
        return False, f"토큰 갱신 실패 -> {error_reason}"

    url = "https://kapi.kakao.com/v2/api/talk/memo/default/send"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/x-www-form-urlencoded"
    }
    
    template_object = {
        "object_type": "text",
        "text": message_text,
        "link": {
            "web_url": "https://developers.kakao.com",
            "mobile_web_url": "https://developers.kakao.com"
        },
        "button_title": "셔틀버스 확인하기"
    }
    
    payload = {
        "template_object": json.dumps(template_object, ensure_ascii=False)
    }

    try:
        response = requests.post(url, headers=headers, data=payload, timeout=5)
        res_data = response.json()
        
        if response.status_code == 200 and (res_data.get("result_code") == 0 or "result_code" not in res_data):
            return True, "카카오톡 전송 성공!"
        else:
            return False, f"카카오 API 오류: {res_data}"
            
    except Exception as e:
        return False, f"통신 중 예외 발생: {e}"