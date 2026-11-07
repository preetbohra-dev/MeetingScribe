"""The numpy stand-ins Windows uses (Smart App Control blocks scipy and torch there)
must match the libraries the Mac uses. Comparisons skip where the library is missing.

    python -m pytest tests
"""

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import meeting_scribe as ms  # noqa: E402

rng = np.random.default_rng(7)


@pytest.mark.parametrize("sr_in,sr_out", [(48000, 16000), (44100, 16000), (24000, 16000),
                                          (16000, 48000), (22050, 16000)])
@pytest.mark.parametrize("n", [1, 137, 4800, 44100])
def test_resample_matches_scipy(sr_in, sr_out, n):
    signal = pytest.importorskip("scipy.signal")
    import math
    g = math.gcd(sr_in, sr_out)
    x = rng.standard_normal(n)
    want = signal.resample_poly(x, sr_out // g, sr_in // g)
    got = ms.resample_poly_np(x, sr_out // g, sr_in // g)
    assert got.shape == want.shape
    np.testing.assert_allclose(got, want, atol=1e-9)


def test_resample_keeps_a_tone():
    t = np.arange(48000) / 48000
    y = ms.resample_poly_np(np.sin(2 * np.pi * 440 * t), 1, 3)
    assert len(y) == 16000
    ref = np.sin(2 * np.pi * 440 * np.arange(16000) / 16000)
    assert np.abs(y[200:-200] - ref[200:-200]).max() < 1e-3


def blobs(k, per, dim=192, spread=0.35):
    centers = rng.standard_normal((k, dim))
    x = np.concatenate([c + spread * rng.standard_normal((per, dim)) for c in centers])
    return x / np.linalg.norm(x, axis=1, keepdims=True), np.repeat(np.arange(k), per)


def same_partition(a, b):
    pairs = set(zip(a.tolist(), b.tolist()))
    return len(pairs) == len(set(a.tolist())) == len(set(b.tolist()))


@pytest.mark.parametrize("t", [0.3, 0.6, 0.9])
def test_average_linkage_matches_scipy(t):
    hierarchy = pytest.importorskip("scipy.cluster.hierarchy")
    for k, per, spread in ((1, 5, 0.3), (3, 40, 0.35), (6, 25, 0.6), (4, 150, 0.5)):
        emb, _ = blobs(k, per, spread=spread)
        want = hierarchy.fcluster(hierarchy.linkage(emb, "average", metric="cosine"), t=t,
                                  criterion="distance")
        assert same_partition(ms.average_linkage_np(emb, t), want)


def test_average_linkage_finds_voices():
    emb, truth = blobs(4, 60)
    got = ms.average_linkage_np(emb, 0.6)
    assert same_partition(got, truth)
    assert ms.average_linkage_np(emb[:1], 0.6).tolist() == [0]


def test_fbank_matches_torchaudio():
    kaldi = pytest.importorskip("torchaudio.compliance.kaldi")
    import torch
    x = (0.1 * rng.standard_normal(32000)).astype(np.float32)
    want = kaldi.fbank(torch.from_numpy(x * 32768).unsqueeze(0), num_mel_bins=80, frame_length=25,
                       frame_shift=10, dither=0.0, sample_frequency=16000, window_type="hamming",
                       use_energy=False).numpy()
    want = want - want.mean(axis=0)
    np.testing.assert_allclose(ms.kaldi_fbank(x), want, atol=2e-3)


def test_fbank_shape():
    f = ms.kaldi_fbank(np.zeros(32000, np.float32) + 1e-3 * rng.standard_normal(32000))
    assert f.shape == (198, 80) and f.dtype == np.float32
    np.testing.assert_allclose(f.mean(axis=0), 0, atol=1e-4)
