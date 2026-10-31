# custos

面向 AI 编程 Agent 的上下文窗口监控与会话轮换工具。

[English](README.md) · **简体中文**

`custos` 盯着 Agent 的上下文窗口快满了没有；在窗口开始劣化之前写下交接快照
（handover），必要时起一个新会话，让长任务不会撞上那种「有损的自动压缩」。内置
Claude Code、Codex、OpenCode、Pi 四个适配器，另有一个配置式适配器，只要你的工具
能用 JSON 报出自己的 token 用量就能接进来。名字取自拉丁语 custos，意为「看守者」——它替你看守着会话的上下文。

> **状态：0.1.0，未发布。** 这里写的功能都已实现，并在本机真实会话上跑过：读数引擎、
> 阈值、交接格式、监控循环、轮换、门禁（gate）执行器、安装脚本、题库。测试在
> Python 3.9 与 3.13 下均通过。尚未发布，见 `CHANGELOG.md`。

**除非你让它跑，它不会启动任何 Agent。** 读数全部来自各工具本来就会写的文件，且只读
打开；唯一会启动进程的是门禁，跑的是你自己配置的那条一次性提问命令。

## 要解决的问题

长会话不是突然崩掉的，是一点点糊掉的：Agent 开始忘记刚刚做过什么、重复读已经读过的
文件、跟一小时前定下的决定自相矛盾。等你察觉时，可靠的那部分上下文已经没了 —— 而
最终触发的那次自动压缩，恰恰是把它们丢掉的那件事。

实践中缺两样东西：

1. **一个数字**：这个会话的窗口现在用了多少？不用麻烦 Agent 自省。
2. **一份交接**：窗口必须换掉时，新会话读什么才能接着干活？

`custos` 把这两样补上，其余时候尽量不出声：没越过线就不打印任何东西，自己没有常驻
进程，不引入你本来没有的依赖。

## 功能

- **按会话读数，不需要 Agent 自省**：每个会话一行 —— tokens、窗口、百分比，以及这个
  数字从哪来，全部读自 Agent 本来就会写的文件。
- **两条阈值线**：warn 线与 rotate 线，各自可以按绝对 token 数设，也可以按模型窗口
  占比设。
- **轮换前先写交接**：一份 markdown 快照 —— 干到哪、做过哪些决定、还有什么没定、
  最近的消息摘录、关键路径；先写文件再发消息，所以消息里点名的文件必然存在。
- **带联锁的轮换**：空闲检查、幂等台账、单实例文件锁，保证不会去动一个正忙或已经
  处理过的会话。
- **给新会话的门禁**：一条配置好的一次性提问，用 `exact`、`last_number`、`regex`、
  `script` 四种判定器之一打分；失败次数跨调度轮次累计，新会话持续答错就报「已降级」。
- **推入模式**：`custos report` 接收任何「自己知道 token 数」的工具推来的读数。
- **默认安静**：没越线不输出、没有常驻进程、无运行时依赖；`--json` 可放在命令行任意
  位置，所有会改磁盘的子命令都支持 `--dry-run`。

## 环境要求

- Python 3.9 及以上（3.9 是实测下限）。
- macOS 或 Linux。工具用到 `fcntl` 锁和进程组；Windows 的立场见 `docs/faq.md`。
- 无运行时依赖。PyYAML 是可选的：只有你想用 `config.yaml` 时才需要，JSON 配置到处都能用。

## 安装

```sh
git clone https://github.com/Cocos666a/custos.git && cd custos
bash install.sh                 # 默认装到 ~/.local
```

| 参数 | 作用 |
| --- | --- |
| `--prefix DIR` | 装到 DIR，而不是 `~/.local` |
| `--yes` | 不交互确认（没有终端时必须加） |
| `--dry-run` | 只打印将要做什么，不落盘 |
| `--no-daemon` | 不装调度器模板 |
| `--with-skills` | 若 `~/.claude/skills/` 存在，一并拷入 `skills/` |

安装脚本把程序写到 `PREFIX/share/custos`，把 `custos` 包装脚本写进
`PREFIX/bin`，并打印对应平台的调度片段。`bash uninstall.sh` 可完整卸载。

它按「装给哪个解释器用」来播种配置：装到的解释器有 PyYAML，就写 `config.yaml` +
`questions.yaml`；没有（系统自带 `python3` 通常没有），就写同内容的 `config.json` +
`questions.json` —— 因为工具遇到自己读不了的 YAML 配置会拒绝启动。带注释的原始文件
始终是 `config/config.example.yaml`。

也可以不装，直接在源码目录里跑：

```sh
python3 -m custos --help
```

## 快速开始

```sh
# 1. 配置目录（$CUSTOS_CONFIG_DIR，否则 ~/.config/custos）
mkdir -p ~/.config/custos
cp config/config.example.yaml ~/.config/custos/config.yaml

# 2. 现在工具看到了什么？
python3 -m custos doctor
python3 -m custos scan

# 3. 手动跑一轮监控，然后交给调度器
python3 -m custos monitor --once
```

`scan` 每个会话打印一行：工具、会话、tokens、窗口、百分比、这个数字的来源。
`monitor --once` 是给调度器用的入口：只有越过阈值时才打印消息 —— 这正是 cron 保持
安静的原因。

先在合成会话上看完整条流水线（不需要装任何 Agent，也不会碰临时目录之外的任何东西）：

```sh
python3 -m custos demo
```

```
reading        demo demo-session-0001 at 184000 of 200000 tokens (92.0%), source=external
threshold      baseline 180000 (90% of window 200000); warn 153000; rotate 162000 -> rotate
interlock      passed (idle, quiet window 0m, no earlier rotation)
handover       .../handovers/handover_demo_demo-ses_20261016-215433.md
rotate         mode=assisted rotated=False

[custos] rotation required
  old session: demo demo-session, 184000 tokens, 92.0% of window, read 60m ago
  trigger: tokens 184000 reached the rotate line 162000
  mode: assisted
  handover: .../handovers/handover_demo_demo-ses_20261016-215433.md
  next: start a new session and read the handover file above
  gate: passed on attempt 2
selection #1   mode=random seed=20261016 -> asked consecutive-sum-87
gate #1        passed on attempt 2 (no streak)
  attempt 1    fail extracted=37 expected 30, last number was 37
  attempt 2    pass extracted=30
rotate #2      handover written; gate left pending for later runs
selection #2.1 mode=sequence cursor=1 -> asked consecutive-sum-87
gate #2.1      failed after 2 attempt(s) (2 fail in a row)
selection #2.2 mode=sequence cursor=2 -> asked lighter-ball-weighings
gate #2.2      failed after 2 attempt(s) (4 fail in a row)

[custos] gate alert: the new session failed the check 4 times in a row
  session: demo demo-session, 184000 tokens, 92.0% of window, read 60m ago
  attempts: 4 in a row, 4 in total for this rotation
  last reply: answer: 10
  next: it is retried on every scheduled run until it recovers; a recovery is
        reported on its own, so no action is needed.
alert          degraded alert emitted after 3 consecutive wrong answers
pending        event rot-...-0001-b stays pending with 4 recorded attempt(s) so
               the next scheduled run continues the streak
files          2 handover(s) in .../handovers; state at .../state.json;
               events at .../events.jsonl

demo finished: reading -> threshold -> interlock -> handover -> rotation ->
gate (random, fail then pass) -> carried-over gate (sequence, alert)
```

演示脚本会在「本该失败的步骤没有失败」时以非零码退出，所以它既是导览也是冒烟测试。

## 命令

| 命令 | 作用 |
| --- | --- |
| `scan` | 每个会话一行：tokens、窗口、百分比、数字来源 |
| `monitor` | 调度器入口：读数 → 评估 → 写交接 → 轮换 |
| `rotate` | 轮换指定会话；`--dry-run` 只打印计划 |
| `gate` | 手动或定时跑一次智能检查 |
| `questions` | 查看与校验题库（`list`、`validate`） |
| `report` | 由「自己知道 token 数的工具」推入一条读数 |
| `demo` | 文档化的端到端场景，跑在合成会话上 |
| `doctor` | 报告配置了什么、发现了什么、缺什么 |

`--json` 与 `--quiet` 放在子命令前后都行。带 `--json` 时 stdout 就是**一份** JSON
文档，别无他物，调用方不用猜哪几行是消息。`--dry-run` 不改变磁盘上任何东西。

## 支持的 Agent

| Agent | 读数来源 | 验证等级 | 轮换 | 门禁 |
| --- | --- | --- | --- | --- |
| Claude Code | `~/.claude/projects/**/*.jsonl` 的 message usage | **已实测** | assisted 或 full-auto | `claude -p` |
| Codex | `~/.codex/sessions/**` rollout 里的 token 计数 | **已实测** | assisted 或 full-auto | `codex exec` |
| OpenCode | `~/.local/share/opencode/opencode.db`（只读） | **已实测** | assisted | `opencode run` |
| Pi | `~/.pi/agent/sessions` | **文档核验** | assisted | `pi --print` |
| 通用适配器 | 你配置的文件或命令 | **实验性** | 你配置的命令，否则 assisted | 你配置的命令 |
| *(任意工具)* | 用 `custos report` 推入 | **实验性** | assisted | 不支持 |

**已实测** = 该工具的占用语义用本机真实数据核对过，核对用的数字记在适配器源码里。
**文档核验** = 路径与参数来自工具官方文档，本机无安装可验证 usage 字段名，因此适配器
是「搜索字段」而不是「假定字段」，找不到就明确报错，绝不瞎编数字。**实验性** =
正确性取决于你写的配置。

适配器会优先采用工具自身记录里的窗口值，只有在工具没写时才退回配置里的 `window` ——
一个会话中途可能换模型，配置里那个数字会悄悄变成错的。

## Claude Code 与 Codex 的 full-auto 轮换

两个适配器都能自己拉起接班会话，默认关闭。未经要求的自动拉起比交给你一份文件去读
更糟，所以这个开关得你亲自打开。

```yaml
adapters:
  claude_code:
    rotate_mode: full_auto
    # rotate_timeout: 900                    # 拉起会话的超时秒数
    # spawn_args: ["--permission-mode", "acceptEdits"]
    # continuation_prompt_file: /path/to/prompt.md
    # rotate_cwd: /path/to/project
  codex:
    rotate_mode: full_auto
    # spawn_args: ["--sandbox", "workspace-write"]
```

| 键 | 默认值 | 作用 |
| --- | --- | --- |
| `rotate_mode` | `assisted` | 设为 `full_auto` 后由适配器自己拉起新会话 |
| `rotate_timeout` | `900` | 拉起会话允许的秒数 |
| `spawn_args` | 每个适配器不同 | 追加的 CLI 参数；显式配置会**替换**默认值 |
| `continuation_prompt_file` | 内置模板 | 模板里可用 `{handover}`、`{session}` 占位符 |
| `rotate_cwd` | 取自旧会话 | 新会话的工作目录 |

full-auto 下适配器只跑一轮无交互的调用：Claude Code 是
`claude -p <prompt> --output-format json --permission-mode acceptEdits`，Codex 是
`codex exec --skip-git-repo-check --sandbox workspace-write <prompt>`。提示词要求新会话
先读交接文件，并在回复首行写 `CONTINUATION RECAP:`——这一行就是「它确实读了交接」的证据。
这样配置过的适配器，在 `custos status` 和 `custos scan --json` 里 `full_auto` 为 `true`。

**安全提醒。** `spawn_args` 决定拉起的会话可以不问就做什么：Claude Code 默认
`acceptEdits`，Codex 默认 `workspace-write`，都比完全跳过权限窄。改成
`bypassPermissions` 或更宽的沙箱模式，等于让无人值守的 agent 随意改你的项目。
新会话能拿到的上下文只有交接文件，所以 `rotate_timeout` 别低于 900 秒——超时的那种
情况是交接已经写好、会话一个都没起成。

### `custos goal`：让一个长任务跨多个会话跑完

`custos goal` 用来推进一个装不进单个上下文窗口的任务。每一轮先续跑当前会话，读数到了
rotate 线就轮换到新会话接着跑。

```bash
custos goal --tool claude_code --task-file ./TASK.md --cwd ./myproject --max-sessions 5
```

它维护三个东西：

* `.custos-goal-state.json`（或 `--state-file`）：会话、轮换记录、解析出的 rotate 线和
  最终结果，每次状态变化后原子写入。
* `custos-goal-progress.md`：首次运行时用任务内容创建，每轮的提示词都要求 agent 往里
  追加一行带日期的进展。
* 每次轮换照常写一份交接快照。

每个事件输出一行（`[goal] <ts> round=<k> event=<名称> session=<id> tokens=<n> rotate_line=<m> detail=<...>`），
加 `--json` 则每行一个 JSON 对象。完整说明见 `docs/full-auto-goal.md`。

## 工作原理

```
scan ─┬─ 适配器 ──> 读数 {tool, session, tokens, window, pct, source, ts}
      │
      └─ 阈值 ────> warn 线 / rotate 线（绝对值，或窗口百分比）
                      │
monitor ── 取锁 ──────┤
      │               ├─ 联锁 ────> 会话空闲吗？这个会话是不是已经处理过？
      │               ├─ 交接 ────> markdown 快照 + 事件记录
      │               ├─ 轮换 ────> assisted（提示你）或 full-auto
      │               └─ 门禁 ────> 一次性提问，并给回答打分
      └─ 状态 ────────> 原子写：state.json、events.jsonl、台账
```

每次轮换记录成一个带稳定 id 的**事件**。门禁结论跨多轮挂在这个事件上 —— 这就是
`alert_after_failures` 能统计「跨若干个五分钟周期」的连续失败、而不是每个 tick 归零的
原因。

完整说明见 `docs/architecture.md`。

## 配置

只有一个文件：`$CUSTOS_CONFIG_DIR`（默认 `~/.config/custos`）下的
`config.yaml`（或 `config.json`）。所有键都可省略，完整说明在
`config/config.example.yaml`。最该动的几个：

| 键 | 默认值 | 什么时候改它 |
| --- | --- | --- |
| `thresholds.baseline_tokens` | `350000` | 你的工具在哪个点数压缩 |
| `thresholds.baseline_percent` | `null` | 同上，用窗口占比表示；设了就优先 |
| `thresholds.warn_factor` | `0.90` | 多早开始提示 |
| `thresholds.rotate_factor` | `0.93` | 多早值得轮换 |
| `monitor.interval_minutes` | `5` | 调度频率；多余的调用自动空转 |
| `monitor.activity_quiet_minutes` | `10` | 比这更近被用过的会话先不动 |
| `rotation.mode` | `assisted` | 只有适配器能起会话时才用 `full-auto` |
| `rotation.idempotency_hours` | `6` | 已处理过的会话在这段时间内不重复处理 |
| `gate.mode` | `random` | `random`、`sequence` 或 `all` |
| `gate.alert_after_failures` | `5` | 连续答错几次算「已降级」 |

两条线都是基线的系数，所以移动基线会同时移动它们。

## 交接（handover）

交接是 `handovers/` 里的一个 markdown 文件，字段固定：活儿干到哪了、做过哪些决定、
还有什么没定、最近几条消息摘录、关键文件路径。`rotation.mode: assisted` 会写好文件并
提示你开新会话；文件先写、消息后发，所以消息里点名的那个文件必然存在。

## 题库

门禁的用途是抓「新会话比旧会话更差」这件事：模型错了、上下文坏了、Agent 自信地答错。
它从题库里抽题，用四种判定器之一给回答打分（`exact`、`last_number`、`regex`、`script`）。

仓库自带题库是**起点而不是成品**：它演示格式，并给你一个可以替换的东西。把
`gate.questions` 指向你自己的题库，用 `custos questions validate` 校验。详见
`docs/questions.md`。

## 从其它工具推入读数

如果你的工具本来就知道自己的 token 数，不用解析：

```sh
custos report --tool my-agent --session "$SESSION_ID" --tokens 128000 --window 200000
```

推入的读数优先级高于任何从 transcript 解析出来的值，同一会话取最新。挂在会话启动脚本
里，就等于给没有适配器的工具也接上了。

## 安全与隐私

完整说明见 `docs/security.md`。简述：

- **不读工具没写过的东西**，且全部只读打开；数据库用 `mode=ro`。
- **不启动任何东西**，除了门禁那条一次性提问：清洗过的环境、超时、独立进程组，不继承
  任何密钥（见 `docs/security.md`）。
- **唯一的网络请求**是可选的通知 webhook：默认关闭、`--dry-run` 时跳过、失败只记日志
  且忽略 —— 一个坏掉的 endpoint 绝不能弄坏一次轮换。
- **`DISABLE`**：在配置目录里放这个文件就能全停。`monitor` 静默退出，`rotate` 除非是
  空跑否则拒绝执行。这是「这个会话绝不能碰」时最快的刹车。
- **同时只跑一个实例**：文件锁让第二个 monitor 直接跳过本轮，而不是跟第一个抢着把同一
  个会话轮换两次。
- **`--dry-run` 不写任何东西**，包括门禁的取题游标和告警节流。

## 限制与已知问题

- **0.1.0，未发布。** 接口在 1.0 前可能变化，以 `CHANGELOG.md` 为准。
- **只支持 macOS 与 Linux。** 单实例锁依赖 `fcntl`，子进程超时依赖 `os.killpg`；
  Windows 不支持，WSL 可用。见 `docs/faq.md`。
- **所有内置适配器都只做 assisted 轮换。** `custos` 写好交接、提示你开新会话，不会
  往正在跑的 TUI 里打字；`full-auto` 需要适配器自己有能力起会话。
- **验证等级不同。** Claude Code、Codex、OpenCode 用本机真实数据核对过，Pi 没有；
  通用适配器与推入模式的正确性取决于你写的配置。
- **题库只是起点。** 它演示格式；请换成针对你自己工作的问题再信任门禁。
- **门禁会消耗模型调用。** 每次尝试都是通过工具的一次性命令发一条提问，按默认配置
  每次轮换最多 5 次。不想跑就设 `gate.enabled: false`。

## 目录结构

```
custos/                  包本体
  cli.py                 参数解析、输出模式、各子命令
  core/                  usage、thresholds、handover、rotation、gate、state
  adapters/              claude_code、codex、opencode、pi、generic、demo
  demo.py                `custos demo` 背后的场景脚本
config/                  示例配置与题库，YAML 与 JSON 各一份
docs/                    architecture、adapters、full-auto goal、questions、security、faq
skills/context-rotation/ 可选的 Agent 技能，描述这套工作流
tests/                   pytest 测试与 fixture
tools/                   sanitize_check.py、acceptance_check.sh
install.sh               安装脚本，含播种配置
uninstall.sh             反安装
pyproject.toml           打包元数据与 pytest 配置
```

测试的组织方式与包一致：每个子系统一个模块（`test_rotation.py`、`test_gate.py`
……），`test_cli.py` 驱动真实的命令行，`test_sanitize.py` 会种下违禁字符串来证明
扫描器抓得到。

## 贡献

欢迎提 issue 和 PR。动手前先跑：

```sh
python3 -m pytest -q                     # 测试套件
python3 tools/sanitize_check.py .        # 必须打印 0 hit(s)
bash tools/acceptance_check.sh           # 完整验收，含 3.9 与 3.13
```

- 保持扫描器干净。它找的是本机专属路径、用户名、内部服务名、端口与凭据形态；
  「扫描干净」是评审的一部分，不是走过场。
- 新适配器照 `docs/adapters.md` 来：记录占用数字是怎么核对出来的，字段要「搜索」而
  不是「假定」，找不到就明确报错，不许瞎编一个读数。
- 不引入新的运行时依赖 —— 只用标准库，PyYAML 保持可选。
- 测试断言的是行为（命令做了什么），不是源码长什么样。
- 安全问题按 `docs/security.md` 的流程反馈；任何情况下都不要把真实会话数据、凭据或
  本机路径贴进 issue。

## 开发

```sh
python3 -m pytest -q                   # 458 个用例
python3 -m custos demo                 # 端到端冒烟测试，不需要装 Agent
python3 tools/sanitize_check.py .      # 仓库内无本机专属字符串
```

测试在 Python 3.9 与 3.13 下均通过，除 pytest 外无依赖；在缺 PyYAML 的解释器上，4 个
YAML 用例是跳过而不是失败，由 CI 的 extras 任务负责跑它们（`tools/acceptance_check.sh`
会把两个解释器都跑一遍）。CI 还会字节编译所有模块，并检查每个子命令能响应 `--help`。

## 文档

| 文件 | 内容 |
| --- | --- |
| `docs/architecture.md` | 流水线、数据模型、模块地图 |
| `docs/adapters.md` | 支持矩阵、占用语义、推入模式、如何写适配器 |
| `docs/full-auto-goal.md` | Claude Code 与 Codex 的 full-auto 轮换，以及 `custos goal` 驱动 |
| `docs/questions.md` | 题库格式、四种判定器、如何换题库 |
| `docs/security.md` | 读了什么、写了什么、子进程隔离、紧急开关 |
| `docs/faq.md` | assisted 与 full-auto、为什么没消息、多机、Windows |
| `docs/publish-checklist.md` | 仓库公开前要做的事 |

## 许可

MIT，见 `LICENSE`。
