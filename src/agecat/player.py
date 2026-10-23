"""Resolve AGE's embedded third-party player pages into real stream URLs.

Every AGE play page embeds an iframe on a parser host (``jx.*``). Two player
flavors exist:

* ``/m3u8/?url=...`` pages print the decrypted HLS playlist URL as a plain
  ``var Vurl = '...'`` assignment.
* ``/vip/?url=...`` pages print an encrypted ``Vurl`` and resolve it through
  ``Api.php``. The request payload and signature are AES-CBC encrypted with a
  fixed key baked into the player's WebAssembly module, and the response is
  AES-CBC encrypted with a key derived from the response itself.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import unquote, urljoin, urlsplit

from .aes import cbc_decrypt, cbc_encrypt
from .errors import ParseError

PlayerKind = Literal["vip", "m3u8"]

# Key and IV of the player's ``hxm_encrypt``/``hxm_decrypt`` (AES-128-CBC, hex I/O).
PARAMS_KEY = b"ni po jie ni ** "
VIP_API_PATH = "Api.php"
VIP_SIGN_SEPARATOR = " | "
# The response key mixes in two meta ids when Code is 10; this filler is stripped.
VIEWPORT_ID_FILLER = "viewport"
VIP_CODE_WITH_META_KEY = 10

M3U8_VURL_PATTERN = re.compile(r"""var\s+Vurl\s*=\s*(['"])(?P<url>[^'"]+)\1""")
VIP_VAR_PATTERN = r"""var\s+{name}\s*=\s*(['"])(?P<value>[^'"]*)\1"""
META_TAG_PATTERN = re.compile(r"<meta\b[^>]*>", re.IGNORECASE)
META_ATTRIBUTE_PATTERN = re.compile(r"""([\w-]+)\s*=\s*(['"])(.*?)\2""")


@dataclass(frozen=True)
class VipPlayerConfig:
    """Values the VIP player page hands to its ``Api.php`` request."""

    vurl: str
    time: str
    version: str
    referer: str
    content_type_meta_id: str = ""
    viewport_meta_id: str = ""


@dataclass(frozen=True)
class VipApiRequest:
    """A fully built ``Api.php`` request: target URL, headers, and form body."""

    url: str
    headers: dict[str, str]
    data: dict[str, str]


@dataclass(frozen=True)
class PlayerStream:
    """Stream URL a player page resolves to."""

    url: str
    kind: Literal["mp4", "hls"]


def player_kind(player_url: str) -> PlayerKind | None:
    """Classify an embedded player URL by its path."""

    path = urlsplit(player_url).path.lower()
    if path.startswith("/vip/"):
        return "vip"
    if path.startswith("/m3u8/"):
        return "m3u8"
    return None


def stream_kind(url: str, declared: str | None = None) -> Literal["mp4", "hls"]:
    """Classify a stream URL as HLS or a single file from its type or path."""

    if declared and declared.lower() in {"m3u8", "hls"}:
        return "hls"
    return "hls" if ".m3u8" in urlsplit(url).path.lower() else "mp4"


def parse_m3u8_player(html: str) -> str:
    """Return the playlist URL printed by an ``/m3u8/`` player page."""

    match = M3U8_VURL_PATTERN.search(html)
    if match is None:
        raise ParseError("m3u8 player page is missing Vurl")
    return match.group("url")


def parse_vip_player(html: str) -> VipPlayerConfig:
    """Parse the encrypted ``Vurl`` and request parameters of a ``/vip/`` player page."""

    values = {name: _player_variable(html, name) for name in ("Vurl", "Time", "Version", "Ref")}
    missing = [name for name, value in values.items() if value is None]
    if missing:
        raise ParseError(f"VIP player page is missing {', '.join(missing)}")

    return VipPlayerConfig(
        vurl=values["Vurl"] or "",
        time=values["Time"] or "",
        version=values["Version"] or "",
        referer=values["Ref"] or "",
        content_type_meta_id=_meta_id(html, "http-equiv", "content-type"),
        viewport_meta_id=_meta_id(html, "name", "viewport"),
    )


def build_vip_request(
    config: VipPlayerConfig,
    player_url: str,
    *,
    request_id: str | None = None,
    mobile: bool = False,
) -> VipApiRequest:
    """Build the signed ``Api.php`` request the VIP player would send."""

    host = urlsplit(player_url).hostname or ""
    request_id = request_id or str(uuid.uuid4())
    params = hxm_encrypt(
        json.dumps(
            {
                "url": config.vurl,
                "wap": "1" if mobile else "0",
                "ios": "0",
                "host": host,
                "referer": config.referer,
                "time": config.time,
            },
            separators=(",", ":"),
        )
    ).upper()
    sign = hxm_encrypt(
        VIP_SIGN_SEPARATOR.join((host, request_id, config.time, config.version, params))
    ).upper()

    return VipApiRequest(
        url=urljoin(player_url, VIP_API_PATH),
        headers={
            "Video-Parse-Uuid": request_id,
            "Video-Parse-Time": config.time,
            "Video-Parse-Version": config.version,
            "Video-Parse-Sign": sign,
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        },
        data={"Params": params},
    )


def parse_vip_response(payload: Any, config: VipPlayerConfig) -> PlayerStream:
    """Decrypt an ``Api.php`` JSON response into the stream it points to."""

    if not isinstance(payload, Mapping):
        raise ParseError(f"VIP API response is not an object: {payload!r}")
    if payload.get("Status") != 1:
        message = payload.get("Msg") or payload.get("msg") or payload
        raise ParseError(f"VIP API rejected the request: {message}")

    code = payload.get("Code")
    appkey = payload.get("Appkey")
    version = payload.get("Version")
    data = payload.get("Data")
    if not isinstance(data, str) or appkey is None or version is None or code is None:
        raise ParseError("VIP API response is missing Code/Appkey/Version/Data")

    meta_key = ""
    if code == VIP_CODE_WITH_META_KEY:
        meta_key = (config.content_type_meta_id + config.viewport_meta_id).replace(
            VIEWPORT_ID_FILLER, "", 1
        )
    key_material = hashlib.md5(f"{code}{meta_key}{appkey}{version}".encode()).hexdigest()

    try:
        info = json.loads(decrypt_api_data(data, key_material))
    except (ValueError, KeyError) as error:
        raise ParseError(f"VIP API data could not be decrypted: {error}") from error
    if not isinstance(info, dict) or not isinstance(info.get("url"), str) or not info["url"]:
        raise ParseError(f"VIP API data is missing url: {info!r}")

    url = info["url"]
    if code == VIP_CODE_WITH_META_KEY:
        url = hxm_decrypt(url)
    url = unquote(url)
    declared = info.get("type")
    return PlayerStream(
        url=url, kind=stream_kind(url, declared if isinstance(declared, str) else None)
    )


def hxm_encrypt(text: str) -> str:
    """Encrypt text exactly like the player's ``hxm_encrypt`` (lowercase hex)."""

    return cbc_encrypt(PARAMS_KEY, PARAMS_KEY, text.encode("utf-8")).hex()


def hxm_decrypt(hex_text: str) -> str:
    """Decrypt hex produced by :func:`hxm_encrypt`."""

    return cbc_decrypt(PARAMS_KEY, PARAMS_KEY, bytes.fromhex(hex_text)).decode("utf-8")


def decrypt_api_data(hex_data: str, key_material: str) -> str:
    """Decrypt ``Api.php`` ``Data``: key and IV are the two halves of an MD5 hex digest."""

    key = key_material[:16].encode("latin-1")
    iv = key_material[16:32].encode("latin-1")
    return cbc_decrypt(key, iv, bytes.fromhex(hex_data)).decode("utf-8")


def _player_variable(html: str, name: str) -> str | None:
    """Return the string literal assigned to a top-level ``var`` in the player page."""

    match = re.search(VIP_VAR_PATTERN.format(name=re.escape(name)), html)
    return match.group("value") if match else None


def _meta_id(html: str, attribute: str, value: str) -> str:
    """Return the ``id`` of the first ``<meta>`` whose ``attribute`` equals ``value``."""

    for tag in META_TAG_PATTERN.findall(html):
        attributes = {
            name.lower(): content for name, _quote, content in META_ATTRIBUTE_PATTERN.findall(tag)
        }
        if attributes.get(attribute, "").lower() == value:
            return attributes.get("id", "")
    return ""
