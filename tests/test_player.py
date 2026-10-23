import hashlib
import json
import unittest

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

from agecat.errors import ParseError
from agecat.player import (
    VipPlayerConfig,
    build_vip_request,
    decrypt_api_data,
    hxm_decrypt,
    hxm_encrypt,
    parse_m3u8_player,
    parse_vip_player,
    parse_vip_response,
    player_kind,
    stream_kind,
)

VIP_PAGE = """
<html><head>
<meta http-equiv="Content-Type" content="text/html; charset=utf-8" id="6701372971">
<meta name="viewport" content="width=device-width" id="viewport111ggfedcba65842585331">
</head><body><script>
    var Ref = "aHR0cHM6Ly93d3cuYWdlZG0uaW8v";
    var Time = "1792731076";
    var Version = "V3.2";
    var Vurl = "204EB66765DD85E14A2E";
</script></body></html>
"""
PLAYER_URL = "https://jx.wuzhoupai.com:8443/vip/?url=age_abc"


def encrypt_api_data(plaintext: str, key_material: str) -> str:
    cipher = AES.new(
        key_material[:16].encode("latin-1"), AES.MODE_CBC, iv=key_material[16:32].encode("latin-1")
    )
    return cipher.encrypt(pad(plaintext.encode(), AES.block_size)).hex().upper()


class PlayerTests(unittest.TestCase):
    def test_hxm_encrypt_matches_the_player_wasm(self):
        # Captured from the live player's hxm_encrypt("abc").
        self.assertEqual(hxm_encrypt("abc"), "e343bcc8050edf5f6bbd0f546e78032f")
        self.assertEqual(hxm_decrypt(hxm_encrypt("你好 world")), "你好 world")

    def test_player_kind_and_stream_kind(self):
        self.assertEqual(player_kind(PLAYER_URL), "vip")
        self.assertEqual(player_kind("https://jx.example/m3u8/?url=x"), "m3u8")
        self.assertIsNone(player_kind("https://jx.example/other/?url=x"))
        self.assertEqual(stream_kind("https://cdn.example/a/index.m3u8"), "hls")
        self.assertEqual(stream_kind("https://cdn.example/video/"), "mp4")
        self.assertEqual(stream_kind("https://cdn.example/video/", "m3u8"), "hls")

    def test_parse_m3u8_player_accepts_either_quote(self):
        self.assertEqual(
            parse_m3u8_player("var Vurl = 'https://a.example/index.m3u8';"),
            "https://a.example/index.m3u8",
        )
        self.assertEqual(
            parse_m3u8_player('var Vurl="https://b.example/x.m3u8"'), "https://b.example/x.m3u8"
        )
        with self.assertRaises(ParseError):
            parse_m3u8_player("<html></html>")

    def test_parse_vip_player_reads_variables_and_meta_ids(self):
        config = parse_vip_player(VIP_PAGE)

        self.assertEqual(
            config,
            VipPlayerConfig(
                vurl="204EB66765DD85E14A2E",
                time="1792731076",
                version="V3.2",
                referer="aHR0cHM6Ly93d3cuYWdlZG0uaW8v",
                content_type_meta_id="6701372971",
                viewport_meta_id="viewport111ggfedcba65842585331",
            ),
        )

    def test_parse_vip_player_rejects_missing_variables(self):
        with self.assertRaisesRegex(ParseError, "Vurl"):
            parse_vip_player('var Time = "1"; var Version = "V"; var Ref = "r";')

    def test_build_vip_request_signs_like_the_player(self):
        config = parse_vip_player(VIP_PAGE)

        request = build_vip_request(config, PLAYER_URL, request_id="fixed-uuid")

        self.assertEqual(request.url, "https://jx.wuzhoupai.com:8443/vip/Api.php")
        params = request.data["Params"]
        self.assertEqual(
            json.loads(hxm_decrypt(params.lower())),
            {
                "url": "204EB66765DD85E14A2E",
                "wap": "0",
                "ios": "0",
                "host": "jx.wuzhoupai.com",
                "referer": "aHR0cHM6Ly93d3cuYWdlZG0uaW8v",
                "time": "1792731076",
            },
        )
        self.assertEqual(
            hxm_decrypt(request.headers["Video-Parse-Sign"].lower()),
            f"jx.wuzhoupai.com | fixed-uuid | 1792731076 | V3.2 | {params}",
        )
        self.assertEqual(request.headers["Video-Parse-Uuid"], "fixed-uuid")

    def test_parse_vip_response_with_meta_key_code(self):
        config = parse_vip_player(VIP_PAGE)
        appkey = "cfbacf2d7bba66958b086eb860e73813"
        key = hashlib.md5(
            f"10{'6701372971111ggfedcba65842585331'}{appkey}V3.2".encode()
        ).hexdigest()
        inner_url = hxm_encrypt("https%3A%2F%2Fv16.example.com%2Fvideo%2Fabc%2F")
        data = encrypt_api_data(json.dumps({"url": inner_url, "type": "mp4"}), key)

        stream = parse_vip_response(
            {"Status": 1, "Code": 10, "Appkey": appkey, "Version": "V3.2", "Data": data}, config
        )

        self.assertEqual(stream.url, "https://v16.example.com/video/abc/")
        self.assertEqual(stream.kind, "mp4")

    def test_parse_vip_response_with_plain_code(self):
        config = parse_vip_player(VIP_PAGE)
        key = hashlib.md5(b"200keyV3.2").hexdigest()
        data = encrypt_api_data(
            json.dumps({"url": "https%3A%2F%2Fcdn.example%2Fa%2Findex.m3u8", "type": "m3u8"}), key
        )

        stream = parse_vip_response(
            {"Status": 1, "Code": 200, "Appkey": "key", "Version": "V3.2", "Data": data}, config
        )

        self.assertEqual(stream.url, "https://cdn.example/a/index.m3u8")
        self.assertEqual(stream.kind, "hls")

    def test_parse_vip_response_rejects_failures(self):
        config = parse_vip_player(VIP_PAGE)

        with self.assertRaisesRegex(ParseError, "rejected"):
            parse_vip_response({"Status": 0, "Msg": "解析失败"}, config)
        with self.assertRaisesRegex(ParseError, "missing"):
            parse_vip_response({"Status": 1}, config)
        with self.assertRaisesRegex(ParseError, "decrypted"):
            parse_vip_response(
                {"Status": 1, "Code": 1, "Appkey": "a", "Version": "v", "Data": "00" * 16}, config
            )

    def test_decrypt_api_data_round_trip(self):
        key = "0123456789abcdef" * 2
        self.assertEqual(decrypt_api_data(encrypt_api_data("payload", key), key), "payload")


if __name__ == "__main__":
    unittest.main()
