"""账号：一个编号 = 一个 Telethon 会话文件 = 一条连接。

编号就是 ``data/sessions/<编号>.session`` 的文件名，`tgdl login --account 3`
会新建它，`tgdl accounts` 扫目录就知道有哪些账号。没有额外的账号表。

session 文件由 Telethon 自己管（里面存主 DC 和 auth key），我们只在需要连到
**别的 DC** 时，把导出的 key 缓存进 ``data/keys.json``。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

from .config import Config
from .keys import KeyCache
from .qr import render as render_qr


class TgdlError(RuntimeError):
    pass


def _telethon():
    try:
        from telethon import TelegramClient, errors, functions, types
    except ImportError as exc:  # pragma: no cover
        raise TgdlError("没装 telethon：uv sync") from exc
    return TelegramClient, errors, functions, types


def check_crypto() -> bool:
    """有没有 cryptg。没有会退回纯 Python AES，速度掉到几百 KB/s。"""
    try:
        import cryptg  # noqa: F401
    except ImportError:
        return False
    return True


# --------------------------------------------------------------------------- #
# 会话文件
# --------------------------------------------------------------------------- #

def list_accounts(cfg: Config) -> list[int]:
    """扫 ``data/sessions/*.session`` 得到编号列表。"""
    if not cfg.sessions_dir.exists():
        return []
    out = []
    for path in cfg.sessions_dir.glob("*.session"):
        try:
            out.append(int(path.stem))
        except ValueError:
            continue
    return sorted(out)


def remove_session(cfg: Config, account_id: int) -> None:
    path = cfg.session_path(account_id)
    for candidate in (path, path.with_suffix(".session-journal")):
        with contextlib.suppress(FileNotFoundError):
            candidate.unlink()
    KeyCache(cfg.keys_path).drop_account(account_id)


def make_client(cfg: Config, account_id: int):
    """按编号建客户端。session 由 Telethon 管理，key 就存在里面。"""
    TelegramClient, _, _, _ = _telethon()
    from telethon.sessions import SQLiteSession

    logging.getLogger("telethon").setLevel(logging.WARNING)
    cfg.ensure_dirs()
    session = SQLiteSession(str(cfg.session_path(account_id)))
    client = TelegramClient(session, cfg.api_id, cfg.api_hash, device_model="tgdl")
    # FLOOD_WAIT 自己处理：Telethon 的全局等待会把所有账号一起卡住
    client.flood_sleep_threshold = 0
    return client


def _label(me) -> str:
    name = me.first_name or ""
    username = f"@{me.username}" if me.username else f"id={me.id}"
    return f"{name} {username}".strip()


# --------------------------------------------------------------------------- #
# 登录
# --------------------------------------------------------------------------- #

class _Passwords:
    """两步验证密码：第一次问，后面同一次运行里的账号复用这个答案。"""

    def __init__(self) -> None:
        self.value: str | None = None

    def ask(self, account_id: int) -> str:
        if self.value is not None:
            print(f"[{account_id}] 沿用刚才的两步验证密码")
            return self.value
        self.value = input(f"[{account_id}] 两步验证密码: ")
        return self.value


async def _login_phone(client, account_id: int, passwords: _Passwords) -> None:
    _, errors, _, _ = _telethon()
    number = input(
        f"[{account_id}] 手机号（含国家码，如 +8613800138000）: "
    ).strip()

    sent = None
    for _ in range(4):
        try:
            sent = await client.send_code_request(number)
            break
        except errors.FloodWaitError as exc:
            wait = int(getattr(exc, "seconds", 3)) + 1
            print(f"[{account_id}] 发码被限流，等 {wait}s 再试")
            await asyncio.sleep(wait)
    if sent is None:
        raise TgdlError(f"[{account_id}] 发码失败（连续被限流）")

    for _ in range(5):
        code = input(f"[{account_id}] 验证码: ").strip()
        try:
            await client.sign_in(number, code=code,
                                 phone_code_hash=sent.phone_code_hash)
            return
        except errors.SessionPasswordNeededError:
            await client.sign_in(password=passwords.ask(account_id))
            return
        except errors.PhoneCodeInvalidError:
            print(f"[{account_id}] 验证码不对，再来一次")
    raise TgdlError(f"[{account_id}] 验证码连续错误，放弃")


async def _login_qr(client, account_id: int, passwords: _Passwords) -> None:
    _, errors, _, _ = _telethon()
    qr = await client.qr_login()
    while True:
        left = max(0, int(qr.expires.timestamp() - time.time()))
        print(f"\n[{account_id}] 用手机 Telegram 扫码登录（{left}s 内有效）：\n")
        print(render_qr(qr.url))
        print()
        try:
            await qr.wait(timeout=left or 30)
            return
        except asyncio.TimeoutError:
            print(f"[{account_id}] 二维码过期，换一张")
            await qr.recreate()
        except errors.SessionPasswordNeededError:
            await client.sign_in(password=passwords.ask(account_id))
            return


async def login(
    cfg: Config,
    account_ids: list[int],
    *,
    mode: str = "phone",
) -> list[str]:
    """批量登录。``mode`` 默认手机号，``qr`` 则逐个扫码。"""
    lines: list[str] = []
    passwords = _Passwords()
    for account_id in account_ids:
        client = make_client(cfg, account_id)
        await client.connect()
        try:
            if await client.is_user_authorized():
                me = await client.get_me()
                raise TgdlError(
                    f"账号 {account_id} 已经登录过（{_label(me)}），拒绝重复登录。"
                    f"要重登先 `tgdl logout --account {account_id}`"
                )

            if mode == "qr":
                await _login_qr(client, account_id, passwords)
            else:
                await _login_phone(client, account_id, passwords)

            me = await client.get_me()
            lines.append(f"[{account_id}] 登录成功：{_label(me)}")
        finally:
            await client.disconnect()
    return lines


# --------------------------------------------------------------------------- #
# 登出 / 查询
# --------------------------------------------------------------------------- #

async def logout(cfg: Config, account_ids: list[int]) -> list[str]:
    lines = []
    for account_id in account_ids:
        client = make_client(cfg, account_id)
        await client.connect()
        try:
            if await client.is_user_authorized():
                await client.log_out()
                lines.append(f"[{account_id}] 已退出")
            else:
                lines.append(f"[{account_id}] 本来就没登录")
        finally:
            await client.disconnect()
        remove_session(cfg, account_id)
    return lines


async def describe(cfg: Config, account_id: int) -> dict:
    """联网确认一个账号：姓名、用户名、主 DC。"""
    client = make_client(cfg, account_id)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            return {"id": account_id, "ok": False, "error": "未登录"}
        me = await client.get_me()
        return {
            "id": account_id,
            "ok": True,
            "uid": me.id,
            "name": me.first_name or "",
            "username": f"@{me.username}" if me.username else "",
            "dc": client.session.dc_id,
        }
    finally:
        await client.disconnect()
