# xztcode

在终端里跑的代码审查助手。用一句自然语言说明要看什么，Agent 自己决定读哪个文件、
跑哪些检查，最后交回一份带行号和严重等级的审查报告。

它是刻意做成「静态分析 + 大模型」两层结构的：标准库 `ast` 负责给出确定的事实，
模型负责解释这些事实在业务上意味着什么、该怎么改。这样既没有纯静态分析那种
只会报「这里是 debug print」的死板，也没有纯让模型看代码时那种凭空编造行号的风险。

跑起来大概长这样：

```text
╔══════════════════════════════════════════════╗
║                 xztcode v1.0                 ║
║             AI Code Review Agent             ║
╚══════════════════════════════════════════════╝
Model: deepseek-flash  |  Thinking: disabled  |  Max steps: 8  |  Key: sk-ee5...5107
输入 /help 查看命令。

You: /review examples/bad_code.py

Agent 我先读取这个文件。
  → read_file path=examples/bad_code.py
     ✓ examples\bad_code.py · 36 行
  → ast_analyzer path=examples/bad_code.py
     ✓ 4 个问题 (high 1 / medium 2 / low 1)

╭──────────────────────────────── CODE REVIEW RESULT ─────────────────────────────────╮
│ 🔴 HIGH      L36                                                                     │
│ `eval(expression)` 会执行任意代码，是明确的注入风险。                                 │
│ 建议：用 `ast.literal_eval` 或 `int()` 等受限解析替代。                               │
│                                                                                      │
│ 🟡 MEDIUM    L21                                                                     │
│ 裸 except 会吞掉包括 KeyboardInterrupt、SystemExit 在内的所有异常，错误被静默掩盖。   │
│ 建议：改为 `except ZeroDivisionError as exc:` 并记录日志。                            │
│                                                                                      │
│ 🔴 HIGH      L20                                                                     │
│ `a / b` 没有校验 b 是否为 0；异常又恰好被上面的裸 except 吞掉，于是 L24 的 print      │
│ 会以 UnboundLocalError 的形式炸出来，报错点和病因完全分离。                           │
│ 建议：除法前显式判断 `if b == 0: return None`。                                       │
╰──────────────────────────────────────────────────────────────────────────────────────╯
```

---

## 能做什么

- 用自然语言下指令，不用背命令。`帮我审查 examples/bad_code.py` 和
  `/review examples/bad_code.py` 走的是同一条链路。
- 完整的 Agent 循环：输入 → 推理 → 工具调用 → 观测 → 再推理 → 输出。
  中间每一步都在终端里看得见，不是黑盒。
- 四个工具可组合：读文件、列目录、AST 静态分析、跨文件搜索。
- 静态分析是确定性的。七类检查全部基于语法树，不依赖模型判断，也就没有幻觉。
- 能追问。看完报告问「详细解释第二个问题」，它知道你在说哪一条。
- 出错不崩。网络抖动会退避重试；工具失败会变成一条反馈让模型自己换策略；
  路径写错、文件是二进制、编码是 GBK，各有各的处理。

## 跑起来需要什么

- Python 3.10 或更高（开发和验证用的是 3.12）
- 能访问 `https://api.deepseek.com`
- 一个 DeepSeek API Key：[platform.deepseek.com/api_keys](https://platform.deepseek.com/api_keys)
- Windows / macOS / Linux 都行

## 安装

```powershell
# 1) 建虚拟环境
python -m venv .venv
.venv\Scripts\activate          # macOS/Linux: source .venv/bin/activate

# 2) 装依赖
pip install -r requirements.txt

# 3) 配 Key
copy .env.example .env          # macOS/Linux: cp .env.example .env
#    然后编辑 .env，把 DEEPSEEK_API_KEY 换成真的

# 4) 先自检一次（能提前发现九成环境问题）
python tools_dev/check_env.py --call

# 5) 启动
python main.py
```

Windows 上也可以直接双击 `run.bat`。它不写死任何解释器路径，会按下面这个顺序找：
项目 `.venv` → PATH 上的 `python`（会自动跳过微软商店那个占位程序）→ `py` 启动器
→ 注册表里登记的安装路径。一个都没找到就打印安装指引并停住，不会一闪而过。

### 装成全局命令（可选）

想跟 Claude Code 那样，在任意目录敲一个词就启动，跑一次安装脚本即可。

**最简单**：在资源管理器里双击项目根目录下的 `install.bat`。

也可以自己开终端执行，但要注意路径 —— 这条命令是按**当前目录**解析相对路径的，
所以要么先 `cd` 到项目目录，要么直接给完整路径：

```powershell
powershell -ExecutionPolicy Bypass -File "D:\完整路径\CodeReview-Agent\tools_dev\install-command.ps1"
```

它会做三件事：把项目路径记到 `%USERPROFILE%\.xztcode\`、把启动脚本放进同目录的
`bin\`、把该 `bin` 目录加进**用户级** PATH（不需要管理员权限）。之后关掉终端重开一个：

```text
C:\> xztcode
```

> 两个实现细节，踩过坑所以写下来：
>
> - 项目路径常带中文，而 cmd 解码文本文件用的是**当前活动代码页**。所以路径记录
>   以 UTF-8 存一份，启动脚本先 `chcp 65001` 再读 —— 读写两侧编码一致，才不受
>   控制台原本是什么代码页影响。
> - 用户级 PATH 写的是注册表，受限环境（沙箱、只读账户）里会被系统拒绝。这种情况下
>   脚本会打印提示，让你手动把 `%USERPROFILE%\.xztcode\bin` 加进环境变量。

卸载：删掉 `%USERPROFILE%\.xztcode` 整个目录，再从 PATH 里移除那个 `bin` 即可。

## 怎么用

最省事的用法就是照着自己的话说：

```text
xztcode > 帮我审查 examples/bad_code.py，重点说安全隐患
xztcode > 这个文件里的 safe_divide 在哪些地方被调用了？
xztcode > 详细解释第二个问题
```

也有一组内置命令：

| 命令 | 作用 |
|---|---|
| `/review <路径>` | 直接审查某个文件 |
| `/tools` | 看看现在装了哪些工具、各自要什么参数 |
| `/history` | 回看这一轮会话里发生了什么 |
| `/clear` | 清空上下文，重新开始 |
| `/help` | 命令列表 |
| `/exit` 或 `/quit` | 退出 |

启动时可以带几个参数（用 `xztcode` 启动的话，参数直接接在后面，例如 `xztcode --ascii`）：

```powershell
python main.py --model deepseek-v4-pro    # 换更强的模型
python main.py --max-steps 12             # 放宽步数上限
python main.py --ascii                    # 纯 ASCII 输出，CMD 乱码时用
python main.py --show-config              # 打印当前生效的配置后退出
```

不想花额度又想看效果的话，有个离线演示脚本，它把模型回复换成写好的脚本，
但 Agent 循环、工具、渲染全是真的：

```powershell
python tools_dev/offline_demo.py
```

## 一次审查背后发生了什么

拿 `/review examples/bad_code.py` 举例，代码是这么流转的：

1. `main.py` 把命令改写成一句固定的话，交给 Agent。命令本身没有特殊逻辑。
2. Agent 把 System Prompt 和这句话一起发给模型，同时附上四个工具的 Schema。
3. 模型说「我要读文件」，Agent 执行 `read_file`，把结果当作一条 `tool` 消息写回历史。
4. Agent 拿着新历史再问一次。这次模型说「我要跑静态分析」，于是执行 `ast_analyzer`。
5. 再问一次。模型这次不再要工具了，说明它要给出结论 —— 循环到此结束。
6. 结论被 `main.py` 渲染成面板。中间那两句「我先读取这个文件」是过程话术，
   和最终报告分开显示，不会重复。

三个地方值得单独说：

- **终止有两条路。** 模型主动给答案是一条；另一条是步数耗尽，这时不抛异常，
  而是把已经查到的内容交回来，并告诉用户「没收敛，建议缩小范围」。
  用户看到「我查到这儿了」比看到一串 traceback 有用。
- **工具出错不算崩溃。** 路径写错会返回 `{"error": "File not found: ...", "hint": "..."}`，
  这条错误会被当成 Observation 喂回模型，下一轮它往往自己就改用 `list_dir` 去找了。
- **历史里存的是原始消息对象。** 不是拆开重拼的 dict。这样 `reasoning_content`
  这类字段不会丢，以后要开思考模式不用改循环。

## 系统结构

```text
main.py                 命令行交互。只负责读输入、认命令、调渲染层
  │
agent/agent.py          循环编排。唯一同时认识「模型」和「工具」的地方
  ├── agent/memory.py   历史、工具缓存、轨迹、超长裁剪
  ├── agent/prompts.py  System Prompt 与固定话术
  ├── agent/renderer.py 把事件画到终端
  └── agent/errors.py   异常体系与状态码归类
  │
  ├── llm/client.py     只收发消息，不认识「工具」
  └── tools/            只做纯计算，不认识「模型」
        ├── file_tool.py    read_file、list_dir
        ├── ast_tool.py     ast_analyzer（七类检查）
        └── search_tool.py  search_code
```

依赖方向是单向的：`main → agent → {memory, tools, llm}`，而且 `tools` 和 `llm`
互相不认识。这条规矩带来两个好处 —— 工具可以脱开模型单独测（文件工具的 21 个用例、
AST 的 18 个用例就是这么来的），模型客户端也能整体换掉（离线演示脚本干的就是这件事）。

## 四个工具

### read_file

读文件，返回带行号的内容。带行号这点很重要：模型引用行号的准确率会明显提高，
不然它经常给「大概第 8 行」这种没法用的定位。

参数是 `path`，另外可以给 `start_line` / `end_line` 分段读。编码按
utf-8 → gbk → 兜底替换 依次尝试，中文 Windows 上的 GBK 源码也能读。

### list_dir

列目录，顺便标出每个 Python 文件有多少行。路径不清楚时先探一下用。

### ast_analyzer

这是项目的核心。基于标准库 `ast`，一共七类检查：

| 检查 | 内部标识 | 级别 | 怎么判的 |
|---|---|---|---|
| 裸 except | `bare_except` | medium | 异常处理器没有指定类型 |
| 过宽捕获 | `broad_except` | low | 直接捕获 `Exception` 本体 |
| 调试 print | `debug_print` | low | 调用了名为 `print` 的函数 |
| 危险函数 | `dangerous_call` | high | `eval` / `exec` / `compile` / `__import__` |
| 函数过长 | `long_function` | medium | 函数体超过 50 行 |
| 嵌套过深 | `deep_nesting` | medium | 控制流嵌套超过 3 层 |
| 语法错误 | `syntax_error` | high | 解析失败（当作正常结果返回，不抛异常） |

用语法树而不是正则，是为了不误报：字符串里写了 `"print(x)"` 不会被当成调用，
注释里的 `except:` 也不会。代价是每条规则要写一个 visitor。

嵌套深度那条有个细节：只沿着嵌套语句往下走，并且取最深的那条分支。
不这么做的话，四个并列的 `if` 会被算成四层嵌套，那就错了。

### search_code

按文本或正则搜代码，返回 `文件:行号: 内容`。回答「这个函数在哪儿被调的」这类跨文件问题。

## 模型接入方面要注意的

用的是 DeepSeek，接口是 OpenAI 兼容的，所以直接拿官方 `openai` SDK 改 `base_url`。

有三个坑记在这里：

1. 模型名现在是 `deepseek-flash` 和 `deepseek-v4-pro`。网上很多教程里的
   `deepseek-chat`、`deepseek-reasoner` 已经下线了，照抄会直接失败。
2. `thinking` 参数必须放进 `extra_body`，当普通关键字参数传会被 SDK 拒掉。
3. 思考模式一旦开启，**带了 `tools` 的请求必须把历史里每一轮的 `reasoning_content`
   都原样回传**，漏一条服务端就返回 400。所以本项目的历史一律用
   `append(消息对象)` 存原始对象，不拆开重拼 —— 这也让以后开思考模式变成零改造。

本期默认是关掉思考模式的（`CRA_THINKING=disabled`）。想开的话改 `.env` 就行。

## 上下文记忆

三件事：

- **完整历史。** `user` / `assistant` / `tool` 三类消息都在，所以能追问。
- **工具结果缓存。** 同一轮对话里，相同的工具加相同的参数只真正跑一次，
  命中时结果里会标 `cached: true`。
- **超长裁剪。** 超过阈值时保留最近的原文，把最早的整段丢掉，并留一条说明
  告诉模型「这里原有 N 条消息被裁了」。

裁剪有个必须守住的约束：切完之后，尾巴不能以 `tool` 消息开头。
`tool` 消息必须紧跟着它对应的 `assistant(tool_calls)`，中间被切断的话
`tool_call_id` 就悬空了，服务端会直接返回 400。这条有专门的测试盯着。

## 出问题怎么办

| 现象 | 原因 | 怎么办 |
|---|---|---|
| 提示 Key 未配置或仍是占位内容 | `.env` 里还是模板文字 | 把 `DEEPSEEK_API_KEY` 换成真的 |
| 提示 Authorization 失败 | Key 写错、过期，或前后有多余空格 | 重新复制一次 |
| 提示余额不足 | 账户欠费 | 充值后重试 |
| 终端显示乱码 | CMD 默认代码页是 GBK | 加 `--ascii`，或改用 Windows Terminal |
| 一直重试然后失败 | 网络不通或被限流 | 检查网络与代理；限流的话等一会儿 |
| 双击 `run.bat` 窗口一闪而过 | 没找到 Python | 装 Python 3.10+ 并勾选 Add to PATH |
| 回答没收敛 | 任务太大，步数用完了 | 缩小范围，或放宽 `--max-steps` |

## 测试

```powershell
pytest
```

一共 115 个用例，**全都不需要 API Key、不联网**，跑完不到一秒。做法是把模型换成假的 ——
这样不仅省钱、可离线，更重要的是能精确构造那些真实模型很难触发的场景
（工具返回非法 JSON、模型点名不存在的工具、步数耗尽等等）。

| 测试文件 | 用例数 | 主要盯什么 |
|---|---|---|
| `tests/test_ast_tool.py` | 18 | 七类规则各有正反例、行号精确性、语法错误路径、示例文件回归 |
| `tests/test_file_tool.py` | 21 | 不存在 / 空 / 超大 / 二进制 / GBK / 路径越界 / 行范围 |
| `tests/test_agent_loop.py` | 15 | 两条终止条件、链路顺序、错误回灌、缓存、`/clear` |
| `tests/test_memory_and_registry.py` | 30 | 裁剪与配对约束、工具注册与参数校验 |
| `tests/test_cli.py` | 31 | 命令解析与分发、五个内置命令、清空后确实失忆 |

代码规范用 ruff，配置在 `pyproject.toml` 里：

```powershell
ruff check .
ruff format .
```

## 真实模型上的实测

在真实 DeepSeek API 上跑过多次完整审查，结果如下：

| 看什么 | 结果 |
|---|---|
| 工具是否都调用了 | 是。`read_file` 与 `ast_analyzer` 都会调用 |
| 两者的先后 | 观察到的两种顺序都出现过：`[read_file, ast_analyzer]` 和 `[ast_analyzer, read_file]`。系统提示同时要求「先读后评」和「先静态后语义」，模型自行取舍；两种顺序都不影响结论正确性 |
| 有没有多余的工具调用 | 没有 |
| 首轮耗时 | 约 5 秒 |
| 报告长度 | 约 1800 字符，各条问题都带修改建议 |
| 追问时上下文是否延续 | 是，历史里 user 轮次为 2 |

顺带说一个只有真实模型才会暴露的问题：第一版实测时开场白是英文
（`I'll start by reading the file...`），因为 System Prompt 的「语言」那一段
只写了「始终使用中文回答」，模型理解成只约束最终报告。改成「**所有输出**都必须用中文，
**包括调用工具前的一句话说明**」之后才正常。同一轮还发现模型会多做一次
`list_dir` 探查，于是补了一条「不做多余探查」的纪律。

结论是：假模型能保证循环逻辑没错，但测不出措辞层面的行为偏差，两边都得有。
想自己复现的话：

```powershell
python tools_dev/real_check.py --follow-up "第一个问题展开讲讲"
```

## 目录

```text
CodeReview-Agent/
├── main.py                     入口：REPL 与命令解析
├── config.py                   集中配置，所有项都有默认值
├── console_compat.py           终端编码兼容
├── xztcode.bat                 主启动脚本（项目内可直接用，也可装成全局命令）
├── run.bat                     备用启动脚本，行为一致
├── install.bat                 双击即可安装全局命令
│
├── agent/
│   ├── agent.py                Agent 循环
│   ├── memory.py               历史、缓存、裁剪
│   ├── prompts.py              System Prompt
│   ├── renderer.py             终端渲染与品牌界面
│   └── errors.py               异常体系
│
├── tools/
│   ├── base.py                 工具基类与参数校验
│   ├── registry.py             注册与派发
│   ├── file_tool.py            read_file、list_dir
│   ├── ast_tool.py             ast_analyzer
│   └── search_tool.py          search_code
│
├── llm/client.py               模型通信与重试
│
├── examples/
│   ├── bad_code.py             反面样本
│   └── good_code.py            正面样本（用于对比）
│
├── tests/                      115 个用例，无需 Key
├── tools_dev/                  开发辅助脚本
│   ├── check_env.py            环境自检
│   ├── real_check.py           真实 API 端到端验证
│   ├── offline_demo.py         离线演示
│   ├── install_deps.py         离线依赖安装器
│   ├── install-command.ps1     把 xztcode 装成全局命令（被 install.bat 调用）
│   └── make_package.py         打包（会自动排除 .env）
│
├── README.md
├── DESIGN.md                   设计说明，重点写「为什么」
├── requirements.txt
├── pyproject.toml
├── .env.example
└── .gitignore
```

## 已知的不足

- 静态分析只覆盖 Python，因为底层用的是标准库 `ast`。
- 只给建议，不改代码。自动改动的风险不好控制。
- 裁剪策略比较粗，是整段丢弃，没有做摘要。
- 没用上 DeepSeek 的 `strict` 模式。目前非法 JSON 靠回灌机制兜住。
- 报告格式偶尔会超出约定的模板，毕竟 Prompt 是软约束。

后面想做的，按性价比排：`/scan <目录>` 批量审查、接 `strict` 模式、
开思考模式、加一个 `run_tests` 工具让模型解读测试失败原因、报告导出 Markdown。
