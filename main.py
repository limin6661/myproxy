from flask import Flask, request, Response, stream_with_context
from curl_cffi import requests
import json
import time
import os

app = Flask(__name__)

# ================= 配置区 =================
TARGET_URL = "https://api.kimi.com/coding/v1/chat/completions"

BASE_HEADERS = {
    "Host": "api.kimi.com",
    "Connection": "keep-alive",
    "Accept": "application/json",
    "X-Stainless-Retry-Count": "0",
    "X-Stainless-Lang": "js",
    "X-Stainless-Package-Version": "5.12.2",
    "X-Stainless-Os": "Windows",
    "X-Stainless-Arch": "x64",
    "X-Stainless-Runtime": "node",
    "X-Stainless-Runtime-Version": "v22.21.1",
    "Http-Referer": "https://github.com/RooVetGit/Roo-Cline",
    "X-Title": "Roo Code",
    "User-Agent": "RooCode/3.36.6",
    "Content-Type": "application/json",
}

@app.route('/', methods=['GET'])
def index():
    return "Kimi Proxy is Running!"

@app.route('/v1/chat/completions', methods=['POST', 'OPTIONS'])
def proxy_handler():
    if request.method == 'OPTIONS':
        return Response(status=204, headers={'Access-Control-Allow-Origin': '*', 'Access-Control-Allow-Headers': '*'})

    client_auth = request.headers.get('Authorization')
    if not client_auth:
        return json.dumps({"error": "Missing API Key"}), 401

    try:
        client_data = request.json
        messages = client_data.get("messages", [])
        client_model_name = client_data.get("model", "kimi").lower()
    except:
        return "Invalid JSON", 400

    upstream_headers = BASE_HEADERS.copy()
    upstream_headers['Authorization'] = client_auth

    request_payload = {
        "model": "kimi-for-coding",
        "messages": messages,
        "stream": True,
        "max_tokens": client_data.get("max_tokens", 200000),
        "temperature": client_data.get("temperature", 0.7)
    }

    if "think" in client_model_name or "reason" in client_model_name:
        request_payload["reasoning_effort"] = "high"
        request_payload["temperature"] = 1.0

    def generate_stream():
        try:
            with requests.Session() as session:
                resp = session.post(
                    TARGET_URL,
                    headers=upstream_headers,
                    json=request_payload,
                    impersonate="chrome110", 
                    stream=True,
                    timeout=120
                )
                
                if resp.status_code != 200:
                    yield f"data: {json.dumps({'error': resp.text})}\n\n"
                    return

                for chunk in resp.iter_content(chunk_size=None):
                    if chunk:
                        yield chunk

        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return Response(stream_with_context(generate_stream()), content_type='text/event-stream')

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8000))
    app.run(host='0.0.0.0', port=port)
