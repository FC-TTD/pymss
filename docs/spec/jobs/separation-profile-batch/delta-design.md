# Design delta

`ProfileManifest -> ModelCoordinator -> PYMSS MSSeparator/DAG -> ArtifactManifest`.

The coordinator owns selected model, residency lock, active count and per-profile concurrency. DAG nodes with different models run sequentially; same-model inputs can share the loaded model. Provider task state and result manifests are provider execution state; Gateway stores business task references and FileRefs.

The native `/ui`, `/v1/models`, `/v1/models/load` and `/v1/audio/separations` remain Hub-managed. Provider batch routes must use the same runtime activity and coordinator, not a second unmanaged service.
