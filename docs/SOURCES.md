# 官方来源与依赖核查

2026-10-19 晚间近期发现验收更新：本机个人key主题检索OpenAlex200、arXiv200，原匿名预算问题本次请求未再出现。CCF官网仍为2026第七版（4月9日勘误），本地正式PDF哈希一致。新的官方证据确认：[文献情报中心声明](https://www.las.cas.cn/news/tzgg/202603/t20260327_8178738.html)，2026年起不再更新发布期刊分区表；下文白天尚未确认的传闻已有官方正文，详细渠道见[JOURNAL_WHITELIST_STATUS.md](JOURNAL_WHITELIST_STATUS.md)。旧记录保留为历史。

核查日期：2026-10-19，Asia/Shanghai。联网请求从本机 WSL 发出；浏览检索用于核对官方资料，没有把连接器提供的远程抓取结果当作本机 API 连通性测试。

## 数据来源

| 项目 | 官方依据 | 本次处理 |
|---|---|---|
| OpenAlex | [当前认证说明](https://help.openalex.org/api/authentication/)、[API 快速参考](https://help.openalex.org/api/llm-quick-reference/) | 使用 Works API、Bearer key、有限 GET 重试。官方当前允许少量免密访问；本机主题检索 429，响应明确为共享 IP 匿名预算不足；稳定 Work ID 单条读取 200 并成功解析写入测试候选。两种操作状态分别报告。|
| arXiv | [官方 API 手册](https://info.arxiv.org/help/api/user-manual.html)、[使用条款](https://info.arxiv.org/help/api/tou.html) | Atom API、lastUpdatedDate 检索、更新排序、3.1 秒间隔。真实检索返回 50 篇首批记录，按关键词与排除词保留相关候选，批次未穷尽不会标成收全。|
| OpenReview | [官方 API2 数据检索说明](https://docs.openreview.net/how-to-guides/data-retrieval-and-modification/how-to-get-all-notes-for-submissions-reviews-rebuttals-etc)、[ICLR 2026 官方 CFP](https://iclr.cc/Conferences/2026/CallForPapers) | `content.venueid` 区分公开录用与投稿。会议配置接口 200，notes 接口 403 `ChallengeRequiredError`。未绕过验证；没有声称完成本机录用论文获取。|
| NeurIPS | [官方论文集真实示例](https://proceedings.neurips.cc/paper/2017/hash/3f5ee243547dee91fbd053c1c4a845aa-Abstract.html) | 实际核验元数据、主会 URL 和原始 PDF，下载 569417 字节，11 页文本解析成功。读取官方 PDF 首页面确认标题与会议页脚。不是当日新论文，也不是离线虚构数据。|
| Semantic Scholar | 尚未接入 | 可选信息补全，当前无空实现。|

注意：源站元数据可能存在旧摘要；本次 NeurIPS 示例网页摘要与 PDF 的数值版本不同。程序保留来源摘要并明确标记版本可能不同，全文模型必须优先依据 PDF 原文，而非仅依据网页摘要。

## 等级与发表核验

CCF 官方 [第七版目录发布页](https://www.ccf.org.cn/Academic_Evaluation/By_category/) 日期为 2026-03-31，注明 4 月 9 日修正勘误。该页限定 Full/Regular 长文，排除 Short、Demo、Technical Brief、Summary、Findings 与 Workshop 等形式。

实际下载的 [第七版正式 PDF](https://www.ccf.org.cn/ccf/contentcore/resource/download?ID=112CF3BF7E1140ACEB271ADAED12A67ADFABB8FF099E40C2759502A85C8A281F) 共 72 页，第 57 页人工智能会议 A 类为 AAAI、NeurIPS、ACL、CVPR、ICCV、ICML、ICLR。本项目仅内置这 7 项真实会议。官网人工智能分栏目页面仍有旧名单差异，因此以正式 PDF 为准。

本地证据：`data/evidence/ccf-2026.pdf`。

SHA-256：`271b630b576bf8a4f802e767f5694caded93680e22b3a19bef7902591c45c1d3`。

SCI/SCIE 使用 [Clarivate Master Journal List](https://mjl.clarivate.com/) 独立核查。分区使用 [中科院期刊分区表官网](https://www.fenqubiao.com/) / [官方开放平台](https://webapi.fenqubiao.com/Help) 的有权获取材料。本次本机对后两者均连接失败；搜索可见登录后 API 说明，也有服务变更的非官方报道，但没有获得可确认当前服务的官方正文。**不能声称官方 API 当前可用，也不能确认服务变更传闻。** 因而期刊名单留空，采用可导入的本地人工核验材料，不引入 JCR 或第三方榜单替代。

## 参考项目、维护情况与复用选择

- [paper-digest-obsidian](https://github.com/dueude666/paper-digest-obsidian)：核对 README、`src/paper_digest/http.py`、LICENSE 和公开仓库元数据。MIT；公开元数据记录最近 push 为 2026-03-09。其 README 将 OpenAlex 列为后续扩展，含启发式总结，缺少本次严格等级证据门槛。本项目采用相似的分层思路，自行实现严格证据筛选与原文模型链路，没有运行或整包引入它的代码。
- [read-summarize-papers](https://github.com/Angelamer/read-summarize-papers)：核对 README、`src/read_summarize_papers/cli.py`、LICENSE 和公开仓库元数据。MIT；公开元数据记录最近 push 为 2026-08-13。CLI 主流程要求 Zotero 配置，且包含研究图谱等扩展，因此不适合作为这次最小依赖主流程直接安装。
- 两者的公开 push 时间只描述核查时的仓库状态，不等价于维护质量保证；不是完整安全审计。尝试本机 GitHub API 目录读取时遇共享 IP 限流，源码正文通过官方 raw URL 核对。

主流程实际复用标准库 SQLite/venv/路径处理，以及 `httpx 0.28.1`、`pypdf 6.11.0`、`PyYAML 6.0.3`、`python-dotenv 1.2.2`，全部安装在项目 `.venv`。

## 双语 PDF 组件

[PDFMathTranslate-next 官方仓库](https://github.com/PDFMathTranslate-next/PDFMathTranslate-next)、[CLI 文档](https://pdf2zh-next.com/getting-started/USAGE_commandline.html)、[高级参数](https://pdf2zh-next.com/advanced/advanced.html)、[翻译服务说明](https://pdf2zh-next.com/advanced/Documentation-of-Translation-Services.html) 均已核对。

当前读取的官方 `pyproject.toml` 为 `pdf2zh-next 2.9.0`，AGPL-3.0，Python `>=3.10,<3.14`，依赖 BabelDOC、ONNX 布局解析及多个翻译 SDK。[BabelDOC 官方声明](https://github.com/funstory-ai/BabelDOC/blob/main/pyproject.toml) 读取为 0.6.4，同样 AGPL-3.0 和 Python `<3.14`，CUDA 作为可选项。主流程不依赖它们，安装脚本单独建立 Python 3.13 环境。

适配器使用官方配置模型中的 `[translate_engine_settings]` 和 OpenAI 配置字段、CLI `--config-file/--output/--qps` 参数。未把密钥放在参数中。**组件安装、依赖可解性、真实模型翻译和双语排版尚未验证**；配置可用后须先在测试 Vault 验证一份真实 PDF。

原文获取只使用 OA/官方公开接口；不绕过付费墙或网站挑战，也不上传论文资料或密钥到 Git 远程。
