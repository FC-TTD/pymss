# 固定DAG批任务与Hub接入

2026-09-19。本地实现与分层验证完成；未注册线上服务、未切换MSST/Gateway、未部署。

## 实现边界

新增独立入口 `python -m hub_batch`，复用原生DAG和真实Hub Runtime SDK。
现有 `python -m hub_runtime` 的Studio/Cover行为与选择状态不变。
两份固定配方在 `hub_batch/recipes/`，请求不接受任意DAG或模型参数。

- CPU任务owner、SQLite状态、异步202回执、状态查询、SSE快照、取消请求。
- 单执行槽，其他提交429；未确认任务阻止新工作，容量不是无限队列。
- GPU仅在SDK子进程中构造BatchEngine；同模型缓存可跨批复用。GPU分块回调检查
  绑定task ID的取消Event，取消RPC不申请新活动，不会取消下一任务。
- runtime.execution覆盖整批；completion同步CUDA后才落终态。取消请求不等于已取消。
- 输入只允许指定根目录内的文件，时长由音频流元数据确认且每文件不超过900秒；
  元数据不可用会明确报该文件失败，不猜测时长。输出使用任务ID/文件序号独立目录。
- 每文件生成结构化声轨记录，失败文件与成功文件分别保留；任一文件失败则整批failed。
  processed_files指已结束输入数量，包含失败，不等同成功数量。
- 已完成文件先原子保存并fsync独立manifest，进度观察者断连/SQLite暂时失败不成为
  唯一结果丢失点；取消/失败后CPU回读已提交结果。未提交的部分文件不当成功结果。
- 重启将未终结任务置unknown，回读已提交结果，但不自动续跑或重放。
  unknown是需人工核实的状态，不能用cancel将它强改为已释放；无自动解锁接口。

## API

- `POST /v1/batches`：`{"recipe":"dedicated-me","input_paths":["/allowed/input.wav"]}`。
  配方也可用`duality-two-stems`。额外请求字段422，不静默接受任意参数。
- `GET /v1/batches/{id}`：状态、输入清单、逐文件outputs与错误。
- `GET /v1/batches/{id}/events`：同一任务状态的SSE快照；断开观察不取消执行。
- `POST /v1/batches/{id}/cancel`：请求取消；终态重复调用只返回既有结果。

状态：queued、running、cancelling、succeeded、failed、cancelled、unknown。
输出当前通过共享文件路径交付，尚未新增公开下载接口或适配Gateway。

## 运行与预算配置

独立批处理服务需要自己的Hub运行身份与8GiB预算；不能把入口放到原4.5GiB
Studio实例中冒充同一资源画像。本次没有执行正式服务登记。

除Hub SDK标准环境变量外：

- `PYMSS_MODEL_DIR`：预置完整权重/YAML，不在业务推理时自动下载。
- `PYMSS_BATCH_STATE_DIR`：任务SQLite持久目录。
- `PYMSS_BATCH_INPUT_ROOT`：允许读取的共享输入根。
- `PYMSS_BATCH_OUTPUT_ROOT`：输出持久目录。
- `PYMSS_BATCH_PORT`：本地入口端口，默认8010，绑定loopback。
- `PYMSS_API_KEY`：可选Bearer令牌；公开业务仍应经过Gateway的正式鉴权边界。

CPU宿主必须`CUDA_VISIBLE_DEVICES=''`，子进程只看租约授予的GPU。
Runtime业务RPC设置execution_timeout=None，允许长批；控制调用仍使用SDK有界超时。
实际Hub registration/policy中的budget_bytes应为8589934592，单实例/单任务执行。
预算是容量预留，不是CUDA驱动硬限制，不能用环境变量声明替代真实Core授权。
正式entry需只把POST `/v1/batches`列为新工作准入路径；查询/SSE/取消沿用
平台认证的CPU路由，不能把它们当作新的推理提交。部署镜像包含hub_batch，但默认入口仍是Studio。

## 验证证据

1. 本仓完整非权重回归：186项通过，含14项批任务测试，以及旧API、Hub、编码与DAG回归。
   覆盖忙拒绝、根目录/鉴权、未知终态拒新、运行中取消、部分失败、历史结果、重启不重放、
   进度丢失后的结果保留，以及准备前状态写失败不污染后续任务。
2. 本机2080Ti＋真实SDK GPU子进程：真实双模型成功；在900秒输入的实际推理分块进度出现后取消；
   活动期间unload返回resource_busy，取消后终态cancelled，随后unload确认group_empty。
   节点准入拒绝时不新增执行活动。此项Node为测试替身，不称真实Core准入。
3. 真实Go Node/认证HTTP/bbolt journal＋新批任务API：异步HTTP hold覆盖工作；维护关闭拒新，
   既有任务可完成；结束并flush后允许卸载且HTTP hold清零。这项使用CPU分离替身，无CUDA/Core。
   初次状态查询漏了入口签名头，修正为平台认证CPU路由后通过，没有放宽SDK认证。
4. 隔离临时MariaDB10.6上的真实Core store/scheduler：4项定向测试通过。
   8GiB预留防超卖、实际占用不重复扣账、活动期间拒绝释放、陈旧代次拒绝及直接容量选择。
   容器及临时数据库已清理，未连接生产数据库。

原始本地证据位于`/workspace/FC-TTD/.scratch/verification/pymss-batch/`：
`gpu-smoke.json`、`core-budget.json`/日志、`node_fixture.py`与`gpu_smoke.py`。
这些是三层组合验证，不是把真实Core+Node+GPU部署成一条生产端到端链路。

## 后续交付边界

正式接入前仍需候选镜像构建、独立运行定义与entry路由、完整Core→Node→GPU联调，
随后适配Gateway/旧插件并验证原业务入口。OOM/失联的真实故障注入、本轮未覆盖的格式与
音质验收也不能从短样本或单元测试推定通过。本次未提交、推送、发布或关闭MSST。


## MSST 兼容 facade

本服务同时提供旧 MSST provider 路径：`POST /api/v1/tasks/msst-batch?mode=sync|async|sse`、
`GET /api/v1/tasks/{task_id}`、`/result`、`POST /cancel`。这条 facade 允许大雁 Gateway
和海鸥保持原客户端合同；provider内部使用 PYMSS 普通 `model_name`，不是固定两个 profile。

兼容层保留目录输入、range_start/range_end、extract_instrumental、共享 output_dir、
SSE progress 和 `_Vocals`/`_Instrumental` 输出命名。单个提交固定一个模型；海鸥的 Dedicated
由消费者提交两次模型任务，兼容层不隐式改写为DAG。Gateway仍负责大雁的target/style、
认证、900秒、计费和Range，兼容层负责MSST原始批任务语义。

当前 facade 是本地实现与测试阶段，尚未正式发布到 `http://msst`/原域名；正式切换前必须
用真实 catalog模型、海鸥目录范围、Gateway结果下载和两消费者端到端 smoke 验收。
