# PYMSS 2.1.12 provider integration

Scope: merge upstream `22b2b03eee20cb64c9ec88b07b95fe2e50a7e39e`
into organization `hub`, based on `90ebd6e6cff86e081189c400f15735a2b2a23fe2`.
Upstream source is 2.1.12 with core 0.1.14. Organization main (2.1.7)
and the divergent local main are not integration sources. No production
service deployment, WebUI release, shared-backend migration or Audio Nav change.

## Accepted export contract

Nine profiles add version `2026-10-28.v2`. Published v1 manifests, assets,
DAG bytes and digests remain available unchanged. The new versions reference
those same verified weights/configs/DAGs and bind the configurable export
capabilities plus native channel metadata into their implementation digest.
The discovery envelope remains `2026-10-23.v1`: its schema is additive and
deployed Gateway consumers require that exact version. Capability contracts
are versioned through the new profile versions and digests. Existing Gateway
registration continues to select the pinned v1 profiles; using v2 through
Gateway requires a separate consumer registration change.

Use the existing `options.audio_params` object, on either `/v1/separations`
or `/v1/batches` (both provider HTTP entries):

```json
{
  "profile_id": "fx",
  "profile_version": "2026-10-28.v2",
  "inputs": ["/configured/input-root/source.wav"],
  "options": {
    "audio_params": {"channels": "source", "sample_rate": 44100, "wav_bit_depth": "PCM_16"}
  }
}
```

Omitting options retains Dayan's WAV, 48000 Hz, PCM_24, two-channel export.
`channels` accepts integers 1/2 or `"source"`; source preserves mono/stereo
input independently for each batch file. Source policy rejects inputs with
more than two channels before inference, rather than inventing a native
multichannel capability. Explicit 1/2 can use upstream's source downmix.
`sample_rate` accepts integers 8000 through 192000 Hz; `wav_bit_depth` accepts
PCM_16, PCM_24, PCM_32 or FLOAT (32-bit IEEE float). Booleans, coerced strings,
unknown fields and options on fixed v1 versions are rejected before admission.
WAV is the supported container; no compressed-format capability is claimed.

These are **export** options. Inference runs at the model's native sample
rate/channel configuration. A run-local copy of verified save-node widgets
sets the requested sample rate and encoding; audio_params reaches the saver
for mono averaging or mono-to-stereo duplication. Ordinary graphs/native
model inference continue to preserve upstream mono behavior. FX/Bandit has
a mono model; a stereo source is inferred independently for L and R by
upstream. Duplicating an exported mono channel is not native stereo inference.

The engine resolves options before execution and per-input source policy
before model inference. It validates actual files against that effective
contract (WAV/subtype/rate/channels, nonempty and finite samples, role completeness,
unique contained paths), then records encoding, bits, rate, channels, duration,
size and SHA256. Each completed input also records its resolved audio_params;
requested options survive task persistence, receipts and result recovery.

## Integration decisions

Upstream bounded LRU SeparatorCache, including default max_entries=1 and
failed-unload propagation, replaces the redundant TTD cache patch. Keep
shared ModelCoordinator, single residency, native concurrency and actual
completion protection for cancellation/disconnection. Keep administrator
safe checkpoint loading, MelBand shape inference and use_shared_bias compatibility,
and local configuration protection. Apply catalog target_instrument_override
before shape-derived naming so target semantics win over filename heuristics.
No unsafe generic checkpoint load fallback is introduced.

The full provider Dockerfile installs core 0.1.14 without dependency upgrades.
It installs the new package into an independent target and puts its distribution
metadata before inherited site-packages, preserving Torch/CUDA and SDK 0.3.0.
The running formal image was read-only checked: Torch 2.7.1+cu128, core 0.1.8,
but stale pymss distribution metadata 2.0.6. The two-file native overlay cannot
implement this core upgrade. Original administrator UI assets are retained.

Upstream's new macOS plugin CI is kept as an on-demand workflow; plugin/graph
business tests remain developer verification, not automatic per-PR cloud work.
Existing tag-triggered release behavior is unchanged; this task creates no tag.

## Verification and limits

Verification artifacts are retained under
`/workspace/FC-TTD/.artifacts/pymss-upstream-20261028` on the development host.
The active evidence index is `docs/spec/jobs/2026-10-28-pymss-upstream/tasks.md`.

- Full CPU suite on Python 3.11.13, fixed Torch 2.7.1+cu128/core 0.1.14:
  **863 passed, 15 skipped, 32 subtests passed**. No test deselected.
  Skips cover unavailable platform/accelerator paths. The known safe-checkpoint
  test failure with development Torch 2.11 does not reproduce on this stack.
- Fixed-stack GPU comparison: all nine default profiles pass on both hub/core
  0.1.8 and candidate/core 0.1.14 with the same real weights and stereo source.
  Output names, shapes, duration and defaults match. Waveform RMS differences
  are recorded; sample/byte identity is not promised.
- Candidate GPU: 14 successful runs cover nine defaults, mono preservation,
  explicit mono/stereo, PCM16/24/32/FLOAT, 16/32/44.1/48 kHz, and a 67.5-second
  FX input with a partial tail. The SDK child exits naturally on unload and
  reports an empty process group. All nine profiles also pass a 27-run export
  matrix: mono/source/16 kHz/PCM16,
  stereo-to-mono/44.1 kHz/FLOAT and mono-to-stereo/32 kHz/PCM32. A real HTTP
  mixed mono/stereo batch at 22.05 kHz/PCM16 preserves one/two channels per input;
  actual media, result metadata and hashes match.
- Real-checkpoint Bandit test: core 0.1.14 still returns the wrong batch axis
  for two channels and raises for batch=2/channels=2/500003 samples. The adapter
  returns correct shapes; native mono output exactly matches raw core output.
  Therefore retain ChannelIndependentBandit for direct demix callers, while
  upstream MSSeparator independently handles L/R for mono models.
- Native HTTP API plus actual SDK child/GPU: mono WAV returns 48 kHz PCM24
  mono, unlike the profile default stereo export. Cancelling the ASGI caller
  waits for actual work, active unload returns resource_busy, a model switch
  drains inference, batch cancellation settles, and final unload exits the child.
  This cancellation probe simulates caller cancellation at ASGI, rather than
  claiming a macOS network-disconnect test. A separate final-image probe
  sends an actual long-audio HTTP request over TCP, closes the socket while
  GPU work is active, verifies unload is rejected and a model switch waits,
  then observes successful drain and empty child process group on unload.
- Browser UI: actual inherited administrator assets load, upload a real source,
  produce three FX tracks, play audio and download all three verified stereo
  48 kHz PCM24 WAVs, with no page errors. No UI source or published assets changed.
- Gateway main `7d4cdb7bf9cbe440c9209bb89bc32f0a3f2e000c` interop test rejects
  a v2 discovery-envelope version and passes after preserving the v1 envelope.
  Published v1 manifest digests and options remain exact matches.
- Fixed-stack FFmpeg 4.4.2 exposed upstream fallback rematrixing 3.0 to 2.1 while
  retaining a 3.0 label. Preserving source channels without output channel-count
  constraints fixes the existing reproduction; all 69 layout/downmix tests pass.

Build uses the full `deploy/hub/Dockerfile`, with immutable inputs:

- native base `sha256:a5bb212a097c35e9d99cb6eefd33323aaa82717530992976c282ef61cd9b672a`;
- SDK wheel image `sha256:f8479f79ca1e5a46383f6da78930ae926b7bc7034e866cc290185121ae4ee728`.

Build assertions verify pymss source/metadata 2.1.12, core 0.1.14 and SDK 0.3.0.
The first build and subsequent cached rebuild succeed. No image was promoted,
no formal service was restarted, and no deployment is authorized by this record.

GPU evidence uses the unmanaged development RTX2080Ti
`GPU-18a20e2c-cdff-b380-8a99-82283aea8400`, with a real SDK subprocess and a
fixture admission Node. It does **not** establish real Hub admission or formal
GPU placement acceptance. Peak allocated memory is about 6.21 GiB, but Apollo
peak reserved memory is about 9.83 GiB on both baseline and candidate. An 8 GiB
fixture lease is not a calibrated formal resource budget; deployment requires
separate budget calibration including allocator/context headroom. All task-owned
GPU processes must stop before cleanup. Other sessions' formal instances remain
untouched.

Real Dayan client/UI acceptance remains an explicit delivery gate until its
job-local smoke report receives an independent artifact-consistency verdict.
The user replaced mac-dev with Windows89 after macOS denied accessibility.
The task-launched macOS process and tunnel were stopped and its task-touched
configuration restored to the prior hash. No macOS smoke pass is claimed.

Windows89 acceptance uses the **unchanged installed** Dayan beta11 executable,
SHA256 `4ed76248640c328c609f6cd3c9b6318b61605ef407fd57ced65453b303328979`,
and the already-published beta16 runtime. A briefly prepared isolation build was
canceled; no rebuilt client was used. The user authorized killing the hung prior
Dayan process, exiting DaVinci and testing only the file-based Dayan path on the
resource-constrained VM. No DaVinci integration acceptance is claimed.

The actual native picker selected the existing `月光青石巷.mp3` test source
(162 seconds), rather than the initially intended 7.5-second fixture. Through the
actual UI, select FX and click Start once. The unchanged native transport reaches
an isolated test-only build of Gateway main `7d4cdb7`, which calls the final-source
provider and actual SDK/GPU/weights. All three roles (dialogue/music/fx) are retained,
only fx is selected by default, and native materialization completes. Exact native
FileRef resolution and Windows ffprobe/SHA256 checks confirm all three files are
162-second WAV/48kHz/PCM24/stereo, each 46656044 bytes, matching result metadata.
The actual result player advances to 30.34 seconds and Start becomes available.

The task uses a fresh WebView profile, per-process HTTP proxy and temporary local
storage configuration. No UI business response is fabricated or overridden. Gateway
authentication/storage and SDK admission remain fixtures; this does not accept
production authorization, billing, PostgreSQL, Hub placement or resource budgets.
The unrelated Bridge credential/DAW discovery path is not accepted. After evidence
capture, the task-launched client and tunnel stop; Windows config is restored to its
exact prior SHA256, with no remaining cargo/rustc or task scheduled jobs. Raw evidence
and native outputs remain at `C:\builder\pymss-upstream-smoke-20261028` on Windows,
and indexed reports at the development artifact root.

PR: https://github.com/FC-TTD/pymss/pull/13.
Verified implementation revision: `51ac023bbc0d20a5f6acd86b5c6aa186eb1aae72`.
The full-source verification image at that revision is
`sha256:755b77ba234586e673ccf5f12ab5f4a1ab2df3f33959bf58f9c745064b503a2b`.
Its source/metadata versions were read back, and the provider browser
upload/separate/play/three-download probe was rerun successfully against it.
This image remains local to the task; it was not published or promoted.

Independent evidence verdicts accept the bounded provider and installed Dayan
paths. See `provider-smoke-review.json` (SHA256
`6103eccf2796fd28c8b86a497e6623f301cfd15118c9159061638ead4ea47d3a`) and
`dayan-smoke-review.json` (SHA256
`cb1c22b4ff485b5ae0d64b6fb4b4db03c2a9fa7d32f97c3e90e5e571c09809b7`)
at the artifact root. The active job documents exact scopes and exclusions.
