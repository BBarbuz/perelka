#!/usr/bin/env python3
"""
Perełka v0.2 — wyszukiwarka najnowszych ogłoszeń z OLX i Vinted w terminalu.

Pobiera najnowsze ogłoszenia, odrzuca te, które nie pasują do szukanej rzeczy
(np. pad albo etui zamiast konsoli), liczy cenę referencyjną (medianę)
i wskazuje oferty wyraźnie poniżej niej. Może działać ciągle (np. 6 godzin),
odświeżając wyniki co kilka minut i oznaczając nowe ogłoszenia oraz obniżki.

Działa na samej bibliotece standardowej Pythona 3 (Fedora ma go od razu).

Przykłady:
    ./perelka.py ps5
    ./perelka.py ps5 --czas 6h --co 5m
    ./perelka.py "iphone 13" --min-cena 500 --wyklucz pro,mini --czas 2h
    ./perelka.py ps5 --zrodla olx --limit 100 --okazje
"""

from __future__ import annotations

import argparse
import gzip
import http.cookiejar
import json
import os
import random
import re
import shutil
import statistics
import subprocess
import sys
import time
import unicodedata
from functools import lru_cache
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

WERSJA = "0.2"
UA = "Mozilla/5.0 (X11; Linux x86_64; rv:132.0) Gecko/20100101 Firefox/132.0"
TIMEOUT = 25
MIN_ODSTEP = 60  # sekundy — nie odpytujemy serwisów częściej niż co minutę


# ───────────────────────────── model danych ─────────────────────────────

@dataclass
class Oferta:
    zrodlo: str
    tytul: str
    cena: float | None
    waluta: str
    url: str
    dodano: datetime | None = None
    lokalizacja: str = ""
    do_negocjacji: bool = False
    id_num: int = 0
    # wypełniane podczas analizy / monitorowania
    data_przyblizona: bool = False  # dodano = moment pierwszego wykrycia (Vinted)
    widziano: datetime | None = None
    runda: int = 0                  # w której rundzie pierwszy raz wykryta
    nowa: bool = False              # pojawiła się w trakcie monitorowania
    poprzednia_cena: float | None = None
    obnizka_runda: int = 0
    pasuje: bool = True
    powod: str = ""
    ocena: str = ""
    roznica: float | None = None  # względem mediany, np. -0.18 = 18% taniej

    @property
    def klucz(self) -> str:
        return self.url.split("?")[0]


# ───────────────────────────── normalizacja tekstu ─────────────────────────────

_PL = str.maketrans("ąćęłńóśźż", "acelnoszz")

# Różne zapisy tego samego modelu sprowadzane do jednej postaci.
ALIASY = [
    (r"\bplay\s*station\s*(\d)\b", r"ps\1"),
    (r"\bx\s*box\b", "xbox"),
    (r"\bi\s+phone\b", "iphone"),
    (r"\bmac\s+book\b", "macbook"),
    (r"\bpro\s*max\b", "pro max"),
    (r"\b(\d+)\s*(gb|tb)\b", r"\1\2"),
]


# Długie nazwy, w których sprzedający często robią literówki ("Playsation", "Nitendo").
# Poprawiane są tylko słowa zaczynające się tą samą literą i o podobnej długości.
SLOWNIK_LITEROWEK = ["playstation", "nintendo", "dualsense", "samsung", "macbook", "thinkpad",
                     "motorola", "steamdeck"]


@lru_cache(maxsize=None)
def odleglosc(a: str, b: str) -> int:
    """Odległość edycyjna z zamianą sąsiednich liter (Damerau-Levenshtein, wariant OSA)."""
    if abs(len(a) - len(b)) > 2:
        return 3
    d = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(len(a) + 1):
        d[i][0] = i
    for j in range(len(b) + 1):
        d[0][j] = j
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            koszt = a[i - 1] != b[j - 1]
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + koszt)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                d[i][j] = min(d[i][j], d[i - 2][j - 2] + 1)
    return d[len(a)][len(b)]


def dopuszczalna_literowka(slowo: str) -> int:
    """Ile pomyłek tolerujemy: krótkie słowa i liczby/modele (ps5, 13, s23) — żadnych."""
    if not slowo.isalpha() or len(slowo) < 7:
        return 0
    return 1 if len(slowo) < 10 else 2


def literowka(token: str, wzor: str) -> bool:
    n = dopuszczalna_literowka(wzor)
    return (n > 0 and token.isalpha() and token[0] == wzor[0]
            and odleglosc(token, wzor) <= n)


@lru_cache(maxsize=4096)
def _popraw_slowo(tok: str) -> str:
    for w in SLOWNIK_LITEROWEK:
        if tok != w and literowka(tok, w):
            return w
    return tok


def popraw_literowki(t: str) -> str:
    toks = t.split()
    wynik, i = [], 0
    while i < len(toks):
        # "play staton" -> "playstation"
        if i + 1 < len(toks) and toks[i] in ("play", "plai", "pley"):
            sklejone = _popraw_slowo(toks[i] + toks[i + 1])
            if sklejone in SLOWNIK_LITEROWEK:
                wynik.append(sklejone)
                i += 2
                continue
        wynik.append(_popraw_slowo(toks[i]))
        i += 1
    return " ".join(wynik)


def _podstawowa(tekst: str, wzorzec: bool = False) -> str:
    t = tekst.lower().translate(_PL)
    if not wzorzec:
        t = t.replace("*", " ")
    t = unicodedata.normalize("NFKD", t)
    t = "".join(z for z in t if not unicodedata.combining(z))
    t = t.replace("+", " + ").replace("&", " + ")
    t = re.sub(r"[^a-z0-9+*]+", " ", t)
    if not wzorzec:
        t = popraw_literowki(t)
    for wzor, zamiana in ALIASY:
        t = re.sub(wzor, zamiana, t)
    return t


def _rozdziel_model(t: str) -> str:
    # "ps5" -> "ps 5", "iphone13" -> "iphone 13", "rtx3060" -> "rtx 3060"
    # Dzięki temu "PS5", "PS 5" i "PlayStation 5" wyglądają tak samo.
    return re.sub(r"\b([a-z]+)(\d{1,4})\b", r"\1 \2", t)


def tokeny(tekst: str, wzorzec: bool = False) -> list[str]:
    """Zamienia tekst na listę znormalizowanych słów (wzorzec=True zachowuje gwiazdki)."""
    return _rozdziel_model(_podstawowa(tekst, wzorzec)).split()


def frazy_zapytania(zapytanie: str) -> list[list[str]]:
    """
    Dzieli zapytanie na frazy, które muszą wystąpić w tytule.
    "ps5 slim" -> [["ps", "5"], ["slim"]]  ("ps" i "5" muszą stać obok siebie)
    "iphone 13" -> [["iphone", "13"]]
    """
    t = _podstawowa(zapytanie)
    t = re.sub(r"\b([a-z]{2,})\s+(\d{1,4})\b", r"\1\2", t)
    return [_rozdziel_model(slowo).split() for slowo in t.split() if slowo != "+"]


def wzorce(lista: list[str]) -> list[list[str]]:
    """Kompiluje listę słów/fraz (z opcjonalną gwiazdką na końcu słowa) do wzorców."""
    wynik = []
    for wpis in lista:
        czesci = tokeny(wpis, wzorzec=True)
        if czesci:
            wynik.append(czesci)
    return wynik


def _slowo_pasuje(token: str, wzor: str) -> bool:
    if wzor.endswith("*"):
        return token.startswith(wzor[:-1])
    return token == wzor


def znajdz(toks: list[str], fraza: list[str], start: int = 0, negacja: bool = False,
           literowki: bool = False) -> int:
    """Indeks pierwszego wystąpienia frazy w tokenach albo -1.
    Przy negacja=True pomija wystąpienia poprzedzone słowem "nie"/"bez".
    Przy literowki=True długie słowa mogą mieć 1–2 pomyłki ("nintedno" = "nintendo")."""
    n = len(fraza)
    for i in range(start, len(toks) - n + 1):
        if all(_slowo_pasuje(toks[i + j], fraza[j])
               or (literowki and literowka(toks[i + j], fraza[j])) for j in range(n)):
            if negacja and i > 0 and toks[i - 1] in ("nie", "bez"):
                continue
            return i
    return -1


def znajdz_dowolna(toks: list[str], frazy: list[list[str]], negacja: bool = False):
    """Zwraca (indeks, fraza) najwcześniejszego wystąpienia którejkolwiek frazy."""
    najlepszy = (-1, None)
    for f in frazy:
        i = znajdz(toks, f, negacja=negacja)
        if i >= 0 and (najlepszy[0] < 0 or i < najlepszy[0]):
            najlepszy = (i, f)
    return najlepszy


# ───────────────────────────── reguły dopasowania ─────────────────────────────

# Zawsze odrzucane — to nie są oferty sprzedaży sprawnego przedmiotu.
ODRZUC_ZAWSZE = [
    "kupie", "kupi*", "skupuje", "skup", "szukam", "poszukuj*",
    "ieskau", "hledam", "keresek", "caut", "kaufe", "suche", "wanted",
    "uszkodzon*", "na czesci", "nie dziala*", "niedziala*", "nie wlacza*",
    "zepsut*", "zbit*", "pekniet*", "zablokowan*", "blokada", "icloud",
    "atrapa", "makieta", "replika",
    "wynajem", "wynajm*", "wypozycz*", "naprawa", "naprawy", "serwis*",
    "samo pudelko", "tylko pudelko", "puste pudelko", "pudelko po", "pudelko od",
    "karton po", "karton od", "pusty karton", "opakowanie po",
]

# Przyimki, które przed szukaną nazwą oznaczają akcesorium: "pad DO ps5", "etui NA iphone".
PRZYIMKI = ["do", "na", "dla", "pod", "for", "kompatybiln*", "za", "pre", "pentru", "fur", "per"]

# Słowa, które w ogłoszeniu sygnalizują zestaw ("ps5 + pad", "ps5 z grą").
ZESTAW = ["+", "z", "i", "oraz", "plus", "w", "with", "and", "zestaw*", "komplet*",
          "gratis", "dodatk*", "bundle"]

# Akcesoria w innych językach — Vinted pokazuje też ogłoszenia z CZ, SK, HU, RO, LT, HR...
OBCE_AKCESORIA = [
    "kryt", "obal", "maskic*", "husa", "huse", "tok", "telefontok", "hulle", "cover*",
    "custodia", "funda", "coque", "charger", "incarcator*", "nabijeck*", "toltő", "tolto",
    "glass", "protector", "stand", "suport", "dekla", "dėklas", "deklas",
]

PROFILE = {
    "ogolny": {
        "opis": "dowolny przedmiot (podstawowy filtr akcesoriów)",
        "akcesoria": [
            "etui", "case", "pokrowiec*", "plecki", "folia", "folie", "szklo", "szkielko",
            "hartowan*", "ladowar*", "kabel", "kabl*", "zasilacz*", "uchwyt*", "podstawk*",
            "stojak*", "naklejk*", "skin*", "obudow*", "adapter*", "pasek", "torba*", "torb*",
            *OBCE_AKCESORIA,
        ],
        "pozytywne": [],
    },
    "konsola": {
        "opis": "konsole do gier (PS, Xbox, Nintendo, Steam Deck...)",
        "akcesoria": [
            "pad", "pady", "padow", "kontroler*", "controller*", "dualsense", "dualshock",
            "joy*", "gamepad*", "ovladac*",
            "gra", "gry", "gier", "gre", "game", "games", "plyt*", "steelbook*",
            "hra", "hry", "joc", "jocuri", "jatek", "spiel", "edition*",
            "etui", "case", "pokrowiec*", "torb*", "obudow*", "panel*", "skin*", "naklejk*",
            "folia", "kabel", "kabl*", "hdmi", "ladowar*", "stacj*", "podstawk*", "stojak*",
            "chlodz*", "wentylator*", "sluchawk*", "headset*", "pulse", "kamer*",
            "kierownic*", "naped*", "dysk*", "ssd", "pilot*", "portal", "vr", "gogle",
            "karta", "karty", "psn", "doladowan*", "kod", "kody", "voucher*", "subskrypc*",
            "abonament*", "konto", "uchwyt*", "zasilacz*", "adapter*", "amiibo", "galk*",
            *OBCE_AKCESORIA,
        ],
        "pozytywne": [
            "konsol*", "console*", "cfi*", "digital", "disc", "slim", "standard", "oled",
        ],
        # Inne platformy w tytule ("gra ps4/ps5", "pad ps4 pc") = gra albo akcesorium.
        "platformy": ["ps 1", "ps 2", "ps 3", "ps 4", "ps 5", "psp", "psx", "pc", "xbox", "switch",
                      "nintendo", "wii", "ps vita"],
    },
    "telefon": {
        "opis": "telefony / smartfony",
        "akcesoria": [
            "etui", "case", "pokrowiec*", "plecki", "folia", "folie", "szklo", "szkielko",
            "hartowan*", "ladowar*", "kabel", "kabl*", "zasilacz*", "uchwyt*", "obudow*",
            "wyswietlacz*", "lcd", "matryc*", "tasm*", "klapk*", "rysik*", "pasek",
            "sluchawk*", "airpods", "glosnik*", "powerbank*", "adapter*", "podstawk*",
            "stojak*", "naklejk*", "skin*", "karta", "magsafe",
            *OBCE_AKCESORIA,
        ],
        "pozytywne": ["telefon*", "smartfon*", "smartphone*"],
    },
    "laptop": {
        "opis": "laptopy / notebooki",
        "akcesoria": [
            "torba*", "torb*", "plecak*", "pokrowiec*", "etui", "zasilacz*", "ladowar*",
            "bateri*", "klawiatur*", "matryc*", "wyswietlacz*", "obudow*", "palmrest*",
            "zawias*", "wentylator*", "chlodz*", "podstawk*", "stacj*", "dock*", "naklejk*",
            "skin*", "plyta glowna", "mysz*", "kabel", "kabl*", "adapter*", "folia",
            *OBCE_AKCESORIA,
        ],
        "pozytywne": ["laptop*", "notebook*", "ultrabook*", "komputer*"],
    },
}

# Automatyczny wybór profilu na podstawie zapytania.
AUTO_PROFIL = {
    "konsola": ["ps*", "playstation", "xbox", "switch", "nintendo", "steam", "deck",
                "konsol*", "wii", "gameboy"],
    "telefon": ["iphone", "galaxy", "pixel", "xiaomi", "redmi", "poco", "oneplus", "motorola",
                "moto", "huawei", "oppo", "realme", "honor", "telefon*", "smartfon*"],
    "laptop": ["laptop*", "macbook", "thinkpad", "notebook*", "zenbook", "vivobook", "ideapad",
               "legion", "latitude", "elitebook", "probook", "xps", "surface", "aspire",
               "pavilion", "omen", "rog", "tuf"],
}


def wybierz_profil(zapytanie: str) -> str:
    toks = tokeny(zapytanie)
    for profil, slowa in AUTO_PROFIL.items():
        if any(_slowo_pasuje(t, s) for t in toks for s in slowa):
            return profil
    return "ogolny"


class Filtr:
    """Dopasowanie słów kluczowych — sprawdza, czy ogłoszenie to szukany przedmiot."""

    def __init__(self, zapytanie: str, profil: str, wyklucz: list[str], wymagane: list[str],
                 bez_akcesoriow: bool = False):
        self.frazy = frazy_zapytania(zapytanie)
        self.wymagane = wzorce(wymagane)
        self.wyklucz = wzorce(wyklucz)
        self.zawsze = wzorce(ODRZUC_ZAWSZE)
        self.przyimki = wzorce(PRZYIMKI)
        self.zestaw = wzorce(ZESTAW)
        p = PROFILE[profil]
        slowa_zapytania = {t for f in self.frazy for t in f}
        wszystkie = wzorce(p["akcesoria"])
        akcesoria = [w for w in wszystkie
                     if not (len(w) == 1 and any(_slowo_pasuje(s, w[0]) for s in slowa_zapytania))]
        # Jeśli ktoś szuka akcesorium (np. "pad ps5"), filtr akcesoriów nie ma sensu.
        szuka_akcesorium = len(akcesoria) < len(wszystkie)
        self.akcesoria = [] if (bez_akcesoriow or szuka_akcesorium) else akcesoria
        self.pozytywne = wzorce(p["pozytywne"])
        self.platformy = [] if not self.akcesoria else [
            w for w in wzorce(p.get("platformy", []))
            if not any(znajdz(f, w) >= 0 or znajdz(w, f) >= 0 for f in self.frazy)]

    def pewny(self, tytul: str) -> bool:
        """Czy tytuł zawiera słowa jednoznacznie wskazujące na szukany przedmiot
        (konsola, slim, 128GB...)? Takie oferty wyznaczają cenę odniesienia."""
        return self._pozytywne_w(tokeny(tytul))

    def _pozytywne_w(self, toks: list[str]) -> bool:
        if znajdz_dowolna(toks, self.pozytywne)[0] >= 0:
            return True
        return any(re.fullmatch(r"\d+(gb|tb)", t) for t in toks)

    def sprawdz(self, tytul: str) -> tuple[bool, str]:
        toks = tokeny(tytul)

        # 1. Wszystkie frazy z zapytania muszą być w tytule.
        poczatek, koniec = None, None
        for f in self.frazy:
            i = znajdz(toks, f, literowki=True)
            if i < 0:
                return False, f"brak słowa „{' '.join(f)}”"
            if poczatek is None or i < poczatek:
                poczatek, koniec = i, i + len(f)
        for f in self.wymagane:
            if znajdz(toks, f) < 0:
                return False, f"brak wymaganego „{' '.join(f)}”"

        # 2. Ogłoszenia kupna, uszkodzone, wynajem itp.
        i, f = znajdz_dowolna(toks, self.zawsze, negacja=True)
        if i >= 0:
            return False, f"odrzucone: „{' '.join(toks[i:i + len(f)])}”"

        # 3. Słowa wykluczone przez użytkownika.
        i, f = znajdz_dowolna(toks, self.wyklucz)
        if i >= 0:
            return False, f"wykluczone: „{' '.join(toks[i:i + len(f)])}”"

        if not self.akcesoria:
            return True, ""

        # 4. Co stoi PRZED szukaną nazwą? Pierwsze rozstrzygające słowo decyduje:
        #    "Konsola ... PS5" -> OK, "Pad DualSense PS5" / "Etui do PS5" -> akcesorium.
        przed = toks[:poczatek]
        for j in range(len(przed)):
            reszta = przed[j:j + 3]
            if any(znajdz(reszta, w) == 0 for w in self.akcesoria):
                return False, f"akcesorium: „{przed[j]}” przed nazwą"
            if any(znajdz(reszta, w) == 0 for w in self.pozytywne):
                break
            if any(znajdz(reszta, w) == 0 for w in self.przyimki):
                return False, f"akcesorium: „{' '.join(przed[j:])} {' '.join(toks[poczatek:koniec])}”"

        # 4b. Kilka platform w tytule ("Nioh 2 ps4/ps5", "gitara ps4 ps5 pc").
        i, f = znajdz_dowolna(toks, self.platformy)
        if i >= 0 and not self._pozytywne_w(toks):
            return False, f"inna platforma „{' '.join(f)}” — raczej gra/akcesorium"

        # 5. Co stoi ZA nazwą? "PS5 pad" -> akcesorium, ale "PS5 + pad", "PS5 z padem",
        #    "Konsola PS5 Slim 2 pady" -> zestaw z konsolą, więc OK.
        po = toks[koniec:]
        i, f = znajdz_dowolna(po, self.akcesoria)
        if i >= 0:
            if self._pozytywne_w(toks):
                return True, ""
            przed_akc = po[:i]
            if znajdz_dowolna(przed_akc, self.zestaw)[0] >= 0 or any(t.isdigit() for t in przed_akc):
                return True, ""
            return False, f"akcesorium: „{po[i]}” bez oznak zestawu"

        return True, ""


# ───────────────────────────── pobieranie ─────────────────────────────

class BladZrodla(Exception):
    pass


# Jeden "klient" z ciasteczkami na cały czas działania programu — jak przeglądarka.
# Cloudflare (Vinted) rzadziej blokuje, gdy ciasteczka __cf_bm itp. są odsyłane.
_SLOIK = http.cookiejar.CookieJar()
_OTWIERACZ = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_SLOIK))

_NAGLOWKI_HTML = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1", "Upgrade-Insecure-Requests": "1",
}


def _pobierz(url: str, accept: str, debug: bool) -> str:
    if debug:
        print(f"[debug] GET {url}", file=sys.stderr)
    naglowki = {"User-Agent": UA, "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.5",
                "Accept-Encoding": "gzip"}
    naglowki.update(_NAGLOWKI_HTML if accept == "text/html" else {"Accept": accept})
    req = urllib.request.Request(url, headers=naglowki)
    try:
        with _OTWIERACZ.open(req, timeout=TIMEOUT) as odp:
            dane = odp.read()
            if odp.headers.get("Content-Encoding", "").lower() == "gzip":
                dane = gzip.decompress(dane)
            return dane.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        if e.headers.get("cf-mitigated") or e.code in (403, 429) and "cloudflare" in str(
                e.headers.get("server", "")).lower():
            raise BladZrodla(f"HTTP {e.code} — tymczasowa blokada anty-bot (Cloudflare), "
                             "ponowię później") from e
        if e.code == 429:
            raise BladZrodla("HTTP 429 — za dużo zapytań, ponowię później") from e
        raise BladZrodla(f"HTTP {e.code} ({e.reason})") from e
    except urllib.error.URLError as e:
        raise BladZrodla(f"brak połączenia: {e.reason}") from e
    except (TimeoutError, OSError) as e:
        raise BladZrodla(f"błąd sieci: {e}") from e


def _data_iso(tekst):
    if not tekst:
        return None
    try:
        return datetime.fromisoformat(tekst.replace("Z", "+00:00"))
    except ValueError:
        return None


def _liczba(tekst) -> float | None:
    """'1 299,99 zł' -> 1299.99"""
    if tekst is None:
        return None
    if isinstance(tekst, (int, float)):
        return float(tekst)
    t = re.sub(r"[^\d,.]", "", str(tekst)).replace(",", ".")
    try:
        return float(t) if t else None
    except ValueError:
        return None


# ── OLX ── publiczne JSON API strony olx.pl

def parsuj_olx(it: dict) -> Oferta:
    cena, waluta, neg = None, "PLN", False
    for p in it.get("params") or []:
        if p.get("key") == "price":
            v = p.get("value") or {}
            if v.get("type") not in ("exchange", "free"):
                cena = _liczba(v.get("value"))
                if cena is None:
                    cena = _liczba(v.get("label"))
            waluta = v.get("currency") or "PLN"
            neg = bool(v.get("negotiable"))
    lok = ((it.get("location") or {}).get("city") or {}).get("name", "")
    return Oferta(
        zrodlo="OLX", tytul=(it.get("title") or "").strip(), cena=cena or None, waluta=waluta,
        url=it.get("url", ""), dodano=_data_iso(it.get("created_time")),
        lokalizacja=lok, do_negocjacji=neg, id_num=int(it.get("id") or 0),
    )


def pobierz_olx(zapytanie: str, limit: int, kategoria: str | None, debug: bool) -> list[Oferta]:
    oferty: list[Oferta] = []
    offset = 0
    while len(oferty) < limit and offset < 1000:
        ile = min(40, limit - len(oferty))
        params = {"offset": offset, "limit": ile, "query": zapytanie, "sort_by": "created_at:desc"}
        if kategoria:
            params["category_id"] = kategoria
        url = "https://www.olx.pl/api/v1/offers/?" + urllib.parse.urlencode(params)
        tekst = _pobierz(url, "application/json", debug)
        try:
            dane = json.loads(tekst)
        except json.JSONDecodeError as e:
            raise BladZrodla("odpowiedź nie jest JSON-em (możliwa blokada anty-bot)") from e
        porcja = dane.get("data") or []
        oferty.extend(parsuj_olx(it) for it in porcja)
        if len(porcja) < ile:
            break
        offset += ile
    return oferty[:limit]


# ── Vinted ── stary endpoint /api/v2/catalog/items zwraca 404, więc czytamy dane
#    osadzone w stronie katalogu (Next.js: self.__next_f.push([1,"..."])).

_NEXT_F = re.compile(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)')
_ZNACZNIK_VINTED = '"items":{"items":['


def wyciagnij_vinted(html: str) -> tuple[list[dict], dict]:
    """Zwraca (lista elementów, paginacja) ze strony katalogu Vinted."""
    czesci = []
    for c in _NEXT_F.findall(html):
        try:
            czesci.append(json.loads('"' + c + '"'))
        except json.JSONDecodeError:
            pass
    dane = "".join(czesci)
    i = dane.find(_ZNACZNIK_VINTED)
    if i < 0:
        raise BladZrodla("nie znaleziono listy ogłoszeń na stronie (Vinted zmienił stronę?)")
    dekoder = json.JSONDecoder()
    elementy, koniec = dekoder.raw_decode(dane, i + len(_ZNACZNIK_VINTED) - 1)
    paginacja = {}
    j = dane.find('"pagination":', koniec)
    if 0 <= j < koniec + 200:
        try:
            paginacja = dekoder.raw_decode(dane, j + len('"pagination":'))[0]
        except json.JSONDecodeError:
            pass
    return elementy, paginacja


def parsuj_vinted(el: dict, baza: str) -> Oferta | None:
    it = el.get("productItem") or el
    if it.get("isPromoted"):
        return None  # promowane nie są "najnowsze"
    cena_d = it.get("price") or {}
    if isinstance(cena_d, dict):
        cena = _liczba(cena_d.get("amount"))
        waluta = cena_d.get("currencyCode") or cena_d.get("currency_code") or "PLN"
    else:
        cena, waluta = _liczba(cena_d), it.get("currency") or "PLN"
    url = it.get("url") or f"/items/{it.get('id')}"
    if url.startswith("/"):
        url = baza + url
    return Oferta(zrodlo="Vinted", tytul=(it.get("title") or "").strip(), cena=cena,
                  waluta=waluta, url=url, id_num=int(it.get("id") or el.get("id") or 0))


def pobierz_vinted(zapytanie: str, limit: int, katalog: str | None, domena: str,
                   debug: bool) -> list[Oferta]:
    baza = f"https://www.vinted.{domena}"
    oferty: list[Oferta] = []
    strona = 1
    while len(oferty) < limit:
        params = [("search_text", zapytanie), ("order", "newest_first"), ("page", strona)]
        for k in (katalog or "").split(","):
            if k.strip():
                params.append(("catalog[]", k.strip()))
        url = f"{baza}/catalog?" + urllib.parse.urlencode(params)
        elementy, pag = wyciagnij_vinted(_pobierz(url, "text/html", debug))
        for el in elementy:
            o = parsuj_vinted(el, baza)
            if o:
                oferty.append(o)
        if not elementy or strona >= int(pag.get("total_pages") or 1):
            break
        strona += 1
    return oferty[:limit]


# ───────────────────────────── ocena cen ─────────────────────────────

@dataclass
class Statystyki:
    liczba: int
    mediana: float
    srednia: float
    minimum: float
    maksimum: float
    waluta: str


def policz_statystyki(oferty: list[Oferta]) -> Statystyki | None:
    z_cena = [o for o in oferty if o.pasuje and o.cena]
    if not z_cena:
        return None
    waluta = statistics.mode(o.waluta for o in z_cena)
    ceny = sorted(o.cena for o in z_cena if o.waluta == waluta)
    # Odcinamy wartości odstające (rozstęp międzykwartylowy), żeby pojedyncze
    # "1 zł" albo "99 999 zł" nie psuły średniej.
    if len(ceny) >= 4:
        q1, _, q3 = statistics.quantiles(ceny, n=4)
        iqr = q3 - q1
        ceny_ok = [c for c in ceny if q1 - 1.5 * iqr <= c <= q3 + 1.5 * iqr] or ceny
    else:
        ceny_ok = ceny
    return Statystyki(len(ceny_ok), statistics.median(ceny_ok), statistics.fmean(ceny_ok),
                      min(ceny_ok), max(ceny_ok), waluta)


def filtr_cenowy(oferty: list[Oferta], filtr: "Filtr", prog: float = 0.35, min_kotwic: int = 3):
    """
    Oferty z "pewnymi" słowami (np. "Konsola PS5 Slim 825GB") wyznaczają cenę odniesienia.
    Oferty bez takich słów i dużo tańsze od niej to prawie zawsze gry/akcesoria,
    których tytuł nie zdradza ("Returnal PS5", "F1 22 PlayStation 5") — odrzucamy je.
    """
    kotwice = [o.cena for o in oferty if o.pasuje and o.cena and filtr.pewny(o.tytul)]
    if len(kotwice) < min_kotwic:
        return
    odniesienie = statistics.median(kotwice)
    for o in oferty:
        if o.pasuje and o.cena and o.cena < odniesienie * prog and not filtr.pewny(o.tytul):
            o.pasuje = False
            o.powod = (f"cena dużo niższa niż typowa ({cena_str(odniesienie, o.waluta)}) "
                       "— raczej gra/akcesorium")


def ocen(oferty: list[Oferta], st: Statystyki | None, prog: float):
    for o in oferty:
        o.ocena, o.roznica = "", None
        if not o.pasuje or not o.cena or not st or o.waluta != st.waluta:
            continue
        o.roznica = o.cena / st.mediana - 1
        if o.cena < st.mediana * 0.5:
            o.ocena = "PODEJRZANA"
        elif o.roznica <= -prog:
            o.ocena = "OKAZJA"
        elif o.roznica <= -0.05:
            o.ocena = "DOBRA"
        elif o.roznica <= 0.10:
            o.ocena = "W NORMIE"
        else:
            o.ocena = "DROGO"


# ───────────────────────────── stan wyszukiwania ─────────────────────────────

class Wyszukiwanie:
    """Przechowuje wszystkie znalezione oferty między rundami odświeżania."""

    def __init__(self, args, filtr: Filtr, zrodla: list[str]):
        self.args = args
        self.filtr = filtr
        self.zrodla = zrodla
        self.baza: dict[str, Oferta] = {}
        self.runda = 0
        self.start = datetime.now(timezone.utc)
        self.ostatni_skan: datetime | None = None
        self.bledy: dict[str, str] = {}
        self.pobrane: dict[str, int] = {}
        self.st: Statystyki | None = None
        self.filtr_profil = ""
        # wycofanie po błędach: źródło -> (liczba błędów z rzędu, kiedy można ponowić)
        self.wstrzymane: dict[str, tuple[int, datetime]] = {}
        self.ostatnie_bledy: dict[str, str] = {}
        self.udane: set[str] = set()  # źródła, które choć raz odpowiedziały

    def skanuj(self) -> list[Oferta]:
        """Jedna runda: pobiera, scala z bazą, ocenia. Zwraca nowe okazje z tej rundy."""
        a = self.args
        self.runda += 1
        teraz = datetime.now(timezone.utc)
        zap = " ".join(a.zapytanie)
        funkcje = {
            "OLX": lambda: pobierz_olx(zap, a.limit, a.olx_kategoria, a.debug),
            "Vinted": lambda: pobierz_vinted(zap, a.limit, a.vinted_katalog, a.vinted_domena,
                                             a.debug),
        }
        self.bledy, self.pobrane = {}, {}
        pobrane: list[Oferta] = []
        zadania = {}
        with ThreadPoolExecutor(max_workers=2) as pula:
            for nazwa, fn in funkcje.items():
                if nazwa.lower() not in self.zrodla:
                    continue
                if nazwa in self.wstrzymane and teraz < self.wstrzymane[nazwa][1]:
                    kiedy = lokalny(self.wstrzymane[nazwa][1])
                    self.bledy[nazwa] = f"{self.ostatnie_bledy.get(nazwa, 'błąd')} " \
                                        f"— wstrzymane do {kiedy}"
                    continue
                zadania[nazwa] = pula.submit(fn)
            for nazwa, z in zadania.items():
                try:
                    wynik = z.result()
                    pobrane.extend(wynik)
                    self.pobrane[nazwa] = len(wynik)
                    self.wstrzymane.pop(nazwa, None)
                except BladZrodla as e:
                    self.bledy[nazwa] = str(e)
                    self.ostatnie_bledy[nazwa] = str(e)
                    # Kolejne błędy = coraz dłuższa przerwa dla tego źródła (maks. 30 min).
                    n = self.wstrzymane.get(nazwa, (0, teraz))[0] + 1
                    przerwa = min(1800.0, a.co * (2 ** (n - 1))) if n > 1 else 0
                    self.wstrzymane[nazwa] = (n, teraz + timedelta(seconds=przerwa))

        # Ogłoszenie uznajemy za nowe, jeśli wcześniej go nie było i nie jest stare
        # (OLX potrafi wrzucić do najnowszych odświeżone ogłoszenia sprzed tygodni).
        # Pierwszy udany odczyt danego źródła to stan początkowy, a nie nowości.
        granica = (self.ostatni_skan or teraz) - timedelta(hours=2)
        wczesniej_udane = set(self.udane)
        self.udane |= set(self.pobrane)
        for o in pobrane:
            stara = self.baza.get(o.klucz)
            if stara is None:
                o.widziano, o.runda = teraz, self.runda
                if o.zrodlo in wczesniej_udane:
                    if o.dodano is None:
                        o.dodano, o.data_przyblizona = teraz, True
                    o.nowa = o.dodano >= granica
                self.baza[o.klucz] = o
            else:
                if o.cena and stara.cena and o.cena < stara.cena - 0.5:
                    stara.poprzednia_cena = stara.cena
                    stara.obnizka_runda = self.runda
                stara.cena = o.cena or stara.cena
                stara.tytul = o.tytul or stara.tytul

        for o in self.baza.values():
            o.pasuje, o.powod = self.filtr.sprawdz(o.tytul)
            if o.pasuje and o.cena is not None:
                if a.min_cena is not None and o.cena < a.min_cena:
                    o.pasuje, o.powod = False, f"cena poniżej {cena_str(a.min_cena)}"
                elif a.max_cena is not None and o.cena > a.max_cena:
                    o.pasuje, o.powod = False, f"cena powyżej {cena_str(a.max_cena)}"

        oferty = list(self.baza.values())
        if not a.bez_filtra_cen:
            filtr_cenowy(oferty, self.filtr)
        self.st = policz_statystyki(oferty)
        ocen(oferty, self.st, a.prog / 100)
        self.ostatni_skan = teraz
        return [o for o in oferty if o.pasuje and o.ocena == "OKAZJA"
                and ((o.nowa and o.runda == self.runda) or o.obnizka_runda == self.runda)]

    def pasujace(self) -> list[Oferta]:
        wynik = [o for o in self.baza.values() if o.pasuje]
        tryb = self.args.sortuj
        if tryb == "data":
            # Oferty z datą od najnowszej; Vinted bez daty (pierwsza runda) według ID.
            wynik.sort(key=lambda o: (o.dodano is not None,
                                      o.dodano.timestamp() if o.dodano else o.id_num),
                       reverse=True)
        elif tryb == "cena":
            wynik.sort(key=lambda o: (o.cena is None, o.cena or 0))
        else:
            wynik.sort(key=lambda o: (o.roznica is None, o.roznica or 0))
        return wynik

    def odrzucone(self) -> list[Oferta]:
        return [o for o in self.baza.values() if not o.pasuje]

    def okazje(self) -> list[Oferta]:
        return sorted((o for o in self.baza.values() if o.pasuje and o.ocena == "OKAZJA"),
                      key=lambda o: o.cena)


# ───────────────────────────── wyświetlanie ─────────────────────────────

class K:
    """Kolory ANSI (wyłączane, gdy wyjście nie jest terminalem)."""
    wl = True

    @classmethod
    def _k(cls, kod, t):
        return f"\033[{kod}m{t}\033[0m" if cls.wl else t

    @classmethod
    def b(cls, t): return cls._k("1", t)
    @classmethod
    def szary(cls, t): return cls._k("2", t)
    @classmethod
    def ziel(cls, t): return cls._k("1;32", t)
    @classmethod
    def zolty(cls, t): return cls._k("33", t)
    @classmethod
    def czer(cls, t): return cls._k("31", t)
    @classmethod
    def fiolet(cls, t): return cls._k("35", t)
    @classmethod
    def niebieski(cls, t): return cls._k("36", t)
    @classmethod
    def nowe(cls, t): return cls._k("1;30;42", t)


KOLOR_OCENY = {"OKAZJA": K.ziel, "DOBRA": K.zolty, "W NORMIE": lambda t: t,
               "DROGO": K.czer, "PODEJRZANA": K.fiolet}


def cena_str(cena: float | None, waluta: str = "PLN") -> str:
    if cena is None:
        return "brak ceny"
    sym = {"PLN": "zł", "EUR": "€"}.get(waluta, waluta)
    if abs(cena - round(cena)) < 0.005:
        s = f"{cena:,.0f}".replace(",", " ")
    else:
        s = f"{cena:,.2f}".replace(",", " ").replace(".", ",")
    return f"{s} {sym}"


def wiek_str(o: Oferta) -> str:
    if not o.dodano:
        return "—"
    s = (datetime.now(timezone.utc) - o.dodano).total_seconds()
    if s < 60:
        t = "teraz"
    elif s < 3600:
        t = f"{int(s // 60)} min"
    elif s < 86400:
        t = f"{int(s // 3600)} godz"
    else:
        t = f"{int(s // 86400)} dni"
    return ("~" + t) if o.data_przyblizona else t


def czas_str(sekundy: float) -> str:
    sekundy = int(max(0, sekundy))
    g, r = divmod(sekundy, 3600)
    m, s = divmod(r, 60)
    return f"{g}:{m:02d}:{s:02d}" if g else f"{m}:{s:02d}"


def lokalny(d: datetime) -> str:
    return d.astimezone().strftime("%H:%M")


def odmien(n: int, jeden: str, kilka: str, wiele: str) -> str:
    if n == 1:
        return jeden
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return kilka
    return wiele


def ogloszen(n: int) -> str:
    return odmien(n, "ogłoszenie", "ogłoszenia", "ogłoszeń")


def _utnij(t: str, n: int) -> str:
    return t if len(t) <= n else t[: max(1, n - 1)] + "…"


def linie_tabeli(oferty: list[Oferta], tytul: str, biezaca_runda: int) -> list[str]:
    if not oferty:
        return []
    szer = shutil.get_terminal_size((120, 30)).columns
    szer_tytulu = max(20, szer - 58)
    out = ["", K.b(tytul),
           K.szary(f"{'#':>3}  {'':4}  {'ŹRÓDŁO':<6}  {'CENA':>12}  {'OCENA':<17}  "
                   f"{'DODANO':>8}  TYTUŁ")]
    for n, o in enumerate(oferty, 1):
        if o.ocena:
            ocena = KOLOR_OCENY[o.ocena](f"{o.roznica * 100:+5.0f}% {o.ocena:<11}")
        else:
            ocena = f"{'':>5}  {'—':<11}"
        cena = cena_str(o.cena, o.waluta) + ("*" if o.do_negocjacji else " ")
        if o.obnizka_runda and o.obnizka_runda >= biezaca_runda - 1:
            znak = K.ziel("↓   ")
        elif o.nowa and o.runda == biezaca_runda:
            znak = K.nowe("NOWE")
        elif o.nowa:
            znak = K.ziel("nowe")
        else:
            znak = "    "
        out.append(f"{n:>3}  {znak}  {K.niebieski(f'{o.zrodlo:<6}')}  {cena:>12}  {ocena}  "
                   f"{wiek_str(o):>8}  {_utnij(o.tytul, szer_tytulu)}")
        dop = f"            {o.url}"
        if o.lokalizacja:
            dop += f"  ({o.lokalizacja})"
        if o.poprzednia_cena:
            dop += f"  [obniżka z {cena_str(o.poprzednia_cena, o.waluta)}]"
        out.append(K.szary(dop))
    return out


def linie_podsumowania(w: Wyszukiwanie) -> list[str]:
    st, prog = w.st, w.args.prog / 100
    pas = sum(1 for o in w.baza.values() if o.pasuje)
    out = ["", K.b("Podsumowanie cen"),
           f"  Ogłoszeń w bazie: {len(w.baza)}, pasujące: {pas}, odrzucone: {len(w.baza) - pas}"]
    if not st:
        out.append("  Brak ofert z ceną — nie da się ocenić cen.")
        return out
    wa = st.waluta
    out.append(f"  Mediana: {K.b(cena_str(st.mediana, wa))}   Średnia: {cena_str(st.srednia, wa)}   "
               f"Zakres: {cena_str(st.minimum, wa)} – {cena_str(st.maksimum, wa)}   "
               f"(z {st.liczba} ofert, bez wartości odstających)")
    out.append(f"  Okazja = co najmniej {prog * 100:.0f}% poniżej mediany, "
               f"czyli ≤ {K.ziel(cena_str(st.mediana * (1 - prog), wa))}.   "
               + K.szary("PODEJRZANA = poniżej połowy mediany — sprawdź dokładnie."))
    if st.liczba < 5:
        out.append(K.zolty("  Uwaga: mało ofert — ocena cen jest orientacyjna."))
    return out


def linie_okazji(okazje: list[Oferta], top: int) -> list[str]:
    if not okazje:
        return [K.szary("\nBrak okazji poniżej progu w tej chwili.")]
    out = ["", K.ziel(f"Najlepsze okazje (top {min(top, len(okazje))} z {len(okazje)}):")]
    for o in okazje[:top]:
        out.append(f"  {K.ziel(cena_str(o.cena, o.waluta)):>12}  ({o.roznica * 100:+.0f}%)  "
                   f"{K.niebieski(o.zrodlo)}  {_utnij(o.tytul, 55)}")
        out.append(K.szary(f"      {o.url}"))
    return out


def linie_naglowka(w: Wyszukiwanie, koniec: datetime | None, monitor: bool) -> list[str]:
    a = w.args
    zap = " ".join(a.zapytanie)
    out = [K.b(f"Perełka v{WERSJA}") + f" — „{zap}” (profil: {w.filtr_profil}, "
           f"źródła: {', '.join(w.zrodla)}, po {a.limit} najnowszych)"]
    if monitor:
        teraz = datetime.now(timezone.utc)
        nowych = sum(1 for o in w.baza.values() if o.nowa and o.pasuje)
        info = (f"Runda {w.runda} · start {lokalny(w.start)} · ostatnie odświeżenie "
                f"{lokalny(w.ostatni_skan or teraz)} · co {czas_str(a.co)}")
        if koniec:
            info += f" · koniec {lokalny(koniec)} (za {czas_str((koniec - teraz).total_seconds())})"
        info += f" · nowych pasujących od startu: {nowych}"
        out.append(K.szary(info))
    stan = []
    for nazwa in ("OLX", "Vinted"):
        if nazwa in w.pobrane:
            stan.append(f"{nazwa}: {w.pobrane[nazwa]} {ogloszen(w.pobrane[nazwa])}")
        elif nazwa in w.bledy:
            stan.append(K.czer(f"{nazwa}: błąd — {w.bledy[nazwa]}"))
    out.append("  " + "   ".join(stan))
    return out


def rysuj(w: Wyszukiwanie, koniec: datetime | None, monitor: bool):
    a = w.args
    linie = linie_naglowka(w, koniec, monitor)
    pas = w.pasujace()
    okazje = w.okazje()
    if a.okazje:
        lista, tytul = okazje, f"Okazje ({len(okazje)})"
    else:
        lista, tytul = pas, f"Pasujące ogłoszenia ({len(pas)}) — sortowanie: {a.sortuj}"
    limit = a.pokaz
    if limit is None and monitor:
        wys = shutil.get_terminal_size((120, 40)).lines
        limit = max(5, (wys - 22) // 2)
    if limit and len(lista) > limit:
        tytul += f", pokazuję {limit}"
        lista = lista[:limit]
    linie += linie_tabeli(lista, tytul, w.runda)
    if a.odrzucone:
        odr = w.odrzucone()
        if odr:
            linie += ["", K.b(f"Odrzucone ({len(odr)})")]
            for o in odr:
                linie.append(K.szary(f"  {'[' + o.zrodlo + ']':<8} {cena_str(o.cena, o.waluta):>12}"
                                     f"  {_utnij(o.tytul, 60):<60}  ← {o.powod}"))
    linie += linie_podsumowania(w)
    if not a.okazje:
        linie += linie_okazji(okazje, a.top)
    legenda = "* = cena do negocjacji (OLX)"
    if "vinted" in w.zrodla:
        legenda += " · Vinted nie podaje daty dodania: „~” = czas od pierwszego wykrycia"
    if monitor:
        legenda += " · NOWE = nowe w tej rundzie · ↓ = obniżka ceny"
    linie += ["", K.szary(legenda)]
    if monitor and sys.stdout.isatty():
        sys.stdout.write("\033[H\033[2J\033[3J")  # wyczyść ekran
    print("\n".join(linie), flush=True)


def powiadom(okazje: list[Oferta], args):
    if not okazje:
        return
    if not args.cicho and sys.stdout.isatty():
        sys.stdout.write("\a")
        sys.stdout.flush()
    if args.powiadomienia and shutil.which("notify-send"):
        for o in okazje[:3]:
            try:
                subprocess.run(["notify-send", "-a", "Perełka", "-u", "critical",
                                f"Okazja: {cena_str(o.cena, o.waluta)} ({o.roznica * 100:+.0f}%)",
                                f"{o.tytul}\n{o.url}"], timeout=5, check=False)
            except (OSError, subprocess.SubprocessError):
                pass


def czekaj(sekundy: float, koniec: datetime | None):
    """Odlicza czas do następnego odświeżenia w ostatniej linii terminala."""
    cel = time.monotonic() + sekundy
    tty = sys.stdout.isatty()
    while True:
        zostalo = cel - time.monotonic()
        if zostalo <= 0:
            break
        if tty:
            txt = f"  Następne odświeżenie za {czas_str(zostalo)}"
            if koniec:
                txt += f" · koniec o {lokalny(koniec)}"
            sys.stdout.write("\r\033[K" + K.szary(txt + " · Ctrl+C kończy"))
            sys.stdout.flush()
        time.sleep(min(1.0, zostalo))
    if tty:
        sys.stdout.write("\r\033[K" + K.szary("  Odświeżam…"))
        sys.stdout.flush()


# ───────────────────────────── CLI ─────────────────────────────

def czas_arg(tekst: str) -> float:
    """'6h', '30m', '1h30m', '90s', '2d', '45' (minuty) -> sekundy; '0' = bez limitu."""
    t = tekst.strip().lower().replace(" ", "")
    if t in ("0", "inf", "bez-konca"):
        return 0.0
    if re.fullmatch(r"\d+(\.\d+)?", t):
        return float(t) * 60
    m = re.fullmatch(r"(?:(\d+(?:\.\d+)?)d)?(?:(\d+(?:\.\d+)?)(?:h|g|godz))?"
                     r"(?:(\d+(?:\.\d+)?)(?:m|min))?(?:(\d+(?:\.\d+)?)s)?", t)
    if not m or not any(m.groups()):
        raise argparse.ArgumentTypeError(f"nieprawidłowy czas „{tekst}” (np. 6h, 30m, 1h30m)")
    d, h, mi, s = (float(x) if x else 0 for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


def _lista(wartosci: list[str] | None) -> list[str]:
    wynik = []
    for w in wartosci or []:
        wynik.extend(x.strip() for x in w.split(",") if x.strip())
    return wynik


def zbuduj_parser() -> argparse.ArgumentParser:
    profile = ", ".join(f"{k} ({v['opis']})" for k, v in PROFILE.items())
    p = argparse.ArgumentParser(
        prog="perelka",
        description="Perełka v" + WERSJA + " — najnowsze ogłoszenia z OLX i Vinted z oceną cen.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Przykłady:\n"
            "  perelka.py ps5                              jednorazowe wyszukiwanie\n"
            "  perelka.py ps5 --czas 6h                    monitoruj 6 godzin, odświeżaj co 5 min\n"
            "  perelka.py ps5 --czas 6h --co 3m --powiadomienia\n"
            "  perelka.py \"iphone 13\" --min-cena 500 --wyklucz pro,mini --co 10m\n"
            "  perelka.py ps5 --zrodla olx --limit 100 --okazje\n\n"
            "Czas: 6h, 30m, 1h30m, 90s, 2d albo sama liczba = minuty; --czas 0 = bez limitu.\n"
            "Profile (--typ): " + profile + "\n"
            "Słowa w --wyklucz/--wymagane mogą kończyć się gwiazdką: uszkodz* = uszkodzony, "
            "uszkodzona..."
        ),
    )
    p.add_argument("zapytanie", nargs="+", help="czego szukać, np. ps5 albo \"iphone 13\"")
    g = p.add_argument_group("tryb ciągły")
    g.add_argument("-c", "--czas", type=czas_arg, metavar="CZAS",
                   help="jak długo monitorować, np. 6h, 90m (0 = do Ctrl+C)")
    g.add_argument("-i", "--co", type=czas_arg, metavar="CZAS",
                   help="co ile odświeżać (domyślnie 5m, minimum 1m); samo --co włącza tryb ciągły")
    g.add_argument("--powiadomienia", action="store_true",
                   help="powiadomienia na pulpicie o nowych okazjach (notify-send)")
    g.add_argument("--cicho", action="store_true", help="bez sygnału dźwiękowego przy okazjach")

    f = p.add_argument_group("wyszukiwanie i filtry")
    f.add_argument("-z", "--zrodla", default="olx,vinted",
                   help="źródła oddzielone przecinkiem: olx, vinted (domyślnie oba)")
    f.add_argument("-n", "--limit", type=int, default=40,
                   help="ile najnowszych ogłoszeń pobrać z każdego źródła (domyślnie 40)")
    f.add_argument("-t", "--typ", choices=["auto", *PROFILE], default="auto",
                   help="kategoria przedmiotu do filtrowania akcesoriów (domyślnie auto)")
    f.add_argument("-w", "--wyklucz", action="append", metavar="SŁOWA",
                   help="odrzuć ogłoszenia z tymi słowami (po przecinku, można powtarzać)")
    f.add_argument("-m", "--wymagane", action="append", metavar="SŁOWA",
                   help="dodatkowe słowa, które muszą być w tytule (po przecinku)")
    f.add_argument("--min-cena", type=float, help="minimalna cena")
    f.add_argument("--max-cena", type=float, help="maksymalna cena")
    f.add_argument("--bez-filtra", action="store_true",
                   help="wyłącz filtr akcesoriów (zostaje tylko dopasowanie słów)")
    f.add_argument("--bez-filtra-cen", action="store_true",
                   help="nie odrzucaj ofert dużo tańszych od typowej ceny przedmiotu")
    f.add_argument("--olx-kategoria", metavar="ID", help="ID kategorii OLX (opcjonalnie)")
    f.add_argument("--vinted-katalog", metavar="ID",
                   help="ID katalogu Vinted, np. 3025 (można kilka po przecinku)")
    f.add_argument("--vinted-domena", default="pl", help="domena Vinted (domyślnie pl)")

    w = p.add_argument_group("wyświetlanie")
    w.add_argument("-p", "--prog", type=float, default=15,
                   help="o ile %% poniżej mediany oferta to okazja (domyślnie 15)")
    w.add_argument("-s", "--sortuj", choices=["data", "cena", "ocena"], default="data",
                   help="kolejność listy (domyślnie data — najnowsze na górze)")
    w.add_argument("-o", "--okazje", action="store_true", help="pokaż tylko okazje")
    w.add_argument("--pokaz", type=int, metavar="N",
                   help="ile ogłoszeń pokazać (domyślnie: wszystkie; w trybie ciągłym tyle, "
                        "ile zmieści się na ekranie)")
    w.add_argument("--top", type=int, default=5, help="ile najlepszych okazji wyróżnić (domyślnie 5)")
    w.add_argument("--odrzucone", action="store_true",
                   help="pokaż też odrzucone ogłoszenia z powodem (pomaga dobrać filtry)")
    w.add_argument("--bez-kolorow", action="store_true", help="wyłącz kolory")
    w.add_argument("--debug", action="store_true", help="pokaż zapytania HTTP")
    p.add_argument("-V", "--version", action="version", version=f"perelka {WERSJA}")
    return p


def main(argv: list[str] | None = None) -> int:
    args = zbuduj_parser().parse_args(argv)
    K.wl = sys.stdout.isatty() and not args.bez_kolorow and "NO_COLOR" not in os.environ

    zapytanie = " ".join(args.zapytanie).strip()
    zrodla = [z.strip().lower() for z in args.zrodla.split(",") if z.strip()]
    nieznane = set(zrodla) - {"olx", "vinted"}
    if nieznane:
        print(f"Nieznane źródło: {', '.join(nieznane)} (dostępne: olx, vinted)", file=sys.stderr)
        return 2
    if args.limit < 1:
        print("--limit musi być większy od 0", file=sys.stderr)
        return 2

    monitor = args.czas is not None or args.co is not None
    if args.co is None:
        args.co = 300.0
    if monitor and args.co < MIN_ODSTEP:
        print(f"--co ustawione na minimum {MIN_ODSTEP} s (żeby nie zostać zablokowanym).",
              file=sys.stderr)
        args.co = float(MIN_ODSTEP)

    profil = wybierz_profil(zapytanie) if args.typ == "auto" else args.typ
    filtr = Filtr(zapytanie, profil, _lista(args.wyklucz), _lista(args.wymagane), args.bez_filtra)
    w = Wyszukiwanie(args, filtr, zrodla)
    w.filtr_profil = profil

    if not monitor:
        print(K.szary(f"Szukam „{zapytanie}”…"), flush=True)
        w.skanuj()
        if not w.baza:
            linie = linie_naglowka(w, None, False)
            print("\n".join(linie))
            print("Nie znaleziono żadnych ogłoszeń.")
            return 1 if len(w.bledy) == len(zrodla) else 0
        rysuj(w, None, False)
        return 0

    koniec = (w.start + timedelta(seconds=args.czas)) if args.czas else None
    print(K.szary(f"Start monitorowania „{zapytanie}”…"), flush=True)
    try:
        while True:
            nowe_okazje = w.skanuj()
            rysuj(w, koniec, True)
            powiadom(nowe_okazje, args)
            odstep = args.co * random.uniform(0.9, 1.1)  # lekki rozrzut odstępów
            if koniec:
                zostalo = (koniec - datetime.now(timezone.utc)).total_seconds()
                if zostalo <= 1:
                    break
                odstep = min(odstep, zostalo)
            czekaj(odstep, koniec)
    except KeyboardInterrupt:
        pass
    if sys.stdout.isatty():
        sys.stdout.write("\r\033[K")
    trwalo = (datetime.now(timezone.utc) - w.start).total_seconds()
    nowych = sum(1 for o in w.baza.values() if o.nowa and o.pasuje)
    print(K.b(f"\nKoniec monitorowania") + f" — {w.runda} {odmien(w.runda, 'runda', 'rundy', 'rund')} w {czas_str(trwalo)}, "
          f"nowych pasujących ogłoszeń: {nowych}, okazji w bazie: {len(w.okazje())}.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nPrzerwano.", file=sys.stderr)
        sys.exit(130)
