# PYMSS upstream integration delivery

- [x] Reconfirm upstream22b2b03 / target90ebd6e and isolate task worktree.
- [x] Merge upstream2.1.12/core0.1.14 and preserve Hub concurrency/checkpoint protections.
- [x] Version nine configurable exports; preserve all published v1 definitions and Gateway discovery envelope.
- [x] Fixed-stack regression:863 passed,15 skipped,32 subtests.
- [x] Nine-profile baseline/candidate real-weight GPU comparison and14 candidate runs.
- [x] Real Bandit core0.1.14 comparison, native cancellation/drain/unload and FFmpeg4.4 compatibility.
- [x] Provider browser UI upload/separate/play/download on the final source image.
- [x] Real TCP disconnect retains active execution, rejects unload, drains before model switch and releases child process group.
- [x] Complete nine-profile/27-case export matrix and mixed mono/stereo source-policy batch.
- [x] Windows89 installed Dayan/UI path: real162-second file, one FX submission, three verified native outputs, playback; no DAW test.
- [x] Independent provider and installed Windows Dayan smoke artifact-consistency verdicts accepted.
- [x] PR delivery prepared; expected-head merge/readback/cleanup recorded through PR13 hosting metadata and external delivery.json.

Scope: provider hub branch only. No production deployment, Audio Nav change,
WebUI publication or shared-backend migration.

Raw artifacts and harnesses:
`/workspace/FC-TTD/.artifacts/pymss-upstream-20261028`.
Source and verification decisions: [upgrade record](../../../hub-takeover/upgrade-2.1.12.md).
Do not equate fixture admission/static auth/memory Gateway storage with formal Hub,
production credentials or PostgreSQL persistence acceptance.

## Current delivery evidence

Implementation commit: `51ac023bbc0d20a5f6acd86b5c6aa186eb1aae72`.
PR: https://github.com/FC-TTD/pymss/pull/13; final client-path evidence review before merge.

- `export-matrix.json`:27/27 real GPU exports, actual media/hash/metadata checked.
- `mixed-source-batch.json`:single HTTP batch preserves mono/stereo per input.
- `final-image.json`:full Docker source revision, image identity and fixed versions.
- `ui-pinned/browser-report.json`:final-image browser upload, playback and all
  three verified WAV downloads; no page errors.
- Windows89 hung Dayan12616 was killed after explicit user instruction.
  The isolation build was canceled; actual smoke uses the unchanged installed
  beta11 executable and published beta16 runtime. No rebuilt client was used.
- mac-dev task process/tunnel stopped; task-touched config restored to prior hash.
- User initially paused Windows for memory recovery, then explicitly resumed
  constrained testing with DaVinci closed and excluded from scope.

- `socket-disconnect/report.json`:real loopback TCP close during long-audio GPU
  execution, resource-busy unload, delayed model switch and final group_empty.
- `provider-smoke-report.json` / `provider-evidence-index.json`:machine-readable
  provider paths, payload/network/output trace and hashes; its reviewer verdict
  does not itself include the separate Windows Dayan evidence.

Independent provider evidence review: `provider-smoke-review.json`, verdict
`accepted`, SHA256 `6103eccf2796fd28c8b86a497e6623f301cfd15118c9159061638ead4ea47d3a`.
It accepts only provider UI/export/TCP paths; the repo-local checker accepts
`provider-smoke-accepted.json`, which references the unchanged reviewed report.
It does not waive Windows Dayan acceptance.

Windows testing resumed by user instruction; the subsequent user direction was
to kill the hung Dayan and use the installed app, rather than compile a test copy.
The task build was canceled and no cargo/rustc remains. The actual installed client
uses native file selection, unchanged payload/transport, isolated actual Gateway
and SDK/GPU inference. `win-dayan/terminal.json`, `native-output.json` and
`output-summary.json` bind task `separation-1`, all three actual files, default FX
selection and playback30.34s. Three Windows outputs each contain162s WAV48k/PCM24/
stereo and exactly match expected hash/size. `dayan-smoke-report.json` and
`dayan-evidence-index.json` retain raw client/network/storage/output traces.

`win-cleanup.json` confirms test client stopped, all task scheduled jobs removed,
config restored exactly (`9c90b2a2a7cea3bf9fe252ed25aa5fd217588a5b10ce3331deab2e16bf51f44d`),
and no remaining cargo/rustc. Windows evidence and native output files remain in
`C:\builder\pymss-upstream-smoke-20261028`. No private configuration is committed.

Independent Windows Dayan evidence review: `dayan-smoke-review.json`, verdict
`accepted`, SHA256 `cb1c22b4ff485b5ae0d64b6fb4b4db03c2a9fa7d32f97c3e90e5e571c09809b7`.
It accepts only installed Dayan MSST file import → one FX submission → three native
outputs → playback. It confirms exactly one create POST, prepared-source hash,
manifest/FileRef identity, three Gateway-download/Windows-file hash and size pairs,
media info and30.343s playback. It does not extend to Dayan-side v2 options, all
nine Dayan routes, DAW, production auth/Hub/PostgreSQL, budget or persistence recovery.
Provider-side nine-profile/v2 coverage remains in the separate accepted report.

The accepted report projections (`provider-smoke-accepted.json` and
`dayan-smoke-accepted.json`) reference the unchanged independently reviewed candidate
reports; the repo-local smoke checker accepts both. Actual merge SHA and readback,
plus final task-container/worktree cleanup, are retained in PR13 and external
`delivery.json` after this documentation commit. No raw/private evidence enters Git.
