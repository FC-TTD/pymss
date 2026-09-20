# PYMSS Profile/Batch Provider

PYMSS is the provider execution layer for the new separation Profile + Batch contract. It must preserve the Hub-managed native UI while adding profile manifests, ordinary model calls, DAG execution, batch files, output roles, progress and confirmed cancellation.

The provider must keep one model resident per Hub-managed instance. Same-model activities may share a residency up to a measured profile concurrency limit. Model switches drain activity, unload with evidence, then load the next model. The native UI and provider adapter share this coordinator.

Success: ordinary catalog/user model calls, immutable Profile manifests, single/multi-input tasks, per-file artifacts, resume manifest, cancellation/unknown containment, and native UI regression.
