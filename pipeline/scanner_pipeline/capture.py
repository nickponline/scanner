"""Readers/writers for the capture package (CONTRACTS.md §2)."""
from __future__ import annotations

import json
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import numpy as np

MAGIC = b"SDP1"
_HDR = struct.Struct("<4sIdHHI")  # magic, i, t, w, h, n


@dataclass
class DepthRecord:
    i: int
    t: float
    depth: np.ndarray  # float32 metres (h, w)
    conf: np.ndarray  # uint8 0/1/2 (h, w)


def encode_depth_record(i: int, t: float, depth: np.ndarray, conf: np.ndarray) -> bytes:
    h, w = depth.shape
    payload = zlib.compress(depth.astype("<f4").tobytes() + conf.astype(np.uint8).tobytes(), 6)
    return _HDR.pack(MAGIC, i, t, w, h, len(payload)) + payload


def read_depth_bin(path: Path, decode: bool = True) -> Iterator[DepthRecord]:
    with open(path, "rb") as f:
        while True:
            hdr = f.read(_HDR.size)
            if len(hdr) < _HDR.size:
                return
            magic, i, t, w, h, n = _HDR.unpack(hdr)
            if magic != MAGIC:
                raise ValueError(f"bad depth record magic {magic!r} in {path}")
            payload = f.read(n)
            if not decode:
                yield DepthRecord(i, t, None, None)  # type: ignore[arg-type]
                continue
            raw = zlib.decompress(payload)
            d = np.frombuffer(raw[: w * h * 4], "<f4").reshape(h, w)
            c = np.frombuffer(raw[w * h * 4:], np.uint8).reshape(h, w)
            yield DepthRecord(i, t, d, c)


def read_jsonl(path: Path) -> list[dict]:
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def arkit_T(frame: dict) -> np.ndarray:
    """4x4 camera->world (ARKit/OpenGL camera, ARKit world) from column-major list."""
    return np.asarray(frame["T"], dtype=np.float64).reshape(4, 4).T
