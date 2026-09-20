# 固定DAG批任务接入意图

范围：在独立worktree增加本地可验证的批处理入口，不部署、不修改现有工作台API、Gateway或Hub实现。
复用已验证的duality-two-stems / dedicated-me配方，显式48kHz/24-bit输出，单执行槽。

公共边界：POST /v1/batches 接受 recipe 与 input_paths；GET /v1/batches/{id} 返回持久状态/逐文件结果；
POST /v1/batches/{id}/cancel 请求取消。文件路径必须位于配置输入根目录，输出只写任务独立目录。
不接受任意DAG、任意模型参数或任意输出路径。SSE提供同一持久任务快照；不是另一条执行路径。

生命周期：queued/running/cancelling/succeeded/failed/cancelled/unknown。运行中取消必须在GPU执行端确认，
SDK completion完成后才提交终态；取消不申请新租约。推理结果未知不重放，未确认任务阻止新工作与卸载。
重启发现未终结任务只标记unknown，不宣称可恢复执行。已完成文件结果保留，结果不依赖文件名猜测。

GPU只在真实SDK子进程中加载，CPU宿主不直接加载权重。固定配方按8GiB预算、并发1登记候选，
预算必须由Hub/node的实际准入决定，provider不把环境变量或NVML采样伪装成授权。

验证：公开API的成功/忙/路径拒绝/逐文件错误/取消/历史结果；真实SDK进程的准入拒绝、活动终态、
取消期间不能卸载、结束后卸载group_empty。可用时用本机GPU及隔离真实Hub组件验证，模拟边界明确标注。
生产注册、预算变更、线上切流不在本轮范围。
