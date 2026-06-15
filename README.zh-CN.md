<p align="center">
  <img src="assets/logo.svg" alt="quark-tracker" width="480">
</p>

<p align="center">
  <a href="README.md">English</a> · <strong>中文</strong>
</p>

# quark-tracker

**把夸克网盘分享链接里新更新的剧集，自动下载到你的 NAS，并按 Infuse/Plex/Jellyfin 规范命名**——带 TMDB 集名、智能去重、多线程下载、可选通知，还有"发个链接就自动搞定"的 AI agent 模式。

<img width="1791" height="1566" alt="image" src="https://github.com/user-attachments/assets/045945ac-424b-46fc-801f-0e8821029a19" />

<img width="1866" height="1326" alt="57e67868-9af1-4eb3-858c-776b28805f74" src="https://github.com/user-attachments/assets/280bbec7-23f0-4a4a-a133-259fca21f484" />



> **关键词**：夸克网盘 · 夸克自动下载 · 夸克网盘下载到 NAS · 自动追番 · 追剧 · 飞牛 fnOS · 群晖 · Infuse 刮削 · TMDB · aria2 加速 · 多线程下载 · quark-auto-save 搭档 · 媒体库自动化

它是 [**quark-auto-save**](https://github.com/Cp0204/quark-auto-save) 的搭档：quark-auto-save 负责监控分享链接、把新剧集**转存**进你自己的夸克账号；quark-tracker 补上缺的另一半——**真正把这些剧集下载到本地硬盘**、用 TMDB 元数据**改名**、和你已有的集数**去重**，并且可以**让 AI agent 驱动**：从一条分享链接判断这部剧完结没、连载就自动加监控并设截止日期。

```
夸克分享链接（持续更新）
  │  ① quark-auto-save（Docker）  监控分享 → 把新集转存进你的夸克账号
  ▼
你的夸克账号  /<根目录>/<剧名>/Season NN/
  │  ② quark-tracker  quark_sync.py（cron）  调夸克 API 列文件 → 按 SxxExx 去重 → aria2c 只下新的
  ▼
NAS 本地库  /media/<剧名>/Season NN/<剧名> - SxxExx - 集名.ext
  │  ③ TMDB 改名（自带） + 可插拔通知（webhook / 命令）
  ▼
  Infuse / Plex / Jellyfin
```

下载直接走夸克下载 API（cookie + 签名直链）+ **aria2c 多线程**，全速且不受任何 FUSE/WebDAV 挂载缓存影响。

## 为什么要它

quark-auto-save 擅长云端转存 + 在夸克内改名。但如果你想要 NAS 上**真正的本地文件**（归档、不怕分享失效、刮削更稳），就需要一个下载器：只取**新增**的集、合并进已有剧集库、而且要快。这就是 quark-tracker。

> **为什么必须用 aria2c：** 夸克对免费账号的**单连接**限速到 ~100 KB/s。用 16 条并发，aria2c 实测达到 **~40–67 MB/s**（**快几百倍**）。一集 1.5 GB 从"几个小时"变成"一分钟以内"。

## 功能

- **只下新增** —— 按 `SxxExx` 和本地库去重，已有的集自动跳过。
- **快** —— aria2c 多线程绕过夸克单连接限速（没装则回退 `curl`）。
- **TMDB 改名** —— 自带独立改名脚本 → `剧名 - S02E10 - 集名.ext`，不依赖外部 agent。
- **多分类** —— 任意标签（动漫 / 剧集 / …）映射到夸克暂存目录 + 本地库路径。
- **可插拔通知** —— `webhook`（Server酱 / Bark / 自定义）或 `command`（跑任意本地推送脚本），也可不推。
- **AI agent 模式（可选）** —— 一个控制入口（`quark_ctl.py`：`probe / tmdb / addtask / sync / …`）+ 一个技能示例，让 agent 拿到一条分享链接就能：探测内容、自动选最优字幕组、查 TMDB 判断完结状态、分情况下载，连载剧自动加监控并设**截止日期 = 完结日 + N 天**。
- **零 Python 依赖** —— 仅标准库 + `aria2c`/`curl` + `docker`。

## 前置要求

- 一台装了 **Docker** 的 Linux/NAS（本教程以 **飞牛 fnOS** 为例，它基于 Debian——任何 Linux 都行）。
- 运行中的 **[quark-auto-save](https://github.com/Cp0204/quark-auto-save)**（仓库自带 `compose.yaml`），并已登录你的夸克账号。
- 一个免费的 **[TMDB](https://www.themoviedb.org/settings/api) API key**（用于集名改名）。
- 宿主机有 `python3`、`docker`、以及 **`aria2c`**（强烈建议）。

---

## 🤖 让 AI 一键部署

让一个能 SSH 进你 NAS 的 AI agent（Claude Code / OpenClaw / Cursor 等）全自动装好。把下面这段提示词发给它：

```text
在我的 Linux NAS 上端到端部署 "quark-tracker"（https://github.com/Fanxiaoyao66/quark-tracker）：
1. 装 aria2c（apt/opkg 等）；
2. git clone 仓库到 /opt/quark-tracker；
3. 用自带 compose.yaml 起 quark-auto-save，然后提醒我去 WebUI（:5005）登录夸克；
4. 把 src/qsync_api.py 复制进 quark-auto-save 的 config 卷（让它落在 /app/config/qsync_api.py）；
5. 用 config.example.json 生成 /opt/quark-tracker/config.json；
6. 按 README 加每小时的 root cron（带 flock）；
7. 跑 quark_sync.py --dry-run 验证，并把输出给我看。
先问我：媒体库路径、TMDB API key、以及通知方式（一个 webhook 地址，或一条 shell 命令）。
然后执行，并准确汇报你改了什么。
```

给 agent root 前，先看它打算做什么——参见第 7 步的 agent 模式说明。

---

## 教程 —— 部署到 NAS（以飞牛 fnOS 为例）

下面以飞牛 fnOS 为具体例子，路径按你自己的系统调整。

### 第 0 步 —— 准备宿主机

1. **开启 SSH**：飞牛 设置 →「终端机/SSH」开启，然后 SSH 登录。
2. **装 aria2c**（飞牛是 Debian）：
   ```bash
   sudo apt update && sudo apt install -y aria2
   ```
3. 记下你的**媒体库**路径，飞牛上通常形如：
   ```
   /vol1/1000/动漫     # 动漫
   /vol1/1000/剧集     # 剧集
   ```
   把你的播放器（Infuse / 飞牛影视 / Emby / Jellyfin）指向这些目录。

### 第 1 步 —— 部署 quark-auto-save

```bash
git clone https://github.com/Fanxiaoyao66/quark-tracker.git
cd quark-tracker
docker compose up -d            # 在 :5005 启动 quark-auto-save
```
打开 `http://<NAS_IP>:5005`（默认 `admin` / `admin123` —— **请改掉**），**登录你的夸克账号**（粘贴 cookie，或扫码，视版本）。

> **飞牛提示：** 飞牛能**原生挂载**夸克（在 `/vol02/...` 下，底层是 rclone + WebDAV 网关）。**quark-tracker 不用这个挂载**——网关会缓存目录列表，刚转存的文件看不到。我们直接调夸克 API，永远是最新的，而且全速下载。

### 第 2 步 —— 把夸克 API 助手放进容器

`qsync_api.py` 必须在 quark-auto-save 容器**内部**运行（复用它的夸克客户端 + cookie）。复制进 config 卷：

```bash
cp src/qsync_api.py ./quark-config/qsync_api.py     # ./quark-config 挂载到 /app/config
```

### 第 3 步 —— 配置 quark-tracker

```bash
sudo mkdir -p /opt/quark-tracker
sudo cp -r src /opt/quark-tracker/
sudo cp config.example.json /opt/quark-tracker/config.json
sudo nano /opt/quark-tracker/config.json
```
飞牛上的最小配置：
```json
{
  "container": "quark-auto-save",
  "categories": {
    "动漫": { "quark_root": "/追剧/动漫", "library": "/vol1/1000/动漫" },
    "剧集": { "quark_root": "/追剧/剧集", "library": "/vol1/1000/剧集" }
  },
  "owner": "",
  "download": { "connections": 16 },
  "tmdb": { "api_key": "你的TMDB_KEY", "language": "zh-CN", "enddate_buffer_days": 14 },
  "rename": { "enabled": true },
  "notify": [],
  "overrides_file": "overrides.json"
}
```
- `quark_root` 是**你夸克账号里**让 quark-auto-save 保存该分类的目录（`/追剧/动漫` 等，首次转存会自动建）。
- `library` 是要下载到的**本地 NAS 目录**。
- 脚本按 `$QUARK_TRACKER_CONFIG` → 仓库旁的 `config.json` → `/opt/quark-tracker/config.json` 顺序找配置。

### 第 4 步 —— 加一部剧

在 quark-auto-save 网页端（`:5005`）新建任务：
- **分享链接**：粘贴夸克分享。多字幕组的分享，把链接指到你要跟的那个字幕组子目录。
- **保存目录**：`/追剧/动漫/<剧名>/Season 0X`（动漫）或 `/追剧/剧集/<剧名>/Season 0X`（剧集）。**`<剧名>` 用和本地库里完全一样的文件夹名**，缺的集才能并进去。
- **正则**：如 `\.mkv$`，只抓视频文件。

### 第 5 步 —— 测试

```bash
# 预览会下载哪些集（不改动）
sudo QUARK_TRACKER_CONFIG=/opt/quark-tracker/config.json python3 /opt/quark-tracker/src/quark_sync.py --dry-run

# 实际执行：下新集 + TMDB 改名 + 通知
sudo QUARK_TRACKER_CONFIG=/opt/quark-tracker/config.json python3 /opt/quark-tracker/src/quark_sync.py
```
落盘结果：
```
/vol1/1000/动漫/<剧名>/Season 01/<剧名> - S01E01 - 集名.mkv
```

### 第 6 步 —— 加定时（cron）

quark-auto-save 按自己的节奏转存（默认 08/18/20 点）。让 quark-tracker 每小时跑一次，把新转存的下下来：
```cron
# sudo crontab -e   （以 root 运行）
17 * * * * QUARK_TRACKER_CONFIG=/opt/quark-tracker/config.json /usr/bin/flock -n /tmp/quark_sync.lock /usr/bin/python3 /opt/quark-tracker/src/quark_sync.py >> /opt/quark-tracker/cron.log 2>&1
```

### 第 7 步 —— （可选）"发个链接就自动下" agent 模式

让 AI agent（如 OpenClaw 或任意工具执行器）从一条裸链接全自动搞定。只放行 agent 用户跑这一个控制脚本：
```sudoers
# /etc/sudoers.d/quark-tracker   （用 visudo -cf 校验）
你的agent用户 ALL=(root) NOPASSWD: /opt/quark-tracker/src/quark_ctl.py
```
把 agent 指向 [`integrations/openclaw/SKILL.md`](integrations/openclaw/SKILL.md)（任意 agent 都可作模板）。之后你只要发条夸克链接，agent 就会探测、查 TMDB、下载、并设好监控。

---

## 配置项

| 字段 | 含义 |
|---|---|
| `container` | quark-auto-save 容器名 |
| `categories.<标签>.quark_root` | 夸克账号里保存该分类的目录 |
| `categories.<标签>.library` | 该分类下载到的本地目录 |
| `owner` | 可选，下载后 chown 的 `user:group`（需 root）；空=不动 |
| `video_exts` | 识别的视频后缀 |
| `download.connections` | aria2c 并发连接数（默认 16）|
| `tmdb.api_key` / `tmdb.bearer_token` / `tmdb.env_file` | TMDB 凭据（任一）|
| `tmdb.language` | 元数据语言（如 `zh-CN`、`en-US`）|
| `tmdb.enddate_buffer_days` | 监控截止日 = 完结日 + 这么多天 |
| `rename.enabled` | 是否跑 TMDB 改名（false = 保留原始 `SxxExx` 名）|
| `notify` | 通知渠道列表（`command` / `webhook`）；空=不通知 |
| `overrides_file` | 给 TMDB 搜不到的中文名登记 `{ "<本地文件夹>": {"tmdb_id": N, "query": "..."} }` |

### 通知

```json
"notify": [
  { "type": "webhook", "url": "https://sctapi.ftqq.com/<key>.send", "json_field": "text" },
  { "type": "command", "cmd": ["/path/to/push.sh", "--message", "{msg}"] }
]
```
`{msg}` 会被替换成消息内容；`command` 若不含 `{msg}` 占位符，则消息从 stdin 传入。

## Agent 控制命令（quark_ctl.py）

| 子命令 | 作用 |
|---|---|
| `probe <分享URL>` | 分享结构 + 解析出的集号（JSON）|
| `tmdb --query Q [--id N] [--season S] [--buffer 14]` | 状态 / 是否完结 / 播出日期 / `suggested_enddate` |
| `addtask --name N --url U --savepath P [--enddate D]` | 新建 quark-auto-save 监控任务 |
| `deltask --name N` | 删除任务 |
| `override --name N --tmdb-id ID [--query Q]` | 给改名登记 TMDB 映射 |
| `transfer` | 立即执行转存 |
| `sync` | 下新集 + TMDB 改名 + 通知 |

## 去重与命名怎么工作

- 集号从分享文件名解析（支持 `S2 - 10`、`[10]`、`第10话`、`EP10`、`S02E10` 等）。
- 仅当本地 `Season NN/` 里没有含该 `SxxExx` 的**已完成**文件时才下载。
- 先存成 `剧名 - SxxExx.ext`，再由 TMDB 改名脚本变成 `剧名 - SxxExx - 集名.ext`。
- 所以已有的旧库（带 `SxxExx` 命名）不会被动，只取真正新增的集。

## 注意事项

- **Cookie 过期** —— 夸克 cookie 有效期数周；转存开始报错时，去 quark-auto-save 网页端重新登录。
- **飞牛原生夸克挂载** —— 不使用（其 WebDAV 网关有缓存）；quark-tracker 直接走夸克 API。
- **多字幕组分享** —— 把任务指向某一个字幕组子目录以保持一致；agent 模式会自动选。
- **TMDB 搜不到的名字** —— 加一条 `overrides`（或让 agent 自动登记），改名才能拿到集名。
- 本工具是先把别人分享的内容转存进你自己账号、再下载到本地——和 quark-auto-save 同一套逻辑。请合规使用，仅下载你有权获取的内容。

## 致谢

- 基于 [**Cp0204/quark-auto-save**](https://github.com/Cp0204/quark-auto-save) 构建。
- 元数据来自 [**The Movie Database (TMDB)**](https://www.themoviedb.org/)。本产品使用 TMDB API，但未获 TMDB 认可或认证。

## 许可证

**AGPL-3.0** —— 见 [LICENSE](LICENSE)。

quark-tracker 在运行时链接了 [quark-auto-save](https://github.com/Cp0204/quark-auto-save)（`qsync_api.py` 导入其 `quark_auto_save` 模块），因此继承相同的 AGPL-3.0 强 copyleft。注意 AGPL 第 13 条：若你把修改版作为可联网访问的服务运行，须向其用户提供对应源码。
