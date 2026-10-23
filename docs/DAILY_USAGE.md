# 日常阅读与维护

在 Obsidian 打开 `<VAULT_ROOT>`，从 [最新简报](../../../AI论文库/00_每日简报/最新简报.md) 开始。

```text
AI论文库/
  00_每日简报/
    最新简报.md
    2026.10.20/
      每日简报.md
  10_论文/
    2026_CVPR_CLCR Cross-Level Semantic Collaborative Representation for Multimodal Learning/
      中英对照.pdf
      阅读笔记.md
  90_候选与待核验/
    2026_PersonaVLM Long-Term Personalized Multimodal LLMs.md
```

每天北京时间12:01使用原 Windows 任务 `PaperObsidianLocal-AI-ML`（2026-10-23 按用户要求由12:00调整）。计划任务进入 `scripts/daily-run.ps1 → run.ps1 → WSL Ubuntu scripts/run.sh → .venv/bin/python -m paper_obsidian daily`。检索、筛选标准、AI原文审校、翻译、缓存及失败恢复沿用原实现。没有安装第二个任务。

同一天多次运行更新同一页。按稳定论文ID累计当天成果，分开列出今日新交付、历史成果迁移／补齐、已缓存复用、待处理／失败。失败、缓存命中、无新增均不能抹掉已完成成果。论文次日才完整交付则记次日，首次发现时间另存。旧论文修订不重复记作新论文。无新增可以直接说明，不重复推荐旧论文凑数量。

最新入口取已登记的实际日常简报中最大日期。离线测试只允许更新已存在的当天页面，不新建虚构日报、不推进或回退最新入口。首次日常完整交付由发布回执确认是2026.10.20；旧10月19日功能验收在项目历史目录。

“我的笔记／我的手写笔记”写在 `PAPER-AUTO:END` 之后。自定义frontmatter会保留。不要手动删改自动区标记。

## 命名规则

- 日期目录按 Asia/Shanghai，格式 `YYYY.MM.DD`。论文年份独立取正式会议／期刊题录；尚无正式年份时取已保存的预印本首次发布年，来源写入 `filename_year_source`；仅有抓取年或OpenAlex未核验日期时写“年份待核实”。
- 候选使用 `年份_可信来源英文原题.md`，正式使用 `年份_会议或期刊_论文标题/`。标题保持原始元数据，不为命名单独调用模型翻译；缺标题时用“标题待补充_序号”。
- 稳定ID、DOI、arXiv ID、数据库主键和请求指纹不变。重命名不触发检索、总结、审校或翻译。
- 清理Windows非法字符、尾随空格／句点、设备保留名及Obsidian链接特殊字符。路径按UTF-16单位保守限制240；确需时才截短标题。同年同名不同论文先追加会议／期刊，再追加持久化确定性序号，大小写不敏感判断，不覆盖、不按同名合并。
- 候选更新名称或晋升时迁移原笔记及其手写内容，维护Markdown、Wiki、显示文字、嵌入、PDF页码和锚点。只有笔记和双语PDF都完成才进入正式库；旧命令准备的结果继续保留为候选。

## 操作入口

在项目根目录运行，每行单独执行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\task.ps1 Status
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\task.ps1 Pause
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\task.ps1 Resume
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\task.ps1 Run
# 完全本地重放：禁止网络、模型和翻译；不新增论文
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\run.ps1 daily --offline-replay
# 仅不检索，仍可能产生正常付费调用
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\run.ps1 daily --no-fetch
```

不要使用普通 `daily --no-fetch` 冒充免费离线审计。状态脚本会显示真实下次触发时间；本机需用户保持登录和处于可运行状态。关机时不能执行，恢复后按原设置补跑一次。

## 归档与恢复

- [历史索引](history/README.md)：旧验收、近期发现、CLCR修复、小批量试运行和旧测试日报。
- `data/production/archive/<稳定ID>/`：旧笔记、单语PDF、证据附件；原有译文及模型回执继续保存在各自缓存目录。
- `data/production/layout-migration/plan.json`：旧路径→新路径及原始哈希；`progress.json`：逐项复制检查点；迁移脚本重复执行已完成计划为无操作。
- `data/production/backups/layout-20261020-220205/`：停写后的SQLite、完整Vault ZIP、源码配置ZIP、任务XML及文件哈希清单；不含明文密钥导出。

如迁移中断，保持原任务禁用及 `data/production/MAINTENANCE` 存在；确认无写入后执行 `tools/migrate_reading_layout.py` 可从复制检查点继续。异常不清空生产数据。需回滚时先停写，再使用备份恢复到单独目录核对；确认后恢复Vault、数据库和源码配置，保留当前现场，不直接覆盖未知用户新增内容。不要删除状态表、缓存、失败回执或未知请求记录来“修复”。

期刊白名单仍为空，SCI/SCIE与中科院分区覆盖未完成；官方会议接入也未穷尽。来源网络健康以已保存回执为准，本轮未发新网络请求。自动审校和翻译仍保留原有限制，见[项目交接书](../项目交接书.md)。
