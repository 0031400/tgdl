"""命令行入口。

    tgdl login --account 1,2          # 批量登录，逐个扫码或输验证码
    tgdl accounts                     # 编号 / 姓名 / 用户名 / 主 DC（联网确认）
    tgdl info   -100123:45            # 看这条消息里有什么文件
    tgdl download -100123:45 --accounts all
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path
from typing import Optional

import typer
from rich import box
from rich.console import Console
from rich.table import Table

from . import __version__
from .account import (
    TgdlError,
    apply_credentials,
    describe,
    list_accounts,
    login as do_login,
    logout as do_logout,
    make_client,
)
from .config import Config, ConfigError, load_config
from .download import album_candidates, download as do_download
from .keys import KeyStore
from .util import human_size, parse_size, parse_target

app = typer.Typer(add_completion=False, no_args_is_help=True,
                  help="多账号并行下载 Telegram 文件。")
console = Console()
err = Console(stderr=True)


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #

_TARGET_ARG = re.compile(r"""^['"`]?-\d+:\d+['"`]?$""")


def _rescue_negative_target(argv: list[str]) -> None:
    """click 会把 ``-100123:45`` 当选项。目标都是唯一位置参数，挪到末尾补个 ``--``。"""
    if "--" in argv:
        return
    for i in range(1, len(argv)):
        if _TARGET_ARG.match(argv[i]):
            argv.append("--")
            argv.append(argv.pop(i))
            return


def _load() -> Config:
    try:
        return load_config()
    except ConfigError as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc


def _run(coro):
    try:
        return asyncio.run(coro)
    except TgdlError as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    except KeyboardInterrupt:
        err.print("[yellow]已中断[/yellow]")
        raise typer.Exit(130) from None


def _parse_accounts(cfg: Config, spec: str) -> list[int]:
    """``all`` 或 ``1,2,3`` → 编号列表。"""
    known = list_accounts(cfg)
    raw = (spec or "all").strip()
    if raw.lower() == "all":
        if not known:
            raise typer.BadParameter("还没有任何账号，先 `tgdl login --account 1`")
        return known
    ids: list[int] = []
    for piece in raw.split(","):
        piece = piece.strip()
        if not piece:
            continue
        try:
            ids.append(int(piece))
        except ValueError:
            raise typer.BadParameter(f"账号编号必须是数字：{piece!r}") from None
    if not ids:
        raise typer.BadParameter("没给账号编号")
    return ids


# --------------------------------------------------------------------------- #
# 命令
# --------------------------------------------------------------------------- #

@app.command()
def login(
    account: str = typer.Option("1", "--account", "-a", help="编号，可用逗号：1,2,3"),
    mode: str = typer.Option("phone", "--mode", "-m", help="phone（默认）或 qr"),
) -> None:
    """登录一个或多个账号（每个编号一个会话）。"""
    cfg = _load()
    if mode not in ("qr", "phone"):
        raise typer.BadParameter("--mode 只能是 phone 或 qr")
    ids = []
    for piece in account.split(","):
        piece = piece.strip()
        if not piece:
            continue
        try:
            ids.append(int(piece))
        except ValueError:
            raise typer.BadParameter(f"账号编号必须是数字：{piece!r}") from None
    if not ids:
        raise typer.BadParameter("至少给一个编号")
    for line in _run(do_login(cfg, ids, mode=mode)):
        console.print(f"[green]{line}[/green]")


@app.command()
def logout(
    account: str = typer.Option("all", "--account", "-a", help="编号或 all"),
    yes: bool = typer.Option(False, "--yes", "-y", help="不确认"),
) -> None:
    """退出账号（服务端撤销 + 删本地会话）。"""
    cfg = _load()
    ids = _parse_accounts(cfg, account)
    if not yes and not typer.confirm(f"确定退出账号 {', '.join(map(str, ids))}？"):
        raise typer.Exit(0)
    for line in _run(do_logout(cfg, ids)):
        console.print(f"[green]{line}[/green]")


@app.command()
def accounts() -> None:
    """列出账号（先按 keys.json 刷新会话，再联网确认）。"""
    cfg = _load()
    # keys.json 是凭据清单：手改过就先灌进 session 文件，再联网确认
    for note in apply_credentials(cfg, force=True):
        console.print(f"[green]{note}[/green]")

    ids = list_accounts(cfg)
    if not ids:
        console.print("[yellow]还没有账号。`tgdl login --account 1`[/yellow]")
        return

    table = Table(box=box.SIMPLE, header_style="bold")
    table.add_column("编号", justify="right")
    table.add_column("姓名")
    table.add_column("用户名")
    table.add_column("主 DC", justify="center")
    table.add_column("DC key 缓存")

    cache = KeyStore(cfg.keys_path).summary()
    seen: dict[int, int] = {}
    dupes: list[str] = []
    for account_id in ids:
        info = _run(describe(cfg, account_id))
        cached = cache.get(account_id) or {}
        cached_text = "，".join(f"DC{dc}" for dc in sorted(cached)) or "[dim]—[/dim]"
        if info.get("ok"):
            table.add_row(
                str(account_id), info["name"] or "[dim]—[/dim]",
                info["username"] or "[dim]—[/dim]",
                f"DC{info['dc']}" if info.get("dc") else "—",
                cached_text,
            )
            uid = info.get("uid")
            if uid:
                if uid in seen:
                    dupes.append(f"#{account_id} 和 #{seen[uid]} 是同一个账号")
                else:
                    seen[uid] = account_id
        else:
            table.add_row(str(account_id), "[red]未登录[/red]", "—", "—", cached_text)
    console.print(table)
    if dupes:
        console.print(
            "[yellow]注意：" + "；".join(dupes)
            + "。同时用会共用同一份限流额度，等于白开一条连接。"
            + "不要的话 `tgdl logout --account <编号>`。[/yellow]"
        )


@app.command()
def info(
    target: str = typer.Argument(..., help="chat_id:message_id"),
    account: Optional[int] = typer.Option(
        None, "--account", "-a", help="只用这个编号查；不填就逐个试，谁先查到用谁"
    ),
) -> None:
    """列出这条消息里的所有文件（不下载）。"""
    cfg = _load()
    try:
        chat_id, message_id = parse_target(target)
    except ValueError as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc

    ids = [account] if account else list_accounts(cfg)
    if not ids:
        raise typer.BadParameter("还没有账号，先 `tgdl login --account 1`")

    candidates, used = _run(_info(cfg, ids, chat_id, message_id))
    if account is None and used != ids[0]:
        console.print(f"[dim]（编号 {used} 查到的）[/dim]")

    table = Table(box=box.SIMPLE, header_style="bold")
    table.add_column("#", justify="right")
    table.add_column("文件名")
    table.add_column("大小", justify="right")
    table.add_column("DC", justify="center")
    for i, cand in enumerate(candidates, 1):
        table.add_row(str(i), cand.name, human_size(cand.size), str(cand.dc_id))
    console.print(table)


async def _info(cfg: Config, ids: list[int], chat_id: int, message_id: int):
    """按编号顺序试，返回第一个能查到这条消息的账号。"""
    last: Exception | None = None
    for account_id in ids:
        client = make_client(cfg, account_id)
        await client.connect()
        try:
            if not await client.is_user_authorized():
                print(f"[{account_id}] 未登录，换下一个")
                last = TgdlError(f"账号 {account_id} 未登录")
                continue
            try:
                candidates = await album_candidates(client, chat_id, message_id)
            except TgdlError as exc:
                print(f"[{account_id}] {exc}，换下一个")
                last = exc
                continue
            if not candidates:
                print(f"[{account_id}] 这条消息里没有可下载的媒体，换下一个")
                last = TgdlError("这条消息里没有可下载的媒体")
                continue
            return candidates, account_id
        finally:
            await client.disconnect()
    raise last or TgdlError("没有可用账号")


@app.command()
def download(
    target: str = typer.Argument(..., help="chat_id:message_id"),
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="文件或目录"),
    account: str = typer.Option("all", "--accounts", "-a", help="all 或 1,2,3"),
    pick: int = typer.Option(0, "--pick", help="相册里选第几个（1 起），0=最大"),
    connections: Optional[int] = typer.Option(
        None, "--connections", "-c", help="每个账号的在途请求数"
    ),
    chunk: Optional[str] = typer.Option(None, "--chunk", help="分片大小，最大 1MiB"),
) -> None:
    """下载 chat_id:message_id 里的文件。"""
    cfg = _load()
    try:
        chat_id, message_id = parse_target(target)
    except ValueError as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc
    ids = _parse_accounts(cfg, account)

    result = _run(do_download(
        cfg, chat_id, message_id, account_ids=ids, pick=pick, output=output,
        connections=connections,
        chunk=parse_size(chunk) if chunk else None,
    ))
    if result.skipped:
        console.print(f"[yellow]已经下过了，跳过：{result.path}[/yellow]")
        return
    console.print(
        f"\n完成 [green]{result.path}[/green]  {human_size(result.size)}  "
        f"耗时 {result.seconds:.2f}s  平均 {result.speed / 1048576:.2f} MiB/s"
        + ("（续传）" if result.resumed else "")
    )


@app.command()
def version() -> None:
    """显示版本。"""
    console.print(f"tgdl {__version__}")


def main() -> None:
    _rescue_negative_target(sys.argv)
    app()


if __name__ == "__main__":
    main()
