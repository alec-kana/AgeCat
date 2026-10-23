# Changelog

## 0.3.0 - 2026-10-23

### Added

- 新增 `agecat login` / `agecat logout`：登录一次后保存登录状态（session cookie），之后的运行直接沿用，不再询问；AGE 让登录过期时才需要重新登录。用户名会记住，可选择保存密码，过期后只需输入验证码。
- 验证码以彩色字符画直接显示在终端（适配手机宽度），并另存为 `captcha.png`；看不清时回车换一张。验证码由人输入，不做自动识别。
- 新增 `agecat collects`：列出账号收藏，需要时自动登录；`-d` 下载全部收藏（已下载的集数跳过），`-p 1,3-5` 按编号选择，其他下载参数原样传递。
- 登录状态保存在 `~/.agecat/session.json`（`AGECAT_HOME` 可改），权限仅本人可读；AGE 换域名后自动套用到新域名。每次成功使用后会更新保存的 cookie，延续登录。
- 新增纯 Python PNG 解码（`agecat.png`）与终端图片显示（`agecat.terminal_image`），不依赖无法在 a-Shell 安装的 Pillow。

## 0.2.0 - 2026-10-23

同步 AniCat-v2 0.4.0 之后的功能。

### Added

- 新增 `agecat search KEYWORD` 子命令：使用 AGE 网站自己的搜索，列出番名、集数状态、首播时间和可直接下载的详情页链接；结果以 Rich 表格显示，URL 列固定宽度不会被截断。`--limit` 控制显示数量（默认 48，按需翻页），超过时会显示总数。
- 新增 `--keep-partial` 参数与 `AGECAT_KEEP_PARTIAL` 环境变量：保留中断或失败集数的临时文件（`.mp4.part`、`.hls.part/`），供下次运行接着下载。
- 新增 exit code `130`：下载被 Ctrl-C、`SIGTERM` 或 `SIGHUP` 中断；结束前显示删除的临时文件数量与释放的空间。
- 新增每周运行的联网 smoke workflow（与 PR 检查分开），覆盖西瓜源、m3u8 源、详情页集数解析和搜索；使用纯 Python AES 路径，与 a-Shell 一致。

### Changed

- 未完成的集数（中断、失败、程序异常）默认删除自己的临时文件，并清理输出目录中以前遗留的临时文件（本次没用到的动画文件夹只清理 30 分钟以上未写入的）。同一次运行中的重试与重新解析仍会接着下载。
- `SIGTERM` / `SIGHUP` 与 Ctrl-C 走同样的收尾流程，不再在写文件途中直接结束。
- 省略必要参数时的提问逻辑抽成共用函数，`agecat` 与 `agecat search` 行为一致：终端下提问，非交互环境直接报参数错误而不是卡住。
- CI 改用 `actions/checkout@v7` / `actions/setup-python@v7`。
- 搜索结果在窄终端（手机竖屏）自动省略信息列，更窄时改为每个结果两行，链接始终完整。

### Fixed

- Ctrl-C 之前不会停止批量下载：线程池退出时会等所有排队的集数下载完。现在中断时会取消排队的集数，正在下载的集数在下一个数据块（HLS 为下一个分片）后停止并关闭文件，所有等待（节奏延迟、限速、重试退避、熔断冷却）立即唤醒，并确认所有线程结束后才返回。
- 等待下载结果的主线程改为每 0.5 秒醒来一次，Ctrl-C 在 Windows 上也能立即生效，而不是等当前集数下载完。
- 中断信号不再被播放源切换逻辑当成「该源失效」而继续尝试下一个源。

## 0.1.0 - 2026-10-23

首个版本，由 AniCat-v2 0.3.0 的框架改写为 AGE 动漫专用下载器。

### Added

- 支持 AGE 详情页（整部下载）与播放页（单集下载），`--episodes` 按集数筛选。
- 多播放源：默认按「西瓜 → 非凡 → 无尽 → 红牛 → 计算云 → 暴风 → 凤雏云」顺序尝试，失效自动切换；`--source` 可指定源及顺序。
- 西瓜（VIP）源解析：复现播放器的 AES-CBC 加密请求与响应解密，得到直链 MP4。
- HLS 下载：主播放列表选最高画质、分片并发、AES-128 解密、分片级续传、广告片段剔除（`--keep-ads` 关闭）、ffmpeg 封装 MP4（无 ffmpeg 时输出 `.ts`）。
- 自动跟随新域名：通过 `age.tv` 等固定域名的跳转找到当前站点，旧域名链接自动改写；`--base-url` 可手动指定。
- 新增参数：`--source`、`--base-url`、`--segment-concurrency`、`--keep-ads`、`--ffmpeg`；环境变量 `AGECAT_FFMPEG`、`AGECAT_PLAIN_PROGRESS`。

### Platform

- 依赖全部为纯 Python（requests、beautifulsoup4、rich），可在 iOS a-Shell 中用 tarball 安装；AES 内置纯 Python 实现，装有 pycryptodome / cryptography 时自动使用（可选 `agecat[fast]`）。
- ffmpeg 不在 `PATH` 时会直接尝试运行 `ffmpeg`（a-Shell 内置命令）；不可用时 HLS 集数保存为 `.ts`。
- 以 GPL-3.0 授权发布（沿用 AniCat-v2）。

### Removed

- Anime1（anime1.me / anime1.pw）支持。
