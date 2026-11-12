import numpy as np

from scanner_pipeline.capture import encode_depth_record, read_depth_bin
from scanner_pipeline.stages.publish import rotate_camera, upright_k


def project(K, c2w, X):
    w2c = np.linalg.inv(c2w)
    p = w2c[:3, :3] @ X + w2c[:3, 3]
    return np.array([K[0] * p[0] / p[2] + K[2], K[1] * p[1] / p[2] + K[3]])


def test_rotate_camera_matches_rot90():
    rng = np.random.default_rng(0)
    W, H = 64, 48
    K = [50.0, 52.0, 31.0, 23.5]
    c2w = np.eye(4)
    c2w[:3, 3] = [0.1, -0.2, 0.3]
    img = rng.integers(0, 255, (H, W), dtype=np.uint8)
    X = np.array([0.15, -0.1, 2.0])
    u, v = project(K, c2w, X)
    for k in range(4):
        K2, c2, W2, H2 = rotate_camera(K, c2w, W, H, k)
        u2, v2 = project(K2, c2, X)
        rot = np.rot90(img, k)
        assert rot.shape == (H2, W2)
        # same pixel content at the projected locations
        assert rot[int(round(v2)), int(round(u2))] == img[int(round(v)), int(round(u))]


def test_upright_portrait():
    # camera rolled 90 deg: image x axis points world down
    c2w = np.eye(4)
    c2w[:3, :3] = np.array([[0, 1, 0], [0, 0, 1], [1, 0, 0]]).T  # columns: x_cam, y_cam, z_cam in world
    c2w[:3, 0] = [0, 0, -1]   # image right = world down
    c2w[:3, 1] = [1, 0, 0]    # image down = world +x
    c2w[:3, 2] = [0, 1, 0]    # looking +y
    k = upright_k(c2w)
    K2, c2, _, _ = rotate_camera([1, 1, 0, 0], c2w, 10, 10, k)
    assert np.allclose(c2[:3, 1], [0, 0, -1])  # image down == world down


def test_depth_roundtrip(tmp_path):
    d = np.random.rand(192, 256).astype(np.float32)
    c = (np.random.rand(192, 256) * 3).astype(np.uint8)
    p = tmp_path / "d.bin"
    p.write_bytes(encode_depth_record(7, 1.5, d, c) + encode_depth_record(8, 1.6, d, c))
    recs = list(read_depth_bin(p))
    assert [r.i for r in recs] == [7, 8] and np.array_equal(recs[0].depth, d) and np.array_equal(recs[1].conf, c)
