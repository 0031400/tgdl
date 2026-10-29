"""下载：一个文件，N 个账号同时搬。

模型很简单：**一个账号 = 一条 TCP = 一把 key**，每个账号跑 ``connections`` 个
并发任务共享同一条连接，所有任务从同一个分片队列里抢活。

限流按 auth key 算，所以账号之间互不影响：某个账号撞了 FLOOD_WAIT 只睡它自己，
其它账号继续搬。
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .account import TgdlError, _telethon, check_crypto, make_client
from .config import Config
from .db import ProgressDb
from .keys import KeyStore
from .util import human_size, to_mtproto_id, to_tdlib_id

MAX_CHUNK = 1 << 20
ALIGN = 4096
CONNECT_TIMEOUT = 25
RPC_TIMEOUT = 30


# --------------------------------------------------------------------------- #
# 消息 → 候选文件
# --------------------------------------------------------------------------- #

@dataclass
class Candidate:
    message_id: int          # TDLib 形式，方便打印成 chat:mid
    location: Any
    size: int
    name: str
    dc_id: int | None


async def resolve_entity(client, chat_id: int, username: str | None = None):
    from telethon import utils

    peer = utils.get_peer(chat_id)
    try:
        return await client.get_input_entity(peer)
    except ValueError:
        pass
    async for _ in client.iter_dialogs():     # 触发一次会话列表同步
        pass
    try:
        return await client.get_input_entity(peer)
    except ValueError as exc:
        if username:
            # 公开频道：没订阅的账号只要知道 @username 也能解析出 access_hash
            name = username if username.startswith("@") else "@" + username
            with contextlib.suppress(Exception):
                return await client.get_input_entity(name)
        raise TgdlError(
            f"这个账号找不到 chat_id={chat_id}（不在会话里，也不是公开频道）"
        ) from exc


async def fetch_message(client, chat_id: int, message_id: int,
                        username: str | None = None):
    entity = await resolve_entity(client, chat_id, username)
    message = await client.get_messages(entity, ids=to_mtproto_id(message_id))
    if message is None:
        raise TgdlError(f"找不到消息 {chat_id}:{message_id}（可能已删除）")
    return message


def _candidate_of(message, types) -> Candidate:
    media = getattr(message, "media", None)

    if isinstance(media, types.MessageMediaDocument):
        doc = media.document
        if not isinstance(doc, types.Document):
            raise TgdlError("文档不完整")
        name = next(
            (a.file_name for a in doc.attributes
             if isinstance(a, types.DocumentAttributeFilename)),
            f"doc_{doc.id}",
        )
        loc = types.InputDocumentFileLocation(
            id=doc.id, access_hash=doc.access_hash,
            file_reference=doc.file_reference, thumb_size="",
        )
        return Candidate(to_tdlib_id(message.id), loc, int(doc.size), name,
                         getattr(doc, "dc_id", None))

    if isinstance(media, types.MessageMediaPhoto) and isinstance(media.photo, types.Photo):
        sizes = [s for s in media.photo.sizes
                 if isinstance(s, (types.PhotoSize, types.PhotoSizeProgressive))]
        if not sizes:
            raise TgdlError("照片没有可用尺寸")
        best = max(sizes, key=lambda s: s.size if isinstance(s, types.PhotoSize)
                   else max(s.sizes))
        total = best.size if isinstance(best, types.PhotoSize) else max(best.sizes)
        loc = types.InputPhotoFileLocation(
            id=media.photo.id, access_hash=media.photo.access_hash,
            file_reference=media.photo.file_reference, thumb_size=best.type,
        )
        return Candidate(to_tdlib_id(message.id), loc, int(total),
                         f"photo_{media.photo.id}.jpg",
                         getattr(media.photo, "dc_id", None))

    raise TgdlError("这条消息里没有可下载的媒体")


async def album_candidates(client, chat_id: int, message_id: int,
                           username: str | None = None) -> list[Candidate]:
    """展开相册：一条相册在服务端是多条消息，共享 grouped_id。"""
    _, _, _, types = _telethon()
    message = await fetch_message(client, chat_id, message_id, username)
    group = getattr(message, "grouped_id", None)

    msgs = {message.id: message}
    if group:
        entity = await resolve_entity(client, chat_id, username)
        base = message.id
        ids = [i for i in range(max(1, base - 10), base + 11) if i != base]
        if ids:
            for item in await client.get_messages(entity, ids=ids):
                if item is not None and getattr(item, "grouped_id", None) == group:
                    msgs.setdefault(item.id, item)

    out: list[Candidate] = []
    for mid in sorted(msgs):
        with contextlib.suppress(TgdlError):
            out.append(_candidate_of(msgs[mid], types))
    return out


def pick_candidate(candidates: list[Candidate], pick: int) -> tuple[Candidate, int]:
    if not candidates:
        raise TgdlError("这条消息里没有可下载的媒体")
    if pick:
        if pick < 1 or pick > len(candidates):
            raise TgdlError(f"--pick {pick} 超出范围：只有 {len(candidates)} 个文件")
        return candidates[pick - 1], pick
    best = max(range(len(candidates)), key=lambda i: candidates[i].size)
    return candidates[best], best + 1


# --------------------------------------------------------------------------- #
# 连接
# --------------------------------------------------------------------------- #

async def _open_with_key(client, dc_id: int, key: bytes):
    """用缓存里的 key 直连，不做 DH。"""
    from telethon.client.telegrambaseclient import LAYER
    from telethon.crypto import AuthKey
    from telethon.network import MTProtoSender
    from telethon.tl.functions import InvokeWithLayerRequest

    _, _, functions, _ = _telethon()
    dc = await client._get_dc(dc_id)
    sender = MTProtoSender(AuthKey(key), loggers=client._log)
    await asyncio.wait_for(
        sender.connect(client._connection(dc.ip_address, dc.port, dc_id,
                                          loggers=client._log)),
        CONNECT_TIMEOUT,
    )
    try:
        init = copy.copy(client._init_request)
        init.query = functions.help.GetConfigRequest()
        await asyncio.wait_for(
            sender.send(InvokeWithLayerRequest(LAYER, init)), RPC_TIMEOUT
        )
    except Exception:
        with contextlib.suppress(Exception):
            await sender.disconnect()
        raise
    return sender


async def _close(sender) -> None:
    with contextlib.suppress(Exception):
        await sender.disconnect()


@contextlib.asynccontextmanager
async def _borrowed(client, dc_id: int):
    sender = await client._borrow_exported_sender(dc_id)
    try:
        yield sender
    finally:
        await client._return_exported_sender(sender)


async def ensure_sender(cfg: Config, client, account_id: int, dc_id: int | None, stack):
    """保证这个账号有一条到 ``dc_id`` 的连接。"""
    own = client.session.dc_id
    if not dc_id or dc_id == own:
        return client._sender          # 文件就在自己的 DC 上，直接用主连接

    cache = KeyStore(cfg.keys_path)
    cached = cache.dc_key(account_id, dc_id)
    if cached:
        try:
            sender = await _open_with_key(client, dc_id, cached)
            stack.push_async_callback(_close, sender)
            return sender
        except Exception:  # noqa: BLE001
            cache.drop_dc_key(account_id, dc_id)   # 缓存失效就丢掉，走下面的导出

    # 借一条导出连接（Telethon 内部做 export + import）
    sender = await stack.enter_async_context(_borrowed(client, dc_id))
    key = getattr(getattr(sender, "auth_key", None), "key", None)
    if key:
        cache.set_dc_key(account_id, dc_id, key)   # 下次就不用再导出了
    return sender


# --------------------------------------------------------------------------- #
# 界面
# --------------------------------------------------------------------------- #

class Dashboard:
    """下载过程中每 0.5s 刷一次的看板。"""

    def __init__(self, name: str, size: int, account_ids: list[int]):
        self.name = name
        self.size = size
        self.ids = account_ids
        self.done = 0
        self.speeds = [0.0] * len(account_ids)
        self.moved = [0] * len(account_ids)
        self.started = time.monotonic()
        self._live = None

    def __enter__(self) -> "Dashboard":
        from rich.live import Live

        self.started = time.monotonic()
        self._live = Live(self._render(), refresh_per_second=4, transient=False)
        self._live.__enter__()
        return self

    def __exit__(self, *exc) -> None:
        if self._live is not None:
            self._live.update(self._render(), refresh=True)
            self._live.__exit__(*exc)

    def update(self, done: int, speeds: list[float], moved: list[int]) -> None:
        self.done = done
        self.speeds = list(speeds)
        self.moved = list(moved)
        if self._live is not None:
            self._live.update(self._render(), refresh=True)

    def _render(self):
        from rich.console import Group
        from rich.table import Table
        from rich.text import Text

        pct = (self.done / self.size * 100) if self.size else 0.0
        elapsed = max(time.monotonic() - self.started, 1e-6)
        total_speed = sum(self.speeds)
        avg = self.done / elapsed

        width = 34
        filled = int(width * pct / 100)
        bar = "█" * filled + "░" * (width - filled)

        head = Text()
        head.append(self.name + "\n", style="bold")
        head.append(f"{bar} ", style="green")
        head.append(f"{pct:5.1f}%   ")
        head.append(f"{human_size(self.done)} / {human_size(self.size)}\n")
        head.append(
            f"用时 {_hms(elapsed)}   平均 {avg / 1048576:5.2f} MiB/s   "
            f"当前 {total_speed / 1048576:5.2f} MiB/s"
        )

        table = Table.grid(padding=(0, 2))
        table.add_column(justify="right", width=4)
        table.add_column(justify="right", width=12)
        table.add_column(justify="right", width=12)
        for i, account_id in enumerate(self.ids):
            table.add_row(
                f"#{account_id}",
                f"{self.speeds[i] / 1048576:6.2f} MiB/s",
                f"{human_size(self.moved[i]):>9}",
            )
        return Group(head, Text(""), table)


def _hms(seconds: float) -> str:
    total = int(seconds)
    return f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"


# --------------------------------------------------------------------------- #
# 下载
# --------------------------------------------------------------------------- #

@dataclass
class Result:
    path: Path
    size: int
    seconds: float
    name: str
    resumed: bool = False
    skipped: bool = False          # 文件本来就在，这次没下
    per_account: dict[int, int] = field(default_factory=dict)
    floods: int = 0

    @property
    def speed(self) -> float:
        return self.size / self.seconds if self.seconds > 0 else 0.0


@dataclass
class _Conn:
    account_id: int
    client: Any
    sender: Any
    location: Any
    flood_until: float = 0.0
    moved: int = 0
    window: int = 0
    reqs: int = 0
    floods: int = 0


async def download(
    cfg: Config,
    chat_id: int,
    message_id: int,
    *,
    account_ids: list[int],
    pick: int = 0,
    output: Path | None = None,
    connections: int | None = None,
    chunk: int | None = None,
) -> Result:
    if not check_crypto():
        print("[警告] 没装 cryptg，会退回纯 Python AES，速度只有几百 KB/s")
    _, errors, _, types = _telethon()
    inflight = max(1, connections or cfg.connections)
    chunk_size = max(ALIGN, min(chunk or cfg.chunk, MAX_CHUNK) // ALIGN * ALIGN)

    clients: dict[int, Any] = {}
    try:
        for account_id in account_ids:
            client = make_client(cfg, account_id)
            await client.connect()
            if not await client.is_user_authorized():
                raise TgdlError(
                    f"账号 {account_id} 未登录：`tgdl login --account {account_id}`"
                )
            clients[account_id] = client

        # 同一个 Telegram 账号登了两次（两个会话文件）没有意义：
        # 它们共用一份限流额度，多开的那条只会白白抢请求。留编号最小的。
        if len(clients) > 1:
            mes = await asyncio.gather(
                *(c.get_me() for c in clients.values()), return_exceptions=True
            )
            owner: dict[int, int] = {}
            for account_id, me in zip(list(clients), mes):
                uid = getattr(me, "id", None)
                if uid is None:
                    continue
                if uid in owner:
                    print(f"[{account_id}] 和账号 {owner[uid]} 是同一个 Telegram 账号，"
                          f"跳过它（同一个号开两条连接没有意义）")
                    with contextlib.suppress(Exception):
                        await clients.pop(account_id).disconnect()
                else:
                    owner[uid] = account_id
            account_ids = [i for i in account_ids if i in clients]

        base = clients[account_ids[0]]
        candidates = await album_candidates(base, chat_id, message_id)
        chosen, pick_no = pick_candidate(candidates, pick)
        if len(candidates) > 1:
            print(f"相册 {len(candidates)} 个文件，选第 {pick_no} 个："
                  f"{chosen.name}（{human_size(chosen.size)}）")
        else:
            print(f"{chosen.name}（{human_size(chosen.size)}）")

        # 公开频道：让没订阅的账号也能用 @username 解析
        username = None
        with contextlib.suppress(Exception):
            from telethon import utils as _utils

            ent = await base.get_entity(_utils.get_peer(chat_id))
            username = getattr(ent, "username", None)

        # 每个账号必须自己取一次消息：file_reference 是账号作用域的
        conns: list[_Conn] = []
        for account_id in account_ids:
            client = clients[account_id]
            try:
                own = await fetch_message(client, chat_id, chosen.message_id, username)
                location = _candidate_of(own, types).location
            except Exception as exc:  # noqa: BLE001
                print(f"[{account_id}] 取不到这条消息（{type(exc).__name__}），跳过该账号")
                continue
            conns.append(_Conn(account_id, client, None, location))
        if not conns:
            raise TgdlError("所有账号都取不到这条消息")

        target = Path(output) if output else cfg.download_dir / chosen.name
        if target.is_dir():
            target = target / chosen.name

        # 已经下完的就别再下一遍。
        # 注意不能只看文件大小：我们开工前会按最终大小预分配，中断的下载也留着
        # 一个「大小刚好」的文件。所以还要确认 db 里没有它的未完成记录。
        if target.exists() and target.stat().st_size == chosen.size:
            probe = ProgressDb(cfg.db_path)
            half_done = probe.exists(f"{chat_id}:{chosen.message_id}")
            probe.close()
            if not half_done:
                return Result(path=target, size=chosen.size, seconds=0.0,
                              name=chosen.name, skipped=True)

        target.parent.mkdir(parents=True, exist_ok=True)

        async with contextlib.AsyncExitStack() as stack:
            for conn in conns:
                conn.sender = await ensure_sender(
                    cfg, conn.client, conn.account_id, chosen.dc_id, stack
                )
            print(f"{len(conns)} 个账号 × 每条 {inflight} 个在途 → "
                  f"{len(conns) * inflight} 个并发请求")
            return await _run(cfg, target, chosen, conns, inflight, chunk_size,
                              chat_id=chat_id, message_id=chosen.message_id)
    finally:
        for client in clients.values():
            with contextlib.suppress(Exception):
                await client.disconnect()


async def _run(
    cfg: Config,
    target: Path,
    chosen: Candidate,
    conns: list[_Conn],
    inflight: int,
    chunk: int,
    *,
    chat_id: int,
    message_id: int,
) -> Result:
    _, errors, _, _ = _telethon()
    size = chosen.size
    total_chunks = (size + chunk - 1) // chunk
    nbytes = (total_chunks + 7) // 8

    db = ProgressDb(cfg.db_path)
    resume_key = f"{chat_id}:{message_id}"
    saved = db.load(resume_key, path=target, size=size, chunk=chunk)
    bitmap = int.from_bytes(saved, "little") if saved else 0
    resumed = saved is not None

    if resumed and target.exists():
        with target.open("r+b") as fp:
            fp.truncate(size)
    else:
        bitmap = 0
        with target.open("wb") as fp:
            fp.truncate(size)          # 预分配，避免边下边扩

    missing = [i for i in range(total_chunks) if not (bitmap >> i) & 1]
    done = sum(min(chunk, size - i * chunk)
               for i in range(total_chunks) if (bitmap >> i) & 1)
    if resumed:
        print(f"接着上次下：已完成 {human_size(done)} / {human_size(size)}"
              f"（{len(missing)} 片待下）")

    queue: asyncio.Queue[int] = asyncio.Queue()
    for index in missing:
        queue.put_nowait(index)

    state = {"done": done, "bitmap": bitmap}
    lock = asyncio.Lock()
    tick_last = [time.monotonic()]
    started = time.monotonic()

    async def fetch(conn: _Conn, offset: int) -> bytes:
        last: Exception | None = None
        for attempt in range(6):
            gap = conn.flood_until - time.monotonic()
            if gap > 0:
                await asyncio.sleep(gap)
            try:
                return await _get_file(conn, offset, chunk)
            except errors.FloodWaitError as exc:
                wait = int(getattr(exc, "seconds", 1)) + 1
                conn.flood_until = max(conn.flood_until, time.monotonic() + wait)
                conn.floods += 1
                print(f"[{conn.account_id}] FLOOD_WAIT {wait}s，只停这个账号"
                      f"（累计 {conn.floods} 次）")
                last = exc
            except errors.FileReferenceExpiredError as exc:
                KeyStore(cfg.keys_path).drop_dc_key(conn.account_id, chosen.dc_id or 0)
                raise TgdlError("file_reference 失效，重跑一次即可") from exc
            except (errors.RPCError, OSError) as exc:
                last = exc
                await asyncio.sleep(min(2 ** attempt * 0.5, 8))
        raise TgdlError(f"offset {offset} 重试 6 次仍失败：{last}")

    async def worker(conn: _Conn) -> None:
        handle = target.open("r+b", buffering=0)
        try:
            while True:
                try:
                    index = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                data = await fetch(conn, index * chunk)
                if not data:
                    continue
                handle.seek(index * chunk)
                view = memoryview(data)
                while view:
                    written = handle.write(view)
                    if not written:
                        raise TgdlError(f"写盘失败 offset={index * chunk}")
                    view = view[written:]
                async with lock:
                    state["done"] += len(data)
                    state["bitmap"] |= 1 << index
                    conn.moved += len(data)
                    conn.window += len(data)
                    conn.reqs += 1
        finally:
            handle.close()

    def snapshot() -> tuple[int, list[float], list[int]]:
        now = time.monotonic()
        dt = max(now - tick_last[0], 1e-6)
        tick_last[0] = now
        speeds = []
        for conn in conns:
            speeds.append(conn.window / dt)
            conn.window = 0
        return state["done"], speeds, [c.moved for c in conns]

    def flush() -> None:
        db.save(resume_key, path=target, size=size, chunk=chunk,
                done=state["bitmap"].to_bytes(nbytes, "little"))

    async def ticker(dash: Dashboard) -> None:
        last_flush = time.monotonic()
        while True:
            await asyncio.sleep(0.5)
            dash.update(*snapshot())
            if time.monotonic() - last_flush >= 2.0:
                flush()
                last_flush = time.monotonic()

    tasks = [asyncio.create_task(worker(c)) for c in conns for _ in range(inflight)]
    with Dashboard(chosen.name, size, [c.account_id for c in conns]) as dash:
        tick = asyncio.create_task(ticker(dash))
        try:
            done_tasks, pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_EXCEPTION
            )
            for task in done_tasks:
                if task.exception() is not None:
                    for rest in pending:
                        rest.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)
                    raise task.exception()  # type: ignore[misc]
            await asyncio.gather(*pending, return_exceptions=True)
        finally:
            tick.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await tick
            dash.update(*snapshot())

    elapsed = time.monotonic() - started
    if state["done"] >= size:
        db.clear(resume_key)
    else:
        flush()          # 中途出错也把进度存下来
    db.close()

    _report(conns, state["done"], elapsed)
    return Result(
        path=target, size=state["done"], seconds=elapsed, name=chosen.name,
        resumed=resumed,
        per_account={c.account_id: c.moved for c in conns},
        floods=sum(c.floods for c in conns),
    )


async def _get_file(conn: _Conn, offset: int, limit: int) -> bytes:
    _, _, functions, types = _telethon()
    request = functions.upload.GetFileRequest(
        location=conn.location, offset=offset, limit=limit, cdn_supported=True
    )
    result = await conn.client._call(conn.sender, request, flood_sleep_threshold=0)
    if isinstance(result, types.upload.FileCdnRedirect):
        raise TgdlError("服务端要求走 CDN（暂不支持）")
    return result.bytes


def _report(conns: list[_Conn], total: int, elapsed: float) -> None:
    print(f"\n每个账号搬了多少（共 {human_size(total)} / {elapsed:.2f}s）")
    for conn in conns:
        share = conn.moved / total * 100 if total else 0
        speed = conn.moved / elapsed / 1048576 if elapsed else 0
        extra = f"  flood {conn.floods} 次" if conn.floods else ""
        print(
            f"  #{conn.account_id}  {conn.moved / 1048576:8.1f} MB  "
            f"{share:5.1f}%  {speed:6.2f} MB/s  {conn.reqs:4d} 请求{extra}"
        )
