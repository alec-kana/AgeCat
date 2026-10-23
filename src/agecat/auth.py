"""AGE accounts: log in once, then reuse the saved session cookies.

Logging in needs a 4-digit image captcha that a person has to read, so the
point of saving anything is to avoid logging in again: the session cookies
are kept in a private file and loaded on every run. Optionally the
username and password are kept too, so that when the session does expire
only the captcha has to be typed.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from http.cookiejar import Cookie
from pathlib import Path
from typing import Any, Protocol

import requests
from requests.cookies import RequestsCookieJar, create_cookie

from .errors import AgeCatError
from .urls import normalized_host

HOME_ENV_VAR = "AGECAT_HOME"
SESSION_FILE_NAME = "session.json"
CAPTCHA_FILE_NAME = "captcha.png"
CAPTCHA_PATH = "/v2/captcha"
LOGIN_PATH = "/api/account/login"
STATUS_PATH = "/api/account/status"
# The captcha answer lives in this short-lived cookie; it is never worth saving.
CAPTCHA_COOKIE = "age_captcha"
API_OK = 200
LOGGER = logging.getLogger(__name__)


class LoginError(AgeCatError):
    """AGE rejected a login attempt (bad credentials or captcha)."""


class NotLoggedInError(AgeCatError):
    """An account page was requested without a valid session."""


class AccountClient(Protocol):
    """HTTP surface the account flow needs: the shared cookie jar plus requests."""

    session: requests.Session

    def get_resource(
        self,
        url: str,
        *,
        page_url: str | None = None,
        headers: Mapping[str, str] | None = None,
        paced: bool = True,
    ) -> requests.Response:
        """Return the raw response for a resource URL."""

        ...

    def post_form(
        self,
        url: str,
        data: Mapping[str, str],
        *,
        headers: Mapping[str, str] | None = None,
        page_url: str | None = None,
    ) -> requests.Response:
        """Return the raw response of a form-encoded XHR."""

        ...


@dataclass
class SavedSession:
    """What AgeCat remembers about the account between runs."""

    cookies: dict[str, dict[str, Any]] = field(default_factory=dict)
    username: str | None = None
    password: str | None = None
    saved_at: float | None = None

    @property
    def has_cookies(self) -> bool:
        """Return whether there is a session to try."""

        return bool(self.cookies)


def agecat_home() -> Path:
    """Return AgeCat's private settings folder (``$AGECAT_HOME`` or ``~/.agecat``).

    On a-Shell this lives in the app's home, outside ``~/Documents``, so the
    session file does not show up in the Files app.
    """

    configured = os.environ.get(HOME_ENV_VAR, "").strip()
    return Path(configured).expanduser() if configured else Path.home() / ".agecat"


class SessionStore:
    """Read and write the saved session file."""

    def __init__(self, home: Path | None = None) -> None:
        self.home = home or agecat_home()

    @property
    def path(self) -> Path:
        """Return the session file path."""

        return self.home / SESSION_FILE_NAME

    @property
    def captcha_path(self) -> Path:
        """Return where the latest captcha image is saved for viewing."""

        return self.home / CAPTCHA_FILE_NAME

    def load(self) -> SavedSession:
        """Return the saved session, or an empty one when none is usable."""

        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return SavedSession()
        except (OSError, ValueError) as error:
            LOGGER.warning("Ignoring unreadable session file %s: %s", self.path, error)
            return SavedSession()
        if not isinstance(payload, dict):
            return SavedSession()

        cookies = payload.get("cookies")
        return SavedSession(
            cookies=cookies if isinstance(cookies, dict) else {},
            username=_optional_str(payload.get("username")),
            password=_optional_str(payload.get("password")),
            saved_at=payload.get("saved_at")
            if isinstance(payload.get("saved_at"), int | float)
            else None,
        )

    def save(self, session: SavedSession) -> None:
        """Write the session file, readable by the current user only where supported."""

        self.home.mkdir(parents=True, exist_ok=True)
        payload = {
            "cookies": session.cookies,
            "username": session.username,
            "password": session.password,
            "saved_at": session.saved_at,
        }
        temporary = self.path.with_name(f"{self.path.name}.tmp")
        # Created with owner-only permissions so the password is never briefly world-readable.
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2)
        temporary.replace(self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def clear(self) -> bool:
        """Delete the saved session and captcha image; return whether a session existed."""

        existed = self.path.exists()
        for path in (self.path, self.captcha_path):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        return existed


def site_root(site_url: str) -> str:
    """Return the site's root page, the Referer for account requests."""

    return f"{site_url.rstrip('/')}/"


def fetch_captcha(client: AccountClient, site_url: str) -> bytes:
    """Download a fresh captcha image; its answer cookie lands in the client's jar."""

    url = f"{site_url.rstrip('/')}{CAPTCHA_PATH}?_t={time.time():.3f}"
    response = client.get_resource(url, page_url=site_root(site_url))
    return response.content


def login(
    client: AccountClient,
    site_url: str,
    username: str,
    password: str,
    captcha: str,
) -> None:
    """Submit the login form exactly as the site's own script does."""

    response = client.post_form(
        f"{site_url.rstrip('/')}{LOGIN_PATH}",
        {"username": username, "password": password, "captcha": captcha, "action": "login"},
        headers={"X-Requested-With": "XMLHttpRequest"},
        page_url=site_root(site_url),
    )
    payload = _json(response)
    if payload.get("code") != API_OK:
        raise LoginError(str(payload.get("message") or f"login failed: {payload}"))


def is_logged_in(client: AccountClient, site_url: str) -> bool:
    """Ask the site whether the client's cookies belong to a logged-in account."""

    response = client.get_resource(
        f"{site_url.rstrip('/')}{STATUS_PATH}",
        page_url=site_root(site_url),
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    try:
        return _json(response).get("code") == API_OK
    except AgeCatError:
        return False


def export_cookies(jar: RequestsCookieJar) -> dict[str, dict[str, Any]]:
    """Return the session cookies worth keeping, keyed by name.

    Domains are dropped on purpose: AGE moves domains every few months, and
    the saved session is replayed onto whichever domain is live.
    """

    now = time.time()
    cookies: dict[str, dict[str, Any]] = {}
    for cookie in jar:
        if cookie.name == CAPTCHA_COOKIE or cookie.value is None:
            continue
        if cookie.expires is not None and cookie.expires <= now:
            continue
        cookies[cookie.name] = {"value": cookie.value, "expires": cookie.expires}
    return cookies


def apply_cookies(
    jar: RequestsCookieJar,
    cookies: Mapping[str, Mapping[str, Any]],
    site_url: str,
) -> int:
    """Load saved cookies into ``jar`` for the live site; return how many were applied."""

    host = normalized_host(site_url)
    now = time.time()
    applied = 0
    for name, details in cookies.items():
        value = details.get("value")
        expires = details.get("expires")
        if not isinstance(value, str):
            continue
        if isinstance(expires, int | float) and expires <= now:
            continue
        # A leading-dot domain covers both agedm.io and www.agedm.io.
        jar.set_cookie(
            _cookie(name, value, f".{host}", expires if isinstance(expires, int | float) else None)
        )
        applied += 1
    return applied


def restore_session(client: AccountClient, site_url: str, store: SessionStore) -> bool:
    """Load the saved session into ``client`` and return whether it is still valid."""

    saved = store.load()
    if not saved.has_cookies or not apply_cookies(client.session.cookies, saved.cookies, site_url):
        return False
    if is_logged_in(client, site_url):
        LOGGER.info("Restored saved AGE session for %s", saved.username or "account")
        return True
    LOGGER.info("Saved AGE session has expired")
    return False


def remember_session(
    client: AccountClient,
    store: SessionStore,
    *,
    username: str,
    password: str | None,
) -> None:
    """Save the client's current cookies, plus the credentials the user chose to keep."""

    store.save(
        SavedSession(
            cookies=export_cookies(client.session.cookies),
            username=username,
            password=password,
            saved_at=time.time(),
        )
    )


def refresh_session(client: AccountClient, store: SessionStore) -> None:
    """Re-save the session after a successful request, keeping any cookie the site renewed."""

    saved = store.load()
    cookies = export_cookies(client.session.cookies)
    if not cookies:
        return
    store.save(
        SavedSession(
            cookies=cookies,
            username=saved.username,
            password=saved.password,
            saved_at=time.time(),
        )
    )


def _cookie(name: str, value: str, domain: str, expires: float | None) -> Cookie:
    """Build a cookie for ``requests``' jar."""

    return create_cookie(
        name=name,
        value=value,
        domain=domain,
        path="/",
        expires=int(expires) if expires is not None else None,
    )


def _json(response: requests.Response) -> dict[str, Any]:
    """Parse an account API response, which is always a JSON object."""

    try:
        payload = response.json()
    except ValueError as error:
        raise AgeCatError(f"account API did not return JSON: {response.text[:200]}") from error
    if not isinstance(payload, dict):
        raise AgeCatError(f"account API returned {type(payload).__name__}, expected an object")
    return payload


def _optional_str(value: object) -> str | None:
    """Return ``value`` when it is a non-empty string."""

    return value if isinstance(value, str) and value else None
