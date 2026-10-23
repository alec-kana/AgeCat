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
- **断点续传与 403 恢复**：MP4 沿用 `.mp4.part` 续传；直链签名过期返回 403 时会重新解析拿到新链接，从已下载的字节处继续。

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
```

### 示例

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
| `--segment-concurrency N` | 每集同时下载的 HLS 分片数 | `4` |
| `--keep-ads` | 保留 m3u8 源中插入的广告片段 | `False` |
| `--ffmpeg PATH` | 用于封装 MP4 的 ffmpeg；也可设环境变量 `AGECAT_FFMPEG` | `PATH` 中的 `ffmpeg` |
| `--no-pacing` | 关闭所有请求节奏延迟（最快） | `False` |
| `--overwrite` | 覆盖已下载的文件 | `False` |
| `--no-resume` | 不沿用已有的 `.part` 临时文件 | `False` |
| `--plain-progress` | 逐行输出纯文本进度（手机终端如 a-shell 使用）；也可设 `AGECAT_PLAIN_PROGRESS=1` | `False` |
| `-v`, `--verbose` | 显示诊断日志，`-vv` 显示 HTTP 细节（会显示每集用了哪个源） | `False` |

完整参数见 `agecat --help`。

### 输出与临时文件

- MP4 源下载中为 `<标题>.mp4.part`，完成后改名为 `<标题>.mp4`。
- HLS 源下载中的分片存放在 `<标题>.hls.part/` 文件夹，中断后重跑会从缺失的分片继续；完成并封装后自动删除。如果换了播放源，旧分片会被丢弃（不同源的编码不能混用）。
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
