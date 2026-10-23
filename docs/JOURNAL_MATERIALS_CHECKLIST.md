# 学校图书馆材料清单与个人阅读标注方案

2026-10-20。正式白名单维持 journals=0，不更改当前正式库政策。

## 官方声明已核对

本次直接读取[中科院文献情报中心2026-03-27声明](https://www.las.cas.cn/news/tzgg/202603/t20260327_8178738.html)，HTTP 200。原文明确“自2026年起……将不再更新与发布期刊分区表”，并称其他机构发布的分区表与该中心无关。声明正文和本次获取信息保存在 `data/repair-2026-10-20/official-journal-statement.json`。

因此不索取所谓该中心官方2026新版。声明没有断言旧平台全面停服；先前页面无法访问也不能得出该结论。历史授权数据是否能取得，由学校图书馆确认。

## 只需先取少量目标期刊

请向学校图书馆学科服务或查收查引部门提出：先核查你准备阅读的3—10本AI/ML目标期刊，按ISSN/eISSN逐一对应。无需全库导出，也不需要提供账号密码。

| 材料 | 至少保留什么 | 保存目录（项目根目录下） |
| --- | --- | --- |
| 历史官方中科院大类分区 | 期刊名、ISSN/eISSN、实际年份、版本、大类名称、大类分区、官方来源链接、导出时间；原导出或可读截图/PDF | `data/journal-materials/cas/` |
| 独立SCIE收录证据 | 期刊名、ISSN/eISSN、Science Citation Index Expanded全称、核查/数据时间、覆盖起止或变更记录、官方来源及原始材料 | `data/journal-materials/scie/` |
| 学校成果认定依据（正式认定才需要） | 学校或科研部门正式通知：2026年论文适用哪年、哪个版本分区，适用发表日期与范围 | `data/journal-materials/school-policy/` |
| 待导入CSV | 从现有 `whitelists/journals-template.csv` 复制后填写，证据相对路径指向上述文件 | `data/journal-materials/import/verified-journals.csv` |

SCIE与中科院分区是两种独立证据，不共用一份含糊的“SCI一区”说明。ESCI、JCR Q1/Q2、小类分区均不能替代当前规则中的SCIE与中科院大类1/2区。当前收录不能自动证明旧论文发表时也在覆盖范围。

## 个人阅读与学校认定分别标注

个人阅读字段：`cas_reference_year`、`cas_reference_edition`、`cas_major_category`、`cas_major_zone`、`cas_source`、`cas_evidence_sha256`、`scie_checked_at`、`scie_evidence_sha256`、`usage=personal_reading_reference`。

学校认定另记：`institution`、`assessment_policy_source`、`assessment_policy_evidence_sha256`、`assessment_applicable_from/to`、`assessment_status`。拿不到本校政策就写“本校成果认定未核实”，不作肯定判断。

可选方案（均尚未启用）：

1. **维持当前正式政策**：取得历史材料与本校跨年适用依据后，按现有导入器进入正式白名单。
2. **个人阅读参考模式**：另建 `whitelists/journals-reading-reference.json`，只筛选个人候选阅读清单，不参与正式资格函数。每条显示“按YYYY年中科院大类N区历史资料筛选，仅供个人阅读；非2026官方分区，本校认定未核实”。例如实际材料若为2025，就明示2025；年份来自材料，不预填猜测。
3. **两条并存**：个人阅读用方案2；正式库继续方案1，只有满足正式规则才晋升。

方案2/3需你选择并提供可靠材料后再实现，不将它们悄悄接入正式库。个人阅读无需本校认定政策，但仍需真实历史分区与独立SCIE证据。

## 现有导入器如何用

正式导入前先人工核对材料内容，填写CSV每一行。关键字段是 `name, issns, sci_index, sci_year, sci_source, sci_checked_at, sci_evidence_file, cas_major_zone, cas_major_category, cas_partition_scope, cas_edition, cas_year, cas_source, cas_checked_at, cas_evidence_file, coverage_from, coverage_to`。ISSN多个值用分号分隔，时间带时区。

把较旧分区用于较新论文时，现有正式导入器额外要求 `coverage_policy_source` 与 `coverage_policy_evidence_file`。这些代表机构适用依据，不能靠把coverage_to改到2026来绕过，也不能用个人阅读偏好冒充学校通知。

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\run.ps1 import-whitelist journals '<PROJECT_ROOT>\data\journal-materials\import\verified-journals.csv'
```

导入器先校验整批字段、ISSN校验位、官方域名、文件存在及日期；分别计算SCI/分区/学校政策证据SHA-256，全部合格后备份并更新白名单。它不能自动判断截图真伪或材料内容是否真的支持某分区，仍须人工核对。
