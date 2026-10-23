"""Optional real SEO metrics enrichment; charges DataForSEO only if --live used."""
import argparse, base64, json, math, os, urllib.request
from pathlib import Path

URL='https://api.dataforseo.com/v3/dataforseo_labs/google/keyword_overview/live'
def classify_trend(history):
    h=sorted([x for x in history if isinstance(x,dict) and isinstance(x.get('search_volume'),(int,float))],key=lambda x:(x.get('year',0),x.get('month',0)),reverse=True)
    if len(h)<6:return '待验证'
    a=sum(x['search_volume'] for x in h[:3])/3
    b=sum(x['search_volume'] for x in h[3:6])/3
    if b<=0:return '待验证'
    ratio=a/b
    return '快速增长' if ratio>=1.4 else '稳定增长' if ratio>=1.1 else '持续下降' if ratio<0.9 else '保持稳定'

def score(volume,kd,cpc):
    if volume is None or kd is None:return None
    demand=min(100,math.log10(volume+1)/5*100) if volume>=0 else 0
    competitive=100-kd
    # CPC is a proxy, not revenue prediction. Clamp at 5 USD.
    business=min(100,max(0,cpc or 0)/5*100)
    return round(max(0,min(100,0.40*demand+0.45*competitive+0.15*business)))

def priority(s):
    if s is None:return '待验证'
    return 'P0 - 优先开发' if s>=85 else 'P1 - 值得研究' if s>=70 else 'P2 - 持续观察' if s>=50 else 'P3 - 暂不考虑'

def fetch(keywords,location,language):
    login=os.environ['DATAFORSEO_LOGIN'];password=os.environ['DATAFORSEO_PASSWORD']
    auth=base64.b64encode((login+':'+password).encode()).decode()
    body=json.dumps([{'keywords':keywords,'location_code':location,'language_code':language,'include_serp_info':False}]).encode()
    request=urllib.request.Request(URL,data=body,headers={'Authorization':'Basic '+auth,'Content-Type':'application/json'},method='POST')
    with urllib.request.urlopen(request,timeout=90) as response:data=json.load(response)
    if data.get('status_code')!=20000:raise RuntimeError('DataForSEO failure: '+str(data.get('status_message')))
    tasks=data.get('tasks') or []
    if any(t.get('status_code')!=20000 for t in tasks):raise RuntimeError('DataForSEO task failed: '+str([(t.get('status_code'),t.get('status_message')) for t in tasks]))
    items=[]
    for task in tasks:
        for result in task.get('result') or []:items.extend(result.get('items') or [])
    return {str(x.get('keyword','')).strip().casefold():x for x in items},sum(float(t.get('cost') or 0) for t in tasks)

def main():
    p=argparse.ArgumentParser();p.add_argument('--input',default='seo_report.json');p.add_argument('--output',default='seo_report.json');p.add_argument('--limit',type=int,default=20);p.add_argument('--location',type=int,default=2840);p.add_argument('--language',default='en');p.add_argument('--live',action='store_true');a=p.parse_args()
    if not 1<=a.limit<=50:raise ValueError('limit must be 1..50 (cost guard)')
    path=Path(a.input);report=json.loads(path.read_text(encoding='utf-8'));rows=report.get('rows',[])
    keys=list(dict.fromkeys(str(r.get('关键词','')).strip().casefold() for r in rows if r.get('关键词')))[:a.limit]
    if not a.live:
        print(f'NO API CHARGE: preview only. Would check {len(keys)} keywords in market {a.location}/{a.language}')
        for k in keys:print('CANDIDATE '+k)
        return
    if not os.getenv('DATAFORSEO_LOGIN') or not os.getenv('DATAFORSEO_PASSWORD'):raise RuntimeError('Missing DATAFORSEO_LOGIN or DATAFORSEO_PASSWORD')
    if not keys:print('No keywords, no API call');return
    # Single paid request, capped to 20 by default.
    metrics,cost=fetch(keys,a.location,a.language)
    matched=0
    for row in rows:
        item=metrics.get(str(row.get('关键词','')).strip().casefold())
        if not item:continue
        info=item.get('keyword_info') or {};props=item.get('keyword_properties') or {}
        vol=info.get('search_volume');kd=props.get('keyword_difficulty');cpc=info.get('cpc')
        if isinstance(vol,(int,float)) and vol>=0:row['月搜索量']=int(vol)
        if isinstance(kd,(int,float)) and 0<=kd<=100:row['SEO难度']=int(kd)
        if isinstance(cpc,(int,float)) and cpc>=0:row['商业潜力']='高' if cpc>=2 else '中' if cpc>=0.5 else '低'
        row['增长趋势']=classify_trend(info.get('monthly_searches') or [])
        sc=score(row.get('月搜索量'),row.get('SEO难度'),cpc)
        if sc is not None:row['机会评分']=sc;row['优先级']=priority(sc)
        matched+=1
    report['seo_source']='DataForSEO Google keyword overview (estimated provider metrics)'
    report['market']={'location_code':a.location,'language_code':a.language}
    Path(a.output).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(f'API charged approx ${cost:.4f}; matched {matched}/{len(keys)} candidate keywords; saved {a.output}')

if __name__=='__main__':main()
