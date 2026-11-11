# PYMSS Profile v2 Worker 发布准备（2026-11-11）

在已合并的 VR 尾帧修复 PR #16、影视/Karaoke Profile PR #17 上新增最小镜像叠加层。基底固定为已验收的 Worker Provider `sha256:e39047593811fbf511d8f065f2fd0e71e16ce4301731139ad7711e4ae85fb5db`；只覆盖这两条 PR 改变的运行文件：Profile 索引、三份 DAG recipe 和两份 VR 文件。CUDA、Torch、`pymss-core`、Hub Runtime SDK、权重和 GPU 拓扑均继承原镜像。构建输入须取本提交的只读快照；新镜像需按不可变 digest 记录。

正式受管发布仍使用 `ttd-hub` 的 `managed_release.py` / `managed_rollout.py`，以当前已接受的 `pymss-v2-fa145fb-20261028` 为精确前序。候选节点限定 Worker；发布前备妥 Gateway 新 API 的不可变 digest 与受管绑定，确认当前任务 active/queued/unknown 为零，再在维护围栏内替换 Provider。验收用固定 v2 Profile 做原生真实音频和 Gateway 请求；失败或响应未知时保持围栏，不重发业务推理。短时 Node Agent 维护会影响共卡兄弟入口，须验证全部恢复。

本文件只描述候选构建与发布边界，不代表新镜像、Hub catalog 或 Stage 客户端权限已发布。旧镜像和 release 记录保留用于受管回退；Gateway 版本改变后的自动回退禁用，未知状态需人工核对。
