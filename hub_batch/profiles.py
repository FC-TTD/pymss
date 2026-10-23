"""Published legacy profiles and pinned complete-result implementations."""
from pathlib import Path
from .provider import ProfileManifest, ProfileRegistry


def default_profiles():
    registry = ProfileRegistry([
        ProfileManifest("dialogue-vocal", "2026-09-20.v1", "duality-two-stems", outputs=("Vocals", "Instrumental"), models=("melband_roformer_instvox_duality_v2.ckpt",)),
        ProfileManifest("instrumental-separation", "2026-09-20.v1", "instrumental-only", outputs=("Instrumental",), models=("mel_band_roformer_instrumental_becruily.ckpt",)),
        ProfileManifest("dialogue-and-instrumental", "2026-09-20.v1", "dedicated-me", outputs=("dialogue", "instrumental"), models=("melband_roformer_instvox_duality_v2.ckpt", "mel_band_roformer_instrumental_becruily.ckpt")),
        ProfileManifest("dialogue-and-instrumental-dedicated", "2026-09-20.v1", "dedicated-me", outputs=("dialogue", "instrumental"), models=("melband_roformer_instvox_duality_v2.ckpt", "mel_band_roformer_instrumental_becruily.ckpt")),
    ])
    packaged = ProfileRegistry.from_json(Path(__file__).with_name("profiles-2026-10-23.json"))
    for manifest in packaged.list():
        registry.register(manifest)
    return registry
