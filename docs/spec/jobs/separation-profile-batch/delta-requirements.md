# Requirements delta

- Provider MUST resolve a Profile implementation version to an immutable PYMSS DAG/model manifest.
- Provider MUST support ordinary catalog and user-registered model execution; Profile is not limited to two fixed recipes.
- Provider MUST expose single and multi-input execution with per-input status and output role records.
- Provider MUST atomically commit completed-file manifests so later failed files do not delete them.
- Provider MUST check cancellation inside GPU execution and report cancelled only after Runtime completion evidence.
- Provider MUST keep one model resident per instance; same-model concurrency is capability/profile-scoped.
- Native UI and provider execution MUST use one model coordinator; no independent multi-model cache.
