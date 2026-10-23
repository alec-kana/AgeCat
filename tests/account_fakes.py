"""An in-memory AGE account server: captcha, login, status, and favorites pages."""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast
from urllib.parse import parse_qs, urlsplit

import requests

SITE = "https://www.agedm.io"
CAPTCHA_PNG = (Path(__file__).parent / "fixtures" / "captcha.png").read_bytes()
SESSION_COOKIE = "age_session"


class FakeResponse:
    """Just enough of ``requests.Response`` for the account flow."""

    def __init__(self, *, url: str, body: bytes = b"", payload: object | None = None) -> None:
        self.url = url
        self.content = json.dumps(payload).encode() if payload is not None else body
        self.text = self.content.decode("utf-8", "replace")
        self.status_code = 200

    def json(self) -> object:
        return json.loads(self.text)


def favorite_card(anime_id: int, title: str, status: str = "第12集") -> str:
    return (
        '<div class="col"><div class="video_item">'
        f'<a href="http://www.agedm.io/detail/{anime_id}"><img alt="{title}" src="x.jpg"></a>'
        f'<span class="video_item--info">{status}</span>'
        f'<div class="video_item-title"><a href="/detail/{anime_id}">{title}</a></div>'
        f'<a class="video-item-cancel-fav" node-data-id="{anime_id}" node-data-status="1">'
        "取消收藏</a>"
        "</div></div>"
    )


def collects_page(cards: list[str], total: int | None = None) -> str:
    summary = f"<p>共 {total} 条记录</p>" if total is not None else ""
    return (
        "<html><body>"
        '<header><a href="/detail/99999999">热门推荐</a></header>'
        f'<div class="body_content_wrapper">{"".join(cards)}{summary}</div>'
        '<div class="foot_content_wrapper"><a href="/detail/88888888">页脚</a></div>'
        "</body></html>"
    )


@dataclass
class FakeAccountSite:
    """Accepts one account; the captcha answer is always '4809' (the fixture image)."""

    username: str = "alec"
    password: str = "correct horse"
    captcha_answer: str = "4809"
    favorites: list[list[str]] = field(
        default_factory=lambda: [[favorite_card(20260096, "欺诈游戏")]]
    )
    session_expires: float | None = None
    valid_tokens: set[str] = field(default_factory=set)
    login_attempts: list[dict[str, str]] = field(default_factory=list)
    captchas_served: int = 0
    pages_served: list[str] = field(default_factory=list)


class FakeAccountClient:
    """A client whose requests are answered by a :class:`FakeAccountSite`."""

    def __init__(self, site: FakeAccountSite) -> None:
        self.site = site
        self.session = requests.Session()

    def _cookie(self, name: str) -> str | None:
        for cookie in self.session.cookies:
            if cookie.name == name:
                return cookie.value
        return None

    def _logged_in(self) -> bool:
        return self._cookie(SESSION_COOKIE) in self.site.valid_tokens

    def get_resource(
        self,
        url: str,
        *,
        page_url: str | None = None,
        headers: Mapping[str, str] | None = None,
        paced: bool = True,
    ) -> requests.Response:
        path = urlsplit(url).path
        if path == "/v2/captcha":
            self.site.captchas_served += 1
            self.session.cookies.set(
                "age_captcha", f"hash{self.site.captchas_served}", domain="www.agedm.io"
            )
            return cast(requests.Response, FakeResponse(url=url, body=CAPTCHA_PNG))
        if path == "/api/account/status":
            payload = (
                {"code": 200, "data": {"username": self.site.username}}
                if self._logged_in()
                else {"code": 40001, "data": [], "message": "用户不存在！"}
            )
            return cast(requests.Response, FakeResponse(url=url, payload=payload))
        raise AssertionError(f"unexpected resource request: {url}")

    def post_form(
        self,
        url: str,
        data: Mapping[str, str],
        *,
        headers: Mapping[str, str] | None = None,
        page_url: str | None = None,
    ) -> requests.Response:
        assert urlsplit(url).path == "/api/account/login", url
        self.site.login_attempts.append(dict(data))
        if self._cookie("age_captcha") is None or data["captcha"] != self.site.captcha_answer:
            payload: dict[str, object] = {"code": 40003, "message": "验证码错误！"}
        elif data["username"] != self.site.username or data["password"] != self.site.password:
            payload = {"code": 40002, "message": "用户名或密码错误！"}
        else:
            token = f"token{len(self.site.login_attempts)}"
            self.site.valid_tokens.add(token)
            self.session.cookies.set(
                SESSION_COOKIE,
                token,
                domain="www.agedm.io",
                expires=int(self.site.session_expires) if self.site.session_expires else None,
            )
            payload = {"code": 200, "message": "登录成功"}
        return cast(requests.Response, FakeResponse(url=url, payload=payload))

    def get_document(self, url: str, *, page_url: str | None = None) -> requests.Response:
        parts = urlsplit(url)
        self.site.pages_served.append(url)
        if not self._logged_in():
            return cast(requests.Response, FakeResponse(url=f"{SITE}/", body=b"<html>home</html>"))
        page = int(parse_qs(parts.query).get("page", ["1"])[0])
        cards = self.site.favorites[page - 1] if page <= len(self.site.favorites) else []
        total = sum(len(group) for group in self.site.favorites)
        body = collects_page(cards, total).encode()
        return cast(requests.Response, FakeResponse(url=url, body=body))

    def close(self) -> None:
        self.session.close()


def far_future() -> float:
    return time.time() + 30 * 24 * 3600
