"""Interactive login: show the captcha, ask for what is missing, keep the session.

Kept apart from the HTTP helpers in :mod:`agecat.auth` so the prompts can be
swapped out in tests and the CLI stays a thin layer.
"""

from __future__ import annotations

import getpass
import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from rich.console import Console

from .auth import (
    AccountClient,
    LoginError,
    NotLoggedInError,
    SessionStore,
    fetch_captcha,
    login,
    remember_session,
    restore_session,
)
from .png import PngError, decode_png
from .terminal_image import print_image

MAX_LOGIN_ATTEMPTS = 5
MAX_CAPTCHA_REFRESHES = 10
LOGGER = logging.getLogger(__name__)

Prompt = Callable[[str], str]


@dataclass
class AccountIO:
    """How the login flow talks to the person at the terminal."""

    prompt: Prompt = input
    secret: Prompt = getpass.getpass
    console: Console = field(default_factory=lambda: Console(highlight=False))
    interactive: bool = True

    def say(self, message: str) -> None:
        """Print one status line."""

        self.console.print(message)


def ensure_login(
    client: AccountClient,
    site_url: str,
    store: SessionStore,
    io: AccountIO,
) -> None:
    """Make ``client`` logged in: reuse the saved session, or log in interactively."""

    if restore_session(client, site_url, store):
        return
    if not io.interactive:
        raise NotLoggedInError("not logged in to AGE; run `agecat login` in a terminal first")
    io.say("[yellow]AGE 登录已过期或尚未登录，需要登录一次。[/yellow]")
    interactive_login(client, site_url, store, io)


def interactive_login(
    client: AccountClient,
    site_url: str,
    store: SessionStore,
    io: AccountIO,
    *,
    save_password: bool | None = None,
) -> str:
    """Log in by asking for whatever is not saved, then keep the session; return the username.

    ``save_password`` forces the choice; ``None`` asks the first time and
    otherwise keeps doing whatever was chosen before.
    """

    saved = store.load()
    username = saved.username or _ask(io.prompt, "? AGE 用户名：")
    if not username:
        raise LoginError("no username provided")
    password = saved.password or _ask(io.secret, "? 密码：")
    if not password:
        raise LoginError("no password provided")
    password_was_saved = saved.password is not None

    for attempt in range(1, MAX_LOGIN_ATTEMPTS + 1):
        captcha = _read_captcha(client, site_url, store, io)
        try:
            login(client, site_url, username, password, captcha)
        except LoginError as error:
            io.say(f"[red]- 登录失败：{error}[/red]")
            if attempt == MAX_LOGIN_ATTEMPTS:
                raise
            if "验证码" not in str(error):
                # Wrong account details: a saved password may be stale, so ask again.
                username = _ask(io.prompt, f"? AGE 用户名（回车沿用 {username}）：") or username
                password = _ask(io.secret, "? 密码：") or password
                password_was_saved = False
            continue
        break

    keep_password = save_password
    if keep_password is None:
        keep_password = password_was_saved or _confirm(
            io.prompt, "? 保存密码？之后登录过期时只需输入验证码 [y/N]："
        )
    remember_session(client, store, username=username, password=password if keep_password else None)
    io.say(f"[green]+[/green] 已登录 {username}，登录状态已保存到 {store.path}")
    return username


def _read_captcha(client: AccountClient, site_url: str, store: SessionStore, io: AccountIO) -> str:
    """Show a fresh captcha until the person types an answer (empty input refreshes it)."""

    for _ in range(MAX_CAPTCHA_REFRESHES):
        # Stale cookies from an expired session must not outlive this login.
        client.session.cookies.clear()
        image_bytes = fetch_captcha(client, site_url)
        _save_captcha(store, image_bytes)
        try:
            print_image(decode_png(image_bytes), io.console)
        except PngError as error:
            LOGGER.debug("Cannot draw captcha: %s", error)
            io.say("（终端无法显示验证码图片）")
        io.say(f"[dim]验证码图片也已保存到 {store.captcha_path}[/dim]")
        answer = _ask(io.prompt, "? 验证码（看不清直接回车换一张）：")
        if answer:
            return answer
    raise LoginError("no captcha entered")


def _save_captcha(store: SessionStore, image_bytes: bytes) -> None:
    """Keep the latest captcha on disk as a fallback for terminals that cannot draw it."""

    try:
        store.home.mkdir(parents=True, exist_ok=True)
        store.captcha_path.write_bytes(image_bytes)
    except OSError as error:
        LOGGER.debug("Could not save captcha image: %s", error)


def _ask(prompt: Prompt, label: str) -> str:
    """Ask one question, treating end-of-input as an empty answer."""

    try:
        return prompt(label).strip()
    except EOFError:
        return ""


def _confirm(prompt: Prompt, label: str) -> bool:
    """Ask a yes/no question that defaults to no."""

    return _ask(prompt, label).lower() in {"y", "yes", "是", "好"}
