"""Hardware sample collector (CONTRACT §2.6).

Collects CPU / load / RAM / swap through psutil, GPUs through ``nvidia-smi``
(NVIDIA) and sysfs (AMD, Intel), and classifies running AI-related processes.

What was verified and how
-------------------------
* psutil part: run for real on this machine (see tests/test_probes_hardware.py).
* ``nvidia-smi`` flags: ``--query-gpu=<fields> --format=csv,noheader,nounits`` and
  ``--query-compute-apps=pid,used_memory --format=csv,noheader,nounits`` are the
  documented, long-stable interface; memory is reported in MiB, power in W,
  unsupported fields come back as ``[N/A]`` / ``[Not Supported]``. There is no
  NVIDIA GPU in the sandbox, so parsing was verified only against recorded
  sample output (tests), not against a live driver.
* sysfs: ``/sys/class/drm/card*/device/{vendor,gpu_busy_percent,mem_info_vram_used,
  mem_info_vram_total,hwmon/hwmon*/temp1_input,power1_average|power1_input}`` follow
  the amdgpu kernel documentation (temp in millidegrees C, power in microwatts).
  Verified against a fake tree only; Intel iGPUs expose almost nothing there
  (best effort: hwmon temperature/power if present).

Privacy: command lines are only inspected to classify a process and are never
copied into the sample; ``name`` is a short display name (for llama.cpp it
includes the basename of the ``-m`` model file, nothing else).
"""
from __future__ import annotations

import asyncio
import csv
import io
import logging
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any

import psutil

log = logging.getLogger("orkestr.probes.hardware")

MAX_PROCESSES = 20
NVIDIA_TIMEOUT_S = 3.0
NVIDIA_RETRY_S = 60.0

_NVIDIA_FIELDS = "index,name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw"
_MIB = 1024 * 1024


# --------------------------------------------------------------------------- helpers
def _num(value: str | None) -> float | None:
    """Parse a number out of nvidia-smi output; ``[N/A]`` and friends -> None."""
    if value is None:
        return None
    v = value.strip()
    if not v or v.startswith("[") or v.upper() in ("N/A", "NA", "NONE", "-"):
        return None
    try:
        return float(v)
    except ValueError:
        return None


def _int_or_none(x: float | None) -> int | None:
    return None if x is None else int(x)


# --------------------------------------------------------------------------- NVIDIA
def parse_nvidia_gpus(text: str) -> list[dict[str, Any]]:
    """Parse ``--query-gpu=index,name,utilization.gpu,memory.used,memory.total,
    temperature.gpu,power.draw`` CSV (noheader, nounits; memory in MiB)."""
    gpus: list[dict[str, Any]] = []
    reader = csv.reader(io.StringIO(text), skipinitialspace=True)
    for row in reader:
        if len(row) < 7:
            continue
        row = [c.strip() for c in row]
        idx = _num(row[0])
        if idx is None:
            continue
        used, total = _num(row[3]), _num(row[4])
        gpus.append({
            "index": int(idx),
            "name": row[1] or "NVIDIA GPU",
            "vendor": "nvidia",
            "util_percent": _num(row[2]),
            "mem_used_bytes": None if used is None else int(used * _MIB),
            "mem_total_bytes": None if total is None else int(total * _MIB),
            "temp_c": _num(row[5]),
            "power_w": _num(row[6]),
        })
    return gpus


def parse_nvidia_apps(text: str) -> dict[int, int]:
    """Parse ``--query-compute-apps=pid,used_memory`` -> {pid: bytes}."""
    out: dict[int, int] = {}
    for row in csv.reader(io.StringIO(text), skipinitialspace=True):
        if len(row) < 2:
            continue
        pid, mem = _num(row[0]), _num(row[1])
        if pid is None or mem is None:
            continue
        out[int(pid)] = out.get(int(pid), 0) + int(mem * _MIB)
    return out


# --------------------------------------------------------------------------- sysfs
def _read(path: Path) -> str | None:
    try:
        return path.read_text(errors="replace").strip()
    except (OSError, ValueError):
        return None


def _read_num(path: Path) -> float | None:
    return _num(_read(path))


_VENDORS = {"0x1002": "amd", "0x1022": "amd", "0x8086": "intel", "0x10de": "nvidia"}


def read_sysfs_gpus(root: str | Path = "/sys/class/drm", start_index: int = 0) -> list[dict[str, Any]]:
    """AMD / Intel GPUs from sysfs. NVIDIA cards are skipped (nvidia-smi owns them).
    Never raises."""
    gpus: list[dict[str, Any]] = []
    try:
        cards = sorted(
            (p for p in Path(root).iterdir() if re.fullmatch(r"card\d+", p.name)),
            key=lambda p: int(p.name[4:]),
        )
    except OSError:
        return gpus
    for card in cards:
        try:
            dev = card / "device"
            vendor = _VENDORS.get((_read(dev / "vendor") or "").lower())
            if vendor not in ("amd", "intel"):
                continue
            util = _read_num(dev / "gpu_busy_percent")
            vu, vt = _read_num(dev / "mem_info_vram_used"), _read_num(dev / "mem_info_vram_total")
            temp = power = None
            for hw in sorted((dev / "hwmon").glob("hwmon*")) if (dev / "hwmon").is_dir() else []:
                if temp is None:
                    t = _read_num(hw / "temp1_input")
                    temp = None if t is None else round(t / 1000.0, 1)
                if power is None:
                    p = _read_num(hw / "power1_average")
                    if p is None:
                        p = _read_num(hw / "power1_input")
                    power = None if p is None else round(p / 1_000_000.0, 1)
            if vendor == "intel" and util is None and vt is None and temp is None and power is None:
                continue  # nothing to show for an iGPU that exposes nothing
            name = _read(dev / "product_name")
            if not name:
                name = ("AMD GPU" if vendor == "amd" else "Intel GPU") + f" ({card.name})"
            gpus.append({
                "index": start_index + len(gpus),
                "name": name,
                "vendor": vendor,
                "util_percent": util,
                "mem_used_bytes": _int_or_none(vu),
                "mem_total_bytes": _int_or_none(vt),
                "temp_c": temp,
                "power_w": power,
            })
        except Exception:  # noqa: BLE001 - hardware is weird, never fail the sample
            log.debug("sysfs gpu read failed for %s", card, exc_info=True)
    return gpus


# --------------------------------------------------------------------------- processes
_INTERPRETERS = {"node", "nodejs", "bun", "deno", "python", "python3", "electron"}


def _base(path: str | None) -> str:
    return os.path.basename(path or "").lower()


def _model_basename(cmdline: list[str]) -> str | None:
    for i, a in enumerate(cmdline):
        if a in ("-m", "--model") and i + 1 < len(cmdline):
            return os.path.basename(cmdline[i + 1]) or None
        if a.startswith("--model="):
            return os.path.basename(a.split("=", 1)[1]) or None
    return None


def classify_process(name: str | None, exe: str | None, cmdline: list[str] | None) -> tuple[str, str] | None:
    """Return ``(kind, display_name)`` for an AI-related process, else None.

    Looks at the executable / process name first and, for interpreters
    (node, bun, python), at the script path. Arguments are never returned.
    """
    n = (name or "").lower()
    eb = _base(exe)
    cmd = [c for c in (cmdline or []) if isinstance(c, str)]
    c0 = _base(cmd[0]) if cmd else ""
    exe_l = (exe or "").lower()
    names = {x for x in (n, eb, c0) if x}

    # --- LM Studio (app, lms CLI, llmster daemon)
    if any(("lm studio" in x or "lm-stud" in x or "lmstudio" in x) for x in names) or "lm-stud" in exe_l \
            or "/.lmstudio/" in exe_l or "/lm studio/" in exe_l:
        if "llmster" in names:
            return "lmstudio", "LM Studio (llmster)"
        if "lms" in names:
            return "lmstudio", "LM Studio (lms)"
        return "lmstudio", "LM Studio"
    if "llmster" in names:
        return "lmstudio", "LM Studio (llmster)"
    if "lms" in names:
        return "lmstudio", "LM Studio (lms)"

    # --- Ollama
    if any(x == "ollama" or x.startswith("ollama") for x in names):
        return "ollama", "Ollama"

    # --- llama.cpp
    for x in (eb, n, c0):
        if x and (x.startswith("llama-") or x.startswith("llama.cpp") or x in ("llama", "llama-cpp-server")) \
                and not x.startswith("llama-swap"):
            return "llamacpp", (eb or n or x) + (f" · {m}" if (m := _model_basename(cmd)) else "")
    if n in ("server", "main") or eb in ("server", "main"):
        if any(a.lower().endswith(".gguf") for a in cmd):
            return "llamacpp", "llama-server" + (f" · {m}" if (m := _model_basename(cmd)) else "")
    if any(a == "llama_cpp.server" for a in cmd[:4]):
        return "llamacpp", "llama-cpp-python server"

    # --- OpenCode
    if any(x.lstrip(".").startswith("opencode") for x in names):
        return "opencode", "OpenCode"

    # --- Claude Code
    if eb == "claude" or n == "claude" or c0 == "claude" or "/claude/versions/" in exe_l:
        return "claude_code", "Claude Code"

    # --- interpreters running a known script
    if (n in _INTERPRETERS or eb in _INTERPRETERS or n.startswith("python") or eb.startswith("python")) and len(cmd) > 1:
        for a in cmd[1:4]:
            if a.startswith("-"):
                continue
            al = a.lower()
            if "@anthropic-ai/claude-code" in al or "/claude-code/" in al or _base(a) in ("claude", "claude.js"):
                return "claude_code", "Claude Code"
            if "opencode" in al:
                return "opencode", "OpenCode"
            if "llama_cpp" in al:
                return "llamacpp", "llama-cpp-python"
            break  # only the first non-flag argument is the script
    return None


def aggregate_processes(rows: list[dict[str, Any]], gpu_mem: dict[int, int] | None = None,
                        limit: int = MAX_PROCESSES) -> list[dict[str, Any]]:
    """Merge classified processes into one entry per process tree.

    ``rows``: ``{pid, ppid, kind, name, cpu_percent, rss_bytes}``. A process whose
    parent has the same ``kind`` is folded into the topmost such ancestor (CPU,
    RSS and GPU memory are summed; RSS of shared pages may be counted twice).
    Result: heaviest first (CPU, then RSS), capped at ``limit``.
    """
    gpu_mem = gpu_mem or {}
    by_pid = {r["pid"]: r for r in rows}

    def root_of(r: dict[str, Any]) -> int:
        seen = set()
        cur = r
        while cur["ppid"] in by_pid and cur["pid"] not in seen:
            seen.add(cur["pid"])
            parent = by_pid[cur["ppid"]]
            if parent["kind"] != r["kind"]:
                break
            cur = parent
        return cur["pid"]

    groups: dict[int, dict[str, Any]] = {}
    for r in rows:
        rid = root_of(r)
        root = by_pid[rid]
        g = groups.setdefault(rid, {
            "pid": rid, "kind": root["kind"], "name": root["name"],
            "cpu_percent": 0.0, "rss_bytes": 0, "gpu_mem_bytes": None,
        })
        g["cpu_percent"] += r["cpu_percent"] or 0.0
        g["rss_bytes"] += r["rss_bytes"] or 0
        if r["pid"] in gpu_mem:
            g["gpu_mem_bytes"] = (g["gpu_mem_bytes"] or 0) + gpu_mem[r["pid"]]
    out = list(groups.values())
    for g in out:
        g["cpu_percent"] = round(g["cpu_percent"], 1)
    out.sort(key=lambda g: (g["cpu_percent"], g["rss_bytes"]), reverse=True)
    return out[:limit]


def scan_processes() -> list[dict[str, Any]]:
    """Blocking scan of all processes -> classified rows (see aggregate_processes)."""
    rows: list[dict[str, Any]] = []
    attrs = ["pid", "ppid", "name", "exe", "cmdline"]
    for p in psutil.process_iter(attrs):
        try:
            info = p.info
            hit = classify_process(info.get("name"), info.get("exe"), info.get("cmdline"))
            if hit is None:
                continue
            with p.oneshot():
                cpu = p.cpu_percent(None)
                rss = p.memory_info().rss
            rows.append({"pid": info["pid"], "ppid": info.get("ppid") or 0, "kind": hit[0],
                         "name": hit[1], "cpu_percent": cpu, "rss_bytes": rss})
        except (psutil.Error, OSError):
            continue
    return rows


# --------------------------------------------------------------------------- collector
class HardwareCollector:
    """Stateful collector: keeps the "no nvidia-smi" decision and psutil priming."""

    def __init__(self, *, sysfs_root: str | Path = "/sys/class/drm", nvidia_smi: str | None = None) -> None:
        self._sysfs_root = sysfs_root
        self._nvidia_path: str | None = nvidia_smi if nvidia_smi is not None else shutil.which("nvidia-smi")
        self._nvidia_checked_at = 0.0
        self._nvidia_down_until = 0.0
        psutil.cpu_percent(None)  # prime: the next call measures since now

    @property
    def nvidia_present(self) -> bool:
        """False once we know there is no usable nvidia-smi (cheap afterwards)."""
        return self._nvidia_path is not None

    async def prime(self) -> None:
        """Prime per-process CPU counters (first reading is always 0)."""
        await asyncio.to_thread(scan_processes)

    async def _run_smi(self, *args: str) -> str | None:
        path = self._nvidia_path
        if path is None:
            return None
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                path, *args,
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), NVIDIA_TIMEOUT_S)
            if proc.returncode != 0:
                return None
            return out.decode("utf-8", "replace")
        except FileNotFoundError:
            self._nvidia_path = None  # cached: binary vanished / never existed
            return None
        except (asyncio.TimeoutError, OSError):
            return None
        finally:
            if proc is not None and proc.returncode is None:
                try:
                    proc.kill()
                    await proc.wait()
                except Exception:  # noqa: BLE001
                    pass

    async def _nvidia(self) -> tuple[list[dict[str, Any]], dict[int, int]]:
        if self._nvidia_path is None or time.monotonic() < self._nvidia_down_until:
            return [], {}
        gpu_txt, app_txt = await asyncio.gather(
            self._run_smi(f"--query-gpu={_NVIDIA_FIELDS}", "--format=csv,noheader,nounits"),
            self._run_smi("--query-compute-apps=pid,used_memory", "--format=csv,noheader,nounits"),
        )
        if gpu_txt is None:
            if self._nvidia_path is not None:  # driver hiccup: retry later, not every tick
                self._nvidia_down_until = time.monotonic() + NVIDIA_RETRY_S
            return [], {}
        return parse_nvidia_gpus(gpu_txt), parse_nvidia_apps(app_txt or "")

    async def sample(self) -> dict[str, Any]:
        """One Hardware sample. Never raises for missing GPUs or odd processes."""
        cpu = psutil.cpu_percent(None)
        try:
            load = [round(x, 2) for x in os.getloadavg()]
        except (OSError, AttributeError):
            load = [0.0, 0.0, 0.0]
        vm = psutil.virtual_memory()
        try:
            sw = psutil.swap_memory()
            swap_used, swap_total = int(sw.used), int(sw.total)
        except Exception:  # noqa: BLE001
            swap_used = swap_total = 0

        (nv_gpus, gpu_mem), rows = await asyncio.gather(
            self._nvidia(), asyncio.to_thread(self._safe_scan)
        )
        other = await asyncio.to_thread(read_sysfs_gpus, self._sysfs_root, len(nv_gpus))
        return {
            "ts": int(time.time() * 1000),
            "cpu_percent": round(float(cpu), 1),
            "cpu_count": psutil.cpu_count(logical=True) or 1,
            "load_avg": load,
            "ram_used_bytes": int(vm.total - vm.available),
            "ram_total_bytes": int(vm.total),
            "swap_used_bytes": swap_used,
            "swap_total_bytes": swap_total,
            "gpus": nv_gpus + other,
            "processes": aggregate_processes(rows, gpu_mem),
        }

    @staticmethod
    def _safe_scan() -> list[dict[str, Any]]:
        try:
            return scan_processes()
        except Exception:  # noqa: BLE001
            log.debug("process scan failed", exc_info=True)
            return []
