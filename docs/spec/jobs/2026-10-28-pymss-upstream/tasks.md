# PYMSS upstream integration delivery

- [x] Reconfirm upstream22b2b03 / target90ebd6e and isolate task worktree.
- [x] Merge upstream2.1.12/core0.1.14 and preserve Hub concurrency/checkpoint protections.
- [x] Version nine configurable exports; preserve all published v1 definitions and Gateway discovery envelope.
- [x] Fixed-stack regression:863 passed,15 skipped,32 subtests.
- [x] Nine-profile baseline/candidate real-weight GPU comparison and14 candidate runs.
- [x] Real Bandit core0.1.14 comparison, native cancellation/drain/unload and FFmpeg4.4 compatibility.
- [x] Provider browser UI upload/separate/play/download.
- [ ] Complete nine-profile export matrix and mixed-source batch.
- [ ] mac-dev real Dayan/UI smoke against isolated candidate (macOS accessibility pending).
- [ ] Independent smoke artifact-consistency verdict.
- [ ] PR checks, expected-head merge to hub, readback and task cleanup.

Scope: provider hub branch only. No production deployment, Audio Nav change,
WebUI publication or shared-backend migration.

Raw artifacts and harnesses:
`/workspace/FC-TTD/.artifacts/pymss-upstream-20261028`.
Source and verification decisions: [upgrade record](../../../hub-takeover/upgrade-2.1.12.md).
Do not equate fixture admission/static auth/memory Gateway storage with formal Hub,
production credentials or PostgreSQL persistence acceptance.
