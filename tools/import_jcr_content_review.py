"""Import a locally audited full-text category review; no model/network calls.

Usage: python -m tools.import_jcr_content_review review.json [--apply]
Preview by default. Does not move or process a paper.
"""
import argparse
import copy
import json
from pathlib import Path
from paper_obsidian.core import Store, load_config, run_lock, atomic_write, digest
from paper_obsidian.eligibility import load_whitelist, reading_classify

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('review');parser.add_argument('--apply',action='store_true');args=parser.parse_args()
    c=load_config();d=c['paths']['data'];target=c['paths']['whitelist']
    with run_lock(d):
        s=Store(d)
        try:
            review=json.loads(Path(args.review).read_text('utf-8-sig'))
            p=s.get(review['paper_id'])
            if not p:raise ValueError('未知稳定ID')
            w=load_whitelist(target);proposed=copy.deepcopy(w)
            rows=proposed.setdefault('jcr_content_reviews',[])
            if review not in rows:rows.append(review)
            q=reading_classify(p,proposed)['jcr_qualification']
            print(json.dumps(q,ensure_ascii=False,indent=2))
            if q['status']=='pending':raise ValueError('审核材料未通过验证；未写入')
            if args.apply:
                atomic_write(target,json.dumps(proposed,ensure_ascii=False,indent=2)+'\n',target.parent/'backups')
                atomic_write(d/'journal-imports'/('jcr-content-'+digest(review)+'.json'),json.dumps({'review':review,'result':q},ensure_ascii=False,indent=2))
        finally:s.db.close()

if __name__=='__main__':main()
