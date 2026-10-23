"""One-use authorization. Creating a grant is a human-authorized maintenance action."""
import json
from .core import atomic_write, now, digest

def consume(folder, original):
    path = folder / 'authorize-once.json'
    if not path.exists(): return False
    grant = json.loads(path.read_text('utf-8'))
    audit = folder / ('authorization-' + str(grant.get('id', 'missing')) + '.json')
    if audit.exists(): return False
    if not (grant.get('id') and grant.get('authorized_by') and grant.get('reason') and
            grant.get('duplicate_charge_risk_accepted') is True and
            grant.get('request_id') == (original.get('request_id') or 'legacy:'+digest(original)) and (original.get('request_id') or 'legacy:'+digest(original))):
        return False
    atomic_write(audit, json.dumps(dict(grant, consumed_at=now(), original=original), ensure_ascii=False))
    return True

def available(folder, original):
    path=folder/'authorize-once.json'
    if not path.exists(): return False
    grant=json.loads(path.read_text('utf-8'))
    return bool(grant.get('id') and grant.get('authorized_by') and grant.get('reason') and grant.get('duplicate_charge_risk_accepted') is True and (original.get('request_id') or 'legacy:'+digest(original)) and grant.get('request_id')==(original.get('request_id') or 'legacy:'+digest(original)) and not (folder/('authorization-'+grant['id']+'.json')).exists())
