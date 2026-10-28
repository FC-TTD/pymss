# Production retirement acceptance — 2026-10-28

Merged [PR14](https://github.com/FC-TTD/pymss/pull/14), source
`fa145fb104e54917c1462fb314e2b192bd6451db`, tag
`separation-v2-20261028-fa145fb104e5`, is deployed as managed release
`pymss-v2-fa145fb-20261028`. Immutable image:
`registry.ttd/pymss/hub@sha256:e39047593811fbf511d8f065f2fd0e71e16ce4301731139ad7711e4ae85fb5db`.
Hub tools are pinned to merged source `7d9ec1ab307fe5abcae5c08416fd3c956018152b`
([PR57](https://github.com/FC-TTD/ttd-hub/pull/57)). No SDK/CUDA/Torch or GPU
topology change accompanied the release. Existing effective policy remains
12GiB, worker-only, the same three UUIDs and policy revision8.

Core accepted the exact FX v2 business evidence hash. One native5s request
`d392ef984feb4c7bae171db61911b708` produced the expected three tracks. The
acceptance lease was drained, engine exited/group emptied and VRAM released
before reopening the ordinary route. Final snapshots show the new release
healthy/accepting with active/pending0, and Core active/pending/unknown/http0.

`/v1/separations/profiles` exposes nine active v2 profiles. The two versions
`2026-09-20.v1` and `2026-10-23.v1` each return `410 profile_retired` on
`POST /v1/separations` and `POST /v1/batches`. Provider old task
`30685a25042443fa9011c0b34ade544a` status and result still return200. Gateway old
task/result/three downloads and native historical recovery work without a new
inference POST. Raw expert recipes retain their independent creation behavior;
Audio Nav's embedded workbench completed a fresh `becruily_deux.ckpt` two-track
request, playback and ZIP after deployment.

CPU regression:882 passed,2 skipped,32 subtests. Full Gateway checks and Dayan
standalone regression/typecheck/build/runtime build passed. Independent
Provider artifact audit accepted deployment identity, retirement/history,
budget, accepted callback, drain and settled Gateway binding. Independent Dayan
audit and repo-local smoke gate accepted bounded native default/monoFLOAT,
historical recovery and embedded-browser paths.

[Evidence index](evidence-index.json) pins persistent raw delivery/review records
under `/workspace/FC-TTD/.artifacts/pymss-v1-retirement-20261028/`. The index is
an audit pointer; audio, target storage traces and private deployment files
remain outside Git.

Production business coverage is FX5s and selected client paths, not the isolated
nine-profile matrix repeated in production. Native history is seeded metadata
bound to a real old task, with requiredRoles supplied. Native input preparation
still normalizes to stereo48kPCM16. Post-release denoise evidence retains result
recovery/playback/export, not its first submission trace. No DAW or subjective
listening acceptance is claimed.

Initial Gateway startup rolled back; corrected source106c0d7 settled both API
replicas before Provider cutover, while its worker stayed unchanged. Managed
rollout temporarily fenced three sibling model entrances. Final maintenance is
open/ACK for all four, but FireRed remains without a lease and resource_busy;
open/fresh does not guarantee immediate GPU capacity. Automatic rollback is
disabled for this Gateway transition: failure/unknown stays fenced and never
replays inference or reopens v1. Preserve data and the pinned12GiB/worker3 policy
when preparing any future controlled rollback.
