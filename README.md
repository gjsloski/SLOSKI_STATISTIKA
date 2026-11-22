# SloSki statistika

Javna stran z rezultati slovenskih športnikov v svetovnem pokalu, ki se osvežuje samodejno.
Naslov: https://gjsloski.github.io/SLOSKI_STATISTIKA/

## Kaj stran vsebuje

- Panoge: alpsko smučanje, smučarski skoki, nordijska kombinacija, smučarski teki, deskanje na snegu, telemark, freestyle, biatlon ter para alpsko smučanje in para smučarski teki.
- Načini: zimska sezona, poletna sezona (Grand Prix), svetovna prvenstva, olimpijske in paralimpijske igre.
- Zgodovina od sezone 1991/92. Pregled po športnikih, sezonah, disciplinah in panogah, rezultati tekem, koledar svetovnega pokala, iskanje, razvrščanje ter izvoz v Excel in PDF.
- Stolpci: 1., 2., 3. mesto ter Top 5 / 10 / 15 / 30 (kumulativno).
- Viri: FIS (fis-ski.com) in IBU (biatlon).

## Datoteke

| Datoteka | Namen |
|---|---|
| `index.html` | stran (vse v eni datoteki) |
| `scripts/update.py` | zbiranje podatkov s FIS in IBU |
| `.github/workflows/update.yml` | GitHub Actions: samodejno in ročno zbiranje ter objava strani |
| `data/races.json` | vse tekme z uvrstitvami slovenskih športnikov |
| `data/meta.json`, `data/state.json` | čas zadnje posodobitve, naslednja osvežitev, stanje zbiranja |
| `data/koledar.json` | koledar svetovnega pokala za trenutno sezono |
| `data/names.json` | imena s šumniki po FIS identifikatorju |
| `data/globusi.json`, `data/osebe.json`, `data/jubileji.json` | globusi, osebe, jubileji |
| `data/validation.json` | rezultat primerjave s FIS stranmi športnikov |

## Samodejno osveževanje

Ob petkih, sobotah in nedeljah ob 12.00, 15.00, 17.00, 19.00, 21.00 in 23.00 ter ob ponedeljkih ob 6.00 (slovenski čas). Na strani gumb »Osveži prikaz« prebere že objavljene podatke brez ponovnega nalaganja strani; novega zbiranja ne sproži.

## Ročni zagon (Actions → Posodobitev statistike → Run workflow → mode)

- `weekly` – tekoča sezona, koledar in para
- `backfill` – zgodovina (traja več ur, ob prekinitvi nadaljuje)
- `biathlon` – biatlon
- `calendar` – samo koledar
- `para` – samo para tekmovalci
- `check` / `validate` / `check-ibu` – primerjava z uradnimi stranmi športnikov (FIS, IBU)
- `list-names` – imena, ki še nimajo šumnikov
- `reset-…` – ponastavi in ponovno zbere posamezno panogo
- `probe-…` – diagnostika (izpis strani FIS)

## Prva namestitev

1. Javni repozitorij na GitHubu.
2. Naloži `index.html`, `scripts/update.py` in `.github/workflows/update.yml`.
3. Settings → Pages → Source: **GitHub Actions**.
4. Settings → Actions → General → Workflow permissions: **Read and write permissions**.
5. Actions → Posodobitev statistike → Run workflow → `weekly`, nato `backfill` za zgodovino.
6. Ko je zagon zelen, je naslov strani v koraku »deploy« (in v Settings → Pages).
