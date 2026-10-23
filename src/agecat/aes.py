"""AES in CBC mode with PKCS#7 padding, without requiring a compiled extension.

pip on iOS a-Shell can only install pure-Python packages, so AgeCat cannot
depend on pycryptodome or cryptography. This module uses whichever of them
is importable for speed and falls back to a table-driven pure-Python AES,
which is plenty for the VIP player's few-block payloads and workable for the
occasional AES-128 HLS mirror.
"""

from __future__ import annotations

import importlib
import struct
from collections.abc import Callable
from typing import Any

BLOCK_SIZE = 16
Cipher = Callable[[bytes, bytes, bytes], bytes]


def pkcs7_pad(data: bytes) -> bytes:
    """Pad ``data`` to a whole number of blocks."""

    padding = BLOCK_SIZE - len(data) % BLOCK_SIZE
    return data + bytes([padding]) * padding


def pkcs7_unpad(data: bytes) -> bytes:
    """Strip PKCS#7 padding, raising ``ValueError`` when it is malformed."""

    if not data or len(data) % BLOCK_SIZE:
        raise ValueError("padded data is not a whole number of blocks")
    padding = data[-1]
    if not 1 <= padding <= BLOCK_SIZE or data[-padding:] != bytes([padding]) * padding:
        raise ValueError("invalid PKCS#7 padding")
    return data[:-padding]


def cbc_encrypt(key: bytes, iv: bytes, plaintext: bytes) -> bytes:
    """Pad and encrypt ``plaintext`` with AES-CBC."""

    return _ENCRYPT(key, iv, pkcs7_pad(plaintext))


def cbc_decrypt(key: bytes, iv: bytes, ciphertext: bytes) -> bytes:
    """Decrypt AES-CBC ``ciphertext`` and strip its padding.

    Raises ``ValueError`` on a partial block or bad padding, which usually
    means the wrong key or IV.
    """

    if len(ciphertext) % BLOCK_SIZE:
        raise ValueError("ciphertext is not a whole number of blocks")
    return pkcs7_unpad(_DECRYPT(key, iv, ciphertext))


def backend_name() -> str:
    """Return which AES implementation is in use, for diagnostics."""

    return _BACKEND


# --- Pure-Python implementation -------------------------------------------------


def _gmul(a: int, b: int) -> int:
    """Multiply two bytes in GF(2^8) with the AES polynomial."""

    result = 0
    while b:
        if b & 1:
            result ^= a
        a <<= 1
        if a & 0x100:
            a ^= 0x11B
        b >>= 1
    return result


def _build_tables() -> tuple[list[int], list[int], list[list[int]], list[list[int]]]:
    """Build the S-boxes and the four encryption and decryption round tables."""

    inverse = [0] * 256
    for value in range(1, 256):
        # a^254 is the multiplicative inverse of a in GF(2^8).
        power, base, exponent = 1, value, 254
        while exponent:
            if exponent & 1:
                power = _gmul(power, base)
            base = _gmul(base, base)
            exponent >>= 1
        inverse[value] = power

    sbox = [0] * 256
    inv_sbox = [0] * 256
    for value in range(256):
        b = inverse[value]
        s = b
        for shift in range(1, 5):
            s ^= ((b << shift) | (b >> (8 - shift))) & 0xFF
        s ^= 0x63
        sbox[value] = s
        inv_sbox[s] = value

    te0 = [
        (_gmul(s, 2) << 24) | (s << 16) | (s << 8) | _gmul(s, 3)
        for s in (sbox[x] for x in range(256))
    ]
    td0 = [
        (_gmul(s, 14) << 24) | (_gmul(s, 9) << 16) | (_gmul(s, 13) << 8) | _gmul(s, 11)
        for s in (inv_sbox[x] for x in range(256))
    ]

    def rotations(table: list[int]) -> list[list[int]]:
        result = [table]
        for _ in range(3):
            previous = result[-1]
            result.append([((word >> 8) | ((word & 0xFF) << 24)) for word in previous])
        return result

    return sbox, inv_sbox, rotations(te0), rotations(td0)


_SBOX, _INV_SBOX, _TE, _TD = _build_tables()
_KEY_CACHE: dict[bytes, tuple[list[int], list[int], int]] = {}


def _expand_key(key: bytes) -> tuple[list[int], list[int], int]:
    """Return encryption and equivalent-inverse decryption round keys plus round count."""

    cached = _KEY_CACHE.get(key)
    if cached is not None:
        return cached
    if len(key) not in (16, 24, 32):
        raise ValueError(f"AES key must be 16, 24, or 32 bytes, not {len(key)}")

    sbox = _SBOX
    nk = len(key) // 4
    rounds = nk + 6
    words = list(struct.unpack(f">{nk}I", key))
    rcon = 1
    for index in range(nk, 4 * (rounds + 1)):
        word = words[index - 1]
        if index % nk == 0:
            word = ((word << 8) & 0xFFFFFFFF) | (word >> 24)
            word = (
                (sbox[word >> 24] << 24)
                | (sbox[(word >> 16) & 0xFF] << 16)
                | (sbox[(word >> 8) & 0xFF] << 8)
                | sbox[word & 0xFF]
            ) ^ (rcon << 24)
            rcon = _gmul(rcon, 2)
        elif nk > 6 and index % nk == 4:
            word = (
                (sbox[word >> 24] << 24)
                | (sbox[(word >> 16) & 0xFF] << 16)
                | (sbox[(word >> 8) & 0xFF] << 8)
                | sbox[word & 0xFF]
            )
        words.append(words[index - nk] ^ word)

    # Decryption uses the round keys in reverse, with InvMixColumns applied to
    # the middle rounds (FIPS-197 "equivalent inverse cipher").
    td0, td1, td2, td3 = _TD
    decrypt_words: list[int] = []
    for round_index in range(rounds, -1, -1):
        block = words[4 * round_index : 4 * round_index + 4]
        if 0 < round_index < rounds:
            block = [
                td0[sbox[w >> 24]]
                ^ td1[sbox[(w >> 16) & 0xFF]]
                ^ td2[sbox[(w >> 8) & 0xFF]]
                ^ td3[sbox[w & 0xFF]]
                for w in block
            ]
        decrypt_words.extend(block)

    expanded = (words, decrypt_words, rounds)
    _KEY_CACHE[key] = expanded
    return expanded


def _python_encrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    """Encrypt whole blocks with AES-CBC in pure Python."""

    rk, _, rounds = _expand_key(key)
    te0, te1, te2, te3 = _TE
    sbox = _SBOX
    words = struct.unpack(f">{len(data) // 4}I", data)
    p0, p1, p2, p3 = struct.unpack(">4I", iv)
    output: list[int] = []

    for offset in range(0, len(words), 4):
        s0 = words[offset] ^ p0 ^ rk[0]
        s1 = words[offset + 1] ^ p1 ^ rk[1]
        s2 = words[offset + 2] ^ p2 ^ rk[2]
        s3 = words[offset + 3] ^ p3 ^ rk[3]
        k = 4
        for _ in range(rounds - 1):
            t0 = (
                te0[s0 >> 24] ^ te1[(s1 >> 16) & 255] ^ te2[(s2 >> 8) & 255] ^ te3[s3 & 255] ^ rk[k]
            )
            t1 = (
                te0[s1 >> 24]
                ^ te1[(s2 >> 16) & 255]
                ^ te2[(s3 >> 8) & 255]
                ^ te3[s0 & 255]
                ^ rk[k + 1]
            )
            t2 = (
                te0[s2 >> 24]
                ^ te1[(s3 >> 16) & 255]
                ^ te2[(s0 >> 8) & 255]
                ^ te3[s1 & 255]
                ^ rk[k + 2]
            )
            t3 = (
                te0[s3 >> 24]
                ^ te1[(s0 >> 16) & 255]
                ^ te2[(s1 >> 8) & 255]
                ^ te3[s2 & 255]
                ^ rk[k + 3]
            )
            s0, s1, s2, s3 = t0, t1, t2, t3
            k += 4
        p0 = (
            (sbox[s0 >> 24] << 24)
            | (sbox[(s1 >> 16) & 255] << 16)
            | (sbox[(s2 >> 8) & 255] << 8)
            | sbox[s3 & 255]
        ) ^ rk[k]
        p1 = (
            (sbox[s1 >> 24] << 24)
            | (sbox[(s2 >> 16) & 255] << 16)
            | (sbox[(s3 >> 8) & 255] << 8)
            | sbox[s0 & 255]
        ) ^ rk[k + 1]
        p2 = (
            (sbox[s2 >> 24] << 24)
            | (sbox[(s3 >> 16) & 255] << 16)
            | (sbox[(s0 >> 8) & 255] << 8)
            | sbox[s1 & 255]
        ) ^ rk[k + 2]
        p3 = (
            (sbox[s3 >> 24] << 24)
            | (sbox[(s0 >> 16) & 255] << 16)
            | (sbox[(s1 >> 8) & 255] << 8)
            | sbox[s2 & 255]
        ) ^ rk[k + 3]
        output += (p0, p1, p2, p3)

    return struct.pack(f">{len(output)}I", *output)


def _python_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    """Decrypt whole blocks with AES-CBC in pure Python."""

    _, rk, rounds = _expand_key(key)
    td0, td1, td2, td3 = _TD
    inv = _INV_SBOX
    words = struct.unpack(f">{len(data) // 4}I", data)
    c0, c1, c2, c3 = struct.unpack(">4I", iv)
    output: list[int] = []

    for offset in range(0, len(words), 4):
        w0, w1, w2, w3 = words[offset : offset + 4]
        s0 = w0 ^ rk[0]
        s1 = w1 ^ rk[1]
        s2 = w2 ^ rk[2]
        s3 = w3 ^ rk[3]
        k = 4
        for _ in range(rounds - 1):
            t0 = (
                td0[s0 >> 24] ^ td1[(s3 >> 16) & 255] ^ td2[(s2 >> 8) & 255] ^ td3[s1 & 255] ^ rk[k]
            )
            t1 = (
                td0[s1 >> 24]
                ^ td1[(s0 >> 16) & 255]
                ^ td2[(s3 >> 8) & 255]
                ^ td3[s2 & 255]
                ^ rk[k + 1]
            )
            t2 = (
                td0[s2 >> 24]
                ^ td1[(s1 >> 16) & 255]
                ^ td2[(s0 >> 8) & 255]
                ^ td3[s3 & 255]
                ^ rk[k + 2]
            )
            t3 = (
                td0[s3 >> 24]
                ^ td1[(s2 >> 16) & 255]
                ^ td2[(s1 >> 8) & 255]
                ^ td3[s0 & 255]
                ^ rk[k + 3]
            )
            s0, s1, s2, s3 = t0, t1, t2, t3
            k += 4
        output += (
            (
                (inv[s0 >> 24] << 24)
                | (inv[(s3 >> 16) & 255] << 16)
                | (inv[(s2 >> 8) & 255] << 8)
                | inv[s1 & 255]
            )
            ^ rk[k]
            ^ c0,
            (
                (inv[s1 >> 24] << 24)
                | (inv[(s0 >> 16) & 255] << 16)
                | (inv[(s3 >> 8) & 255] << 8)
                | inv[s2 & 255]
            )
            ^ rk[k + 1]
            ^ c1,
            (
                (inv[s2 >> 24] << 24)
                | (inv[(s1 >> 16) & 255] << 16)
                | (inv[(s0 >> 8) & 255] << 8)
                | inv[s3 & 255]
            )
            ^ rk[k + 2]
            ^ c2,
            (
                (inv[s3 >> 24] << 24)
                | (inv[(s2 >> 16) & 255] << 16)
                | (inv[(s1 >> 8) & 255] << 8)
                | inv[s0 & 255]
            )
            ^ rk[k + 3]
            ^ c3,
        )
        c0, c1, c2, c3 = w0, w1, w2, w3

    return struct.pack(f">{len(output)}I", *output)


# --- Optional compiled backends ---------------------------------------------------


def _pycryptodome_backend() -> tuple[Cipher, Cipher] | None:
    """Return CBC functions backed by pycryptodome, when it is installed."""

    try:
        aes: Any = importlib.import_module("Crypto.Cipher.AES")
    except ImportError:
        return None

    def encrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
        return aes.new(key, aes.MODE_CBC, iv=iv).encrypt(data)

    def decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
        return aes.new(key, aes.MODE_CBC, iv=iv).decrypt(data)

    return encrypt, decrypt


def _cryptography_backend() -> tuple[Cipher, Cipher] | None:
    """Return CBC functions backed by cryptography, when it is installed."""

    try:
        ciphers: Any = importlib.import_module("cryptography.hazmat.primitives.ciphers")
    except ImportError:
        return None

    def run(key: bytes, iv: bytes, data: bytes, *, encrypt: bool) -> bytes:
        cipher = ciphers.Cipher(ciphers.algorithms.AES(key), ciphers.modes.CBC(iv))
        context = cipher.encryptor() if encrypt else cipher.decryptor()
        return context.update(data) + context.finalize()

    return (
        lambda key, iv, data: run(key, iv, data, encrypt=True),
        lambda key, iv, data: run(key, iv, data, encrypt=False),
    )


# FIPS-197 appendix C.1, used to check that a compiled backend actually works.
_SELF_TEST_KEY = bytes(range(16))
_SELF_TEST_PLAINTEXT = bytes.fromhex("00112233445566778899aabbccddeeff")
_SELF_TEST_CIPHERTEXT = bytes.fromhex("69c4e0d86a7b0430d8cdb78070b4c55a")


def _backend_works(encrypt: Cipher, decrypt: Cipher) -> bool:
    iv = bytes(BLOCK_SIZE)
    return (
        encrypt(_SELF_TEST_KEY, iv, _SELF_TEST_PLAINTEXT) == _SELF_TEST_CIPHERTEXT
        and decrypt(_SELF_TEST_KEY, iv, _SELF_TEST_CIPHERTEXT) == _SELF_TEST_PLAINTEXT
    )


def _select_backend(
    factories: tuple[tuple[str, Callable[[], tuple[Cipher, Cipher] | None]], ...] = (
        ("pycryptodome", _pycryptodome_backend),
        ("cryptography", _cryptography_backend),
    ),
) -> tuple[str, Cipher, Cipher]:
    """Pick the fastest AES implementation that actually works.

    A compiled package can be importable yet broken: a-Shell ships pycryptodome
    whose native frameworks fail to load with ``OSError``. Any failure while
    loading or self-testing a backend just moves on to the next one.
    """

    for name, factory in factories:
        try:
            backend = factory()
            if backend is not None and _backend_works(*backend):
                return name, backend[0], backend[1]
        except Exception:
            continue
    return "python", _python_encrypt, _python_decrypt


_BACKEND, _ENCRYPT, _DECRYPT = _select_backend()
