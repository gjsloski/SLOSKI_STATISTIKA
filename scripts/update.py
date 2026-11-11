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