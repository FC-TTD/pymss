# 双模型 DAG 与显存预算验证

2026-09-18。范围：本地 PYMSS 2.1.5 升级工作树上的真实 CUDA 实验与源码/CPU契约检查。
没有修改DAG实现、Hub在线预算或部署流量。上游参数/输出问题保留为后续修复项。

## 负载和测量方法

- GPU：RTX2080Ti，11264MiB；Python3.10，Torch2.7.1+cu128，pymss-core0.1.8。
- 配方：同一原始音轨分别送入 duality_v2 输出 Vocals、instrumental_becruily 输出 Instrumental。
- 精确模型：melband_roformer_instvox_duality_v2.ckpt / mel_band_roformer_instrumental_becruily.ckpt。
- 权重和YAML来自PYMSS模型源；独立保存哈希。推理batch_size=1、normalize=false、standardize=false、device=cuda均在step显式指定。
- 实际配置：duality chunk_size=485100/overlap_size=24255；becruily chunk_size=352800/overlap_size=17640；两者use_amp=true。
- 从已有测试WAV抽取8秒，扩展到60/900秒时重复该片段；用于容量和编排验证，不代表15分钟节目音质验收。
- 每个场景独立进程，在同一块空闲卡串行运行。8秒场景连续两次执行同一DAG并共享缓存，验证批内模型复用。
- 默认模式使用上游SeparatorCache；对照模式仅在外部实验脚本中以单条目缓存替换它，切模型前close/gc/empty_cache。未把该策略加入产品代码。
- NVML以50ms间隔采样整卡显存，同时记录PyTorch峰值allocated/reserved、节点边界、模型加载次数和进程RSS。

## 结果

| 场景 | 策略 | NVML整卡峰值MiB | Torch峰值allocated/reserved MiB | CPU峰值RSS MiB | 模型加载次数 | 执行时间秒 |
|---|---|---:|---:|---:|---:|---:|
| 8秒×2文件 | 默认保留两模型 | 5571 | 4317 / 4924 | 2819 | 2 | 2.51 |
| 8秒×2文件 | 切模前释放旧模型 | 3545 | 2765 / 2898 | 4112 | 4 | 5.52 |
| 60秒×1文件 | 默认保留两模型 | 5239 | 4356 / 4592 | 2840 | 2 | 4.03 |
| 60秒×1文件 | 切模前释放旧模型 | 4447 | 3266 / 3800 | 3222 | 2 | 4.85 |
| 900秒×1文件 | 默认保留两模型 | 6123 | 5204 / 5476 | 3719 | 2 | 40.58 |
| 900秒×1文件 | 切模前释放旧模型 | 6071 | 4403 / 5424 | 4287 | 2 | 41.53 |

执行时间含加载、推理、保存与验证；不含输入片段准备，是单次观测，不作为稳定吞吐承诺。

全部六个双模型场景成功，输出各一份对白/国际声，双声道、时长匹配。默认DAG保存为44.1kHz/FLOAT。
对应场景两种缓存策略解码后的音频采样完全一致，最大绝对差为0；WAV容器字节不相同，不声称文件哈希一致。
缓存close后仍保留CUDA上下文/少量缓存；进程退出后整卡恢复9MiB、0%利用率。没有影响其他计算进程。

## 预算建议（不是已应用策略）

这个固定双模型配方，在本卡、上述参数、单任务且最长900秒的测试边界内：

- 默认缓存按实测最高6123MiB×1.25、向上取整到512MiB，得到 **7680MiB（7.5GiB）** 初始GPU预算。
- 首期同worker并发1。两份7.5GiB预算不能同时排到11GiB卡；不能把本次串行结果解释为并发能力。
- 可为这条作业准备约6GiB CPU内存，基于测试最高RSS约4.19GiB留余量；额外服务组件需另计。
- 历史PYMSS单模型4.5GiB预算不能直接复用于这条DAG：8秒双模型批次和900秒均已超过。
- 单条目缓存明显降低短输入峰值，但900秒整卡峰值只减少52MiB，不能靠逐模型释放把长输入预算降到4.5GiB。
- 预算应覆盖真实整条DAG的峰值，包括模型缓存、推理工作区、CUDA上下文和分配器保留；不能只按checkpoint大小相加。
- 这是测试派生的起始预算，不是硬上限证明；并未接入Hub执行7.5GiB强制配额。3090或其他GPU、不同Torch/kernel、batch/chunk/TTA参数、更长输入和其他模型需独立校准。

## 发现的上游DAG接入缺口

以下由源码与CPU替身实验交叉确认，非本轮升级引入：

1. YAML defaults写入dag.meta后未被执行器消费；step未指定设备时auto会覆盖运行级device。
2. MSS参数节点仅传递有限字段，use_amp/num_overlap等可能静默丢失；VR参数节点强制use_amp=true。
3. SeparatorCache实际按完整kwargs建键，含store_dirs：同模型不同输出声轨可能生成两个GPU实例。应在同一个分离节点取多声轨，避免无意义重复节点。
4. YAML保存节点未继承defaults/运行级输出设置；step.output_format可选容器格式，但采样率/位深编译为44100/FLOAT。原生DAG保存节点可直接配置48000/PCM_24，不能把YAML编译限制说成整个引擎固定输出。
5. 两个分支同stem且同保存目录时可能写入同一路径；本实验用dialogue/me独立目录，业务应按node_id与明确角色收结果。
6. 原生DAG无预算准入、模型驱逐、持久任务状态或业务取消。目录runner只返回成功输入basename；业务应使用run_dag的结构化records。

因此结论是“DAG编排可运行、这个配方的容量已有实测”，不是“可直接替代现有业务任务服务”。
先修复参数/输出合同、约束缓存身份和输入配置，再把上述配方预算接到Hub受管执行；无需为了省短音频显存默认牺牲所有批任务的模型复用。

## 证据与复现

本次工作区独立证据目录：`/workspace/FC-TTD/.scratch/verification/pymss-dag-budget/`。
`measure.py`是一次性实验工具；`summary.json`含6场景峰值与逐采样比较，
各场景JSON含节点快照、实际参数、输出records与关闭状态，`model-inputs.json`含权重/YAML哈希。
媒体输出位于`/tmp/pymss-dag-budget/`，未提交到Git。

例如：

```sh
CUDA_VISIBLE_DEVICES=0 /tmp/pymss-215-cuda/bin/python   /workspace/FC-TTD/.scratch/verification/pymss-dag-budget/measure.py   --mode retain --seconds 900 --files 1
```

单模型校准场景另有8/60秒结果，不计入上述六个双模型场景。

## 缺陷与配置边界补充核实

- defaults继承失效，以及接受参数后静默丢弃，属于当前YAML→DAG适配的实现缺口；逐step显式填写受支持参数可绕开部分问题，不保证所有MSSeparator参数均有节点配置入口。
- 同模型不同stems重复缓存与代码“输出路由不参与模型身份”的注释矛盾，属于缓存键设计缺陷，没有去重开关；同一分离节点导出多声轨、再连接多个保存节点可在配方层避免触发。
- 原生DAG保存节点widgets_values设为[wav, configured, 48000, PCM_24, PCM_24, 320k]，实际CPU编码得到48000Hz/PCM_24/双声道，未改源码。证据native-save-config.json。
- 因此无需为固定配方先修完整YAML编译器：可直接使用原生DAG显式节点配置与单模型单节点布局；任务准入和显存预算仍由Hub外围提供，这不是DAG承诺但失效的内置选项。
