import numpy as np

from scanner_pipeline.stages.densify import (consistency_count, lidar_scale, neighbours, normals_from_points, unproject,
                                             voxel_fuse, windows, with_context)


def cam(cx_world: float):
    """Camera at (cx_world, 0, 0) looking down +Z (world == camera axes)."""
    w2c = np.eye(4)
    w2c[0, 3] = -cx_world
    return w2c


K = np.array([[100.0, 0, 32], [0, 100.0, 24], [0, 0, 1]])


def plane_depth(z=2.0, h=48, w=64):
    return np.full((h, w), z, np.float32)


def test_unproject_and_consistency():
    d = plane_depth()
    c0, c1 = cam(0.0), cam(0.1)
    P = unproject(d, K, np.linalg.inv(c0))
    assert np.allclose(P[:, 2], 2.0)
    # a neighbour seeing the same plane agrees; one seeing a plane 20% nearer does not
    good = consistency_count(P, [(plane_depth(), K, c1)], 0.03)
    bad = consistency_count(P, [(plane_depth(1.6), K, c1)], 0.03)
    assert good.mean() > 0.7 and bad.sum() == 0
    # points projecting outside the neighbour's image get no vote
    far = consistency_count(P, [(plane_depth(), K, cam(5.0))], 0.03)
    assert far.sum() == 0


def test_normals_face_camera():
    d = plane_depth()
    c2w = np.linalg.inv(cam(0.0))
    P = unproject(d, K, c2w)
    n = normals_from_points(P, 48, 64, c2w).reshape(48, 64, 3)[5:-5, 5:-5].reshape(-1, 3)
    assert np.allclose(n, [0, 0, -1], atol=1e-4)


def test_voxel_fuse_averages():
    P = np.array([[0.001, 0.001, 0.001], [0.003, 0.003, 0.003], [0.5, 0.5, 0.5]], np.float32)
    C = np.array([[1, 0, 0], [0, 0, 1], [0, 1, 0]], np.float32)
    N = np.array([[0, 0, 1], [0, 0, 1], [1, 0, 0]], np.float32)
    Pf, Cf, Nf, cnt = voxel_fuse(P, C, N, 0.01)
    assert len(Pf) == 2 and sorted(cnt.tolist()) == [1, 2]
    i = int(np.argmax(cnt))
    assert np.allclose(Cf[i], [0.5, 0, 0.5]) and np.allclose(Pf[i], 0.002)


def test_lidar_scale_recovers_factor():
    da3 = plane_depth(2.0, 378, 504)
    lidar = np.full((192, 256), 2.2, np.float32)
    lidar[:10] = 0
    st = lidar_scale(da3, lidar)
    assert abs(st["scale"] - 1.1) < 1e-4 and st["abs_rel_after"] < 1e-4 and st["abs_rel_before"] > 0.08
    assert lidar_scale(da3, None) is None


def test_windows_and_context():
    centers = np.array([[i * 0.2, 0, 0] for i in range(10)] + [[0.05, 0, 0]], float)  # view 10 revisits view 0
    dirs = np.tile([0, 0, 1.0], (11, 1))
    nb = neighbours(centers, dirs, 3, 70)
    assert 10 in nb[0] and 0 in nb[10]
    w = windows(11, 4)
    assert w[0] == [0, 1, 2, 3] and w[-1] == [8, 9, 10]
    ctx = with_context(w[0], nb, 2)
    assert ctx[:4] == [0, 1, 2, 3] and 10 in ctx[4:]
    # views looking the opposite way are never neighbours
    dirs2 = dirs.copy()
    dirs2[10] = [0, 0, -1]
    assert 10 not in neighbours(centers, dirs2, 3, 70)[0]
