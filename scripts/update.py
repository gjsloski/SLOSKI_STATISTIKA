#!/usr/bin/env python3
"""SloSki statistika: zbira uvrstitve slovenskih tekmovalcev v svetovnem pokalu (FIS).

Zimska sezona: alpsko smucanje (AL) in smucarski skoki (JP), posamicne tekme svetovnega pokala (WC).
Poletna sezona: FIS Grand Prix (GP) v smucarskih skokih (JP) in nordijski kombinaciji (NK), od leta 2026 naprej.
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
    "nordic": {"code": "NK", "path": "nordic-combined", "label": "Nordijska kombinacija"},
}
WINTER_SPORTS = ["alpine", "jumping"]
SUMMER_SPORTS = ["jumping", "nordic"]
FIRST_SEASON = 1992  # prva sezona, ki jo zbiramo (1991/92)
SUMMER_FROM = 2027  # koda sezone FIS (2027 = julij 2026 - junij 2027), torej poletje 2026

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
    ("Gundersen", "Gundersen"),
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
SKIP_WORDS = ("Team", "Mixed", "Relay", "Standings")
TYPE_RE = re.compile(r"\d{1,2}\.\d{1,2}\.\d{4}\s*-\s*[^|]{3,90}\|\s*([A-Za-z]{2,6})\b")
# oznake vrste tekme na FIS: WC = tekma, TRA = trening, COM = etapa kombinacije, kvalifikacije in poskusne serije
CODES_SEEN = {}
BAD_TYPES = {"TRA", "COM", "QUA", "QUAL", "QUF", "Q", "TRI", "TRL", "PRO", "PCR", "TRN", "FIS", "COC"}
QUAL_RE = re.compile(r"\b(Qualification|Qualifying|Training|Trial|Prologue)\b", re.I)


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
def parse_calendar(html, cat="WC"):
    """Vrne seznam (eventid, url) za dogodke dane kategorije (WC ali GP) po oznaki v vrstici."""
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
        if re.search(r"\b" + cat + r"\b", " ".join(e["text"])):
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


def parse_race(html, sport_key, raceid, extra_names, cat="WC"):
    """Vrne (status, race). status: ok | skip | empty | fail"""
    cfg = SPORTS[sport_key]
    soup = BeautifulSoup(html, "lxml") if _has_lxml() else BeautifulSoup(html, "html.parser")
    lines = [l for l in soup.get_text("\n", strip=True).split("\n") if l]
    head = lines[:90]
    # Nekatere strani (raceid) so STARTNE LISTE: prva stolpca sta startna stevilka in vrstni red starta, ne uvrstitev
    # (npr. 'FIS | Alpine Skiing Start List - Bormio (ITA) 2007/2008'). Prave rezultate prepozna glava tabele 'Rank'.
    # Taksnih strani ne stejemo.
    has_rank_header = any(l.strip().lower() == "rank" for l in lines)
    if not has_rank_header and any("start list" in l.lower() for l in lines[:3]):
        CODES_SEEN[(cat, "STARTLIST")] = CODES_SEEN.get((cat, "STARTLIST"), 0) + 1
        return "skip", None
    needle = "World Cup" if cat == "WC" else "Grand Prix"
    if not any(needle.lower() in l.lower() for l in head):
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
    # Stran vsebuje seznam VSEH sej prireditve ("dd.mm.yyyy - Naslov | oznaka"). Nasa vrstica je tista, ki ima
    # enak datum in tocno enak naslov kot stran sama (kvalifikacije imajo v naslovu se "Qualification").
    own_code = None
    ddmm = datetime.datetime.strptime(date, "%Y-%m-%d").strftime("%d.%m.%Y")
    norm = lambda t: re.sub(r"[^a-z0-9]+", "", t.lower())
    for m in TYPE_RE.finditer(" ".join(head)):
        mm = re.match(r"(\d{1,2}\.\d{1,2}\.\d{4})\s*-\s*(.+?)\s*\|", m.group(0))
        if mm and mm.group(1) == ddmm and norm(mm.group(2)) == norm(title):
            own_code = m.group(1).upper()
            break
    if own_code:
        CODES_SEEN[(cat, own_code)] = CODES_SEEN.get((cat, own_code), 0) + 1
        if own_code in BAD_TYPES or own_code.startswith(("QU", "TR")):
            return "skip", None
    # kvalifikacije, treningi in poskusne serije niso tekme za uvrstitve
    ti = head.index(title)
    if any(QUAL_RE.search(l) for l in head[ti:ti + 3] if len(l) < 90):
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
        "id": f"{cfg['code'].lower()}-{raceid}",
        "fis": int(raceid),
        "d": date,
        "place": re.sub(r"\s+", " ", place),
        "sport": cfg["label"],
        "disc": disc,
        "g": gender,
        "t": "posamicno",
        "res": sorted(res, key=lambda x: x["r"]),
    }
    if cat == "GP":
        race["summer"] = True
    return "ok", race





def refilter(races, state, extra_names, deadline):
    """Ponovno preveri ze shranjene tekme (brez novega zbiranja) in odstrani tiste, ki jih filter zdaj zavrne."""
    by_label = {cfg["label"]: (k, cfg) for k, cfg in SPORTS.items()}
    done = set(state.setdefault("refiltered", []))
    removed = 0
    todo = [r for r in races.values() if r["id"] not in done]
    log(f"Ponovni pregled: {len(todo)} tekem")
    for i, r in enumerate(sorted(todo, key=lambda r: r["d"])):
        if time.time() > deadline:
            log("Casovna omejitev: ponovni pregled se nadaljuje ob naslednjem zagonu.")
            return False, removed
        sk, cfg = by_label[r["sport"]]
        cat = "GP" if r.get("summer") else "WC"
        url = f"{BASE}/DB/general/results.html?sectorcode={cfg['code']}&raceid={r['fis']}"
        html = get(url)
        if not html:
            continue
        status, _ = parse_race(html, sk, r["fis"], extra_names, cat)
        if status == "skip":
            races.pop(r["id"], None)
            state["seen"][f"{cfg['code']}:{r['fis']}"] = "skip"
            removed += 1
            log(f"  odstranjena: {r['d']} {r['place']} {r['disc']} {r['g']} (fis {r['fis']})")
        done.add(r["id"])
        state["refiltered"] = sorted(done)
        if len(done) >= 100 and removed / len(done) > 0.30:
            raise SystemExit(f"PREKINJENO: filter je odstranil {removed} od {len(done)} tekem (>30 %). Podatki NISO spremenjeni.")
    return True, removed


def probe(spec):
    """Samo bere: za izbrane tekme izpise vse vrstice 'datum - naslov | oznaka' (brez sprememb podatkov)."""
    by_code = {cfg["code"]: cfg for cfg in SPORTS.values()}
    for item in spec.split(","):
        if item.strip().startswith("ATH:"):
            _, sec, cid, season = item.strip().split(":")
            for extra in ("&categorycode=WC", ""):
                ahtml = get(f"{BASE}/DB/general/athlete-biography.html?sectorcode={sec}&competitorid={cid}&type=result&seasoncode={season}{extra}")
                if not ahtml:
                    log(f"PREIZKUS {item}: stran ni dosegljiva ({extra or 'brez kategorije'})")
                    continue
                al = [l for l in BeautifulSoup(ahtml, "html.parser").get_text("\n", strip=True).split("\n") if l]
                i1 = next((i for i, l in enumerate(al) if l.lower() in ("date", "place", "pos", "rank")), 0)
                log(f"PREIZKUS {item} ({extra or 'brez kategorije'}) VRSTICE: {al[max(0,i1-8):i1+90]}")
            continue
        sec, rid = item.strip().split(":")
        html = get(f"{BASE}/DB/general/results.html?sectorcode={sec}&raceid={rid}")
        if not html:
            log(f"PREIZKUS {item}: stran ni dosegljiva")
            continue
        soup = BeautifulSoup(html, "lxml") if _has_lxml() else BeautifulSoup(html, "html.parser")
        head = [l for l in soup.get_text("\n", strip=True).split("\n") if l][:90]
        found = [m.group(0) for m in TYPE_RE.finditer(" ".join(head))]
        h1 = soup.find("h1")
        wc = any("world cup" in l.lower() for l in head)
        log(f"PREIZKUS {item}: kraj={h1.get_text(' ', strip=True) if h1 else '?'} | World Cup v glavi={wc} | vrstice={found}")
        # vrstice rezultatov: prvih 5 uvrscenih in vsi slovenski tekmovalci (brez sprememb podatkov)
        allv = [l for l in soup.get_text("\n", strip=True).split("\n") if l]
        try:
            i0 = next(i for i, l in enumerate(allv) if l.lower() in ("rank", "rk"))
        except StopIteration:
            i0 = 0
        log(f"PREIZKUS {item} TABELA: {allv[i0:i0+45]}")
        # povezave na strani, ki bi lahko vsebovale rezultate (pdf, results, raceid, type=)
        hrefs = []
        for a in soup.find_all("a", href=True):
            h = a["href"]
            if any(k in h.lower() for k in ("pdf", "result", "raceid", "type=", "download")):
                t = a.get_text(" ", strip=True)[:40]
                if (t, h) not in hrefs:
                    hrefs.append((t, h))
        log(f"PREIZKUS {item} POVEZAVE: {hrefs[:40]}")
        for variant in (f"&type=result", f"&type=results"):
            vh = get(f"{BASE}/DB/general/results.html?sectorcode={sec}&raceid={rid}{variant}")
            if vh:
                vl = [l for l in BeautifulSoup(vh, "html.parser").get_text("\n", strip=True).split("\n") if l]
                log(f"PREIZKUS {item} INACICA {variant}: rank_glava={any(l.lower()=='rank' for l in vl)} prva_vrstica={vl[:1]}")
        for i, l in enumerate(allv):
            if l == "SLO":
                log(f"PREIZKUS {item} SLO: {allv[max(0,i-5):i+6]}")

# ------------------------------------------------------------- preverjanje s FIS
def season_of(date):
    y, m = int(date[:4]), int(date[5:7])
    return y + 1 if m >= 7 else y


def parse_podiums(html):
    """Iz strani FIS 'Podiums' vrne {sezona: [1., 2., 3.]} ali None, ce tabele ni."""
    soup = BeautifulSoup(html, "lxml") if _has_lxml() else BeautifulSoup(html, "html.parser")
    lines = [l for l in soup.get_text("\n", strip=True).split("\n") if l]
    out = {}
    for i, l in enumerate(lines):
        if re.fullmatch(r"(19|20)\d\d", l) and i + 4 < len(lines):
            nums = lines[i + 1:i + 5]
            if all(re.fullmatch(r"\d{1,3}", n) for n in nums):
                a, b, c, tot = (int(n) for n in nums)
                if a + b + c == tot:
                    out[int(l)] = [a, b, c]
    return out or None


def validate(races, today, deadline):
    """Primerja nase stetje 1./2./3. mesta po sezonah s podatki FIS (Podiums) za vsakega sportnika."""
    ours = {}  # (sport_key, athlete_id) -> {sezona: [1,2,3]}, ime
    names = {}
    by_label = {cfg["label"]: k for k, cfg in SPORTS.items()}
    for r in races.values():
        if r.get("summer") or r.get("t") != "posamicno":
            continue
        sk = by_label.get(r["sport"])
        if not sk:
            continue
        for x in r["res"]:
            if "id" not in x or x["r"] > 3:
                continue
            seas = ours.setdefault((sk, x["id"]), {})
            seas.setdefault(season_of(r["d"]), [0, 0, 0])[x["r"] - 1] += 1
            names[(sk, x["id"])] = x["n"]
    rows, n_ok, n_diff, n_na = [], 0, 0, 0
    for (sk, aid), seas in sorted(ours.items(), key=lambda kv: names[kv[0]]):
        if time.time() > deadline:
            log("Preverjanje: casovna omejitev.")
            break
        url = f"{BASE}/DB/general/athlete-biography.html?sectorcode={SPORTS[sk]['code']}&competitorid={aid}&type=podiums"
        html = get(url)
        fis = parse_podiums(html) if html else None
        if fis is None:
            n_na += 1
            rows.append({"n": names[(sk, aid)], "id": aid, "sport": SPORTS[sk]["label"], "status": "nepreverjeno"})
            continue
        diffs = []
        for season in sorted(set(seas) | set(fis)):
            if season < FIRST_SEASON:
                continue
            a, b = seas.get(season, [0, 0, 0]), fis.get(season, [0, 0, 0])
            if a != b:
                diffs.append({"s": f"{season-1}/{str(season)[2:]}", "nase": a, "fis": b})
        if diffs:
            n_diff += 1
            rows.append({"n": names[(sk, aid)], "id": aid, "sport": SPORTS[sk]["label"], "status": "razlika", "razlike": diffs})
            log(f"  RAZLIKA {names[(sk, aid)]}: " + "; ".join(f"{d['s']} nase {d['nase']} FIS {d['fis']}" for d in diffs))
        else:
            n_ok += 1
    out = {"updated": today.isoformat(), "ujema": n_ok, "razlike": n_diff, "nepreverjeno": n_na,
           "athletes": [r for r in rows if r["status"] != "ok"]}
    (DATA / "validation.json").write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    log(f"Preverjanje s FIS: ujema {n_ok}, razlike {n_diff}, nepreverjeno {n_na}")

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


def process_season(code, sport_key, races, state, cat, label, finished, extra_names, deadline):
    """Obdela koledar ene sezone/kategorije. Vrne: popolno | delno | v teku | None (cas potekel)."""
    cfg = SPORTS[sport_key]
    cal_url = (f"{BASE}/DB/{cfg['path']}/calendar-results.html?sectorcode={cfg['code']}"
               f"&seasoncode={code}&categorycode={cat}")
    html = get(cal_url)
    failures = 0
    if not html:
        log(f"  koledar ni dosegljiv: {cal_url}")
        return "delno"
    events = parse_calendar(html, cat)
    log(f"{label} {cfg['label']} ({cat}): {len(events)} dogodkov")
    if not events:
        return "delno" if finished else "v teku"
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
            status, race = parse_race(rhtml, sport_key, rid, extra_names, cat)
            if status == "ok":
                if race["res"]:
                    races[race["id"]] = race
                state["seen"][key] = "ok"
                log(f"  {race['d']} {race['place']} {race['disc']} {race['g']}: "
                    f"{', '.join(str(x['r']) + '. ' + x['n'] for x in race['res']) or '-'}")
            elif status == "skip":
                state["seen"][key] = "skip"
            elif status == "empty":
                if finished:
                    state["seen"][key] = "empty"
            else:
                failures += 1
                log(f"  NAPAKA razclenjevanja: {rurl}")
    if not finished:
        return "v teku"
    return "popolno" if failures == 0 else "delno"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["weekly", "backfill"], default="weekly")
    ap.add_argument("--from-season", type=int, default=1992, help="zimska sezona: koda = koncno leto (1992 = 1991/92)")
    ap.add_argument("--no-summer", action="store_true", help="preskoci poletno sezono (Grand Prix)")
    ap.add_argument("--max-minutes", type=int, default=330)
    ap.add_argument("--reset-sport", help="(alpine, jumping, nordic ali vec z vejicami) zavrze zbrane podatke tega sporta (zimske in poletne) in jih zbere znova")
    ap.add_argument("--refilter", action="store_true", help="ponovno preveri ze shranjene tekme z novim filtrom in odstrani neprave")
    ap.add_argument("--probe", help="samo bere oznake vrst za tekme, npr. JP:6696,JP:7566")
    ap.add_argument("--validate", action="store_true", help="po zbiranju primerja stetje z FIS Podiums")
    ap.add_argument("--only-validate", action="store_true", help="samo preveri, brez zbiranja")
    ap.add_argument("--force", action="store_true", help="ponovno obdela tudi sezone, oznacene kot popolne")
    args = ap.parse_args()
    if args.probe:
        probe(args.probe)
        return 0

    today = datetime.date.today()
    cur = current_season_code(today)
    code_of = {cfg["label"]: cfg["code"].lower() for cfg in SPORTS.values()}
    loaded = load_json(DATA / "races.json", [])
    for r in loaded:  # stara oblika id "fis-123" je bila skupna za vse sporte; zdaj "al-123", "jp-123"
        r["id"] = f"{code_of[r['sport']]}-{r['fis']}"
    races = {r["id"]: r for r in loaded}
    state = load_json(DATA / "state.json", {"seen": {}})
    state.setdefault("seen", {})
    meta = load_json(DATA / "meta.json", {"coverage": {}})
    meta.setdefault("coverage", {})
    extra_names = load_json(DATA / "names.json", {})
    deadline = time.time() + args.max_minutes * 60

    if args.reset_sport:
        keys = [k.strip() for k in args.reset_sport.split(",") if k.strip()]
        n0 = len(races)
        for key in keys:
            cfg = SPORTS[key]
            races = {k: r for k, r in races.items() if r["sport"] != cfg["label"]}
            state["seen"] = {k: v for k, v in state["seen"].items() if not k.startswith(cfg["code"] + ":")}
            for lab in meta["coverage"].values():
                lab.pop(key, None)
        log(f"Ponastavitev {args.reset_sport}: odstranjenih {n0 - len(races)} tekem")

    if args.refilter:
        finished_all, n = refilter(races, state, extra_names, deadline)
        log(f"Ponovni pregled: odstranjenih {n} tekem; oznake vrst: {CODES_SEEN}")
        if finished_all:
            state.pop("refiltered", None)
        save_all(races, state, meta, today)
        if finished_all and args.validate:
            validate(races, today, deadline)
        return 0

    if args.only_validate:
        validate(races, today, deadline)
        return 0

    jobs = []  # (code, sport, cat, label, finished)
    winter_codes = [cur] if args.mode == "weekly" else list(range(cur, args.from_season - 1, -1))
    for code in winter_codes:
        for sk in WINTER_SPORTS:
            jobs.append((code, sk, "WC", season_code_label(code), code < cur))
    if not args.no_summer:
        summer_codes = [cur] if args.mode == "weekly" else list(range(cur, SUMMER_FROM - 1, -1))
        for code in summer_codes:
            if code < SUMMER_FROM:
                continue
            year = code - 1
            finished = today > datetime.date(year, 11, 1)
            for sk in SUMMER_SPORTS:
                jobs.append((code, sk, "GP", f"Poletje {year}", finished))

    for code, sk, cat, label, finished in jobs:
        cov = meta["coverage"].setdefault(label, {})
        if not args.force and finished and cov.get(sk) == "popolno":
            continue
        result = process_season(code, sk, races, state, cat, label, finished, extra_names, deadline)
        if result is None:
            log("Casovna omejitev dosezena: shranjujem in koncam. Ponovni zagon nadaljuje.")
            save_all(races, state, meta, today)
            return 0
        cov[sk] = result
        save_all(races, state, meta, today)
    save_all(races, state, meta, today)
    if args.validate:
        validate(races, today, deadline)
    log(f"Konec. Tekem s slovenskimi uvrstitvami: {len(races)}; oznake vrst tekem: {CODES_SEEN}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
