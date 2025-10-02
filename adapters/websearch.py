# adapters/websearch.py
import os
import re
import json
import time
import html
import csv
import requests
from urllib.parse import urlparse
from datetime import datetime

# ========== Konfiguracja ekstrakcji ceny ==========

PRICE_RE = re.compile(
    r'(\d{1,5}(?:[ \xa0]?\d{3})*(?:[.,]\d{1,2})?)\s*(zł|pln|€|eur|kč|kc|czk)\b',
    re.IGNORECASE
)

def _domain(url: str) -> str:
    try:
        host = urlparse(url).hostname or ""
        parts = host.split(".")
        return ".".join(parts[-3:]) if len(parts) >= 3 and len(parts[-1]) <= 3 else ".".join(parts[-2:])
    except Exception:
        return ""

def _to_float(num_str: str):
    try:
        return float(num_str.replace("\xa0", " ").replace(" ", "").replace(",", "."))
    except Exception:
        return None

def _convert_to_pln(value: float, unit: str, rates: dict) -> float | None:
    unit = (unit or "").lower()
    if unit in ("zł", "pln"):
        return value
    if unit in ("€", "eur"):
        if rates.get("parse_eur") and rates.get("eur_to_pln"):
            return value * float(rates["eur_to_pln"])
        return None
    if unit in ("kč", "kc", "czk"):
        if rates.get("parse_czk") and rates.get("czk_to_pln"):
            return value * float(rates["czk_to_pln"])
        return None
    return None

# ========== Debug CSV ==========

def _dbg_write(debug_cfg: dict | None, row: dict):
    """Dopisuje wiersz do CSV z podglądem kandydatów."""
    if not debug_cfg or not debug_cfg.get("dump_urls_csv"):
        return
    path = debug_cfg.get("dump_file", "checked_urls.csv")
    fields = [
        "ts","query","url","title","domain",
        "passed_domain","passed_url_regex",
        "fetched","matched_pattern","used_js",
        "price_pln","filtered_out_reason"
    ]
    write_header = not os.path.exists(path)
    try:
        with open(path, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            if write_header:
                w.writeheader()
            row = {"ts": datetime.utcnow().isoformat(), **row}
            w.writerow(row)
    except Exception:
        # jeśli z jakiegoś powodu nie da się dopisać – pomiń (nie blokujemy wyszukiwania)
        pass

# ========== Ekstrakcja ze strukturalnych danych ==========

def _extract_from_jsonld(text: str, rates: dict) -> float | None:
    """
    Szuka <script type="application/ld+json"> i próbuje znaleźć Product/Offer z price/priceCurrency.
    Zwraca najniższą cenę w PLN (po konwersji) lub None.
    """
    prices = []
    for m in re.finditer(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', text, re.I | re.S):
        block = m.group(1).strip()
        if not block:
            continue
        try:
            data = json.loads(block)
        except Exception:
            # niektóre sklepy sklejają kilka JSON-ów w jedno <script>; spróbuj po liniach
            chunks = []
            for line in block.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    chunks.append(json.loads(line))
                except Exception:
                    pass
            if not chunks:
                continue
            data = chunks

        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                t = (node.get("@type") or node.get("type") or "").lower()
                if "product" in t or "offer" in t or "aggregateoffer" in t:
                    if "offers" in node:
                        stack.append(node["offers"])
                    else:
                        price = node.get("price") or node.get("lowPrice") or node.get("highPrice")
                        cur   = node.get("priceCurrency") or node.get("pricecurrency")
                        if price:
                            val = _to_float(str(price))
                            if val is not None:
                                if cur:
                                    pln = _convert_to_pln(val, str(cur), rates)
                                else:
                                    pln = val  # brak waluty – traktuj jak PLN
                                if pln is not None:
                                    prices.append(pln)
                for v in node.values():
                    if isinstance(v, (dict, list)):
                        stack.append(v)
            elif isinstance(node, list):
                for it in node:
                    if isinstance(it, (dict, list)):
                        stack.append(it)
    return min(prices) if prices else None

# ========== Ekstrakcja "statyczna" regexem ==========

def _extract_price_regex(text: str, rates: dict) -> float | None:
    candidates = []
    for m in PRICE_RE.finditer(text):
        val = _to_float(m.group(1))
        if val is None:
            continue
        unit = m.group(2)
        pln = _convert_to_pln(val, unit, rates)
        if pln is not None:
            candidates.append(pln)
    return min(candidates) if candidates else None

# ========== Playwright fallback (render JS) ==========

_RENDER_COUNT = 0  # prosty licznik na przebieg

def _render_and_get_html(url: str, nav_timeout_ms: int, wait_until: str) -> str | None:
    """
    Renderuje stronę w headless Chromium i zwraca HTML po załadowaniu.
    Wymaga: playwright + zainstalowanego chromium (workflow: playwright install --with-deps chromium).
    """
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return None

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(locale="pl-PL", user_agent="Mozilla/5.0")
            page = ctx.new_page()
            page.set_default_navigation_timeout(nav_timeout_ms)
            page.goto(url, wait_until=wait_until)
            page.wait_for_timeout(500)
            content = page.content()
            browser.close()
            return content
    except Exception:
        return None

# ========== Główne wyszukiwanie ==========

def search(term: str, timeout: int = 10, ctx: dict | None = None):
    """
    Wyszukiwanie przez Google CSE.
    Env: GOOGLE_CSE_KEY, GOOGLE_CSE_CX
    ctx:
      - websearch: { region, max_results, site_whitelist, site_blacklist,
                     url_whitelist_patterns, url_blacklist_patterns, exact_phrase, prefer_country_pl }
      - availability_keywords: { out_of_stock:[...] }
      - require_in_stock: bool
      - pattern: regex tytułu/HTML (dopasowanie PRODUKTU)
      - currency: { parse_eur, parse_czk, eur_to_pln, czk_to_pln }
      - rendering: { enable_js, max_js_pages_per_run, nav_timeout_ms, wait_until, js_domains_whitelist }
      - debug: { dump_urls_csv, dump_file }
    """
    key = os.getenv("GOOGLE_CSE_KEY")
    cx  = os.getenv("GOOGLE_CSE_CX")
    if not key or not cx:
        return []

    ws = (ctx or {}).get("websearch", {}) or {}
    max_results = int(ws.get("max_results", 10))
    whitelist = set((ws.get("site_whitelist") or []))
    blacklist = set((ws.get("site_blacklist") or []))
    url_wl_patterns = [re.compile(p) for p in (ws.get("url_whitelist_patterns") or [])]
    url_bl_patterns = [re.compile(p) for p in (ws.get("url_blacklist_patterns") or [])]
    exact_phrase = bool(ws.get("exact_phrase", False))
    prefer_pl    = bool(ws.get("prefer_country_pl", True))

    out_words = [w.lower() for w in (ctx or {}).get("availability_keywords", {}).get("out_of_stock", [])]
    require_in_stock = bool((ctx or {}).get("require_in_stock", False))
    rates = (ctx or {}).get("currency", {})  # kursy walut

    rend = (ctx or {}).get("rendering", {}) or {}
    enable_js = bool(rend.get("enable_js", False))
    max_js = int(rend.get("max_js_pages_per_run", 0))
    nav_timeout_ms = int(rend.get("nav_timeout_ms", 12000))
    wait_until = str(rend.get("wait_until", "networkidle"))
    js_domains_wl = set((rend.get("js_domains_whitelist") or []))

    debug_cfg = (ctx or {}).get("debug", {}) or {}

    # regex produktu (sprawdzamy tytuł, a jeśli nie pasuje—treść HTML)
    pat = None
    pat_str = (ctx or {}).get("pattern")
    if pat_str:
        try:
            pat = re.compile(pat_str)
        except Exception:
            pat = None

    items = []
    session = requests.Session()
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept-Language": "pl-PL,pl;q=0.9",
    }

    # Parametry zapytania do CSE
    params = {
        "key": key,
        "cx": cx,
        "num": 10,     # per page
        "hl": "pl",
        "gl": "pl",
        "safe": "off",
    }
    if exact_phrase:
        params["q"] = term
        params["exactTerms"] = term
    else:
        params["q"] = term
    if prefer_pl:
        params["cr"] = "countryPL"

    fetched = 0
    start = 1
    global _RENDER_COUNT

    while fetched < max_results:
        params["start"] = start
        r = session.get("https://www.googleapis.com/customsearch/v1", params=params, timeout=timeout)
        r.raise_for_status()
        data = r.json()
        results = data.get("items", []) or []
        if not results:
            _dbg_write(debug_cfg, {
                "query": term, "url": "", "title": "", "domain": "",
                "passed_domain": "", "passed_url_regex": "",
                "fetched": 0, "matched_pattern": "", "used_js": 0,
                "price_pln": "", "filtered_out_reason": "cse_no_results"
            })
            break

        for it in results:
            link = it.get("link") or ""
            title = it.get("title") or ""
            if not link:
                continue
            dom = _domain(link)

            # listed (surowy kandydat z CSE)
            _dbg_write(debug_cfg, {
                "query": term, "url": link, "title": title, "domain": dom,
                "passed_domain": "", "passed_url_regex": "", "fetched": 0,
                "matched_pattern": "", "used_js": 0, "price_pln": "",
                "filtered_out_reason": "listed"
            })

            # 1) filtr domen
            if whitelist and all(not dom.endswith(w) for w in whitelist):
                _dbg_write(debug_cfg, {
                    "query": term, "url": link, "title": title, "domain": dom,
                    "passed_domain": 0, "passed_url_regex": "", "fetched": 0,
                    "matched_pattern": "", "used_js": 0, "price_pln": "",
                    "filtered_out_reason": "domain_not_whitelisted"
                })
                continue
            if blacklist and any(dom.endswith(b) for b in blacklist):
                _dbg_write(debug_cfg, {
                    "query": term, "url": link, "title": title, "domain": dom,
                    "passed_domain": 0, "passed_url_regex": "", "fetched": 0,
                    "matched_pattern": "", "used_js": 0, "price_pln": "",
                    "filtered_out_reason": "domain_blacklisted"
                })
                continue

            # 2) filtr po ścieżce
            if url_wl_patterns and not any(p.search(link) for p in url_wl_patterns):
                _dbg_write(debug_cfg, {
                    "query": term, "url": link, "title": title, "domain": dom,
                    "passed_domain": 1, "passed_url_regex": 0, "fetched": 0,
                    "matched_pattern": "", "used_js": 0, "price_pln": "",
                    "filtered_out_reason": "url_not_whitelisted"
                })
                continue
            if url_bl_patterns and any(p.search(link) for p in url_bl_patterns):
                _dbg_write(debug_cfg, {
                    "query": term, "url": link, "title": title, "domain": dom,
                    "passed_domain": 1, "passed_url_regex": 0, "fetched": 0,
                    "matched_pattern": "", "used_js": 0, "price_pln": "",
                    "filtered_out_reason": "url_blacklisted"
                })
                continue

            # 3) pobierz statyczny HTML
            try:
                pr = session.get(link, headers=headers, timeout=timeout)
                html_text = pr.text
                _dbg_write(debug_cfg, {
                    "query": term, "url": link, "title": title, "domain": dom,
                    "passed_domain": 1, "passed_url_regex": 1, "fetched": 1,
                    "matched_pattern": "", "used_js": 0, "price_pln": "",
                    "filtered_out_reason": "fetched"
                })
            except Exception:
                _dbg_write(debug_cfg, {
                    "query": term, "url": link, "title": title, "domain": dom,
                    "passed_domain": 1, "passed_url_regex": 1, "fetched": 0,
                    "matched_pattern": "", "used_js": 0, "price_pln": "",
                    "filtered_out_reason": "fetch_error"
                })
                continue

            # 4) pattern na tytule/HTML (wstępnie)
            matched = True
            if pat:
                matched = bool(pat.search(title)) or bool(pat.search(html_text))
                if not matched:
                    _dbg_write(debug_cfg, {
                        "query": term, "url": link, "title": title, "domain": dom,
                        "passed_domain": 1, "passed_url_regex": 1, "fetched": 1,
                        "matched_pattern": 0, "used_js": 0, "price_pln": "",
                        "filtered_out_reason": "pattern_no_match_yet"
                    })
                    # nie odrzucamy jeszcze – spróbujemy JSON-LD/JS

            # 5) dostępność
            if require_in_stock and out_words:
                low = html_text.lower()
                if any(w in low for w in out_words):
                    _dbg_write(debug_cfg, {
                        "query": term, "url": link, "title": title, "domain": dom,
                        "passed_domain": 1, "passed_url_regex": 1, "fetched": 1,
                        "matched_pattern": int(matched), "used_js": 0, "price_pln": "",
                        "filtered_out_reason": "out_of_stock_marker"
                    })
                    continue

            # 6) cena: JSON-LD → regex
            price = _extract_from_jsonld(html_text, rates) or _extract_price_regex(html_text, rates)

            # 7) jeśli brak ceny, a JS włączony – spróbuj renderu (z limitem)
            used_js = 0
            rendered = None
            if price is None and enable_js:
                if (not js_domains_wl) or any(dom.endswith(w) for w in js_domains_wl):
                    if _RENDER_COUNT < max_js:
                        rendered = _render_and_get_html(link, nav_timeout_ms, wait_until)
                        _RENDER_COUNT += 1
                        if rendered:
                            used_js = 1
                            # pattern po renderze
                            if pat and not matched:
                                matched = bool(pat.search(title)) or bool(pat.search(rendered))
                            # dostępność po renderze
                            if require_in_stock and out_words:
                                low2 = rendered.lower()
                                if any(w in low2 for w in out_words):
                                    price = None
                                else:
                                    price = _extract_from_jsonld(rendered, rates) or _extract_price_regex(rendered, rates)
                            else:
                                price = _extract_from_jsonld(rendered, rates) or _extract_price_regex(rendered, rates)

            # 8) jeśli pattern finalnie nie pasuje – odrzuć
            if pat and not matched:
                _dbg_write(debug_cfg, {
                    "query": term, "url": link, "title": title, "domain": dom,
                    "passed_domain": 1, "passed_url_regex": 1, "fetched": 1,
                    "matched_pattern": 0, "used_js": used_js, "price_pln": price or "",
                    "filtered_out_reason": "pattern_final_no_match"
                })
                continue

            # 9) zaakceptowany wynik
            _dbg_write(debug_cfg, {
                "query": term, "url": link, "title": title, "domain": dom,
                "passed_domain": 1, "passed_url_regex": 1, "fetched": 1,
                "matched_pattern": 1 if (not pat or matched) else 0,
                "used_js": used_js, "price_pln": price if price is not None else "",
                "filtered_out_reason": "accepted"
            })

            items.append({
                "store": "web",
                "title": html.unescape(title),
                "url": link,
                "price_pln": price
            })

            fetched += 1
            if fetched >= max_results:
                break

        start += 10
        time.sleep(1.0)

    # deduplikacja po URL
    uniq = {}
    for o in items:
        uniq[o["url"]] = o
    return list(uniq.values())
