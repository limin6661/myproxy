# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "curl_cffi",
#     "flask",
#     "gevent",
# ]
# ///

from flask import Flask, request, Response, stream_with_context, jsonify
from curl_cffi import requests
import json
import time
import os

# ====== 新增：心跳保活所需 ======
import threading
import queue

app = Flask(__name__)

# ================= 基础配置 =================
LOG_FILE = "traffic.log"
PORT = int(os.getenv("PORT", "8000"))

# ====== 新增：心跳间隔（秒），建议 5~15，越小越稳但更“吵” ======
HEARTBEAT_INTERVAL = int(os.getenv("HEARTBEAT_INTERVAL", "10"))

# 统一：只做“Authorization 透传”，不在服务器端保存任何 Key
# 你在 Cherry Studio / ChatBox / Roo Code 里，仍然按 OpenAI 兼容模式，把 Key 放到 Authorization 里即可。

# ============== Provider 配置（可继续扩展）==============
PROVIDERS = {
    "kimi": {
        "name": "Kimi For Coding",
        # 官方文档给的 Entrypoint 是 https://api.kimi.com/coding/v1
        # OpenAI 兼容模式下 chat completions 路径就是 /chat/completions
        "url": "https://api.kimi.com/coding/v1/chat/completions",
        "default_model": "kimi-for-coding",
        "base_headers": {
            "Host": "api.kimi.com",
            "Connection": "keep-alive",
            "Accept": "application/json",
            "Accept-Encoding": "identity",
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
        },
    },
    "bigmodel": {
        "name": "Zhipu BigModel (Coding Plan)",
        # Coding 套餐的 base 是 /api/coding/paas/v4 （不是通用 /api/paas/v4）
        # chat completions 路径是 /chat/completions
        "url": "https://open.bigmodel.cn/api/coding/paas/v4/chat/completions",
        "default_model": "glm-4.7",
        "base_headers": {
            "Host": "open.bigmodel.cn",
            "Connection": "keep-alive",
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36",
        },
    },
}

# ================= 工具函数 =================
def write_log(tag, content):
    timestamp = time.strftime("%H:%M:%S", time.localtime())
    line = f"[{timestamp}] [{tag}] {content}"
    print(line[:300])
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except:
        pass


def cors_headers():
    return {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Headers": "*",
        "Access-Control-Allow-Methods": "POST, OPTIONS",
    }


def detect_provider(model_name: str) -> str:
    """
    你想要的“多层识别”核心就在这里：
    - 包含 glm / zhipu / bigmodel / zai 之类关键词 => 走智谱 BigModel
    - 包含 kimi / moonshot 之类关键词 => 走 Kimi
    - 都不命中 => 报错，让你显式指定
    """
    m = (model_name or "").strip().lower()

    # 第一层：明确识别 GLM
    if any(k in m for k in ["glm", "zhipu", "bigmodel", "z.ai", "zai"]):
        return "bigmodel"

    # 第二层：识别 Kimi
    if any(k in m for k in ["kimi", "moonshot"]):
        return "kimi"

    return ""


def choose_upstream_model(provider_key: str, client_model: str) -> str:
    """
    - 如果客户端传了明确型号，比如 glm-4.6 / glm-4.7 / kimi-for-coding，就尽量原样使用
    - 如果只是传了 glm / kimi 这种路由关键词，就落到 default_model
    """
    m = (client_model or "").strip()
    if not m:
        return PROVIDERS[provider_key]["default_model"]

    ml = m.lower()

    if provider_key == "bigmodel":
        # 用户只写 glm，也给他兜底到 glm-4.7
        if ml in ["glm", "zhipu", "bigmodel", "zai", "z.ai"]:
            return PROVIDERS[provider_key]["default_model"]
        return m

    if provider_key == "kimi":
        # 用户只写 kimi，也兜底到 kimi-for-coding
        if ml in ["kimi", "moonshot", "kimi-for-coding"]:
            return "kimi-for-coding"
        # 如果用户写了 kimi-xxx，也尊重
        return m

    return m


def build_payload(provider_key: str, client_data: dict) -> dict:
    """
    这里做“尽量 OpenAI 兼容”的转发：
    - 保留 messages / stream / temperature / max_tokens 等常用字段
    - 按不同厂家，补齐或改写少量关键字段
    """
    messages = client_data.get("messages", [])
    client_model_name = (client_data.get("model", "") or "").lower()

    stream = bool(client_data.get("stream", True))

    payload = {
        "model": "",  # 下面填
        "messages": messages,
        "stream": stream,
    }

    # 常见参数：能带就带
    for k in [
        "temperature",
        "max_tokens",
        "top_p",
        "stop",
        "presence_penalty",
        "frequency_penalty",
        "seed",
        "tools",
        "tool_choice",
        "response_format",
    ]:
        if k in client_data:
            payload[k] = client_data[k]

    # 给 max_tokens 一个更友好的默认值
    if "max_tokens" not in payload:
        payload["max_tokens"] = 1024 * 32

    # 是否“推理模式”的开关：沿用你原来的规则
    want_think = ("think" in client_model_name) or ("reason" in client_model_name)

    if provider_key == "kimi":
        payload["model"] = choose_upstream_model("kimi", client_data.get("model", ""))
        if want_think:
            write_log("MODE", "kimi => 已激活 reasoning_effort=high")
            payload["reasoning_effort"] = "high"
            # 推理更稳一点，稍微放开随机性也行，你也可以删掉这一行
            payload["temperature"] = float(client_data.get("temperature", 1.0))
        else:
            write_log("MODE", "kimi => 普通模式")
            # 普通模式时，尽量不要夹带 reasoning_effort
            payload.pop("reasoning_effort", None)

        return payload

    if provider_key == "bigmodel":
        payload["model"] = choose_upstream_model("bigmodel", client_data.get("model", ""))
        if want_think:
            write_log("MODE", "bigmodel => thinking.type=enabled")
            payload["thinking"] = {"type": "enabled", "clear_thinking": True}
            # 可选：你也可以在推理时固定更确定的生成
            # payload["do_sample"] = False
        else:
            write_log("MODE", "bigmodel => thinking.type=disabled")
            payload["thinking"] = {"type": "disabled", "clear_thinking": True}
        return payload

    return payload


# ================= 路由 =================
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True}), 200


@app.route("/v1/chat/completions", methods=["POST", "OPTIONS"])
def proxy_handler():
    # 1) CORS 预检
    if request.method == "OPTIONS":
        return Response(status=204, headers=cors_headers())

    # 2) 取 Authorization，原样透传
    client_auth = request.headers.get("Authorization")
    if not client_auth:
        return Response(
            json.dumps({"error": "Missing API Key in Authorization header"}, ensure_ascii=False),
            status=401,
            content_type="application/json",
            headers=cors_headers(),
        )

    # 3) 解析 JSON
    try:
        client_data = request.get_json(force=True)
        messages = client_data.get("messages", [])
        model_name = client_data.get("model", "")

        # 记录第一条 user 消息
        for msg in messages:
            if isinstance(msg, dict) and msg.get("role") == "user":
                write_log("USER_ASK", msg.get("content", "")[:5000])
                break

    except Exception as e:
        write_log("BAD_JSON", str(e))
        return Response(
            json.dumps({"error": "Invalid JSON"}, ensure_ascii=False),
            status=400,
            content_type="application/json",
            headers=cors_headers(),
        )

    # 4) 识别 Provider 并分发
    provider_key = detect_provider(model_name)
    if not provider_key:
        return Response(
            json.dumps(
                {
                    "error": "Unsupported model/provider. Put 'kimi' or 'glm' keyword in model name to route.",
                    "example": {"model": "kimi-for-coding  或  glm-4.7"},
                },
                ensure_ascii=False,
            ),
            status=400,
            content_type="application/json",
            headers=cors_headers(),
        )

    provider = PROVIDERS[provider_key]
    upstream_url = provider["url"]

    # 5) 组装 headers + payload
    upstream_headers = provider["base_headers"].copy()
    upstream_headers["Authorization"] = client_auth

    payload = build_payload(provider_key, client_data)

    # 6) 发起请求并透传流
    client_wants_stream = bool(payload.get("stream", True))

    # ====== 新增：SSE 心跳（注释行，标准 SSE 客户端会忽略，但能让链路“有字节流动”） ======
    def sse_heartbeat_bytes():
        # 发一个“合法的 data: …”空增量，客户端更容易认可为“有响应”
        payload = {"choices": [{"delta": {}}]}
        s = f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
        # 适当填充，避免中间层攒小包
        if len(s) < 2048:
            s = s + (" " * (2048 - len(s)))
        return s.encode("utf-8")

    def generate_stream():
        """
        这里保持你原来的结构，只做“必要修改”：
        - 用后台线程读取上游数据放入队列
        - 主生成器每隔 HEARTBEAT_INTERVAL 秒，若队列没数据，就吐一个 SSE 心跳
        这样即使上游 400~900 秒不吐 token，连接也不会因为“无输出”被掐断
        """
        # 非流式：维持你原来的行为（不引入心跳，避免破坏 JSON）
        if not client_wants_stream:
            try:
                with requests.Session() as session:
                    resp = session.post(
                        upstream_url,
                        headers=upstream_headers,
                        json=payload,
                        impersonate="chrome110",
                        stream=True,
                        timeout=600,
                    )

                    if resp.status_code != 200:
                        err_text = resp.text
                        write_log("UPSTREAM_ERR", f"{provider['name']} {resp.status_code}: {err_text[:2000]}")
                        yield err_text.encode("utf-8")
                        return

                    write_log("CONN", f"UPSTREAM OK => {provider['name']}")
                    for chunk in resp.iter_content(chunk_size=None):
                        if chunk:
                            yield chunk
            except Exception as e:
                write_log("EXC", str(e))
                yield json.dumps({"error": str(e)}, ensure_ascii=False).encode("utf-8")
            return

        # ====== 流式：启用“队列 + 心跳” ======
        q: "queue.Queue[object]" = queue.Queue()
        done = threading.Event()

        def reader_thread():
            try:
                with requests.Session() as session:
                    resp = session.post(
                        upstream_url,
                        headers=upstream_headers,
                        json=payload,
                        impersonate="chrome110",
                        stream=True,
                        # 你原来的超时保留
                        timeout=600,
                    )

                    if resp.status_code != 200:
                        err_text = resp.text
                        write_log("UPSTREAM_ERR", f"{provider['name']} {resp.status_code}: {err_text[:2000]}")
                        q.put(("err", err_text))
                        return

                    write_log("CONN", f"UPSTREAM OK => {provider['name']}")

                    for chunk in resp.iter_content(chunk_size=None):
                        if chunk:
                            q.put(("data", chunk))

            except Exception as e:
                write_log("EXC", str(e))
                q.put(("exc", str(e)))
            finally:
                done.set()
                q.put(("eof", None))

        threading.Thread(target=reader_thread, daemon=True).start()

        saw_any_data = False
        
        yield sse_heartbeat_bytes()
        while True:
            try:
                item = q.get(timeout=HEARTBEAT_INTERVAL)
            except queue.Empty:
                # 上游在“深度思考”阶段一字不出时，靠这个心跳保活
                yield sse_heartbeat_bytes()
                continue

            kind, val = item

            if kind == "data":
                saw_any_data = True
                yield val
                continue

            if kind == "err":
                # 用 SSE 形式把错误推回去，并结束
                msg = f"data: {json.dumps({'error': val}, ensure_ascii=False)}\n\n"
                yield msg.encode("utf-8")
                yield b"data: [DONE]\n\n"
                break

            if kind == "exc":
                msg = f"data: {json.dumps({'error': val}, ensure_ascii=False)}\n\n"
                yield msg.encode("utf-8")
                yield b"data: [DONE]\n\n"
                break

            if kind == "eof":
                # 正常结束：上游通常已经发了 [DONE]，这里不强行补，避免重复
                break

    # 7) 返回
    if client_wants_stream:
        h = cors_headers()
        # ====== 新增：尽量让中间层别缓冲 SSE ======
        h["Cache-Control"] = "no-cache, no-transform"
        h["X-Accel-Buffering"] = "no"

        return Response(
            stream_with_context(generate_stream()),
            content_type="text/event-stream",
            headers=h,
        )
    else:
        # 非流式：也走同一条 generate_stream，但 content-type 改成 json
        return Response(
            stream_with_context(generate_stream()),
            content_type="application/json",
            headers=cors_headers(),
        )


# ================= 启动 =================
if __name__ == "__main__":
    if os.path.exists(LOG_FILE):
        try:
            os.remove(LOG_FILE)
        except:
            pass

    write_log("BOOT", "安全代理已启动：按 model 自动分发 Kimi / BigModel（Key 透传）")
    write_log("BOOT", "入口: http://0.0.0.0:%d/v1/chat/completions" % PORT)

    # gevent 对流式转发更稳一些，你依赖里本来就带了它
    try:
        from gevent.pywsgi import WSGIServer

        http_server = WSGIServer(("0.0.0.0", PORT), app)
        http_server.serve_forever()
    except Exception as e:
        write_log("BOOT_WARN", f"gevent 启动失败，回退到 Flask 内置服务器: {e}")
        app.run(host="0.0.0.0", port=PORT, threaded=True)
