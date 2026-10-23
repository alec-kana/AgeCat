# Contributing

## 项目结构

```txt
src/agecat/
├── cli.py            # 命令行参数、输出、exit code、search 子命令、中断处理
├── service.py        # URL 展开、域名改写、并发下载协调、403 恢复、中断与临时文件清理
├── site.py           # 通过 age.tv 等跳转解析当前域名
├── search.py         # AGE 站内搜索结果解析与翻页
├── auth.py           # 登录 API、验证码、session 保存与恢复
├── account.py        # 交互式登录流程（提示、验证码显示、保存密码询问）
├── collects.py       # 收藏页解析与翻页
├── png.py            # 纯 Python PNG 解码（显示验证码用）
├── terminal_image.py # 用半格字符在终端画图
├── client.py         # HTTP session、超时、重试、节奏控制、请求身份头
├── extractor.py      # 详情页 / 播放页解析、播放源排序与切换
├── player.py         # 第三方解析播放器（/vip/ 解密、/m3u8/）
├── aes.py            # AES-CBC：纯 Python 实现，装有 pycryptodome/cryptography 时自动加速
├── downloader.py     # MP4 Range 续传、Content-Range 校验、原子写入
├── hls.py            # m3u8 解析、分片下载与解密、去广告、ffmpeg 封装
├── progress.py       # Rich / 纯文本进度
├── pacing.py         # 限速、抖动退避、熔断器
├── options.py        # 运行参数与校验
├── models.py         # 数据模型与进度事件
├── urls.py           # AGE 域名与 URL 形态识别
├── constants.py      # 默认值
├── errors.py         # 异常类型
└── logging_config.py
tests/
├── fakes.py          # 内存中的假 AGE 站点
└── test_*.py         # 单元测试；test_integration_smoke.py 需联网，默认跳过
```

## 开发环境

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e . ruff pyright
```

## 提交前检查

```bash
ruff format --check .
ruff check .
pyright
python3 -m unittest
```

网站或播放器改版时，先用联网冒烟测试确认：

```bash
AGECAT_RUN_INTEGRATION=1 python3 -m unittest tests.test_integration_smoke -v
```

## 约定

- 用户可见的文字（README、提示语）使用简体中文；代码、注释、日志使用英文。
- 必须能在 iOS a-Shell 上运行：运行时依赖只能是纯 Python 包（a-Shell 的 pip 无法安装 C 扩展），不能假设 `subprocess`、`git` 或 `PATH` 中的程序一定可用。
- 不在日志中输出密码、cookie 或签名链接的值；登录相关测试用 `tests/account_fakes.py` 的假账号服务器，不连真实账号。
- 解析逻辑放在纯函数里（输入 HTML/文本，输出数据），方便用 `tests/fakes.py` 测试。
- 不要在日志中输出签名链接或密钥的值。
