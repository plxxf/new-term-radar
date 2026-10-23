"""Prepare conservative SEO candidates from New Term Radar.
No search-volume/KD numbers are inferred from GitHub stars.
"""
import argparse
import json
import re
from pathlib import Path

CATEGORIES = ('文件转换','图片/视频','办公效率','开发者工具','SEO/营销','计算器','AI工具','其他')
RULES = [
 ('文件转换', r'\b(pdf|convert|converter|docx|pptx|file format|file conversion)\b'),
 ('图片/视频', r'\b(image|video|audio|photo|gif|subtitle|3d|cad|drawing|illustration)\b'),
 ('SEO/营销', r'\b(seo|keyword|marketing|analytics|social media)\b'),
 ('计算器', r'\b(calculator|unit conversion|percentage|mortgage)\b'),
 ('开发者工具', r'\b(json|developer|cli|api|sdk|debug|code|git|framework|database|rust|python)\b'),
 ('办公效率', r'\b(powerpoint|presentation|spreadsheet|notes|office|productivity|workflow)\b'),
 ('AI工具', r'\b(ai|llm|agent|inference|model|machine learning|deepseek|gpt)\b'),
]

def classify(text):
    text = (text or '').lower()
    for category, pattern in RULES:
        if re.search(pattern, text):
            return category
    return '其他'

def candidates(report, max_count=50):
    repos = [x for x in ((report.get('breakout') or {}).get('repos') or []) if isinstance(x,dict)]
    lookup = {str(x.get('repo','')).lower():x for x in repos}
    out, seen = [], set()
    def add(term, repo='', info=None, lead=None):
        term = str(term or '').strip()
        if not term or term.casefold() in seen or len(out)>=max_count:
            return
        seen.add(term.casefold())
        info = info or {}
        lead = lead or {}
        desc = str(info.get('description') or ' '.join(str(v) for v in lead.get('examples',[])[:2]) or '')
        url = 'https://github.com/'+repo if repo and '/' in repo else ''
        # GitHub velocity is NOT Google search trend; leave trend unverified.
        out.append({'关键词':term,'产品分类':classify(term+' '+desc),
                    '增长趋势':'待验证','商业潜力':'待评估','优先级':'待验证',
                    '参考网站':url,'状态':'待分析'})
    for lead in report.get('leads') or []:
        if isinstance(lead,dict):
            repo = str(lead.get('repo') or '')
            add(lead.get('term'),repo,lookup.get(repo.lower()),lead)
    for info in repos:
        repo = str(info.get('repo') or '')
        add(info.get('name') or repo.split('/')[-1],repo,info)
    return out

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--input',default='report.json')
    parser.add_argument('--output',default='seo_report.json')
    parser.add_argument('--limit',type=int,default=50)
    a=parser.parse_args()
    if not 1<=a.limit<=500: raise ValueError('limit must be 1..500')
    data=json.loads(Path(a.input).read_text(encoding='utf8'))
    rows=candidates(data,a.limit)
    Path(a.output).write_text(json.dumps({'rows':rows,'source':a.input},ensure_ascii=False,indent=2),encoding='utf8')
    print(f'Prepared {len(rows)} conservative SEO candidates in {a.output}')
    if not rows: print('No leads or breakout repos found; no rows to sync')
if __name__=='__main__': main()
