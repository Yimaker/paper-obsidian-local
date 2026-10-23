from __future__ import annotations

import json
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from urllib.parse import urlencode, urlparse

import httpx

from .core import TZ, arxiv_id, atomic_write, digest, doi_id, now, safe_error, today


class FetchError(RuntimeError):
    pass


class HTTP:
    def __init__(self, c, env, *, transport=None, sleep=time.sleep):
        self.c, self.env, self.sleep = c, env, sleep
        self.cache = c["paths"]["data"] / "http-cache"
        self.client = httpx.Client(timeout=c["network"]["timeout_seconds"], follow_redirects=True,
                                   headers={"User-Agent": c["network"]["user_agent"]}, transport=transport)
        self.last = {}
        self.events = []

    def get(self, url, params=None, *, refresh=False, binary=False):
        host = urlparse(url).hostname or ""
        if urlparse(url).scheme != "https":
            raise FetchError("只接受 HTTPS 公开来源")
        headers = {}
        if host == "api.openalex.org" and self.env.get("OPENALEX_API_KEY"):
            headers["Authorization"] = "Bearer " + self.env["OPENALEX_API_KEY"]
        key = digest([url, params, digest(headers)])
        path = self.cache / (key + ".bin")
        ttl = self.c["network"]["cache_hours"] * 3600
        if not refresh and path.exists() and time.time() - path.stat().st_mtime < ttl:
            self.events.append({"host": host, "status": "cache_hit", "cache_key": key})
            return path.read_bytes()
        error = "unknown"
        for attempt in range(self.c["network"]["retries"] + 1):
            diagnostic = ""
            interval = 3.1 if host.endswith("arxiv.org") else 1.0
            self.sleep(max(0, interval - (time.monotonic() - self.last.get(host, 0))))
            self.last[host] = time.monotonic()
            try:
                with self.client.stream("GET", url, params=params, headers=headers) as response:
                    if response.is_error:
                        snippet=bytearray()
                        for chunk in response.iter_bytes():
                            snippet.extend(chunk[:max(0,8192-len(snippet))])
                            if len(snippet)>=8192:break
                        diagnostic=bytes(snippet).decode('utf-8',errors='replace').lower()
                    response.raise_for_status()
                    limit = self.c["limits"]["pdf_max_mb"] * 1024 * 1024 if binary else 15 * 1024 * 1024
                    chunks, size = [], 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > limit:
                            raise FetchError("响应超过配置的大小限制")
                        chunks.append(chunk)
                    raw = b"".join(chunks)
                if binary and not raw.startswith(b"%PDF-"):
                    raise FetchError("下载结果不是 PDF（可能为登录页或错误页）")
                atomic_write(path, raw)
                self.events.append({"host": host, "status": "success", "http_status": response.status_code, "cache_key": key})
                return raw
            except httpx.HTTPStatusError as exc:
                code = exc.response.status_code
                # Keep diagnostics useful without retaining URLs, credentials, or arbitrary provider text.
                # Read the bounded diagnostic before the stream context closes;
                # .read() here would itself raise StreamClosed on live errors.
                category = ("authentication" if code == 401 or (code == 403 and any(x in diagnostic for x in ("api key", "api_key", "unauthorized", "authentication")))
                            else "quota_or_rate_limit" if code == 429
                            else "access_denied" if code == 403
                            else "server" if code >= 500 else "interface")
                self.events.append({"host": host, "status": "failed", "http_status": code, "category": category,
                                    "anonymous_budget_exhausted": "anonymous" in diagnostic and any(x in diagnostic for x in ("budget", "limit", "quota")),
                                    "challenge_required": "challenge" in diagnostic})
                error = f"HTTP {code}"
                if code not in (408, 429, 500, 502, 503, 504):
                    break
                retry_after = exc.response.headers.get("Retry-After", "")
                pause = min(30, int(retry_after)) if retry_after.isdigit() else min(20, 2 ** (attempt + 1))
                self.sleep(pause)
            except (httpx.HTTPError, OSError) as exc:
                error = safe_error(exc)
                self.events.append({"host": host, "status": "failed", "category": "network", "error": error})
                self.sleep(min(20, 2 ** (attempt + 1)))
        # URL paths/queries may contain provider credentials; log only host and sanitized status.
        raise FetchError(f"{host}: {error}")

    def json(self, url, params=None, **kw):
        return json.loads(self.get(url, params, **kw))


def abstract_from_index(index):
    pairs = [(pos, word) for word, positions in (index or {}).items() for pos in positions]
    return " ".join(word for _, word in sorted(pairs))


def openalex_paper(w):
    loc = w.get("primary_location") or {}
    source = loc.get("source") or {}
    doi = doi_id(w.get("doi"))
    aliases = ["openalex:" + w["id"].split("/")[-1]] + (["doi:" + doi] if doi else [])
    pdf = (w.get("best_oa_location") or {}).get("pdf_url") or (loc.get("pdf_url") if loc.get("is_oa") else "")
    # Only a direct repository location associates arXiv with the same OpenAlex work.
    for location in w.get("locations", []):
        u = location.get("landing_page_url") or ""
        if urlparse(u).hostname in ("arxiv.org", "www.arxiv.org"):
            aliases.append("arxiv:" + arxiv_id(u))
    published = loc.get("is_published") is True and loc.get("version") == "publishedVersion"
    return {"source": "openalex", "aliases": aliases, "title": w.get("title") or w.get("display_name", ""),
            "abstract": abstract_from_index(w.get("abstract_inverted_index")), "date": w.get("publication_date", ""),
            "dates": {"openalex_publication_date": w.get("publication_date"), "openalex_created_date": w.get("created_date"),
                      "openalex_updated_date": w.get("updated_date"), "openalex_retrieved_at": now()},
            "authors": [a["author"]["display_name"] for a in w.get("authorships", [])],
            "doi": doi, "issns": source.get("issn") or [], "venue": source.get("display_name", ""),
            "venue_kind": source.get("type", ""), "url": w.get("doi") or w["id"], "pdf_url": pdf,
            "cited_by_count": w.get("cited_by_count", 0),
            "publication": {"status": "published" if published else "unknown", "source": w["id"],
                            "checked_at": now(), "version": loc.get("version"), "work_type": w.get("type")}}


def arxiv_papers(raw):
    ns = {"a": "http://www.w3.org/2005/Atom", "x": "http://arxiv.org/schemas/atom"}
    root = ET.fromstring(raw)
    papers = []
    for e in root.findall("a:entry", ns):
        def get(tag):
            return " ".join((e.findtext(tag, "", ns)).split())
        url = get("a:id")
        if "api/errors" in url:
            raise FetchError("arXiv 返回查询错误，请检查主题语法")
        aid, doi = arxiv_id(url), doi_id(get("x:doi"))
        pdf = next((x.attrib["href"].replace("http:", "https:") for x in e.findall("a:link", ns)
                    if x.attrib.get("title") == "pdf"), "https://arxiv.org/pdf/" + aid)
        papers.append({"source": "arxiv", "aliases": ["arxiv:" + aid] + (["doi:" + doi] if doi else []),
                       "title": get("a:title"), "abstract": get("a:summary"), "date": get("a:published")[:10],
                       "updated_date": get("a:updated")[:10], "authors": [a.findtext("a:name", "", ns) for a in e.findall("a:author", ns)],
                       "dates": {"preprint_published": get("a:published"), "preprint_updated": get("a:updated"), "arxiv_retrieved_at": now()},
                       "arxiv_categories": [x.attrib.get("term", "") for x in e.findall("a:category", ns)],
                       "arxiv_id": aid, "doi": doi, "url": "https://arxiv.org/abs/" + aid, "pdf_url": pdf,
                       "journal_ref": get("x:journal_ref"), "comment": get("x:comment"),
                       "publication": {"status": "preprint", "source": url, "checked_at": now()}})
    return papers, int(root.findtext("{http://a9.com/-/spec/opensearch/1.1/}totalResults", str(len(papers))))


def content_value(content, name, default=""):
    v = content.get(name, default)
    return v.get("value", default) if isinstance(v, dict) else v


def openreview_paper(n, profile):
    content = n.get("content", {})
    value = lambda k, d="": content_value(content, k, d)
    venueid = value("venueid")
    expected = profile["venue_id"]
    venue_text = value("venue")
    # Exact, separately verified main-conference profile; submission/withdrawal IDs never pass.
    accepted = venueid == expected and not any(x in venue_text.lower() for x in ("workshop", "withdraw", "reject", "submitted", "conditional"))
    aid = arxiv_id(value("arxiv_id"))
    doi = doi_id(value("doi"))
    aliases = ["openreview:" + n["id"]]
    if doi:
        aliases.append("doi:" + doi)
    # Only explicit structured IDs, not a model or a title match.
    if aid and ("arxiv.org" in value("arxiv_id") or aid[:4].isdigit()):
        aliases.append("arxiv:" + aid)
    pdf = value("pdf")
    pdf = "https://openreview.net" + pdf if pdf.startswith("/") else pdf
    date = datetime.fromtimestamp((n.get("pdate") or n.get("cdate") or 0) / 1000, TZ).date().isoformat()
    return {"source": "openreview", "aliases": aliases, "title": value("title"), "abstract": value("abstract"),
            "authors": value("authors", []), "date": date, "venue": profile["acronym"], "venue_kind": "conference",
            "doi": doi, "url": "https://openreview.net/forum?id=" + n["id"], "pdf_url": pdf,
            "publication": {"status": "accepted" if accepted else "submitted", "source": "https://api2.openreview.net/notes?id=" + n["id"],
                            "checked_at": now(), "venue_id": venueid, "main_track": accepted and profile.get("main_track") is True,
                            "paper_type": profile.get("paper_type", "unknown"),
                            "profile_source": profile.get("source", ""), "venue_label": venue_text}}


def query_signature(c, name):
    s = c["search"]
    return "source:" + name + ":" + digest({k: s[k] for k in ("keywords", "exclude", "arxiv_categories", "start_date", "end_date", "per_source", "max_pages")})[:20]


def window(c, store, name):
    s = c["search"]
    end = s.get("end_date") or today()
    state = store.get_state(query_signature(c, name), {})
    if state.get("resume"):
        r = state["resume"]
        return r["start"], r["end"], r["full"], state
    last = state.get("last_success")
    backfill = state.get("last_backfill", "1900-01-01")
    full = not last or (datetime.fromisoformat(end).date() - datetime.fromisoformat(backfill).date()).days >= s["full_backfill_every_days"]
    start = s["start_date"] if full else max(s["start_date"], (datetime.fromisoformat(last) - timedelta(days=s["lookback_days"])).date().isoformat())
    return start, end, full, state


def discover(name, c, http, start, end, profiles):
    s = c["search"]
    size = min(100, s["per_source"])
    offset = s.get("_page_start", 0)
    papers, truncated = [], False
    if name == "official":
        return official_papers(c, http, start, end)
    elif name == "openalex":
        query = " OR ".join('"' + k.replace('"', '') + '"' for k in s["keywords"])
        for page in range(offset + 1, offset + s["max_pages"] + 1):
            params = {"search": query, "filter": f"from_publication_date:{start},to_publication_date:{end}",
                      "sort": "publication_date:desc", "per_page": size, "page": page}
            d = http.json("https://api.openalex.org/works", params)
            papers.extend(openalex_paper(w) for w in d["results"])
            total = d.get("meta", {}).get("count", len(papers))
            if offset * size + len(papers) >= total or len(d["results"]) < size:
                break
        truncated = total > offset * size + len(papers)
    elif name == "arxiv":
        terms = " OR ".join('all:"' + k.replace('"', '') + '"' for k in s["keywords"])
        cats = " OR ".join("cat:" + k for k in s["arxiv_categories"])
        query = f"({terms}) AND ({cats}) AND lastUpdatedDate:[{start.replace('-', '')}0000 TO {end.replace('-', '')}2359]"
        for page in range(offset, offset + s["max_pages"]):
            raw = http.get("https://export.arxiv.org/api/query", {"search_query": query, "start": page * size,
                        "max_results": size, "sortBy": "lastUpdatedDate", "sortOrder": "descending"})
            batch, total = arxiv_papers(raw)
            papers.extend(batch)
            if offset * size + len(papers) >= total or len(batch) < size:
                break
        truncated = total > offset * size + len(papers)
    elif name == "openreview":
        for profile in profiles:
            for page in range(offset, offset + s["max_pages"]):
                d = http.json("https://api2.openreview.net/notes", {"content.venueid": profile["venue_id"], "limit": size,
                            "offset": page * size, "sort": "pdate:desc"})
                batch = d.get("notes", [])
                papers.extend(openreview_paper(n, profile) for n in batch)
                if len(batch) < size:
                    break
                if page + 1 == offset + s["max_pages"]:
                    truncated = True
        papers = [p for p in papers if start <= p["date"] <= end]
    else:
        raise ValueError("未实现的来源: " + name)
    return papers, truncated


def official_papers(c, http, start, end):
    """Reuse verified NeurIPS/CVF parsers with bounded index discovery.

    Index links alone do not grant eligibility. Each selected main paper passes
    the existing official metadata/track/full-paper gate before ingestion.
    """
    from .proceedings import Page, verify_neurips, verify_cvpr
    from urllib.parse import urljoin
    s=c['search'];size=s['per_source'];offset=s.get('_page_start',0)*size
    current=int(end[:4]);first=max(int(start[:4]),current-1)
    matches=[]
    errors=[]
    for year in range(current,first-1,-1):
        for url in (f'https://proceedings.neurips.cc/paper_files/paper/{year}',
                    f'https://openaccess.thecvf.com/CVPR{year}?day=all'):
            try:
                page=Page();page.feed(http.get(url).decode('utf-8'))
                for href,title in page.links:
                    absolute=urljoin(url,href)
                    main=(re.fullmatch(r'https://proceedings\.neurips\.cc/paper_files/paper/\d{4}/hash/[a-f0-9]{32}-Abstract-Conference\.html',absolute) or
                          re.fullmatch(r'https://openaccess\.thecvf\.com/content/CVPR\d{4}/html/[^/]+_paper\.html',absolute))
                    if main and any(term in title.lower() for term in ('multimodal','multi-modal','language model','machine learning','representation learning','reasoning','agent')):
                        matches.append((absolute,title))
            except Exception as exc:errors.append(safe_error(exc))
    matches=list(dict.fromkeys(matches))
    if not matches and errors:raise FetchError('官方论文集索引暂不可用；现有合格候选继续处理')
    results=[]
    for url,title in matches[offset:offset+size]:
        try:
            p=verify_neurips(url,http) if 'neurips.cc' in url else verify_cvpr(url,http)
            # Month precision is compared as the actual calendar interval.
            pdate=p.get('date','')
            if pdate[:7] < start[:7] or pdate[:7] > end[:7]:continue
            p['dates']['retrieved_at']=now()
            results.append(p)
        except Exception:
            # Keep the index candidate pending instead of losing a paper merely
            # because one official detail page was temporarily unavailable.
            results.append({'source':'official_index','aliases':['official-index:'+url],
                'title':title,'abstract':'','url':url,'pdf_url':'','date':'',
                'publication':{'status':'unknown','source':url,'checked_at':now()},
                'dates':{'retrieved_at':now()},'venue_kind':'conference',
                'venue':'NeurIPS' if 'neurips.cc' in url else 'CVPR'})
    if errors: raise FetchError('官方索引部分失败；窗口未完成，允许去重重放')
    return results,len(matches)>offset+size


def relevance(p, c):
    if c.get("_branch")=="cross":
        from .branches import matches
        return len(matches(p)) if not any(x.lower() in (p.get("title","")+" "+p.get("abstract","")).lower() for x in c["search"]["exclude"]) else 0
    text = (p.get("title", "") + " " + p.get("abstract", "")).lower()
    if any(k.lower() in text for k in c["search"]["exclude"]):
        return 0
    return sum(1 for k in c["search"]["keywords"] if k.lower() in text)
