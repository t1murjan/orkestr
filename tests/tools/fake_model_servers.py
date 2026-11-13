"""Fake LM Studio and llama.cpp servers for offline tests (stdlib only).

Responses are shaped like the documentation (LM Studio docs repo
``1_developer/2_rest``, llama.cpp ``tools/server/README.md``), NOT captured from
real servers. Run standalone:

    python tests/tools/fake_model_servers.py [--lmstudio-variant v1|v0|openai|auth]
                                             [--llama-variant full|noslots|old|loading|forbidden]

LM Studio on 8805, llama.cpp on 8815. In tests use ``FakeServer(routes).start()``
(port 0 = ephemeral).
"""
from __future__ import annotations

import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

Route = tuple[int, Any]  # (status, json-able body | str)

LMSTUDIO_PORT = 8805
LLAMACPP_PORT = 8815

# --------------------------------------------------------------------------- LM Studio
LMS_V1_MODELS = {"models": [
    {"type": "llm", "publisher": "google", "key": "google/gemma-4-26b-a4b", "display_name": "Gemma 4 26B A4B",
     "architecture": "gemma4", "quantization": {"name": "Q4_K_M", "bits_per_weight": 4},
     "size_bytes": 17990911801, "params_string": "26B-A4B",
     "loaded_instances": [{"id": "google/gemma-4-26b-a4b", "config": {
         "context_length": 4096, "eval_batch_size": 512, "parallel": 4, "flash_attention": True,
         "num_experts": 8, "offload_kv_cache_to_gpu": True}}],
     "max_context_length": 262144, "format": "gguf",
     "capabilities": {"vision": True, "trained_for_tool_use": True}, "description": None},
    {"type": "llm", "publisher": "deepseek", "key": "deepseek-r1", "display_name": "DeepSeek R1",
     "architecture": "deepseek", "quantization": {"name": "Q4_K_M", "bits_per_weight": 4},
     "size_bytes": 40492610355, "params_string": "671B", "loaded_instances": [],
     "max_context_length": 131072, "format": "gguf", "description": None},
    {"type": "embedding", "publisher": "gaianet", "key": "text-embedding-nomic-embed-text-v1.5-embedding",
     "display_name": "Nomic Embed Text v1.5", "quantization": {"name": "F16", "bits_per_weight": 16},
     "size_bytes": 274290560, "params_string": None, "loaded_instances": [],
     "max_context_length": 2048, "format": "gguf"},
]}

LMS_V0_MODELS = {"object": "list", "data": [
    {"id": "qwen2-vl-7b-instruct", "object": "model", "type": "vlm", "publisher": "mlx-community",
     "arch": "qwen2_vl", "compatibility_type": "mlx", "quantization": "4bit", "state": "not-loaded",
     "max_context_length": 32768},
    {"id": "meta-llama-3.1-8b-instruct", "object": "model", "type": "llm", "publisher": "lmstudio-community",
     "arch": "llama", "compatibility_type": "gguf", "quantization": "Q4_K_M", "state": "loaded",
     "max_context_length": 131072, "loaded_context_length": 8192},
]}

OPENAI_MODELS = {"object": "list", "data": [
    {"id": "meta-llama-3.1-8b-instruct", "object": "model", "owned_by": "organization_owner"},
    {"id": "text-embedding-nomic-embed-text-v1.5", "object": "model", "owned_by": "organization_owner"},
]}


def lmstudio_routes(variant: str = "v1") -> dict[str, Route]:
    """variant: v1 (native /api/v1), v0 (only legacy /api/v0), openai (no native API), auth (401 everywhere)."""
    nf: Route = (404, {"error": "Unexpected endpoint or method."})
    if variant == "auth":
        unauth: Route = (401, {"error": {"message": "Invalid or missing API token", "type": "invalid_request_error"}})
        return {"/api/v1/models": unauth, "/api/v0/models": unauth, "/v1/models": unauth}
    return {
        "/api/v1/models": (200, LMS_V1_MODELS) if variant == "v1" else nf,
        "/api/v0/models": (200, LMS_V0_MODELS) if variant in ("v1", "v0") else nf,
        "/v1/models": (200, OPENAI_MODELS),
    }


# --------------------------------------------------------------------------- llama.cpp
LLAMA_PROPS = {
    "default_generation_settings": {"id": 0, "id_task": -1, "n_ctx": 8192, "speculative": False,
                                    "is_processing": False, "params": {"n_predict": -1, "temperature": 0.8}},
    "total_slots": 2, "model_path": "../models/Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf",
    "chat_template": "...", "chat_template_caps": {}, "modalities": {"vision": False},
    "build_info": "b4567-abcdef0", "is_sleeping": False,
}
LLAMA_MODELS = {"object": "list", "data": [{
    "id": "../models/Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf", "object": "model",
    "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
    "created": 1735142223, "owned_by": "llamacpp",
    "meta": {"vocab_type": 2, "n_vocab": 128256, "n_ctx_train": 131072, "n_embd": 4096,
             "n_params": 8030261312, "size": 4912898304}}]}
LLAMA_SLOTS = [
    {"id": 0, "id_task": 135, "n_ctx": 4096, "speculative": False, "is_processing": True,
     "params": {"n_predict": -1}, "next_token": {"has_next_token": True, "n_remain": -1, "n_decoded": 136}},
    {"id": 1, "id_task": -1, "n_ctx": 4096, "speculative": False, "is_processing": False,
     "params": {"n_predict": -1}, "next_token": {"has_next_token": False, "n_remain": -1, "n_decoded": 0}},
]
LLAMA_SLOTS_OLD = [
    {"id": 0, "state": 1, "n_ctx": 4096, "n_past": 1500, "model": "llama-3.1-8b"},
    {"id": 1, "state": 0, "n_ctx": 4096, "n_past": 0, "model": "llama-3.1-8b"},
]
LLAMA_METRICS = """# HELP llamacpp:prompt_tokens_total Number of prompt tokens processed.
# TYPE llamacpp:prompt_tokens_total counter
llamacpp:prompt_tokens_total 12345
# HELP llamacpp:prompt_seconds_total Prompt process time
# TYPE llamacpp:prompt_seconds_total counter
llamacpp:prompt_seconds_total 20.5
# HELP llamacpp:tokens_predicted_total Number of generation tokens processed.
# TYPE llamacpp:tokens_predicted_total counter
llamacpp:tokens_predicted_total 6789
# HELP llamacpp:prompt_tokens_seconds Average prompt throughput in tokens/s.
# TYPE llamacpp:prompt_tokens_seconds gauge
llamacpp:prompt_tokens_seconds 602.1
# HELP llamacpp:predicted_tokens_seconds Average generation throughput in tokens/s.
# TYPE llamacpp:predicted_tokens_seconds gauge
llamacpp:predicted_tokens_seconds 41.37
# HELP llamacpp:requests_processing Number of requests processing.
# TYPE llamacpp:requests_processing gauge
llamacpp:requests_processing 1
# HELP llamacpp:requests_deferred Number of requests deferred.
# TYPE llamacpp:requests_deferred gauge
llamacpp:requests_deferred 2
"""


def llamacpp_routes(variant: str = "full") -> dict[str, Route]:
    """variant: full | noslots (--no-slots, no --metrics: 501) | old (legacy shapes, 404 metrics) |
    loading (/health 503) | forbidden (slots+metrics 403)."""
    not_supported = lambda what: (501, {"error": {"code": 501, "message": f"This server does not support {what} endpoint.",  # noqa: E731
                                                  "type": "not_supported_error"}})
    ok: Route = (200, {"status": "ok"})
    if variant == "loading":
        return {"/health": (503, {"error": {"code": 503, "message": "Loading model", "type": "unavailable_error"}})}
    r: dict[str, Route] = {
        "/health": ok, "/props": (200, LLAMA_PROPS), "/v1/models": (200, LLAMA_MODELS),
        "/slots": (200, LLAMA_SLOTS), "/metrics": (200, LLAMA_METRICS),
    }
    if variant == "noslots":
        r["/slots"], r["/metrics"] = not_supported("slots"), not_supported("metrics")
    elif variant == "old":
        r["/props"] = (200, {"assistant_name": "", "user_name": "", "default_generation_settings": {
            "n_ctx": 4096, "model": "llama-3.1-8b"}, "total_slots": 2})
        r["/slots"] = (200, LLAMA_SLOTS_OLD)
        r["/metrics"] = (404, {"error": "File Not Found"})
        r["/v1/models"] = (200, {"object": "list", "data": [{"id": "llama-3.1-8b", "object": "model",
                                                             "created": 1, "owned_by": "llamacpp"}]})
    elif variant == "forbidden":
        deny: Route = (403, {"error": {"code": 403, "message": "Forbidden", "type": "permission_error"}})
        r["/slots"], r["/metrics"] = deny, deny
    return r


# --------------------------------------------------------------------------- server
class FakeServer:
    """Tiny threaded HTTP server. ``routes`` maps path -> (status, body) or a callable returning one.
    Anything else: 404. ``requests`` records the paths seen. ``delay_s`` slows every answer."""

    def __init__(self, routes: dict[str, Route | Callable[[], Route]], *, delay_s: float = 0.0) -> None:
        self.routes = routes
        self.delay_s = delay_s
        self.requests: list[str] = []
        self.auth_headers: list[str | None] = []
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.port = 0

    def start(self, port: int = 0, host: str = "127.0.0.1") -> "FakeServer":
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:  # silence
                pass

            def do_GET(self) -> None:  # noqa: N802
                path = self.path.split("?", 1)[0]
                outer.requests.append(path)
                outer.auth_headers.append(self.headers.get("Authorization"))
                if outer.delay_s:
                    time.sleep(outer.delay_s)
                route = outer.routes.get(path)
                if callable(route):
                    route = route()
                status, body = route if route else (404, {"error": "File Not Found"})
                if isinstance(body, (dict, list)):
                    data, ctype = json.dumps(body).encode(), "application/json"
                else:
                    data, ctype = str(body).encode(), "text/plain; version=0.0.4"
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self._httpd = ThreadingHTTPServer((host, port), H)
        self._httpd.daemon_threads = True
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=lambda: self._httpd.serve_forever(poll_interval=0.02), daemon=True)
        self._thread.start()
        return self

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lmstudio-variant", default="v1", choices=["v1", "v0", "openai", "auth"])
    ap.add_argument("--llama-variant", default="full", choices=["full", "noslots", "old", "loading", "forbidden"])
    ap.add_argument("--lmstudio-port", type=int, default=LMSTUDIO_PORT)
    ap.add_argument("--llama-port", type=int, default=LLAMACPP_PORT)
    a = ap.parse_args()
    s1 = FakeServer(lmstudio_routes(a.lmstudio_variant)).start(a.lmstudio_port)
    s2 = FakeServer(llamacpp_routes(a.llama_variant)).start(a.llama_port)
    print(f"fake LM Studio {s1.url} ({a.lmstudio_variant}), fake llama.cpp {s2.url} ({a.llama_variant})", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        s1.stop()
        s2.stop()


if __name__ == "__main__":
    main()
