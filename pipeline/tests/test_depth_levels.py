import numpy as np

from scanner_pipeline.stages.depth import mode_z


def test_mode_z_few_bins():
    # regression: two-bin histograms crashed (np.convolve "same" grows the array past the bin edges)
    assert abs(mode_z(np.array([2.5, 2.51, 2.52, 2.53] * 30)) - 2.51) < 0.02


def test_mode_z_peak():
    z = np.r_[np.random.default_rng(0).normal(2.4, 0.004, 2000), np.random.default_rng(1).uniform(0, 3, 300)]
    assert abs(mode_z(z) - 2.4) < 0.02
    assert mode_z(np.array([])) is None
