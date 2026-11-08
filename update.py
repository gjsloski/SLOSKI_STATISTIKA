#!/usr/bin/env python3
"""SloSki statistika: zbira uvrstitve slovenskih tekmovalcev v svetovnem pokalu (FIS).

Podprta sta alpsko smucanje (AL) in smucarski skoki (JP), samo posamicne tekme svetovnega pokala.
Nacini:
  --mode weekly    samo tekoca sezona (vsako nedeljo)
  --mode backfill  vse sezone od --from-season (privzeto 1992 = sezona 1991/92) do danes
Izhod: data/races.json, data/meta.json, data/state.json
"""
import argparse
import datetime
import json
import pathlib
import re
import sys
import time
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup, NavigableString, Tag

BASE = "https://www.fis-ski.com"
ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
UA = "Mozilla/5.0 (compatible; SloSkiStatistika/1.0)"
DELAY = 1.0

SPORTS = {
    "alpine": {"code": "AL", "path": "alpine-skiing", "label": "Alpsko smučanje"},
    "jumping": {"code": "JP", "path": "ski-jumping", "label": "Smučarski skoki"},
}

DISC = [
    ("Alpine Combined", "Kombinacija"),
    ("Combined", "Kombinacija"),
    ("Parallel", "Paralelni slalom"),
    ("Super G", "Superveleslalom"),
    ("Super-G", "Superveleslalom"),
    ("Giant Slalom", "Veleslalom"),
    ("Slalom", "Slalom"),
    ("Downhill", "Smuk"),
    ("Flying", "Letalnica"),
    ("Large Hill", "Velika skakalnica"),
    ("Normal Hill", "Normalna skakalnica"),
]

# FIS zapisuje imena brez strešic; tukaj so popravki za pogosta slovenska imena.
# Lahko jih dopolnis tudi v data/names.json (kljuc = ime kot ga izpise FIS ali FIS competitorid).
NAME_FIX = {
    "Zan Kranjec": "Žan Kranjec",
    "Stefan Hadalin": "Štefan Hadalin",
    "Ilka Stuhec": "Ilka Štuhec",
    "Marusa Ferk": "Maruša Ferk",
    "Ana Bucik Jogan": "Ana Bucik Jogan",
    "Anze Lanisek": "Anže Lanišek",
    "Ziga Jelar": "Žiga Jelar",
    "Nika Kriznar": "Nika Križnar",
    "Ursa Bogataj": "Urša Bogataj",
    "Spela Rogelj": "Špela Rogelj",
    "Jurij Tepes": "Jurij Tepeš",
    "Bor Pavlovcic": "Bor Pavlovčič",
    "Jernej Damjan": "Jernej Damjan",
    "Rok Justin": "Rok Justin",
    "Bostjan Kline": "Boštjan Kline",
    "Jure Kosir": "Jure Košir",
    "Mitja Kunc": "Mitja Kunc",
    "Tina Maze": "Tina Maze",
}

MONTH_RE = r"(January|February|March|April|May|June|July|August|September|October|November|December)"
DATE_RE = re.compile(r"^" + MONTH_RE + r" \d{1,2}, \d{4}$")
TITLE_RE = re.compile(r"^(Men|Women|Ladies|Mixed|Team)\b")
STOP_RE = re.compile(r"^(Did not|Did Not|Disqualified|Not qualified|DNF|DSQ|DNS|DNQ)", re.I)
SKIP_WORDS = ("Team", "Mixed", "Relay")


def log(*a):
    print(*a, flush=True)


# ----------------------------------------------------------------- HTTP
_session = requests.Session()
_session.headers.update({"User-Agent": UA, "Accept-Language": "en"})


def get(url, tries=4):
    for i in range(tries):
        try:
            r = _session.get(url, timeout=40)
            time.sleep(DELAY)
            if r.status_code == 200:
                return r.text
            log(f"  HTTP {r.status_code} za {url}")
            if r.status_code in (403, 429):
                time.sleep(15 * (i + 1))
        except requests.RequestException as e:
            log(f"  napaka {e} za {url}")
            time.sleep(5 * (i + 1))
    return None


# ----------------------------------------------------------------- pomozne
def season_label(d):
    y, m = int(d[:4]), int(d[5:7])
    s = y if m >= 8 else y - 1
    return f"{s}/{str(s + 1)[2:]}"


def season_code_label(code):
    return f"{code - 1}/{str(code)[2:]}"


def current_season_code(today):
    return today.year + 1 if today.month >= 7 else today.year


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def fix_name(name, cid, extra):
    if str(cid) in extra:
        return extra[str(cid)]
    if name in extra:
        return extra[name]
    return NAME_FIX.get(name, name)


# ----------------------------------------------------------------- razclenjevanje
def parse_calendar(html):
    """Vrne seznam (eventid, url) za dogodke svetovnega pokala (oznaka WC v vrstici)."""
    soup = BeautifulSoup(html, "lxml") if _has_lxml() else BeautifulSoup(html, "html.parser")
    events = {}
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "event-details" not in href or "eventid=" not in href:
            continue
        eid = parse_qs(urlparse(href).query).get("eventid", [None])[0]
        if not eid:
            continue
        e = events.setdefault(eid, {"url": urljoin(BASE, href), "text": []})
        e["text"].append(a.get_text(" ", strip=True))
    out = []
    for eid, e in events.items():
        if re.search(r"\bWC\b", " ".join(e["text"])):
            out.append((eid, e["url"]))
    return out


def parse_event(html, code):
    """Vrne seznam (raceid, url rezultatov) za vse tekme dogodka."""
    soup = BeautifulSoup(html, "lxml") if _has_lxml() else BeautifulSoup(html, "html.parser")
    seen, out = set(), []
    for a in soup.find_all("a", href=True):
        q = parse_qs(urlparse(a["href"]).query)
        rid = q.get("raceid", [None])[0]
        if rid and rid not in seen:
            seen.add(rid)
            out.append((rid, f"{BASE}/DB/general/results.html?sectorcode={code}&raceid={rid}"))
    return out


def _has_lxml():
    try:
        import lxml  # noqa: F401
        return True
    except ImportError:
        return False


def parse_row(text, href):
    tokens = text.split()
    if len(tokens) < 6 or not tokens[0].isdigit():
        return None
    k = 1
    while k < len(tokens) and tokens[k].isdigit():
        k += 1
    if k == 1:
        return None  # vrstica brez startne stevilke
    y = None
    for i in range(k, len(tokens) - 1):
        if re.fullmatch(r"(19|20)\d\d", tokens[i]) and re.fullmatch(r"[A-Z]{3}", tokens[i + 1]):
            y = i
            break
    if y is None:
        return None
    name_tokens = tokens[k:y]
    if not name_tokens:
        return None
    sur = []
    for t in name_tokens:
        if t == t.upper() and any(c.isalpha() for c in t):
            sur.append(t)
        else:
            break
    rest = name_tokens[len(sur):]
    given = rest[0] if rest else ""
    surname = " ".join(w.title() for w in sur)
    name = f"{given} {surname}".strip()
    cid = parse_qs(urlparse(href).query).get("competitorid", [None])[0]
    return {"cid": cid, "name": name, "rank": int(tokens[0]), "nat": tokens[y + 1]}


def parse_race(html, sport_key, raceid, extra_names):
    """Vrne (status, race). status: ok | skip | empty | fail"""
    cfg = SPORTS[sport_key]
    soup = BeautifulSoup(html, "lxml") if _has_lxml() else BeautifulSoup(html, "html.parser")
    lines = [l for l in soup.get_text("\n", strip=True).split("\n") if l]
    head = lines[:90]
    if not any("World Cup" in l for l in head):
        return "skip", None
    title = next((l for l in head if TITLE_RE.match(l) and len(l) < 90), None)
    date = None
    for l in head:
        if DATE_RE.match(l):
            date = datetime.datetime.strptime(l, "%B %d, %Y").strftime("%Y-%m-%d")
            break
    h1 = soup.find("h1")
    place = h1.get_text(" ", strip=True) if h1 else ""
    if not title or not date:
        return "fail", None
    if any(w in title for w in SKIP_WORDS):
        return "skip", None
    gender = "M" if title.startswith("Men") else "Ž"
    disc = None
    for key, sl in DISC:
        if key.lower() in title.lower():
            disc = sl
            break
    if disc is None:
        disc = re.sub(r"^(Men|Women|Ladies)['’]?s?\s*", "", title).strip() or title

    ranked, stopped = [], False
    for node in soup.descendants:
        if isinstance(node, NavigableString):
            s = str(node).strip()
            if s and ranked and not stopped and node.find_parent("a") is None and STOP_RE.match(s):
                stopped = True
            continue
        if isinstance(node, Tag) and node.name == "a" and not stopped:
            href = node.get("href", "")
            if "athlete-biography" not in href:
                continue
            row = parse_row(node.get_text(" ", strip=True), href)
            if row:
                ranked.append(row)
    if len(ranked) < 5:
        return "empty", None
    res = []
    for r in ranked:
        if r["nat"] == "SLO" and r["rank"] <= 30:
            item = {"n": fix_name(r["name"], r["cid"], extra_names), "r": r["rank"]}
            if r["cid"]:
                item["id"] = int(r["cid"])
            res.append(item)
    race = {
        "id": f"fis-{raceid}",
        "fis": int(raceid),
        "d": date,
        "place": re.sub(r"\s+", " ", place),
        "sport": cfg["label"],
        "disc": disc,
        "g": gender,
        "t": "posamicno",
        "res": sorted(res, key=lambda x: x["r"]),
    }
    return "ok", race


# ----------------------------------------------------------------- glavni tok
def save_all(races, state, meta, today):
    DATA.mkdir(exist_ok=True)
    rs = sorted(races.values(), key=lambda r: (r["d"], r["id"]))
    body = "[\n" + ",\n".join(json.dumps(r, ensure_ascii=False) for r in rs) + "\n]\n"
    (DATA / "races.json").write_text(body, encoding="utf-8")
    meta["updated"] = today.isoformat()
    nxt = today + datetime.timedelta(days=(6 - today.weekday()) % 7 or 7)
    meta["next"] = nxt.isoformat()
    (DATA / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    (DATA / "state.json").write_text(json.dumps(state, ensure_ascii=False) + "\n", encoding="utf-8")


def process_season(code, sport_key, races, state, meta, cur, extra_names, deadline):
    cfg = SPORTS[sport_key]
    label = season_code_label(code)
    cal_url = (f"{BASE}/DB/{cfg['path']}/calendar-results.html?sectorcode={cfg['code']}"
               f"&seasoncode={code}&categorycode=WC")
    html = get(cal_url)
    failures = 0
    if not html:
        log(f"  koledar ni dosegljiv: {cal_url}")
        return "delno"
    events = parse_calendar(html)
    log(f"{label} {cfg['label']}: {len(events)} dogodkov WC")
    if not events:
        return "delno"
    for eid, eurl in events:
        if time.time() > deadline:
            return None
        ehtml = get(eurl)
        if not ehtml:
            failures += 1
            continue
        for rid, rurl in parse_event(ehtml, cfg["code"]):
            key = f"{cfg['code']}:{rid}"
            if key in state["seen"]:
                continue
            rhtml = get(rurl)
            if not rhtml:
                failures += 1
                continue
            status, race = parse_race(rhtml, sport_key, rid, extra_names)
            if status == "ok":
                if race["res"]:
                    races[race["id"]] = race
                state["seen"][key] = "ok"
                log(f"  {race['d']} {race['place']} {race['disc']} {race['g']}: "
                    f"{', '.join(str(x['r']) + '. ' + x['n'] for x in race['res']) or '-'}")
            elif status == "skip":
                state["seen"][key] = "skip"
            elif status == "empty":
                if code < cur:
                    state["seen"][key] = "empty"
            else:
                failures += 1
                log(f"  NAPAKA razclenjevanja: {rurl}")
    if code == cur:
        return "v teku"
    return "popolno" if failures == 0 else "delno"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["weekly", "backfill"], default="weekly")
    ap.add_argument("--from-season", type=int, default=1992, help="koda sezone = koncno leto (1992 = 1991/92)")
    ap.add_argument("--sports", default="alpine,jumping")
    ap.add_argument("--max-minutes", type=int, default=330)
    ap.add_argument("--force", action="store_true", help="ponovno obdela tudi sezone, oznacene kot popolne")
    args = ap.parse_args()

    today = datetime.date.today()
    cur = current_season_code(today)
    races = {r["id"]: r for r in load_json(DATA / "races.json", [])}
    state = load_json(DATA / "state.json", {"seen": {}})
    state.setdefault("seen", {})
    meta = load_json(DATA / "meta.json", {"coverage": {}})
    meta.setdefault("coverage", {})
    extra_names = load_json(DATA / "names.json", {})
    deadline = time.time() + args.max_minutes * 60
    seasons = [cur] if args.mode == "weekly" else list(range(cur, args.from_season - 1, -1))
    sports = [s for s in args.sports.split(",") if s in SPORTS]

    for code in seasons:
        label = season_code_label(code)
        for sk in sports:
            cov = meta["coverage"].setdefault(label, {})
            if (not args.force and code < cur and cov.get(sk) == "popolno"):
                continue
            result = process_season(code, sk, races, state, meta, cur, extra_names, deadline)
            if result is None:
                log("Casovna omejitev dosezena: shranjujem in koncam. Ponovni zagon nadaljuje.")
                save_all(races, state, meta, today)
                return 0
            cov[sk] = result
            save_all(races, state, meta, today)
    save_all(races, state, meta, today)
    log(f"Konec. Tekem s slovenskimi uvrstitvami: {len(races)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
