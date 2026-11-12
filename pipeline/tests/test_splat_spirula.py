import json

import numpy as np
import pytest

from scanner_pipeline.stages.splat_spirula import read_ply, to_world


def _ply(path, n=5):
    props = ["x", "y", "z", "nx", "ny", "nz", "f_dc_0", "f_dc_1", "f_dc_2", "opacity", "scale_0", "scale_1", "scale_2",
             "rot_0", "rot_1", "rot_2", "rot_3"]
    a = np.zeros(n, np.dtype([(p, "<f4") for p in props]))
    a["x"] = np.arange(n)
    a["scale_0"] = -3
    hdr = f"ply\nformat binary_little_endian 1.0\nelement vertex {n}\n" + "".join(f"property float {p}\n" for p in props) + "end_header\n"
    path.write_bytes(hdr.encode() + a.tobytes())
    return props


def _tf(path, scale, t, R=None):
    R = R or [[1, 0, 0], [0, 1, 0], [0, 0, 1]]
    path.write_text(json.dumps({"world_from_train": {"scale": scale, "translation": t, "rotation": {"matrix_3x3": R}}}))


def test_read_and_identity(tmp_path):
    props = _ply(tmp_path / "s.ply")
    a, p = read_ply(tmp_path / "s.ply")
    assert p == props and len(a) == 5
    _tf(tmp_path / "t.json", 1, [0, 0, 0])
    assert to_world(a, p, tmp_path / "t.json") is a


def test_scale_translate(tmp_path):
    _ply(tmp_path / "s.ply")
    a, p = read_ply(tmp_path / "s.ply")
    _tf(tmp_path / "t.json", 2.0, [1, 0, 0])
    w = to_world(a, p, tmp_path / "t.json")
    assert np.allclose(w["x"], 2 * np.arange(5) + 1)
    assert np.allclose(w["scale_0"], -3 + np.log(2))


def test_rotation_refused(tmp_path):
    _ply(tmp_path / "s.ply")
    a, p = read_ply(tmp_path / "s.ply")
    _tf(tmp_path / "t.json", 1, [0, 0, 0], [[0, -1, 0], [1, 0, 0], [0, 0, 1]])
    with pytest.raises(RuntimeError):
        to_world(a, p, tmp_path / "t.json")
