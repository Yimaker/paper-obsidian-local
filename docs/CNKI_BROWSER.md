# DeepSeek + 本地浏览器 MCP 知网流程

2026-10-23。复用现有系统；不是知网官方 API。

## 当前入口与运行架构

生产项目 `<PROJECT_ROOT>`；Vault 为 `<VAULT_ROOT>`，数据库仍为 `data/production/papers.sqlite3`。

`run.ps1 → WSL Ubuntu .venv Python → Windows Node → 官方 @playwright/mcp 0.0.83 → Windows Edge`。
Python 调用既有 DeepSeek 配置，让模型基于有限页面文字和控件编号决定下一步；Windows 执行器使用 MCP 执行固定的网页操作。
文件在同一项目磁盘交接，Windows 绝对路径由既有路径函数转为 `/mnt/c/...`。不依赖 Codex 会话或其浏览器工具。

官方 MCP 通过 SDK 的内存传输连接运行，未开放网络调试端口。其 `browser_run_code_unsafe` 仅接收执行器按固定模板生成的点击、填检索词和选 EndNote 操作；DeepSeek 没有代码、选择器、文件、网络请求或其他 MCP 工具权限。此处使用的工具名称来自已安装版本实际 `tools/list`，不是猜测旧工具名称。

安装依赖：在项目的 `browser` 目录运行 `npm ci --ignore-scripts`。使用已安装的 Windows Node 和 Edge，不安装第三方知网爬虫。`package-lock.json` 锁定依赖。

## 手动使用

在项目 PowerShell 执行：

```powershell
# 不调用模型的页面预检
.\run.ps1 cnki-browser inspect --query '机器学习' --url 'https://www.cnki.net/'
# 手动触发，一次最多一条题录；预算和价格未通过时停止
.\run.ps1 cnki-browser run --query '机器学习'
# 已有稳定 ID 的论文可直接使用原来源链接，并允许尝试单篇普通下载
.\run.ps1 cnki-browser run --query '论文完整题名' --url 'https://link.cnki.net/doi/真实DOI' --paper-id '<paper_id>' --download-one
.\run.ps1 cnki-browser status
.\run.ps1 cnki-browser resume --session '<会话ID>'
```

只检索不允许下载时省略 `--download-one`。已有题录、归档、固定、容量及资格规则仍由原模块处理；已导入题录可以复用，不为了下载重复增加候选。已归档论文不能通过下载入口恢复。`run/resume` 暂停或失败返回退出码 2；完整浏览器阶段返回 0，**仍不等于完整阅读交付**。`inspect/open` 是有意预检或人工操作，不要求完成全文。

关闭与重新允许手动运行：

```powershell
.\run.ps1 cnki-browser disable
.\run.ps1 cnki-browser enable
```

`disable` 写入 `data/production/cnki-browser/DISABLED`；执行器在安全边界停止。也可把 `config.toml` 的 `[cnki_browser].enabled` 改为 false。它们不禁用原每日任务，不影响本地题录扫描或其他来源。

## 暂停与继续

本次已完成单篇 PDF 下载的真实会话：`8ce6270205244bbc`。目标原文：

<https://link.cnki.net/doi/10.16157/j.issn.0258-7998.268230>

本次由用户完成深圳理工大学认证；论文页未显示机构名称，已如实保留“未确认”，单篇普通 PDF 响应和下载验证已成功。此前普通浏览器的机构访问状态不继承。学校官网：

<https://lib.suat-sz.edu.cn/info/1012/1091.htm>

```powershell
.\run.ps1 cnki-browser open --session 8ce6270205244bbc --school
```

`--school` 使用学校资源页明确公布的 CARSI 链接，避免 Windows → WSL 传递包含 `&` 的网址时出现参数转义问题。若自动跳转失败，窗口保持打开，可手动访问 `https://fsso.cnki.net/`，选择深圳理工大学。也可在专用 Edge 中打开上述学校资源页，点击“carsi登录”，自行完成认证；扫码或验证码也由本人操作。不要在聊天提供密码、Cookie 或令牌。完成后关闭该专用 Edge，再执行 `resume`。手动登录窗口不持有主论文系统运行锁，其他本地工作和原每日任务可继续。

如果本人已完成学校认证，但论文页没有显示机构名，可对已有的单篇下载会话使用 `resume --session '<id>' --after-school-login`。它只记录本人完成认证的报告，允许普通 PDF 按钮向网站检查该篇权限；不是已获下载权限的断言，也不会更改机构核验字段。文件无效、要求登录／付费或验证码时仍暂停。只有精确题名、完整 PDF 等检查均通过才记成功。已有同哈希有效全文会直接复用，不再次启动浏览器或收费。

Edge 原生配置保存在 `data/production/cnki-browser/edge-profile`，只交给浏览器使用，执行器不读取其中的凭据文件，不导出 Cookie，不复制给 HTTP 脚本。登录不能证明程序批量下载权限；本版始终最多一次单篇全文操作。

遇付费、无权限、验证码、错误学校、越界域名、身份不明、题名不符、步骤上限或费用未知，保存会话后暂停。不会购买、自动切换模型、绕过认证或调用逆向接口。CAJ 保存原件，标记待转换／待补 PDF；通过 `attach-fulltext` 提供本地合法 PDF，保留两份原件，再继续核对。不同 PDF 版本不会覆盖已有全文阶段。

## 原件、回执与交付

浏览器会话和下载原件：`data/production/cnki-browser/sessions/<id>/`。下载清单 `downloads/manifest.json`；控制台采集、会话录像及模型任意代码均关闭。浏览器退出前后均能通过清单恢复已落盘文件，不用重新下载。

题录仍进入既有 `imports/cnki/originals/<SHA256>.bin` 和 `receipts/<SHA256>.json`，保存原始编码、缺失字段、逐条错误、来源、时间和哈希。原待导入目录仍为 `imports/cnki/pending`。超过一条的自动导出文件保留，但本流程不自动批量导入；需检查后由既有导入入口处理。网页导出的引用字符串不会冒充完整元数据。

全文通过既有 `attach_fulltext/parse_original`，附加 EOF、加密及结构修复检查；原件不改后缀、不覆盖、不上传在线转换站。检查通过仍不是语义完整性证明，扫描件、图表和缺失正文仍可能需要人工处理。

新候选或本次新取得的全文记录 `cnki:processing:<paper_id>` 预算保留状态，默认不启动总结、审校或翻译。本次 1 元只授权浏览器导航决策，不能扩展成阅读预算。后续必须在获得单独阅读预算后，由维护者为指定稳定 ID 解除该状态并使用既有 `daily --paper-id ...`；本版没有给用户提供绕过预算的一键放行开关。

通过资格门槛及单独预算后，既有日常处理复用分页解析、摘要生成、原文审校、翻译缓存和正式发布：英文保留原 PDF、生成中英对照 PDF 与中文笔记；中文跳过翻译但仍做总结和原文审核；知网全文语言未知时在调用总结前暂停。候选、摘要、下载及译文未完成均不计完整交付。

资格仍为原 SCIE／中科院分区／CCF 规则；不把“核心”、CSCD、CSSCI 等作为替代资格。候选共用 200、正式共用 100，没有另建交叉分支或配额。

## 预算及每日任务

本次唯一账本：`cnki-browser/campaign-20261023-one-yuan/ledger.json`。总上限 1 CNY，最多 12 次调用、每次输出 384 tokens、序列化输入上界 24000 bytes、最多 16 步、每个运行片段 240 秒、单篇题录／单篇全文。网页操作和模型等待也有单步超时。

模型固定为实际配置的 `deepseek-flash`。官方 CNY 价格证据在 `.firecrawl/deepseek-pricing-cny.md`，配置保存其哈希和有效区间，仅适用于 2026-10-23。峰值单价用于发送前的保守预留；本次实际调用在工作日高峰时段，按返回的缓存命中、未命中及输出用量计费。日期过期、证据文件缺失、模型不匹配或未知用量均停止。价格到期需要重新核实，不能靠修改日期继续付费；本次账本不能删除、改名或按会话重置来追加额度。

请求、响应、决策缓存、用量和费用均持久化；发送前写 pending 回执，未确认请求永久阻止该验收账本新增付费请求，不能次日自动重试。已有 API 余额查询能力复用，余额差仅作旁证；余额精度、延迟或账户并发调用可造成与 token 费用不一致。

原 `PaperObsidianLocal-AI-ML` 每天北京时间 12:01（2026-10-23 按用户要求调整原任务时间，未新增任务）。现有 daily 继续其他来源、待导入扫描和累计交付，并读取已保存的浏览器状态；**不调用浏览器，也没有长期付费浏览器预算**。`daily_enabled=false` 声明此限制；不能仅改为 true 就取得每日额度或程序批量下载授权。未来长期频率、数量、价格及权限需另行确定。

## 权限证据

- [深圳理工大学知网资源页](https://lib.suat-sz.edu.cn/info/1012/1091.htm)：提供官网、CARSI、VPN；版权公告禁止软件工具批量下载及短时间连续下载多篇／整期全文。
- 2026-10-23 用户提供的认证失败页面确认其实际跳转到 `sso.suat-sz.edu.cn`；仅将这个学校 SSO 主机补入允许列表，没有扩大到其他高校或全网。Windows 浏览器沙箱已启用并检查，专用进程没有 `--no-sandbox`。
- [官方 Playwright MCP](https://github.com/microsoft/playwright-mcp)：支持本地 MCP；其 allowed origins 明确不覆盖所有重定向，因此执行器另外对所有请求、导航和弹窗设置 HTTPS 域名边界，并关闭 service worker 和 WebSocket。
- [DeepSeek 模型与价格](https://api-docs.deepseek.com/zh-cn/quick_start/pricing)：本次模型及 CNY 单价依据。

没有取得本项目适用的知网官方 API 文档、凭据和程序权限，官方 API 未接通。也未取得明确允许自动批量获取全文的知网／学校许可；专项程序许可状态记录为未知。学校页面所列正常访问入口不等于此类许可。技术可执行、网页访问、元数据导出和全文下载是不同状态。

本次验证与已知缺陷见 [2026-10-23 验收](CNKI_BROWSER_ACCEPTANCE_2026-10-23.md)。
