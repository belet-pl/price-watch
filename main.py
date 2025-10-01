import os
import sqlite3
import time
import re
import yaml
import logging
from pathlib import Path
from importlib import import_module
from datetime import datetime, timezone

# from notifications.email_notifier import EmailNotifier
# from notifications.telegram_notifier import TelegramNotifier

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

BASE = Path(__file__).parent
DB_PATH = BASE / "offers.db"

def ensure_db():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS offers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            store TEXT,
            title TEXT,
            url TEXT,
            price_pln REAL,
            found_at TEXT,
            UNIQUE(store, url, price_pln, found_at)
        )
    """)
    con.commit()
    con.close()

def load_config():
    with open(BASE / "config.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

def normalize_price(p):
    if p is None:
        return None
    # zamień przecinki itp. i wyciągnij liczby
    s = str(p).replace("\xa0", " ").replace(",", ".")
    m = re.search(r"(\d+(?:\.\d{1,2})?)", s)
    return float(m.group(1)) if m else None

def run_once(cfg):
    products = cfg.get("products", [])
    politeness = cfg.get("politeness", {})
    delay = politeness.get("per_store_delay_seconds", 5)

    # globalny kontekst do websearch / filtrów dostępności
    ctx_global = {
        "websearch": cfg.get("websearch", {}),
        "availability_keywords": cfg.get("availability_keywords", {"in_stock": [], "out_of_stock": []}),
        "require_in_stock": bool(cfg.get("require_in_stock", False)),
    }

    # powiadomienia (na razie wyłączone w Twoim configu)
    email_cfg = cfg.get("notifications", {}).get("email", {})
    tel_cfg = cfg.get("notifications", {}).get("telegram", {})

    email_notifier = EmailNotifier(email_cfg) if email_cfg.get("enabled") else None
    tg_notifier = TelegramNotifier(tel_cfg) if tel_cfg.get("enabled") else None

    for p in products:
        name = p["name"]
        max_price = p["max_price_pln"]
        stores = p.get("stores", [])
        pattern = p.get("pattern")  # regex per produkt

        logging.info(f"Sprawdzam: {name} (<= {max_price} PLN) w {stores}")

        for store in stores:
            try:
                mod = import_module(f"adapters.{store}")
            except ModuleNotFoundError:
                logging.warning(f"Brak adaptera sklepu: {store}")
                continue

            try:
                # przekazujemy ctx z patternem (jeśli jest)
                ctx = dict(ctx_global)
                if pattern:
                    ctx["pattern"] = pattern
                # NOWY podpis: adapter może (ale nie musi) przyjąć ctx=
                offers = mod.search(name, timeout=cfg.get("politeness", {}).get("request_timeout_seconds", 10), ctx=ctx)
            except TypeError:
                # starsze adaptery bez ctx – fallback
                offers = mod.search(name, timeout=cfg.get("politeness", {}).get("request_timeout_seconds", 10))
            except Exception as e:
                logging.exception(f"Błąd pobierania z {store}: {e}")
                offers = []

            # zapis do DB i prog cenowy
            con = sqlite3.connect(DB_PATH)
            cur = con.cursor()
            for o in offers:
                price = normalize_price(o.get("price_pln"))
                if price is None:
                    continue
                row = (o.get("store"), o.get("title"), o.get("url"), price, datetime.now(timezone.utc).isoformat())
                try:
                    cur.execute("INSERT OR IGNORE INTO offers(store,title,url,price_pln,found_at) VALUES (?,?,?,?,?)", row)
                except Exception as e:
                    logging.debug(f"Insert ignore failed: {e}")
            con.commit()
            con.close()

            # notyfikacje (u Ciebie wyłączone, ale zostawiamy logi)
            good = [o for o in offers if normalize_price(o.get("price_pln")) is not None and normalize_price(o.get("price_pln")) <= max_price]
            if good:
                lines = [f"✅ {g['store']}: {g['title']} — {g['price_pln']} PLN\n{g['url']}" for g in good]
                message = f"Znaleziono ofertę ≤ {max_price} PLN dla: {name}\n\n" + "\n\n".join(lines)
                logging.info(message)
                if email_notifier:
                    email_notifier.send("Price Watch: znaleziono okazję", message)
                if tg_notifier:
                    tg_notifier.send(message)

            time.sleep(delay)

if __name__ == "__main__":
    ensure_db()
    cfg = load_config()
    if os.environ.get("RUN_ONCE", "").lower() in ("1", "true", "yes"):
        run_once(cfg)
    else:
        from apscheduler.schedulers.blocking import BlockingScheduler
        from dateutil.tz import tzlocal
        from datetime import datetime
        freq = int(cfg.get("frequency_minutes", 60))
        sched = BlockingScheduler(timezone=str(tzlocal()))
        sched.add_job(run_once, "interval", minutes=freq, args=[cfg], next_run_time=datetime.now())
        logging.info(f"Start agenta. Częstotliwość: co {freq} min.")
        try:
            sched.start()
        except (KeyboardInterrupt, SystemExit):
            logging.info("Zatrzymano.")

