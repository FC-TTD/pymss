"""Preserve audio channels when using the mono-configured Bandit core."""

from pymss_core.modules.bandit.core.model import MultiMaskMultiSourceBandSplitRNNSimple


class ChannelIndependentBandit(MultiMaskMultiSourceBandSplitRNNSimple):
    """Run each channel as mono, then restore the chunk/source/channel axes.

    pymss-core 0.1.8 through 0.1.14 flatten channels when estimating masks but multiply
    those masks against an unflattened spectrogram. Feeding explicit mono
    batches avoids broadcasting channels into the chunk batch dimension.
    Inheritance keeps checkpoint parameter names and shapes unchanged.
    """

    def forward(self, batch):
        batch_size, channels, length = batch.shape
        estimates = super().forward(batch.reshape(batch_size * channels, 1, length))
        return estimates.reshape(batch_size, channels, estimates.shape[1], length).permute(0, 2, 1, 3)
