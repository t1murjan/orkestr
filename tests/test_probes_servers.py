import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "tools"))
import fake_model_servers as fk  # noqa: E402

from orkestr.probes import servers as sv  # noqa: E402
from orkestr.probes.servers import ServerProber, normalize_base  # noqa: E402


@pytest.fixture
async def prober():
    p = ServerProber()
    yield p
    await p.aclose()


def srv(kind, url):
    return {"id": "s1", "kind": kind, "base_url": url}


def run_fake(routes, **kw):
    return fk.FakeServer(routes, **kw).start()


@pytest.mark.parametrize("raw,exp", [
    ("http://127.0.0.1:1234", "http://127.0.0.1:1234"),
    ("http://127.0.0.1:1234/", "http://127.0.0.1:1234"),
    ("http://127.0.0.1:1234/v1", "http://127.0.0.1:1234"),
    ("http://127.0.0.1:1234/v1/", "http://127.0.0.1:1234"),
    ("http://h/openai/v1", "http://h/openai"),
    ("localhost:8080", "http://localhost:8080"),
])
def test_normalize(raw, exp):
    assert normalize_base(raw) == exp


# ------------------------------------------------------------------ LM Studio
async def test_lmstudio_native_v1(prober):
    f = run_fake(fk.lmstudio_routes("v1"))
    try:
        st = await prober.probe(srv("lmstudio", f.url))
    finally:
        f.stop()
    assert st["state"] == "up" and st["error"] is None and st["latency_ms"] >= 0
    assert st["slots"] == [] and st["metrics"] is None
    ms = {m["id"]: m for m in st["models"]}
    g = ms["google/gemma-4-26b-a4b"]
    assert g["state"] == "loaded" and g["context_window"] == 4096 and g["max_context_window"] == 262144
    assert g["arch"] == "gemma4" and g["quantization"] == "Q4_K_M" and g["type"] == "llm"
    assert g["size_bytes"] == 17990911801
    assert ms["deepseek-r1"]["state"] == "available" and ms["deepseek-r1"]["context_window"] is None
    assert ms["text-embedding-nomic-embed-text-v1.5-embedding"]["type"] == "embedding"
    assert st["models"][0]["state"] == "loaded"  # loaded first
    assert "/api/v0/models" not in f.requests
    assert all(a is None for a in f.auth_headers)  # no credentials ever


async def test_lmstudio_v0_fallback(prober):
    f = run_fake(fk.lmstudio_routes("v0"))
    try:
        st = await prober.probe(srv("lmstudio", f.url + "/v1"))
    finally:
        f.stop()
    assert st["state"] == "up"
    ms = {m["id"]: m for m in st["models"]}
    m = ms["meta-llama-3.1-8b-instruct"]
    assert m["state"] == "loaded" and m["context_window"] == 8192 and m["max_context_window"] == 131072
    assert m["arch"] == "llama" and m["quantization"] == "Q4_K_M"
    assert ms["qwen2-vl-7b-instruct"]["state"] == "available" and ms["qwen2-vl-7b-instruct"]["type"] == "vlm"


async def test_lmstudio_openai_only(prober):
    f = run_fake(fk.lmstudio_routes("openai"))
    try:
        st = await prober.probe(srv("lmstudio", f.url))
    finally:
        f.stop()
    assert st["state"] == "up"
    assert {m["id"] for m in st["models"]} == {"meta-llama-3.1-8b-instruct", "text-embedding-nomic-embed-text-v1.5"}
    assert all(m["state"] is None and m["context_window"] is None for m in st["models"])


async def test_lmstudio_auth_required(prober):
    f = run_fake(fk.lmstudio_routes("auth"))
    try:
        st = await prober.probe(srv("lmstudio", f.url))
    finally:
        f.stop()
    assert st["state"] == "up" and st["models"] == [] and "токен" in st["error"]


async def test_lmstudio_garbage_shapes(prober):
    f = run_fake({"/api/v1/models": (200, {"models": [None, 5, {"key": "a", "loaded_instances": "x",
                                                                  "max_context_length": "oops"}, {"nokey": 1}]})})
    try:
        st = await prober.probe(srv("lmstudio", f.url))
    finally:
        f.stop()
    assert st["state"] == "up" and [m["id"] for m in st["models"]] == ["a"]
    assert st["models"][0]["max_context_window"] is None


# ------------------------------------------------------------------ llama.cpp
async def test_llamacpp_full(prober):
    f = run_fake(fk.llamacpp_routes("full"))
    try:
        st = await prober.probe(srv("llamacpp", f.url))
    finally:
        f.stop()
    assert st["state"] == "up" and st["version"] == "b4567-abcdef0"
    m = st["models"][0]
    assert m["id"].endswith("Q4_K_M.gguf") and m["state"] == "loaded"
    assert m["context_window"] == 8192 and m["max_context_window"] == 131072 and m["size_bytes"] == 4912898304
    assert [(s["id"], s["state"], s["context_window"]) for s in st["slots"]] == [(0, "processing", 4096), (1, "idle", 4096)]
    assert st["slots"][0]["model"] == "Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf"
    assert st["metrics"] == {"tokens_per_s": 41.37, "requests_processing": 1, "requests_deferred": 2,
                             "prompt_tokens_total": 12345, "generated_tokens_total": 6789}


@pytest.mark.parametrize("variant", ["noslots", "forbidden"])
async def test_llamacpp_slots_and_metrics_disabled(prober, variant):
    f = run_fake(fk.llamacpp_routes(variant))
    try:
        st = await prober.probe(srv("llamacpp", f.url))
    finally:
        f.stop()
    assert st["state"] == "up" and st["error"] is None
    assert st["slots"] == [] and st["metrics"] is None and len(st["models"]) == 1


async def test_llamacpp_old_shapes(prober):
    f = run_fake(fk.llamacpp_routes("old"))
    try:
        st = await prober.probe(srv("llamacpp", f.url))
    finally:
        f.stop()
    assert st["state"] == "up" and st["version"] is None
    assert st["models"][0]["id"] == "llama-3.1-8b" and st["models"][0]["context_window"] == 4096
    s0, s1 = st["slots"]
    assert (s0["state"], s0["context_used"], s0["context_window"]) == ("processing", 1500, 4096)
    assert s1["state"] == "idle"
    assert st["metrics"]["requests_processing"] == 1  # derived from slots, no /metrics


async def test_llamacpp_loading_is_not_up(prober):
    f = run_fake(fk.llamacpp_routes("loading"))
    try:
        st = await prober.probe(srv("llamacpp", f.url))
    finally:
        f.stop()
    assert st["state"] == "down" and "загружает модель" in st["error"]


async def test_llamacpp_router_mode_models():
    ms = sv.parse_openai_models({"data": [
        {"id": "a", "status": {"value": "loaded"}}, {"id": "b", "status": {"value": "unloaded"}}]})
    assert [(m["id"], m["state"]) for m in ms] == [("a", "loaded"), ("b", "available")]


def test_prometheus_variants():
    assert sv.parse_llamacpp_metrics("") is None
    assert sv.parse_llamacpp_metrics("something_else 1\n") is None
    m = sv.parse_llamacpp_metrics("llamacpp_predicted_tokens_seconds 3.14159\nllamacpp_requests_processing 0\n")
    assert m["tokens_per_s"] == 3.14 and m["requests_processing"] == 0 and m["requests_deferred"] is None
    assert sv.parse_prometheus('foo{a="b"} 1\nbar 2\nbaz notnum\n') == {"bar": 2.0}


# ------------------------------------------------------------------ OpenAI / Anthropic
@pytest.mark.parametrize("suffix", ["", "/", "/v1", "/v1/"])
async def test_openai_compat_url_forms(prober, suffix):
    f = run_fake({"/v1/models": (200, {"data": [{"id": "gpt-x", "context_length": 32000}]})})
    try:
        st = await prober.probe(srv("openai_compat", f.url + suffix))
    finally:
        f.stop()
    assert st["state"] == "up" and st["models"][0]["id"] == "gpt-x"
    assert st["models"][0]["max_context_window"] == 32000
    assert f.requests == ["/v1/models"]


async def test_openai_compat_401_and_500(prober):
    f = run_fake({"/v1/models": (401, {"error": "no"})})
    try:
        st = await prober.probe(srv("openai_compat", f.url))
    finally:
        f.stop()
    assert st["state"] == "up" and "ключ" in st["error"]
    f = run_fake({"/v1/models": (500, "boom")})
    try:
        st = await prober.probe(srv("openai_compat", f.url))
    finally:
        f.stop()
    assert st["state"] == "down" and "500" in st["error"]


async def test_anthropic_any_http_answer_is_up_without_key(prober):
    f = run_fake({})  # everything 404
    try:
        st = await prober.probe(srv("anthropic", f.url))
    finally:
        f.stop()
    assert st["state"] == "up" and st["latency_ms"] is not None and st["models"] == []
    assert f.auth_headers == [None]


# ------------------------------------------------------------------ failure paths
async def test_down_connection_refused_russian(prober):
    f = run_fake({})
    port = f.port
    f.stop()
    for kind, hint in [("lmstudio", "LM Studio"), ("llamacpp", "llama-server"), ("openai_compat", "адрес"),
                       ("anthropic", "интернет")]:
        st = await prober.probe(srv(kind, f"http://127.0.0.1:{port}"))
        assert st["state"] == "down" and hint in st["error"] and f"127.0.0.1:{port}" in st["error"]
        assert st["models"] == [] and st["slots"] == [] and st["metrics"] is None


async def test_timeout(prober, monkeypatch):
    monkeypatch.setattr(sv, "TOTAL_TIMEOUT_S", 0.3)
    p = ServerProber()
    import httpx
    p._local = httpx.AsyncClient(timeout=httpx.Timeout(0.3, connect=0.3), trust_env=False)
    f = run_fake(fk.lmstudio_routes("v1"), delay_s=1.0)
    try:
        st = await p.probe(srv("lmstudio", f.url))
    finally:
        f.stop()
        await p.aclose()
    assert st["state"] == "down" and "не ответил" in st["error"]


async def test_bad_urls_and_unknown_kind(prober):
    assert (await prober.probe(srv("lmstudio", "")))["state"] == "down"
    assert "Некорректный" in (await prober.probe(srv("llamacpp", "http://")))["error"]
    assert (await prober.probe(srv("weird", "http://127.0.0.1:1")))["state"] == "unknown"


async def test_not_json_body_never_crashes(prober):
    f = run_fake({"/health": (200, "<html>hi</html>"), "/props": (200, "nope"), "/slots": (200, "[1,2"),
                  "/metrics": (200, "garbage"), "/v1/models": (200, "x")})
    try:
        st = await prober.probe(srv("llamacpp", f.url))
    finally:
        f.stop()
    assert st["state"] == "up" and st["models"] == [] and st["slots"] == [] and st["metrics"] is None
