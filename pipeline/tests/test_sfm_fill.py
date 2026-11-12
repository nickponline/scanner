"""fill_unregistered: frames COLMAP missed get drift-corrected ARKit poses; COLMAP poses are left untouched."""
import numpy as np
import pycolmap
from scipy.spatial.transform import Rotation as R

from scanner_pipeline.geom import arkit_to_zup_cv
from scanner_pipeline.stages.sfm import align_to_arkit, fill_unregistered


def _umeyama(X, Y):
    mx, my = X.mean(0), Y.mean(0)
    Xc, Yc = X - mx, Y - my
    U, S, Vt = np.linalg.svd(Yc.T @ Xc / len(X))
    D = np.eye(3)
    D[2, 2] = np.sign(np.linalg.det(U @ Vt))
    Rm = U @ D @ Vt
    s = (S * np.diag(D)).sum() / Xc.var(0).sum()
    return s, Rm, my - s * Rm @ mx


def test_fill_corrects_arkit_drift():
    rng = np.random.default_rng(1)
    frames, truth = [], {}
    for i in range(300):
        t, ang = i * 0.2, i * 0.03
        Tt = np.eye(4)
        Tt[:3, :3] = R.from_euler("y", ang).as_matrix()
        Tt[:3, 3] = [4 * np.cos(ang), 1.4, 4 * np.sin(ang)]
        D = np.eye(4)  # ~1% VIO drift
        D[:3, :3] = R.from_euler("y", 0.0002 * i).as_matrix()
        D[:3, 3] = [0.0008 * i, 0.0002 * i, -0.0005 * i]
        kind = "keyframe" if i % 4 else "hero"
        img = f"{'kf' if kind == 'keyframe' else 'hero'}/{i:06d}.jpg"
        frames.append({"name": f"{i:06d}", "kind": kind, "seg": 0, "t": t, "w": 640, "h": 480,
                       "K": [500, 500, 320, 240], "T_arkit": (D @ Tt).tolist(), "img": img})
        truth[img] = arkit_to_zup_cv(Tt)
    sub = [f for i, f in enumerate(frames) if not (100 <= i < 160) and rng.random() > 0.2]
    rec = pycolmap.Reconstruction()
    for cid in (1, 2):
        rec.add_camera_with_trivial_rig(pycolmap.Camera(model="PINHOLE", width=640, height=480,
                                                        params=[500, 500, 320, 240], camera_id=cid))
    for k, f in enumerate(sub):
        w2c = np.linalg.inv(truth[f["img"]])
        rec.add_image_with_trivial_frame(
            pycolmap.Image(name=f["img"], camera_id=1 if f["kind"] == "keyframe" else 2, image_id=k + 1),
            pycolmap.Rigid3d(pycolmap.Rotation3d(w2c[:3, :3]), w2c[:3, 3]))
    rec.transform(pycolmap.Sim3d(2.5, pycolmap.Rotation3d(R.from_euler("xyz", [0.3, -0.2, 1.0]).as_quat()),
                                 np.array([5.0, -3, 2])))
    arkit_c = {f["img"]: arkit_to_zup_cv(np.asarray(f["T_arkit"]))[:3, 3] for f in frames}
    _, _, dropped = align_to_arkit(rec, arkit_c, 0.72, 0.36)
    assert dropped == 0
    assert fill_unregistered(rec, frames) == len(frames) - len(sub)
    assert rec.num_reg_images() == len(frames)
    names = [im.name for im in rec.images.values()]
    A = np.array([im.projection_center() for im in rec.images.values()])
    B = np.array([truth[n][:3, 3] for n in names])
    s, Rm, t = _umeyama(A, B)
    err = np.linalg.norm((s * (Rm @ A.T)).T + t - B, axis=1)
    assert np.percentile(err, 95) < 0.02
    ark = np.array([arkit_c[n] for n in names])
    s2, R2, t2 = _umeyama(ark, B)
    assert np.percentile(np.linalg.norm((s2 * (R2 @ ark.T)).T + t2 - B, axis=1), 95) > 0.1  # raw ARKit is worse
