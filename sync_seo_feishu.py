"""Separate, safe Feishu SEO-table sync; previews unless SEO_WRITE=true.
Requires: FEISHU_APP_ID, FEISHU_APP_SECRET, FEISHU_APP_TOKEN, SEO_FEISHU_TABLE_ID.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE='https://open.feishu.cn/open-apis'
ALLOWED=('关键词','产品分类','月搜索量','SEO难度','增长趋势','商业潜力','机会评分','优先级','参考网站','状态')
# Do not overwrite an analyst's manual workflow state, nor unset metrics.
UPDATE_FIELDS=('产品分类','增长趋势','商业潜力','优先级','参考网站')

def request(method,url,token=None,payload=None):
    raw=json.dumps(payload,ensure_ascii=False).encode() if payload is not None else None
    headers={'Content-Type':'application/json; charset=utf-8'}
    if token: headers['Authorization']='Bearer '+token
    for attempt in range(3):
        req=urllib.request.Request(url,data=raw,headers=headers,method=method)
        try:
            with urllib.request.urlopen(req,timeout=30) as resp: data=json.load(resp)
            if data.get('code',0)!=0: raise RuntimeError(f"Feishu code={data.get('code')}: {data.get('msg')}")
            return data
        except urllib.error.HTTPError as e:
            if e.code in (429,500,502,503,504) and attempt<2:
                time.sleep(2**attempt);continue
            raise RuntimeError(f'HTTP {e.code}: {e.read(400).decode(errors="replace")}') from e
    raise RuntimeError('API retries exhausted')

def text_value(x):
    if isinstance(x,list):return ''.join(str(v.get('text','')) for v in x if isinstance(v,dict))
    if isinstance(x,dict):return str(x.get('text') or x.get('link') or '')
    return str(x or '')

def main():
    rows=json.loads(Path(os.getenv('SEO_REPORT_PATH','seo_report.json')).read_text(encoding='utf8')).get('rows',[])
    rows=[r for r in rows if isinstance(r,dict) and str(r.get('关键词','')).strip()]
    limit=int(os.getenv('SEO_SYNC_LIMIT','50'))
    if not 1<=limit<=500:raise ValueError('SEO_SYNC_LIMIT must be 1..500')
    rows=rows[:limit]
    write=os.getenv('SEO_WRITE','false').lower()=='true'
    required=('FEISHU_APP_ID','FEISHU_APP_SECRET','FEISHU_APP_TOKEN','SEO_FEISHU_TABLE_ID')
    missing=[k for k in required if not os.getenv(k)]
    if missing:raise ValueError('Missing Secrets/vars: '+','.join(missing))
    app_token=os.environ['FEISHU_APP_TOKEN'].strip()
    table_id=os.environ['SEO_FEISHU_TABLE_ID'].strip()
    if app_token.startswith('http') or table_id.startswith('http'):raise ValueError('Use bare app_token and table_id, not URLs')
    auth=request('POST',BASE+'/auth/v3/tenant_access_token/internal',payload={'app_id':os.environ['FEISHU_APP_ID'],'app_secret':os.environ['FEISHU_APP_SECRET']})
    token=auth['tenant_access_token']
    root=f'{BASE}/bitable/v1/apps/{urllib.parse.quote(app_token,safe="")}/tables/{urllib.parse.quote(table_id,safe="")}'
    fields_meta=(request('GET',root+'/fields?page_size=100',token).get('data') or {}).get('items',[])
    types={f['field_name']:f.get('type') for f in fields_meta}
    missing_fields=[k for k in ALLOWED if k not in types]
    if missing_fields:raise ValueError('Missing Feishu columns: '+', '.join(missing_fields))
    # Feishu field type 15 = hyperlink, 1 = text.
    if types['参考网站'] not in (1,15):raise ValueError('参考网站 must be text or hyperlink type')
    existing={};page=None
    while True:
        query={'page_size':500}
        if page:query['page_token']=page
        response=request('GET',root+'/records?'+urllib.parse.urlencode(query),token)
        data=response.get('data') or {}
        for item in data.get('items') or []:
            keyword=text_value((item.get('fields') or {}).get('关键词')).strip().casefold()
            if keyword:existing.setdefault(keyword,item)
        if not data.get('has_more'):break
        page=data.get('page_token')
        if not page:raise RuntimeError('Pagination missing page token')
    created=updated=unchanged=0
    for row in rows:
        keyword=str(row['关键词']).strip()
        key=keyword.casefold()
        fields={k:v for k,v in row.items() if k in ALLOWED and v is not None and v!=''}
        if '参考网站' in fields:
            url=fields['参考网站']
            if types['参考网站']==15:fields['参考网站']={'link':url,'text':'项目链接'}
        prior=existing.get(key)
        if prior:
            old=prior.get('fields') or {}
            changes={k:v for k,v in fields.items() if k in UPDATE_FIELDS and v!=old.get(k)}
            # Avoid overwriting manual editorial values in existing records.
            changes={k:v for k,v in changes.items() if not text_value(old.get(k))}
            if not changes:unchanged+=1;continue
            if write:
                rid=prior.get('record_id') or prior.get('id')
                request('PUT',root+'/records/'+urllib.parse.quote(rid,safe=''),token,{'fields':changes})
            print(('UPDATE ' if write else 'PREVIEW UPDATE ')+keyword+': '+','.join(changes))
            updated+=1
        else:
            if write:
                result=request('POST',root+'/records',token,{'fields':fields})
                existing[key]=(result.get('data') or {}).get('record') or {'fields':fields}
            else:existing[key]={'fields':fields}
            print(('CREATE ' if write else 'PREVIEW CREATE ')+keyword)
            created+=1
        if write:time.sleep(0.25)
    print(f'FINISHED mode={"WRITE" if write else "PREVIEW"} rows={len(rows)} create={created} update={updated} unchanged={unchanged}')
if __name__=='__main__':
    try:main()
    except Exception as exc:print('ERROR:',exc,file=sys.stderr);sys.exit(1)
