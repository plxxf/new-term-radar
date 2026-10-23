#!/usr/bin/env python3
"""Standalone GitHub Star tracker for new-term-radar; stdlib only."""
import datetime as dt
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DB = Path(os.getenv('TREND_DB_PATH', 'state/trends.sqlite'))
REPORT = Path(os.getenv('RADAR_REPORT_PATH', 'report.json'))
LIMIT = max(1, min(int(os.getenv('TREND_MAX_REPOS', '50')), 200))
API = 'https://open.feishu.cn/open-apis'
REPO_RE = re.compile(r'^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$')
UTC = dt.timezone.utc


def request_json(method, url, token=None, body=None, retries=3):
    raw = json.dumps(body, ensure_ascii=False).encode('utf-8') if body is not None else None
    headers = {'Accept': 'application/json', 'User-Agent': 'new-term-radar-trend/1.2'}
    if body is not None:
        headers['Content-Type'] = 'application/json; charset=utf-8'
    if token:
        headers['Authorization'] = f'Bearer {token}'
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=raw, headers=headers, method=method), timeout=25) as r:
                return json.load(r)
        except urllib.error.HTTPError as exc:
            detail = exc.read(400).decode('utf-8', 'replace')
            if exc.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(f'HTTP {exc.code} at {url.split("?")[0]}: {detail}') from exc
        except (TimeoutError, urllib.error.URLError) as exc:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(f'Connection failed: {exc}') from exc


def feishu(method, path, token, body=None):
    data = request_json(method, API + path, token, body)
    if data.get('code') != 0:
        raise RuntimeError(f"Feishu API code={data.get('code')} msg={data.get('msg')}")
    return data.get('data') or {}


def init_db(con):
    con.execute('CREATE TABLE IF NOT EXISTS projects (repo TEXT PRIMARY KEY, name TEXT NOT NULL, last_seen TEXT NOT NULL)')
    con.execute('CREATE TABLE IF NOT EXISTS snapshots (repo TEXT NOT NULL, day TEXT NOT NULL, stars INTEGER NOT NULL, measured_at TEXT NOT NULL, PRIMARY KEY(repo,day))')
    con.execute('CREATE INDEX IF NOT EXISTS idx_snapshots_day ON snapshots(day)')
    con.commit()


def get_candidates():
    if not REPORT.is_file():
        print(f'No {REPORT}; processing tracked repositories only')
        return []
    data = json.loads(REPORT.read_text(encoding='utf-8'))
    seen = {}
    for x in data.get('leads') or []:
        if isinstance(x, dict):
            repo = x.get('repo') or ''
            if REPO_RE.fullmatch(repo):
                seen[repo.casefold()] = (repo, x.get('term') or repo.split('/')[-1])
    repos = (data.get('breakout') or {}).get('repos') or []
    for x in repos:
        if isinstance(x, dict):
            repo = x.get('repo') or ''
            if REPO_RE.fullmatch(repo):
                seen[repo.casefold()] = (repo, x.get('name') or repo.split('/')[-1])
    return list(seen.values())


def baseline(con, repo, today, days):
    """Only compare with a snapshot no newer than the target day (no interpolated observations)."""
    target = dt.date.fromisoformat(today) - dt.timedelta(days=days)
    start = (target - dt.timedelta(days=2)).isoformat()
    end = target.isoformat()
    return con.execute('SELECT stars, day FROM snapshots WHERE repo=? AND day BETWEEN ? AND ? ORDER BY day DESC LIMIT 1', (repo, start, end)).fetchone()


def growth(con, repo, today, stars, days):
    point = baseline(con, repo, today, days)
    if not point:
        return None, None
    previous, day = point
    elapsed = (dt.date.fromisoformat(today) - dt.date.fromisoformat(day)).days
    if not elapsed:
        return None, None
    return stars - previous, elapsed


def feishu_existing(table_path, token):
    items = {}
    page = None
    while True:
        params = {'page_size': 500}
        if page:
            params['page_token'] = page
        data = feishu('GET', table_path + '/records?' + urllib.parse.urlencode(params), token)
        for x in data.get('items') or []:
            value = (x.get('fields') or {}).get('GitHub 仓库') or ''
            if isinstance(value, list):
                value = ''.join(k.get('text', '') for k in value if isinstance(k, dict))
            if isinstance(value, str) and value:
                items[value.casefold()] = x
        if not data.get('has_more'):
            break
        page = data.get('page_token')
        if not page:
            raise RuntimeError('Feishu pagination token missing')
    return items


def sync_trends(rows):
    table = os.getenv('FEISHU_TREND_TABLE_ID', '').strip()
    if not table:
        print('No FEISHU_TREND_TABLE_ID; skip Feishu trend table (history is saved).')
        return
    needed = ['FEISHU_APP_ID', 'FEISHU_APP_SECRET', 'FEISHU_APP_TOKEN']
    values = [os.getenv(k, '').strip() for k in needed]
    if not all(values):
        print('Missing Feishu credentials; trend table not synced.')
        return
    tenant_data = request_json('POST', API + '/auth/v3/tenant_access_token/internal', body={'app_id': values[0], 'app_secret': values[1]})
    if tenant_data.get('code') != 0:
        raise RuntimeError(f"Feishu auth failed: {tenant_data.get('msg')}")
    token = tenant_data['tenant_access_token']
    path = '/bitable/v1/apps/' + urllib.parse.quote(values[2], safe='') + '/tables/' + urllib.parse.quote(table, safe='')
    existing = feishu_existing(path, token)
    write = os.getenv('TREND_FEISHU_WRITE', 'false').lower() == 'true'
    print(f'Trend Feishu mode={"WRITE" if write else "PREVIEW"}, existing={len(existing)}, candidates={len(rows)}')
    created = updated = failed = 0
    for r in rows:
        fields = {
            'GitHub 仓库': r['repo'],
            '项目名称': r['name'],
            '当前 Stars': r['stars'],
            '最后采样日期': r['day'],
            '项目链接': {'text': r['repo'], 'link': 'https://github.com/' + r['repo']},
        }
        for key, value in (('24小时增长', r['d1']), ('7天增长', r['d7']), ('30天增长', r['d30']), ('7天日均增长', r['daily7'])):
            if value is not None:
                fields[key] = value
        item = existing.get(r['repo'].casefold())
        if not write:
            print(f'PREVIEW {"UPDATE" if item else "CREATE"}: {r["repo"]}')
            continue
        try:
            if item:
                rid = item.get('record_id') or item.get('id')
                feishu('PUT', path + '/records/' + urllib.parse.quote(rid, safe=''), token, {'fields': fields})
                updated += 1
            else:
                result = feishu('POST', path + '/records', token, {'fields': fields})
                rid = (result.get('record') or {}).get('record_id')
                if rid:
                    existing[r['repo'].casefold()] = {'record_id': rid}
                created += 1
            time.sleep(0.25)
        except Exception as exc:
            failed += 1
            print(f'ERROR syncing {r["repo"]}: {exc}', file=sys.stderr)
    print(f'Trend Feishu results: created={created}, updated={updated}, failed={failed}')
    if failed:
        raise RuntimeError(f'{failed} trend rows failed; original Feishu sync was not affected')


def main():
    DB.parent.mkdir(parents=True, exist_ok=True)
    now = dt.datetime.now(UTC)
    today = now.date().isoformat()
    with sqlite3.connect(DB) as con:
        init_db(con)
        incoming = get_candidates()
        for repo, name in incoming:
            con.execute('INSERT INTO projects(repo,name,last_seen) VALUES(?,?,?) ON CONFLICT(repo) DO UPDATE SET name=excluded.name,last_seen=excluded.last_seen', (repo, str(name), today))
        con.commit()
        tracked = con.execute('SELECT repo,name FROM projects ORDER BY last_seen DESC,repo ASC LIMIT ?', (LIMIT,)).fetchall()
        github_token = os.getenv('GITHUB_TOKEN', '')
        rows, errors = [], 0
        for repo, name in tracked:
            try:
                api_repo = urllib.parse.quote(repo, safe='/')
                info = request_json('GET', 'https://api.github.com/repos/' + api_repo, github_token or None)
                stars = info.get('stargazers_count')
                if not isinstance(stars, int) or info.get('message'):
                    raise ValueError('Missing numeric stargazers_count')
                canonical = info.get('full_name') or repo
                con.execute('INSERT INTO snapshots(repo,day,stars,measured_at) VALUES(?,?,?,?) ON CONFLICT(repo,day) DO UPDATE SET stars=excluded.stars,measured_at=excluded.measured_at', (repo, today, stars, now.isoformat()))
                d1, age1 = growth(con, repo, today, stars, 1)
                d7, age7 = growth(con, repo, today, stars, 7)
                d30, age30 = growth(con, repo, today, stars, 30)
                daily7 = round(d7 / age7, 2) if d7 is not None else None
                rows.append({'repo': repo, 'name': name, 'stars': stars, 'day': today, 'd1': d1 if age1 == 1 else None, 'd7': d7, 'd30': d30, 'daily7': daily7})
                print(f'OK {canonical}: stars={stars}, +24h={d1 if age1 == 1 else "N/A"}, +7d={d7}, +30d={d30}')
            except Exception as exc:
                errors += 1
                print(f'WARN {repo}: {exc}', file=sys.stderr)
        con.commit()
    summary = Path(os.getenv('TREND_SUMMARY_PATH', 'trend_summary.json'))
    summary.write_text(json.dumps({'updated_at': now.isoformat(), 'projects': rows, 'fetch_errors': errors}, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'Saved {len(rows)} rows to {summary}, history DB {DB}; fetch_errors={errors}')
    sync_trends(rows)


if __name__ == '__main__':
    main()
