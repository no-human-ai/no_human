<div align="center">

<img src="docs/assets/nh-mark.png" alt="" width="140" height="140">

# no_human

**从工单到已评审的 Pull Request。**<br>***免费开源，跑在你自己的机器上。***

[English](README.md) · **简体中文** · [日本語](README.ja.md) · [한국어](README.ko.md) · [Español](README.es.md) · [Français](README.fr.md) · [Deutsch](README.de.md) · [Português (Brasil)](README.pt-BR.md)

[![latest release](https://img.shields.io/github/v/release/no-human-ai/no_human?label=release&color=4C9AFF)](https://github.com/no-human-ai/no_human/releases/latest) [![CI](https://img.shields.io/github/actions/workflow/status/no-human-ai/no_human/ci.yml?branch=main&label=CI)](https://github.com/no-human-ai/no_human/actions/workflows/ci.yml) [![python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue)](https://www.python.org/) [![license MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE) [![downloads](https://img.shields.io/github/downloads/no-human-ai/no_human/total?label=downloads&color=4C9AFF)](https://github.com/no-human-ai/no_human/releases) [![Discord](https://img.shields.io/badge/Discord-join-5865F2?logo=discord&logoColor=white)](https://discord.gg/mSARvj6yW6)

[getnohuman.com](https://getnohuman.com) · [快速上手](docs/quickstart.md) · [文档](docs/README.md) · [看它跑完一个 Sprint](https://getnohuman.com/demo) · [Discord](https://discord.gg/mSARvj6yW6)

[![下载 macOS](https://img.shields.io/badge/%E4%B8%8B%E8%BD%BD-macOS-4C9AFF?style=for-the-badge)](https://github.com/no-human-ai/no_human/releases/latest) [![下载 Windows](https://img.shields.io/badge/%E4%B8%8B%E8%BD%BD-Windows-4C9AFF?style=for-the-badge)](https://getnohuman.com/) [![下载 Linux](https://img.shields.io/badge/%E4%B8%8B%E8%BD%BD-Linux-4C9AFF?style=for-the-badge)](https://getnohuman.com/)

<a href="https://getnohuman.com/"><img src="docs/assets/hero-loop-poster.jpg" alt="no_human 看板：一个任务在“待回答”栏等你回复，四个任务并行进行，一个 Pull Request 等待评审。" width="880"></a>

<sub>▶ <a href="https://getnohuman.com/">看完整流程</a>——工单进，已评审的 Pull Request 出；全程 57 秒。</sub>

</div>

> 本文档译自英文版 README，两者有出入时以[英文版](README.md)为准。文中链接指向的文档目前均为英文。

<ins>**信得过**</ins>的 AI 编程流水线：

- **先有计划，再写代码。** 计划由工单内容加上它在你仓库里的发现生成。
  规划失败时，coder 会被告知它在无计划状态下工作；改动被判定为琐碎时
  会跳过计划，也不告知 coder——这是有意的设计，跳过这件事仍会在该次
  运行的事件流里写明。
- **对抗式评审。** 换一个模型，在一个从未见过 coder 会话记录的会话
  里，指示它去推翻“已完成”这个结论。你拿到的是
  逐条引用文件和行号的通过/不通过检查清单——绝不是模型给自己打的
  数字分数。
- **防篡改守卫。** 删掉的测试、新加的 skip、被改成永远为真的断言——在
  评审门禁运行之前就由程序逐项清点出来；然后要么对照你的
  验收标准给出正当理由，要么这次尝试就此中止。
- **证明修复修掉了那个 bug。** 作为证据提交的测试必须在 merge base 上
  失败，在新代码上通过——复现门禁两边都会跑。默认对 Python 的 bug 修复
  生效；设置 `repro_gate.mode: required` 后对所有任务类型、所有改动都
  生效。
- **你的测试会运行**，在本地跑，也可以接入你的 CI——找不到测试命令的
  PR，正文里就写着 **NOT RUN**。
- **诚实地停下来。** 无法完成时，它会停下并说明原因——如果你的回答能让
  它继续，它给你一个具体的问题；如果只是预算用完了，它留下一份结构化
  记录——绝不会编造一个看似合理的 diff。

## 安装

无论用哪种方式安装，都需要一个 **Claude 凭证**：用 `claude setup-token`
生成的 OAuth token（个人订阅或企业账号均可），所以请先安装 Claude Code
CLI——`npm install -g @anthropic-ai/claude-code`，或
`curl -fsSL https://claude.ai/install.sh | bash`。桌面版每个任务同样会调用
这个 CLI。如果想改为直接向 Anthropic 付费，设置
`llm.auth_mode: "api_key"` 并把你的 `ANTHROPIC_API_KEY` 写进
`~/.no_human/.env`。

### 一行命令（CLI + 看板）

```bash
uv tool install no-human   # 或 pipx install no-human——wheel 里已带看板
nh init && nh doctor       # 配 token、写配置、加第一个仓库；然后确认安装确实可用
```

### 桌面版

[![下载 macOS](https://img.shields.io/badge/%E4%B8%8B%E8%BD%BD-macOS-4C9AFF?style=for-the-badge)](https://github.com/no-human-ai/no_human/releases/latest) [![下载 Windows](https://img.shields.io/badge/%E4%B8%8B%E8%BD%BD-Windows-4C9AFF?style=for-the-badge)](https://getnohuman.com/) [![下载 Linux](https://img.shields.io/badge/%E4%B8%8B%E8%BD%BD-Linux-4C9AFF?style=for-the-badge)](https://getnohuman.com/)

每个发布版本都在制品旁附有 SHA-256。各平台注意事项和首次运行指引见
[docs/quickstart.md](docs/quickstart.md)。

### 从源码安装

```bash
git clone https://github.com/no-human-ai/no_human.git && cd no_human
uv sync                 # 把 `nh` 入口安装进 .venv
(cd web && npm install && npm run build)   # 构建看板（首次安装依赖较慢，可能要几分钟）
uv run nh init          # 配 token、写配置、加第一个仓库（约 2 分钟）
uv run nh doctor        # 正式使用前，先确认安装确实可用
```

想用看板的话，`web` 这一步构建不可省：从源码检出的仓库里没有
`web/dist`，不构建则 `nh start` 只提供 API，不渲染任何界面。需要
Python 3.12+、[uv](https://github.com/astral-sh/uv)、git，以及带 npm 的
Node（用于构建看板）。

## 产品亮点

<table>
  <tr>
    <td width="36%" valign="middle">
      <h3>先有计划，再写代码</h3>
      <p>可逐条核对的验收标准，由工单内容加上它在你仓库里的发现写成。</p>
    </td>
    <td width="64%">
      <img src="docs/assets/readme/highlight-plan.png" alt="任务的计划：我们理解到的三条验收标准、要改的两个文件、实现思路、测试计划、范围之外的事项，以及验证命令。" width="100%" />
    </td>
  </tr>
  <tr>
    <td width="36%" valign="middle">
      <h3>独立的评审者</h3>
      <p>换一个从未见过 coder 会话的模型，指示它去推翻“已完成”。通过或不通过；每一条阻塞性发现都引用文件和行号。</p>
    </td>
    <td width="64%">
      <img src="docs/assets/readme/highlight-verdict.png" alt="评审者的裁定：PASSED，每条验收标准都打了勾并标出满足它的文件和行号，另有一条非阻塞的小建议并附上它所指的 diff。" width="100%" />
    </td>
  </tr>
  <tr>
    <td width="36%" valign="middle">
      <h3>你的测试，写在 PR 正文里</h3>
      <p>在本地跑，或通过你的 CI 跑。找不到测试命令时写明 <b>NOT RUN</b>，绝不留白。</p>
    </td>
    <td width="64%">
      <img src="docs/assets/readme/highlight-tests.png" alt="任务的 Test results 面板：CLEAN，5 个通过、共 5 个，下方是 pytest 输出。" width="100%" />
    </td>
  </tr>
  <tr>
    <td width="36%" valign="middle">
      <h3>防篡改守卫</h3>
      <p>删掉的测试、新加的 skip、被改成永远为真的断言，在评审之前逐项清点。给不出正当理由，这次尝试就此中止。</p>
    </td>
    <td width="64%">
      <img src="docs/assets/readme/highlight-tamper.png" alt="一次被中止的尝试：红色的 TAMPER DETECTED 横幅、评审裁定 FAILED，以及一条阻塞性发现——三个测试被删除，且没有任何验收标准能为此辩护。" width="100%" />
    </td>
  </tr>
  <tr>
    <td width="36%" valign="middle">
      <h3>证明修复修掉了那个 bug</h3>
      <p>作为证据提交的测试必须在旧代码上失败、在新代码上通过。门禁两边都跑，结论写在事件日志里。</p>
    </td>
    <td width="64%">
      <img src="docs/assets/readme/highlight-repro.png" alt="任务的事件日志：测试通过、状态为 reviewing、评审者的篡改检查为 none、复现门禁为 pass（required），随后是 lint、提交和拉取请求打开。" width="100%" />
    </td>
  </tr>
  <tr>
    <td width="36%" valign="middle">
      <h3>诚实地停下来</h3>
      <p>需要你时，它带着一个具体的问题停下来等你，而不是去猜。</p>
    </td>
    <td width="64%">
      <img src="docs/assets/readme/highlight-question.png" alt="看板的 Needs answer 栏：一个任务带着问题“Dedupe by user, or by digest id?”停在那里，下方是 Answer question 按钮；旁边是 Working 和 Review PR 两栏。" width="100%" />
    </td>
  </tr>
  <tr>
    <td width="36%" valign="middle">
      <h3>你的工单，在你的看板上</h3>
      <p>从待办里挑选 Jira 或 Linear 的工单（monday.com 看板则由轮询接入）。每一张在开始前都会和你一起厘清范围。</p>
    </td>
    <td width="64%">
      <img src="docs/assets/readme/highlight-backlog.png" alt="从 Jira 同步过来的 Backlog：四张匹配的工单已勾选，以及 Start 4 tasks 按钮。" width="100%" />
    </td>
  </tr>
</table>

<sub>截图：真实看板，演示用的工作负载。</sub>

## 跑一个任务

不带参数运行 `nh` 进入交互 shell：你的任务泳道、实时事件流，以及一个用
日常语言描述任务的入口。下面的命令照样全部可用。

```bash
nh                                   # 交互 shell
nh start                             # 看板 + worker，监听 127.0.0.1:8420
nh task add https://github.com/org/repo/issues/42 --repo ~/git/repo
nh status                            # needs-you / working / waiting / done
nh review <id>                       # reviewer 的证据清单
nh diff <id>                         # 它想合入的 diff
nh approve <id>                      # 你的批准会以 squash 方式合入 PR(git.approve_identity)
nh reject <id> --reason "..."        # 带上反馈打回去
```

## 集成

把 no_human 指向你正在用的工单系统，它就会把工单拉到你的看板上——过滤
条件写在你的配置里，绝不会出现在任务自己的文本中；网络传输出错时记录
日志并在下个轮询周期重试，而不是让任务池崩溃。

| 工单系统 | 工单如何进来 | 你配置的过滤条件 |
|---|---|---|
| **Jira Cloud** | 通过 REST `search/jql` 轮询（HTTP Basic `email:token`） | `integrations.jira.jql` |
| **Linear** | 通过 GraphQL API 轮询 | `integrations.linear.team_key` + `state_types` + `label` |
| **monday.com** | 通过 GraphQL v2 轮询 | `integrations.monday.board_id` + `status_column` + `todo_labels` |

开启回写（`write_back`，默认关闭）后，工单会随任务流转——按状态类别、
类型或你指定的标签匹配，绝不硬编码流转 ID——并附上 PR 链接；需要人介入
的任务只会被评论，不会被流转。GitHub 和 GitLab 的 issue 可按 URL 导入为
任务，PR/MR 开在你自己的托管平台上；任务需要你时 Slack 和 Teams 会收到
消息；Jenkins 和 CircleCI 可以运行你的测试层，作为循环的门禁。各项配置
见 [docs/adapters.md](docs/adapters.md)。

**完整看一遍 Jira 流程**——工单从 Jira 看板同步进来，确定范围，完成实现，
最终以评审通过的 Pull Request 交付（点击查看包含每一步的完整视频）：

[![Jira 流程演示](https://getnohuman.com/assets/demo-jira.gif)](https://getnohuman.com/assets/demo-jira.mp4)

<p align="center">▶️&nbsp;&nbsp;<strong><a href="https://getnohuman.com/assets/demo-jira.mp4">播放完整演示</a></strong>——1 分 33 秒，从 Jira 看板到评审通过的 PR</p>

## MCP 服务器——在你正在用的智能体里给它派任务

no_human 自带一个 **MCP（Model Context Protocol）服务器**：基于官方
Python MCP SDK 的 stdio 桥接层，让 Claude Code、Cursor 或任何 MCP 客户端
把工作交给你本地的 no_human，并查询进度。

```bash
nh mcp-serve        # MCP 服务器，走 stdio
```

只有两个工具：

| 工具 | 作用 |
|---|---|
| `task_add(title, description, repo_path)` | 提交一个任务。no_human 随后做计划，写改动，跑你的测试，交给第二个模型评审，然后开出 Pull Request。 |
| `task_status(task_id_or_external_id)` | 返回该任务的当前情况——状态、尝试次数，以及 PR 链接（有了之后就带上）。 |

它只和你自己的 no_human（`http://127.0.0.1:8420`）通信，别无其他：不做
鉴权，因为那个地址是 localhost，中间也没有我们的任何服务。在 Claude
Code 里，同一个服务器还以插件形式提供——本仓库自身就是它的插件市场，
执行下面两条命令后，这两个工具就会出现在你的会话里：

```
/plugin marketplace add no-human-ai/no_human
/plugin install no-human@no-human-ai
```

其他 MCP 客户端走常规的 stdio 入口：

```jsonc
// .mcp.json
{ "mcpServers": { "no_human": { "command": "nh", "args": ["mcp-serve"] } } }
```

## 文档

| | |
|---|---|
| [quickstart.md](docs/quickstart.md) | 从零到第一个任务，分平台说明 |
| [configuration.md](docs/configuration.md) | 每一项配置及其默认值 |
| [verification.md](docs/verification.md) | 各道门禁、有界循环、各项限制 |
| [security.md](docs/security.md) | 鉴权边界、“绝不合并”规则、各类守卫 |
| [blockers.md](docs/blockers.md) | 升级上报、唤醒监视器、`nh reply` |
| [adapters.md](docs/adapters.md) | 工单接入、上下文、VCS 与 CI 后端 |
| [eval.md](docs/eval.md) | 黄金标准集、回放评分、影子模式 |
| [CHANGELOG.md](CHANGELOG.md) | 每个版本改了什么 |

## 开发

```bash
uv sync
uv run pytest -q
uv run nh --help
```

欢迎提 issue 和 Pull Request；提交前请先跑 `uv run pytest -q`。

如果 no_human 帮你省下过一轮评审，点个 star 能让更多人找到它：
[![GitHub stars](https://img.shields.io/github/stars/no-human-ai/no_human?style=social)](https://github.com/no-human-ai/no_human/stargazers)

## 社区

提问、报 bug、晒运行结果：加入 [Discord](https://discord.gg/mSARvj6yW6)，
在 [r/no_human](https://www.reddit.com/r/no_human/) 发帖，或提一个
[GitHub issue](https://github.com/no-human-ai/no_human/issues)。

## 许可证

MIT——见 [LICENSE](LICENSE)。许可证覆盖的是代码，不是名字：
[TRADEMARK.md](TRADEMARK.md) 是关于使用 `no_human` 名称和 logo 的政策。
把它打包成二进制，会带来源码树本身没有的义务，列在
[THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md)。
