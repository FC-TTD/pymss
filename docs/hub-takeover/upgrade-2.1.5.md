# PYMSS 2.1.5 本地基线升级

2026-09-18。范围：独立worktree升级与验证，不部署、不引入业务批量任务。

## 来源

- 原Hub分支：`99b5174b04636e32f6f846f2f732300757f56e90`。
- 上游目标：`df96b9212fbd1d4491dff2c5043bcfea2f0cdff6`，PYMSS 2.1.5。
- 新模型实现依赖：`pymss-core==0.1.8`；沿用原Torch 2.7.1进行CUDA对照。

## 合并与兼容

- 接入上游workflow、graph、plugins和模型模块迁移；未向HTTP服务新增批任务API。
- 保留管理员checkpoint安全加载、MelBand形状推断、本地手工配置保留、
  JSON容器音频及48kHz/24-bit WAV输出行为。
- 原MelBand接受但忽略`use_shared_bias`；新core拒绝该参数。兼容处理留在
  本仓构造入口，未修改pymss-core或复制旧模型实现。
- 恢复上游关闭模型后还原cuDNN benchmark的行为。
- 适配目录删除`config_instruments`：MSS读取本地配置，VR读取内置声轨元数据；
  MSS无配置时要求显式load，未选定真实输出schema前不接受推理。
- Hub HTTP同步上游下载超时参数规范化，保留受管执行、原限流、切模和取消等待。
- 更新镜像配方，使构建使用升级后的源码/core，而非继续运行旧基底中的2.0.6；
  保留管理员UI静态文件及固定基底CUDA栈。此次未构建或部署生产镜像。

## 验证

CPU验证环境：Python3.11、Torch2.7.1+cpu、pymss-core0.1.8、本地Hub SDK。
172项全套非权重回归通过，随后新增的缺配置HTTP回归1项通过，共173项。
涵盖模型管理、实际localhost HTTP、音频编码、图/工作流、插件和Hub合约。
Hub子进程测试使用真实SDK传输与CPU fixture，不冒充GPU调度验收。
另用小型随机MelBand权重验证新core构造、旧参数、checkpoint形状推断和真实CPU前向。
Wheel构建成功，OpenAPI导出成功，git diff --check通过。

初轮失败及处理：缺失目录字段导致Hub启动失败；旧use_shared_bias参数被core拒绝；
上游MLX单元测试未模拟平台可用性；原生HTTP测试默认PCM与管理员默认WAV合同不符；
Hub测试替身缺当前SDK的pending_work方法。分别按真实兼容行为修复或校正测试前提，
没有改变产品合同来迎合测试。

GPU验证环境：开发机RTX2080Ti/11GB，Python3.10、Torch2.7.1+cu128。
旧Hub源码与候选源码使用同一环境、同一权重和配置、同一段8秒双声道测试音频，
通过各自NativeBackend完成真实加载/推理/保存与关闭；并确认同spec复用模型。

| 模型 | 最大绝对采样差 | RMS差范围 | 旧/新峰值allocated显存 |
|---|---:|---:|---:|
| BS-Roformer-Resurrection | 8.95e-8以下 | 7.70e-9～9.15e-9 | 1097.19 / 1097.00 MiB |
| 1_HP-UVR | 2.64e-5以下 | 1.64e-6左右 | 8149.60 / 7946.35 MiB |

声轨名称/数组形状相同，输出有限数，WAV保持48kHz/PCM_24。
VR原有输出比输入短32个44.1kHz采样点，两版本一致。
进程结束后GPU回到9MiB、0%利用率、无测试计算进程。
时延受预热/编译等影响，本次不据单次运行宣称性能提升。

## 限制与后续

输出存在小幅浮点差异，不宣称逐字节一致；8秒测试不证明整目录模型、长输入、
所有参数或主观音质等价。尚未运行真实Hub控制面/租约的GPU全链路，也未进行
生产镜像构建、原工作台浏览器验收、Cover端到端和900秒压力测试。
上述上线验证应在后续发布范围内完成。当前工作仅保留在独立worktree。
