# Provider implementation evidence

Fixed source: `89b6187cdf74421b239fba1a370d4ad1a1dd2bd6`. No production
inference or deployment is claimed by these local checks.

Publication targets the formal long-term `hub` adapter branch declared by
README.hub.md. The fork's `main` was synchronized to upstream 2.1.7 after this
task's fixed source; rebasing onto it would change the native dependency and
exceed this delivery. Current `hub` is an ancestor of the fixed source, so the
PR aggregate also includes the already-published PRs 1–4 and 2.1.5 adaptation
history. Those ancestors are separate from this task's independently reviewed
delta. Production still identifies native source
`d108368a69d6ea0e7d21b0bebb7a57bcd03c8183`, included in the fixed source; no
upstream 2.1.7 code or native dependency upgrade is part of this change.

The packaged manifests declare exact native stems, public semantic roles, DAG
digest, model weights/config digests and WAV48k/PCM24/stereo output. All nine
cached weight hashes were streamed from the existing worker mount. FX's old
formal `.chpt` source and the current `.ckpt` bytes share SHA256
`c48284779f7d1258a6527d3aaa18a532d45c1f506e2dcc25d5ab179a8c5e2573`;
the manifest records its fixed upstream commit/source. Raw evidence remains in
the external task directory, not a new model inventory in governance.

Red/green boundaries: missing complete-version registry; modified pinned weight
rejected before separator construction; missing discovery/unknown options; and
Hub single-candidate publication selecting only actual candidates.

`test_dayan_profile_batch.py` executes all nine real packaged DAGs and actual
WAV encoding with a CPU separator double at the inference boundary. It checks
21 outputs, reversed model dictionary order, exact model name, no downloads or
expert overrides, semantic roles, hashes/media, receipt identity/timestamps and
opaque Hub metadata. Four legacy DAGs additionally retain their exact digest
and publish additive semantic artifact records.

`test_profile_contract_v2.py` checks both HTTP entries, unavailable/unknown
options and versions before acceptance, registry mutation isolation, asset
tampering, missing outputs, corrupt media, duplicate/escaped output paths,
committed results after failure/cancellation, and unknown completion retaining
capacity. Existing Batch tests cover restart recovery without replay, busy
rejection, cancellation settlement and native coordinator safety.

Full regression command uses the existing PYMSS virtual environment and SDK
source on PYTHONPATH:
`python -m pytest -q test test/hub_runtime_test.py --ignore=test/test_all.py`.
Final full regression excluding the independently reproduced baseline failure:
**214 passed, 1 deselected**. The first full run had 207 passed and one pre-existing failure:
`test_training_metadata_checkpoint_still_loads_safely`. The same test fails on
unchanged main with the installed PyTorch weights-only unpickler rejecting
defaultdict SETITEM. No unsafe load fallback or model loader change was made.
The native coordinator test's original fixture accidentally called the actual
native factory and attempted a model download; patching that explicit factory
restores its intended CPU-only boundary. It is not a product inference fix.
The plugin-test fixture also cleared global node registrations while Python
retained the built-in module import cache. Restoring the pre-test registry at
fixture teardown removes the resulting order-dependent `input_audio` failure;
the product graph loader is unchanged.

Remaining acceptance: formal pinned image build/cache proof, managed SDK and
provider publication, every accurate model under the effective 4.5GiB lease,
original Windows audio and longest supported input, per-Profile allocated and
reserved peaks, real cancel/unknown recovery and native UI coexistence. Stop
capacity acceptance on an over-budget Profile; do not silently increase budget,
lower precision or enable offload.
