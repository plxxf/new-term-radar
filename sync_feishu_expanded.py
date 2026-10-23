"""New Term Radar expanded Feishu sync: leads + breakout.repos, safe upsert.

Requires existing Feishu fields: 关键词(text primary), 关键词类型(single select),
处理状态(single select), GitHub 仓库(text), 项目链接(text), 项目简介(text),
GitHub Stars(number), Stars 日均增长(number).

Env: FEISHU_APP_ID, FEISHU_APP_SECRET, FEISHU_APP_TOKEN, FEISHU_TABLE_ID;
FEISHU_WRITE=false (default), FEISHU_SYNC_LIMIT=50,
FEISHU_UPDATE_EXISTING=true, RADAR_REPORT_PATH=report.json.
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
META_FIELDS = ('GitHub 仓库', '项目链接', '项目简介', 'GitHub Stars', 'Stars 日均增长')


def request(method, url, token=None, payload=None):
    raw = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
    headers = {'Content-Type': 'application/json; charset=utf-8'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    for attempt in range(3):
        req = urllib.request.Request(url, data=raw, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                obj = json.load(response)
            if obj.get('code', 0) != 0:
                raise RuntimeError(f"Feishu API code={obj.get('code')} msg={obj.get('msg')}")
            return obj
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(f'Feishu HTTP {exc.code}: {exc.read(700).decode("utf-8", "replace")}') from exc
    raise RuntimeError('Request retries exhausted')


def norm(value):
    return str(value or '').strip().casefold()


def as_text(value):
    if isinstance(value, list):
        return ''.join(str(part.get('text', '')) for part in value if isinstance(part, dict)).strip()
    if isinstance(value, dict):
        return str(value.get('text') or value.get('link') or '').strip()
    return str(value or '').strip()


def metadata(repo, info):
    url = 'https://github.com/' + urllib.parse.quote(repo, safe='/')
    fields = {
        'GitHub 仓库': repo,
        '项目链接': {
            'text': repo,
            'link': url
        }
    }
    description = str(info.get('description') or '').strip()
    if description:
        fields['项目简介'] = description
    for src, dst in (('stars', 'GitHub Stars'), ('velocity', 'Stars 日均增长')):
        val = info.get(src)
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            fields[dst] = val
    return fields


def choose_candidates(report, limit):
    """Lead priority, then extra breakout repos; dedupe by repo OR keyword."""
    repos = [r for r in ((report.get('breakout') or {}).get('repos') or []) if isinstance(r, dict)]
    lookup = {norm(r.get('repo')): r for r in repos if norm(r.get('repo'))}
    candidates, seen_repos, seen_terms = [], set(), set()
    lead_count = repo_extra = 0

    def add(term, repo, info, kind):
        nonlocal lead_count, repo_extra
        term, repo = str(term or '').strip(), str(repo or '').strip()
        if not term or not norm(term) or (repo and '/' not in repo):
            return
        if norm(term) in seen_terms or (repo and norm(repo) in seen_repos):
            return
        fields = {'关键词': term, '关键词类型': '品牌词' if repo else '通用词', '处理状态': '待分析'}
        if repo:
            fields.update(metadata(repo, info))
            seen_repos.add(norm(repo))
        seen_terms.add(norm(term))
        candidates.append(fields)
        if kind == 'lead':
            lead_count += 1
        else:
            repo_extra += 1

    for lead in report.get('leads') or []:
        if not isinstance(lead, dict):
            continue
        repo = str(lead.get('repo') or '').strip()
        add(lead.get('term'), repo, lookup.get(norm(repo), {}), 'lead')
        if len(candidates) >= limit:
            return candidates, lead_count, repo_extra

    for repo in repos:
        repo_id = str(repo.get('repo') or '').strip()
        # Prefer repository name, preserving the official spelling; do not invent an SEO keyword.
        term = str(repo.get('name') or (repo_id.rsplit('/', 1)[-1] if '/' in repo_id else '')).strip()
        add(term, repo_id, repo, 'repo')
        if len(candidates) >= limit:
            break
    return candidates, lead_count, repo_extra


def existing_records(endpoint, token):
    by_term, by_repo = {}, {}
    page_token = None
    while True:
        params = {'page_size': 500}
        if page_token:
            params['page_token'] = page_token
        data = (request('GET', endpoint + '?' + urllib.parse.urlencode(params), token).get('data') or {})
        for record in data.get('items') or []:
            fields = record.get('fields') or {}
            term, repo = norm(as_text(fields.get('关键词'))), norm(as_text(fields.get('GitHub 仓库')))
            if term:
                by_term.setdefault(term, record)
            if repo:
                by_repo.setdefault(repo, record)
        if not data.get('has_more'):
            return by_term, by_repo
        page_token = data.get('page_token')
        if not page_token:
            raise RuntimeError('Missing page_token despite has_more=true')

def same(a, b):
    if isinstance(b, dict):
        if isinstance(a, dict):
            return (
                as_text(a) == as_text(b)
                and str(a.get('link') or '') == str(b.get('link') or '')
            )
        return as_text(a) == str(b.get('link') or '')

    if isinstance(b, (int, float)) and not isinstance(b, bool):
        try:
            return float(a) == float(b)
        except (ValueError, TypeError):
            return False

    return as_text(a) == str(b)

def main():
    path = Path(os.getenv('RADAR_REPORT_PATH', 'report.json'))
    if not path.is_file():
        print(f'No report found: {path}; no sync performed.', flush=True)
        return
    report = json.loads(path.read_text(encoding='utf-8'))
    limit = int(os.getenv('FEISHU_SYNC_LIMIT', '50'))
    if not 1 <= limit <= 500:
        raise ValueError('FEISHU_SYNC_LIMIT must be between 1 and 500')
    candidates, lead_count, extra_count = choose_candidates(report, limit)
    print(f'Report: leads={len(report.get("leads") or [])}; breakout.repos={len((report.get("breakout") or {}).get("repos") or [])}; '
          f'candidates={len(candidates)} (from_leads={lead_count}, extra_repos={extra_count}); limit={limit}', flush=True)
    if not candidates:
        print('No candidates in report; no sync performed.', flush=True)
        return

    creds = {key: os.getenv(key, '').strip() for key in
             ('FEISHU_APP_ID', 'FEISHU_APP_SECRET', 'FEISHU_APP_TOKEN', 'FEISHU_TABLE_ID')}
    for key, value in creds.items():
        if not value:
            raise ValueError(f'Missing secret {key}')
    if any(creds[k].startswith(('https://', 'http://')) for k in ('FEISHU_APP_TOKEN', 'FEISHU_TABLE_ID')):
        raise ValueError('Use bare app_token and table_id, not URLs')

    write = os.getenv('FEISHU_WRITE', 'false').strip().lower() == 'true'
    update_existing = os.getenv('FEISHU_UPDATE_EXISTING', 'true').strip().lower() == 'true'
    print('STEP 1: Requesting Feishu tenant token', flush=True)
    token = request('POST', BASE + '/auth/v3/tenant_access_token/internal',
                    payload={'app_id': creds['FEISHU_APP_ID'], 'app_secret': creds['FEISHU_APP_SECRET']}).get('tenant_access_token')
    if not token:
        raise RuntimeError('No tenant_access_token in response')
    print('STEP 1 OK: Token obtained', flush=True)
    endpoint = (BASE + '/bitable/v1/apps/' + urllib.parse.quote(creds['FEISHU_APP_TOKEN'], safe='') +
                '/tables/' + urllib.parse.quote(creds['FEISHU_TABLE_ID'], safe='') + '/records')
    print('STEP 2: Listing existing Feishu records', flush=True)
    by_term, by_repo = existing_records(endpoint, token)
    print(f'STEP 2 OK: Read {len(by_term)} keywords and {len(by_repo)} repo identifiers', flush=True)

    created = updated = unchanged = 0
    for fields in candidates:
        term, repo = fields['关键词'], fields.get('GitHub 仓库', '')
        old = (by_repo.get(norm(repo)) if repo else None) or by_term.get(norm(term))
        if old:
            if not update_existing:
                print(f'SKIP EXISTING: {term}', flush=True)
                unchanged += 1
                continue
            old_fields = old.get('fields') or {}
            changes = {k: v for k, v in fields.items() if k in META_FIELDS and not same(old_fields.get(k), v)}
            if not changes:
                print(f'UNCHANGED: {term}', flush=True)
                unchanged += 1
                continue
            if write:
                rid = old.get('record_id') or old.get('id')
                if not rid:
                    raise RuntimeError(f'Missing record_id for {term}')
                request('PUT', endpoint + '/' + urllib.parse.quote(rid, safe=''), token, {'fields': changes})
                print(f'UPDATED: {term} | {", ".join(changes)}', flush=True)
                time.sleep(0.25)
            else:
                print(f'PREVIEW UPDATE: {term} | {", ".join(changes)}', flush=True)
            updated += 1
        else:
            if write:
                result = request('POST', endpoint, token, {'fields': fields})
                print(f'CREATED: {term} | record_id={((result.get("data") or {}).get("record") or {}).get("record_id", "")}', flush=True)
                time.sleep(0.25)
            else:
                print(f'PREVIEW CREATE: {term} | {repo}', flush=True)
            created += 1
            # Prevent duplicates within the same batch, even if a source duplicates names.
            placeholder = {'fields': fields}
            by_term[norm(term)] = placeholder
            if repo:
                by_repo[norm(repo)] = placeholder
    print(f'Completed. mode={"WRITE" if write else "PREVIEW"}; candidates={len(candidates)}; '
          f'create={created}; update={updated}; unchanged={unchanged}', flush=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(f'ERROR: {exc}', file=sys.stderr, flush=True)
        sys.exit(1)
