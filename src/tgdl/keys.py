"""``data/keys.json``：账号凭据清单，可以手改。

里面放**主 key**（登录拿到的）和**各 DC 的导出 key**。``tgdl accounts`` 会把它
灌进 session 文件，所以「改这个文件 + 跑一次 accounts」= 换了一台机器/手动加了个账号。

    {
      "version": 2,
      "accounts": {
        "1": {"dc": 5, "ip": "91.108.56.196", "port": 443,
              "key": "<hex>",                     ← 主 key，手改这里
              "dc_keys": {"4": "<hex>"}}          ← 导出缓存，删了只是慢 4 秒
      }
    }

主 key 是裸的，和密码等价，别外传。
"""

from __future__ import annotations

import json
from pathlib import Path

VERSION = 2


class KeyStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._accounts: dict[str, dict] = {}
        self._load()

    # ---------- 读写 ----------
    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return
        if not isinstance(raw, dict):
            return

        accounts = raw.get("accounts")
        if isinstance(accounts, dict):          # v2
            for acc, rec in accounts.items():
                if isinstance(rec, dict):
                    self._accounts[str(acc)] = {
                        "dc": rec.get("dc"),
                        "ip": rec.get("ip"),
                        "port": rec.get("port"),
                        "key": rec.get("key"),
                        "dc_keys": {
                            str(dc): str(hexed)
                            for dc, hexed in (rec.get("dc_keys") or {}).items()
                            if hexed
                        },
                    }
        elif isinstance(raw.get("dc_keys"), dict):   # v1：只有导出缓存
            for acc, per in raw["dc_keys"].items():
                if isinstance(per, dict):
                    self._accounts[str(acc)] = {
                        "dc": None, "ip": None, "port": None, "key": None,
                        "dc_keys": {str(dc): str(h) for dc, h in per.items() if h},
                    }

    def _flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": VERSION,
            "accounts": {
                acc: {
                    "dc": rec["dc"],
                    "ip": rec["ip"],
                    "port": rec["port"],
                    "key": rec["key"],
                    "dc_keys": {dc: h for dc, h in rec["dc_keys"].items() if h},
                }
                for acc, rec in sorted(self._accounts.items(), key=lambda kv: int(kv[0]))
            },
        }
        self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def _rec(self, account_id: int) -> dict:
        return self._accounts.setdefault(
            str(account_id),
            {"dc": None, "ip": None, "port": None, "key": None, "dc_keys": {}},
        )

    # ---------- 主 key ----------
    def main(self, account_id: int) -> dict | None:
        """返回 ``{"dc", "ip", "port", "key"}``，缺主 key 就是 None。"""
        rec = self._accounts.get(str(account_id)) or {}
        if not rec.get("key") or not rec.get("dc") or not rec.get("ip"):
            return None
        return {
            "dc": int(rec["dc"]),
            "ip": str(rec["ip"]),
            "port": int(rec.get("port") or 443),
            "key": str(rec["key"]),
        }

    def set_main(self, account_id: int, dc_id: int, ip: str, port: int,
                 key: bytes) -> None:
        rec = self._rec(account_id)
        rec.update({"dc": int(dc_id), "ip": str(ip), "port": int(port),
                    "key": key.hex()})
        self._flush()

    def drop_account(self, account_id: int) -> None:
        if self._accounts.pop(str(account_id), None) is not None:
            self._flush()

    # ---------- 导出到别的 DC 的 key ----------
    def dc_key(self, account_id: int, dc_id: int) -> bytes | None:
        hexed = ((self._accounts.get(str(account_id)) or {})
                 .get("dc_keys", {}).get(str(dc_id)))
        return bytes.fromhex(hexed) if hexed else None

    def set_dc_key(self, account_id: int, dc_id: int, key: bytes) -> None:
        per = self._rec(account_id)["dc_keys"]
        hexed = key.hex()
        if per.get(str(dc_id)) != hexed:
            per[str(dc_id)] = hexed
            self._flush()

    def drop_dc_key(self, account_id: int, dc_id: int) -> None:
        per = (self._accounts.get(str(account_id)) or {}).get("dc_keys", {})
        if per.pop(str(dc_id), None) is not None:
            self._flush()

    # ---------- 汇总 ----------
    def accounts(self) -> list[int]:
        return sorted(int(a) for a in self._accounts)

    def with_main_key(self) -> list[int]:
        return [a for a in self.accounts() if self.main(a) is not None]

    def summary(self) -> dict[int, dict[int, int]]:
        """{账号: {DC: 1}}，给 `tgdl accounts` 展示缓存了哪些 DC。"""
        return {
            int(acc): {int(dc): 1 for dc in rec["dc_keys"]}
            for acc, rec in self._accounts.items()
            if rec["dc_keys"]
        }
