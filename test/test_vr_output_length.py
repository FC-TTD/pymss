from types import SimpleNamespace

import librosa
import numpy as np

from pymss.modules.vocal_remover.uvr_lib_v5.spec_utils import spectrogram_to_wave


def test_centered_vr_inverse_keeps_nonzero_tail_after_partial_hop():
    sample_rate = 44100
    frames = sample_rate * 6
    hop = 512
    time = np.arange(frames, dtype=np.float32) / sample_rate
    source = np.stack((0.2 * np.sin(2 * np.pi * 440 * time), 0.2 * np.sin(2 * np.pi * 660 * time)))
    padded = np.pad(source, ((0, 0), (0, hop)))
    spec = np.stack([librosa.stft(channel, n_fft=768, hop_length=hop) for channel in padded])
    model_params = SimpleNamespace(param={"reverse": False, "mid_side": False, "mid_side_b2": False})

    recovered = spectrogram_to_wave(spec, hop_length=hop, mp=model_params, is_v51_model=False, length=frames)

    assert recovered.shape == source.shape
    np.testing.assert_allclose(recovered[:, -408:], source[:, -408:], atol=2e-5)
