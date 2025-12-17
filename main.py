from flask import Flask, request, Response, stream_with_context
from curl_cffi import requests
import json
import os
import threading
import queue

app = Flask(__name__)

# ================= 配置区 =================
TARGET_URL = "https://api.kimi.com/coding/v1/chat/completions"

BASE_HEADERS = {
    "Connection": "keep-alive",
    "Accept": "application/json",
    "Content-Type": "application/json",
    "User-Agent": "RooCode/3.36.6",
    "Http-Referer": "https://github.com/RooVetGit/Roo-Cline",
    "X-Title": "Roo Code",
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
}

# ================= 通用 CORS =================
@app.after_request
def add_cors_headers(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS, GET"
    return resp

@app.route("/", methods=["GET"])
def index():
    return "Kimi Proxy is Running!"

@app.route("/v1/chat/completions", methods=["POST", "OPTIONS"])
def proxy_handler():
    if request.method == "OPTIONS":
        return Response(status=204)

    client_auth = request.headers.get("Authorization")
    if not client_auth:
        return Response(
            json.dumps({"error": "Missing API Key"}, ensure_ascii=False),
            status=401,
            mimetype="application/json",
        )

    try:
        client_data = request.get_json(force=True, silent=False)
        if not isinstance(client_data, dict):
            raise ValueError("JSON must be an object")
        messages = client_data.get("messages", [])
        client_model_name = str(client_data.get("model", "kimi")).lower()
    except Exception:
        return Response(
            json.dumps({"error": "Invalid JSON"}, ensure_ascii=False),
            status=400,
            mimetype="application/json",
        )

    upstream_headers = BASE_HEADERS.copy()
    upstream_headers["Authorization"] = client_auth

    request_payload = {
        "model": "kimi-for-coding",
        "messages": messages,
        "stream": True,
        "stream_options": {"include_usage": True},
        "max_tokens": int(client_data.get("max_tokens", 200000)),
        "temperature": float(client_data.get("temperature", 0.7)),
    }

    if "think" in client_model_name or "reason" in client_model_name:
        request_payload["reasoning_effort"] = "high"
        request_payload["temperature"] = 1.0

    def sse_data(obj) -> bytes:
        return (f"data: {json.dumps(obj, ensure_ascii=False)}\n\n").encode("utf-8")

    def generate_stream():
        q = queue.Queue(maxsize=2000)
        stop_event = threading.Event()

        def upstream_worker():
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
                        q.put(sse_data({"error": resp.text}))
                        return

                    for chunk in resp.iter_content(chunk_size=8192):
                        if stop_event.is_set():
                            break
                        if chunk:
                            q.put(chunk)

            except Exception as e:
                q.put(sse_data({"error": str(e)}))
            finally:
                try:
                    q.put(None)
                except Exception:
                    pass

        threading.Thread(target=upstream_worker, daemon=True).start()

        yield b": connected\n\n"

        heartbeat_sec = 15
        try:
            while True:
                try:
                    item = q.get(timeout=heartbeat_sec)
                except queue.Empty:
                    yield b": keep-alive\n\n"
                    continue

                if item is None:
                    break

                yield item

        except GeneratorExit:
            stop_event.set()
            raise
        except Exception as e:
            stop_event.set()
            yield sse_data({"error": str(e)})
        finally:
            stop_event.set()

    response_headers = {
        "Content-Type": "text/event-stream; charset=utf-8",
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }

    return Response(stream_with_context(generate_stream()), headers=response_headers)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
