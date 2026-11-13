"""Manual smoke test: one tiny real task through ClaudeCodeAdapter, events printed.

    .venv/bin/python tests/tools/claude_smoke.py [--perm read|edit|full] [--model haiku] [--task TEXT]

Uses a temp project folder with hello.txt. Costs a fraction of a cent with haiku.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from orkestr.runtimes.base import RunSpec  # noqa: E402
from orkestr.runtimes.claude_code import ClaudeCodeAdapter  # noqa: E402


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--perm", default="read", choices=["read", "edit", "full"])
    ap.add_argument("--model", default="haiku")
    ap.add_argument("--task", default="Read hello.txt and tell me the secret word in one short sentence.")
    args = ap.parse_args()

    adapter = ClaudeCodeAdapter()
    info = await adapter.detect()
    print("DETECT:", json.dumps(info.to_dict(), ensure_ascii=False)[:600])
    if not info.available:
        print("Runtime unavailable:", info.error)
        return 2
    with tempfile.TemporaryDirectory(prefix="orkestr-smoke-") as cwd:
        Path(cwd, "hello.txt").write_text("The secret word is PINEAPPLE.\n")
        spec = RunSpec(run_id="smoke", agent={"name": "smoke"}, task=args.task, cwd=cwd, model=args.model,
                       system_prompt="Answer very briefly.", permissions=args.perm, mcp_url=None)
        results = 0
        last = None
        async for ev in adapter.run(spec):
            last = ev
            results += ev.type == "result"
            data = {k: (v[:120] if isinstance(v, str) else v) for k, v in ev.data.items()}
            print(f"{ev.type:11s}", json.dumps(data, ensure_ascii=False)[:300])
        ok = last is not None and last.type == "result" and results == 1 and not last.data.get("is_error")
        print("OK" if ok else f"PROBLEM: last event = {last}")
        return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
