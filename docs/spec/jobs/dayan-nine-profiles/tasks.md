# Dayan nine separation profiles

Fixed baseline: `89b6187cdf74421b239fba1a370d4ad1a1dd2bd6`.

Dayan's nine existing UI routes need complete model outputs and immutable
implementation identity. Publish all nine at `2026-10-23.v1`; preserve the four
published `2026-09-20.v1` manifests and digests. The approved feasibility scope is
Dayan `docs/spec/jobs/2026-10-23-msst-options-recovery/implementation-feasibility.md`.
Use the exact original models, native stems and model YAML defaults. Do not
substitute cached models, download during inference, change resource budgets,
precision, offload or concurrency, or filter outputs by default UI selection.

- [x] Bind all nine implementations to packaged DAG, YAML and weight SHA256; fail before model load on identity mismatch.
- [x] Register the same 13 definitions in both entries; expose private discovery, strict Profile submission and structured receipts.
- [x] Require all 21 new output roles and inspect actual WAV48k/PCM24/stereo, distinct owned paths, byte size and streamed hash before committing each input.
- [x] Add explicit output identity to old Profile receipts without changing their manifest/digest; map old dedicated DAG native Vocals to dialogue inside Provider.
- [x] Pass bounded opaque implementation identity through Runtime SDK 0.3.0; preserve cancellation/completion/unknown capacity fencing and committed partial results.
- [x] CPU contract and regression checks, with evidence and the independently reproduced baseline failure in evidence.md.
- [ ] Independent final review, PR/merge and fixed-artifact managed publication.
- [ ] Real GPU/Windows original-input, long-input, capacity, cancellation and recovery acceptance; CPU doubles do not satisfy these.

Raw read-only deployment, model catalog and cache evidence is kept outside Git:
`/workspace/FC-TTD/dayan-msst-profile-audit`.

The superseded seven-profile draft is archived outside Git at
`/workspace/FC-TTD/dayan-msst-contract-exploration/paused-draft-before-authorized-implementation.tar.gz`.
