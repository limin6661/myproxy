from flask import Flask, request, Response, stream_with_context
from curl_cffi import requests
import json
import os

app = Flask(__name__)

TARGET_URL = "https://api.kimi.com/coding/v1/chat/completions"

BASE_HEADERS = {
    "Host": "api.kimi.com",
    "Connection": "keep-alive",
    "Accept": "application/json",
    "Accept-Encoding": "identity",
    "Accept-Language": "*",
    "Sec-Fetch-Mode": "cors",
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

@app.route("/", methods=["GET"])
def index():
    return "Kimi Proxy is Running!"

def pick_max_tokens(client_data: dict) -> int:
    # 兼容不同客户端字段名
    v = (
        client_data.get("max_tokens")
        or client_data.get("max_completion_tokens")
        or client_data.get("max_output_tokens")
        or client_data.get("max_new_tokens")
    )
    if v is None:
        return int(os.environ.get("DEFAULT_MAX_TOKENS", "32000"))
    try:
        return int(v)
    except Exception:
        return int(os.environ.get("DEFAULT_MAX_TOKENS", "32000"))

@app.route("/v1/chat/completions", methods=["POST", "OPTIONS"])
def proxy_handler():
    if request.method == "OPTIONS":
        return Response(
            status=204,
            headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Headers": "*",
                "Access-Control-Allow-Methods": "POST, OPTIONS",
            },
        )

    client_auth = request.headers.get("Authorization")
    if not client_auth:
        return json.dumps({"error": "Missing API Key"}), 401

    try:
        client_data = request.get_json(force=True)
        messages = client_data.get("messages", [])
        client_model_name = (client_data.get("model", "kimi") or "kimi").lower()
    except Exception:
        return "Invalid JSON", 400

    upstream_headers = BASE_HEADERS.copy()
    upstream_headers["Authorization"] = client_auth

    request_payload = {
        "model": "kimi-for-coding",
        "messages": messages,
        "stream": True,
        "stream_options": {"include_usage": True},
        "temperature": client_data.get("temperature", 0.7),
        "max_tokens": pick_max_tokens(client_data),
    }

    # 也可以顺手透传一些常见参数（可选，但更兼容）
    for k in ("top_p", "presence_penalty", "frequency_penalty", "stop", "seed", "n"):
        if k in client_data:
            request_payload[k] = client_data[k]

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
                    timeout=3600,
                )

                if resp.status_code != 200:
                    yield f"data: {json.dumps({'error': resp.text})}\n\n".encode("utf-8")
                    return

                # 用一个较小的 chunk_size，避免某些环境“攒一大坨再吐”
                for chunk in resp.iter_content(chunk_size=1024):
                    if chunk:
                        yield chunk

        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n".encode("utf-8")

    r = Response(stream_with_context(generate_stream()), content_type="text/event-stream; charset=utf-8")
    # SSE 常用头，减少中间层缓存/缓冲导致的“看起来像断流”
    r.headers["Cache-Control"] = "no-cache"
    r.headers["Connection"] = "keep-alive"
    r.headers["X-Accel-Buffering"] = "no"
    r.headers["Access-Control-Allow-Origin"] = "*"
    r.headers["Access-Control-Allow-Headers"] = "*"
    return r

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, threaded=True)
