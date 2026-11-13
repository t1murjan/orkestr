"""`python -m orkestr [--host] [--port] [--db] [--demo]`."""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from .config import DEFAULT_HOST, DEFAULT_PORT, Config


def parse_args(argv: list[str] | None = None) -> Config:
    p = argparse.ArgumentParser(prog="orkestr", description="Оркестр: пульт управления ИИ-агентами")
    p.add_argument("--host", default=DEFAULT_HOST, help=f"адрес (по умолчанию {DEFAULT_HOST})")
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"порт (по умолчанию {DEFAULT_PORT})")
    p.add_argument("--db", default="./orkestr.db", help="файл базы данных (по умолчанию ./orkestr.db)")
    p.add_argument("--demo", action="store_true", help="создать демо-команду и включить демо-запуски")
    p.add_argument("--log-level", default="info", choices=["debug", "info", "warning", "error"])
    a = p.parse_args(argv)
    return Config(host=a.host, port=a.port, db_path=a.db, demo=a.demo, log_level=a.log_level)


def main(argv: list[str] | None = None) -> int:
    import uvicorn

    from .app import create_app

    cfg = parse_args(argv)
    logging.basicConfig(level=cfg.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    app = create_app(cfg)

    class _Server(uvicorn.Server):
        def handle_exit(self, sig: int, frame: object) -> None:
            # End SSE streams first, otherwise graceful shutdown waits for them.
            end = getattr(app.state, "begin_shutdown", None)
            if end is not None:
                try:
                    asyncio.get_running_loop().call_soon(end)
                except RuntimeError:
                    pass
            super().handle_exit(sig, frame)

    config = uvicorn.Config(app, host=cfg.host, port=cfg.port, log_level=cfg.log_level,
                            access_log=False, timeout_graceful_shutdown=3)
    server = _Server(config)
    if cfg.host not in ("127.0.0.1", "localhost", "::1"):
        print("Внимание: сервер доступен не только локально, а авторизации в нём нет.", file=sys.stderr)
    print(f"Оркестр: http://{cfg.host if cfg.host not in ('0.0.0.0', '') else '127.0.0.1'}:{cfg.port}", flush=True)
    try:
        server.run()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
