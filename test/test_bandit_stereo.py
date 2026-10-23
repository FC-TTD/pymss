"""Bandit must keep chunk batches separate from independently processed channels."""

import numpy as np
import pytest
import torch
import yaml

from pymss_core import get_model_from_config as core_get_model_from_config

from pymss.utils import demix_track, get_model_from_config


@pytest.fixture
def bandit_config_path(tmp_path):
    # Keep the deployed FX chunk/overlap and exercise its exact failing lengths.
    # A tiny randomly initialized network needs no checkpoint or GPU.
    config_data = {
        "audio": {"chunk_size": 264600, "sample_rate": 44100, "num_channels": 2},
        "model": {
            "in_channel": 1,
            "stems": ["speech", "music", "effects"],
            "band_specs": "musical",
            "n_bands": 4,
            "n_fft": 32,
            "win_length": 32,
            "hop_length": 16,
            "emb_dim": 4,
            "mlp_dim": 4,
            "rnn_dim": 4,
            "n_sqm_modules": 0,
        },
        "training": {"instruments": ["Speech", "Music", "Effects"], "target_instrument": None, "use_amp": True},
        "inference": {"batch_size": 1, "num_overlap": 4},
    }
    path = tmp_path / "bandit.yaml"
    path.write_text(yaml.safe_dump(config_data))
    return path


@pytest.mark.parametrize("batch_size", [1, 2])
@pytest.mark.parametrize("length", [330750, 500003])
def test_bandit_stereo_preserves_chunk_batch_and_tail(bandit_config_path, batch_size, length):
    torch.manual_seed(17)
    model, config = get_model_from_config("bandit", bandit_config_path)
    model.eval()
    config.inference.batch_size = batch_size
    time = torch.arange(length, dtype=torch.float32) / 44100
    mix = torch.stack((torch.sin(time * 1200), torch.cos(time * 700)))

    stereo = demix_track(config, model, mix, "cpu")

    assert set(stereo) == {"Speech", "Music", "Effects"}
    for channel in range(2):
        mono = demix_track(config, model, mix[channel : channel + 1], "cpu")
        for source in stereo:
            assert stereo[source].shape == (2, length)
            assert np.isfinite(stereo[source]).all()
            np.testing.assert_allclose(stereo[source][channel], mono[source][0], atol=1e-5, rtol=1e-4)


def test_bandit_existing_checkpoint_loads_strictly_and_mono_output_is_unchanged(bandit_config_path):
    original, _ = core_get_model_from_config("bandit", bandit_config_path)
    adapted, config = get_model_from_config("bandit", bandit_config_path)
    adapted.load_state_dict(original.state_dict(), strict=True)
    original.eval()
    adapted.eval()
    mix = torch.randn(1, 1000)

    expected = demix_track(config, original, mix, "cpu")
    actual = demix_track(config, adapted, mix, "cpu", source_indices=[2, 0])

    assert list(actual) == ["Effects", "Speech"]
    for source in actual:
        np.testing.assert_array_equal(actual[source], expected[source])
