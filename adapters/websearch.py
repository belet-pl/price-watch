# adapters/websearch.py
import os
import re
import time
import html
import requests
from urllib.parse import urlparse

# Ceny w PLN / EUR / CZK
PRICE_RE = re.compile(
    r'(\d{1,5}(?:[ \xa0]?\d{3})*(?:[.,]\d{1,2})?)\s*(zł|pln|€|eur|kč|kc|czk)\b',
    re.IGNORECASE
)

def _domain(url: str) -> str:
    """Zwróć domenę bazową (np. x-kom.pl, reolink.com)."""
    try:
        host = urlparse(url).hostname or ""
        parts = host.split(".")
        # dla TLD 2-3 znakowych weź 2 ostatnie segmenty, w innym przypadku 3
        return ".".join(parts[-3:]) if len(parts) >= 3 and len(parts[-1]) <= 3 else ".".join(parts[-2:])
    except Exception:
        return ""

def _extract_price(text: str, rates: dict | None = None):
    """
    Zwraca *najniższą* cenę w PLN wyciągniętą z HTML (po konwersji walut).
    rates (config.yaml -> currency):
      - parse_eur: bool, parse_czk: bool
      - eur_to_pln: float, czk_to_pln: float
    """
    rates = rates or {}
    eur_to_pln = float(rates.get("eur_to_pln")) if rates.get("parse_eur") and rates.get("eur_to_pln") else None
    czk_to_pln = float(rates.get("czk_to_pln")) if rates.get("parse_czk") and rates.get("czk_to_pln") else None

    candidates = []
    for m in PRICE_RE.finditer(text):
        raw_num = m.group(1).replace("\xa0", " ").replace(" ", "").replace(",", ".")
        unit = (m.group(2) or "").lower()
        try:
            val = float(raw_num)
        except Exception:
            continue

        # Konwersje walut
        if unit in ("€", "eur"):
            if eur_to_pln:
                val *= eur_to_pln
            else:
                continue  # brak kursu EUR -> pomijamy
        elif unit in ("kč", "kc", "czk"):
            if czk_to_pln:
                val *= czk_to_pln
            else:
                continue  # brak kursu CZK -> pomijamy
        # zł/pln -> bez zmian

        candidates.append(val)

    return min(candidates) if candidates else None

def search(term: str, timeout: int = 10, ctx: dict | None = None):
    """
    Wyszukiwanie przez Google CSE.
    Env: GOOGLE_CSE_KEY, GOOGLE_CSE_CX
    ctx:
      - websearch: {
          region, max_results, site_whitelist, site_blacklist,
          url_whitelist_patterns, url_blacklist_patterns
        }
      - availability_keywords: { out_of_stock:[...] }
      - require_in_stock: bool
      - pattern: regex tytułu/HTML
      - currency: { parse_eur, parse_czk, eur_to_pln, czk_to_pln }
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

    out_words = [w.lower() for w in (ctx or {}).get("availability_keywords", {}).get("out_of_stock", [])]
    require_in_stock = bool((ctx or {}).get("require_in_stock", False))
    rates = (ctx or {}).get("currency", {})  # kursy walut

    # opcjonalny regex produktu
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

    # Preferuj Polskę (nie jest to twardy filtr, ale pomaga)
    params = {
        "key": key,
        "cx": cx,
        "q": term,
        "num": 10,       # per page (CSE limit)
        "hl": "pl",
        "gl": "pl",
        "safe": "off",
        "cr": "countryPL",
    }

    fetched = 0
    start = 1
    while fetched < max_results:
        params["start"] = start
        r = session.get("https://www.googleapis.com/customsearch/v1", params=params, timeout=timeout)
        r.raise_for_status()
        data = r.json()
        results = data.get("items", []) or []
        if not results:
            break

        for it in results:
            link = it.get("link") or ""
            title = it.get("title") or ""
            if not link:
                continue

            dom = _domain(link)

            # 1) Filtr domen (whitelist/blacklist przez endswith)
            if whitelist and all(not dom.endswith(w) for w in whitelist):
                continue
            if blacklist and any(dom.endswith(b) for b in blacklist):
                continue

            # 2) Filtry po ścieżce URL (np. wymagamy /pl/)
            if url_wl_patterns and not any(p.search(link) for p in url_wl_patterns):
                continue
            if url_bl_patterns and any(p.search(link) for p in url_bl_patterns):
                continue

            # 3) Pobierz stronę (pattern sprawdzimy też na HTML)
            try:
                pr = session.get(link, headers=headers, timeout=timeout)
                html_text = pr.text
            except Exception:
                continue

            # 4) Pattern: najpierw tytuł, jeśli nie pasuje—spróbuj na treści
            if pat:
                if not pat.search(title):
                    if not pat.search(html_text):
                        continue

            # 5) Filtrowanie dostępności
            if require_in_stock and out_words:
                low = html_text.lower()
                if any(w in low for w in out_words):
                    continue

            # 6) Cena (po konwersji walut)
            price = _extract_price(html_text, rates=rates)

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
        time.sleep(1.0)  # throttle

    # Deduplikacja po URL
    uniq = {}
    for o in items:
        uniq[o["url"]] = o
    return list(uniq.values())
