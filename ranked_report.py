"""Build independent, highest-price-first corn and soybean bid rankings."""
import html,json,re
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
D=Path(__file__).resolve().parent/'docs'
MILES={'Magnolia':0,'Luverne':7,'Ellsworth':9,'Rushmore':14,'Beaver Creek':14,'Reading':15,'Rock Rapids':16,'Hills Terminal':16,'Chandler':21,'Bigelow':22,'Worthington':24,'Sibley':24,'Pipestone':27,'Ashton':27,'Brewster':31,'Sheldon':34,'Canton':35,'Ruthton':37,'Heron Lake':39,'Worthing Ag Grain':41,'Elkton':46,'Tracy':47,'Adrian':20,'Dundee':34,'Jeffers':44,'Ocheyedan':36,'Alvord':24,'Larchwood':30,'Inwood':26,'George':30,'Harris':40,'Lake Park':46,'Rock Valley':33,'Wilmont':27,'Miloma':28,'Mountain Lake':46,'Windom':50}
def buyer(b):
 s=b['source'];place=b['location'].split(',')[0].strip()
 if s=='CHS Brandon':return 'CHS · '+place,place,'CHS'
 if s=='Chandler Feed':return 'Chandler Feed · '+place,place,'Chandler'
 if s=='CFE':return 'CFE · '+place,place,'CFE'
 if s=='POET':return 'POET · '+place,place,'POET'
 if s=='New Vision (Farmbucks)':
  for pre in ('MNSP ','AGP ','POET '):
   if place.startswith(pre):return pre.strip()+' · '+place[len(pre):],place[len(pre):],pre.strip()
  if place.startswith(('ADM ','CHS ')):return '','',''
  return 'New Vision · '+place,place,'NV'
 return '','',''
def months(d):
 if d.upper()=='NC 26':return ['Oct']
 if not re.search(r'\b2026\b',d):return []
 if re.match(r'^Oct(?:ober)?\b',d,re.I):return ['Oct','Nov'] if re.match(r'^Oct\s*[-–]\s*Nov\b',d,re.I) else ['Oct']
 if re.match(r'^Nov(?:ember)?\b',d,re.I):return ['Nov']
 return []
def chandler_trades():
 p=D/'captures'/'chandler-feed.txt'
 if not p.exists():return {}
 lines=[x.strip() for x in p.read_text(encoding='utf-8').splitlines() if x.strip()]
 cur='';out={}
 for i,v in enumerate(lines):
  if v in ('Corn','Soybeans'):cur=v;continue
  if v in ('Oats','Wheat'):cur='';continue
  if not cur or not re.fullmatch(r'(?:NC 26|Oct 26|Nov 26|Dec 26)',v):continue
  try:price=float(lines[i+1])
  except (ValueError,IndexError):continue
  tm=next((x for x in lines[i+2:i+9] if re.fullmatch(r'\d{1,2}:\d\d\s*[AP]M',x)),None)
  if tm:out[(cur,v,price)]=tm
 return out
def timestamp(b,trades):
 if b['source']=='Chandler Feed':
  t=trades.get((b['commodity'],b['delivery'],b['cash_usd_per_bu']))
  if t:
   try:
    d=datetime.strptime(b['source_updated'].removeprefix('Cash bids for '),'%A, %B %d, %Y')
    tm=datetime.strptime(t,'%I:%M %p')
    return d.strftime('%m/%d')+' '+tm.strftime('%-I:%M%p').lower(),'T'
   except ValueError:pass
 m=re.search(r'Last updated:\s*(\w+\s+\d+,\s+\d{4})\s+(\d{1,2}:\d{2}\s*[ap]m)\s+MT',b.get('source_updated',''),re.I)
 if m:
  try:
   d=datetime.strptime(m[1]+' '+m[2].upper(),'%b %d, %Y %I:%M%p')
   d=d.replace(tzinfo=ZoneInfo('America/Denver')).astimezone(ZoneInfo('America/Chicago'))
   return d.strftime('%m/%d %-I:%M%p').lower(),'U'
  except ValueError:pass
 m=re.search(r'^(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2}\s+[AP]M)',b.get('captured_at_ct',''))
 if m:
  d=datetime.strptime(m[1]+' '+m[2],'%Y-%m-%d %I:%M:%S %p')
  return d.strftime('%m/%d %-I:%M%p').lower(),'C'
 return 'Unknown','?'
def build():
 j=json.loads((D/'latest.json').read_text(encoding='utf-8'))
 groups={m:{'Corn':[],'Soybeans':[]} for m in ('Oct','Nov')}
 trades=chandler_trades()
 for section in j['sources']:
  for b in section['bids']:
   crop=b.get('commodity')
   mm=months(b.get('delivery',''))
   if crop not in ('Corn','Soybeans') or not mm:continue
   label,town,kind=buyer(b)
   if town not in MILES or MILES[town]>50:continue
   if kind=='POET' and town in ('Chancellor','Hudson'):continue
   if kind=='POET' and town=='Ashton' and b['source']=='POET' and 'Oct' in mm:continue
   if kind=='POET' and town=='Ashton' and b['source']=='New Vision (Farmbucks)' and 'Nov' in mm:continue
   tm,flag=timestamp(b,trades)
   row=(label,MILES[town],float(b['cash_usd_per_bu']),b.get('basis_cents_per_bu'),tm,flag,b['delivery'],b.get('url',''))
   for m in mm:groups[m][crop].append(row)
 def table(crop,rows):
  seen=set();out=[]
  for name,mi,cash,basis,tm,flag,delivery,url in sorted(rows,key=lambda r:(-r[2],r[1],r[0])):
   key=(name,delivery,cash)
   if key in seen:continue
   seen.add(key)
   esc=lambda s:html.escape(str(s),quote=True)
   bas='—' if basis is None else f'{basis:+.0f}¢'
   subtitle=f'<small>{esc(delivery)}</small>' if re.search(r'\d\s*[-–]\s*\d',delivery) else ''
   out.append(f'<tr><td><a href="{esc(url)}">{esc(name)}</a>{subtitle}</td><td class="num">{mi}</td><td class="num cash">&#36;{cash:.2f}</td><td class="num">{bas}</td><td class="num">{esc(tm)}<sup>{flag}</sup></td></tr>')
  return f'<section><h2>{crop} <small>{len(out)} bids</small></h2><div class="scroll"><table><thead><tr><th>Buyer</th><th class="num">Mi*</th><th class="num">Cash</th><th class="num">Basis</th><th class="num">Date/time CT</th></tr></thead><tbody>{"".join(out)}</tbody></table></div></section>'
 panels=''.join(f'<div class="panel" id="panel-{m}" {"hidden" if m=="Nov" else ""}><div class="cols">{table("Corn",groups[m]["Corn"])}{table("Soybeans",groups[m]["Soybeans"])}</div></div>' for m in ('Oct','Nov'))
 status=''.join(f'<li>{html.escape(s["source"])}: {len(s["bids"])} bids; {html.escape(s["error"] or "OK")}</li>' for s in j['sources'])
 page='''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Magnolia grain bid rankings</title><style>
 body{font:14px system-ui,sans-serif;max-width:1450px;margin:25px auto;padding:0 15px;background:#f5f7fa;color:#1d2939}
 h1{font-size:26px;margin-bottom:4px}p{line-height:1.5}.muted{color:#667085;font-size:13px}.tabs{display:flex;gap:8px;align-items:center;margin:18px 0;flex-wrap:wrap}
 button{padding:9px 16px;background:white;border:1px solid #ccd4df;border-radius:7px;cursor:pointer;font-weight:600}
 button.active{background:#244766;color:white}.cols{display:grid;grid-template-columns:1fr 1fr;gap:14px}
 section{background:white;border:1px solid #e3e8f0;border-radius:10px;overflow:hidden}h2{background:#edf2f7;margin:0;padding:13px;font-size:18px}
 h2 small{font-size:11px;color:#5b6573;font-weight:400}.scroll{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:12px;white-space:nowrap}
 td,th{text-align:left;border-bottom:1px solid #e5e8ee;padding:8px 7px}th{font-size:11px;background:#fafbfc;color:#616c79}
 tr:hover td{background:#f9fbff}.num{text-align:right}.cash{font-weight:700}a{color:#145782}td small{display:block;color:#687586}
 sup{color:#748093;margin-left:2px}.notes{font-size:13px;margin:17px 0}.panel[hidden]{display:none}
 @media(max-width:1000px){.cols{grid-template-columns:1fr}}
 </style></head><body><h1>Magnolia grain cash bids</h1><p class="muted">Captured '''+html.escape(j['captured_at_ct'])+''' • Highest cash prices first in each commodity • Within ~50 straight-line miles</p>
 <div class="tabs"><b>Delivery:</b><button class="active" data-month="Oct">October 2026</button><button data-month="Nov">November 2026</button>
 <a href="latest.csv">Download all bids (CSV)</a></div>'''+panels+'''
 <p class="notes"><b>Time key:</b> <b>T</b> buyer-posted last trade; <b>U</b> third-party website last updated; <b>C</b> automated browser captured (not the exact time the buyer set the price). All times Central. A dash in basis means the source did not post basis. *Distances are approximate straight-line miles, not truck-route miles. Short-delivery windows are indicated under the buyer. Quotes are not executable until confirmed with buyer.</p>
 <details><summary>Collector diagnostics</summary><ul>'''+status+'''</ul><a href="latest.json">Raw JSON</a></details>
 <script>document.querySelectorAll('[data-month]').forEach(b=>b.onclick=()=>{document.querySelectorAll('[data-month]').forEach(x=>x.classList.toggle('active',x===b));document.querySelectorAll('.panel').forEach(x=>x.hidden=x.id!=='panel-'+b.dataset.month)})</script></body></html>'''
 (D/'index.html').write_text(page,encoding='utf-8')
 print('Ranked dashboard published')
if __name__=='__main__':build()
