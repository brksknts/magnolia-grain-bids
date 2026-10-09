"""Collect publicly displayed grain cash bids with Chromium, including framed tables.

Publish valid rows only. If a site blocks browser automation or supplies no
readable price table, save its screenshot and diagnostics rather than inventing data.
"""
from __future__ import annotations
import argparse
import csv
import html
import json
import os
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from playwright.sync_api import sync_playwright

BASE=Path(__file__).resolve().parent
OUT=BASE/'docs'; OUT.mkdir(exist_ok=True)
CAPTURES=OUT/'captures'; CAPTURES.mkdir(exist_ok=True)
CT=ZoneInfo('America/Chicago')
SOURCES=[
 ('CHS Brandon','chs-brandon','https://www.chsbrandon.com/grain/cash-bids/'),
 ('New Vision','new-vision','https://newvision.coop/current-grain-prices/?format=table&groupby=location&setLocation=20359&commodity='),
 ('Chandler Feed','chandler-feed','https://www.chandlerfeed.com/cash-bids'),
 ('CFE','cfe','https://farmbucks.com/grain-prices/cfe'),
 ('POET','poet','https://farmbucks.com/grain-prices/poet'),
 ('New Vision (Farmbucks)','farmbucks-newvision','https://farmbucks.com/grain-prices/new-vision-coop'),
]

# Use actual page DOM. Works with <table> and ARIA table/grid widgets.
EXTRACT_JS=r'''() => {
 const clean=x=>(x||'').replace(/\s+/g,' ').trim();
 const known=['Magnolia','Luverne','Pipestone','Chandler','Edgerton','Woodstock','Leota',
 'Hardwick','Worthington','Ellsworth','Hills','Brewster','Rock Rapids','Bigelow',
 'Rushmore','Ruthton','Sheldon','Bingham Lake','Heron Lake','Brandon','Ashton','Chancellor',
 'Reading','Lismore','Jasper','Tracy','Marshall','Windom'];
 function preceding(el,limit=10000){
   try {const r=document.createRange(); r.setStart(document.body,0);r.setEndBefore(el);
    return r.toString().slice(-limit)}catch(e){return ''}
 }
 function context(el){
   const prev=preceding(el), tail=prev.slice(-2500);
   const hits=known.map(n=>({n,p:prev.toLowerCase().lastIndexOf(n.toLowerCase())})).sort((a,b)=>b.p-a.p);
   const location=hits[0]&&hits[0].p>=0?hits[0].n:'';
   const cm=[['soybeans','Soybeans'],['yellow corn','Corn'],['corn','Corn']]
     .map(([t,n])=>({n,p:tail.toLowerCase().lastIndexOf(t)})).sort((a,b)=>b.p-a.p);
   const commodity=cm[0]&&cm[0].p>=0?cm[0].n:'';
   return {location,commodity,previous:tail.slice(-400)};
 }
 function cells(row){
   const td=Array.from(row.querySelectorAll(':scope > th, :scope > td, :scope > [role="cell"], :scope > [role="columnheader"]'));
   if (td.length) return td.map(e=>clean(e.innerText || e.textContent));
   return [];
 }
 const tables=[];
 for(const el of document.querySelectorAll('table,[role="table"],[role="grid"]')){
   if(el.closest('table,[role="table"],[role="grid"]')!==el)continue;
   const rowEls=Array.from(el.querySelectorAll('tr,[role="row"]'));
   const rows=rowEls.map(cells).filter(r=>r.length>1);
   let headers=Array.from(el.querySelectorAll('thead th')).map(e=>clean(e.innerText));
   if(!headers.length){const h=el.querySelector('[role="rowgroup"] [role="columnheader"]');
     if(h)headers=Array.from(h.closest('[role="row"]').querySelectorAll('[role="columnheader"]')).map(e=>clean(e.innerText))}
   tables.push({kind:'table',...context(el),headers,rows, snippet:clean(el.innerText).slice(0,1500)});
 }
 return {title:document.title, url:location.href, tables, text:(document.body?.innerText||'').slice(0,130000),
    heading:Array.from(document.querySelectorAll('h1,h2,h3')).map(e=>clean(e.innerText)).slice(0,30)};
}'''

MONTH_RE=re.compile(r'\b(?:nc|new crop|spot|fall|jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b',re.I)

def _num(s):
 if s is None:return None
 s=str(s).replace('$','').replace(',','').replace('−','-').replace('¢','').strip()
 try: return float(s)
 except ValueError: return None

def basis_cents(s):
 v=_num(s)
 if v is None:return None
 return round(v * 100 if abs(v)<=2 else v,2)

def header_index(headers,*terms):
 for i,h in enumerate(headers):
  h=re.sub(r'\s+',' ',h.lower()).strip()
  if any(t in h for t in terms):return i
 return -1

def norm_commodity(s):
 s=(s or '').lower()
 if 'soy' in s or 'bean' in s:return 'Soybeans'
 if 'corn' in s:return 'Corn'
 return ''

def rows_from_table(t, source,url,captured,frame_url):
 rows=t.get('rows',[])
 if not rows:return []
 header=t.get('headers') or []
 # CHS/Chandler often use header cells in the first body row; New Vision uses <th> too.
 candidate=[(i,row) for i,row in enumerate(rows[:7]) if any(re.search(r'\b(bid|cash price|cash bid)\b',c,re.I) for c in row)]
 if not candidate:return []
 start,header=(candidate[0] if len(candidate[0][1])>=len(header) else (-1,header))
 header=[re.sub(r'\s+',' ',v.lower()).strip() for v in header]
 cash_i=header_index(header,'cash price','cash bid','bid (','bid')
 # Some published cash-bid tables label the column 'Price'. Exclude tables with only futures price.
 if cash_i<0 and 'futures' not in ' '.join(header):cash_i=header_index(header,'price')
 if cash_i<0:return []
 basis_i=header_index(header,'basis')
 del_i=header_index(header,'delivery','notes','period')
 commodity_i=header_index(header,'commodity','grain','name')
 location_i=header_index(header,'location','elevator','facility')
 futures_i=header_index(header,'futures month','basis month','contract')
 bids=[]
 for row in rows[start+1:]:
  if cash_i>=len(row):continue
  cash=_num(row[cash_i])
  if cash is None or not (1<cash<30):continue
  commodity=norm_commodity(row[commodity_i]) if 0<=commodity_i<len(row) else ''
  commodity=commodity or norm_commodity(t.get('commodity'))
  # Commodity is essential: a numerical price alone could be a futures quote.
  if not commodity:continue
  delivery=row[del_i] if 0<=del_i<len(row) else ''
  # Skip website navigation, article/listing tables, and irrelevant summary prices.
  if not MONTH_RE.search(delivery):continue
  basis=basis_cents(row[basis_i]) if 0<=basis_i<len(row) else None
  location=row[location_i] if 0<=location_i<len(row) else t.get('location','')
  if not location and source=='Chandler Feed':location='Chandler'
  bids.append({'source':source,'location':location,'commodity':commodity,'delivery':delivery,
    'cash_usd_per_bu':cash,'basis_cents_per_bu':basis,
    'futures_month':row[futures_i] if 0<=futures_i<len(row) else '',
    'captured_at_ct':captured,'url':url,'frame_url':frame_url,
    'basis_status':'posted' if basis is not None else 'not posted'})
 return bids

def extract_line_based(text,source,url,captured,frame_url):
 """Conservative fallback for widget divs, if the rendered text is row-by-row.
 CHS has <location>, <commodity>, <column names>, <month> <bid> <basis> ...
 Only match complete, recognizable price lines. Any ambiguous lines are ignored.
 """
 if source not in ('CHS Brandon','New Vision'):return []
 lines=[re.sub(r'\s+',' ',x).strip() for x in text.splitlines() if x.strip()]
 locations=['Magnolia','Luverne','Pipestone','Ruthton','Tracy','Marshall','Worthington','Hills','Ellsworth','Brewster','Reading']
 loc=''; commodity=''; out=[]
 # Examples: Oct 2026 12.45 -0.51 12.96 0.085 ZSX26
 pat=re.compile(r'^(?P<month>Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{2,4}\s+(?P<bid>\$?\d{1,2}\.\d{2,4})\s+(?P<basis>-\d+\.\d{1,4})\b',re.I)
 for i,s in enumerate(lines):
  if s.lower() in [l.lower() for l in locations]:loc=s
  if s.lower() in ('soybeans','yellow corn','corn'):commodity=norm_commodity(s)
  m=pat.match(s)
  if not m or not commodity or not loc:continue
  price=_num(m['bid']); bas=basis_cents(m['basis'])
  if price is None or bas is None or not(1<price<30):continue
  out.append({'source':source,'location':loc,'commodity':commodity,'delivery':m['month']+' '+s.split(' ')[1],
     'cash_usd_per_bu':price,'basis_cents_per_bu':bas,'futures_month':'',
     'captured_at_ct':captured,'url':url,'frame_url':frame_url,'basis_status':'posted'})
 return out

def safe_shot(page,slug):
 p=CAPTURES/(slug+'.png')
 try:page.screenshot(path=str(p),full_page=True,timeout=20000,animations='disabled')
 except Exception:
  try:page.screenshot(path=str(p),full_page=False,timeout=15000)
  except Exception:pass

def run_source(browser,source,slug,url,captured):
 context=browser.new_context(viewport={'width':1600,'height':1000},device_scale_factor=1,
     extra_http_headers={'Cache-Control':'no-cache','Pragma':'no-cache'})
 page=context.new_page()
 data={'source':source,'url':url,'status':'unavailable','tables':[], 'bids':[], 'error':'', 'frames':[]}
 try:
  response=page.goto(url,wait_until='domcontentloaded',timeout=45000)
  page.wait_for_timeout(9000)
  data['status']=str(response.status) if response else 'loaded'
  safe_shot(page,slug)
  all_text=[]
  for i,frame in enumerate(page.frames):
   try:
    info=frame.evaluate(EXTRACT_JS)
    data['frames'].append({'url':frame.url,'table_count':len(info['tables']),'text_length':len(info['text'])})
    for table in info['tables']:
     table['frame_url']=frame.url
     data['tables'].append(table)
     data['bids'].extend(rows_from_table(table,source,url,captured,frame.url))
    all_text.append('\n\n=== FRAME '+str(i)+' '+frame.url+' ===\n'+info['text'])
    data['bids'].extend(extract_line_based(info['text'],source,url,captured,frame.url))
   except Exception as e:
    data['frames'].append({'url':frame.url,'error':str(e)[:200]})
  (CAPTURES/(slug+'.txt')).write_text('\n'.join(all_text)[:400000],encoding='utf-8')
  # The most common issue is access denial or pages that show empty placeholders.
  lowered=' '.join(all_text).lower()
  if not data['bids'] and any(word in lowered for word in ['access denied','verify you are human','captcha','just a moment...']):
   data['error']='Website displayed an access challenge; automation cannot collect bids.'
  elif not data['bids']:
   data['error']='No verified bid rows extracted. Inspect linked screenshot and visible page text.'
  # Deduplicate any rows found in both DOM tables and text fallback.
  seen=set(); unique=[]
  for b in data['bids']:
   key=(b['location'].lower(),b['commodity'],b['delivery'].lower(),b['cash_usd_per_bu'],b['basis_cents_per_bu'])
   if key not in seen:seen.add(key);unique.append(b)
  data['bids']=unique
 except Exception as e:
  data['error']=str(e)[:1500]
  safe_shot(page,slug)
 finally: context.close()
 return data

def render_html(result):
 rows=[]
 for s in result['sources']:
  for b in s['bids']:
   fields=['source','location','commodity','delivery','cash_usd_per_bu','basis_cents_per_bu','futures_month','captured_at_ct']
   rows.append('<tr>'+''.join('<td>'+html.escape(str(b.get(k,'') if b.get(k) is not None else 'Not posted'))+'</td>' for k in fields)+'</tr>')
 status=''.join('<li>'+html.escape(f"{s['source']}: HTTP {s['status']} / {len(s['tables'])} tables / {len(s['bids'])} bids"+
   (' — '+s['error'] if s['error'] else ''))+'</li>' for s in result['sources'])
 links=''.join(f'<li><a href="captures/{slug}.png">{html.escape(name)} screenshot</a> | <a href="captures/{slug}.txt">visible text</a></li>' for name,slug,_ in SOURCES)
 return '''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Magnolia grain bids</title><style>body{font:16px system-ui,sans-serif;max-width:1300px;margin:32px auto;padding:0 16px;color:#223}table{border-collapse:collapse;width:100%;font-size:14px}td,th{border-bottom:1px solid #ddd;padding:8px;text-align:left}th{background:#f1f5f9}a{color:#176795}main{overflow-x:auto}</style></head><body><h1>Magnolia-area public grain bids</h1><p>Capture: <b>'''+html.escape(result['captured_at_ct'])+'''</b> Central. Public data are NOT executable quotes. Confirm with buyer. Missing basis means 'not posted', never estimated.</p><p><a href="latest.json">JSON</a> | <a href="latest.csv">CSV</a></p><main><table><thead><tr>'''+''.join('<th>'+v+'</th>' for v in ['Buyer','Location','Commodity','Delivery','Cash $/bu','Basis cents','Futures month','Captured CT'])+'''</tr></thead><tbody>'''+''.join(rows)+'''</tbody></table></main><h2>Source status</h2><ul>'''+status+'''</ul><h2>Source screenshots and diagnostics</h2><ul>'''+links+'''</ul><p>If a source remains empty, the collector did not obtain a usable quote. It does not infer cash bids from futures.</p></body></html>'''

def main():
 p=argparse.ArgumentParser();p.add_argument('--force',action='store_true');args=p.parse_args()
 now=datetime.now(CT)
 if os.getenv('GITHUB_EVENT_NAME')=='schedule' and now.hour!=5 and not args.force:
  print('Skipping daylight-saving alternate run',now.isoformat());return
 captured=now.strftime('%Y-%m-%d %I:%M:%S %p %Z')
 result={'captured_at_ct':captured,'sources':[]}
 with sync_playwright() as pw:
  browser=pw.chromium.launch(headless=True,args=['--disable-dev-shm-usage'])
  for source,slug,url in SOURCES:
   print('Reading',source,url,flush=True)
   data=run_source(browser,source,slug,url,captured)
   print('Result',data['status'],len(data['tables']),'tables;',len(data['bids']),'bids;',data['error'][:140],flush=True)
   result['sources'].append(data)
  browser.close()
 (OUT/'latest.json').write_text(json.dumps(result,indent=2,ensure_ascii=False),encoding='utf-8')
 fields=['source','location','commodity','delivery','cash_usd_per_bu','basis_cents_per_bu','futures_month','captured_at_ct','url','frame_url','basis_status']
 with (OUT/'latest.csv').open('w',newline='',encoding='utf-8') as fp:
  w=csv.DictWriter(fp,fieldnames=fields);w.writeheader()
  for s in result['sources']:
   for b in s['bids']:w.writerow({k:b.get(k) for k in fields})
 (OUT/'index.html').write_text(render_html(result),encoding='utf-8')
 print('Wrote report to',OUT)
if __name__=='__main__':main()
