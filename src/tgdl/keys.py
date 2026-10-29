"""``data/keys.json``：导出到别的 DC 时拿到的 key 缓存。

主 key 不在这里 —— 它在 Telethon 的 session 文件里。这里只存「这个账号在别的
DC 上的一条授权」，每个 (账号, DC) 最多一条，纯粹是为了省掉每次启动的
``exportAuthorization`` + ``importAuthorization``（约 4 秒，而且不能并发）。

写得很稀疏（一次登录后基本只读），所以用 JSON 而不是 SQLite。

    {"version": 1, "dc_keys": {"1": {"4": "<hex>"}, "2": {"4": "<hex>"}}}
"""

from __future__ import annotations

import json
from pathlib import Path


class KeyCache:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._data: dict[str, dict[str, str]] = {}
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return
        if not isinstance(raw, dict):
            return
        for acc, per in (raw.get("dc_keys") or {}).items():
            if isinstance(per, dict):
                self._data[str(acc)] = {
                    str(dc): str(hexed) for dc, hexed in per.items() if hexed
                }

    def _flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "dc_keys": {acc: per for acc, per in sorted(self._data.items()) if per},
        }
        self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    # ---- 读写 ----
    def get(self, account_id: int, dc_id: int) -> bytes | None:
        hexed = (self._data.get(str(account_id)) or {}).get(str(dc_id))
        return bytes.fromhex(hexed) if hexed else None

    def set(self, account_id: int, dc_id: int, key: bytes) -> None:
        per = self._data.setdefault(str(account_id), {})
        hexed = key.hex()
        if per.get(str(dc_id)) != hexed:
            per[str(dc_id)] = hexed
            self._flush()

    def drop(self, account_id: int, dc_id: int) -> None:
        per = self._data.get(str(account_id)) or {}
        if per.pop(str(dc_id), None) is not None:
            self._flush()

    def drop_account(self, account_id: int) -> None:
        if self._data.pop(str(account_id), None) is not None:
            self._flush()

    def summary(self) -> dict[int, dict[int, int]]:
        """{账号: {DC: 1}}，给 `tgdl accounts` 展示缓存了哪些 DC。"""
        return {
            int(acc): {int(dc): 1 for dc in per}
            for acc, per in self._data.items()
            if per
        }
