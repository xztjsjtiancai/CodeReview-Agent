# CodeReview Agent 设计文档

> 本文重点说明**为什么这样设计**，而非重复「是什么」。功能清单与使用方式见 [README.md](README.md)。

---

## 目录

1. [设计目标与约束](#1-设计目标与约束)
2. [关键技术决策记录](#2-关键技术决策记录)
3. [架构设计](#3-架构设计)
4. [Agent 循环详解](#4-agent-循环详解)
5. [工具抽象设计](#5-工具抽象设计)
6. [Prompt 工程](#6-prompt-工程)
7. [上下文记忆](#7-上下文记忆)
8. [错误处理体系](#8-错误处理体系)
9. [测试策略](#9-测试策略)
10. [开发过程中修复的缺陷](#10-开发过程中修复的缺陷)
11. [局限性与未来工作](#11-局限性与未来工作)

---

## 1. 设计目标与约束

### 1.1 来自作业的要求

| 要求 | 权重 | 本项目的应对 |
|---|---|---|
| 功能完整性 | 40% | 4 个工具协同 + 7 类确定性检查 + 行号定位 + 边界情况全覆盖 |
| Agent 架构 | 30% | 自研循环 + 双终止条件 + 工具抽象 + 记忆与裁剪 + 分层重试 |
| 代码质量 | 20% | 分层解耦 + 类型注解 + 115 项离线测试 + ruff 零告警 |
| 文档 | 10% | README（含架构与真实输出）+ 本文 |

硬性功能要求只有三条：Agent 循环、至少一种工具、命令行或简单 Web 界面。

### 1.2 由此推导出的设计约束

1. **架构必须可讲清**：评分看的是循环设计是否清晰，因此不引入会隐藏循环的框架
2. **结果必须可复现**：演示不能依赖模型当天的心情，因此把「确定性检查」从 LLM 手里拿走
3. **演示不能翻车**：终端环境（中文 Windows / GBK 代码页）必须处理
4. **成本必须可控**：按 token 计费，不能把整个项目塞进上下文

### 1.3 代码风格上的取舍

本项目在风格上明确**接受一定程度的重复**，具体表现：

- 不追求把每个小逻辑都抽成通用函数。像「从消息里取 content」这种三行判断，
  在需要的地方就近写，比跨模块引用一个 helper 更容易读懂。
- 工具之间可以有相似的参数校验写法，不强求抽公共基类之外的更多抽象。
- 每个模块的顶部用注释块交代「这个文件为什么存在、有哪些坑」，而不是把说明
  集中写进一份架构文档。读代码的人不必来回跳文件。

代价是行数变多、局部有重复；换来的是单个文件自洽、改动时影响面小。
这个取舍是有意为之，不是没做重构。

---

## 2. 关键技术决策记录

| # | 决策 | 备选方案 | 选择理由 | 代价 |
|---|---|---|---|---|
| 1 | **自研 Agent Loop** | LangChain / AutoGen / CrewAI | 30% 的架构分看的正是循环设计本身；框架会把 Thought/Action/Observation 藏在抽象层里，答辩时难以解释每一步 | 需自己处理工具协议、重试、上下文 |
| 2 | **标准库 `ast` 做静态分析** | 正则匹配 / 接入 pylint | `ast` 是语法树级分析，不会误报（例如字符串里的 `print` 不会被当成调用）；零依赖、规则完全可控 | 需要为每条规则写 visitor，工作量略大 |
| 3 | **工具返回结构化 JSON** | 工具直接返回自然语言 | JSON 让 LLM 负责「解释与建议」，工具负责「事实」；职责分离使二者可独立测试 | 需要设计 JSON 结构并写进 Prompt |
| 4 | **终端 CLI** | Web 界面 | 作业要求「命令行或简单界面」二者取一；界面分不在评分表里，Web 会挤占核心功能的开发时间 | 演示观感受限 |
| 5 | **一期关闭思考模式** | 开启思考模式 | DeepSeek 在携带 `tools` 的请求中要求完整回传历史 `reasoning_content`，漏传直接 400；一期优先保证链路稳定 | 复杂推理质量略降，二期可开 |
| 6 | **`messages.append(msg)` 原样入历史** | 手工拆字段重组为 dict | 这一行等价于完整保留 `content` + `reasoning_content` + `tool_calls`，是决策 5 的**逃生通道**：二期开思考模式零改造 | 消息对象不可序列化，`/history` 需另存轨迹 |
| 7 | **配置通过模块属性运行时读取** | `from config import CONFIG` | 后者在导入时绑定对象，测试替换配置后工具仍看旧值（实际踩过这个坑，见 §10.2） | 每次访问多一次属性查找 |
| 8 | **测试全部 mock LLM** | 打真实 API | 无 Key 可跑、无费用、秒级反馈；且能精确构造「工具报错」「非法 JSON」「步数超限」等边界场景 | 无法覆盖真实模型的措辞变化 |
| 9 | **异常体系分可重试/不可重试** | 统一捕获后重试 | 401 重试 3 次毫无意义还浪费 3 秒；把「能否重试」的决策收拢到异常定义里，避免散落各处 | 需要维护状态码映射表 |
| 10 | **ASCII 降级开关** | 只用 emoji | CMD 默认代码页为 GBK，emoji 与框线会乱码甚至直接抛 `UnicodeEncodeError` 让程序崩掉 | 多一条渲染分支 |

---

## 3. 架构设计

### 3.1 分层与依赖方向

```text
┌──────────────────────────────────────────────────────────┐
│ main.py                    CLI / REPL 层                  │
│  · 读取输入、识别命令、调用渲染层                          │
│  · 不含任何分析逻辑                                        │
└───────────────────────────┬──────────────────────────────┘
                            │ 自然语言 / 命令
┌───────────────────────────▼──────────────────────────────┐
│ agent/agent.py             Agent Controller               │
│  · 驱动循环、控制步数、判断终止、派发工具、发事件            │
│  · ★ 唯一同时了解「LLM」与「工具」的模块                    │
└────┬────────────────┬────────────────┬───────────────────┘
     │                │                │
┌────▼──────┐  ┌──────▼───────┐  ┌─────▼──────────┐
│ memory.py │  │ tool registry│  │ llm/client.py  │
│ 历史/裁剪  │  │ 注册/派发     │  │ 通信/重试       │
└───────────┘  └──────┬───────┘  └─────┬──────────┘
                      │                │
        ┌─────────────┼──────────┐     │
        ▼             ▼          ▼     ▼
   file_tool    ast_tool   search_tool  DeepSeek API
   （纯计算，不认识 LLM）                （只收发消息，不认识工具）
```

**依赖方向是单向的**：`main → agent → {memory, tools, llm}`，`tools` 与 `llm` 之间没有依赖。
这条规则带来两个直接好处：

- `tools/*` 可以脱离 LLM 单独做单元测试（21 项文件工具测试、18 项 AST 测试就是这么来的）
- `llm/client.py` 可以整体替换（离线演示脚本正是替换了它）

### 3.2 数据流

```text
用户输入 ──► Memory(user 消息) ──► LLMClient.chat(历史 + 工具 Schema)
                                          │
                              ┌───────────┴────────────┐
                              ▼                        ▼
                     无 tool_calls              有 tool_calls
                              │                        │
                              ▼                        ▼
                     返回最终答案             ToolRegistry.dispatch()
                     （交给渲染层）                   │
                                             ┌────────┴────────┐
                                             ▼                 ▼
                                        成功：结果         失败：ToolError
                                             │                 │
                                             └────────┬────────┘
                                                      ▼
                                       Memory(tool 消息) ──► 进入下一轮
```

---

## 4. Agent 循环详解

### 4.1 循环结构

```text
for step in 1..max_steps:
    memory.maybe_trim()                             # ① 控制上下文体积
    message = llm.chat(memory.messages, tools)      # ② 推理
    memory.append(message)                          # ③ 原样保留消息对象
    if not message.tool_calls:                      # ④ 终止条件 A
        return message.content                      #    → 输出
    for call in message.tool_calls:                 # ⑤ 工具调用
        result = dispatch_or_error(call)            #    错误也变成结果
        memory.append(tool_message(call.id, result))
raise MaxStepsExceeded                              # ⑥ 终止条件 B（兜底）
```

### 4.2 双终止条件

| 条件 | 触发 | 行为 |
|---|---|---|
| A：模型不再请求工具 | 正常情况 | 返回内容作为最终答案 |
| B：达到 `max_steps` | 模型陷入循环或任务过大 | 捕获 `MaxStepsExceeded`，返回「未收敛」提示 + 已有信息，**不抛给用户** |

为什么 B 必须优雅兜底：Agent 面对模糊请求时可能反复调用工具。若此时抛异常，用户看到的是
traceback 而不是「我已经查到的部分结论」，体验会明显变差。因此 `run()` 内部捕获该异常并
转成一条说明性回复。

### 4.3 为什么「工具错误」要转成 Observation

```python
try:
    result = self.registry.dispatch(name, arguments)
except AgentError as exc:
    return {"error": exc.message, "hint": exc.hint}, False  # ← 作为 Observation 回灌
```

这带来一个可见的行为改进：当模型把路径写成 `test.py`（实际不存在）时，它会收到
`{"error": "File not found: test.py", "hint": "可用 list_dir 查看目录内容"}`，
下一轮就会自己改用 `list_dir` 探查或修正路径。这类「自我纠正」是 Agent 与「一次性问答」
的关键区别，也是架构分的体现。

同时这也意味着工具实现里可以放心抛 `ToolError`，不必在每个工具里写防御式返回。

### 4.4 事件回调：把「编排」与「展示」解耦

Agent 不直接 `print`，而是发出事件：

```python
{"type": "assistant", "step": 1, "content": "我先读取这个文件。"}
{"type": "tool_start", "step": 1, "name": "read_file", "arguments": {...}}
{"type": "tool_end", "step": 1, "name": "read_file", "ok": True, "result": {...}}
{"type": "error", "message": "达到最大步数上限……"}
```

好处：

- Agent 可脱离终端测试（测试里把事件收集进一个 list 直接断言）
- 换界面（Web / JSON 日志）无需改 Agent
- 最终答案**不作为事件输出**，由调用方统一渲染，避免同一段文字被打印两次（这是开发中实际发现并修掉的问题，见 §10.3）

### 4.5 工具结果缓存

同一轮对话里，相同工具 + 相同参数只真正执行一次：

```python
key = self.memory.cache_key(name, arguments)  # f"{name}:{json.dumps(args, sort_keys=True)}"
```

用 `sort_keys=True` 是为了让 `{"a":1,"b":2}` 与 `{"b":2,"a":1}` 命中同一个缓存。
命中时结果里会带 `"cached": true`，界面上显示「命中缓存，未重复执行」，便于观察也便于测试断言。

---

## 5. 工具抽象设计

### 5.1 Tool 协议

```python
class Tool(ABC):
    name: str  # 模型据此调用
    description: str  # ★ 决定模型能否选对工具
    parameters: dict  # JSON Schema

    @abstractmethod
    def run(self, **kwargs) -> dict: ...  # 必须返回 JSON 可序列化的 dict

    def validate(self, arguments) -> dict: ...  # 统一参数校验与类型强制
    def schema(self) -> dict: ...  # 导出 OpenAI 兼容定义
```

三个设计要点：

1. **`description` 是接口的一部分**。工具的可用性上限由描述质量决定——`read_file` 的描述里
   明确写了「读取代码前必须先调用本工具，不要凭猜测评价未读过的文件」，这是在用接口文档
   约束模型行为。
2. **`validate` 集中做参数校验**。模型常把整数写成字符串（`"times": "3"`），这里做
   宽松强制转换；同时拒绝未知参数——拼错的参数名如果被静默忽略，模型会以为调用成功了。
3. **返回结构化 dict 而非自然语言**。见决策 3。

### 5.2 ToolRegistry 的职责

| 方法 | 作用 | 关键实现 |
|---|---|---|
| `register` | 注册工具 | 校验 `name` 非空 |
| `schemas` | 导出全部 Schema 给 LLM | 直接映射 `Tool.schema()` |
| `dispatch` | 派发调用 | 参数解析 → 校验 → 执行，异常统一包装 |
| `describe` | 供 `/tools` 命令展示 | 与 `schemas` 分开，因为面向对象不同 |

`dispatch` 是「模型意图」与「本地能力」的唯一连接点，也是安全边界：所有路径校验都发生在
工具内部，但统一经由这里进入。

### 5.3 路径安全

```python
resolved = candidate.resolve()
if resolved != root and root not in resolved.parents:
    raise ToolError(f"拒绝访问工作目录之外的路径：{path_str}")
```

为什么要防：模型可能被用户输入诱导（「读取 ../../.env 里的配置」），也可能单纯写错路径。
`resolve()` 会展开 `..` 与符号链接，因此 `../../etc/passwd` 会被拦下。这是防止 API Key
等敏感文件被读入上下文并可能回显给用户的必要防线。

### 5.4 为什么静态分析用 `ast` 而不是正则

对比「查找裸 except」：

| 方式 | 字符串 `"except:"` | 注释里的 `# except:` | 语法错误文件 |
|---|---|---|---|
| 正则 | 匹配到，但可能来自字符串字面量 | 误报 | 仍能匹配（但不可信） |
| `ast` | 精确命中 `ExceptHandler(type=None)` | 不误报 | 明确报告语法错误 |

`ast` 的代价是需要为每条规则写 visitor（`visit_ExceptHandler`、`visit_Call`、
`visit_FunctionDef` 等），收益是**零误报**——而「审查结果可信」正是本项目相对于
「把代码贴给 ChatGPT」的核心价值。

嵌套深度检测值得单独说明：`_nesting_depth` 递归下探嵌套语句，取最深分支。
不这样做的话（例如只数 `If` 节点总数），会把「四个并列的 if」误判为「四层嵌套」。

---

## 6. Prompt 工程

### 6.1 System Prompt 的六段结构

| 段落 | 目的 | 对应问题 |
|---|---|---|
| 能力边界 | 明确「你看不到文件，只能通过工具获取信息」 | 防止模型凭空编造代码内容 |
| 工具清单与用法 | 列出 4 个工具的适用场景 | 提高工具选择准确率 |
| 工作纪律 | 先读后评、先静态后语义、行号必须来自工具 | **抑制幻觉的核心** |
| 输出格式 | 固定模板，不用 Markdown 表格 | 便于终端渲染；表格在窄终端会错位 |
| 严重等级判据 | 明确 HIGH/MEDIUM/LOW 的界定 | 防止所有问题都被标成 HIGH |
| 语言 | 中文回答 | 与演示环境一致 |

### 6.2 迭代过程

以「行号」这一项为例，需求是这样逐步收敛的：

1. 初版没有约束 → 模型偶尔给出「第 8 行左右」这种模糊定位
2. 加入「行号必须与工具返回一致」 → 定位变准，但仍会自行推算
3. `read_file` 返回内容自带行号前缀 → 模型有了可直接引用的依据，错误基本消失

这体现了一个原则：**能用工具输出解决的问题，不要靠 Prompt 约束**。Prompt 是软约束，
工具输出是硬事实。

另有两处约束是在真实 API 实测后才补上的，说明 Prompt 必须用真实模型验证（详见 §11.2）：

- 「**所有输出**都必须使用中文，包括调用工具前的一句话说明」——初版只写了「始终使用中文
  回答」，模型把它理解为仅约束最终回答，开场白仍是英文
- 「不做多余探查：用户已给出明确文件路径时，直接读取该文件」——初版未限制，
  模型会先额外调用一次 `list_dir`

### 6.3 为什么要求「不要罗列工具输出」

`ast_analyzer` 的 message 是给机器看的原始描述，例如「使用了裸 except，会吞掉包括
KeyboardInterrupt、SystemExit 在内的所有异常」。如果模型原样复述，就等于把静态分析的
输出转了一道手，没有增量价值。

因此 Prompt 明确要求：**解释它的影响，并给出具体可执行的修改建议**。这才形成了
「AST 提供事实 + LLM 提供判断」的分工，也是本项目区别于「套壳」的关键。

---

## 7. 上下文记忆

### 7.1 数据结构

```python
class Memory:
    _system_message: dict  # System Prompt
    _messages: list[Any]  # 完整消息（dict 与 SDK 消息对象混存）
    _tool_cache: dict[str, dict]  # 工具结果缓存
    _trail: list[dict]  # 人类可读轨迹，供 /history
```

`_trail` 与 `_messages` 分开的原因：`_messages` 里存的是 SDK 消息对象（不可 JSON 序列化、
不适合直接展示），而 `/history` 需要的是「谁在什么时候调了什么工具」这种可读记录。

### 7.2 裁剪策略的演进（三次修正）

这是本项目改动最多的一处，过程值得记录：

| 版本 | 策略 | 问题 |
|---|---|---|
| v1 | 保留最近 N 条，压缩中间轮次 | 切分点可能落在 `tool` 消息上 → 悬空 `tool_call_id` → API 返回 400 |
| v2 | 修正切分点对齐 | 只压缩不丢弃。实测发现**窗口越宽总量反而越大**——因为只有 `tool` 消息压得掉，普通消息压不掉，中间段反而更长 |
| v3（最终） | **从末尾向前累积，装不下就整段丢弃最旧的** | — |

v3 的核心是**预留预算**：裁剪说明本身也占字符，必须先扣掉，否则裁完仍会略微超限
（实测超出约 100 字符）。最终实现：

```python
budget = self.max_context_chars - note_reserve  # 预留说明文字
used = len(content_of(head[0]))
for index in range(len(messages) - 1, 0, -1):  # 从末尾向前
    cost = len(content_of(messages[index])) + 16  # 结构开销估计
    if used + cost > budget:
        break
    used += cost
    split = index
while split > 1 and role_of(messages[split]) == "tool":  # 对齐约束
    split -= 1
```

### 7.3 为什么保留第一条 user 消息

第一条 user 消息承载了**任务目标**（「帮我审查 examples/bad_code.py」）。如果它被裁掉，
模型会失去「我在做什么」的锚点，后续追问容易跑偏。因此无论怎么裁剪，`head` 始终保留。

---

## 8. 错误处理体系

### 8.1 异常层级

```text
AgentError                  ← 所有可预期错误的基类
├── LLMError                ← retryable = False（默认不可重试）
│   ├── LLMRetryableError   ← retryable = True（超时/连接/429/5xx）
│   ├── LLMAuthError        ← 401/403
│   ├── LLMBalanceError     ← 402
│   ├── LLMBadRequestError  ← 400
│   └── LLMResponseError    ← HTTP 200 但结构异常
├── ToolError
│   ├── ToolNotFound
│   └── ToolArgumentError
└── MaxStepsExceeded
```

### 8.2 把重试决策收拢到异常定义

```python
class LLMError(AgentError):
    retryable: bool = False


class LLMRetryableError(LLMError):
    retryable = True
```

客户端只需读 `error.retryable`，不必写 `if isinstance(...) or isinstance(...)` 的长条件。
新增错误类型时，只需在类上声明一次。

### 8.3 状态码分类表与理由

| 状态码 | 异常 | 重试 | 理由 |
|---|---|---|---|
| 401 / 403 | `LLMAuthError` | ❌ | Key 错了，重试 3 次只是浪费 3 秒 |
| 402 | `LLMBalanceError` | ❌ | 余额不足，必须充值 |
| 400 | `LLMBadRequestError` | ❌ | 请求本身非法（如思考模式下传 temperature） |
| 429 | `LLMRetryableError` | ✅ | 限流是瞬时的 |
| 5xx | `LLMRetryableError` | ✅ | 服务端抖动 |
| 超时/连接 | `LLMRetryableError` | ✅ | 网络抖动 |

退避公式：`1s × 2^(attempt-1) + random(0, 0.3s)`。加抖动是为了避免多个请求同时重试
再次打满限流。

**重试过程必须在终端可见**。作业明确要求「错误处理与重试机制」，静默重试等于没做这一项：

```text
! LLM 请求失败（第 1/3 次）：请求被限流（429）。 → 1.2s 后重试
```

### 8.4 工具层的错误处理原则

工具应抛出**可读的** `ToolError`，消息面向模型与用户，不含 traceback：

```python
raise ToolError(
    f"File not found: {path}",
    "请确认路径是否正确；可用 list_dir 查看目录内容。",
)
```

`hint` 字段是给模型的自救提示。实测中模型会依据 hint 换用 `list_dir`，
这正是「错误作为 Observation」的价值所在。

---

## 9. 测试策略

### 9.1 为什么全部 mock LLM

| 维度 | mock | 真实 API |
|---|---|---|
| 是否需要 Key | 否 | 是 |
| 费用 | 0 | 每次约 0.01 元 |
| 速度 | 0.6 秒 / 115 项 | 每项数秒 |
| 可复现性 | 完全确定 | 受模型输出波动影响 |
| 能否构造边界场景 | 能（非法 JSON、未知工具、步数超限） | 很难 |

「工具返回非法 JSON」「模型请求不存在的工具」这类场景，用 mock 可以精确构造；
打真实 API 几乎无法触发。因此**边界场景测试用 mock 是必要的，不是偷懒**。

### 9.2 分布

| 文件 | 用例数 | 覆盖重点 |
|---|---|---|
| `test_ast_tool.py` | 18 | 7 类规则的正反例、行号精确性、语法错误路径、示例文件回归 |
| `test_file_tool.py` | 21 | 边界矩阵（不存在/空/超大/二进制/GBK/越界/行范围） |
| `test_agent_loop.py` | 15 | 双终止条件、链路顺序、错误回灌、缓存、记忆与 `/clear` |
| `test_memory_and_registry.py` | 30 | 裁剪与配对约束、工具注册与参数校验 |
| `test_cli.py` | 31 | 命令解析与分发、五个内置命令、`/clear` 后确实失忆 |
| **合计** | **115** | **全部无需 API Key** |

### 9.3 两个「回归测试」

有两类测试的价值不在验证功能，而在防止已经踩过的坑再次出现：

1. **`test_empty_memory_is_falsy_which_is_a_known_trap`**
   `Memory` 定义了 `__len__`，因此空 Memory 的布尔值为 `False`。若 Agent 写成
   `memory or Memory()`，调用方传入的空内存会被丢弃。这个用例把陷阱固定下来。

2. **`test_trim_preserves_tool_call_id_pairing`**
   断言裁剪后每条 `tool` 消息都能找到对应的 `assistant.tool_calls`。
   这条约束一旦被破坏，真实 API 会直接返回 400，而 mock 测试不会报错——所以必须显式断言。

### 9.4 示例文件的回归测试

`test_bad_code_example_triggers_all_ast_rules` 与
`test_good_code_example_has_no_high_severity_issues` 保证演示效果可复现：

- `bad_code.py` 必须稳定触发 4 条 AST 规则
- `good_code.py` 必须 0 高危问题 —— 这是「不硬凑问题」的证据

如果后续调整了某条规则的判定逻辑，这两个用例会立刻发现演示效果被破坏。

---

## 10. 开发过程中修复的缺陷

记录下来是因为它们都有一定代表性。

### 10.1 每轮对话都在丢失上下文（严重）

```python
self.memory = memory or Memory()  # ← 错误
```

`Memory` 定义了 `__len__`，空 Memory 的布尔值是 `False`。于是**调用方传入的内存对象被整个
丢弃**，Agent 每轮都从零开始。后果是多轮追问和 `/clear` 全部失效——而这恰好是作业明确
推荐实现的两项功能。

```python
self.memory = memory if memory is not None else Memory()  # ← 修正
```

教训：对实现了 `__len__` / `__bool__` 的对象，永远用 `is None` 判断而非真值测试。

### 10.2 配置替换对工具不可见

工具里写 `from config import CONFIG`，导入时就把对象绑定到局部名字了。测试替换配置后，
工具仍读旧值，导致「路径逃逸」等测试无法进行。

修正：工具统一改为运行时访问 `config_module.CONFIG`。同时测试夹具改为替换
`sys.modules["config"]` 模块对象（而重新执行工具模块会产生重复的类对象，破坏 `isinstance`）。

### 10.3 最终报告被打印两次

Agent 把每一步的 assistant 文本都作为事件抛出，其中包含最终答案；而 `main.py`
又把返回值渲染成报告面板。结果同一段文字出现两次。

修正：Agent 只在「后面还有工具调用」时才抛出 assistant 事件——即该事件只承载**中间话术**，
最终答案由调用方统一渲染。修改后事件序列更清晰，也顺带简化了测试断言。

### 10.4 AST 工具行号偏移

`analyze_file` 把裸文件名当成 `filename` 参数传入，导致 `SyntaxError` 路径下行号计算偏移。
而这个工具的全部价值恰恰在于行号准确。

另有一次是**文档与实际不符**：示例文件的注释里写的行号是早期版本的，代码调整后没有同步。
这类问题不会让程序报错，却会让演示时念错行号。已通过回归测试（断言具体行号）兜住。

### 10.5 思考模式与 `tools` 的兼容约束

DeepSeek 文档明确：携带 `tools` 的请求，历史每一轮的 `reasoning_content` **必须完整回传**，
漏传返回 400。若按常见教程那样手工拼 `{"role": "assistant", "content": ...}`，必然踩坑。

本项目一次性规避：一期关闭思考模式，同时历史一律 `append(response.choices[0].message)`
原样保留。二期若要开启，无需改动循环。

---

## 11. 局限性与未来工作

### 11.1 当前局限

| 局限 | 原因 | 影响 |
|---|---|---|
| 仅支持 Python 静态分析 | `ast` 是 Python 标准库 | 审查其他语言时只剩 LLM 的语义判断 |
| 不做自动修码 | 有意选择：自动改动风险高、难以验证 | 用户需手工应用建议 |
| 裁剪策略粗粒度 | 丢弃最旧轮次，不做摘要压缩 | 长会话可能丢失早期结论 |
| 未使用 `strict` 模式 | 一期优先把容错逻辑写出来 | 模型返回非法 JSON 时需靠回灌机制恢复 |
| 报告格式偶尔超出约定 | 模型自由发挥，Prompt 属软约束 | 个别轮次带 Markdown 标题或表格，终端仍可读但不完全一致 |

### 11.2 真实 API 验证已完成（含由它发现的两个缺陷）

早期版本把「未在真实 API 上回归」列为首要缺口，该缺口现已关闭。真实 DeepSeek API
（`deepseek-flash`）实测结论：

| 验证项 | 期望 | 实测 |
|---|---|---|
| 工具调用序列 | `read_file` → `ast_analyzer` | ✅ `['read_file', 'ast_analyzer']`，顺序正确 |
| 多余探查 | 无 | ✅ 无 `list_dir` / `search_code` |
| 输出语言 | 全程中文 | ✅ 含工具调用前的说明语句 |
| 多轮记忆 | 追问时首轮请求仍在上下文 | ✅ user 轮次 = 2 |
| 首轮耗时 | 可接受 | ✅ 约 5.9 秒 |
| 审查质量 | 能解释语义影响并给建议 | ✅ 正确指出 `eval` 注入风险，并串起「裸 except → 未定义变量」的因果链 |

更重要的是，**第一轮真实实测直接暴露了两个 mock 测试无法发现的 Prompt 缺陷**：

1. **开场白是英文**（`I'll start by reading the file and running static analysis...`）。
   根因是 System Prompt 的「语言」段落写的是「始终使用中文回答」，被模型理解为
   仅约束最终回答。修正为：「**所有输出**都必须使用中文，**包括调用工具前的一句话说明**」。
2. **多余的工具调用**：路径已明确给出，模型仍先调用了一次 `list_dir` 探查目录。
   修正为新增纪律「不做多余探查……每一次多余的调用都会增加等待时间与费用」。

修正后重跑，两项问题均消失，工具序列稳定为 `read_file → ast_analyzer`。

这验证了一个容易被忽略的判断：**mock 测试保证循环正确性，但测不出措辞层面的行为偏差**。
两者不是替代关系，而是互补——mock 覆盖边界与异常路径，真实 API 覆盖 Prompt 的实际效果。

### 11.3 改进优先级

| 优先级 | 改进 | 理由 |
|---|---|---|
| P1 | `/scan <目录>` 批量审查 | 演示效果最好，也最实用 |
| P1 | 接入 `strict` 模式 | 用 JSON Schema 强约束输出，可删除一类容错代码 |
| P1 | 报告格式收窄 | 用更强的结构约束（或 `strict`）让输出与模板完全一致 |
| P2 | 开启思考模式 | 提升复杂推理质量；循环已预留零改造空间 |
| P2 | `run_tests` 工具 | 让 LLM 解读测试失败原因，形成「审查 → 验证」闭环 |
| P3 | 报告导出 Markdown | 便于归档与提交 |
| P3 | 摘要式上下文压缩 | 替代当前的丢弃策略，保留早期结论 |

### 11.4 如果重做一次

- **更早建立离线演示脚本**。它既是文档素材，也是渲染层与整链路的第一道验收，能提前暴露
  10.3 这类重复输出问题。
- **更早接入真实 API 做一次冒烟**。11.2 说明 Prompt 的行为偏差只有真实模型能暴露；
  若在第一轮编码后就跑一次真实调用，这两处措辞问题可以更早发现。
- **配置注入而非模块单例**。当前用模块属性替换来实现测试隔离，虽然可行但略显迂回；
  把 `workspace_root` 作为工具构造参数会更直接。
- **先写行号回归测试再写规则**。行号是本工具的核心承诺，应该被测试最先固定下来。
