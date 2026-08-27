"""Jackson SMILE, the subset Elasticsearch writes into shard documents.

Written from the SMILE specification rather than adapted from the decoder in
`s3_repo_sweeper.py`. That duplication is the point of this package: a shared
decoder would make both derivations wrong in the same way at the same time,
and two tools that agree because they share a bug are worse evidence than one
tool on its own.

Every token this decoder does not recognise raises. Nothing is skipped and
nothing is guessed, because a decoder that resynchronised after a token it did
not understand would produce a file list nobody wrote, and a file list nobody
wrote is how a blob that is still in use gets named.
"""

from __future__ import annotations

import struct
from typing import Any, Dict, List, Optional, Tuple

from ..errors import BlobFormatError

SMILE_SIGNATURE = b":)\n"
HEADER_LENGTH = 4

HAS_SHARED_NAMES = 0x01
HAS_SHARED_STRING_VALUES = 0x02

# Jackson resets each back-reference table once it holds this many entries,
# so a decoder that grew its table without bound would drift out of step with
# the writer on any document large enough to matter.
MAX_SHARED_ENTRIES = 1024
MAX_SHARED_LENGTH = 64

TOKEN_LITERAL_EMPTY_STRING = 0x20
TOKEN_LITERAL_NULL = 0x21
TOKEN_LITERAL_FALSE = 0x22
TOKEN_LITERAL_TRUE = 0x23
TOKEN_INT_32 = 0x24
TOKEN_INT_64 = 0x25
TOKEN_FLOAT_32 = 0x28
TOKEN_FLOAT_64 = 0x29
TOKEN_LONG_KEY_UNICODE = 0x34
TOKEN_END_OF_LONG_TEXT = 0xFC
TOKEN_RAW_BINARY = 0xFD
TOKEN_START_ARRAY = 0xF8
TOKEN_END_ARRAY = 0xF9
TOKEN_START_OBJECT = 0xFA
TOKEN_END_OBJECT = 0xFB

MAX_DEPTH = 200


def decode_smile(data: bytes) -> Any:
    """Decode one SMILE document to plain Python values."""
    return _Decoder(data).document()


def _zigzag(value: int) -> int:
    return (value >> 1) ^ -(value & 1)


class _SharedTable:
    """One of SMILE's two back-reference tables.

    The writer and the reader each keep the same list and refer to entries by
    position, so the reader has to add exactly what the writer added, in the
    same order, and reset at the same point. Getting that wrong decodes a
    document into a different document rather than into an error.
    """

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled
        self._entries: List[str] = []

    def add(self, value: str) -> None:
        if not self.enabled:
            return
        if len(self._entries) >= MAX_SHARED_ENTRIES:
            self._entries = []
        self._entries.append(value)

    def get(self, index: int) -> str:
        if not self.enabled:
            raise BlobFormatError(
                "a back reference appeared in a document whose header says "
                "that table is off")
        if index < 0 or index >= len(self._entries):
            raise BlobFormatError(f"back reference {index} is out of range")
        return self._entries[index]


class _Decoder:

    def __init__(self, data: bytes) -> None:
        if not data.startswith(SMILE_SIGNATURE) or len(data) < HEADER_LENGTH:
            raise BlobFormatError("not a SMILE document")
        flags = data[3]
        version = (flags >> 4) & 0x0F
        if version != 0:
            raise BlobFormatError(f"SMILE version {version} is not understood")
        self._data = data
        self._at = HEADER_LENGTH
        self._names = _SharedTable(bool(flags & HAS_SHARED_NAMES))
        self._values = _SharedTable(bool(flags & HAS_SHARED_STRING_VALUES))

    # -- document ---------------------------------------------------------

    def document(self) -> Any:
        value = self._value(self._byte(), depth=0)
        return value

    # -- raw reads --------------------------------------------------------

    def _byte(self) -> int:
        if self._at >= len(self._data):
            raise BlobFormatError("SMILE document ends mid-token")
        byte = self._data[self._at]
        self._at += 1
        return byte

    def _take(self, count: int) -> bytes:
        end = self._at + count
        if count < 0 or end > len(self._data):
            raise BlobFormatError("SMILE document ends mid-value")
        chunk = self._data[self._at:end]
        self._at = end
        return chunk

    def _vint(self) -> int:
        """SMILE's unsigned vint: seven bits a byte, six in the last one."""
        value = 0
        for _ in range(10):
            byte = self._byte()
            if byte & 0x80:
                return (value << 6) | (byte & 0x3F)
            value = (value << 7) | byte
        raise BlobFormatError("SMILE vint does not terminate")

    def _text(self, length: int) -> str:
        try:
            return self._take(length).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise BlobFormatError(f"SMILE string is not utf-8: {exc}") from exc

    def _long_text(self) -> str:
        end = self._data.find(bytes([TOKEN_END_OF_LONG_TEXT]), self._at)
        if end < 0:
            raise BlobFormatError("SMILE long string has no terminator")
        chunk = self._data[self._at:end]
        self._at = end + 1
        try:
            return chunk.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise BlobFormatError(
                f"SMILE long string is not utf-8: {exc}") from exc

    # -- values -----------------------------------------------------------

    def _value(self, token: int, depth: int) -> Any:
        if depth > MAX_DEPTH:
            raise BlobFormatError("SMILE document nests too deeply")
        if token == TOKEN_START_OBJECT:
            return self._object(depth + 1)
        if token == TOKEN_START_ARRAY:
            return self._array(depth + 1)
        if 0x01 <= token <= 0x1F:
            return self._values.get(token - 1)
        if 0x30 <= token <= 0x33:
            return self._values.get(((token & 0x03) << 8) | self._byte())
        if token == TOKEN_LITERAL_EMPTY_STRING:
            return ""
        if token == TOKEN_LITERAL_NULL:
            return None
        if token == TOKEN_LITERAL_FALSE:
            return False
        if token == TOKEN_LITERAL_TRUE:
            return True
        if token in (TOKEN_INT_32, TOKEN_INT_64):
            return _zigzag(self._vint())
        if token == TOKEN_FLOAT_32:
            return struct.unpack(">f", self._bits(5, 4))[0]
        if token == TOKEN_FLOAT_64:
            return struct.unpack(">d", self._bits(10, 8))[0]
        if 0x40 <= token <= 0x7F:
            return self._shared_string(self._ascii_length(token))
        if 0x80 <= token <= 0xBF:
            return self._shared_string(self._unicode_length(token))
        if 0xC0 <= token <= 0xDF:
            return _zigzag(token & 0x1F)
        if 0xE0 <= token <= 0xE7:
            return self._long_text()
        if 0xE8 <= token <= 0xEB:
            return self._binary_7bit()
        if token == TOKEN_RAW_BINARY:
            return self._take(self._vint())
        raise BlobFormatError(f"SMILE value token 0x{token:02X} is reserved")

    @staticmethod
    def _ascii_length(token: int) -> int:
        return (token & 0x1F) + (1 if token < 0x60 else 33)

    @staticmethod
    def _unicode_length(token: int) -> int:
        return (token & 0x1F) + (2 if token < 0xA0 else 34)

    def _shared_string(self, length: int) -> str:
        text = self._text(length)
        if length <= MAX_SHARED_LENGTH:
            self._values.add(text)
        return text

    def _bits(self, septets: int, byte_width: int) -> bytes:
        """A float written as seven bits a byte, most significant group first."""
        value = 0
        for byte in self._take(septets):
            value = (value << 7) | (byte & 0x7F)
        return (value & ((1 << (byte_width * 8)) - 1)).to_bytes(byte_width, "big")

    def _binary_7bit(self) -> bytes:
        """Seven bytes of payload per eight bytes on the wire.

        The tail is not a truncated version of the same loop, it is a
        different arithmetic, and Elasticsearch puts a writer uuid in every
        FileInfo record so this path runs on every real document. Getting the
        tail wrong leaves the cursor between tokens and turns the rest of the
        file list into nonsense.
        """
        total = self._vint()
        if total < 0 or total > len(self._data):
            raise BlobFormatError("SMILE binary claims more bytes than exist")
        out = bytearray()
        while len(out) + 7 <= total:
            value = 0
            for byte in self._take(8):
                value = (value << 7) | (byte & 0x7F)
            out += value.to_bytes(7, "big")
        remaining = total - len(out)
        if remaining > 0:
            septets = self._take(remaining + 1)
            value = septets[0]
            for position in range(1, remaining):
                value = (value << 7) + septets[position]
                out.append((value >> (7 - position)) & 0xFF)
            value = (value << 7) + septets[remaining]
            out.append(value & 0xFF)
        return bytes(out)

    # -- containers -------------------------------------------------------

    def _array(self, depth: int) -> List[Any]:
        out: List[Any] = []
        while True:
            token = self._byte()
            if token == TOKEN_END_ARRAY:
                return out
            out.append(self._value(token, depth))

    def _object(self, depth: int) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        while True:
            token = self._byte()
            if token == TOKEN_END_OBJECT:
                return out
            key = self._key(token)
            out[key] = self._value(self._byte(), depth)

    def _key(self, token: int) -> str:
        if token == TOKEN_LITERAL_EMPTY_STRING:
            return ""
        if 0x30 <= token <= 0x33:
            return self._names.get(((token & 0x03) << 8) | self._byte())
        if token == TOKEN_LONG_KEY_UNICODE:
            name = self._long_text()
            self._names.add(name)
            return name
        if 0x40 <= token <= 0x7F:
            return self._names.get(token - 0x40)
        if 0x80 <= token <= 0xBF:
            return self._shared_key((token & 0x3F) + 1)
        if 0xC0 <= token <= 0xF7:
            return self._shared_key((token & 0x3F) + 2)
        raise BlobFormatError(f"SMILE key token 0x{token:02X} is reserved")

    def _shared_key(self, length: int) -> str:
        name = self._text(length)
        self._names.add(name)
        return name
