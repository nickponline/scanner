"""Frame conversions (CONTRACTS.md §1)."""
from __future__ import annotations

import math

import numpy as np

# ARKit world (Y up) -> Z-up world: (x, y, z) -> (x, -z, y)
YUP_TO_ZUP = np.array([[1, 0, 0, 0], [0, 0, -1, 0], [0, 1, 0, 0], [0, 0, 0, 1]], dtype=np.float64)
# OpenGL camera (x right, y up, -z fwd) <-> OpenCV camera (x right, y down, +z fwd)
GL_TO_CV = np.diag([1.0, -1.0, -1.0, 1.0])


def arkit_to_zup_cv(T_arkit: np.ndarray) -> np.ndarray:
    """ARKit camera->world (GL cam, Y-up world) -> OpenCV camera->world in Z-up world."""
    return YUP_TO_ZUP @ T_arkit @ GL_TO_CV


def rot_z(theta: float) -> np.ndarray:
    c, s = math.cos(theta), math.sin(theta)
    R = np.eye(4)
    R[:2, :2] = [[c, -s], [s, c]]
    return R


def bearing_of(vec_xy, plus_y_bearing_deg: float) -> float:
    """Compass bearing of a horizontal vector given the bearing of the frame's +Y axis."""
    x, y = vec_xy
    return (plus_y_bearing_deg + math.degrees(math.atan2(x, y))) % 360.0


def rotation_angle_deg(R1: np.ndarray, R2: np.ndarray) -> float:
    R = R1.T @ R2
    return math.degrees(math.acos(max(-1.0, min(1.0, (np.trace(R) - 1) / 2))))


def qvec_to_R(q) -> np.ndarray:
    w, x, y, z = q
    return np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)],
        [2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)],
        [2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)],
    ])
