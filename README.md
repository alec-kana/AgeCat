# AgeCat

> 稳定、快速、可续传的 AGE 动漫（agedm）命令行下载器。

AgeCat 能把 AGE 动漫的单集或整部动画下载为本地 MP4 文件，支持并发下载、断点续传、多播放源自动切换、Rich 多任务进度条，以及 AGE 换域名后的自动跟随。

本项目基于 [AniCat-v2](https://github.com/HeiTang/AniCat-v2)（Anime1 下载器）的框架改写而成，沿用其续传、节奏控制与 403 恢复机制，并以 GPL-3.0 授权发布。

## 特色

- **整部或单集下载**：输入详情页 `/detail/<id>` 即下载全部集数；输入播放页 `/play/<id>/<源>/<集>` 只下载该集。
- **多播放源自动切换**：AGE 每集都有多个播放源（西瓜、非凡、无尽、红牛……）。默认先用最快的「西瓜」源（直链 MP4，通常 1080p），失效时依次尝试其他 m3u8 源，直到有一个能播放为止。也可以用 `--source` 指定。
- **HLS（m3u8）下载**：分片并发下载、AES-128 自动解密、分片级续传，最后用 ffmpeg 无损封装为 MP4；没有 ffmpeg 时保留为 `.ts`。
- **自动去广告**：部分 m3u8 源会在播放列表中插入短广告片段，AgeCat 会识别并剔除（可用 `--keep-ads` 关闭）。
- **自动跟随新域名**：AGE 每隔几个月更换域名。AgeCat 启动时会通过 `age.tv` 等固定域名的跳转找到当前域名，旧域名的链接（如 `agedm.vip`、`agefans.la`）会自动改写到新域名。
- **按动画名称分文件夹**：文件保存为 `<输出目录>/<动画名>/<动画名> 第01集.mp4`。
- **重复运行很便宜**：已下载的集数只请求一次播放页（用来确定文件名）就跳过，不会去解析播放源。每周追番时重跑同一个详情页几乎没有额外等待。
- **先搜索再下载**：`agecat search 关键词` 直接列出 AGE 上的匹配结果和详情页链接，不必自己去网站找 URL。
- **续传与不残留临时文件**：同一次运行中，网络中断或直链签名过期（403）都会从已下载的位置继续。下载被中断（Ctrl-C、终止信号）或失败时，默认会停止所有下载、关闭文件并删除该集的临时文件，不会留下占用空间的残留；需要下次接着下载时加 `--keep-partial`。

## 播放源

| 播放源 | key | 类型 | 说明 |
|---|---|---|---|
| 西瓜（VIP） | `xigua` | 直链 MP4 | 默认首选，速度最快、画质最高。站内标「VIP」但实际免费 |
| 非凡 | `ffm3u8` | HLS | |
| 无尽 | `wjm3u8` | HLS | |
| 红牛 | `hnm3u8` | HLS | AES-128 加密，自动解密 |
| 计算云 | `lzm3u8` | HLS | |
| 暴风 | `bfzym3u8` | HLS | |
| 凤雏云 | `wolong` | HLS | |

不同网络环境下各源的可用性差异很大（部分源对海外 IP 返回 403/404），所以默认会自动按上表顺序逐个尝试。

## 安装

需要 Python 3.12 以上。所有依赖（requests、beautifulsoup4、rich）都是纯 Python，可以在 iOS 的 a-Shell 中直接安装。

### A. iOS a-Shell / 没有 git 的环境（推荐手机使用）

a-Shell 没有内置 `git`，用 tarball 安装：

```bash
python3 -m pip install https://github.com/alec-kana/AgeCat/archive/refs/heads/main.tar.gz
agecat --help
```

建议先 `cd ~/Documents` 再下载，这样文件会出现在「文件」App 的 a-Shell 目录里，可以直接用其他播放器打开。

### B. 电脑（有 git）

```bash
python3 -m pip install "git+https://github.com/alec-kana/AgeCat.git@main"
```

电脑上可以额外安装 C 加速的 AES（只影响加密的 m3u8 源，如红牛）：

```bash
python3 -m pip install "agecat[fast] @ git+https://github.com/alec-kana/AgeCat.git@main"
```

### 更新

跟随 main 分支安装时版本号不一定变化，需要强制重装：

```bash
# a-Shell
python3 -m pip install --upgrade --force-reinstall --no-cache-dir https://github.com/alec-kana/AgeCat/archive/refs/heads/main.tar.gz

# 电脑
python3 -m pip install --upgrade --force-reinstall --no-cache-dir "git+https://github.com/alec-kana/AgeCat.git@main"
```

### 关于 ffmpeg

- 西瓜源本身就是 MP4，**不需要 ffmpeg**。
- m3u8 源需要 ffmpeg 把分片封装成 MP4。AgeCat 会依次尝试 `AGECAT_FFMPEG` 环境变量、`PATH` 中的 `ffmpeg`，最后直接尝试运行 `ffmpeg` 命令（a-Shell 内置的 ffmpeg 就是这样找到的）。
- 都找不到或封装失败时，会保存为 `.ts`。VLC、Infuse 等播放器可以直接播放 `.ts`，也可以之后自己用 ffmpeg 转换。

### 在 a-Shell 上的进度显示

a-Shell 会自称支持终端重绘，但实际上 Rich 进度条不会实时刷新，画面会卡在 `0/N` 直到全部结束。请加上 `--plain-progress` 改为逐行输出进度：

```bash
agecat https://www.agedm.io/detail/20260096 --plain-progress
```

不想每次都输入的话，把环境变量写进 a-Shell 每次启动都会执行的 `~/Documents/.profile`（执行一次即可）：

```bash
echo 'export AGECAT_PLAIN_PROGRESS=1' >> ~/Documents/.profile
```

### 在手机上的速度

- 西瓜源（MP4）和未加密的 m3u8 源速度只取决于网络。
- 加密的 m3u8 源（如红牛）在 a-Shell 上用纯 Python 解密，大约每秒 1 MB 左右，一集可能多花几分钟。默认优先使用西瓜源，一般不会用到。

### 本地开发安装

```bash
git clone https://github.com/alec-kana/AgeCat.git
cd AgeCat
python3 -m pip install -e .
```

## 使用方法

```bash
agecat URL [URL ...] [OPTIONS]
agecat search 关键词 [OPTIONS]
```

### 示例

0. 先搜索再下载（不必自己去网站找 URL）：

    ```bash
    agecat search 欺诈
    ```

    ```text
     Title                Status · Premiere                URL
     ─────────────────────────────────────────────────────────────────────────────────
     欺诈游戏             第26集(完结) · TV · 2026-04-06   https://www.agedm.io/detail/20260096
     大欺诈师 razbliuto   全集 · WEB · 2024-02-23          https://www.agedm.io/detail/20240004
     大欺诈师             第23集(完结) · TV · 2020-07-08   https://www.agedm.io/detail/20200107

    + 3 result(s)
    ```

    URL 列宽度固定，终端再窄也不会被截断，可以直接复制。手机竖屏（不到 80 列）会省略中间的「状态 · 首播」列；更窄（不到 56 列）时改为每个结果两行：第一行是番名和信息，第二行是链接。

    复制链接交给 `agecat` 即可下载整部：

    ```bash
    agecat https://www.agedm.io/detail/20260096
    ```

1. 下载整部动画：

    ```bash
    agecat https://www.agedm.io/detail/20260096
    ```

2. 下载单集：

    ```bash
    agecat https://www.agedm.io/play/20260096/1/3
    ```

3. 只下载部分集数：

    ```bash
    agecat https://www.agedm.io/detail/20260096 --episodes 1-3,5
    ```

4. 指定播放源及其顺序（可用 key、名称或标签页序号）：

    ```bash
    agecat https://www.agedm.io/detail/20260096 --source 无尽,红牛
    agecat https://www.agedm.io/detail/20260096 --source wjm3u8,4
    ```

5. 旧域名的链接也可以直接用：

    ```bash
    agecat https://www.agedm.vip/play/20260096/1/3
    ```

### 常用参数

| 参数 | 说明 | 默认值 |
|---|---|---|
| `-o`, `--output DIR` | 输出目录（每部动画各建一个子文件夹） | `./age` |
| `-e`, `--episodes SPEC` | 只下载详情页中指定的集数，如 `15-17` 或 `1,3,5-8`；直接输入的播放页不受影响 | 不限制 |
| `-s`, `--source LIST` | 只使用这些播放源，按给定顺序尝试 | 见上方播放源表 |
| `--base-url URL` | 指定 AGE 站点，不再自动解析当前域名 | 自动 |
| `-c`, `--concurrency N` | 同时下载的集数 | `3` |
| `--segment-concurrency N` | 每集同时下载的 HLS 分片数 | `8` |
| `--keep-ads` | 保留 m3u8 源中插入的广告片段 | `False` |
| `--ffmpeg PATH` | 用于封装 MP4 的 ffmpeg；也可设环境变量 `AGECAT_FFMPEG` | `PATH` 中的 `ffmpeg` |
| `--no-pacing` | 关闭所有请求节奏延迟。默认已不加延迟，仅在用 `--min-delay` 等参数加了延迟时有用；遇到 403/429 仍会自动退避重试 | `False` |
| `--overwrite` | 覆盖已下载的文件 | `False` |
| `--no-resume` | 不沿用已有的 `.part` 临时文件 | `False` |
| `--keep-partial` | 保留中断或失败集数的临时文件，下次运行可以接着下载。默认会删除，并顺便清理输出目录中以前遗留的临时文件（本次没用到的动画文件夹只清理 30 分钟以上未写入的）。也可设环境变量 `AGECAT_KEEP_PARTIAL=1` | `False` |
| `--plain-progress` | 逐行输出纯文本进度（手机终端如 a-shell 使用）；也可设 `AGECAT_PLAIN_PROGRESS=1` | `False` |
| `-v`, `--verbose` | 显示诊断日志，`-vv` 显示 HTTP 细节（会显示每集用了哪个源） | `False` |

完整参数见 `agecat --help`。

### `agecat search`

| 参数 | 说明 | 默认值 |
|---|---|---|
| `关键词` | 番名关键词，由 AGE 网站自己的搜索处理（请用简体中文，繁体关键词搜不到） | 省略时会提示输入 |
| `-n`, `--limit N` | 最多显示几条结果（每页 24 条，按需翻页） | `48` |
| `--base-url URL` | 指定 AGE 站点，不再自动解析当前域名 | 自动 |
| `-v` / `-q` | 显示诊断日志 / 只显示错误 | - |

结果超过 `--limit` 时会提示总数（例如 `+ 48 result(s) of 1392`），可以加大 `--limit` 或换更精确的关键词。没有结果时 exit code 为 `1`。

### 中断下载与存储空间

按下 Ctrl-C，或程序收到 `SIGTERM` / `SIGHUP` 时，AgeCat 会：

1. 不再开始任何排队中的集数；
2. 让正在下载的集数在下一个数据块（HLS 为下一个分片）后停止，关闭网络连接和文件；
3. 等所有下载线程都结束后才把控制权交还终端（再按一次 Ctrl-C 也会继续等待收尾），并以 exit code `130` 结束；
4. 删除未完成集数的临时文件，最后显示删除了几个、释放了多少空间。

这在 a-Shell 这类「Python 跑在 App 进程里」的环境特别重要：如果中断后后台线程还在写入并一直开着文件，即使在「文件」App 删除了视频，空间也要等 a-Shell 完全退出才会释放。

如果程序被强制结束（例如 iOS 在后台直接终止 a-Shell），来不及清理的临时文件会在下一次下载结束（或被中断）时清掉：本次下载用到的文件夹全部清理，输出目录中其他动画的文件夹只清理 30 分钟以上未写入的，避免误删另一个同时运行的下载。想保留临时文件、下次接着下载大文件时，请加 `--keep-partial`。

### Exit code

| Exit code | 说明 |
|---|---|
| `0` | 全部下载完成或已跳过 |
| `1` | 至少一个 URL 失败、没有找到任何集数，或搜索没有结果 |
| `2` | 参数错误 |
| `130` | 下载被中断（Ctrl-C、`SIGTERM` 或 `SIGHUP`），未完成的下载已停止并清除临时文件 |

### 输出与临时文件

- MP4 源下载中为 `<标题>.mp4.part`，完成后改名为 `<标题>.mp4`。
- HLS 源下载中的分片存放在 `<标题>.hls.part/` 文件夹，完成并封装后自动删除；中断或失败时默认也会删除，加 `--keep-partial` 则保留，下次从缺失的分片继续。如果换了播放源，旧分片会被丢弃（不同源的编码不能混用）。
- 没有 ffmpeg 或封装失败时，HLS 集数保存为 `<标题>.ts`，大多数播放器都能直接播放；之后重跑会把 `.ts` 视为已下载。

## 工作原理

AGE 的播放页通过 iframe 嵌入第三方解析播放器（`jx.*`）：

- `/m3u8/` 播放器直接在页面里写出解密后的 m3u8 地址。
- `/vip/` 播放器（西瓜源）的地址经过加密，需要向 `Api.php` 发送 AES-CBC 加密并签名的请求，再用响应中的参数派生密钥解密，得到一个有时效（约 30 分钟）的直链 MP4。AgeCat 在 `src/agecat/player.py` 中完整复现了这一流程，不需要浏览器。

如果网站或播放器改版导致解析失败，用 `-vv` 运行可以看到每个源失败的具体原因。

## 开发

```bash
python3 -m pip install -e . ruff pyright
ruff format --check . && ruff check . && pyright
python3 -m unittest
```

联网冒烟测试默认跳过，需要时：

```bash
AGECAT_RUN_INTEGRATION=1 python3 -m unittest tests.test_integration_smoke
```

## 授权

[GPL-3.0](LICENSE)。

AgeCat 的下载、续传、节奏控制等框架来自 [HeiTang/AniCat-v2](https://github.com/HeiTang/AniCat-v2)（GPL-3.0），作为其衍生作品同样以 GPL-3.0 发布：可以自由使用、修改和分发，但分发修改版时必须同样开源并保留本授权。

本项目仅供学习与个人使用，视频内容的版权归原权利人所有。
