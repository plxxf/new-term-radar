"""Import New Term Radar leads into a Feishu Bitable, avoiding duplicates.

Required secrets: FEISHU_APP_ID, FEISHU_APP_SECRET, FEISHU_APP_TOKEN, FEISHU_TABLE_ID.
Default is a safe preview: set FEISHU_WRITE=true to create rows.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = 'https://open.feishu.cn/open-apis'

def request(method, url, token=None, payload=None):
    data = json.dumps(payload, ensure_ascii=False).encode('utf-8') if payload is not None else None
    headers = {'Content-Type': 'application/json; charset=utf-8'}
    if token:
        headers['Authorization'] = f'Bearer {token}'
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                obj = json.load(resp)
            if obj.get('code', 0) != 0:
                raise RuntimeError(f"Feishu API error code={obj.get('code')} msg={obj.get('msg')}")
            return obj
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(f'Feishu HTTP {e.code}: {e.read(500).decode("utf-8", "replace")}') from e
    raise RuntimeError('Request failed')

def normalize(v):
    return str(v or '').strip().casefold()

def main():
    report = Path(os.getenv('RADAR_REPORT_PATH', 'report.json'))
    if not report.exists():
        print(f'No report found: {report}. Nothing to sync.')
        return
    leads = json.loads(report.read_text(encoding='utf-8')).get('leads', [])
    if not leads:
        print('No leads in report.')
        return
    write = os.getenv('FEISHU_WRITE', 'false').lower() == 'true'
    limit = int(os.getenv('FEISHU_SYNC_LIMIT', '3'))
    if limit < 1:
        raise ValueError('FEISHU_SYNC_LIMIT must be >= 1')
    app_id, secret, app_token, table_id = (os.environ[k] for k in ('FEISHU_APP_ID','FEISHU_APP_SECRET','FEISHU_APP_TOKEN','FEISHU_TABLE_ID'))
    token_obj = request('POST', f'{BASE}/auth/v3/tenant_access_token/internal', payload={'app_id':app_id,'app_secret':secret})
    token = token_obj.get('tenant_access_token')
    if not token:
        raise RuntimeError('No tenant_access_token returned')
    endpoint = f'{BASE}/bitable/v1/apps/{urllib.parse.quote(app_token)}/tables/{urllib.parse.quote(table_id)}/records'
    existing = set()
    page_token = ''
    while True:
        qs = {'page_size':500}
        if page_token:
            qs['page_token'] = page_token
        result = request('GET', f'{endpoint}?{urllib.parse.urlencode(qs)}', token)
        page = result.get('data') or {}
        for row in page.get('items') or []:
            existing.add(normalize((row.get('fields') or {}).get('关键词')))
        if not page.get('has_more'):
            break
        page_token = page.get('page_token')
        if not page_token:
            raise RuntimeError('Pagination indicated more records but no page_token')
    count = 0
    for lead in leads:
        term = str(lead.get('term') or '').strip()
        key = normalize(term)
        if not key or key in existing:
            continue
        repo = lead.get('repo') or ''
        # Only confirmed existing field names are written. Don't invent SEO metrics.
        fields = {'关键词':term, '关键词类型':'品牌词' if repo else '通用词', '处理状态':'待分析'}
        if write:
            result = request('POST', endpoint, token, {'fields':fields})
            record = ((result.get('data') or {}).get('record') or {})
            print(f'CREATED: {term} ({record.get("record_id", record.get("id", "ok"))})')
            time.sleep(.25)
        else:
            print(f'PREVIEW: {term} | repo={repo}')
        existing.add(key)
        count += 1
        if count >= limit:
            break
    print(f'Completed. mode={"WRITE" if write else "PREVIEW"}; candidates={len(leads)}; processed={count}; existing={len(existing)-count}')

if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        sys.exit(1)
