# Changelog

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
