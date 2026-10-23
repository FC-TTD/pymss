# 原生 JSON DAG 配置验证

2026-09-18。使用本地2.1.5升级worktree与RTX2080Ti/11GiB。没有修改上游DAG源码、
没有部署或更新Hub预算。目的是验证固定原生配方能否绕开YAML编译器缺口。

## 可复用配方

- [单模型双声轨](recipes/duality-two-stems.json)：一个duality_v2分离节点同时输出Vocals、Instrumental，连接两个保存节点。
- [对白＋独立国际声](recipes/dedicated-me.json)：同一原始音频分别输入duality_v2与instrumental_becruily，分别保存对白和国际声。

两份JSON均由load_comfy_file解析并经run_dag运行，没有调用YAML编译器。
设备、batch_size、chunk_size、overlap_size、normalize、standardize在节点中显式配置；
实际模型构造后再次断言设备/分块/重叠/批大小。use_amp=true来自固定模型YAML，
不声称原生参数节点可配置任意MSSeparator参数。测试使用本地权重，未启用下载。
保存节点显式配置WAV/48000/PCM_24；dialogue和me分开目录，业务结果用node_id区分。

## 真实验证结果

| 场景 | 模型加载次数 | 输出 | NVML整卡峰值 | CPU峰值RSS |
|---|---:|---|---:|---:|
| 单模型双声轨，8秒输入连续执行2次 | 1 | 4份WAV | 4365MiB / 4.26GiB | 2983MiB |
| 双模型，8秒输入连续执行2次 | 2 | 4份WAV | 5571MiB / 5.44GiB | 3007MiB |
| 双模型，900秒输入连续执行2次 | 2 | 4份WAV | 6317MiB / 6.17GiB | 4659MiB |
| 双模型，两段不同的8秒同名文件 | 2 | 4份独立WAV，无覆盖 | 5571MiB / 5.44GiB | 3009MiB |

全部文件实际解码确认48000Hz、PCM_24、双声道、有限采样、时长匹配（重采样可能产生一个采样点的取整差）。
单模型多声轨没有产生第二份模型实例；双模型跨执行没有额外加载。
900秒为重复8秒片段，前3场景重复同一输入以验证热缓存；不是不同节目音质测试。
最后一场景从源测试音频0秒和40秒分别取片段，在不同目录使用相同的中文/空格文件名。
已确认两段解码音频不同、结果source_path与输入逐项对应，输出隔离依赖每次执行独立目录和前缀。

原生CLI comfy run也实际完成双模型分离，输出格式核对通过。
缺失输入文件返回音频加载错误，模型工厂调用次数为0，未申请或加载GPU模型。
最初CLI尝试将--model-dir放在顶层，解析器直接拒绝；更正为comfy run子命令参数后成功，
首次没有执行推理。负例测试最初预期FileNotFoundError，实际库包装为RuntimeError；
修正测试后确认错误内容和0模型加载，未改变库行为。

## 更新后的预算建议

900秒×2的PyTorch峰值allocated=5288MiB、reserved=5670MiB，
NVML以50ms采样到整卡峰值6317MiB。按最高观测值加25%余量并向上取整到512MiB，
建议将该固定双模型配方的起始预算从7.5GiB修订为 **8GiB**，同worker并发1。
CPU可先按6GiB规划（最高RSS约4.55GiB，加25%约5.69GiB）。

实验中的7.5GiB是事后峰值断言，全部场景未越过；它不是已实现的显存准入或硬配额。
缓存close后仍有CUDA上下文：长测整卡约807MiB、allocated约8MiB、reserved160MiB；
测试进程退出后整卡回到9MiB、0%利用率、无计算进程。不能将close描述为进程内显存归零。
预算仅覆盖当前硬件、Torch/core版本、固定两模型及参数；并发、其他模型或不同分块需另测。

## 使用示例

```sh
python -m pymss.cli comfy run \
  --model-dir /path/to/models \
  -c docs/hub-takeover/recipes/dedicated-me.json \
  -i /path/to/input.wav -o /path/to/task-output --device cuda
```

批量调用run_dag时，每个输入使用独立output_dir和name_prefix，可复用同一个SeparatorCache；
finally关闭缓存。直接反复用CLI写同一输出目录仍可能覆盖同名结果，本次配方不替代业务任务管理。

## 交付边界

固定原生配方已具备可运行证据，无需先改完整YAML编译器或复制推理引擎。
下一阶段可接入现有批任务与Hub执行边界。真实Hub租约、预算拒绝、取消、OOM恢复、
并发和浏览器端到端尚未验证；本轮未声称产品上线验收。

原始日志、测量脚本和逐节点JSON：
`/workspace/FC-TTD/.scratch/verification/pymss-native-dag/`。
媒体只保留在`/tmp/pymss-native-dag/`，未加入Git。
