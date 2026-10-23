# Paper Obsidian Local · 本地论文阅读工作流

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

把论文发现、来源核验、全文解析、中文笔记、原文审校和中英对照 PDF 串成可恢复的本地工作流，最终在 **Obsidian** 中阅读。

适合希望长期维护 AI／机器学习论文库，并保留原文、来源证据、处理记录与个人笔记的研究者和学生。核心数据使用 SQLite、Markdown 和本地文件保存；启用在线功能时，查询或论文文本会发送到配置的来源与 API 服务。

> 这是需要自行配置的研究工具。推荐 Windows + WSL Ubuntu；完整英文双语交付还依赖 Windows 翻译环境。仓库不附带论文、数据库、个人 Vault、密钥、浏览器会话或受限期刊材料。先从离线演示开始。

## 主要功能

| 功能 | 说明 |
| --- | --- |
| 多来源发现 | 接入 OpenAlex、arXiv、OpenReview 和部分官方会议论文集，按主题、日期与分页预算发现候选。 |
| 稳定 ID 去重 | 基于 DOI、arXiv ID、来源 ID 等合并记录，保留来源与日期，不凭标题相似强行合并。 |
| 证据筛选 | 区分候选与正式成果，分别核验会议／期刊资格，材料不足时保留待核验状态。 |
| 全文与中文笔记 | 保存 PDF 原件，建立物理页码与证据编号，生成结构化中文阅读分析。 |
| AI 原文审校 | 将草稿与原文对照，检查方法、数字、比较对象及结论归属；失败保留状态。 |
| 双语 PDF | 英文原页与中文译页交替，保留原文与翻译缓存；中文全文可跳过翻译。 |
| JCR 学科审核 | 支持指标导入、全部学科关系分析和最终原文审校，需自行提供有效官方材料。 |
| Obsidian 阅读 | 生成每日简报、正式论文文件夹与候选笔记，更新机器管理区并保留区外的个人内容。 |
| 中断恢复与费用记录 | 保存阶段缓存、请求回执与用量，未知计费结果保持阻断，避免盲目重发。 |
| 容量管理 | 按稳定 ID 整理候选与正式库，支持预览、固定、可恢复归档和恢复。 |
| 知网辅助 | 导入本地 EndNote 题录、附加合法取得的 PDF，另有需要人工登录的受限浏览器流程。 |

```text
发现候选 → 元数据与去重 → 资格证据 → 取得全文 → 解析与证据编号
         → 中文总结 → AI 原文审校 → 翻译／中文跳译 → 笔记与每日简报
```

下载全文、生成摘要或完成翻译，均不单独等于完整阅读交付。

## 快速开始：先离线体验

### 1. 获取代码

```bash
git clone https://github.com/Yimaker/paper-obsidian-local.git
cd paper-obsidian-local
```

需要 Python 3.11+ 和 Git；Linux／WSL 需有 `venv` 支持。Windows 包装脚本要求已安装名为 `Ubuntu` 的 WSL 发行版。建议放在 WSL 可访问的 Windows 磁盘目录，供后续 Windows 翻译工具共享文件。

### 2. 安装与演示

**Windows PowerShell：**

```powershell
.\install.ps1
.\run.ps1 doctor
.\run.ps1 demo
```

**Linux／WSL Bash：**

```bash
bash scripts/install.sh
.venv/bin/python -m paper_obsidian doctor
.venv/bin/python -m paper_obsidian demo
```

安装需要网络下载依赖。安装后 `doctor` 默认不做网络连通性查询。`demo` 使用标为合成数据的记录，写入 `data/offline-demo` 和 `data/离线测试 Vault`，不调用真实模型或翻译服务。在 Obsidian 中打开演示 Vault 即可查看；内容不是真实论文总结。

先保持 `.env` 中的凭据为空。`doctor` 不显示密钥，但会输出本机路径和主机信息，对外分享前仍需脱敏。

## 配置自己的论文库

安装脚本会在缺失时从 `.env.example` 创建本地 `.env`。真实凭据只填本地文件，不提交 Git。

| 环境变量 | 用途 |
| --- | --- |
| `API_BASE_URL`、`API_KEY`、`MODEL` | 总结与审校服务。实现包含 DeepSeek 特定参数，其他兼容服务需自行验证。 |
| `OPENALEX_API_KEY` | OpenAlex 检索凭据，按来源当前要求配置。 |
| `TRANSLATION_API_BASE_URL`、`TRANSLATION_API_KEY`、`TRANSLATION_MODEL` | 独立翻译服务，不应假设继承总结密钥。 |

建议把 `config.example.toml` 复制为被 Git 忽略的 `config.local.toml`，再修改路径和主题：

```powershell
Copy-Item config.example.toml config.local.toml
.\run.ps1 --config config.local.toml doctor
.\run.ps1 --config config.local.toml status
```

Linux／WSL 用 `cp config.example.toml config.local.toml` 复制；将命令前缀 `.\run.ps1` 替换为 `.venv/bin/python -m paper_obsidian`。

| 配置节 | 需要检查的内容 |
| --- | --- |
| `[paths]` | `vault` 指向自己的 Vault；`data` 保存数据库、原件、缓存和回执。模板 Vault 为 `data/example-vault`。 |
| `[search]` | 关键词、排除词、学科、起止日期、回溯窗口、来源与分页数。示例历史起点不一定适合自己的计划。 |
| `[daily]` | 交付规划、上下文预算、输出长度、有限重试与修订次数。完成篇数不是 API 请求数或金额上限。 |
| `[capacity]` | 模板上限为候选 200、普通正式 100、交叉正式 50，可自行调整。 |
| `[model]`、`[translation]` | 普通处理与翻译选项、组件路径和预算声明；完整 daily 有独立调用链，见下方费用边界。 |
| `[cross]`、`[jcr_auto]` | 可选交叉主题及自动学科审核，需要额外材料和调用预算。 |
| `[cnki_browser]`、`[api_accounting]` | 浏览器、价格证据与费用核算；示例价格有有效期，不能直接当作当前报价。 |

期刊名单为空，不分发绑定本机证据的生产审核状态。会议目录记录也需自行核对适用年份及逐篇发表证据。JCR 不代替 SCIE 身份或中科院分区；缺材料时保留候选是预期行为。

## 常用命令

以下在项目 PowerShell 中运行；用本地配置时，在子命令前加 `--config config.local.toml`。

| 命令 | 行为 |
| --- | --- |
| `.\run.ps1 doctor` | 检查环境、组件、配置和路径写入能力。 |
| `.\run.ps1 status` | 查看论文、阶段、失败与容量状态。 |
| `.\run.ps1 check-links` | 检查 Vault 内部链接。 |
| `.\run.ps1 demo` | 创建隔离的合成数据演示。 |
| `.\run.ps1 run --metadata-only` | 检索并处理元数据，禁止模型与翻译；仍会联网、写入候选。 |
| `.\run.ps1 daily` | 完整日常处理，配置真实服务后可能产生费用。 |
| `.\run.ps1 daily --no-fetch` | 跳过新检索，处理已有记录，仍可能产生模型／翻译费用。 |
| `.\run.ps1 daily --offline-replay` | 禁止网络、模型与翻译；会重放并可能更新机器管理区，不是只读预览。 |
| `.\run.ps1 capacity preview` | 查看容量整理预览，不执行归档。 |
| `.\run.ps1 capacity apply` | 执行容量整理和可恢复归档，先检查 preview。 |
| `.\run.ps1 import-cnki 'data/example.enw'` | 导入本地 EndNote 题录，不调用模型。 |
| `.\run.ps1 attach-fulltext '<paper_id>' 'data/example.pdf' --language zh` | 保存自己有权使用的本地全文。 |
| `.\run.ps1 stop` | 请求在安全边界停止，保留已完成阶段。 |

查看参数：`.\run.ps1 --help`、`.\run.ps1 daily --help`。

### 启用完整总结与双语交付

1. 先运行离线演示和元数据模式，检查主题、Vault 与资格材料。
2. 配置真实总结凭据，核对服务商、模型、上下文与价格，按需设置模型和 daily 参数。
3. 英文 daily 双语链当前调用 `.venv-translation-win/Scripts/python.exe`。在 Windows 安装好 `uv` 后运行 `scripts/install-translation.ps1`，再配置独立翻译凭据。安装可能下载较大的模型／字体资源。
4. 确认实际费用预算后，先用 `.\run.ps1 daily --paper-id '<paper_id>' --max-complete 1` 处理指定记录，再扩大运行范围。该参数限制完成篇数，不能保证只请求一次。

**费用边界：**完整 `daily` 有独立模型和翻译链，不能把 `daily.enabled=false`、`model.enabled=false` 或 `translation.enabled=false` 当作统一的禁止付费开关。不要在填入真实凭据后用 `daily` 做免费诊断。免费体验使用 `demo`；元数据使用 `run --metadata-only`；已有资料离线恢复使用 `daily --offline-replay`。请在服务商侧设置预算／限额，并检查请求回执。

纯 Linux 的核心命令可运行，但完整英文 daily 翻译链尚未作为通用 Linux 方案交付。`scripts/install-translation.sh` 安装的 Linux 组件不等于已接通当前 Windows daily worker。

### 可选：Windows 定时任务

手动运行验证后再安装。任务默认读取根目录 `config.toml`，不会自动使用 `config.local.toml`；请先配置好定时链，并避免提交个人改动。

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\task.ps1 Install -Time '12:01'
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\task.ps1 Status
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\task.ps1 Pause
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\task.ps1 Resume
```

固定任务名为 `PaperObsidianLocal-AI-ML`，不要从多个副本重复安装。当前使用已登录用户与 WSL；关机或注销后的执行不能保证，恢复后按任务设置补跑。关闭 Obsidian 不会停止任务。

## 在哪里阅读与排查

```text
你的 Vault/
├── 00_每日简报/
│   ├── 最新简报.md
│   └── YYYY.MM.DD/每日简报.md
├── 10_论文/
│   └── 年份_会议或期刊_论文标题/
│       ├── 阅读笔记.md
│       └── 中英对照.pdf
└── 90_候选与待核验/
```

中文论文的 PDF 名称／翻译状态会有所不同。每日简报是阅读入口，候选不等于完成推荐。运行数据在 `[paths].data` 下，默认 `data/production/`：

- `papers.sqlite3`：稳定论文记录与处理阶段。
- `daily-runs/`、`last-run.json`：运行报告与费用回执。
- `daily-api/`、`daily-translation/`：模型／翻译缓存与请求记录。
- `scheduler/`：Windows 任务的开始、结束、退出码与日志。

失败后先看保存的阶段和回执，不要删账本绕过未知计费阻断。请求成功而后续发布失败时，应优先复用缓存。

## 测试、文档与已知限制

```bash
.venv/bin/python -m unittest discover -s tests -q
```

测试使用临时数据库、临时目录、模拟 HTTP 和独立配置。初次源码发布复核时 251 项通过；一项候选笔记重放测试存在跨秒时间戳导致的偶发失败。离线测试不能证明真实模型质量、所有供应商兼容性、定时任务收尾或 Obsidian GUI 已通过验证。

- 来源可能限流、出现挑战或延迟收录；分页预算内的检索不保证穷尽文献。
- 扫描件、复杂图表和缺失文本可能需人工补充；AI 自审不能替代用户核读。
- 自动 JCR 需要完整输入及有效官方材料，超限时保留待审，不保证所有长文都能自动审核。
- 知网浏览器依赖 Windows Node／Edge 与人工认证，含特定机构适配，不是通用批量下载器。副本不含生效价格原件或登录状态，不能直接复用历史会话。
- 部分专题文档保留历史实现和验收背景，具体命令以当前 CLI 和本 README 为准；历史成功不代表新安装已验证。

专题说明：[费用核算](docs/API_ACCOUNTING.md) · [容量管理](docs/CAPACITY_MANAGEMENT.md) · [期刊信息](docs/JOURNAL_METADATA.md) · [材料清单](docs/JOURNAL_MATERIALS_CHECKLIST.md) · [知网题录](docs/CNKI_IMPORT.md) · [浏览器流程](docs/CNKI_BROWSER.md) · [来源说明](docs/SOURCES.md)。

## 安全、贡献与许可证

请阅读 [SECURITY.md](SECURITY.md)。不要将密钥、Cookie、原始响应、个人数据库或未授权全文附在 Issue／PR 中。`config.toml` 已被跟踪，填写个人信息后需要检查提交差异；`.gitignore` 不能保护已跟踪文件。启用 AI 时全文内容可能发给外部服务，应确认自己有权这样使用。

欢迎在 Issue 中提供可复现问题，或提交带必要测试与使用说明的 PR。新增来源、模型步骤或资格规则时，应保留稳定 ID、来源证据、缓存恢复及未知费用处理。依赖和第三方工具遵守各自许可证。

本项目源码采用 [MIT License](LICENSE)。许可证不授予论文、数据库、期刊材料、第三方服务或依赖项目的使用权。
