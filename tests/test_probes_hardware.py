import asyncio
from pathlib import Path

import pytest

from orkestr.probes.hardware import (
    HardwareCollector, aggregate_processes, classify_process, parse_nvidia_apps,
    parse_nvidia_gpus, read_sysfs_gpus,
)

NVIDIA_MULTI = """0, NVIDIA GeForce RTX 4090, 37, 1523, 24564, 45, 68.12
1, NVIDIA RTX A4000, 0, 5, 16376, 31, [N/A]
2, Tesla T4, [N/A], [N/A], 15360, [Not Supported], [N/A]
"""


def test_nvidia_csv_multi_gpu_and_na():
    g = parse_nvidia_gpus(NVIDIA_MULTI)
    assert [x["index"] for x in g] == [0, 1, 2]
    assert g[0]["name"] == "NVIDIA GeForce RTX 4090"
    assert g[0]["mem_used_bytes"] == 1523 * 1024 * 1024
    assert g[0]["mem_total_bytes"] == 24564 * 1024 * 1024
    assert g[0]["util_percent"] == 37 and g[0]["temp_c"] == 45 and g[0]["power_w"] == 68.12
    assert g[1]["power_w"] is None and g[1]["util_percent"] == 0
    assert g[2]["util_percent"] is None and g[2]["mem_used_bytes"] is None and g[2]["temp_c"] is None
    assert all(x["vendor"] == "nvidia" for x in g)


def test_nvidia_garbage():
    assert parse_nvidia_gpus("") == []
    assert parse_nvidia_gpus("NVIDIA-SMI has failed because it couldn't communicate") == []
    assert parse_nvidia_apps("") == {}


def test_nvidia_apps():
    assert parse_nvidia_apps("1234, 2048\n1234, 100\n77, [N/A]\n55, 512\n") == {
        1234: 2148 * 1024 * 1024, 55: 512 * 1024 * 1024}


def _card(root: Path, name: str, vendor: str, **files: str) -> Path:
    dev = root / name / "device"
    dev.mkdir(parents=True)
    (dev / "vendor").write_text(vendor + "\n")
    for k, v in files.items():
        p = dev / k
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(v)
    return dev


def test_sysfs_amd_intel(tmp_path):
    _card(tmp_path, "card0", "0x1002", gpu_busy_percent="42\n", mem_info_vram_used="1073741824\n",
          mem_info_vram_total="8589934592\n", product_name="Radeon RX 7600\n",
          **{"hwmon/hwmon3/temp1_input": "55000\n", "hwmon/hwmon3/power1_average": "90000000\n"})
    _card(tmp_path, "card1", "0x8086")  # iGPU exposing nothing -> skipped
    _card(tmp_path, "card2", "0x8086", **{"hwmon/hwmon5/temp1_input": "40500\n"})
    _card(tmp_path, "card3", "0x10de")  # nvidia: left to nvidia-smi
    (tmp_path / "card0-HDMI-A-1").mkdir()
    g = read_sysfs_gpus(tmp_path, start_index=1)
    assert [x["index"] for x in g] == [1, 2]
    a = g[0]
    assert a["vendor"] == "amd" and a["name"] == "Radeon RX 7600" and a["util_percent"] == 42
    assert a["mem_used_bytes"] == 1 << 30 and a["mem_total_bytes"] == 8 << 30
    assert a["temp_c"] == 55.0 and a["power_w"] == 90.0
    assert g[1]["vendor"] == "intel" and g[1]["temp_c"] == 40.5 and g[1]["util_percent"] is None


def test_sysfs_missing_and_garbage(tmp_path):
    assert read_sysfs_gpus(tmp_path / "nope") == []
    _card(tmp_path, "card0", "0x1002", gpu_busy_percent="abc", mem_info_vram_total="")
    g = read_sysfs_gpus(tmp_path)
    assert g[0]["util_percent"] is None and g[0]["mem_total_bytes"] is None


@pytest.mark.parametrize("name,exe,cmd,expected", [
    ("claude", "/home/u/.local/bin/claude", ["claude", "-p", "secret-token-xyz"], ("claude_code", "Claude Code")),
    ("2.0.5", "/home/u/.local/share/claude/versions/2.0.5", ["claude"], ("claude_code", "Claude Code")),
    ("node", "/usr/bin/node", ["node", "/usr/lib/node_modules/@anthropic-ai/claude-code/cli.js", "--x"], ("claude_code", "Claude Code")),
    ("opencode", "/home/u/.opencode/bin/opencode", ["opencode", "serve"], ("opencode", "OpenCode")),
    (".opencode-wrapped", None, [], ("opencode", "OpenCode")),
    ("LM Studio", "/opt/LM Studio/lm-studio", ["/opt/LM Studio/lm-studio", "--type=renderer"], ("lmstudio", "LM Studio")),
    ("lm-studio", "/tmp/.mount_LM-Stud1/lm-studio", [], ("lmstudio", "LM Studio")),
    ("lms", "/home/u/.lmstudio/bin/lms", ["lms", "server", "start"], ("lmstudio", "LM Studio (lms)")),
    ("llmster", "/home/u/.lmstudio/llmster/x/llmster", [], ("lmstudio", "LM Studio (llmster)")),
    ("llama-server", "/usr/local/bin/llama-server", ["llama-server", "-m", "/models/qwen.gguf", "--api-key", "SECRET"],
     ("llamacpp", "llama-server · qwen.gguf")),
    ("llama-cli", "/b/llama-cli", ["llama-cli", "--model=/m/x.gguf"], ("llamacpp", "llama-cli · x.gguf")),
    ("server", "/b/server", ["server", "-m", "/m/old.gguf"], ("llamacpp", "llama-server · old.gguf")),
    ("python3", "/usr/bin/python3", ["python3", "-m", "llama_cpp.server"], ("llamacpp", "llama-cpp-python server")),
    ("ollama", "/usr/local/bin/ollama", ["ollama", "serve"], ("ollama", "Ollama")),
])
def test_classify_positive(name, exe, cmd, expected):
    got = classify_process(name, exe, cmd)
    assert got == expected
    assert "SECRET" not in got[1] and "secret" not in got[1]


@pytest.mark.parametrize("name,exe,cmd", [
    ("bash", "/bin/bash", ["bash", "-c", "echo claude"]),
    ("python3", "/usr/bin/python3", ["python3", "app.py", "opencode"]),
    ("node", "/usr/bin/node", ["node", "server.js"]),
    ("llama-swap", "/x/llama-swap", []),
    ("systemd", "/sbin/init", ["/sbin/init"]),
    (None, None, None),
])
def test_classify_negative(name, exe, cmd):
    assert classify_process(name, exe, cmd) is None


def _row(pid, ppid, kind, cpu, rss, name="x"):
    return dict(pid=pid, ppid=ppid, kind=kind, name=name, cpu_percent=cpu, rss_bytes=rss)


def test_aggregate_children_and_gpu_and_cap():
    rows = [
        _row(10, 1, "lmstudio", 5.0, 1000, "LM Studio"),
        _row(11, 10, "lmstudio", 20.0, 3000, "LM Studio"),
        _row(12, 11, "lmstudio", 1.0, 500, "LM Studio"),
        _row(20, 10, "claude_code", 2.0, 100, "Claude Code"),  # different kind child stays separate
        _row(30, 1, "llamacpp", 0.0, 9000, "llama-server"),
    ]
    out = aggregate_processes(rows, {30: 4096})
    by = {o["pid"]: o for o in out}
    assert set(by) == {10, 20, 30}
    assert by[10]["cpu_percent"] == 26.0 and by[10]["rss_bytes"] == 4500 and by[10]["gpu_mem_bytes"] is None
    assert by[30]["gpu_mem_bytes"] == 4096
    assert [o["pid"] for o in out] == [10, 20, 30]  # heaviest (cpu, then rss) first

    many = [_row(100 + i, 1, "claude_code", float(i), i) for i in range(30)]
    capped = aggregate_processes(many)
    assert len(capped) == 20 and capped[0]["cpu_percent"] == 29.0
    assert not any("cmdline" in o or "args" in o for o in capped)


async def test_real_sample_shape_and_no_gpu_ok(tmp_path):
    c = HardwareCollector(sysfs_root=tmp_path, nvidia_smi=None)
    c._nvidia_path = None  # force "no nvidia" regardless of host
    await c.prime()
    await asyncio.sleep(0.1)
    s = await c.sample()
    assert set(s) == {"ts", "cpu_percent", "cpu_count", "load_avg", "ram_used_bytes", "ram_total_bytes",
                      "swap_used_bytes", "swap_total_bytes", "gpus", "processes"}
    assert isinstance(s["ts"], int) and s["cpu_count"] >= 1 and len(s["load_avg"]) == 3
    assert 0 <= s["cpu_percent"] <= 100
    assert 0 < s["ram_used_bytes"] <= s["ram_total_bytes"]
    assert s["gpus"] == [] and isinstance(s["processes"], list) and len(s["processes"]) <= 20
    assert not c.nvidia_present


async def test_nvidia_with_fake_binary(tmp_path):
    exe = tmp_path / "nvidia-smi"
    exe.write_text("#!/bin/sh\ncase \"$1\" in\n--query-gpu*) echo '0, Fake GPU, 10, 100, 1000, 50, [N/A]';;\n"
                   "--query-compute-apps*) echo '4242, 64';;\nesac\n")
    exe.chmod(0o755)
    c = HardwareCollector(sysfs_root=tmp_path / "none", nvidia_smi=str(exe))
    s = await c.sample()
    assert s["gpus"][0]["name"] == "Fake GPU" and s["gpus"][0]["power_w"] is None


async def test_nvidia_binary_missing_cached(tmp_path):
    c = HardwareCollector(sysfs_root=tmp_path, nvidia_smi=str(tmp_path / "gone"))
    assert (await c.sample())["gpus"] == []
    assert not c.nvidia_present  # FileNotFoundError cached -> never spawned again
