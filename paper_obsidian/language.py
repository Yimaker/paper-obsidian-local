"""Language decisions use actual parsed full text, never title/venue/default."""
import re
from .core import digest


def detect(parsed):
    text = '\n'.join(parsed.get('pages', []))
    chinese = len(re.findall(r'[\u4e00-\u9fff]', text))
    latin = len(re.findall(r'[A-Za-z]', text))
    total = chinese + latin
    language = 'zh' if chinese >= 500 and chinese / max(total,1) >= .35 else 'en' if latin >= 1000 and chinese / max(total,1) < .02 else 'unknown'
    return {'language':language,'basis':'完整原文文本层字符比例 v1','chinese_chars':chinese,'latin_chars':latin}


def prepare(p, source, parsed, c, s, accounting=None):
    from .daily import translate
    sha = digest(source.read_bytes()); decision = dict(detect(parsed),source_sha256=sha)
    s.record(p['id'],'fulltext_language',sha,'success',decision)
    if decision['language'] == 'unknown' and 'cnki' in p.get('sources',[]):
        from .daily_model import DailyError
        raise DailyError('知网全文语言未知，待确认；不默认跳过或启动翻译')
    if decision['language'] == 'zh':
        detail = dict(decision,path=str(source),output_sha256=sha,skipped=True,
                      reason='原始全文为中文，提供原文与中文阅读笔记；不生成重复双语PDF')
        s.record(p['id'],'translation',sha,'skipped',detail)
        return detail
    return translate(p,source,c,s,accounting=accounting)
