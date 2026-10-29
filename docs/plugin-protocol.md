# pymss 插件协议（v2）

本文定义 pymss 插件的**清单、依赖、生命周期、贡献类型**。pymss 是规则与运行时的实现方；宿主（CLI、pymss 工作流、ComfyUI 适配器、pymss-studio）消费同一份清单。宿主专属的声明（例如 Studio 的页面）放在各自的 `[tool.pymss.<host>]` 表中，pymss 不读取。

## 1. 插件包结构

```text
my-plugin/
├── pyproject.toml        # 身份、依赖、操作与贡献声明（本协议）
├── __init__.py           # 默认入口模块（可覆盖 entry）
├── graphs/               # 可选：DAG JSON 文件（comfy-mss 格式）
│   └── chain.json
└── ui/                   # 可选：宿主页面产物，由 [tool.pymss.studio] 声明
    └── dist/index.html
```

## 2. 清单（pyproject.toml）

```toml
[project]
name = "pymss-plugin-my-chain"     # Python 包名
version = "1.2.0"                   # 插件版本（PEP 440）
dependencies = ["numpy>=1.26"]      # Python 包依赖（pip 解析）

[tool.pymss.plugin]
id = "author.my_chain"              # 插件身份：小写字母/数字/._-，建议含点；也是安装目录名
api_version = 2                     # 本协议版本，必须为 2
entry = "__init__:setup"            # 可选；缺省即 "__init__:setup"
requires_pymss = ">=2.2"            # 可选：pymss 版本约束

[tool.pymss.plugin.dependencies]    # 插件间依赖（插件管理器解析）
"author.denoise" = ">=1,<2"

[[tool.pymss.plugin.operations]]    # setup() 注册的全部操作，必须与实际注册完全一致
id = "author.my_chain.process"
title = "Process"

[[tool.pymss.plugin.contributions.graphs]]
id = "chain"
file = "graphs/chain.json"
operation = "author.my_chain.process"

[[tool.pymss.plugin.contributions.nodes]]
type = "MyChainNode"
title = "My Chain"
```

### 字段规则

- `id`：插件唯一身份；使用点分隔非空命名段，每段以小写 ASCII 字母或数字开头，其余字符允许小写字母、数字、下划线和连字符，例如 `author.my-plugin`。同一运行时内重复 id 报错。安装器把插件放在 `<plugins_dir>/<id>`；Studio 安装目录要求插件 id 不超过 100 个字符。
- `api_version`：只接受 `2`。其他值在发现阶段即 blocked，不会导入插件代码。
- `entry`：`module:attr`，attr 是 `setup(handle)`；`__init__` 指插件根包本身。
- `operations`：静态声明，宿主无需执行代码就能列出插件提供的操作。id 使用相同的命名段规则，必须以 `<插件 id>.` 开头并追加至少一个非空命名段，且不重复。清单和运行时共享校验规则。`setup()` 注册的集合与声明不一致时，激活失败并回滚。
- 版本约束（`requires_pymss`、插件依赖）支持子集：`==,>=,<=,>,<,~=`，按 PEP 440 风格比较。
- `contributions.graphs.file` 必须解析到插件目录内部（拒绝 `..` 越界、绝对路径、越界符号链接）。
- 没有 `pyproject.toml` 或 `[tool.pymss.plugin]` 的目录不再加载，报告中给出原因。

## 3. 贡献类型

| 类型 | 作用 | 消费方 |
|---|---|---|
| Operation | 可执行操作（`register_operation`），单一定义，在清单中静态声明 | CLI、工作流节点（`op.<id>`）、Studio 自动表单与自定义页面 |
| graph | 声明本插件附带 DAG 文件，并引用暴露的操作 | pymss 图运行时（子图/模板） |
| node | 工作流节点类型的纯数据声明 | 工作流编辑器 |

**约束：**

- DAG 与页面不得各自定义参数；参数 schema 只存在于 Operation。
- 普通 DAG 默认离线执行；实时音频需要未来单独的流式接口，本协议不隐含。
- 页面、播放器等宿主贡献由宿主定义，例如 Studio 读取 `[[tool.pymss.studio.views]]`。

## 4. 生命周期

```text
discover（读清单，不执行代码；可同时扫描多个插件根目录）
  → resolve（依赖检查：缺失/版本不满足/环 → blocked，保留原因）
  → activate（依赖序：先依赖后依赖方；setup() 注册贡献；核对声明的操作；单插件失败回滚自身）
```

- 宿主显式传入插件根目录：`Runtime.load_plugins(roots=[...])`、`bootstrap(roots=[...])`。未传入时使用 `get_plugins_dir()`（`$PYMSS_PLUGINS_DIR` 或 `~/.pymss/plugins`）。
- 加载报告每行包含 `name, path, loaded, error`；清单有效时另含 `plugin_id, version, api_version, requires_pymss, dependencies, operations, status, blocked_by`，激活成功时含 `contributions`。
- `status ∈ {resolved, activated, blocked, failed}`。
- 插件间调用必须经由 Operation/Service 公共接口，不得 import 其他插件内部模块。
- 安装（`pymss.plugins.install.install`）接受 `plugins_dir` 与 `registry_url` 参数；registry 以插件 id 为键。

## 5. 进度与插件工具包

`OperationContext.report_progress(fraction, message="", *, event=None)`：`event` 携带结构化宿主数据（`{"type": ..., "payload": {...}}`），宿主的 `progress` 回调签名为 `(fraction, message, event)`。

`pymss.plugins.kit` 提供插件常用的公共实现：

| API | 用途 |
|---|---|
| `register_tool(plugin, id, handler, *, title, tool=None)` | 注册以 `request` 对象为输入、返回 `report` 的文件工具操作 |
| `operation_context(ctx)` / `emit(type, payload)` | 在嵌套代码中上报结构化进度（`completed/total` 决定比例），并检查取消 |
| `isolate_stdout()` | 把第三方库的输出重定向到 stderr |
| `require_file` / `require_directory` / `available_path` | 路径校验与不覆盖的文件名分配 |
| `render_output` / `publish_export_path` | 先写私有临时文件，再原子发布到最终路径 |
| `ffmpeg_path` / `ffprobe_path` / `run_ffmpeg` / `run_process` / `subprocess_detail` | 外部进程调用 |
| `ToolError` | 携带 `code`、`recoverable`、`extra` 的工具错误 |

## 6. DAG 贡献执行（pymss 侧 API）

`pymss.plugins.graph_ops.build_graph_operation(...)`（由 setup 使用）：

```python
from pymss.plugins.graph_ops import build_graph_operation

plugin.register_operation(
    id="author.my_chain.process",
    ...,
    run=build_graph_operation(
        graph_file=Path(__file__).parent / "graphs/chain.json",
        audio_input={"node": "1", "widget": 0, "name": "audio"},   # 输入音频→load 节点
        param_bindings={"gain_db": {"nodes": ["2"], "widget": 0}},  # 参数→音量节点第一个 widget
        outputs={"audio": {"node": "4", "output": 0}},              # 指定节点输出→命名输出
    ),
)
```

执行语义：加载 comfy JSON → 应用输入/参数绑定 → `run_dag` → 按声明提取输出。绑定失败（节点缺失等）在安装/激活时即报错，而非运行时崩溃。

参数绑定始终写入目标节点的 `data["params"]`。内置 Comfy 节点使用 `widgets_values`，因此需指定 `widget`，其索引必须是图中已存在的非负整数位置；省略时保留仅写 `data["params"]` 的插件节点行为。输出端口必须存在，不会回退到其他端口。每次执行使用独立临时目录，完成或失败后清理。

音频输入绑定支持 `pymss_load_audio` 及其 `LoadAudio` 别名：`audio_input.widget=0` 写入临时音频路径，`widget=1` 写入运行时输入名；其他位置在构建时拒绝。

## 7. 校验

`pymss plugins validate <目录>` 只解析清单，打印插件 id、版本和声明的操作，不执行插件代码。

## Migrating from v1

pymss 2.2 的最低 Python 版本为 3.11，升级前请确认宿主使用的解释器满足要求。

v2 是破坏性升级，v1 插件和无清单插件都不再加载：

| v1 | v2 |
|---|---|
| `api_version = 1` | `api_version = 2` |
| 操作只存在于 `setup()` | 在 `[[tool.pymss.plugin.operations]]` 中逐一声明 |
| `entry` 缺省为 `<目录名>:setup` | 缺省为 `__init__:setup` |
| `contributions.views` / `contributions.player` | 移到宿主表，例如 `[[tool.pymss.studio.views]]` |
| `[project.entry-points."pymss.plugins"]` | 删除；插件只从插件目录加载 |
| import 时调用 `register_capability/register_node` | 移进 `setup(plugin)`，使用 `plugin.register_*` |
| 复制的 `protocol.py`/`entry.py`/`common.py` | 改用 `pymss.plugins.kit` |
| 进度消息里的 JSON 字符串 | `report_progress(..., event=...)` 或 `kit.emit` |
| 安装目录 = 源目录名 | 安装目录 = 插件 id |
