import numpy as np
import pycolmap

from scanner_pipeline.geom import arkit_to_zup_cv
from scanner_pipeline.stages.sfm import align_to_arkit, arkit_model, residuals


def _frames(n=60):
    rng = np.random.default_rng(0)
    out = []
    for i in range(n):
        T = np.eye(4)
        a = i / n * 2 * np.pi
        T[:3, 3] = [3 * np.cos(a), 1.4 + 0.05 * rng.standard_normal(), 2 * np.sin(a)]  # ARKit Y-up loop
        c, s = np.cos(a), np.sin(a)
        T[:3, :3] = [[c, 0, s], [0, 1, 0], [-s, 0, c]]
        out.append({"kind": "keyframe", "w": 640, "h": 480, "K": [500, 500, 320, 240], "img": f"kf/{i:04d}.jpg",
                    "T_arkit": T.tolist()})
    return out


def test_align_recovers_sim3_and_drops_outliers(tmp_path):
    frames = _frames()
    rec = arkit_model(frames, tmp_path / "m")
    arkit_c = {f["img"]: arkit_to_zup_cv(np.asarray(f["T_arkit"]))[:3, 3] for f in frames}
    # arbitrary SfM gauge + 4 catastrophic frames + 1 unregistered frame
    rec.transform(pycolmap.Sim3d(0.39, pycolmap.Rotation3d(np.array([0.1, 0.7, -0.2, 0.68]) / np.linalg.norm([0.1, 0.7, -0.2, 0.68])), np.array([5.0, -2, 1])))
    ims = sorted(rec.images.values(), key=lambda i: i.name)
    for im in ims[10:14]:
        p = im.cam_from_world()
        im.frame.rig_from_world = pycolmap.Rigid3d(p.rotation, p.translation + np.array([3.0, 0, 0]))
    rec.deregister_frame(ims[20].frame_id)
    sim, res, n_bad = align_to_arkit(rec, arkit_c, 0.3)
    assert n_bad == 4
    assert rec.num_reg_images() == 55
    assert max(res.values()) < 1e-3
    assert len(res) == 55 and residuals(rec, arkit_c) == res
    assert abs(sim.scale - 1 / 0.39) < 1e-3
