"""配置：只放 api 凭据和下载默认值。账号不在这里，由会话文件决定。

配置查找顺序：``$TGDL_CONFIG`` → ``./config.toml`` → ``~/.tgdl/config.toml``。
所有运行时数据都在 ``[data] dir`` 里，默认是配置文件旁边的 ``data/``。
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .util import parse_size

DEFAULT_CHUNK = "1MiB"
DEFAULT_CONNECTIONS = 4


class ConfigError(RuntimeError):
    pass


def find_config(explicit: Path | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    env = os.environ.get("TGDL_CONFIG")
    if env:
        return Path(env).expanduser()
    local = Path.cwd() / "config.toml"
    if local.exists():
        return local
    return Path.home() / ".tgdl" / "config.toml"


@dataclass
class Config:
    path: Path
    api_id: int
    api_hash: str
    download_dir: Path
    chunk: int = parse_size(DEFAULT_CHUNK)
    connections: int = DEFAULT_CONNECTIONS
    data_dir: Path = field(default=Path("data"))

    # ---- 数据目录 ----
    @property
    def db_path(self) -> Path:
        return self.data_dir / "tgdl.db"

    @property
    def keys_path(self) -> Path:
        return self.data_dir / "keys.json"

    @property
    def sessions_dir(self) -> Path:
        return self.data_dir / "sessions"

    def session_path(self, account_id: int) -> Path:
        return self.sessions_dir / f"{account_id}.session"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.sessions_dir.mkdir(parents=True, exist_ok=True)


def load_config(explicit: Path | None = None) -> Config:
    path = find_config(explicit)
    if not path.exists():
        raise ConfigError(
            f"找不到配置文件 {path}\n"
            "在项目目录放一份 config.toml，填上 api_id / api_hash 即可。"
        )
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} 语法有问题：{exc}") from exc

    tg = data.get("telegram") or {}
    dl = data.get("download") or {}
    dt = data.get("data") or {}
    try:
        api_id = int(tg.get("api_id") or 0)
    except (TypeError, ValueError) as exc:
        raise ConfigError("telegram.api_id 必须是整数") from exc
    api_hash = str(tg.get("api_hash") or "").strip()
    if not api_id or not api_hash:
        raise ConfigError(f"{path} 里的 telegram.api_id / api_hash 还没填")

    base = path.parent

    def _abs(value: str) -> Path:
        p = Path(value).expanduser()
        return p if p.is_absolute() else (base / p).resolve()

    return Config(
        path=path,
        api_id=api_id,
        api_hash=api_hash,
        download_dir=_abs(str(dl.get("dir") or ".")),
        chunk=parse_size(str(dl.get("chunk") or DEFAULT_CHUNK)),
        connections=max(1, int(dl.get("connections") or DEFAULT_CONNECTIONS)),
        data_dir=_abs(str(dt.get("dir") or "data")),
    )
