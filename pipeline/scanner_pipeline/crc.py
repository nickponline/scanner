"""CRC32C (Castagnoli), base64 big-endian, identical to GCS object crc32c metadata."""
import base64
import struct

_POLY = 0x82F63B78
_TABLE = []
for _i in range(256):
    _c = _i
    for _ in range(8):
        _c = (_c >> 1) ^ _POLY if _c & 1 else _c >> 1
    _TABLE.append(_c)


def crc32c(data: bytes, crc: int = 0) -> int:
    try:
        import google_crc32c  # fast path (dependency of google-cloud-storage)

        return google_crc32c.extend(crc, data)
    except ImportError:
        pass
    crc ^= 0xFFFFFFFF
    for b in data:
        crc = _TABLE[(crc ^ b) & 0xFF] ^ (crc >> 8)
    return crc ^ 0xFFFFFFFF


def crc32c_b64(data: bytes) -> str:
    return base64.b64encode(struct.pack(">I", crc32c(data))).decode()
