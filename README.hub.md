# PYMSS 模型池接入

组织fork：`FC-TTD/pymss`，上游`pymss-project/pymss`；长期适配分支`hub`。
原WebUI另在`FC-TTD/pymss-webui`的`hub`分支保留。

最初接管以原edge手工preview的实际成果为基线，保留管理员改动后新增
`hub_runtime/`和`deploy/hub/`。当前工作分支将源码升级到上游2.1.5，
保留必要兼容补丁；这不代表线上已升级。验证范围见
[2.1.5升级记录](docs/hub-takeover/upgrade-2.1.5.md)。
原接口与`pymss-studio`域名、中文工作台、分轨预览、下载与48kHz/24-bit WAV保持。
源码接管与实际交接证据见[接管记录](docs/hub-takeover/adoption.md)。

模型权重在SDK独立GPU子进程，API/UI宿主在CPU。请求完成保持热驻留；只在
资源压力或明确drain时释放。保留原native limiter和推理锁，当前max_queue_size=1
表示一个已接纳请求，另一个返回原429，不改成无上限并发或新平台队列。

原动态模型选择仍通过同一受管实例执行，选择与调参保留在CPU父进程，驱逐后
沿用选择重新加载；`/health.model_loaded`表示真实驻留，`model_selected`表示已有
逻辑选择。模型目录与下载本身不申请GPU。目录有352个条目，不表示已验证每种模型
和调参组合都能在同一预算内运行；预算按实际验收模型峰值校准。

原`separator.close()`的临时CPU清理属于原生退出流程，不是Agent新增的运行中
offload。不更改精度/CPU-GPU分工、不纳管独立CPU Only服务。

`python -m hub_runtime describe`导出真实OpenAPI但不加载模型。构建使用固定原
运行镜像，叠加SDK、2.1.5源码及固定的pymss-core依赖，删除旧版遗留模块，
保留原管理员UI静态产物；模型和输出挂载保持，原CUDA依赖不自动升级。
原GPU接口和CPU入口在`deploy/hub/compose.yml`，原域名和Cover旧内部DNS兼容
在`business.yml`；只在旧实例正常退出后应用该overlay，避免双后端分流。

2.1.5模型目录不再携带完整输出音轨配置。Hub优先读取本地YAML，VR使用原生
内置元数据；缺失MSS配置时可查询选择状态，但须先调用`/v1/models/load`，
推理会返回明确的`503 model_not_loaded`，不会猜测音轨或在健康检查中下载权重。

本地CPU回归（需要额外安装pytest、soundfile、httpx和Hub Runtime SDK）：
`python -m pytest -q test test/hub_runtime_test.py --ignore=test/test_all.py`。
显式包含`hub_runtime_test.py`，因为上游默认发现规则只匹配`test_*.py`。

独立固定DAG批处理入口为`python -m hub_batch`，不改变默认Studio入口。
接口、状态持久化、取消语义、8GiB候选预算及本地验证边界见
[批任务接入记录](docs/hub-takeover/batch-integration.md)。尚未注册或部署该服务。
