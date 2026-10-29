"""零碎工具：单位、目标解析、消息 id 换算。"""

from __future__ import annotations

import re

# TDLib 把服务端 message_id 存在高位：MessageId(server_id) == server_id << 20
# 见 td/telegram/MessageId.h（SERVER_ID_SHIFT = 20）
SHIFT = 20
_MASK = (1 << SHIFT) - 1

_UNITS = {
    "": 1,
    "B": 1,
    "K": 1 << 10, "KB": 1 << 10, "KIB": 1 << 10,
    "M": 1 << 20, "MB": 1 << 20, "MIB": 1 << 20,
    "G": 1 << 30, "GB": 1 << 30, "GIB": 1 << 30,
}


def parse_size(text: str) -> int:
    """``512K`` / ``1MiB`` / ``2G`` → 字节数。"""
    raw = str(text).strip().upper().replace(" ", "")
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([A-Z]*)", raw)
    if not m:
        raise ValueError(f"看不懂的大小：{text!r}（例：512K、1M、2G）")
    number, unit = m.group(1), m.group(2)
    if unit not in _UNITS:
        raise ValueError(f"不认识的单位：{unit!r}")
    return int(float(number) * _UNITS[unit])


def human_size(num: float) -> str:
    value = float(num)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(value) < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} TiB"


_QUOTES = {"'": "'", '"': '"', "`": "`", "“": "”", "‘": "’"}


def strip_quotes(text: str) -> str:
    """剥掉首尾成对的引号（cmd 不认单引号，会把引号原样传进来）。"""
    out = (text or "").strip()
    while len(out) >= 2 and out[0] in _QUOTES and out[-1] == _QUOTES[out[0]]:
        out = out[1:-1].strip()
    return out


def parse_target(text: str) -> tuple[int, int]:
    """``chat_id:message_id`` → (int, int)。

    message_id 两种写法都收：MTProto 原始 id（6733）或 TDLib 的（6733 << 20）。
    """
    raw = strip_quotes(text)
    if ":" not in raw:
        raise ValueError(f"格式应为 chat_id:message_id，收到 {raw!r}")
    left, _, right = raw.partition(":")
    try:
        chat_id = int(strip_quotes(left))
        message_id = int(strip_quotes(right))
    except ValueError as exc:
        raise ValueError(
            f"chat_id 和 message_id 都必须是整数，收到 {raw!r}"
            f"（chat_id={left.strip()!r}，message_id={right.strip()!r}）"
        ) from exc
    if message_id <= 0:
        raise ValueError(f"message_id 必须是正整数，收到 {message_id}")
    return chat_id, message_id


def is_tdlib_id(message_id: int) -> bool:
    return message_id >= (1 << SHIFT) and (message_id & _MASK) == 0


def to_tdlib_id(message_id: int) -> int:
    return message_id if is_tdlib_id(message_id) else message_id << SHIFT


def to_mtproto_id(message_id: int) -> int:
    return message_id >> SHIFT if is_tdlib_id(message_id) else message_id


def key_tag(sender) -> str:
    """连接用的 auth key 前 8 位，用来在日志里区分连接。"""
    key = getattr(sender, "auth_key", None)
    raw = getattr(key, "key", None) if key is not None else None
    return raw.hex()[:8] if raw else "?"
