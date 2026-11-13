"""Runtime configuration of the Оркестр server."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787
DEFAULT_WEB_DIR = Path(__file__).resolve().parent.parent / "web"


@dataclass
class Config:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    db_path: str = "./orkestr.db"
    demo: bool = False
    web_dir: Path = field(default_factory=lambda: DEFAULT_WEB_DIR)
    log_level: str = "info"

    def __post_init__(self) -> None:
        self.web_dir = Path(self.web_dir)
        if self.db_path != ":memory:":
            self.db_path = os.path.abspath(os.path.expanduser(self.db_path))

    @property
    def base_url(self) -> str:
        """URL other local processes (the CLIs spawned by the engine) use to reach us."""
        host = self.host
        if host in ("0.0.0.0", "", "::", "[::]"):
            host = "127.0.0.1"
        elif ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"http://{host}:{self.port}"
