# Agentic Price Watch (PL)

Lekki agent do monitorowania cen produktów w polskich sklepach internetowych i powiadamiania, gdy cena spadnie poniżej progu.

## Funkcje
- Konfigurowany przez `config.yaml` (lista produktów, progi cenowe, sklepy, częstotliwość).
- Modularne adaptery sklepów (`adapters/`), łatwe do rozszerzenia.
- Harmonogram (APScheduler) do cyklicznych sprawdzeń.
- SQLite (`offers.db`) do historii ofert i deduplikacji.
- Powiadomienia: e‑mail (SMTP) i Telegram Bot (opcjonalnie webhook/URL).
- Gotowe do uruchomienia lokalnie, w Dockerze i przez GitHub Actions (cron).

## Szybki start
1. **Zainstaluj zależności:**
   ```bash
   pip install -r requirements.txt
   ```
2. **Skonfiguruj plik** `config.yaml`:
   - Uzupełnij listę produktów (frazy/regex), próg `max_price_pln`, sklepy do monitorowania.
   - Ustaw dane SMTP (jeśli chcesz e‑mail) lub token Telegram bota.
3. **Uruchom:**
   ```bash
   python main.py
   ```

## Dodawanie sklepów
Dodaj nowy plik w `adapters/` z funkcją `search(term: str) -> list[dict]` zwracającą:
```python
[{
  "store": "x-kom",
  "title": "Nazwa produktu",
  "price_pln": 1999.00,
  "url": "https://...",
  "sku": "opcjonalnie",
}]
```
Zobacz przykłady: `adapters/xkom.py`, `adapters/rtv_euro_agd.py`.

> **Uwaga prawna/etyczna:** sprawdzaj i respektuj regulaminy/robots.txt serwisów. Preferuj oficjalne API/RSS, ogranicz częstotliwość zapytań (rozsądny `rate limit`).

## Uruchomienie w Dockerze
```bash
docker build -t price-watch .
docker run --rm -v $(pwd):/app price-watch
```

## GitHub Actions (cron)
Plik workflow w `.github/workflows/price_watch.yml` uruchamia agenta cyklicznie (np. co 2 godziny).

---

Made for: szybki PoC agentic AI pod monitoring cen w PL. 
