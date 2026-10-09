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

def _lines(text):
 """Keep DOM text's column breaks; some cash-bid widgets emit one cell per line."""
 return [re.sub(r'\s+', ' ', str(t).replace('\xa0',' ')).strip() for t in text.splitlines() if t.strip()]


def _price(s):
 """Parse an isolated USD cash price, never a futures value or a price range."""
 m=re.fullmatch(r'(?:USD\s*)?\$?\s*(\d{1,2}\.\d{2,4})',s.strip(),re.I)
 if not m:return None
 x=float(m.group(1))
 return x if 2<=x<=25 else None


def _basis(s):
 """Normalize -0.45 dollars or -45.00 cents to basis cents."""
 m=re.fullmatch(r'([+-]?\d{1,3}\.\d{1,4})',s.strip().replace('−','-'))
 if not m:return None
 x=float(m.group(1))
 return round(100*x if abs(x)<3 else x,2) if -250<=x<=250 else None


def _row(source,loc,crop,month,price,basis,url,captured,frame_url,futures='',page_date=''):
 return {'source':source,'location':loc,'commodity':crop,'delivery':month,
         'cash_usd_per_bu':price,'basis_cents_per_bu':basis,'futures_month':futures,
         'captured_at_ct':captured,'url':url,'frame_url':frame_url,
         'source_updated':page_date,
         'basis_status':'posted' if basis is not None else 'not posted'}


def _cme_dash(s):
 """502-4 = $5.025; 1297-0 = $12.970; 502-0 = $5.020."""
 m=re.fullmatch(r'(\d{3,4})-([0-7])',s.strip())
 if not m:return None
 return (int(m.group(1))+int(m.group(2))/8)/100


def _page_date(lines,source):
 for line in lines[:120]:
  if source=='Chandler Feed' and re.search(r'Cash bids for (?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)',line,re.I):
   return line.strip()
  if 'Farmbucks' in source or source in ('CFE','POET'):
   if re.search(r'Last updated\s*:',line,re.I):return line.strip()
 return 'Not published in captured text'


_CHS_BASE_LOCATIONS={
 'brandon','magnolia','luverne','pipestone','ruthton','tracy','marshall',
 'lismore','hills','ellsworth','worthington','garretson','sioux falls','hartford',
 'sioux center','rock rapids','adrian','jasper','chandler','edgerton','hardwick',
 'woodstock','leota','windom','reading','brewster'
}
_CHS_MONTH=re.compile(r'^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+20\d{2}$',re.I)
_CHS_FUT=re.compile(r'^Z[CS][A-Z]\d{2}$',re.I)
_CHS_EXCLUDE_HEADINGS={'location','commodity','cash bids','cash bid','basis','bid','delivery',
 'futures','futures month','change','notes','grain','login','yellow corn','corn','soybeans'}


def _chs_location_header(line, next_line):
 """Recognize the LOCATION label immediately preceding a crop section.

 CHS can publish multiple delivery points in one page, sometimes with
 qualifiers (e.g. 'Brandon East'). A closed list silently assigns unknown
 headings to the preceding elevator; instead, recognize the repeated heading
 structure, requiring an immediate Soybeans/Corn section after it.
 """
 name=line.strip().replace('\u00a0',' ')
 low=name.casefold()
 if low in _CHS_EXCLUDE_HEADINGS or not name or len(name)>70:return None
 if _CHS_MONTH.fullmatch(name) or _CHS_FUT.fullmatch(name):return None
 if _price(name) is not None or _basis(name) is not None:return None
 if next_line.casefold() not in ('soybeans','corn','yellow corn'):return None
 # No navigation fragments, explanatory text or bid headers.
 if not re.fullmatch(r"[A-Za-z][A-Za-z0-9 .&'()\-]{0,69}",name):return None
 words=name.split()
 if len(words)>6:return None
 if low in _CHS_BASE_LOCATIONS or any(re.search(r'\b'+re.escape(loc)+r'\b',low) for loc in _CHS_BASE_LOCATIONS):
  return name
 # Other CHS cash bid cards can have a new town not in our list.
 # On a repeated 'LOCATION / Soybeans / DELIVERY' layout, accept short
 # place-like headings, but NOT a generic page title.
 if len(words)<=3 and words[0][0].isupper() and not any(t in low for t in ('cash bid','location','futures','grain prices','harvest','2026','2027')):
  return name
 return None


def parse_chs(lines,src,url,captured,frame_url,page_date):
 """Extract CHS cash bids, tying every block to its explicit location header.

 One cell per line, e.g. Magnolia / Soybeans / Oct 2026 / 12.45 / -0.51 /
 12.9600 / 0.0850 / ZSX26. If headings aren't recognized, downstream
 conflict checking quarantines contradictory same-location quotations.
 """
 loc='';crop='';result=[]
 for i,line in enumerate(lines):
  next_line=lines[i+1] if i+1<len(lines) else ''
  new_location=_chs_location_header(line,next_line)
  if new_location:
   loc=new_location;crop='';continue
  low=line.casefold()
  if low in ('soybeans','yellow corn','corn'):
   crop='Soybeans' if low=='soybeans' else 'Corn';continue
  if not (loc and crop and _CHS_MONTH.fullmatch(line)):continue
  tail=lines[i+1:i+8]
  if len(tail)<3:continue
  bid=_price(tail[0]);basis=_basis(tail[1]);fut=_price(tail[2])
  if bid is None or basis is None or fut is None:continue
  if abs((bid-basis/100)-fut)>.025:continue
  future=next((v for v in tail[3:] if _CHS_FUT.fullmatch(v)), '')
  if not future:continue
  result.append(_row(src,loc,crop,line,bid,basis,url,captured,frame_url,future,page_date))
 return result


_CHANDLER_DEL=re.compile(r'^(?:NC|Cash|Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s*(?:20)?\d{2}$',re.I)

def parse_chandler(lines,src,url,captured,frame_url,page_date):
 """Chandler public page emits 'NC 26', '4.52', '-0.50', '502-0', '+1-6', 'Dec 26 Corn'."""
 loc='Chandler';crop='';out=[]
 for i,line in enumerate(lines):
  low=line.lower()
  if low=='corn':crop='Corn';continue
  if low=='soybeans':crop='Soybeans';continue
  if low in ('oats','wheat','milo','grain sorghum'):crop='';continue
  if not crop or not _CHANDLER_DEL.fullmatch(line):continue
  tail=lines[i+1:i+9]
  if len(tail)<3:continue
  bid=_price(tail[0]);basis=_basis(tail[1]);future=_cme_dash(tail[2])
  if bid is None or basis is None or future is None:continue
  if abs((bid-basis/100)-future)>.025:continue
  futuremonth=next((v for v in tail[3:] if re.match(r'^(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{2}\s+(?:Corn|Soybeans)$',v,re.I)),'')
  if not futuremonth:continue
  out.append(_row(src,loc,crop,line,bid,basis,url,captured,frame_url,futuremonth,page_date))
 return out


_FARM_LOC=re.compile(r'^([\w&.() \-]+?),\s*(Minnesota|Iowa|South Dakota|Nebraska)(?:\s+Compare prices)?\s*$',re.I)
_FARM_DEL=re.compile(r'^(?:(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?)\s+20\d{2}|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*[\u2013-](?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+20\d{2}|Sep\s+\d+[\u2013-]\d+,\s*20\d{2})$',re.I)

def parse_farmbucks(lines, src, url, captured, frame_url, page_date):
 """Read Farmbucks location rows whose delivery and USD price share one line.

 Farmbucks currently renders:
 Rock Rapids, Iowa
 Compare prices
 October 2026    USD 4.34

 Older layouts can place delivery and the amount on separate lines.
 Reject regional range summaries and prices without a named location.
 """
 crop='';loc='';result=[]
 price_line=re.compile(r'^(.*?)\\s+USD\\s+(\\d{1,2}\\.\\d{2,4})\\s*$',re.I)
 date_words=re.compile(r'^(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?|NC)\\b',re.I)
 for i,line in enumerate(lines):
  low=line.lower()
  if low in ('#2 yellow corn','yellow corn') or low.startswith('#2 yellow corn prices in '):
   crop='Corn';loc='';continue
  if low in ('#2 yellow soybeans','yellow soybeans') or low.startswith('#2 yellow soybeans prices in '):
   crop='Soybeans';loc='';continue
  candidate=re.sub(r'\\s+Compare prices$','',line,flags=re.I).strip()
  m=_FARM_LOC.fullmatch(candidate)
  if m:
   loc=m.group(1).strip()+', '+m.group(2).title()
   continue
  if not(crop and loc):continue
  delivery=None;price=None
  match=price_line.fullmatch(line)
  if match:
   delivery=match.group(1).strip()
   price=_price(match.group(2))
  elif _FARM_DEL.fullmatch(line) and i+1<len(lines):
   delivery=line
   next_match=re.fullmatch(r'USD\\s+(\\d{1,2}\\.\\d{2,4})',lines[i+1],re.I)
   if next_match:price=_price(next_match.group(1))
   else:price=_price(lines[i+1])
  if not delivery or not date_words.match(delivery) or price is None:continue
  if not re.search(r'\\b20\\d{2}\\b',delivery):continue
  result.append(_row(src,loc,crop,delivery,price,None,url,captured,frame_url,page_date=page_date))
 return result

_NV_LOC={'magnolia','worthington','hills terminal','hills','ellsworth','reading','brewster',
         'lismore','heron lake','dundee','miloma','jeffers','mountain lake','windom','wilmont'}
_NV_DEL=re.compile(r'^(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)(?:[a-z]*)?\s+(?:\d{2}|20\d{2})$',re.I)

def parse_newvision(lines,src,url,captured,frame_url,page_date):
 """New Vision's dynamic location widget prints: Corn, Oct 26, $4.5750, Oct 26,
 Fut.Chg, -45.00, Dec 2026. Require concrete location and a matching basis equation
 when a quoted futures value can be inferred from price and basis.
 """
 loc='';out=[]
 for i,line in enumerate(lines):
  lo=line.lower()
  if lo in _NV_LOC:loc=line.title();continue
  if lo not in ('corn','soybeans') or not loc:continue
  crop='Corn' if lo=='corn' else 'Soybeans'
  tail=lines[i+1:i+12]
  if len(tail)<3 or not _NV_DEL.fullmatch(tail[0]):continue
  price=_price(tail[1]);month=tail[0]
  if price is None:continue
  # Basis at New Vision is in cents, e.g. -45.00; avoid negative futures change (2-2).
  basis=None
  for candidate in tail[2:8]:
   if re.fullmatch(r'-\d{1,3}\.\d{2}',candidate):
    maybe=_basis(candidate)
    if maybe is not None and -250<=maybe<=0:basis=maybe;break
  future=next((x for x in tail[2:10] if re.match(r'^(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+20\d{2}$',x,re.I)),'')
  out.append(_row(src,loc,crop,month,price,basis,url,captured,frame_url,future,page_date))
 return out


def extract_line_based(text,source,url,captured,frame_url):
 """Source-aware multiline extraction for rendered widget and card text."""
 lines=_lines(text)
 if not lines:return []
 page_date=_page_date(lines,source)
 if source=='CHS Brandon':return parse_chs(lines,source,url,captured,frame_url,page_date)
 if source=='Chandler Feed':return parse_chandler(lines,source,url,captured,frame_url,page_date)
 if source in ('CFE','POET','New Vision (Farmbucks)'):
  return parse_farmbucks(lines,source,url,captured,frame_url,page_date)
 if source=='New Vision':return parse_newvision(lines,source,url,captured,frame_url,page_date)
 return []

def validate_and_resolve_bids(bids):
 """Never show two incompatible prices for the same location/crop/month.

 Store contradictory rows in a diagnostics list instead of selecting an
 arbitrary price. Extra quotes from separate HTML and text paths deduplicate.
 """
 seen=set();unique=[]
 for b in bids:
  key=(b['location'].casefold().strip(),b['commodity'],b['delivery'].casefold().strip(),
       b['cash_usd_per_bu'],b['basis_cents_per_bu'])
  if key not in seen:seen.add(key);unique.append(b)
 grouped={}
 for b in unique:
  key=(b['location'].casefold().strip(),b['commodity'],b['delivery'].casefold().strip())
  grouped.setdefault(key,[]).append(b)
 conflicts=[{'key':list(k),'quotations':values} for k,values in grouped.items()
            if len({(v['cash_usd_per_bu'],v['basis_cents_per_bu']) for v in values})>1]
 bad={tuple(x['key']) for x in conflicts}
 kept=[v for k,values in grouped.items() if k not in bad for v in values]
 return kept,conflicts

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
 data={'source':source,'url':url,'status':'unavailable','tables':[], 'bids':[], 'conflicts':[], 'error':'', 'frames':[]}
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
   data['error']='No verified bid rows extracted: site may have no data or require different parser. Inspect screenshot and text.'
  data['bids'],data['conflicts']=validate_and_resolve_bids(data['bids'])
  if data['conflicts']:
   msg=f"{len(data['conflicts'])} contradictory location/crop/month group(s) excluded; inspect JSON conflicts and page diagnostics."
   data['error']=(data['error']+'; ' if data['error'] else '')+msg

 except Exception as e:
  data['error']=str(e)[:1500]
  safe_shot(page,slug)
 finally: context.close()
 return data

def render_html(result):
 rows=[]
 for s in result['sources']:
  for b in s['bids']:
   fields=['source','location','commodity','delivery','cash_usd_per_bu','basis_cents_per_bu','futures_month','source_updated','captured_at_ct']
   rows.append('<tr>'+''.join('<td>'+html.escape(str(b.get(k,'') if b.get(k) is not None else 'Not posted'))+'</td>' for k in fields)+'</tr>')
 status=''.join('<li>'+html.escape(f"{s['source']}: HTTP {s['status']} / {len(s['tables'])} tables / {len(s['bids'])} bids"+
   (' — '+s['error'] if s['error'] else ''))+'</li>' for s in result['sources'])
 links=''.join(f'<li><a href="captures/{slug}.png">{html.escape(name)} screenshot</a> | <a href="captures/{slug}.txt">visible text</a></li>' for name,slug,_ in SOURCES)
 return '''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Magnolia grain bids</title><style>body{font:16px system-ui,sans-serif;max-width:1300px;margin:32px auto;padding:0 16px;color:#223}table{border-collapse:collapse;width:100%;font-size:14px}td,th{border-bottom:1px solid #ddd;padding:8px;text-align:left}th{background:#f1f5f9}a{color:#176795}main{overflow-x:auto}</style></head><body><h1>Magnolia-area public grain bids</h1><p>Capture: <b>'''+html.escape(result['captured_at_ct'])+'''</b> Central. Public data are NOT executable quotes. Confirm with buyer. Missing basis means 'not posted', never estimated.</p><p><a href="latest.json">JSON</a> | <a href="latest.csv">CSV</a></p><main><table><thead><tr>'''+''.join('<th>'+v+'</th>' for v in ['Buyer','Location','Commodity','Delivery','Cash $/bu','Basis cents','Futures month','Source updated','Captured CT'])+'''</tr></thead><tbody>'''+''.join(rows)+'''</tbody></table></main><h2>Source status</h2><ul>'''+status+'''</ul><h2>Source screenshots and diagnostics</h2><ul>'''+links+'''</ul><p>If a source remains empty, the collector did not obtain a usable quote. It does not infer cash bids from futures.</p></body></html>'''

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
 fields=['source','location','commodity','delivery','cash_usd_per_bu','basis_cents_per_bu','futures_month','source_updated','captured_at_ct','url','frame_url','basis_status']
 with (OUT/'latest.csv').open('w',newline='',encoding='utf-8') as fp:
  w=csv.DictWriter(fp,fieldnames=fields);w.writeheader()
  for s in result['sources']:
   for b in s['bids']:w.writerow({k:b.get(k) for k in fields})
 (OUT/'index.html').write_text(render_html(result),encoding='utf-8')
 print('Wrote report to',OUT)
if __name__=='__main__':main()
