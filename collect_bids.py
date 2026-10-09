"""Capture publicly displayed cash-bid tables with Chromium.

Run locally: python collect_bids.py --force
Run in GitHub Actions: python collect_bids.py (DST-aware 5 AM CT filtering)

This collector does not log in, bypass access controls, or promise that every
publisher will allow automated access. It saves screenshots AND tabular data.
"""
from __future__ import annotations

import argparse
import csv
import html
import json
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright

BASE = Path(__file__).resolve().parent
OUT = BASE / "docs"
OUT.mkdir(exist_ok=True)
CAPTURES = OUT / "captures"
CAPTURES.mkdir(exist_ok=True)
CT = ZoneInfo("America/Chicago")

SOURCES = [
    ("CHS Brandon", "chs-brandon", "https://www.chsbrandon.com/grain/cash-bids/"),
    ("New Vision", "new-vision", "https://newvision.coop/current-grain-prices/?format=table&groupby=location&setLocation=20359&commodity="),
    ("Chandler Feed", "chandler-feed", "https://www.chandlerfeed.com/cash-bids"),
    ("CFE", "cfe", "https://farmbucks.com/grain-prices/cfe"),
    ("POET", "poet", "https://farmbucks.com/grain-prices/poet"),
    ("New Vision (alternate)", "farmbucks-newvision", "https://farmbucks.com/grain-prices/new-vision-coop"),
]

# This runs in the webpage and reads the REAL rendered DOM, not an image OCR.
EXTRACT_JS = r"""() => {
  const known = ['Magnolia', 'Luverne', 'Pipestone', 'Chandler', 'Edgerton',
    'Woodstock', 'Leota', 'Hardwick', 'Worthington', 'Ellsworth', 'Hills',
    'Brewster', 'Rock Rapids', 'Bigelow', 'Rushmore', 'Ruthton', 'Sheldon',
    'Bingham Lake', 'Heron Lake', 'Brandon', 'Ashton', 'Chancellor', 'Reading'];
  const clean = v => (v || '').replace(/\s+/g, ' ').trim();
  function recentLocation(t) {
    const range = document.createRange();
    range.setStart(document.body, 0);
    try { range.setEndBefore(t); } catch(e) { return ''; }
    const before = range.toString().slice(-7000);
    const candidates = known.map(name => ({name, pos:before.toLowerCase().lastIndexOf(name.toLowerCase())}));
    candidates.sort((a,b)=> b.pos-a.pos);
    return candidates[0].pos>=0 ? candidates[0].name : '';
  }
  function recentCommodity(t) {
    const r = document.createRange();
    r.setStart(document.body,0);
    try {r.setEndBefore(t)} catch(e) { return '' }
    const before=r.toString().slice(-1800).toLowerCase();
    const values = [['soybeans','Soybeans'],['yellow corn','Corn'],['corn','Corn']];
    const ranked=values.map(([term,label])=>({label,pos:before.lastIndexOf(term)}));
    ranked.sort((a,b)=>b.pos-a.pos);
    return ranked[0].pos>=0 ? ranked[0].label : '';
  }
  return Array.from(document.querySelectorAll('table')).map((t,i) => ({
    table: i,
    location: recentLocation(t),
    commodity: recentCommodity(t),
    headers: Array.from(t.querySelectorAll('thead th')).map(x=>clean(x.innerText)),
    rows: Array.from(t.querySelectorAll('tr')).map(r=>
      Array.from(r.querySelectorAll('th,td')).map(c=>clean(c.innerText))
    ).filter(r=>r.length>=2),
  }));
}"""


def basis_cents(value: str):
    """Normalizes '-0.56' dollars or '-56.00' cents to cents."""
    try:
        n = float(value.replace("$", "").replace("¢", "").replace(",", "").strip())
        return round(n if abs(n) > 2 else n * 100, 2)
    except (ValueError, AttributeError):
        return None


def value_for(row, header, aliases):
    for ix, name in enumerate(header):
        low = name.lower()
        if any(a in low for a in aliases) and ix < len(row):
            return row[ix]
    return ""


def normalize(raw_tables, source, link, capture_time):
    """Extract rows only when a clear cash-bid and basis table exists."""
    bids = []
    for t in raw_tables:
        rows = t.get("rows", [])
        header = [s.lower() for s in t.get("headers", [])]
        if not header:
            for row in rows[:4]:
                if any('basis' in c.lower() for c in row) and any('bid' in c.lower() or 'cash price' in c.lower() for c in row):
                    header = [s.lower() for s in row]
                    break
        if not header or not any('basis' in c for c in header) or not any('bid' in c or 'cash price' in c for c in header):
            continue
        for row in rows:
            if len(row) != len(header) or [c.lower() for c in row] == header:
                continue
            cash = value_for(row, header, ['cash price', 'bid'])
            bas = value_for(row, header, ['basis'])
            if basis_cents(bas) is None:
                continue
            try:
                cash_float = float(cash.replace('$','').replace(',',''))
                if not 1 < cash_float < 30:
                    continue
            except (ValueError, AttributeError):
                continue
            raw_delivery = value_for(row, header, ['delivery', 'notes'])
            raw_commodity = value_for(row, header, ['name'])
            commodity = t.get('commodity', '')
            if raw_commodity.lower() in ('corn', 'yellow corn', 'soybeans', 'soybean'):
                commodity = 'Soybeans' if 'soy' in raw_commodity.lower() else 'Corn'
            if not commodity:
                # No guessing commodity when source lacks a clear label.
                continue
            bids.append({
                'source': source,
                'location': t.get('location', '') or ('Chandler' if source=='Chandler Feed' else ''),
                'commodity': commodity,
                'delivery': raw_delivery,
                'cash_usd_per_bu': cash_float,
                'basis_cents_per_bu': basis_cents(bas),
                'futures_month': value_for(row, header, ['futures month', 'basis month']),
                'captured_at_ct': capture_time,
                'url': link,
            })
    return bids


def safe_shot(page, slug):
    path = CAPTURES / (slug + '.png')
    try:
        page.screenshot(path=str(path), full_page=True, timeout=20000, animations='disabled')
    except Exception:
        try:
            page.screenshot(path=str(path), full_page=False, timeout=15000, animations='disabled')
        except Exception:
            pass


def run_source(browser, source, slug, url, captured):
    context = browser.new_context(viewport={"width": 1600, "height": 1000}, device_scale_factor=1,
                                  extra_http_headers={"Cache-Control": "no-cache", "Pragma": "no-cache"})
    page = context.new_page()
    data = {"source": source, "url":url, "status":"unavailable", "tables":[], "bids":[], "error":""}
    try:
        response = page.goto(url, wait_until='domcontentloaded', timeout=45000)
        page.wait_for_timeout(9000)  # Allow async cash-bid widgets to finish rendering.
        data['status'] = str(response.status) if response else 'loaded'
        data['tables'] = page.evaluate(EXTRACT_JS)
        data['bids'] = normalize(data['tables'], source, url, captured)
        # Save the actual text the user would see. Essential for first-run diagnostics.
        txt = page.locator('body').inner_text(timeout=15000)
        (CAPTURES / (slug + '.txt')).write_text(txt[:200000], encoding='utf-8')
        safe_shot(page, slug)
        if source == 'Chandler Feed':
            # Each branch is a separate posted price. Browser clicks if available.
            for index, select in enumerate(page.locator('select').all()[:3]):
                try:
                    opts = select.locator('option').all()
                    names = [(o.inner_text().strip(), o.get_attribute('value')) for o in opts]
                    names = [(n,v) for n,v in names if n.lower() in ('chandler','edgerton','woodstock','leota','hardwick','pipestone')]
                    if len(names)<2:
                        continue
                    for name,value in names:
                        if not value:
                            continue
                        select.select_option(value)
                        page.wait_for_timeout(3000)
                        data['bids'].extend(normalize(page.evaluate(EXTRACT_JS), source, page.url, captured))
                        safe_shot(page, slug+'-'+name.lower())
                    break
                except Exception:
                    continue
    except Exception as exc:
        data['error'] = str(exc)[:1500]
        try: safe_shot(page, slug)
        except Exception: pass
    finally:
        context.close()
    return data


def render_html(result):
    rows=[]
    for source in result['sources']:
        for b in source['bids']:
            rows.append('<tr>' + ''.join(f'<td>{html.escape(str(b[k]))}</td>' for k in
                ('source','location','commodity','delivery','cash_usd_per_bu','basis_cents_per_bu','futures_month','captured_at_ct')) + '</tr>')
    statuses = ''.join('<li>' + html.escape(s['source']+': HTTP '+s['status']+' / '+str(len(s['tables']))+' HTML tables / '+str(len(s['bids']))+' bid rows') +
                       (' — '+html.escape(s['error']) if s['error'] else '') + '</li>' for s in result['sources'])
    links = ''.join(f'<li><a href="captures/{slug}.png">{html.escape(src)}</a> | <a href="captures/{slug}.txt">visible text</a></li>'
                   for src,slug,_ in SOURCES)
    return '''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Magnolia grain bids</title>
    <style>body{font:16px system-ui,sans-serif;max-width:1300px;margin:32px auto;padding:0 16px;color:#223}table{border-collapse:collapse;width:100%;font-size:14px}td,th{border-bottom:1px solid #d8dee3;padding:8px;text-align:left}th{background:#f1f5f9}small{color:#555}main{overflow-x:auto}a{color:#135e86}</style></head><body><h1>Magnolia-area public grain bids</h1>
    <p>Capture: <b>''' + html.escape(result['captured_at_ct']) + '''</b> (Central time). Public data only. Bid snapshots are not executable quotes. Confirm with the buyer. Nonmatching tables are NOT treated as price data.</p>
    <p><a href="latest.json">Structured JSON</a> | <a href="latest.csv">CSV</a></p><main><table><thead><tr>''' + ''.join('<th>'+h+'</th>' for h in ['Buyer','Location','Commodity','Delivery','Cash $/bu','Basis cents','Futures month','Captured CT']) + '''</tr></thead><tbody>''' + ''.join(rows) + '''</tbody></table></main>
    <h2>Source status (important)</h2><ul>''' + statuses + '''</ul><h2>Rendered screenshots and visible page text</h2><ul>''' + links + '''</ul>
    <p><small>Only scraped publicly displayed information, once a day. If a page blocks browsing, ask the site operator for permission or an official feed.</small></p></body></html>'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--force', action='store_true', help='ignore the Central-time scheduled-hour gate')
    args = parser.parse_args()
    now = datetime.now(CT)
    if os.getenv('GITHUB_EVENT_NAME') == 'schedule' and now.hour != 5 and not args.force:
        print('Skipping daylight-saving alternate run at', now.isoformat())
        return
    captured = now.strftime('%Y-%m-%d %I:%M:%S %p %Z')
    result = {'captured_at_ct':captured, 'sources':[]}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=['--disable-dev-shm-usage'])
        for source,slug,url in SOURCES:
            print('Reading', source, url, flush=True)
            data = run_source(browser, source, slug, url, captured)
            print('Result', data['status'], len(data['tables']), 'tables;', len(data['bids']), 'bids;', data['error'][:140], flush=True)
            result['sources'].append(data)
        browser.close()
    (OUT/'latest.json').write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding='utf-8')
    fields=['source','location','commodity','delivery','cash_usd_per_bu','basis_cents_per_bu','futures_month','captured_at_ct','url']
    with (OUT/'latest.csv').open('w', newline='', encoding='utf-8') as fp:
        writer=csv.DictWriter(fp, fieldnames=fields)
        writer.writeheader()
        for source in result['sources']:
            writer.writerows(source['bids'])
    (OUT/'index.html').write_text(render_html(result), encoding='utf-8')
    print('Wrote report to', OUT)


if __name__ == '__main__':
    main()
