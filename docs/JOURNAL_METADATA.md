> 2026-10-23更新：资格政策已切换为独立的当前/发表时双口径。下文历史说明中“必须历史通过才能阅读”已被新政策替代。见[本轮验收与材料清单](dual-eligibility-20261023/验收与待补材料.md)。完整白名单仍0；已新增3本SCIE单项证据。

# 期刊信息查询与笔记头部

日常入口 `run.ps1 daily` 在每篇论文的资格判定后、正文处理前调用 `JournalService`；即使已交付或资格未知，也会补已有笔记的头部。查询、缓存和笔记更新异常有独立边界，不阻断已有合格论文的原文、总结和翻译。未知期刊生成候选元数据笔记，是否生成候选正文继续服从原有配置；不会自动付费总结所有未知期刊。

## 数据源和实际能力

- OpenAlex `/sources/issn:<ISSN>`：复用现有API Key、HTTP缓存和请求器；校验ISSN校验位及返回标识、来源类型。同一输入的多个ISSN指向不同期刊时拒绝合并。不按名称模糊匹配；当前名称与题录名称不同会提示可能更名及历史适用范围未知。
- Clarivate MJL：通过现有已登录授权的 **Firecrawl CLI服务凭据**（不是网页账号、Cookie）读取公开 `search-results?issn=...` 页面。只接受“单个精确结果＋该结果内的ISSN＋Core Collection索引字段”；过滤器中出现SCIE、有JIF、没有搜索结果均不能作为收录证据。只记录查询日快照，不推断论文发表年的收录。配置仅用 `basic` 抓取；遇到登录跳转、验证码、拒绝访问或超时即停止。不调用未公开MJL内部接口。
- JCR各学科分区、JIF、中科院大类/小类、新锐分区：提供CSV/JSON导入。当前未配置自动指标提供方；不会由OpenAlex的其他引用指标推导JIF。
- LetPub：2026-10-20检查公开主页、期刊工具、robots及公开API搜索。robots允许公开目录，但没有找到公开API或明确自动采集指标授权，故 **自动抓取关闭**，只支持人工整理公开资料后本地导入，并标“第三方来源”。没有虚构接口；这不代表断言LetPub永远没有API。
- Clarivate官方Journals API文档明确需要JCR/InCites附加付费许可；网页账号不是API许可。若将来需要官方JIF/JCR自动指标，具体服务和申请入口为 [Web of Science Journals API](https://developer.clarivate.com/apis/wos-journal)。本次不需注册即可完成查询及导入功能。

实测来源：[MJL Machine Learning](https://mjl.clarivate.com/search-results?issn=0885-6125)、[Springer Machine Learning](https://link.springer.com/journal/10994)、[LetPub robots](https://www.letpub.com.cn/robots.txt)、[LetPub期刊工具](https://www.letpub.com.cn/index.php?page=journalapp)。公开页面受服务、配额、结构和访问规则变化影响，失败时转待补状态。

## 缓存、资格和安全更新

继续使用 `data/production/papers.sqlite3` 的 `state` / `stages`；不建第二套数据库。`journal:alias:<ISSN>` 指向 `journal:cache:<ISSN-L>`，多篇共享。导入记录为 `journal:import:<ISSN>`，保留每项来源、查询日期、年份和证据哈希，重导同一记录不重复。原始请求复用 `http-cache`；MJL证据在 `journal-evidence/`；导入回执在 `journal-imports/`。默认成功缓存30天、失败缓存24小时；MJL每天最多3个查询，不自动重试。过期或失败的旧证据保留并标历史/待更新。

`journal_info` 是独立处理阶段；不会改论文内容指纹、总结、审校或翻译缓存。已有笔记仅更新 `PAPER-AUTO` 内嵌的 `JOURNAL-INFO` 标记区；标记异常拒绝覆盖并报告失败。更新前按既有 `atomic_write` 规则备份，其他正文、YAML、手写和附件字节保留。正式笔记和候选通过原有稳定ID查找，不新建第二份。新期刊候选按原有年份+标题分配器命名；正式出版版本的OpenAlex元数据年份仅用于文件名，不证明资格。补已有头部不搬迁目录。

正式资格仍只认 `eligibility.classify` 与原有已审计白名单：独立SCIE证据＋中科院大类1/2区＋发表日期适用范围。JCR、小类、新锐及JIF不能替代。指标导入不会写白名单或提升正式计数。界面区分：

1. 已核验符合SCI一二区：现有严格资格通过。
2. 第三方资料显示符合、待官方核验：最新可取得SCIE/中科院大类资料显示符合，但正式日期适用口径未通过，不计正式数量。
3. 分区或SCIE收录信息未知／资格待核验：证据缺失、过期或冲突，不计正式数量。

一个指标有多个学科就逐项显示。同一体系/年份/版本/学科出现不同值时保留双方并标冲突，不选择最高区。中科院 `edition_year`、JIF/JCR `metric_year`、JCR `report_year` 分别记录；不自动加1推算报告年。每项显示来源与查询时间，最新可取得与论文年份对应记录分开。会议头部仅列会议名称、年份、已取得的CCF等级及证据，不套期刊指标。

## 使用

```powershell
# 在线补期刊缓存和已有笔记头部；不生成正文或PDF
.\run.ps1 journal-refresh
# 指定稳定ID，或只读取本地缓存
.\run.ps1 journal-refresh --paper-id 3a3a2d1c5c6a9d443e89
.\run.ps1 journal-refresh --offline
# 导入并离线补所有已有头部，不联网、不调用模型/翻译
.\run.ps1 journal-import docs/journal-metrics-template.csv
.\run.ps1 journal-import path/to/metrics.json
```

模板是空文件，不含虚构指标。JSON格式为 `{"journals":[{"issns":["有效ISSN"],"metrics":[{...}]}]}`；CSV每行一项指标，`issns` 用英文分号分隔。多个JCR学科使用多行，不能拼成“最高Q1”。

必填共有字段：`field`、`value`、`source`（HTTPS来源）、`checked_at`（带时区真实查询时间）。缺失指标不导入该行，系统显示待核验，不填写0。

| field | 额外必填 | value |
|---|---|---|
| scie | as_of（状态日期） | true / false，必须有明确收录资料 |
| cas_major | category、edition、edition_year | 1～4 |
| cas_minor | category、edition、edition_year | 1～4，每小类一行 |
| jcr | category、metric_year；report_year取得时另填 | Q1～Q4，每学科一行 |
| jif | metric_year；report_year取得时另填 | 实际数值 |
| xinrui | category、edition、edition_year | 1～4；独立体系 |

可选 `note`、`evidence_file`（相对于导入文件或绝对路径）。声明 `verification=verified_official` 需对应指标官方域名及真实本地证据文件，否则整批拒绝。第三方来源即使填写“官方”也不能升级。URL和本地文件不自动证明语义支持，导入者仍须核对原资料；正式资格继续走独立白名单审计。

本次实际导入示例：`data/production/journal-acceptance/machine-learning-publisher-import.json`。Springer页面列出 **JIF 4.9，指标年2025**，仅标出版商第三方来源；未给出JCR报告年，不补猜。该值不能当作2001年 Random Forests 的历史指标。

## 每日12点

继续使用现有 `PaperObsidianLocal-AI-ML`：`scripts/daily-run.ps1 -> WSL Ubuntu scripts/run.sh daily -> run_daily`。没有注册新的任务，也不修改现有触发器。MJL使用已实测的Windows Node/Firecrawl绝对路径，从同一WSL环境调用；如果CLI凭据/配额失效，会记录失败、短缓存，其他处理继续。检查 `journal-refresh-latest.json`、`journal_info` 阶段、`journal_warnings` 及日常简报。

## 2026-10-23 JCR 独立阅读通道更新

新增官方SCIE＋论文内容相关学科JIF Q1/Q2通道，独立于中科院当前/发表时及CCF A。JCR不再一概排除阅读，但仍不能替代中科院字段。四刊真实官方记录和4篇候选结果、导入/复核机制、预取全文状态及回滚详见 [JCR验收报告](jcr-20261023/验收报告.md)。本轮新增正式论文资格0；2篇待全文学科审核、1篇身份待核验、1篇JCR Q3失败。无付费或生产daily。
