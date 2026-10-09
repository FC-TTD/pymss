# pymss 插件系统改造总计划

状态：执行中（experiment/plugin-redesign 分支）。

背景问题（已核实）：

1. `bootstrap()` 只有 `pymss plugins list` 警用；常规入口（infer / workflow / comfy run / serve）不加载用户插件。
2. `register_cli()` 只写 `cli_commands` 列表，`main()` 不挂载这些命令。
3. `save_audio()` 每次调用 `register_builtin_codecs()`；该函数只跳过 `source=="builtin"` 的同名项，会把外部插件覆盖过的编码器改回内置实现；`supported_formats()` 只枚举 `source=="builtin"` 的条目，外部新增格式注册成功却不被发现。
4. 图内 `nodes.py` 的 `_resample` 直接 import `plugins.builtins.resample`，不经 capability；替换注册表不影响该路径。
5. capability 只有 name/func/source/description，无可序列化的输入输出/参数/默认值/范围描述，宿主（Studio/Comfy）无法据此自动生成 UI/节点。
6. loudnorm 等外部插件作者必须读取 `widgets_values[0]`、`nodes_by_id` 等 DAG 内部结构。
7. loader 失败插件无注册回滚；全局注册表 last-writer-wins。
8. 插件目录全局、依赖装进某个 sys.executable，与 Studio/Comfy 各自环境不一致，诊断分层不足。

改造总方向：插件声明 Operation（可执行处理）与 Provider（底层服务实现）；pymss runtime 负责发现/校验/执行；宿主（CLI/Studio/Comfy）只做适配。内置功能与外部插件走同一路径。

## 轮次

### 第 1 轮：修复现有连接性缺陷（pymss）✅ 已完成

- [x] 1.1 `register_builtin_codecs()` 幂等不覆盖；`supported_formats()` 枚举任意来源。已验证：插件覆盖 wav_encode 后3次 save_audio 仍生效并写出插件内容。
- [x] 1.2 `ensure_plugins_loaded()`（loader）：惰性、幂等。
- [x] 1.3 `cli.py main()` 加载用户插件并挂载 `cli_commands`（同名冲突跳过+告警）。e2e 验证 `pymss probe` 可执行。
- [x] 1.4 `cmd_infer/cmd_workflow_run/cmd_comfy_run/cmd_serve` 接入。
- [x] 1.5 修复 test_plugins autouse fixture 清空注册表不恢复内置注册导致的跨模块泄漏。

验收：安装一个测试插件后，`pymss <plugin-cmd>` 可执行；`pymss infer` 路径中该插件的 capability 可被 require；外部 codec 覆盖在多次 save_audio 后仍生效。

### 第 2 轮：Operation 模型 + runtime（pymss）✅ 已完成

- [x] 2.1 `pymss/operations/`：OperationSpec/OperationContext/Runtime（隔离实例、冲突报错、参数校验、setup 失败回滚）。
- [x] 2.2 `create_runtime()` + `default_runtime()`；`PluginRegistration.register_operation` 桥接全局 loader。
- [x] 2.3 插件 setup() 协议（round 1 已完成 loader 侧；本轮补 register_operation）。
- [x] 2.5 CLI `pymss ops list/describe/run`。
- [x] 2.6 测试：13 个单元测试 + ops CLI e2e 六场景。
- [ ] 2.4 loudnorm 改写（在 pymss-plugins 仓库，随第 4 轮图适配一起验证）

### 第 3 轮：音频服务（decode/resample/channels/encode 走 Provider）

- [ ] 3.1 定义 `AudioService` 接口：`probe/read/read_blocks/resample/convert_channels/encode/save`，Provider 显式登记多实现 + 显式选择（配置或任务级），记录"实际选中实现"。
- [ ] 3.2 `audio_io.load_audio/save_audio` 委托音频服务；保留 PyAV→librosa→ffmpeg 回退链与解码重采样融合路径（行为不变，来源收拢）。
- [ ] 3.3 图 `nodes.py` 的 `_resample`、`separator.py` 的标准化/归一化直接调用处改经 runtime 服务（保持算法与多 stem 共享增益语义）。
- [ ] 3.4 测试：替换 resample provider 后图/CLI 保存路径确实改变；长音频内存/帧数回归（复用现有测试基线）。

### 第 4 轮：图执行器接入 runtime（pymss）

- [ ] 4.1 `run_dag()`/`NodeContext` 接受 runtime 参数（默认惰性创建）。
- [ ] 4.2 Operation 自动生成图节点：signature 由 OperationSpec 生成；执行时解析命名参数调用 `runtime.run()`；旧 `widgets_values` 读取移入导入适配层。
- [ ] 4.3 旧节点 id 兼容：现有类型继续注册；新工作流保存 operation id + 命名参数 + schema_version。
- [ ] 4.4 loudnorm 经图端到端测试：新格式节点执行 + 旧 Comfy 工作流导入后执行同一实现。

### 第 5 轮：Studio 通用操作通道（pymss-studio）

- [ ] 5.1 Python worker 新增命令：`list_operations` / `describe_operation` / `run_operation`（worker 用当前激活环境 import pymss 创建 runtime；音频传引用不传 PCM）。
- [ ] 5.2 Rust：通用 run_operation 接入现有任务管理（start/cancel/终态事件），补通用终态事件类型（不硬编码命令名单）。
- [ ] 5.3 前端：通用插件工具页（operationId 驱动表单：number/enum/bool/string/file）；结果通用展示（audio/file/text/table）；插件状态（加载失败/依赖缺失/可用）。
- [ ] 5.4 工作流编辑器：从 describe_operation 动态生成 LiteGraph 节点（与工具页共享控件生成）；静态 nodeSpecs 缩为兼容映射。
- [ ] 5.5 测试：worker 命令冒烟（tests/python）、前端 vitest、手动 tauri dev 验证 loudnorm 显示与执行。

### 第 6 轮：Comfy 适配（comfy-mss）

- [ ] 6.1 加载时创建 runtime，OperationSpec → 节点工厂（INPUT_TYPES/RETURN_TYPES/FUNCTION/CATEGORY）。
- [ ] 6.2 音频 batch 语义明确：单音频操作校验 batch=1；列表输出与 batch 分开处理。
- [ ] 6.3 进度/取消接 Comfy ProgressBar；缓存失效考虑 operation + provider 版本。
- [ ] 6.4 现有 13 个静态节点保留为薄包装，逐步内部改调 operation；分离动态 stem 节点保留专用类。

### 第 7 轮：内置功能迁移 + 收尾

- [ ] 7.1 builtins DSP（音量/反相/归一化/裁剪/拼接/混音/EQ/静音/声道）改为内置 Operation，图节点消重（如 AudioAdjustVolume 复用同一实现）。
- [ ] 7.2 separator 保存/读取路径全面走音频服务；编码器成为 Provider。
- [ ] 7.3 安装器分层诊断：installed → loaded → contributed → executable；每环境依赖状态可见。
- [ ] 7.4 文档：docs/plugins.md 更新为新协议；AGENTS/README 同步；跨宿主验收清单。

## 全局验收

- 新增 loudnorm 插件（不改任何宿主注册代码）：CLI 可跑、Studio 工具页显示参数并可执行、Studio 工作流出现节点、Comfy 出现节点，四处执行同一实现。
- 替换 resample provider：所有声明接入的路径（图、separator 保存、编辑器通用路径）实际改变；ARA/模型内部路径按记录的例外保留。
- 旧工作流/旧 CLI 行为回归通过。

## 例外与保留

- ARA 独立 NumPy 重采样（REAPER 内 OpenMP 死锁）不动。
- Comfy 原生节点（torch 直算）第 6 轮前不动，其后逐步薄包装化。
- 模型内部 DSP（STFT/内部插值）不插件化。
