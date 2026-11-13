"""Scripted OpenAI-compatible server that plays LM Studio in OpenCode tests.

Stdlib only.  Endpoints: ``GET /v1/models``, ``POST /v1/chat/completions``
(streaming and non-streaming, tool calls, ``usage`` in the final chunk).

The scenario is chosen by a marker in the first user message: ``[[scn:NAME]]``
(default ``plain``).  Scenarios decide the next move from the number of tool
results already present in the conversation, so the server is stateless.

Scenarios
---------
plain       one text answer
read_file   call the ``read`` tool on ``hello.txt`` in the cwd, then answer
edit        call ``write`` (``[[target:PATH]]``, default ``<cwd>/created_by_agent.txt``),
            then answer.  The tool is called even when it was not offered.
bash        call ``bash`` (``echo shell-ran``), then answer
webfetch    call ``webfetch`` on http://127.0.0.1:<port>/page, then answer
delegate    call the ``orkestr`` MCP tool ``delegate`` then answer
think       reasoning_content deltas, then an answer
slow        stream tokens for ~60 s (for cancellation tests)
multi       two tool turns (read, then read again), then answer

Run: ``python tests/tools/mock_openai_server.py --port 8804``.
Requests are appended (without headers) to ``--log`` when given.
"""
from __future__ import annotations

import argparse
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

MODEL_ID = "mock-model"
CONTEXT = 32768


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return ""


def _find_tool(tools: list[dict], *needles: str) -> dict | None:
    """Pick a tool by (case-insensitive) name; exact match first, then suffix."""
    names = [(t.get("function") or {}).get("name", "") for t in tools]
    for n in needles:
        for t, name in zip(tools, names):
            if name.lower() == n:
                return t["function"]
    for n in needles:
        for t, name in zip(tools, names):
            if name.lower().endswith("_" + n):
                return t["function"]
    return None


def _args_for(fn: dict, values: dict[str, Any]) -> dict[str, Any]:
    """Map our logical values onto the parameter names the tool really has."""
    props = ((fn.get("parameters") or {}).get("properties")) or {}
    out: dict[str, Any] = {}
    for key, val in values.items():
        for cand in (key, key.lower(), re.sub(r"([A-Z])", r"_\1", key).lower()):
            if cand in props:
                out[cand] = val
                break
        else:
            out[key] = val
    return out


def decide(body: dict, port: int) -> dict[str, Any]:
    """Return {"text": str} or {"tool": (name, args)} plus usage numbers."""
    msgs = body.get("messages") or []
    tools = body.get("tools") or []
    first_user = next((_text_of(m.get("content")) for m in msgs if m.get("role") == "user"), "")
    m = re.search(r"\[\[scn:(\w+)\]\]", first_user)
    scn = m.group(1) if m else "plain"
    cwdm = re.search(r"\[\[cwd:([^\]]+)\]\]", first_user)
    cwd = cwdm.group(1) if cwdm else "."
    tgtm = re.search(r"\[\[target:([^\]]+)\]\]", first_user)
    target = tgtm.group(1) if tgtm else f"{cwd}/created_by_agent.txt"
    results = [x for x in msgs if x.get("role") == "tool"]
    n = len(results)
    last_result = _text_of(results[-1].get("content")) if results else ""

    if not tools:  # OpenCode's title / summary side requests
        return {"text": "Test title"}

    def call(*names: str, **values: Any) -> dict[str, Any]:
        fn = _find_tool(tools, *names)
        if fn is None:
            # A hallucinating model may call a tool that was not offered.
            return {"tool": (names[-1], dict(values))}
        return {"tool": (fn["name"], _args_for(fn, values))}

    if scn == "plain":
        return {"text": "Привет! Это ответ мок-модели."}
    if scn == "think":
        return {"text": "Ответ после размышления.", "reasoning": "Сначала подумаю. Потом отвечу."}
    if scn == "slow":
        return {"text": "slow", "slow": True}
    if scn == "read_file":
        if n == 0:
            return call("read", filePath=f"{cwd}/hello.txt")
        return {"text": f"Файл прочитан: {last_result[:200]}"}
    if scn == "multi":
        if n < 2:
            return call("read", filePath=f"{cwd}/hello.txt")
        return {"text": "Готово после двух чтений."}
    if scn == "edit":
        if n == 0:
            return call("write", filePath=target, content="made by mock\n")
        return {"text": f"Результат записи: {last_result[:300]}"}
    if scn == "bash":
        if n == 0:
            return call("bash", command="echo shell-ran", description="echo")
        return {"text": f"Результат shell: {last_result[:300]}"}
    if scn == "webfetch":
        if n == 0:
            return call("webfetch", url=f"http://127.0.0.1:{port}/page", format="text")
        return {"text": f"Результат webfetch: {last_result[:300]}"}
    if scn == "delegate":
        if n == 0:
            return call("orkestr_delegate", "delegate", agent="worker", task="Сделай подзадачу")
        return {"text": f"Подчинённый ответил: {last_result[:300]}"}
    return {"text": f"unknown scenario {scn}"}


class Handler(BaseHTTPRequestHandler):
    server_version = "MockOpenAI/1"
    protocol_version = "HTTP/1.1"
    log_path: str | None = None
    lock = threading.Lock()

    def log_message(self, *a: Any) -> None:  # silence
        pass

    def _json(self, obj: Any, code: int = 200) -> None:
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/") in ("/v1/models", "/models"):
            self._json({"object": "list", "data": [
                {"id": MODEL_ID, "object": "model", "owned_by": "mock",
                 "context_length": CONTEXT, "max_context_length": CONTEXT}]})
        elif self.path.startswith("/page"):
            data = b"hello from the mock web page"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        if Handler.log_path:
            with Handler.lock, open(Handler.log_path, "a") as f:
                f.write(json.dumps({"path": self.path, "body": body}, ensure_ascii=False) + "\n")
        if not self.path.rstrip("/").endswith("/chat/completions"):
            self._json({"error": "not found"}, 404)
            return
        port = self.server.server_address[1]
        plan = decide(body, port)
        prompt_tokens = 100 + 50 * len(body.get("messages") or [])
        if plan.get("tool"):
            completion = 20
        else:
            completion = max(5, len(plan.get("text", "")) // 3)
        usage = {"prompt_tokens": prompt_tokens, "completion_tokens": completion,
                 "total_tokens": prompt_tokens + completion}
        cid = f"chatcmpl-{int(time.time() * 1000)}"
        if not body.get("stream"):
            msg: dict[str, Any] = {"role": "assistant", "content": plan.get("text")}
            finish = "stop"
            if plan.get("tool"):
                name, args = plan["tool"]
                msg = {"role": "assistant", "content": None, "tool_calls": [{
                    "id": "call_1", "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args)}}]}
                finish = "tool_calls"
            self._json({"id": cid, "object": "chat.completion", "created": int(time.time()),
                        "model": MODEL_ID, "choices": [{"index": 0, "message": msg,
                                                        "finish_reason": finish}], "usage": usage})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        def send(delta: dict, finish: str | None = None, usage_obj: dict | None = None) -> None:
            chunk: dict[str, Any] = {"id": cid, "object": "chat.completion.chunk",
                                     "created": int(time.time()), "model": MODEL_ID,
                                     "choices": [] if usage_obj else
                                     [{"index": 0, "delta": delta, "finish_reason": finish}]}
            if usage_obj:
                chunk["usage"] = usage_obj
            payload = f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode()
            self.wfile.write(f"{len(payload):x}\r\n".encode() + payload + b"\r\n")
            self.wfile.flush()

        try:
            send({"role": "assistant", "content": ""})
            if plan.get("tool"):
                name, args = plan["tool"]
                raw = json.dumps(args)
                send({"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                                      "function": {"name": name, "arguments": ""}}]})
                half = len(raw) // 2
                for part in (raw[:half], raw[half:]):
                    send({"tool_calls": [{"index": 0, "function": {"arguments": part}}]})
                send({}, "tool_calls")
            else:
                text = plan["text"]
                if plan.get("reasoning"):
                    for w in re.findall(r"\S+\s*", plan["reasoning"]):
                        send({"reasoning_content": w})
                if plan.get("slow"):
                    for i in range(120):
                        send({"content": f"tok{i} "})
                        time.sleep(0.5)
                else:
                    words = re.findall(r"\S+\s*", text)
                    for w in words:
                        send({"content": w})
                send({}, "stop")
            send({}, usage_obj=usage)
            end = b"data: [DONE]\n\n"
            self.wfile.write(f"{len(end):x}\r\n".encode() + end + b"\r\n0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass


def make_server(port: int = 8804, log_path: str | None = None) -> ThreadingHTTPServer:
    Handler.log_path = log_path
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    srv.daemon_threads = True
    return srv


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8804)
    ap.add_argument("--log", default=None)
    a = ap.parse_args()
    srv = make_server(a.port, a.log)
    print(f"mock openai on 127.0.0.1:{a.port}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
