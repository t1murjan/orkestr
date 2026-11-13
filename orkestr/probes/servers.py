"""Probes for model servers (CONTRACT §2.5): returns the ``status`` dict.

Documentation used (fetched 2026-11, not from memory)
-----------------------------------------------------
* LM Studio docs repo ``lmstudio-ai/docs`` (cloned, last commit 2026-10-20):
  ``1_developer/2_rest/list.md`` -> ``GET /api/v1/models`` returns
  ``{"models":[{type: llm|embedding|decision, key, display_name, architecture,
  quantization:{name,bits_per_weight}, size_bytes, loaded_instances:[{id,
  config:{context_length,...}}], max_context_length, format, ...}]}``;
  ``endpoints.mdx`` -> legacy ``GET /api/v0/models`` returns ``{"data":[{id, type:
  llm|vlm|embeddings, arch, quantization:"Q4_K_M", state:"loaded"|"not-loaded",
  max_context_length, loaded_context_length (only when loaded)}]}``.
  Fallback: OpenAI-compatible ``GET /v1/models`` (lists all downloaded models, no
  load state). LM Studio has no version endpoint -> ``version`` is null.
  Servers may require ``Authorization: Bearer`` (401): we never send keys, the
  status is "up" with a Russian note and no model list.
* llama.cpp ``tools/server/README.md`` (master, fetched 2026-11): ``GET /health``
  (200 ``{"status":"ok"}``, 503 while loading), ``GET /props`` (``total_slots``,
  ``model_path``, ``build_info``, ``default_generation_settings.n_ctx``,
  ``is_sleeping``), ``GET /slots`` (list of ``{id, n_ctx, is_processing, ...}``;
  501 when started with ``--no-slots``), ``GET /metrics`` (Prometheus, only with
  ``--metrics``; 501 otherwise), ``GET /v1/models`` (``data[0].id`` = alias or model
  path, ``meta.{n_ctx_train,size,...}``, router mode adds ``status.value``).

Best-effort fields (names differ between versions, parsed with candidate keys,
``null`` when absent): llama.cpp slot ``context_used`` (``n_past`` /
``n_tokens`` / ``n_ctx_used`` — the current README does not document one);
slot ``state`` (``is_processing`` bool, or legacy int ``state`` 0/1); metrics
(``llamacpp:`` or ``llamacpp_`` prefix); model ``context_window`` for llama.cpp
(``/props`` ``n_ctx`` is the TOTAL context across slots; per-slot context is in
the slot list); LM Studio ``size_bytes``/``quantization`` on the OpenAI fallback.

Validation: against responses shaped like the documents above served by
tests/tools/fake_model_servers.py. See that file / the report for whether a real
llama-server was also tried.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

log = logging.getLogger("orkestr.probes.servers")

CONNECT_TIMEOUT_S = 1.5
TOTAL_TIMEOUT_S = 4.0
OVERALL_LIMIT_S = 12.0
LOCAL_KINDS = ("lmstudio", "llamacpp", "openai_compat")


# --------------------------------------------------------------------------- helpers
def normalize_base(url: str) -> str:
    """Root URL without trailing slash and without a trailing ``/v1``."""
    u = (url or "").strip()
    if u and "://" not in u:
        u = "http://" + u
    parts = urlsplit(u)
    path = parts.path.rstrip("/")
    if path.endswith("/v1"):
        path = path[:-3]
    return urlunsplit((parts.scheme, parts.netloc, path.rstrip("/"), "", ""))


def _hostport(url: str) -> str:
    return urlsplit(url).netloc or url


def _now_ms() -> int:
    return int(time.time() * 1000)


def _as_int(v: Any) -> int | None:
    if isinstance(v, bool) or v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return int(f) if f == f and abs(f) != float("inf") else None


def _as_num(v: Any) -> float | None:
    if isinstance(v, bool) or v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and abs(f) != float("inf") else None


def _first(d: Any, *keys: str) -> Any:
    if not isinstance(d, dict):
        return None
    for k in keys:
        if d.get(k) is not None:
            return d[k]
    return None


def _str(v: Any) -> str | None:
    return v if isinstance(v, str) and v else None


def _model(id_: str, **kw: Any) -> dict[str, Any]:
    m: dict[str, Any] = {
        "id": id_, "state": None, "context_window": None, "max_context_window": None,
        "arch": None, "quantization": None, "size_bytes": None, "type": None,
    }
    m.update(kw)
    return m


def make_status(state: str, *, latency_ms: int | None = None, error: str | None = None,
                version: str | None = None, models: list | None = None, slots: list | None = None,
                metrics: dict | None = None) -> dict[str, Any]:
    return {
        "state": state, "checked_at": _now_ms(), "latency_ms": latency_ms, "error": error,
        "version": version, "models": models or [], "slots": slots or [], "metrics": metrics,
    }


# --------------------------------------------------------------------------- parsers
def parse_openai_models(body: Any, *, state: str | None = None) -> list[dict[str, Any]]:
    """``/v1/models`` -> models. Tolerates ``{"data":[...]}``, ``{"models":[...]}``, bare lists."""
    items = body
    if isinstance(body, dict):
        items = body.get("data") if isinstance(body.get("data"), list) else body.get("models")
    out: list[dict[str, Any]] = []
    for it in items if isinstance(items, list) else []:
        if isinstance(it, str):
            out.append(_model(it, state=state))
            continue
        if not isinstance(it, dict):
            continue
        mid = _str(_first(it, "id", "name", "model", "key"))
        if not mid:
            continue
        meta = it.get("meta") if isinstance(it.get("meta"), dict) else {}
        st = state
        status = it.get("status")
        if isinstance(status, dict) and isinstance(status.get("value"), str):  # llama.cpp router mode
            st = "loaded" if status["value"] == "loaded" else "available"
        out.append(_model(
            mid, state=st,
            max_context_window=_as_int(_first(meta, "n_ctx_train", "max_context_length")
                                       or _first(it, "max_context_length", "context_length")),
            size_bytes=_as_int(_first(meta, "size") or _first(it, "size_bytes", "size")),
            arch=_str(_first(it, "arch", "architecture") if isinstance(_first(it, "arch", "architecture"), str) else None),
            quantization=_str(_first(it, "quantization")),
            type=_str(_first(it, "type")),
        ))
    return out


def _quant_name(q: Any) -> str | None:
    if isinstance(q, dict):
        return _str(q.get("name"))
    return _str(q)


def parse_lmstudio_v1(body: Any) -> list[dict[str, Any]]:
    """``GET /api/v1/models`` (``{"models": [...]}``)."""
    items = body.get("models") if isinstance(body, dict) else body
    out: list[dict[str, Any]] = []
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict):
            continue
        key = _str(_first(it, "key", "id", "model_key", "modelKey"))
        if not key:
            continue
        inst = [i for i in (it.get("loaded_instances") or []) if isinstance(i, dict)]
        ctx = None
        for i in inst:
            c = _as_int(_first(i.get("config") if isinstance(i.get("config"), dict) else {}, "context_length"))
            if c is not None:
                ctx = max(ctx or 0, c)
        arch = it.get("architecture")
        out.append(_model(
            key, state="loaded" if inst else "available",
            context_window=ctx if inst else None,
            max_context_window=_as_int(_first(it, "max_context_length", "maxContextLength")),
            arch=_str(arch if isinstance(arch, str) else _first(it, "arch")),
            quantization=_quant_name(it.get("quantization")),
            size_bytes=_as_int(_first(it, "size_bytes", "size")),
            type=_str(_first(it, "type")),
        ))
    return out


def parse_lmstudio_v0(body: Any) -> list[dict[str, Any]]:
    """Legacy ``GET /api/v0/models`` (``{"data": [...]}``)."""
    items = body.get("data") if isinstance(body, dict) else body
    out: list[dict[str, Any]] = []
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict):
            continue
        mid = _str(_first(it, "id", "key"))
        if not mid:
            continue
        loaded = _str(it.get("state")) == "loaded"
        out.append(_model(
            mid, state="loaded" if loaded else "available",
            context_window=_as_int(_first(it, "loaded_context_length")) if loaded else None,
            max_context_window=_as_int(_first(it, "max_context_length")),
            arch=_str(_first(it, "arch", "architecture")),
            quantization=_quant_name(it.get("quantization")),
            size_bytes=_as_int(_first(it, "size_bytes", "size")),
            type=_str(_first(it, "type")),
        ))
    return out


def _sort_models(models: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(models, key=lambda m: (m["state"] != "loaded", m["id"].lower()))


def parse_llamacpp_slots(body: Any, default_model: str | None = None) -> list[dict[str, Any]]:
    """``GET /slots`` -> contract slots. Handles ``is_processing`` and legacy ``state`` ints."""
    items = body
    if isinstance(body, dict):
        items = body.get("slots") if isinstance(body.get("slots"), list) else None
    out: list[dict[str, Any]] = []
    for pos, it in enumerate(items if isinstance(items, list) else []):
        if not isinstance(it, dict):
            continue
        sid = _as_int(it.get("id"))
        proc = it.get("is_processing")
        if proc is None:
            st = it.get("state")
            proc = bool(st) if isinstance(st, (int, float)) and not isinstance(st, bool) else (st == "processing")
        n_ctx = _as_int(_first(it, "n_ctx", "context_size"))
        used = _as_int(_first(it, "n_past", "n_tokens", "n_ctx_used", "tokens_cached", "n_cache_tokens"))
        if used is None:
            nt = it.get("next_token")
            if isinstance(nt, dict):
                used = None  # n_decoded is generation progress, not context fill
        out.append({
            "id": sid if sid is not None else pos,
            "state": "processing" if proc else "idle",
            "context_window": n_ctx,
            "context_used": used,
            "model": _str(_first(it, "model", "model_alias")) or default_model,
        })
    return out


_PROM_RE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{[^}]*\})?\s+([^\s]+)")


def parse_prometheus(text: str) -> dict[str, float]:
    """Minimal Prometheus text parser: unlabeled samples (first wins) -> {name: value}."""
    out: dict[str, float] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        m = _PROM_RE.match(line)
        if not m or m.group(2):
            continue
        try:
            out.setdefault(m.group(1), float(m.group(3)))
        except ValueError:
            continue
    return out


def parse_llamacpp_metrics(text: str) -> dict[str, Any] | None:
    vals = parse_prometheus(text)
    if not vals:
        return None

    def g(*names: str) -> float | None:
        for n in names:
            for pre in ("llamacpp:", "llamacpp_"):
                if pre + n in vals:
                    return vals[pre + n]
        return None

    tps, proc, defer = g("predicted_tokens_seconds"), g("requests_processing"), g("requests_deferred")
    pt, gt = g("prompt_tokens_total"), g("tokens_predicted_total")
    if all(x is None for x in (tps, proc, defer, pt, gt)):
        return None
    return {
        "tokens_per_s": None if tps is None else round(tps, 2),
        "requests_processing": _as_int(proc), "requests_deferred": _as_int(defer),
        "prompt_tokens_total": _as_int(pt), "generated_tokens_total": _as_int(gt),
    }


# --------------------------------------------------------------------------- prober
class _Reply:
    __slots__ = ("status", "json", "text", "ms")

    def __init__(self, status: int, text: str, ms: int) -> None:
        self.status, self.text, self.ms = status, text, ms
        self.json: Any = None
        if text and text.lstrip()[:1] in ("{", "["):
            try:
                import json
                self.json = json.loads(text)
            except ValueError:
                self.json = None

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


class ServerProber:
    """Owns the shared ``httpx.AsyncClient``s. Never sends credentials.

    A local client ignores proxy environment variables (a system HTTPS proxy
    must never intercept 127.0.0.1); the cloud client honours them.
    """

    def __init__(self) -> None:
        t = httpx.Timeout(TOTAL_TIMEOUT_S, connect=CONNECT_TIMEOUT_S)
        self._local = httpx.AsyncClient(timeout=t, trust_env=False, follow_redirects=False)
        self._cloud = httpx.AsyncClient(timeout=t, trust_env=True, follow_redirects=False)

    async def aclose(self) -> None:
        await self._local.aclose()
        await self._cloud.aclose()

    async def _get(self, client: httpx.AsyncClient, url: str) -> _Reply:
        t0 = time.perf_counter()
        r = await client.get(url, headers={"Accept": "application/json", "User-Agent": "orkestr-probe"})
        ms = int((time.perf_counter() - t0) * 1000)
        return _Reply(r.status_code, r.text[:2_000_000], ms)

    async def _try(self, client: httpx.AsyncClient, url: str) -> _Reply | None:
        """GET that swallows transport errors (for secondary endpoints)."""
        try:
            return await self._get(client, url)
        except (httpx.HTTPError, ValueError):
            return None

    async def probe(self, server: dict[str, Any]) -> dict[str, Any]:
        """Probe one server. Always returns a status dict, never raises."""
        kind = server.get("kind")
        base = normalize_base(str(server.get("base_url") or ""))
        if not base or not urlsplit(base).netloc:
            return make_status("down", error=f"Некорректный адрес сервера: «{server.get('base_url') or ''}».")
        hp = _hostport(base)
        try:
            return await asyncio.wait_for(self._dispatch(kind, base), OVERALL_LIMIT_S)
        except httpx.ConnectError:
            return make_status("down", error=self._down_msg(kind, hp))
        except (httpx.TimeoutException, asyncio.TimeoutError):
            return make_status("down", error=f"Сервер {hp} не ответил за {int(TOTAL_TIMEOUT_S)} с — он перегружен или завис.")
        except httpx.InvalidURL:
            return make_status("down", error=f"Некорректный адрес сервера: «{server.get('base_url')}».")
        except httpx.HTTPError as e:
            return make_status("down", error=f"Ошибка соединения с {hp} ({type(e).__name__}).")
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - a weird server must not break monitoring
            log.warning("probe of %s failed unexpectedly: %r", hp, e)
            return make_status("down", error=f"Не удалось разобрать ответ сервера {hp}.")

    @staticmethod
    def _down_msg(kind: str | None, hp: str) -> str:
        if kind == "lmstudio":
            return f"Сервер не отвечает на {hp} — LM Studio запущен и включён ли локальный сервер?"
        if kind == "llamacpp":
            return f"Сервер не отвечает на {hp} — запущен ли llama-server на этом порту?"
        if kind == "anthropic":
            return f"Нет связи с {hp} — проверьте подключение к интернету."
        return f"Сервер не отвечает на {hp} — проверьте адрес и что сервер запущен."

    async def _dispatch(self, kind: str | None, base: str) -> dict[str, Any]:
        if kind == "lmstudio":
            return await self._lmstudio(base)
        if kind == "llamacpp":
            return await self._llamacpp(base)
        if kind == "anthropic":
            return await self._anthropic(base)
        if kind == "openai_compat":
            return await self._openai(base)
        return make_status("unknown", error=f"Неизвестный тип сервера: {kind}.")

    # ---- anthropic: reachability only, unauthenticated, any HTTP answer = up
    async def _anthropic(self, base: str) -> dict[str, Any]:
        r = await self._get(self._cloud, base + "/")
        return make_status("up", latency_ms=r.ms)

    # ---- generic OpenAI compatible
    async def _openai(self, base: str) -> dict[str, Any]:
        r = await self._get(self._local, base + "/v1/models")
        if r.status in (401, 403):
            return make_status("up", latency_ms=r.ms, error="Сервер отвечает, но требует ключ доступа — список моделей недоступен.")
        if not r.ok:
            return make_status("down", latency_ms=r.ms, error=f"Сервер {_hostport(base)} ответил ошибкой {r.status} на /v1/models.")
        return make_status("up", latency_ms=r.ms, models=_sort_models(parse_openai_models(r.json)))

    # ---- LM Studio
    async def _lmstudio(self, base: str) -> dict[str, Any]:
        c = self._local
        r = await self._get(c, base + "/api/v1/models")
        parser = parse_lmstudio_v1
        if r.status in (404, 405) or (r.ok and r.json is None):
            r = await self._get(c, base + "/api/v0/models")
            parser = parse_lmstudio_v0
        if r.status in (404, 405) or (r.ok and r.json is None):
            r = await self._get(c, base + "/v1/models")
            parser = lambda b: parse_openai_models(b)  # noqa: E731 - no load info on this endpoint
        if r.status in (401, 403):
            return make_status("up", latency_ms=r.ms, error="LM Studio отвечает, но требует токен API — список моделей недоступен.")
        if not r.ok:
            return make_status("down", latency_ms=r.ms, error=f"Сервер {_hostport(base)} ответил ошибкой {r.status} — это точно LM Studio?")
        return make_status("up", latency_ms=r.ms, models=_sort_models(parser(r.json)))

    # ---- llama.cpp
    async def _llamacpp(self, base: str) -> dict[str, Any]:
        c = self._local
        hp = _hostport(base)
        # health first: it decides up/down; the rest is gathered concurrently
        health = await self._get(c, base + "/health")
        if health.status == 503:
            return make_status("down", latency_ms=health.ms, error="llama-server запущен, но ещё загружает модель — подождите.")
        props_r, slots_r, metrics_r, models_r = await asyncio.gather(
            self._try(c, base + "/props"), self._try(c, base + "/slots"),
            self._try(c, base + "/metrics"), self._try(c, base + "/v1/models"),
        )
        if health.status in (401, 403):
            return make_status("up", latency_ms=health.ms, error="llama-server требует ключ API — подробности недоступны.")
        if not health.ok:
            # very old / proxied builds: accept a working /v1/models as proof of life
            if not (models_r and models_r.ok):
                return make_status("down", latency_ms=health.ms, error=f"Сервер {hp} ответил ошибкой {health.status} на /health — это llama-server?")
        props = props_r.json if props_r and props_r.ok and isinstance(props_r.json, dict) else {}
        gen = props.get("default_generation_settings") if isinstance(props.get("default_generation_settings"), dict) else {}
        n_ctx = _as_int(_first(gen, "n_ctx") if gen else _first(props, "n_ctx"))
        alias = _str(_first(props, "model_alias", "alias"))
        path = _str(_first(props, "model_path", "model"))
        default_name = alias or (os.path.basename(path) if path else None)
        version = _str(_first(props, "build_info", "version"))
        if props.get("is_sleeping") is True and not version:
            version = None

        models = parse_openai_models(models_r.json, state="loaded") if models_r and models_r.ok else []
        if not models and default_name:
            models = [_model(default_name, state="loaded")]
        for m in models:
            if m["state"] == "loaded" and m["context_window"] is None:
                m["context_window"] = n_ctx
        slots = parse_llamacpp_slots(slots_r.json, default_name) if slots_r and slots_r.ok else []
        for s in slots:
            if s["context_window"] is None:
                s["context_window"] = None if n_ctx is None else n_ctx
        metrics = parse_llamacpp_metrics(slots_text(metrics_r)) if metrics_r and metrics_r.ok else None
        if metrics is None and slots and metrics_r is not None and not metrics_r.ok:
            # no /metrics: derive what we can from slots
            metrics = {"tokens_per_s": None,
                       "requests_processing": sum(1 for s in slots if s["state"] == "processing"),
                       "requests_deferred": None, "prompt_tokens_total": None, "generated_tokens_total": None}
        return make_status("up", latency_ms=health.ms, version=version, models=_sort_models(models),
                           slots=slots, metrics=metrics)


def slots_text(r: _Reply | None) -> str:
    return r.text if r is not None else ""
