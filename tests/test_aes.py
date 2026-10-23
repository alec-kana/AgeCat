import os
import unittest

from agecat import aes

# FIPS-197 appendix C example vectors (single block, so CBC with a zero IV).
FIPS_PLAINTEXT = bytes.fromhex("00112233445566778899aabbccddeeff")
FIPS_VECTORS = (
    (bytes(range(16)), "69c4e0d86a7b0430d8cdb78070b4c55a"),
    (bytes(range(24)), "dda97ca4864cdfe06eaf70a0ec0d7191"),
    (bytes(range(32)), "8ea2b7ca516745bfeafc49904b496089"),
)


class PurePythonAesTests(unittest.TestCase):
    """The pure-Python path is what runs on a-Shell, so test it directly."""

    def test_fips_197_vectors(self):
        for key, ciphertext in FIPS_VECTORS:
            with self.subTest(key_size=len(key)):
                self.assertEqual(
                    aes._python_encrypt(key, bytes(16), FIPS_PLAINTEXT).hex(), ciphertext
                )
                self.assertEqual(
                    aes._python_decrypt(key, bytes(16), bytes.fromhex(ciphertext)),
                    FIPS_PLAINTEXT,
                )

    def test_cbc_chains_blocks(self):
        key, iv = os.urandom(16), os.urandom(16)
        data = os.urandom(16 * 5)

        ciphertext = aes._python_encrypt(key, iv, data)

        # Identical plaintext blocks must not produce identical ciphertext blocks.
        repeated = aes._python_encrypt(key, iv, bytes(32))
        self.assertNotEqual(repeated[:16], repeated[16:])
        self.assertEqual(aes._python_decrypt(key, iv, ciphertext), data)

    def test_rejects_bad_key_sizes(self):
        with self.assertRaises(ValueError):
            aes._python_encrypt(b"short", bytes(16), bytes(16))

    def test_matches_whichever_backend_is_active(self):
        key, iv = os.urandom(16), os.urandom(16)
        data = os.urandom(1000)

        self.assertEqual(
            aes.cbc_encrypt(key, iv, data), aes._python_encrypt(key, iv, aes.pkcs7_pad(data))
        )


class PaddingTests(unittest.TestCase):
    def test_round_trip_for_every_remainder(self):
        key, iv = os.urandom(16), os.urandom(16)
        for size in range(0, 34):
            with self.subTest(size=size):
                data = os.urandom(size)
                self.assertEqual(aes.cbc_decrypt(key, iv, aes.cbc_encrypt(key, iv, data)), data)

    def test_bad_padding_and_partial_blocks_raise(self):
        with self.assertRaises(ValueError):
            aes.pkcs7_unpad(bytes(16))
        with self.assertRaises(ValueError):
            aes.cbc_decrypt(bytes(16), bytes(16), b"abc")


if __name__ == "__main__":
    unittest.main()
