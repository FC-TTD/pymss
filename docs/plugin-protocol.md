# pymss 插件协议（v1）

本文定义 pymss 插件的**清单、依赖、生命周期、贡献类型**，以及 pymss-studio 消费这些贡献的规则。pymss 是规则与运行时的实现方；宿主（CLI、pymss 工作流、ComfyUI 适配器、pymss-studio）消费同一份清单。

## 1. 插件包结构

```text
my-plugin/
├── pyproject.toml        # 身份、依赖、贡献声明（本协议）
├── __init__.py           # 默认入口模块（可覆盖 entry）
├── graphs/               # 可选：DAG JSON 文件（comfy-mss 格式）
│   └── chain.json
└── ui/                   # 可选：studio 页面静态产物
    └── index.html
```

## 2. 清单（pyproject.toml）

```toml
[project]
name = "pymss-plugin-my-chain"     # Python 包名
version = "1.2.0"                   # 插件版本（PEP 440）
dependencies = ["numpy>=1.26"]      # Python 包依赖（pip 解析）

[tool.pymss.plugin]
id = "author.my_chain"              # 插件身份：小写字母/数字/._-，建议含点
api_version = 1                     # 本协议版本
entry = "my_chain:setup"            # 可选；默认 "<目录名>:setup"
requires_pymss = ">=2.1"            # 可选：pymss 版本约束

[tool.pymss.plugin.dependencies]    # 插件间依赖（插件管理器解析）
"author.denoise" = ">=1,<2"

[[tool.pymss.plugin.contributions.graphs]]
id = "chain"
file = "graphs/chain.json"
operation = "author.my_chain.process"   # 由 setup() 注册的操作

[[tool.pymss.plugin.contributions.views]]
id = "editor"
title = "My Chain Editor"
entry = "ui/index.html"             # 相对插件目录，必须落在包内
placements = ["tool", "workspace"]  # tool=小工具页，workspace=独立页面
operation = "author.my_chain.process"
permissions = ["operations", "files.pickAudio", "app.info"]

[[tool.pymss.plugin.contributions.player]]
operation = "author.my_chain.process"
scopes = ["clip", "track"]
modes = ["rendered_preview", "offline_export"]
```

### 字段规则

- `id`：插件唯一身份；同一运行时内重复 id 报错。
- `api_version`：当前支持 `1`；声明更高版本时插件被 blocked，写明原因，不由 import 失败暴露。
- `entry`：`module:attr`，attr 是 `setup(handle)`；缺省取目录名包内的 `setup`。
- 版本约束（`requires_pymss`、插件依赖）支持子集：`==,>=,<=,>,<,~=`，按 PEP 440 风格比较。
- `contributions.*.entry/file` 必须解析到插件目录内部（拒绝 `..` 越界、绝对路径）。
- 无 `[tool.pymss.plugin]` 的目录 = **legacy 插件**：行为与旧版完全一致（`setup()` 或 import 副作用，按目录顺序加载）。

## 3. 贡献类型

| 类型 | 作用 | 消费方 |
|---|---|---|
| Operation | 可执行操作（`register_operation`），单一定义 | CLI、工作流节点（`op.<id>`）、Studio 插件操作自动表单、自定义页面 SDK |
| graph | 声明本插件附带 DAG 文件，并引用暴露的操作 | pymss 图运行时（子图/模板）；Studio 工作流后续接入 |
| view | studio 页面（静态 HTML/JS 产物） | pymss-studio 页面宿主 |
| player | 播放器处理入口（离线渲染/导出；实时需另行扩展） | pymss-studio 播放器（本期解析与校验，不在播放链路强制启用） |

**约束：**

- DAG 与页面不得各自定义参数；参数 schema 只存在于 Operation。
- view/page 只通过 SDK 调用操作，不直接访问宿主内部命令。
- 普通 DAG 默认离线执行；实时音频需要未来单独的流式接口，本协议不隐含。

## 4. 生命周期

```text
discover（读清单，不执行代码）
  → resolve（依赖检查：缺失/版本不满足/环 → blocked，保留原因）
  → activate（依赖序：先依赖后依赖方；setup() 注册贡献；单插件失败回滚自身）
  → deactivate（移除该插件注册的操作与贡献；被依赖时给出被依赖方警示）
```

- 加载报告向后兼容旧字段 `{name, loaded, error, operations}`，并新增：
  `plugin_id, version, api_version, status, blocked_by, dependencies, contributions`。
- `status ∈ {resolved, activated, blocked, failed}`。
- 插件间调用必须经由 Operation/Service 公共接口，不得 import 其他插件内部模块。

## 5. DAG 贡献执行（pymss 侧 API）

`pymss.plugins.graph_ops.build_graph_operation(...)`（由 setup 使用）：

```python
from pymss.plugins.graph_ops import build_graph_operation

plugin.register_operation(
    id="author.my_chain.process",
    ...,
    run=build_graph_operation(
        graph_file=Path(__file__).parent / "graphs/chain.json",
        audio_input={"node": "1", "widget": 0, "name": "audio"},   # 输入音频→load 节点
        param_bindings={"gain_db": {"nodes": ["2"]}},               # 参数→目标节点 data["params"]
        outputs={"audio": {"node": "4", "output": 0}},              # 指定节点输出→命名输出
    ),
)
```

执行语义：加载 comfy JSON → 应用输入/参数绑定 → `run_dag` → 按声明提取输出。绑定失败（节点缺失等）在安装/激活时即报错，而非运行时崩溃。

## 6. Studio SDK（消息协议）

页面宿主以 sandbox iframe 加载 `entry`，SDK 走 `postMessage` JSON-RPC：

```json
{"jsonrpc":"2.0","id":1,"method":"operations.describe","params":{"operation":"author.my_chain.process"}}
{"jsonrpc":"2.0","id":2,"method":"operations.start","params":{"operation":"...","inputs":{},"params":{}}}
{"jsonrpc":"2.0","id":3,"method":"files.pickAudio"}
{"jsonrpc":"2.0","id":4,"method":"app.info"}
```

- 事件：`{"method":"task.progress","params":{...}}`；`operations.start` 的结果在任务终态时随 `result` 返回。
- 宿主按清单 `permissions` 白名单放行方法，其余返回结构化错误码（`PERMISSION_DENIED`）。
- 客户端脚本由 Studio 打包提供，页面通过哈希参数 `#pymss-sdk=<url>` 获得注入地址（生产与 dev 均可用）。

## 7. 迁移

| 现状 | 行为 |
|---|---|
| 现有 `setup()` 插件（无清单） | 照旧加载，逐出报告标记 `api_version: 0`、`legacy: true` |
| 新插件 | 完整走清单 + 依赖解析 + 生命周期 |
| Comfy/market 安装路径 | 不变；清单只是随 pyproject 分发的额外节 |
