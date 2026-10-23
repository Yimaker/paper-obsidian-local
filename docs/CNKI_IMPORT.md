# 知网来源：使用与限制

2026-10-23 更新：题录仍使用本文入口；新增正常网页上的 DeepSeek＋Playwright MCP 手动检索和导出，真实单篇中文 PDF 下载已验证，见 [浏览器流程](CNKI_BROWSER.md)。官方 API 仍未接通；原文已取得，阅读处理等待独立预算。以下“自动检索未接通”和旧登录状态是 2026-10-21 的历史快照，不作为当前状态。

状态：**题录导入可用；自动检索未接通**。本次优先支持已实际取得的 CNKI EndNote 标记文本；RIS、BibTeX、RefWorks 和格式化引文会明确拒绝，不按扩展名猜格式。

## 入口

生产项目 `<PROJECT_ROOT>`，Vault `<VAULT_ROOT>`，数据库仍为 `data/production/papers.sqlite3`。

将知网“EndNote → 导出”的文件放到：

`<PROJECT_ROOT>\data\production\imports\cnki\pending`

在项目 PowerShell 执行：

```powershell
.\run.ps1 import-cnki
# 或直接导入指定文件（保留原件）：
.\run.ps1 import-cnki 'data/production/imports/cnki/pending/example.enw'
```

既有每日 12:00 的 `daily` 会扫描相同入口；`--no-fetch` 仍会扫描本地题录。`daily --offline-replay`、渲染和打开笔记不扫描。没有第二个计划任务。

`originals/<SHA256>.bin` 保存原始字节，原文件名在回执中；`receipts/<SHA256>.json` 保存编码、格式、记录数量、每条状态、具体错误、稳定 ID、待确认关系及时间。原 pending 文件保留，可自行移出，已完成文件按哈希跳过；不要修改 originals。手动命令 JSON 的 `this_import` 是本次增量，回执数量是该文件累计结果；重复文件以 `repeated_file`／`repeated_files` 标记，不重复累计论文。

缺字段保持未知；中文编码支持 UTF-8、GB18030、带 BOM 的 UTF-16。无效 DOI/日期/记录会报告失败，其他有效记录继续；无效 ISSN/链接保留原始字段并报告 warning。超出当日候选接纳量的记录标记 deferred，下次扫描继续。

## 去重、候选与资格

复用 `Store`、`papers`、`identifiers`、`provenance`、`state`、`stages`。先按 DOI 或来源 URL 中明确的 dbcode + 可识别 filename 匹配；不把 v 等不透明参数当稳定 ID。没有强标识时，仅完整规范化标题、作者集合、年份、期刊均一致且标识不冲突时匹配；相似标题仅建立待确认关系。无强标识又缺联合字段的记录保留内容指纹，更新存在歧义时需人工核对，不冒险合并中英文标题或网络首发版本。

匹配范围包括正式、候选及归档实体，支持先 CNKI 后其他来源的联合匹配。知网补全只填空值，现有值不覆盖；原始来源字段留在 provenance。涉及多个实体的强标识桥接冲突阻止合并。归档实体导入不直接渲染/恢复，不生成新总结、译文或全文。

知网候选共享原有 **候选 200／精选 100** 的容量、固定、归档恢复规则及每日候选接纳限额。交叉领域分支当前未实现，本次未新增交叉区或配额。

CNKI 是来源；其“核心”等信息只作为原始来源字段。SCIE、中科院分区、CCF 资格仍由原有核验流程决定。题录导入不设已发表资格、不调用模型、不下载全文、不计完整交付。

## 授权取得的全文

```powershell
.\run.ps1 attach-fulltext '<paper_id>' 'C:\path\paper.pdf' --language zh
```

只接受已存在的稳定 ID。原始全文按 SHA-256 保存到 `imports/cnki/fulltext/<paper_id>/`，PDF 调用既有完整性解析，不自动总结。CAJ 或非 PDF 保存原件并标记“待转换/待补全文”；不会改后缀伪装或上传转换服务。新的不同版本遇到已有全文会标记待确认，不覆盖原阶段。

`--language` 保存用户声明，不单凭声明或中文标题跳过翻译。日常处理从完整原文文本层检测语言并记录依据、字符数量、原文哈希；明确中文时跳过翻译，提供 `原始中文.pdf + 阅读笔记.md`，仍必须通过全文解析、总结及原文审核。英文沿用原行为，未知语言不默认中文。历史英文完成状态不批量改写。

为了兼容现有路径、容量、历史简报代码，delivery 回执的 `bilingual`／`bilingual_sha256` 在中文分支指向**实际原始中文 PDF**，同时明确写入 `fulltext_language=zh`、`translation_skipped=true` 和原因。展示层不会标成双语 PDF。

## 网页权限与自动接口

[深圳理工大学图书馆的中国知网说明](https://lib.suat-sz.edu.cn/info/1012/1091.htm)提供官网、CARSI 和 VPN 入口；该校版权公告禁止软件工具批量下载电子资源。它没有提供本项目可用的程序检索接口文档、凭据或权限范围。

本次公开查询及本地配置检查未取得三者，因此没有接入自动检索/API，也没有把第三方逆向 API 或浏览器持续爬取注册为来源。不能据此断言 CNKI 对所有机构都不存在授权接口。

当前普通网页机构访问已显示深圳理工大学，真实题录导出已成功，无需你额外登录。以后若网页实际显示验证码、扫码、学校认证或权限提示，应由用户完成；普通网页登录不意味着自动检索、付费模型或批量下载授权。不会购买、接受新付费协议或发送咨询邮件。

## 验收

详见 [本次验收记录](CNKI_ACCEPTANCE_2026-10-21.md)。真实中文全文尚未取得；全文接入与中文交付行为在临时目录验证，不声称该真实论文已经完成阅读。
