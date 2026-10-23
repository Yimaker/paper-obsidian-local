"""Official NeurIPS proceedings verification; deliberately excludes companion tracks."""
import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

from .core import arxiv_id, now


class Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta, self.links, self.parts = {}, [], []
        self.href, self.label = None, []
        self.in_abstract, self.abstract_done, self.abstract_parts = False, False, []
        self.in_h4, self.heading = False, []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("script", "style"):
            self.skip += 1
        if tag in ("h2", "h3", "h4"):
            self.in_h4, self.heading = True, []
        if tag == "p" and self.in_abstract:
            self.abstract_done = False
        if tag == "meta":
            key = a.get("name", "")
            self.meta.setdefault(key, []).append(a.get("content", ""))
        if tag == "a":
            self.href, self.label = a.get("href"), []

    def handle_data(self, value):
        if self.skip:
            return
        if self.in_h4:
            self.heading.append(value)
        if self.in_abstract and not self.abstract_done:
            self.abstract_parts.append(value)
        self.parts.append(value)
        if self.href is not None:
            self.label.append(value)

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip = max(0, self.skip - 1)
        if tag in ("h2", "h3", "h4") and self.in_h4:
            self.in_h4 = False
            if " ".join(self.heading).strip() == "Abstract":
                self.in_abstract = True
        if tag == "p" and self.in_abstract:
            self.abstract_done, self.in_abstract = True, False
        if tag == "a" and self.href:
            self.links.append((self.href, " ".join(self.label).strip()))
            self.href = None


def verify_neurips(url, http):
    u = urlparse(url)
    if u.scheme != "https" or u.hostname != "proceedings.neurips.cc":
        raise ValueError("只实现了 NeurIPS 官方主会论文集 URL 核验")
    match = re.fullmatch(r"/(?:paper|paper_files/paper)/(\d{4})/hash/([a-f0-9]{32})-Abstract(?:-Conference)?\.html", u.path)
    if not match:
        raise ValueError("不是已支持的主会长文地址；Workshop/Creative/Datasets 等不自动计入")
    year, sourceid = match.groups()
    raw = http.get(url)
    page = Page()
    page.feed(raw.decode("utf-8"))
    get = lambda k: page.meta.get(k, [""])[0]
    title = get("citation_title")
    pdf = get("citation_pdf_url")
    # Older official pages have citation metadata and Paper/Bibtex links.
    pdf = pdf or next((urljoin(url, h) for h, label in page.links if label.lower() in ("paper", "pdf")), "")
    if not title:
        raise ValueError("官方页面缺少 citation_title，无法核验")
    pdf = urljoin(url, pdf)
    if urlparse(pdf).hostname != "proceedings.neurips.cc" or not pdf.lower().endswith(".pdf"):
        raise ValueError("官方页面缺少同站原文链接")
    parts = "\n".join(page.parts)
    if "Advances in Neural Information Processing Systems" not in parts and not get("citation_conference_title").startswith("Advances in Neural"):
        raise ValueError("没有发现正式论文集信息")
    abstract = get("description")
    if not abstract:
        abstract = " ".join(" ".join(page.abstract_parts).split())
    # DOI/arXiv associations are taken only from explicit citation metadata.
    doi = get("citation_doi").lower()
    aliases = [f"neurips:{year}:{sourceid}"] + (["doi:" + doi] if doi else [])
    return {"source": "neurips_proceedings", "aliases": aliases, "title": title, "abstract": abstract,
            "authors": page.meta.get("citation_author", []), "date": get("citation_publication_date").replace("/", "-") or year,
            "date_precision": "year" if len(get("citation_publication_date")) <= 4 else "source", "doi": doi,
            "dates": {"official_citation_publication_date": get("citation_publication_date").replace("/", "-"),
                      "proceedings_year": int(year), "official_verified_at": now(),
                      "date_note": "citation_publication_date 是官方页面的论文集出版元数据；会议年份、首次预印本日期及首次上线日期分别记录，不互相替代"},
            "venue": "NeurIPS", "venue_kind": "conference", "url": url, "pdf_url": pdf,
            "publication": {"status": "published", "source": url, "checked_at": now(), "main_track": True,
                            "paper_type": "full", "proceedings_year": int(year), "verification": "official proceedings citation metadata + main-track URL"}}


class CVFPage(Page):
    def __init__(self):
        super().__init__()
        self.abstract_depth = 0
        self.cvf_abstract = []

    def handle_starttag(self, tag, attrs):
        super().handle_starttag(tag, attrs)
        if tag == "div":
            if dict(attrs).get("id") == "abstract": self.abstract_depth = 1
            elif self.abstract_depth: self.abstract_depth += 1

    def handle_endtag(self, tag):
        if tag == "div" and self.abstract_depth: self.abstract_depth -= 1
        super().handle_endtag(tag)

    def handle_data(self, value):
        if self.abstract_depth: self.cvf_abstract.append(value)
        super().handle_data(value)


def verify_cvpr(url, http):
    """CVF main-conference long papers only; CVPRW/Workshop URL never passes."""
    u=urlparse(url)
    match=re.fullmatch(r"/content/CVPR(\d{4})/html/([A-Za-z0-9_.-]+)_CVPR_\1_paper\.html",u.path)
    if u.scheme!="https" or u.hostname!="openaccess.thecvf.com" or not match:
        raise ValueError("只支持CVF官方CVPR主会路径，不接受Workshop或其他轨道")
    year,slug=match.groups()
    raw=http.get(url).decode("utf-8")
    page=CVFPage(); page.feed(raw)
    get=lambda k:page.meta.get(k,[""])[0]
    expected="Proceedings of the IEEE/CVF Conference on Computer Vision and Pattern Recognition"
    pdf=get("citation_pdf_url")
    if get("citation_conference_title")!=expected or not get("citation_title") or "identical to the accepted versions" not in " ".join(page.parts):
        raise ValueError("没有可核验的CVPR主会录用说明或完整题录")
    first,last=get("citation_firstpage"),get("citation_lastpage")
    if not first.isdigit() or not last.isdigit() or int(last)-int(first)+1<8:
        raise ValueError("没有主会正式长文页码证据")
    if pdf!=f"https://openaccess.thecvf.com/content/CVPR{year}/papers/{slug}_CVPR_{year}_paper.pdf":
        raise ValueError("同站PDF与主会题录不一致")
    months={"January":"01","February":"02","March":"03","April":"04","May":"05","June":"06","July":"07","August":"08","September":"09","October":"10","November":"11","December":"12"}
    month=re.search(r"month\s*=\s*\{([A-Za-z]+)\}",raw)
    month=months.get(month.group(1)) if month else None
    if not month: raise ValueError("官方BibTeX缺少出版月份，不能精确判断近期范围")
    aliases=[f"cvpr:{year}:{slug}"]
    for href,label in page.links:
        if urlparse(href).hostname in ("arxiv.org","www.arxiv.org"):
            aliases.append("arxiv:"+arxiv_id(href))
    return {"source":"cvf_proceedings","aliases":aliases,"title":get("citation_title"),
            "abstract":" ".join(" ".join(page.cvf_abstract).split()),"authors":page.meta.get("citation_author",[]),
            "venue":"CVPR","venue_kind":"conference","date":year+"-"+month,"date_precision":"month",
            "url":url,"pdf_url":pdf,
            "dates":{"official_citation_publication_date":get("citation_publication_date"),"official_proceedings_month":year+"-"+month,
                     "proceedings_year":int(year),"official_verified_at":now(),"date_note":"官方BibTeX精确到月；录用日及首次上线日未知，未补造具体日期。"},
            "publication":{"status":"accepted","source":url,"checked_at":now(),"main_track":True,"paper_type":"full",
                           "page_range":first+"-"+last,"verification":"CVF主会路径、准确会议名称、accepted versions声明、至少8页正式长文题录及同站PDF；保守记录为已录用"}}
