"""RSA PKCS#1 v1.5 signing over SHA-256, standard library only.

Oracle's native API signs with an RSA key, and this package installs nothing,
so the signature is built here: parse the PEM, recover the modulus and the
private exponent, pad the digest the way EMSA-PKCS1-v1_5 says, and raise it to
the private exponent. Python's big integers do the arithmetic.

Nothing here is a general RSA implementation and it must not become one. It
signs, it does not decrypt, and it has no key generation. The one thing it
does is measured against openssl by a known-answer test, because an operator
would otherwise discover a padding mistake as a bare 401 from a production
tenancy.
"""

from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import dataclass
from typing import List, Tuple

from ...errors import GenerationChainError

# The DER prefix EMSA-PKCS1-v1_5 puts in front of a SHA-256 digest. It is a
# fixed DigestInfo and getting one byte of it wrong produces a signature that
# verifies nowhere.
SHA256_DIGEST_INFO = bytes.fromhex("3031300d060960864801650304020105000420")

PEM_BLOCK = re.compile(
    rb"-----BEGIN ([A-Z ]+)-----(.*?)-----END \1-----", re.DOTALL)


class KeyError_(GenerationChainError):
    """The private key cannot be used, and the message says which way."""


@dataclass(frozen=True)
class RsaPrivateKey:
    modulus: int
    private_exponent: int

    @property
    def size_in_bytes(self) -> int:
        return (self.modulus.bit_length() + 7) // 8

    @classmethod
    def from_pem(cls, pem: bytes) -> "RsaPrivateKey":
        label, der = _pem_block(pem)
        if label == "RSA PRIVATE KEY":
            return cls._from_pkcs1(der)
        if label == "PRIVATE KEY":
            return cls._from_pkcs8(der)
        if label == "ENCRYPTED PRIVATE KEY":
            raise KeyError_(
                "the private key is passphrase-protected and this package "
                "cannot decrypt it; decrypt it once with `openssl pkcs8` or "
                "point --oci-key-file at an unencrypted copy")
        raise KeyError_(
            f"a PEM block labelled {label!r} is not an RSA private key; a "
            "public key and a certificate both land here")

    @classmethod
    def _from_pkcs1(cls, der: bytes) -> "RsaPrivateKey":
        fields = _sequence(der)
        if len(fields) < 4:
            raise KeyError_("the RSA private key structure is too short")
        return cls(modulus=_integer(fields[1]),
                   private_exponent=_integer(fields[3]))

    @classmethod
    def _from_pkcs8(cls, der: bytes) -> "RsaPrivateKey":
        fields = _sequence(der)
        if len(fields) < 3:
            raise KeyError_("the PKCS#8 structure is too short")
        tag, body = fields[2]
        if tag != 0x04:
            raise KeyError_("the PKCS#8 private key is not an octet string")
        return cls._from_pkcs1(body)

    def sign_sha256(self, message: bytes) -> bytes:
        """The signature bytes, ready to base64 into an Authorization header."""
        block = self._pad(hashlib.sha256(message).digest())
        value = int.from_bytes(block, "big")
        if value >= self.modulus:
            raise KeyError_("the padded digest does not fit the modulus")
        signed = pow(value, self.private_exponent, self.modulus)
        return signed.to_bytes(self.size_in_bytes, "big")

    def _pad(self, digest: bytes) -> bytes:
        """EMSA-PKCS1-v1_5: 0x00 0x01, 0xFF filler, 0x00, then the DigestInfo."""
        payload = SHA256_DIGEST_INFO + digest
        filler = self.size_in_bytes - len(payload) - 3
        if filler < 8:
            raise KeyError_("the RSA key is too small to sign a SHA-256 digest")
        return b"\x00\x01" + b"\xff" * filler + b"\x00" + payload


def _pem_block(pem: bytes) -> Tuple[str, bytes]:
    match = PEM_BLOCK.search(pem)
    if not match:
        raise KeyError_("no PEM block found in the private key file")
    try:
        return (match.group(1).decode("ascii"),
                base64.b64decode(match.group(2), validate=False))
    except (ValueError, UnicodeDecodeError) as exc:
        raise KeyError_(f"the PEM block does not decode: {exc}") from exc


def _read_tlv(der: bytes, offset: int) -> Tuple[int, bytes, int]:
    """One DER tag-length-value, returned with the offset after it."""
    if offset + 2 > len(der):
        raise KeyError_("the key structure ends inside a DER header")
    tag = der[offset]
    length = der[offset + 1]
    offset += 2
    if length & 0x80:
        count = length & 0x7F
        if count == 0 or count > 4 or offset + count > len(der):
            raise KeyError_("the key structure has an unusable DER length")
        length = int.from_bytes(der[offset:offset + count], "big")
        offset += count
    if offset + length > len(der):
        raise KeyError_("a DER value runs past the end of the key structure")
    return tag, der[offset:offset + length], offset + length


def _sequence(der: bytes) -> List[Tuple[int, bytes]]:
    tag, body, _ = _read_tlv(der, 0)
    if tag != 0x30:
        raise KeyError_("the key structure does not start with a SEQUENCE")
    out: List[Tuple[int, bytes]] = []
    offset = 0
    while offset < len(body):
        item_tag, item_body, offset = _read_tlv(body, offset)
        out.append((item_tag, item_body))
    return out


def _integer(field: Tuple[int, bytes]) -> int:
    tag, body = field
    if tag != 0x02:
        raise KeyError_("expected a DER INTEGER in the key structure")
    return int.from_bytes(body, "big")
