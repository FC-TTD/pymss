# Profile v1 retirement

New creation for 2026-09-20.v1 and 2026-10-23.v1 returns 410 profile_retired at
both Provider HTTP entries and direct Jobs admission before filesystem,
persistence or runtime acceptance. Discovery exposes nine 2026-10-28.v2
manifests and retains envelope 2026-10-23.v1. Keep immutable legacy manifests,
digest-bound DAG files, existing task IDs, committed results, status/events,
accepted-handle observation and cancellation. Do not replay or rewrite history.

Compatibility expert-model requests remain supported for the independent Studio.
Reject profile identities on that route rather than ignoring their version or
silently substituting the profile's first model. Versioned requests use the
canonical Profile route. Raw expert recipes retain their independent identity.

Validation: 882 passed, 2 skipped, 32 subtests in the current fixed production
image with candidate source mounted read-only, without GPU access. A missing
external loudnorm fixture was supplied from the official plugin source; its
pinned SHA and raw test logs are indexed outside Git at
/workspace/FC-TTD/.artifacts/pymss-v1-retirement-20261028/.
Independent Standards/Spec review accepted. Production deployment pending the
Gateway/Dayan v2 consumers and managed worker-only 12 GiB acceptance callback.
