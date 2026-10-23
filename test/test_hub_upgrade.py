"""CPU regressions for the deployed customizations across the 2.1.5 upgrade."""
from collections import defaultdict
import logging

import numpy as np
import pytest
import torch
import yaml

from hub_runtime.state import model_spec, selected_metadata
from pymss.server.config import ServerConfig
from pymss.separator import MSSeparator, _load_state_dict
from pymss.utils import get_model_from_config


def tiny_config():
    return {
        "model": {
            "dim": 16, "depth": 1, "stereo": True, "num_stems": 1,
            "time_transformer_depth": 1, "freq_transformer_depth": 1,
            "num_bands": 8, "dim_head": 8, "heads": 2,
            "stft_n_fft": 256, "stft_hop_length": 64, "stft_win_length": 256,
            "attn_dropout": 0.0, "ff_dropout": 0.0, "flash_attn": False,
        },
        "audio": {"sample_rate": 44100, "chunk_size": 4096},
        "training": {"instruments": ["vocals"], "target_instrument": None},
        "inference": {"batch_size": 1, "num_overlap": 2},
    }


@pytest.mark.parametrize("shared_bias", [False, True])
def test_legacy_mel_config_loads_with_core_without_changing_weights(tmp_path, shared_bias):
    config = tiny_config()
    path = tmp_path / "model.yaml"
    path.write_text(yaml.safe_dump(config))
    torch.manual_seed(123)
    original, _ = get_model_from_config("mel_band_roformer", path)
    # The deployed MelBand constructor accepted this field but never used it.
    config["model"]["use_shared_bias"] = shared_bias
    path.write_text(yaml.safe_dump(config))
    torch.manual_seed(123)
    upgraded, returned_config = get_model_from_config("mel_band_roformer", path)
    assert returned_config.model.use_shared_bias is shared_bias
    assert original.state_dict().keys() == upgraded.state_dict().keys()
    for key, expected in original.state_dict().items():
        torch.testing.assert_close(upgraded.state_dict()[key], expected)


def test_checkpoint_shape_overrides_preserved_with_new_core(tmp_path):
    config = tiny_config()
    path = tmp_path / "model.yaml"
    path.write_text(yaml.safe_dump(config))
    reference, _ = get_model_from_config("mel_band_roformer", path)
    checkpoint = tmp_path / "custom_vocals.ckpt"
    torch.save({"state_dict": reference.state_dict()}, checkpoint)
    config["model"].update(dim=32, depth=2, num_stems=2)
    config["training"]["instruments"] = ["vocals", "instrumental"]
    path.write_text(yaml.safe_dump(config))
    before = torch.backends.cudnn.benchmark
    with MSSeparator("mel_band_roformer", checkpoint, path, device="cpu",
                     store_dirs=str(tmp_path / "outputs"), logger=logging.getLogger(__name__)) as separator:
        assert separator.config.model.dim == 16
        assert separator.config.model.depth == 1
        assert separator.config.training.instruments == ["vocals"]
        for key, expected in reference.state_dict().items():
            torch.testing.assert_close(separator.model.state_dict()[key], expected)
        result = separator.separate(np.zeros((2, 4096), dtype=np.float32), pbar=False)
        assert set(result) == {"vocals"}
        assert np.isfinite(result["vocals"]).all()
    assert torch.backends.cudnn.benchmark == before


def test_training_metadata_checkpoint_still_loads_safely(tmp_path):
    path = tmp_path / "weights.ckpt"
    torch.save({"state_dict": {"weight": torch.ones(2)},
                "optimizer": defaultdict(dict, {"state": {}})}, path)
    state = _load_state_dict("mel_band_roformer", path, "cpu")
    torch.testing.assert_close(state["weight"], torch.ones(2))


def test_missing_config_can_be_selected_without_catalog_instruments(tmp_path):
    config = ServerConfig(model="BS-Roformer-Resurrection.ckpt", model_dir=str(tmp_path))
    loaded = selected_metadata(model_spec(config, config.model))
    assert loaded.model_id == config.model
    assert loaded.separator is None
    assert loaded.instruments == ()


def test_vr_selection_uses_native_schema_without_loading_weights(tmp_path):
    config = ServerConfig(model="1_HP-UVR.pth", model_dir=str(tmp_path))
    loaded = selected_metadata(model_spec(config, config.model))
    assert loaded.instruments == ("Instrumental", "Vocals")
    assert loaded.sample_rate == 44100
    assert loaded.separator is None
