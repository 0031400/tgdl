# tgdl

多账号并行下载 Telegram 文件。只做一件事：给一个 `chat_id:message_id`，用你配置的
所有账号同时把这个文件搬下来。

```
tgdl download -1001234567890:42
```

## 为什么是多账号

实测下来，速度基本跟**账号数**成正比。同一个文件、每个账号 4 个在途请求：

```
3 个账号 × 4 个在途 = 12 个并发请求  →  13~14 MiB/s
```

三个账号分别贡献 4.25 / 4.98 / 4.39 MiB/s，搬运量基本均分（31% / 37% / 32%）。
只用一个账号时会明显慢一截。

## 安装

```bash
uv sync
```

依赖里 `cryptg` 是必须的。缺了会退回纯 Python AES，速度被锁死在几百 KB/s。

## 配置

项目目录下的 `config.toml`：

```toml
[telegram]
api_id = 0          # 从 my.telegram.org 申请
api_hash = ""

[download]
dir = "."           # 下载目录，相对路径按本文件所在目录算
chunk = "1MiB"      # 分片大小，最大 1MiB
connections = 4     # 每个账号的在途请求数

[data]
dir = "data"        # 所有运行时数据
```

配置文件查找顺序：`$TGDL_CONFIG` → `./config.toml` → `~/.tgdl/config.toml`。

## 数据目录

```
data/
├── tgdl.db            # 断点续传位图（SQLite）
├── keys.json          # 凭据清单（主 key + 各 DC 导出 key）
└── sessions/
    ├── 1.session      # Telethon 会话，编号就是文件名
    └── 2.session
```

**账号编号 = session 文件名**。没有单独的账号表，`tgdl accounts` 扫目录就知道有哪些号。

## 命令

### 登录

```bash
tgdl login --account 1          # 手机号 + 验证码
tgdl login --account 1,2,3      # 批量，两步验证密码复用第一次输入
tgdl login --account 1 -m qr    # 扫码（二维码直接画在终端里）
```

已经登录过的账号会被**拒绝**，不会重复登录。要重登先 `tgdl logout`。

### 登出

```bash
tgdl logout --account 2
tgdl logout --account all
```

服务端撤销授权 + 删本地 session + 删 `keys.json` 条目。

### 看账号

```bash
tgdl accounts
```

```
  编号   姓名     用户名      主 DC   DC key 缓存
     1   Alice    @alice      DC5    DC4
     2   Bob      @bob        DC5    DC4
     3   Carol    —           DC1    DC4，DC5
```

这个命令会做三件事：先把 `keys.json` 灌进 session 文件，再联网确认每个账号，
最后**发现同一个账号登了两次就自动删掉编号大的那个**（只删本地，不动服务端授权）。

「主 DC」是账号自己的 DC；「DC key 缓存」是它在哪些别的 DC 上已经导出过授权。
文件不在主 DC 上时，必须先导出一次（约 4 秒），导出结果缓存在 `keys.json` 里。

### 看消息里有什么

```bash
tgdl info -1001234567890:42
```

```
  #   文件名            大小        DC
  1   cover.jpg         30.3 KiB    5
  2   example.mp4       198.7 MiB   4
```

一条相册在服务端是多条消息（封面图 + 正片），这里会全部列出来。
默认按编号顺序逐个账号试，谁先查到用谁；`--account N` 强制指定。

### 下载

```bash
tgdl download -1001234567890:42
tgdl download -1001234567890:42 --pick 1      # 相册里选第 1 个（封面图）
tgdl download -1001234567890:42 -a 1,3        # 只用 1 号和 3 号
tgdl download -1001234567890:42 -c 8          # 每个账号 8 个在途
tgdl download -1001234567890:42 -o D:/x.mp4   # 指定输出
```

| 参数 | 说明 | 默认 |
|---|---|---|
| `-o/--output` | 文件或目录 | 配置里的 `dir` |
| `-a/--accounts` | `all` 或 `1,2,3` | `all` |
| `--pick` | 相册里选第几个（1 起） | `0` = 最大的那个 |
| `-c/--connections` | 每个账号的在途请求数 | 配置里的 `4` |
| `--chunk` | 分片大小，最大 1MiB | 配置里的 `1MiB` |

下载过程中每 0.5 秒刷新一次：

```
example.mp4
██████████████████████████████████ 100.0%   198.7 MiB / 198.7 MiB
用时 00:00:14   平均 13.67 MiB/s   当前 11.03 MiB/s

  #1    3.93 MiB/s      55.7 MiB
  #2    2.88 MiB/s      56.0 MiB
  #3    7.11 MiB/s      67.0 MiB
```

结束会按账号列出搬运量和 flood 次数，能一眼看出谁在偷懒。

## 断点续传

下载进度按分片存进 `data/tgdl.db`，每 2 秒落盘一次。中断后重跑同一条命令会接着下：

```
接着上次下：已完成 20.0 MiB / 198.7 MiB（179 片待下）
```

已经下完的文件会直接跳过：

```
已经下过了，跳过：data/example.mp4
```

判断依据是「文件大小对得上 **且** 数据库里没有它的有效进度记录」——
不能只看大小，因为开工前会按最终大小预分配，中断的文件也「大小刚好」。

如果数据库里有一条进度记录，但它指向的文件已经被删了（或者换了输出路径、
换了分片大小），这条记录会被自动清掉，不会让下次误以为「还有半截没下完」。

## 手改 keys.json

`data/keys.json` 是凭据清单，可以手改，改完跑一次 `tgdl accounts` 就生效：

```json
{
  "version": 2,
  "accounts": {
    "1": {
      "dc": 5,
      "ip": "149.154.167.51",
      "port": 443,
      "key": "<256 字节的 hex 主 key>",
      "dc_keys": { "4": "<导出到 DC4 的 key>" }
    }
  }
}
```

- 加一个账号：复制一条改个编号，`accounts` 会自动建出对应的 session
- 换台机器：把 `keys.json` 拷过去跑一次 `accounts`，session 自动重建
- `dc_keys` 删掉没关系，只是下次要多花 4 秒重新导出

`key` 是裸的 auth key，和密码等价，别外传。文件权限会自动设成 600。

## 常见问题

**一直 FLOOD_WAIT**
说明这个账号被限流了。`FLOOD_WAIT N` 里的 N 由服务端决定，而且会累积升级
（1s → 11s → 12s）。这时加账号比调参数有用；同一个账号加并发只会更快撞墙。

**`accounts` 里突然显示未登录**
授权被服务端撤销了。常见原因：手机上「设备」里手动终止了这个会话，或者同一账号
登录太频繁。重新 `tgdl login --account N` 即可。
session 文件还在不代表还登录着——以 `accounts` 的联网结果为准。

**公开频道里某个账号没订阅**
只要频道有 `@username`，没订阅的账号也能取到文件——程序会把第一个账号解析出的
username 传给其它账号。

**文件在别的 DC 上**
自动处理（`exportAuthorization` + `importAuthorization`），导出结果缓存在
`keys.json`，第二次就不花时间了。

## 目录结构

```
src/tgdl/
├── cli.py       命令行
├── config.py    配置
├── account.py   会话、登录、登出、凭据注入、重复账号清理
├── download.py  相册解析、并行下载、进度看板、断点续传
├── keys.py      keys.json
├── db.py        断点续传位图（SQLite）
├── qr.py        终端二维码
└── util.py      单位、目标解析、消息 id 换算
```
