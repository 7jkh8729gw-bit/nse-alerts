"""Catalyst news from the exchanges (NSE / BSE corporate announcements).

News tells us WHERE to look; price and volume (catalyst.py) decide WHEN to buy. We do not try to judge
whether results are 'good' from the text: the market's reaction does that.

  python -m nse_alerts.news --test      # connectivity diagnostic: run it from GitHub (action: newstest)
  python -m nse_alerts.news --collect   # silently refresh data/catalyst_watch.json (used at 06:05)
"""
import argparse
import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta

import pandas as pd
import requests

from . import config

log = logging.getLogger("news")
WATCH_FILE = os.path.join(config.DATA_DIR, "catalyst_watch.json")
NAMES_FILE = os.path.join(config.DATA_DIR, "names.json")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36")
NSE_HOME = "https://www.nseindia.com"
NSE_API = "https://www.nseindia.com/api/corporate-announcements"
BSE_API = "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"

# ------------------------------------------------------------------ classification
_NOISE = re.compile(
    r"newspaper|trading window|investor (presentation|meet|conference|call)|analyst|earnings call|conference call|"
    r"schedule of|intimation of (board meeting|analyst|investor|record|schedule|trading window|closure)|"
    r"shareholding pattern|certificate|loss of share|duplicate|record date|\bagm\b|e-?voting|postal ballot|"
    r"credit rating|appointment|resignation|cessation|transcript|clarification|spurt in volume", re.I)
_CANCEL = re.compile(r"cancel|terminat|withdraw|rejected|penalt", re.I)
_RULES = [
    ("Big order", r"\b(bagging|receiving|receipt|award(ed|ing)?|bagged|secures?|secured|wins?|won|lands?)\b.{0,80}"
                  r"\b(orders?|contracts?|projects?|tender)\b|\bletter of (intent|award)\b|\bwork order\b"),
    ("Results", r"\bfinancial results?\b|\bresults? for the (quarter|half[- ]year|year)\b|\bquarter(ly)? results?\b"),
    ("Bonus / split", r"\bbonus (issue|shares?)\b|\bissue of bonus\b|\bstock split\b|\bsub-?division\b|"
                      r"\bsplit of (equity )?shares?\b"),
    ("Buyback", r"\bbuy[- ]?back\b"),
    ("Merger / deal", r"\b(acquisition|acquires?|acquiring|amalgamation|merger|demerger|scheme of arrangement|"
                      r"takeover|joint venture)\b"),
    ("Approval", r"\b(usfda|fda approval|drug approval|regulatory approval|nclt approval|cci approval)\b"),
]
_RULES = [(n, re.compile(rx, re.I)) for n, rx in _RULES]
BASE_SCORE = {"Results": 3, "Big order": 3, "Buyback": 3, "Bonus / split": 2, "Merger / deal": 2, "Approval": 2}
_AMOUNT = re.compile(r"(?:rs\.?|₹|inr)\s*([\d,]+(?:\.\d+)?)\s*(crore|cr|lakh|million|mn|billion|bn)\b", re.I)


def order_value_cr(text: str):
    """Largest rupee amount mentioned, in crore (None if none found)."""
    best = None
    for num, unit in _AMOUNT.findall(text or ""):
        v = float(num.replace(",", ""))
        v *= {"crore": 1, "cr": 1, "lakh": 0.01, "million": 0.1, "mn": 0.1, "billion": 100, "bn": 100}[unit.lower()]
        best = max(best or 0, v)
    return best


def classify_announcement(subject: str, text: str = ""):
    """(category, amount_cr) for a catalyst announcement, else None."""
    subject, text = subject or "", text or ""
    if _NOISE.search(subject):
        return None
    blob = f"{subject} {text}"[:1500]
    for name, rx in _RULES:
        if rx.search(blob):
            if name == "Big order" and _CANCEL.search(blob):
                return None
            return name, order_value_cr(blob)
    return None


def score_announcement(category: str, amount_cr, ts: datetime, now: datetime) -> float:
    s = BASE_SCORE[category]
    if amount_cr and amount_cr >= 100:
        s += 1
    if ts and (now - ts) <= timedelta(hours=18):
        s += 1                                      # fresh
    return float(s)


# ------------------------------------------------------------------ time helpers
def last_close(now: datetime) -> datetime:
    """Previous trading day's 15:30 IST (weekends skipped; holidays not known)."""
    d = now.date()
    if now.time() < datetime(2000, 1, 1, 15, 30).time() or now.weekday() >= 5:
        d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return datetime(d.year, d.month, d.day, 15, 30, tzinfo=config.IST)


def _parse_dt(value) -> datetime:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    v = str(value).strip()
    try:                                                   # ISO strings, with or without a UTC offset
        d = datetime.fromisoformat(v)
        return d if d.tzinfo else d.replace(tzinfo=config.IST)
    except ValueError:
        pass
    v = v.replace("T", " ")[:26]
    for fmt in ("%d-%b-%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%d-%m-%Y %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(v, fmt).replace(tzinfo=config.IST)
        except ValueError:
            continue
    return None


# ------------------------------------------------------------------ fetchers
def fetch_nse(since: datetime, now: datetime, timeout: float = 20) -> list:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "application/json, text/plain, */*",
                      "Accept-Language": "en-US,en;q=0.9", "Referer": NSE_HOME + "/companies-listing/corporate-filings-announcements"})
    s.get(NSE_HOME, timeout=timeout)                                   # cookies
    r = s.get(NSE_API, params={"index": "equities", "from_date": since.strftime("%d-%m-%Y"),
                               "to_date": now.strftime("%d-%m-%Y")}, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    items = data if isinstance(data, list) else (data.get("data") or [])
    out = []
    for it in items:
        out.append(dict(source="NSE", symbol=str(it.get("symbol") or "").strip().upper() or None,
                        name=str(it.get("sm_name") or ""), subject=str(it.get("desc") or "").strip(),
                        text=str(it.get("attchmntText") or "").strip(),
                        ts=_parse_dt(it.get("an_dt") or it.get("sort_date"))))
    return out


def fetch_bse(since: datetime, now: datetime, max_pages: int = 40, timeout: float = 20) -> list:
    headers = {"User-Agent": UA, "Accept": "application/json, text/plain, */*",
               "Referer": "https://www.bseindia.com/", "Origin": "https://www.bseindia.com"}
    out = []
    for page in range(1, max_pages + 1):
        r = requests.get(BSE_API, headers=headers, timeout=timeout, params=dict(
            pageno=page, strCat="-1", strPrevDate=since.strftime("%Y%m%d"), strScrip="", strSearch="P",
            strToDate=now.strftime("%Y%m%d"), strType="C", subcategory="-1"))
        r.raise_for_status()
        rows = (r.json() or {}).get("Table") or []
        if not rows:
            break
        old = False
        for it in rows:
            ts = _parse_dt(it.get("NEWS_DT") or it.get("DT_TM") or it.get("DissemDT"))
            if ts and ts < since:
                old = True
                continue
            out.append(dict(source="BSE", symbol=None, name=str(it.get("SLONGNAME") or ""),
                            subject=str(it.get("NEWSSUB") or it.get("HEADLINE") or "").strip(),
                            text=f"{it.get('HEADLINE') or ''} {it.get('MORE') or ''}".strip(), ts=ts))
        if old:
            break
    return out


# ------------------------------------------------------------------ company name -> NSE symbol (for BSE items)
_SUFFIX = re.compile(r"\b(LIMITED|LTD|PRIVATE|PVT|THE|COMPANY|CO)\b")


def normalize_name(name: str) -> str:
    n = (name or "").upper().replace("&", " AND ")
    n = _SUFFIX.sub(" ", re.sub(r"[.,\-()/']", " ", n))
    return re.sub(r"[^A-Z0-9]", "", n)


def load_name_map(force: bool = False) -> dict:
    """{normalised company name: NSE symbol} from Upstox's instrument file (cached daily)."""
    if not force and os.path.exists(NAMES_FILE) and \
            datetime.fromtimestamp(os.path.getmtime(NAMES_FILE)).date() == datetime.now().date():
        return json.load(open(NAMES_FILE))
    import gzip, io
    r = requests.get(config.UPSTOX_INSTRUMENTS_URL, timeout=120)
    r.raise_for_status()
    raw = r.content
    try:
        raw = gzip.decompress(raw)
    except OSError:
        pass
    df = pd.read_csv(io.BytesIO(raw))
    df = df[df["instrument_key"].astype(str).str.startswith("NSE_EQ|")]
    if "instrument_type" in df.columns:
        df = df[df["instrument_type"].astype(str).str.upper() == "EQUITY"]
    m = {normalize_name(n): str(s).upper() for n, s in zip(df["name"], df["tradingsymbol"]) if isinstance(n, str)}
    json.dump(m, open(NAMES_FILE, "w"))
    return m


# ------------------------------------------------------------------ watchlist
def load_watch(path: str = None) -> dict:
    try:
        return json.load(open(path or WATCH_FILE)).get("items", {})
    except Exception:
        return {}


def save_watch(items: dict, now: datetime, path: str = None):
    path = path or WATCH_FILE
    tmp = path + ".tmp"
    json.dump({"updated": now.isoformat(timespec="seconds"), "items": items}, open(tmp, "w"), indent=1)
    os.replace(tmp, path)


def build_watch(anns: list, name_map: dict, now: datetime, since: datetime, existing: dict = None) -> tuple:
    """Merge announcements into the watchlist. Returns (items, stats). Pure: no network, no files."""
    items = {s: dict(v) for s, v in (existing or {}).items()
             if (_parse_dt(v.get("ts")) or now) >= since}                       # drop expired entries
    stats = dict(total=len(anns), catalysts=0, unmapped=0, added=[])
    for a in anns:
        ts = a.get("ts")
        if ts and ts < since:
            continue
        c = classify_announcement(a.get("subject"), a.get("text"))
        if not c:
            continue
        stats["catalysts"] += 1
        sym = a.get("symbol") or name_map.get(normalize_name(a.get("name")))
        if not sym:
            stats["unmapped"] += 1
            continue
        cat, amount = c
        sc = score_announcement(cat, amount, ts, now)
        cur = items.get(sym)
        entry = dict(cats=sorted({*(cur or {}).get("cats", []), cat}), subject=a.get("subject", "")[:140],
                     detail=(a.get("text") or "")[:200], ts=(ts or now).isoformat(timespec="seconds"),
                     score=max(sc, (cur or {}).get("score", 0)), source=a.get("source", ""),
                     amount_cr=amount or (cur or {}).get("amount_cr"))
        if cur is None:
            stats["added"].append(sym)
        elif sc <= cur.get("score", 0):
            entry.update(subject=cur["subject"], detail=cur.get("detail", ""), ts=cur["ts"])
        items[sym] = entry
    return items, stats


def collect(now: datetime = None, fetchers=None, name_map=None, path: str = None) -> dict:
    """Fetch announcements (NSE first, BSE as fallback), merge into the watchlist file."""
    now = now or config.now_ist()
    since = last_close(now)
    fetchers = fetchers or [("NSE", fetch_nse), ("BSE", fetch_bse)]
    anns, used, errors = [], None, {}
    for name, fn in fetchers:
        try:
            got = fn(since, now)
            if got:
                anns, used = got, name
                break
            errors[name] = "returned 0 announcements"
        except Exception as e:
            errors[name] = f"{type(e).__name__}: {str(e)[:120]}"
    if used is None:
        return dict(ok=False, errors=errors, watch=load_watch(path), stats={})
    if name_map is None and any(not a.get("symbol") for a in anns):
        try:
            name_map = load_name_map()
        except Exception as e:
            errors["names"] = str(e)[:120]
            name_map = {}
    items, stats = build_watch(anns, name_map or {}, now, since, load_watch(path))
    save_watch(items, now, path)
    return dict(ok=True, source=used, errors=errors, watch=items, stats=stats)


# ------------------------------------------------------------------ diagnostic
def diagnose() -> int:
    now = config.now_ist()
    since = max(last_close(now), now - timedelta(hours=40))
    print(f"Looking for announcements since {since:%Y-%m-%d %H:%M} IST (now {now:%H:%M})\n")
    ok_any = False
    for name, fn in (("NSE", fetch_nse), ("BSE", fetch_bse)):
        print(f"=== {name} ===")
        try:
            anns = fn(since, now)
        except Exception as e:
            resp = getattr(e, "response", None)
            print(f"FAILED: {type(e).__name__}: {str(e)[:200]}" + (f" (HTTP {resp.status_code})" if resp is not None else ""))
            print("-> this feed is NOT reachable from here\n")
            continue
        ok_any = ok_any or bool(anns)
        print(f"reachable. announcements returned: {len(anns)}")
        by_cat, examples = {}, {}
        for a in anns:
            c = classify_announcement(a["subject"], a["text"])
            if c:
                by_cat[c[0]] = by_cat.get(c[0], 0) + 1
                if len(examples.setdefault(c[0], [])) < 4:
                    examples[c[0]].append(a)
        print("catalysts by type:", by_cat or "none")
        for cat, rows in examples.items():          # a few real examples of each type, to judge precision
            print(f"  -- {cat} --")
            for a in rows:
                who = a.get("symbol") or a.get("name")
                when = a["ts"].strftime("%d %b %H:%M") if a.get("ts") else ""
                snippet = " ".join((a.get("text") or "").split())[:150]
                print(f"  {who} | {a['subject'][:60]} | {when}")
                if snippet:
                    print(f"      \"{snippet}\"")
        if anns:
            print("fields in first record:", sorted(k for k, v in anns[0].items() if v))
        if name == "BSE" and anns:
            try:
                nm = load_name_map(force=True)
                mapped = sum(1 for a in anns if nm.get(normalize_name(a["name"])))
                print(f"company names matched to NSE symbols: {mapped}/{len(anns)}")
            except Exception as e:
                print("name map failed:", e)
        print()
    print("RESULT:", "at least one feed works" if ok_any else "NO feed returned data from here")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--collect", action="store_true")
    a = ap.parse_args()
    if a.test:
        sys.exit(diagnose())
    if a.collect:
        res = collect()
        if res["ok"]:
            st = res["stats"]
            log.info("news source %s: %d announcements, %d catalysts, %d unmapped, %d watched",
                     res["source"], st["total"], st["catalysts"], st["unmapped"], len(res["watch"]))
        else:
            log.warning("NO news feed reachable: %s", res["errors"])
            from . import notify
            notify.send("⚠️ News feeds unreachable today: news-based alerts are off. The breakout engine is unaffected.")
        sys.exit(0)
    ap.print_help()
