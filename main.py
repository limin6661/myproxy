from flask import Flask, request, Response, stream_with_context
from curl_cffi import requests
import json
import os
import time
import threading
import queue

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

# ===== 关键：心跳配置（可用环境变量覆盖）=====
SSE_PING_INTERVAL = float(os.environ.get("SSE_PING_INTERVAL", "15"))  # 秒
# 很多网关会缓冲“小包”，2KB+ 更容易被立刻刷出去
SSE_PING_BYTES = int(os.environ.get("SSE_PING_BYTES", "2048"))
STREAM_QUEUE_MAX = int(os.environ.get("STREAM_QUEUE_MAX", "256"))  # 防止断连后堆内存

@app.route("/", methods=["GET"])
def index():
    return "Kimi Proxy is Running!"

def pick_max_tokens(client_data: dict) -> int:
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

    for k in ("top_p", "presence_penalty", "frequency_penalty", "stop", "seed", "n"):
        if k in client_data:
            request_payload[k] = client_data[k]

    if "think" in client_model_name or "reason" in client_model_name:
        request_payload["reasoning_effort"] = "high"
        request_payload["temperature"] = 1.0

    # ========= 核心改动开始：后台读上游 + 前台心跳 =========
    stop_event = threading.Event()
    q: queue.Queue[bytes] = queue.Queue(maxsize=STREAM_QUEUE_MAX)
    END = b"__END__"

    def put_blocking(data: bytes):
        # 队列满就等，但要能响应 stop_event，避免断连后无限堆内存/卡死
        while not stop_event.is_set():
            try:
                q.put(data, timeout=1)
                return
            except queue.Full:
                continue

    def upstream_reader():
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
                    put_blocking(f"data: {json.dumps({'error': resp.text})}\n\n".encode("utf-8"))
                    return

                for chunk in resp.iter_content(chunk_size=1024):
                    if stop_event.is_set():
                        break
                    if chunk:
                        put_blocking(chunk)

        except Exception as e:
            put_blocking(f"data: {json.dumps({'error': str(e)})}\n\n".encode("utf-8"))
        finally:
            # 尽量通知结束
            try:
                q.put_nowait(END)
            except Exception:
                pass

    t = threading.Thread(target=upstream_reader, daemon=True)
    t.start()

    def make_ping() -> bytes:
        # SSE 注释行，不会影响 data: JSON 的解析
        # 做大一点（>=2KB）更容易穿透网关缓冲
        payload_len = max(0, SSE_PING_BYTES - len(b": ping\n\n"))
        return b": ping " + (b" " * payload_len) + b"\n\n"

    def generate_stream():
        try:
            # 先吐一口，保证“首字节”尽快到客户端，避免被认为没响应
            yield make_ping()

            while True:
                try:
                    item = q.get(timeout=SSE_PING_INTERVAL)
                except queue.Empty:
                    # 上游没数据也要定时心跳，防 120s 断连
                    yield make_ping()
                    continue

                if item == END:
                    break

                yield item

        except GeneratorExit:
            # 客户端断开
            raise
        finally:
            stop_event.set()

    r = Response(stream_with_context(generate_stream()), content_type="text/event-stream; charset=utf-8")
    r.headers["Cache-Control"] = "no-cache, no-transform"
    r.headers["Connection"] = "keep-alive"
    r.headers["X-Accel-Buffering"] = "no"
    r.headers["Access-Control-Allow-Origin"] = "*"
    r.headers["Access-Control-Allow-Headers"] = "*"
    return r
    # ========= 核心改动结束 =========

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, threaded=True)
