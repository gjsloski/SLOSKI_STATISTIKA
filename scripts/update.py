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
DISC.sort(key=lambda kv: 0 if kv[0] == "Gundersen" else 1)  # nordijska kombinacija: ime discipline je Gundersen, ne skakalnica

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
SKIP_WORDS = ("Relay", "Standings")
TEAM_RE = re.compile(r"\bTeam\b")
TYPE_RE = re.compile(r"\d{1,2}\.\d{1,2}\.\d{4}\s*-\s*[^|]{3,90}\|\s*([A-Za-z]{2,6})\b")
# oznake vrste tekme na FIS: WC = tekma, TRA = trening, COM = etapa kombinacije, kvalifikacije in poskusne serije
CODES_SEEN = {}
BAD_TYPES = {"TRA", "COM", "QUA", "QUAL", "QUF", "Q", "TRI", "TRL", "PRO", "PCR", "TRN", "FIS", "COC", "PR"}
# Slovenski tekmovalci, ki jih FIS pri nekaterih tekmah vodi pod drugo drzavo (JUG v sezoni 1991/92, GER pri Urbanu Francu 1995).
# Vrednost: competitorid. Dodaj samo, ce je to dokazano s stranjo sportnika na FIS.
# Tekme svetovnega pokala, ki jih koledar FIS (kategorija WC) ne navaja, ker so voden kot svetovno prvenstvo v poletih (SFWC + WC):
# Harrachov 1992, Planica 1994, Tauplitz/Bad Mitterndorf 1996 in 1998. Strani so preverjene s probe (naslov in oznaka 'WC').
EXTRA_RACES = ["JP:1823", "JP:1822", "JP:1321", "JP:329", "JP:330", "JP:331", "JP:652", "JP:809"]  # zadnji dve: ekipni tekmi 1992 in 1995 (clani brez leta rojstva)
EXTRA_SLO = {"68518", "27683", "58901", "18035", "53613", "61013"}  # Zupan, Janus, Stirn, Franc Urban, Sarec Veronika, Tepes Miran
# Strani, ki jih FIS vodi kot tekmo (OWG), a jih na straneh sportnikov NI med uvrstitvami (del olimpijske kombinacije):
# Nagano 13.2.1998, smuk kot del kombinacije. Preverjeno: strani sportnikov Brezavsek in Pen tega raceid nimata.
EXCLUDE_RACES = {"AL:5453"}
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
    nat_off = 1
    for i in range(k, len(tokens) - 1):
        if re.fullmatch(r"(19|20)\d\d", tokens[i]) and re.fullmatch(r"[A-Z]{3}", tokens[i + 1]):
            y = i
            break
    if y is None:
        # starejse vrstice brez leta rojstva ('5 2864 JEKOVEC Dajan SLO 227.5 -28'): drzava je prva 3-crkovna oznaka,
        # ki sledi imenu (pred njo je ime z malimi crkami), nato pridejo tocke
        for i in range(k + 1, len(tokens)):
            if re.fullmatch(r"[A-Z]{3}", tokens[i]) and (tokens[i - 1] != tokens[i - 1].upper() or tokens[i - 1].endswith(".")) \
                    and (i + 1 >= len(tokens) or re.fullmatch(r"[-+]?[\d'.:]+|DNF\d?|DSQ\d?|DNS\d?", tokens[i + 1])):
                y = i
                nat_off = 0
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
    return {"cid": cid, "name": name, "rank": int(tokens[0]), "nat": tokens[y + nat_off]}


def parse_member(text, href):
    """Clan ekipe: '11-1 6681 MARUYAMA Nozomi 1998 1 128.0 ...' (skoki) ali '1 31 STJERNESUND Thea Louise 1996 NOR 22.12' (alpsko)."""
    tk = text.split()
    if not tk or not (tk[0].isdigit() or re.fullmatch(r"\d+-\d+", tk[0])):
        return None
    k = 0
    while k < len(tk) and (tk[k].isdigit() or re.fullmatch(r"\d+-\d+", tk[k])):
        k += 1
    y = next((i for i in range(k + 1, len(tk)) if re.fullmatch(r"(19|20)\d\d", tk[i])), None)
    if y is None:  # stari zapis brez leta rojstva ('2809 STRGAR Igor'): ime do prve stevilke
        y = next((i for i in range(k, len(tk)) if re.search(r"\d", tk[i])), len(tk))
    name_tokens = tk[k:y]
    if not name_tokens:
        return None
    sur = []
    for t in name_tokens:
        if t == t.upper() and any(c.isalpha() for c in t):
            sur.append(t)
        else:
            break
    rest = name_tokens[len(sur):]
    if sur:
        name = f"{rest[0] if rest else ''} {' '.join(w.title() for w in sur)}".strip()
    else:  # stari zapis 'Priimek Ime'
        name = " ".join(name_tokens[1:] + name_tokens[:1])
    nat = tk[y + 1] if y + 1 < len(tk) and re.fullmatch(r"[A-Z]{3}", tk[y + 1]) and tk[y + 1] not in STATUS_CODES else None
    cid = parse_qs(urlparse(href).query).get("competitorid", [None])[0]
    return {"cid": cid, "name": name, "nat": nat}


STATUS_CODES = {"DNF", "DSQ", "DNS", "DQ", "DNQ", "LAP", "NPS", "DPO", "DNC", "NQ"}
NUMLIKE_RE = re.compile(r"[+\-]?[\d:.,]+")


def team_nat_index(tk):
    """Indeks oznake drzave v vrstici ekipe ('6 74576 SLOVENIA SLO', '8 7 101876 SLOVENIA SLO 359.9 44:48.5 +7:10.3',
    '1 1 54569 AUSTRIA AUT 480.2'): rang na zacetku, nato ime, oznaka drzave in samo stevilke/casi do konca.
    Vrstice clanov (z letom rojstva pred oznako) niso vrstice ekip."""
    if len(tk) < 4 or not tk[0].isdigit():
        return None
    for i in range(2, len(tk)):
        if (re.fullmatch(r"[A-Z]{3}", tk[i]) and tk[i] not in STATUS_CODES
                and not re.fullmatch(r"(19|20)\d\d", tk[i - 1])
                and all(NUMLIKE_RE.fullmatch(t) for t in tk[i + 1:])):
            return i
    return None


def parse_team_page(soup):
    """Vrne seznam ekip [{'rank','nat','tid','members'}] in seznam vseh clanov [{'cid','name','nat','team'}]."""
    teams, persons, cur = [], [], None
    for a in soup.find_all("a"):
        href = a.get("href", "")
        if "athlete-biography" not in href:
            continue
        text = a.get_text(" ", strip=True)
        tk = text.split()
        cid = parse_qs(urlparse(href).query).get("competitorid", [None])[0]
        ti = team_nat_index(tk)
        if ti is not None:
            cur = next((t for t in teams if t["tid"] == cid and t["nat"] == tk[ti]), None)
            if cur is None:
                cur = {"rank": int(tk[0]), "nat": tk[ti], "tid": cid, "members": []}
                teams.append(cur)
            continue
        m = parse_member(text, href)
        if m:
            m["team"] = cur
            persons.append(m)
            if cur is not None:
                cur["members"].append(m)
    return teams, persons


def team_result(soup, extra_names):
    """Slovenski clani ekip z mestom ekipe. Vrne seznam {'n','r','id'} ali None, ce strani ni (odpovedana tekma)."""
    teams, persons = parse_team_page(soup)
    if not teams:
        return None
    slo_rank = next((t["rank"] for t in teams if t["nat"] == "SLO"), None)
    res, seen = [], set()
    for m in persons:
        cur = m["team"]
        if m["nat"] is None and cur is not None:
            if cur["nat"] != "SLO" and m["cid"] not in EXTRA_SLO:
                continue
            rank = cur["rank"]
        else:
            if m["nat"] != "SLO" and m["cid"] not in EXTRA_SLO:
                continue
            rank = slo_rank
        if rank is None or rank > 30 or not m["cid"] or m["cid"] in seen:
            continue
        seen.add(m["cid"])
        res.append({"n": fix_name(m["name"], m["cid"], extra_names), "r": rank, "id": int(m["cid"])})
    return res


def parse_race(html, sport_key, raceid, extra_names, cat="WC", trusted=False):
    """Vrne (status, race). status: ok | skip | empty | fail"""
    cfg = SPORTS[sport_key]
    soup = BeautifulSoup(html, "lxml") if _has_lxml() else BeautifulSoup(html, "html.parser")
    lines = [l for l in soup.get_text("\n", strip=True).split("\n") if l]
    head = lines[:90]
    # Nekatere strani (raceid) so STARTNE LISTE: prva stolpca sta startna stevilka in vrstni red starta, ne uvrstitev
    # (npr. 'FIS | Alpine Skiing Start List - Bormio (ITA) 2007/2008'). Prave rezultate prepozna glava tabele 'Rank'.
    # Taksnih strani ne stejemo.
    has_rank_header = any(l.strip().lower() == "rank" for l in lines)
    is_startlist = (not has_rank_header) and any("start list" in l.lower() for l in lines[:3])
    if is_startlist:
        CODES_SEEN[(cat, "STARTLIST")] = CODES_SEEN.get((cat, "STARTLIST"), 0) + 1
    # Prekinjena tekma (npr. Maribor 15.1.2011): naslov strani je '... Competition ...', v tabeli ni tock ('Cup Points'),
    # FIS je pri sportnikih ne steje. Stran z rezultati ima '... Results ...' in stolpec 'Cup Points'.
    has_cup_points = any("cup points" in l.lower() for l in lines)
    if not is_startlist and not has_cup_points and any("competition" in l.lower() for l in lines[:3]):
        CODES_SEEN[(cat, "NEPOPOLNA")] = CODES_SEEN.get((cat, "NEPOPOLNA"), 0) + 1
        return "skip", None
    needle = "World Cup" if cat == "WC" else "Grand Prix"
    champ = cat not in ("WC", "GP")  # OWG, WSC: koledar kategorije ze zagotavlja vrsto tekmovanja
    if not trusted and not champ and not any(needle.lower() in l.lower() for l in head):
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
    own_codes = []
    for m in TYPE_RE.finditer(" ".join(head)):
        mm = re.match(r"(\d{1,2}\.\d{1,2}\.\d{4})\s*-\s*(.+?)\s*\|", m.group(0))
        if mm and mm.group(1) == ddmm and norm(mm.group(2)) == norm(title):
            own_codes.append(m.group(1).upper())
    # ista seja ima lahko dve oznaki (npr. SFWC + WC pri svetovnem prvenstvu v poletih, ki je hkrati tekma SP):
    # ce je med njimi svetovni pokal / Grand Prix, je to tekma za uvrstitve
    if own_codes:
        own_code = cat if cat in own_codes else own_codes[0]
    if champ and own_code != cat:
        return "skip", None  # trening, kvalifikacije, del kombinacije ...
    if own_code:
        CODES_SEEN[(cat, own_code)] = CODES_SEEN.get((cat, own_code), 0) + 1
        if own_code in BAD_TYPES or own_code.startswith(("QU", "TR")):
            return "skip", None
    # kvalifikacije, treningi in poskusne serije niso tekme za uvrstitve
    ti = head.index(title)
    if any(QUAL_RE.search(l) for l in head[ti:ti + 3] if len(l) < 90):
        return "skip", None
    is_team = bool(TEAM_RE.search(title)) or title.startswith("Mixed")
    gender = "M" if title.startswith("Men") else ("X" if title.startswith(("Mixed", "Team")) else "Ž")
    disc = None
    for key, sl in DISC:
        if key.lower() in title.lower():
            disc = sl
            break
    if disc is None:
        disc = re.sub(r"^(Men|Women|Ladies)['’]?s?\s*", "", title).strip() or title

    if is_team:
        # ekipna tekma: vsak slovenski clan dobi mesto svoje ekipe (kot na strani sportnika na FIS)
        if not has_rank_header:
            return "empty", None
        tres = team_result(soup, extra_names)
        if tres is None:
            return "empty", None  # brez tabele: odpovedana tekma
        race = {
            "id": f"{cfg['code'].lower()}-{raceid}", "fis": int(raceid), "d": date,
            "place": re.sub(r"\s+", " ", place), "sport": cfg["label"], "disc": disc, "g": gender,
            "t": "ekipno", "res": sorted(tres, key=lambda x: x["r"]),
        }
        if cat == "GP":
            race["summer"] = True
        elif champ:
            race["c"] = cat
        return "ok", race

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
    if is_startlist and champ:
        return "empty", None
    if is_startlist:
        # Na strani je samo startna lista: prave uvrstitve pridejo s strani posameznega sportnika (fill_from_athletes)
        slo, seen_c = [], set()
        for r in ranked:
            if r["nat"] == "SLO" and r["cid"] and r["cid"] not in seen_c:
                seen_c.add(r["cid"])
                slo.append({"cid": r["cid"], "name": fix_name(r["name"], r["cid"], extra_names)})
        stub = {
            "id": f"{cfg['code'].lower()}-{raceid}", "fis": int(raceid), "d": date,
            "place": re.sub(r"\s+", " ", place), "sport": cfg["label"], "disc": disc, "g": gender,
            "t": "posamicno", "res": [], "_slo": slo, "_sec": cfg["code"], "_cat": cat,
        }
        if cat == "GP":
            stub["summer"] = True
        return "startlist", stub
    res = []
    for r in ranked:
        if r["rank"] <= 30 and r["nat"] in ("JUG", "YUG") and r["cid"] not in EXTRA_SLO:
            log(f"  JUG/YUG v top 30: {r['name']} (competitorid {r['cid']}) {r['rank']}. mesto, {date} {place} - ni steto")
        if (r["nat"] == "SLO" or r["cid"] in EXTRA_SLO) and r["rank"] <= 30:
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
    elif champ:
        race["c"] = cat
    return "ok", race





ATH_CACHE = {}
TEAMS_DONE = None  # mnozica kljucev, ki so ze bili ponovno obdelani v nacinu --teams


def season_code_of(date):
    y, m = int(date[:4]), int(date[5:7])
    return y + 1 if m >= 7 else y


ATH_ROW_RE = re.compile(
    r"^(?P<date>\d{2}-\d{2}-\d{4})\s+(?P<place>.+?)\s+(?P<nat>[A-Z]{3})\s+(?P<cat>World Cup|Grand Prix)\s+"
    r"(?P<disc>.+?)\s+(?P<pos>\d{1,3}|(?:DNF|DNS|DSQ|DNQ|NPS|DQ|PEN|LAP)\d?)(?:\s+[\d.\-]+)*\s*$")


POS_RE = re.compile(r"^(\d{1,3}|(?:DNF|DNS|DSQ|DNQ|NPS|DQ|PEN|LAP)\d?)$")
CAT_NAMES = {"World Cup": "WC", "Grand Prix": "GP"}


def athlete_row_cells(html):
    """Stran sportnika (type=result): {raceid: [celice vrstice]}. FIS zavije vrstico v eno povezavo (...&raceid=N)."""
    soup = BeautifulSoup(html, "lxml") if _has_lxml() else BeautifulSoup(html, "html.parser")
    groups, order = {}, []
    for a in soup.find_all("a", href=True):
        rid = parse_qs(urlparse(a["href"]).query).get("raceid", [None])[0]
        if not rid:
            continue
        if rid not in groups:
            groups[rid] = []
            order.append(rid)
        groups[rid].extend(c.strip() for c in a.get_text("\n", strip=True).split("\n") if c.strip())
    return {rid: groups[rid] for rid in order}


def parse_athlete_results(html):
    """Vrne {raceid: {"pos", "disc", "date", "cat", "code"}} samo za vrstice World Cup / Grand Prix.
    FIS izpise vrstico z dvojnimi celicami: [datum, kraj, kraj, disciplina, drzava, KODA, KODA, IME KATEGORIJE,
    disciplina, UVRSTITEV, FIS tocke, tocke pokala]; uvrstitev je torej dve celici za imenom kategorije."""
    out = {}
    for rid, cells in athlete_row_cells(html).items():
        if not cells or not re.fullmatch(r"\d{2}-\d{2}-\d{4}", cells[0]):
            continue
        i = next((j for j, c in enumerate(cells) if c in CAT_NAMES), None)
        if i is None:
            # druge kategorije (OWG, WSC ...): ime kategorije stoji za dvema enakima kodama tekme ('OWG', 'OWG', 'Olympic Winter Games')
            i = next((j + 2 for j in range(4, len(cells) - 4)
                      if cells[j] == cells[j + 1] and re.fullmatch(r"[A-Z]{2,5}", cells[j]) and not POS_RE.match(cells[j])), None)
        if i is not None and i >= 1 and i + 2 < len(cells) and POS_RE.match(cells[i + 2]):
            out[rid] = {"date": cells[0], "cat": cells[i], "code": cells[i - 1], "disc": cells[i + 1], "pos": cells[i + 2]}
            continue
        # drugi zapis (vrstica kot eno besedilo: 'datum kraj DRZ World Cup disciplina uvrstitev ...')
        m = ATH_ROW_RE.match(re.sub(r"\s+", " ", " ".join(cells)).strip())
        if m:
            out[rid] = {"date": m.group("date"), "cat": m.group("cat"), "code": "WC" if m.group("cat") == "World Cup" else "GP",
                        "disc": m.group("disc"), "pos": m.group("pos")}
    return out


def athlete_results(sec, cid, season, cat):
    """Uvrstitve sportnika v sezoni (kategorija WC/GP) -> {raceid: polozaj} ali None, ce strani ni bilo mogoce prebrati."""
    key = (sec, cid, season, cat)
    if key in ATH_CACHE:
        return ATH_CACHE[key]
    result = None
    for extra in (f"&categorycode={cat}&sort=&place=&disciplinecode=&position=&limit=100", ""):
        html = get(f"{BASE}/DB/general/athlete-biography.html?sectorcode={sec}&seasoncode={season}"
                   f"&competitorid={cid}&type=result{extra}")
        if html:
            result = parse_athlete_results(html)
            if cat in ("WC", "GP"):
                want = "World Cup" if cat == "WC" else "Grand Prix"
                result = {k: v for k, v in result.items() if want.lower() in v["cat"].lower()}
            else:
                result = {k: v for k, v in result.items() if v["code"] == cat}
            if result:
                break
    if not result:
        log(f"  OPOZORILO: stran sportnika {sec}:{cid} sezona {season} ({cat}) nima prebranih vrstic")
    ATH_CACHE[key] = result
    return result


def fill_from_athletes(stub):
    """Iz strani slovenskih tekmovalcev s startne liste prebere prave uvrstitve. Vrne (tekma, popolno)."""
    season = season_code_of(stub["d"])
    res, complete = [], True
    for a in stub["_slo"]:
        ar = athlete_results(stub["_sec"], a["cid"], season, stub["_cat"])
        if ar is None:
            complete = False
            continue
        row = ar.get(str(stub["fis"]))
        pos = row["pos"] if row else None
        if pos and pos.isdigit() and int(pos) <= 30:
            res.append({"n": a["name"], "r": int(pos), "id": int(a["cid"])})
    race = {k: v for k, v in stub.items() if not k.startswith("_")}
    race["res"] = sorted(res, key=lambda x: x["r"])
    return race, complete


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
        cat = r.get("c") or ("GP" if r.get("summer") else "WC")
        url = f"{BASE}/DB/general/results.html?sectorcode={cfg['code']}&raceid={r['fis']}"
        html = get(url)
        if not html:
            continue
        status, _ = parse_race(html, sk, r["fis"], extra_names, cat)
        if status == "startlist":
            races.pop(r["id"], None)
            state["seen"].pop(f"{cfg['code']}:{r['fis']}", None)
            removed += 1
            log(f"  startna lista (bo dopolnjena): {r['d']} {r['place']} {r['disc']} {r['g']} (fis {r['fis']})")
        elif status == "skip":
            races.pop(r["id"], None)
            state["seen"][f"{cfg['code']}:{r['fis']}"] = "skip"
            removed += 1
            log(f"  odstranjena: {r['d']} {r['place']} {r['disc']} {r['g']} (fis {r['fis']})")
        done.add(r["id"])
        state["refiltered"] = sorted(done)
        if len(done) >= 100 and removed / len(done) > 0.30:
            raise SystemExit(f"PREKINJENO: filter je odstranil {removed} od {len(done)} tekem (>30 %). Podatki NISO spremenjeni.")
    return True, removed


def _probe_team_race(sec, item, rid):
    html = get(f"{BASE}/DB/general/results.html?sectorcode={sec}&raceid={rid}")
    if not html:
        return
    soup = BeautifulSoup(html, "html.parser")
    anchors = [(a.get_text(" ", strip=True)[:90], a["href"][-60:]) for a in soup.find_all("a", href=True)
               if "athlete-biography" in a["href"]]
    log(f"EKIPE2 {item}: sidrov z athlete-biography: {len(anchors)}; prvih 6: {anchors[:6]}")
    lines = [l for l in soup.get_text("\n", strip=True).split("\n") if l]
    log(f"EKIPE2 {item}: 'Rank' v strani: {any(l.strip().lower() == 'rank' for l in lines)}, vrstic {len(lines)}")
    i0 = next((i for i, l in enumerate(lines) if l.strip().lower() == "rank"), 0)
    log(f"EKIPE2 {item} TABELA: {lines[i0:i0 + 40]}")
    node = soup.find(string=re.compile(r"SLOVENIA", re.I))
    if node:
        par = node.parent
        for _ in range(3):
            if par.parent is not None and par.parent.name not in ("body", "html"):
                par = par.parent
        log(f"EKIPE2 {item} HTML ekipe SLO: {str(par)[:1600]}")
    allA = [a for a in soup.find_all("a", href=True) if "athlete-biography" in a["href"]]
    si = next((i for i, a in enumerate(allA) if "SLOVENIA" in a.get_text(" ", strip=True).upper()
               and not a.get_text(" ", strip=True)[:1].isdigit() is False), None)
    if si is not None:
        log(f"EKIPE2 {item} SLO SIDRA OD ZAPISA EKIPE: {[(a.get_text(' ', strip=True)[:80], a['href'][-45:]) for a in allA[si:si + 9]]}")
        log(f"EKIPE2 {item} SLO HTML clana: {str(allA[si + 1])[:1200] if si + 1 < len(allA) else '-'}")
    log(f"EKIPE2 {item} team_result: {team_result(soup, {})}")
    mem = next((a for a in soup.find_all("a", href=True) if "athlete-biography" in a["href"]), None)
    if mem:
        log(f"EKIPE2 {item} prvi sidro HTML: {str(mem.parent)[:900]}")
        cid = parse_qs(urlparse(mem['href']).query).get("competitorid", [None])[0]
        dm = re.search(r"([A-Z][a-z]+ \d{1,2}, \d{4})", " ".join(lines[:90]))
        if cid and dm:
            d = datetime.datetime.strptime(dm.group(1), "%B %d, %Y").date()
            season = season_code_of(d.isoformat())
            ah = get(f"{BASE}/DB/general/athlete-biography.html?sectorcode={sec}&seasoncode={season}&competitorid={cid}"
                     f"&type=result&categorycode=WC&sort=&place=&disciplinecode=&position=&limit=100")
            if ah:
                cells = athlete_row_cells(ah)
                log(f"EKIPE2 {item} athlete {cid} season {season}: vrstica raceid {rid}: {cells.get(str(rid)) or cells.get(rid)}")
                log(f"EKIPE2 {item} athlete {cid}: raceid-ji: {list(cells)[:12]}")


def probe_team2(spec):
    """JP:7460 -> zgradba HTML ekipne tekme (raceid); AL:S2023 -> ekipni naslovi sezone in zgradba prve ekipne tekme."""
    for item in spec.split(","):
        item = item.strip()
        sec, _, rest = item.partition(":")
        sk = next((k for k, c in SPORTS.items() if c["code"] == sec), None)
        if not sk:
            continue
        cfg = SPORTS[sk]
        if not rest.startswith("S"):
            _probe_team_race(sec, item, rest)
            continue
        season = rest[1:]
        html = get(f"{BASE}/DB/{cfg['path']}/calendar-results.html?sectorcode={sec}&seasoncode={season}&categorycode=WC")
        done = False
        for eid, eurl in (parse_calendar(html, "WC") if html else []):
            ehtml = get(eurl)
            for rid, rurl in (parse_event(ehtml, sec) if ehtml else []):
                rhtml = get(rurl)
                if not rhtml:
                    continue
                lines = [l for l in BeautifulSoup(rhtml, "html.parser").get_text("\n", strip=True).split("\n") if l][:90]
                title = next((l for l in lines if TITLE_RE.match(l) and len(l) < 90), "")
                if re.search(r"Team", title) and not done:
                    log(f"ALTEAM {item} raceid={rid} {title}")
                    _probe_team_race(sec, f"{sec}:{rid}", rid)
                    done = True


def probe_cat(spec):
    """Izpise dogodke in tekme drugih kategorij (OWG, WSC, ...): SEKTOR:SEZONA:KATEGORIJA, npr. JP:2022:OWG"""
    for item in [x.strip() for x in spec.split(",") if x.strip()]:
        sec, season, cat = item.split(":")
        path = next(c["path"] for c in SPORTS.values() if c["code"] == sec)
        url = (f"{BASE}/DB/{path}/calendar-results.html?sectorcode={sec}&seasoncode={season}&categorycode={cat}")
        html = get(url)
        if not html:
            log(f"KAT {item}: koledar ni dosegljiv")
            continue
        soup = BeautifulSoup(html, "html.parser")
        evs = {}
        for a in soup.find_all("a", href=True):
            if "event-details" in a["href"] and "eventid=" in a["href"]:
                eid = parse_qs(urlparse(a["href"]).query).get("eventid", [None])[0]
                e = evs.setdefault(eid, {"url": urljoin(BASE, a["href"]), "t": []})
                e["t"].append(a.get_text(" ", strip=True)[:50])
        log(f"KAT {item}: dogodkov {len(evs)}")
        for eid, e in evs.items():
            log(f"KAT {item} dogodek {eid}: {' | '.join(e['t'])[:160]}")
            ehtml = get(e["url"])
            if not ehtml:
                continue
            for rid, rurl in parse_event(ehtml, sec):
                rh = get(rurl)
                if not rh:
                    continue
                sp = BeautifulSoup(rh, "html.parser")
                lines = [l for l in sp.get_text("\n", strip=True).split("\n") if l]
                head = lines[:90]
                title = next((l for l in head if TITLE_RE.match(l) and len(l) < 90), "?")
                dt = next((l for l in head if DATE_RE.match(l)), "?")
                codes = [m.group(1) for m in TYPE_RE.finditer(" ".join(head))]
                has_rank = any(l.strip().lower() == "rank" for l in lines)
                slo = []
                if TEAM_RE.search(title) or title.startswith("Mixed"):
                    tr = team_result(sp, {})
                    slo = [f"{x['r']}. {x['n']}" for x in (tr or [])]
                else:
                    for a in sp.find_all("a", href=True):
                        if "athlete-biography" in a["href"]:
                            row = parse_row(a.get_text(" ", strip=True), a["href"])
                            if row and row["nat"] == "SLO" and row["rank"] <= 30:
                                slo.append(f"{row['rank']}. {row['name']}")
                log(f"KAT {item} race {rid}: {dt} | {title} | kode {codes[:4]} | rank-glava {has_rank} | SLO: {', '.join(slo) or '-'}")


def probe_team(spec):
    """Izpise ekipne tekme sezone (npr. JP:2026,AL:2026,JP:2024): naslov, datum, raceid, oznako, vrstice SLO in prvih nekaj vrstic tabele."""
    for item in spec.split(","):
        sec, _, season = item.strip().partition(":")
        sk = next((k for k, c in SPORTS.items() if c["code"] == sec), None)
        if not sk:
            continue
        cfg = SPORTS[sk]
        html = get(f"{BASE}/DB/{cfg['path']}/calendar-results.html?sectorcode={sec}&seasoncode={season}&categorycode=WC")
        events = parse_calendar(html, "WC") if html else []
        log(f"EKIPE {item}: {len(events)} dogodkov")
        shown = 0
        for eid, eurl in events:
            ehtml = get(eurl)
            if not ehtml:
                continue
            for rid, rurl in parse_event(ehtml, sec):
                rhtml = get(rurl)
                if not rhtml:
                    continue
                soup = BeautifulSoup(rhtml, "html.parser")
                lines = [l for l in soup.get_text("\n", strip=True).split("\n") if l]
                title = next((l for l in lines[:90] if TITLE_RE.match(l) and len(l) < 90), "")
                if not any(w in title for w in SKIP_WORDS):
                    continue
                h1 = soup.find("h1")
                log(f"EKIPE {item} raceid={rid} {title} | {h1.get_text(' ', strip=True) if h1 else ''}")
                if shown < 4:
                    shown += 1
                    i0 = next((i for i, l in enumerate(lines) if l.strip().lower() == "rank"), 0)
                    log(f"EKIPE {item} {rid} TABELA: {lines[i0:i0 + 45]}")
                    for i, l in enumerate(lines):
                        if l == "SLO":
                            log(f"EKIPE {item} {rid} SLO: {lines[max(0, i - 6):i + 14]}")
                            break


def probe(spec):
    """Samo bere: za izbrane tekme izpise vse vrstice 'datum - naslov | oznaka' (brez sprememb podatkov)."""
    by_code = {cfg["code"]: cfg for cfg in SPORTS.values()}
    for item in spec.split(","):
        if item.strip().startswith("ATH:"):
            _, sec, cid, season = item.strip().split(":")
            base = f"{BASE}/DB/general/athlete-biography.html?sectorcode={sec}&competitorid={cid}"
            variants = [
                f"&type=result&seasoncode={season}&categorycode=WC&sort=&place=&disciplinecode=&position=&limit=100",
                f"&type=result&seasoncode={season}&categorycode=WC",
                f"&type=result&seasoncode={season}",
                f"&seasoncode={season}&categorycode=WC&type=result",
                "&type=result",
                f"&type=result&seasoncode={season}&categorycode=WC&sort=",
                f"&type=result&seasoncode={season}&categorycode=GP&sort=&place=&disciplinecode=&position=&limit=100",
                f"&type=result&seasoncode={int(season)-1}&categorycode=GP&sort=&place=&disciplinecode=&position=&limit=100",
                f"&type=result&seasoncode={season}&categorycode=OWG&sort=&place=&disciplinecode=&position=&limit=100",
                f"&type=result&seasoncode={season}&categorycode=WSC&sort=&place=&disciplinecode=&position=&limit=100",
            ]
            for v in variants:
                ahtml = get(base + v)
                if not ahtml:
                    log(f"PREIZKUS {item} [{v}]: stran ni dosegljiva")
                    continue
                asoup = BeautifulSoup(ahtml, "html.parser")
                n_anch = sum(1 for x in asoup.find_all("a", href=True) if "raceid=" in x["href"])
                rows = parse_athlete_results(ahtml)
                raw = list(athlete_row_cells(ahtml).items())[:2]
                log(f"PREIZKUS {item} [{v}] CELICE: {raw}")
                al = [l for l in asoup.get_text("\n", strip=True).split("\n") if l]
                log(f"PREIZKUS {item} [{v}]: dolzina={len(ahtml)} povezav_raceid={n_anch} vrstic_rezultatov={len(rows)} "
                    f"VSE={[(k, x['date'], x['code'], x['disc'], x['pos']) for k, x in rows.items()]} besedilo={al[40:70]}")
            continue
        parts = item.strip().split(":")
        sec, rid = parts[0], parts[1]
        find = parts[2].lower() if len(parts) > 2 else None   # npr. JP:643:zupan -> izpise vrstice z tem imenom
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
            if find and find in l.lower():
                log(f"PREIZKUS {item} NAJDI {find}: {allv[max(0,i-4):i+8]}")
        if find:
            nats = {}
            for l in allv:
                if re.fullmatch(r"[A-Z]{3}", l):
                    nats[l] = nats.get(l, 0) + 1
            log(f"PREIZKUS {item} DRZAVE: {nats}")


_SKIP_PAGE = {}


def page_is_skipped(sec, rid):
    """Ali je stran FIS (sec, raceid) ekipna / skupni sestevek turneje ('Overall Standings'): taksnih ne stejemo."""
    k = (sec, rid)
    if k not in _SKIP_PAGE:
        html = get(f"{BASE}/DB/general/results.html?sectorcode={sec}&raceid={rid}")
        title = ""
        if html:
            soup = BeautifulSoup(html, "html.parser")
            lines = [l for l in soup.get_text("\n", strip=True).split("\n") if l][:90]
            title = next((l for l in lines if TITLE_RE.match(l) and len(l) < 90), "")
        _SKIP_PAGE[k] = bool(title) and any(w in title for w in SKIP_WORDS)
    return _SKIP_PAGE[k]


def check_races(races, today, deadline):
    """Vsako nasu uvrstitev slovenskega tekmovalca primerja z njegovo stranjo na FIS (po raceid). Zapise data/check.json."""
    by_label = {cfg["label"]: cfg["code"] for cfg in SPORTS.values()}
    ours = {}
    for r in races.values():
        sec = by_label.get(r["sport"])
        cat = r.get("c") or ("GP" if r.get("summer") else "WC")
        for x in r["res"]:
            if "id" in x:
                ours.setdefault((sec, str(x["id"]), season_code_of(r["d"]), cat), {})[str(r["fis"])] = (x["r"], r, x["n"])
    issues, n_ok, n_na = [], 0, 0
    for key, mine in sorted(ours.items(), key=lambda kv: (kv[0][2], kv[0][0], kv[0][1])):
        if time.time() > deadline:
            log("Preverjanje tekem: casovna omejitev.")
            break
        sec, cid, season, cat = key
        theirs = athlete_results(sec, cid, season, cat)
        if not theirs:
            n_na += 1
            continue
        for rid, (rank, race, name) in mine.items():
            t = theirs.get(rid)
            tp = t["pos"] if t else None
            if not (tp and tp.isdigit() and int(tp) == rank):
                issues.append({"n": name, "sezona": season_code_label(season), "raceid": rid, "d": race["d"],
                               "place": race["place"], "disc": race["disc"], "nase": rank, "fis": tp})
                log(f"  NESKLAD {name} {season_code_label(season)} {race['d']} {race['place']} {race['disc']}: nase {rank}, FIS {tp} (raceid {rid})")
        for rid, t in theirs.items():
            if rid in mine or not t["pos"].isdigit() or int(t["pos"]) > 30:
                continue
            if any(w.lower() in t["disc"].lower() for w in SKIP_WORDS):
                continue
            if page_is_skipped(sec, rid):
                continue
            issues.append({"n": next((v[2] for v in mine.values()), cid), "sezona": season_code_label(season), "raceid": rid,
                           "d": t["date"], "place": "", "disc": t["disc"], "nase": None, "fis": t["pos"]})
            log(f"  MANJKA {cid} {season_code_label(season)} {t['date']} {t['disc']}: FIS {t['pos']} (raceid {rid}), pri nas ni")
    n_ok = len(ours) - n_na - len({(i["n"], i["sezona"]) for i in issues})
    out = {"updated": today.isoformat(), "preverjenih_sezon": len(ours) - n_na, "nepreverjenih": n_na, "nesklad": issues}
    (DATA / "check.json").write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    log(f"Preverjanje tekem: preverjenih sezon {len(ours) - n_na}, nepreverjenih {n_na}, neskladij {len(issues)}")

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
        if r.get("summer") or r.get("c") or r.get("t") != "posamicno":
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
    ind = {}
    for r in races.values():
        if r.get("t") == "posamicno":
            for x in r["res"]:
                if "id" in x:
                    ind[x["id"]] = x["n"]
    for r in races.values():  # ekipne tekme: ime po posamicnih tekmah (stare strani zapisujejo 'Priimek Ime')
        if r.get("t") == "ekipno":
            for x in r["res"]:
                if x.get("id") in ind:
                    x["n"] = ind[x["id"]]
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
            if key in EXCLUDE_RACES:
                state["seen"][key] = "skip"
                continue
            if key in state["seen"]:
                if TEAMS_DONE is None or key in TEAMS_DONE or state["seen"][key] not in ("skip", "empty"):
                    continue
            if TEAMS_DONE is not None:
                TEAMS_DONE.add(key)
                state["teams_done"] = sorted(TEAMS_DONE)
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
            elif status == "startlist":
                if race["d"] >= datetime.date.today().isoformat():
                    continue  # tekma se ni bila: poskusimo naslednjic
                filled, complete = fill_from_athletes(race)
                if filled["res"]:
                    races[filled["id"]] = filled
                    log(f"  (s strani sportnikov) {filled['d']} {filled['place']} {filled['disc']} {filled['g']}: "
                        f"{', '.join(str(x['r']) + '. ' + x['n'] for x in filled['res'])}")
                else:
                    log(f"  (s strani sportnikov) {filled['d']} {filled['place']} {filled['disc']} {filled['g']}: brez uvrstitve v top 30")
                age_days = (datetime.date.today() - datetime.date.fromisoformat(filled["d"])).days
                if complete and (filled["res"] or age_days > 21):
                    state["seen"][key] = "ok"
                elif complete:
                    log("  (rezultati se niso objavljeni, poskusimo znova ob naslednjem zagonu)")
                else:
                    failures += 1
                    log(f"  NAPAKA: stran sportnika ni dosegljiva za {rurl}")
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


REDO_CHAMP_RACES = [  # ekipne tekme OWG/WSC, ki jih je prejsnji razclenjevalnik zgresil
    "JP:338:OWG", "JP:842:WSC", "JP:1294:WSC",
    "NK:1471:WSC", "NK:1469:WSC", "NK:1647:WSC", "NK:1649:WSC", "NK:2305:WSC", "NK:2609:WSC",
    "NK:2948:WSC", "NK:3401:WSC", "NK:3536:OWG",
    "AL:86890:WSC", "AL:91475:OWG", "AL:95556:WSC", "AL:104442:WSC", "AL:109199:OWG", "AL:114194:WSC",
    "AL:122891:WSC", "AL:122892:WSC",
]


def redo_races(races, state, extra_names, items):
    """Ponovno prebere navedene tekme (SEC:raceid:KATEGORIJA) in prepise shranjene."""
    n = 0
    for item in items:
        sec, rid, cat = item.split(":")
        sk = next((k for k, c in SPORTS.items() if c["code"] == sec), None)
        if not sk:
            continue
        html = get(f"{BASE}/DB/general/results.html?sectorcode={sec}&raceid={rid}")
        if not html:
            log(f"  NAPAKA: {item} ni dosegljiva")
            continue
        status, race = parse_race(html, sk, rid, extra_names, cat)
        key = f"{sec}:{rid}"
        if status == "ok" and race["res"]:
            races[race["id"]] = race
            state["seen"][key] = "ok"
            n += 1
            log(f"  (ponovno) {race['d']} {race['place']} {race['disc']} {race['g']}: "
                f"{', '.join(str(x['r']) + '. ' + x['n'] for x in race['res'])}")
        else:
            log(f"  OPOZORILO: {item} ni bila dodana (status {status})")
    return n


def add_extra_races(races, state, extra_names):
    """Doda tekme iz EXTRA_RACES neposredno (brez koledarja)."""
    n = 0
    for item in EXTRA_RACES:
        sec, rid = item.split(":")
        sk = next((k for k, c in SPORTS.items() if c["code"] == sec), None)
        if not sk:
            continue  # tekma se vedno znova prebere in prepise (popravek ze shranjenih)
        html = get(f"{BASE}/DB/general/results.html?sectorcode={sec}&raceid={rid}")
        if not html:
            log(f"  NAPAKA: {item} ni dosegljiva")
            continue
        status, race = parse_race(html, sk, rid, extra_names, "WC", trusted=True)
        if status == "ok" and race["res"]:
            races[race["id"]] = race
            state["seen"][item] = "ok"
            n += 1
            log(f"  (dodatna tekma) {race['d']} {race['place']} {race['disc']} {race['g']}: "
                f"{', '.join(str(x['r']) + '. ' + x['n'] for x in race['res'])}")
        else:
            log(f"  OPOZORILO: dodatna tekma {item} ni bila dodana (status {status})")
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["weekly", "backfill"], default="weekly")
    ap.add_argument("--from-season", type=int, default=1992, help="zimska sezona: koda = koncno leto (1992 = 1991/92)")
    ap.add_argument("--no-summer", action="store_true", help="preskoci poletno sezono (Grand Prix)")
    ap.add_argument("--max-minutes", type=int, default=330)
    ap.add_argument("--reset-sport", help="(alpine, jumping, nordic ali vec z vejicami) zavrze zbrane podatke tega sporta (zimske in poletne) in jih zbere znova")
    ap.add_argument("--refilter", action="store_true", help="ponovno preveri ze shranjene tekme z novim filtrom in odstrani neprave")
    ap.add_argument("--recheck", help="odstrani te tekme iz seznama ze obdelanih (npr. AL:49869,JP:4401), da se obdelajo znova")
    ap.add_argument("--probe", help="samo bere oznake vrst za tekme, npr. JP:6696,JP:7566")
    ap.add_argument("--redo", action="store_true", help="ponovno prebere ekipne tekme OWG/WSC iz REDO_CHAMP_RACES")
    ap.add_argument("--champs", action="store_true", help="zbere samo olimpijske igre (OWG) in svetovna prvenstva (WSC) od sezone --from-season")
    ap.add_argument("--probe-cat", help="dogodki in tekme kategorije, npr. JP:2022:OWG,JP:2023:WSC,AL:2022:OWG")
    ap.add_argument("--probe-team2", help="zgradba ekipne tekme: JP:7460,JP:6853 ali alpski naslovi AL:2024")
    ap.add_argument("--probe-team", help="izpise ekipne tekme sezone, npr. JP:2026,AL:2026")
    ap.add_argument("--validate", action="store_true", help="po zbiranju primerja stetje z FIS Podiums")
    ap.add_argument("--check-races", action="store_true", help="vsako uvrstitev primerja s stranjo sportnika na FIS (po raceid)")
    ap.add_argument("--only-validate", action="store_true", help="samo preveri, brez zbiranja")
    ap.add_argument("--add-extra", action="store_true", help="doda tekme iz EXTRA_RACES (letalnice SFWC+WC), ki jih koledar ne navaja")
    ap.add_argument("--teams", action="store_true", help="ponovno obdela strani, ki so bile prej preskocene (ekipne tekme), brez brisanja posamicnih")
    ap.add_argument("--force", action="store_true", help="ponovno obdela tudi sezone, oznacene kot popolne")
    args = ap.parse_args()
    if args.probe_cat:
        probe_cat(args.probe_cat)
        return 0
    if args.probe_team2:
        probe_team2(args.probe_team2)
        return 0
    if args.probe_team:
        probe_team(args.probe_team)
        return 0
    if args.probe:
        probe(args.probe)
        return 0

    today = datetime.date.today()
    cur = current_season_code(today)
    code_of = {cfg["label"]: cfg["code"].lower() for cfg in SPORTS.values()}
    loaded = load_json(DATA / "races.json", [])
    loaded = [r for r in loaded if f"{next(c['code'] for c in SPORTS.values() if c['label'] == r['sport'])}:{r['fis']}" not in EXCLUDE_RACES]
    for r in loaded:  # stara oblika id "fis-123" je bila skupna za vse sporte; zdaj "al-123", "jp-123"
        r["id"] = f"{code_of[r['sport']]}-{r['fis']}"
    races = {r["id"]: r for r in loaded}
    state = load_json(DATA / "state.json", {"seen": {}})
    state.setdefault("seen", {})
    meta = load_json(DATA / "meta.json", {"coverage": {}})
    meta.setdefault("coverage", {})
    extra_names = load_json(DATA / "names.json", {})
    deadline = time.time() + args.max_minutes * 60

    if args.recheck:
        n_rc = 0
        for item in args.recheck.split(","):
            item = item.strip()
            if item and state["seen"].pop(item, None) is not None:
                n_rc += 1
            sec_, _, rid_ = item.partition(":")
            races.pop(f"{sec_.lower()}-{rid_}", None)
        log(f"Ponovna obdelava: {n_rc} tekem odstranjenih iz seznama obdelanih")

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

    global TEAMS_DONE
    if args.teams:
        TEAMS_DONE = set(state.get("teams_done", []))
        args.mode, args.force = "backfill", True
        log(f"Ekipne tekme: ze ponovno obdelanih {len(TEAMS_DONE)} prej preskocenih strani")

    if args.refilter:
        finished_all, n = refilter(races, state, extra_names, deadline)
        log(f"Ponovni pregled: odstranjenih {n} tekem; oznake vrst: {CODES_SEEN}")
        if finished_all:
            state.pop("refiltered", None)
        save_all(races, state, meta, today)
        if finished_all and args.validate:
            validate(races, today, deadline)
        return 0

    if args.redo:
        n = redo_races(races, state, extra_names, REDO_CHAMP_RACES)
        log(f"Ponovno prebranih tekem: {n} od {len(REDO_CHAMP_RACES)}")
        save_all(races, state, meta, today)
        if args.validate:
            validate(races, today, deadline)
        return 0

    if args.add_extra:
        n = add_extra_races(races, state, extra_names)
        log(f"Dodatne tekme: dodanih {n}")
        save_all(races, state, meta, today)
        if args.validate:
            validate(races, today, deadline)
        return 0

    if args.only_validate:
        validate(races, today, deadline)
        return 0

    if args.check_races:
        check_races(races, today, deadline)
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

    CHAMP_NAMES = {"OWG": "Olimpijske igre", "WSC": "Svetovno prvenstvo"}
    champ_jobs = []
    for code in ([cur] if args.mode == "weekly" and not args.champs else list(range(cur, args.from_season - 1, -1))):
        for cat in ("OWG", "WSC"):
            for sk in ("alpine", "jumping", "nordic"):
                champ_jobs.append((code, sk, cat, f"{CHAMP_NAMES[cat]} {code}", code < cur))
    if args.champs:
        jobs = champ_jobs
    else:
        jobs = jobs + champ_jobs
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
    add_extra_races(races, state, extra_names)
    if TEAMS_DONE is not None:
        state.pop("teams_done", None)
        log("Ekipne tekme: vse strani ponovno obdelane.")
    save_all(races, state, meta, today)
    if args.validate:
        validate(races, today, deadline)
    log(f"Konec. Tekem s slovenskimi uvrstitvami: {len(races)}; oznake vrst tekem: {CODES_SEEN}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
