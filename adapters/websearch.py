# adapters/websearch.py
import os
import re
import time
import html
import requests
from urllib.parse import urlparse

PRICE_RE = re.compile(r'(\d{1,5}(?:[ \xa0]?\d{3})*(?:[.,]\d{2})?)\s*(?:zł|pln)\b', re.IGNORECASE)

def _domain(url: str) -> str:
    try:
        host = urlparse(url).hostname or ""
        # zrzucamy subdomeny do postaci "domena.tld"
        parts = host.split(".")
        return ".".join(parts[-3:]) if len(parts) >= 3 and len(parts[-1]) <= 3 else ".".join(parts[-2:])
    except Exception:
        return ""

def _extract_price(text: str):
    # znajdź najniższą cenę wyglądającą na PLN
    candidates = []
    for m in PRICE_RE.finditer(text):
        raw = m.group(1).replace("\xa0", " ").replace(" ", "")
        raw = raw.replace(",", ".")
        try:
            val = float(raw)
            candidates.append(val)
        except Exception:
            pass
    return min(candidates) if candidates else None

def search(term: str, timeout: int = 10, ctx: dict | None = None):
    """
    Wyszukiwanie w sieci przez Google CSE.
    Wymaga zmiennych środowiskowych:
      - GOOGLE_CSE_KEY  (API key)
      - GOOGLE_CSE_CX   (Custom Search Engine ID)
    ctx powinien zawierać:
      - websearch: {engine, region, max_results, site_whitelist, site_blacklist}
      - availability_keywords: {in_stock:[], out_of_stock:[...]}
      - require_in_stock: bool
      - pattern: opcjonalny regex string do filtrowania tytułu
    """
    key = os.getenv("GOOGLE_CSE_KEY")
    cx  = os.getenv("GOOGLE_CSE_CX")
    if not key or not cx:
        # Bez kluczy zwracamy pustą listę, żeby nie psuć cyklu
        return []

    ws = (ctx or {}).get("websearch", {})
    region = ws.get("region", "pl-PL")
    max_results = int(ws.get("max_results", 10))
    whitelist = set((ws.get("site_whitelist") or []))
    blacklist = set((ws.get("site_blacklist") or []))

    out_words = [w.lower() for w in (ctx or {}).get("availability_keywords", {}).get("out_of_stock", [])]
    require_in_stock = bool((ctx or {}).get("require_in_stock", False))

    # Kompilujemy regex (jeśli podany) do filtrowania tytułów
    pat = None
    pat_str = (ctx or {}).get("pattern")
    if pat_str:
        try:
            pat = re.compile(pat_str)
        except Exception:
            pat = None

    items = []
    session = requests.Session()
    headers = {"User-Agent": "Mozilla/5.0"}

    # Google CSE – endpoint
    # Doc: https://developers.google.com/custom-search/v1/reference/rest/v1/cse/list
    # Parametry regionalne: hl, gl (tu użyjemy hl=pl, gl=pl dla PL)
    params = {
        "key": key,
        "cx": cx,
        "q": term,
        "num": 10,  # per page
        "hl": "pl",
        "gl": "pl",
        "safe": "off",
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

            # whitelist/blacklist domen
            if whitelist and all(not dom.endswith(w) for w in whitelist):
                continue
            if blacklist and any(dom.endswith(b) for b in blacklist):
                continue

            # opcjonalne filtrowanie regexem po tytule wyniku
            if pat and not pat.search(title):
                # jeśli sam tytuł nie pasuje, i tak spróbujemy stronę – ale żeby ograniczyć koszty
                # możesz zakomentować tę linię, by zawsze sprawdzać stronę:
                continue

            # pobierz stronę, by spróbować wyłuskać cenę i dostępność
            try:
                pr = session.get(link, headers=headers, timeout=timeout)
                html_text = pr.text
            except Exception:
                continue

            # szybkie sprawdzenie dostępności
            if require_in_stock and out_words:
                low = html_text.lower()
                if any(w in low for w in out_words):
                    continue

            price = _extract_price(html_text)
            items.append({
                "store": "web",
                "title": html.unescape(title),
                "url": link,
                "price_pln": price
            })

            fetched += 1
            if fetched >= max_results:
                break

        # Stronicowanie CSE – zwiększamy start o 10
        start += 10

        # delikatny throttle
        time.sleep(1.0)

    # deduplikacja po URL
    uniq = {}
    for o in items:
        uniq[o["url"]] = o
    return list(uniq.values())
