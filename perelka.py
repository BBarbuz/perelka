#!/usr/bin/env python3
"""
Perełka v0.1 — wyszukiwarka najnowszych ogłoszeń z OLX i Vinted w terminalu.

Pobiera najnowsze ogłoszenia, odrzuca te, które nie pasują do szukanej rzeczy
(np. pad albo etui zamiast konsoli), liczy cenę referencyjną (medianę)
i wskazuje oferty wyraźnie poniżej niej.

Działa na samej bibliotece standardowej Pythona 3 (Fedora ma go od razu).

Przykłady:
    ./perelka.py ps5
    ./perelka.py "iphone 13" --typ telefon --min-cena 500
    ./perelka.py ps5 --zrodla olx --limit 100 --okazje
    ./perelka.py "xbox series x" --wyklucz "series s" --odrzucone
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import re
import shutil
import statistics
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone

WERSJA = "0.1"
UA = "Mozilla/5.0 (X11; Linux x86_64; rv:132.0) Gecko/20100101 Firefox/132.0"
TIMEOUT = 20


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
    # wypełniane podczas analizy
    pasuje: bool = True
    powod: str = ""
    ocena: str = ""
    roznica: float | None = None  # względem mediany, np. -0.18 = 18% taniej


# ───────────────────────────── normalizacja tekstu ─────────────────────────────

_PL = str.maketrans("ąćęłńóśźż", "acelnoszz")

# Różne zapisy tego samego modelu sprowadzane do jednej postaci.
ALIASY = [
    (r"\bplay\s*station\s*(\d)\b", r"ps\1"),
    (r"\bx\s*box\b", "xbox"),
    (r"\bi\s+phone\b", "iphone"),
    (r"\bmac\s+book\b", "macbook"),
    (r"\b(\d+)\s*(gb|tb)\b", r"\1\2"),
]


def _podstawowa(tekst: str, wzorzec: bool = False) -> str:
    t = tekst.lower().translate(_PL)
    if not wzorzec:
        t = t.replace("*", " ")
    t = unicodedata.normalize("NFKD", t)
    t = "".join(z for z in t if not unicodedata.combining(z))
    t = t.replace("+", " + ").replace("&", " + ")
    t = re.sub(r"[^a-z0-9+*]+", " ", t)
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


def znajdz(toks: list[str], fraza: list[str], start: int = 0, negacja: bool = False) -> int:
    """Indeks pierwszego wystąpienia frazy w tokenach albo -1.
    Przy negacja=True pomija wystąpienia poprzedzone słowem "nie"/"bez"."""
    n = len(fraza)
    for i in range(start, len(toks) - n + 1):
        if all(_slowo_pasuje(toks[i + j], fraza[j]) for j in range(n)):
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
    "uszkodzon*", "na czesci", "nie dziala*", "niedziala*", "nie wlacza*",
    "zepsut*", "zbit*", "pekniet*", "zablokowan*", "blokada", "icloud",
    "atrapa", "makieta", "replika",
    "wynajem", "wynajm*", "wypozycz*", "naprawa", "naprawy", "serwis*",
    "samo pudelko", "tylko pudelko", "puste pudelko", "pudelko po", "pudelko od",
    "karton po", "karton od", "pusty karton", "opakowanie po",
]

# Przyimki, które przed szukaną nazwą oznaczają akcesorium: "pad DO ps5", "etui NA iphone".
PRZYIMKI = ["do", "na", "dla", "pod", "for", "kompatybiln*"]

# Słowa, które w ogłoszeniu sygnalizują zestaw ("ps5 + pad", "ps5 z grą").
ZESTAW = ["+", "z", "i", "oraz", "plus", "w", "with", "and", "zestaw*", "komplet*",
          "gratis", "dodatk*", "bundle"]

PROFILE = {
    "ogolny": {
        "opis": "dowolny przedmiot (podstawowy filtr akcesoriów)",
        "akcesoria": [
            "etui", "case", "pokrowiec*", "plecki", "folia", "folie", "szklo", "szkielko",
            "hartowan*", "ladowar*", "kabel", "kabl*", "zasilacz*", "uchwyt*", "podstawk*",
            "stojak*", "naklejk*", "skin*", "obudow*", "adapter*", "pasek", "torba*", "torb*",
        ],
        "pozytywne": [],
    },
    "konsola": {
        "opis": "konsole do gier (PS, Xbox, Nintendo, Steam Deck...)",
        "akcesoria": [
            "pad", "pady", "padow", "kontroler*", "dualsense", "dualshock", "joy*", "gamepad*",
            "gra", "gry", "gier", "gre", "game", "games", "plyt*", "steelbook*",
            "etui", "case", "pokrowiec*", "torb*", "obudow*", "panel*", "skin*", "naklejk*",
            "folia", "kabel", "kabl*", "hdmi", "ladowar*", "stacj*", "podstawk*", "stojak*",
            "chlodz*", "wentylator*", "sluchawk*", "headset*", "pulse", "kamer*",
            "kierownic*", "naped*", "dysk*", "ssd", "pilot*", "portal", "vr", "gogle",
            "karta", "karty", "psn", "doladowan*", "kod", "kody", "voucher*", "subskrypc*",
            "abonament*", "konto", "uchwyt*", "zasilacz*", "adapter*", "amiibo",
        ],
        "pozytywne": [
            "konsol*", "console*", "cfi*", "digital", "disc", "slim", "standard", "edition",
            "zestaw*", "komplet*", "bundle", "oled",
        ],
    },
    "telefon": {
        "opis": "telefony / smartfony",
        "akcesoria": [
            "etui", "case", "pokrowiec*", "plecki", "folia", "folie", "szklo", "szkielko",
            "hartowan*", "ladowar*", "kabel", "kabl*", "zasilacz*", "uchwyt*", "obudow*",
            "wyswietlacz*", "lcd", "matryc*", "tasm*", "klapk*", "rysik*", "pasek",
            "sluchawk*", "airpods", "glosnik*", "powerbank*", "adapter*", "podstawk*",
            "stojak*", "naklejk*", "skin*", "karta",
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
        akcesoria = [w for w in wzorce(p["akcesoria"])
                     if not (len(w) == 1 and any(_slowo_pasuje(s, w[0]) for s in slowa_zapytania))]
        # Jeśli ktoś szuka akcesorium (np. "pad ps5"), filtr akcesoriów nie ma sensu.
        szuka_akcesorium = len(akcesoria) < len(wzorce(p["akcesoria"]))
        self.akcesoria = [] if (bez_akcesoriow or szuka_akcesorium) else akcesoria
        self.pozytywne = wzorce(p["pozytywne"])

    def _pozytywne_w(self, toks: list[str]) -> bool:
        if znajdz_dowolna(toks, self.pozytywne)[0] >= 0:
            return True
        return any(re.fullmatch(r"\d+(gb|tb)", t) for t in toks)

    def sprawdz(self, tytul: str) -> tuple[bool, str]:
        toks = tokeny(tytul)

        # 1. Wszystkie frazy z zapytania muszą być w tytule.
        poczatek, koniec = None, None
        for f in self.frazy:
            i = znajdz(toks, f)
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
            if any(znajdz(reszta, w) == 0 for w in self.pozytywne):
                break
            if any(znajdz(reszta, w) == 0 for w in self.akcesoria):
                return False, f"akcesorium: „{przed[j]}” przed nazwą"
            if any(znajdz(reszta, w) == 0 for w in self.przyimki):
                return False, f"akcesorium: „{' '.join(przed[j:])} {' '.join(toks[poczatek:koniec])}”"

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


def _pobierz_json(opener, url: str, naglowki: dict, debug: bool):
    if debug:
        print(f"[debug] GET {url}", file=sys.stderr)
    req = urllib.request.Request(url, headers={"User-Agent": UA, **naglowki})
    try:
        with opener.open(req, timeout=TIMEOUT) as odp:
            return json.loads(odp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise BladZrodla(f"HTTP {e.code} ({e.reason})") from e
    except urllib.error.URLError as e:
        raise BladZrodla(f"brak połączenia: {e.reason}") from e
    except json.JSONDecodeError as e:
        raise BladZrodla("odpowiedź nie jest JSON-em (możliwa blokada anty-bot)") from e
    except TimeoutError as e:
        raise BladZrodla("przekroczono czas oczekiwania") from e


def _data_iso(tekst):
    if not tekst:
        return None
    try:
        return datetime.fromisoformat(tekst.replace("Z", "+00:00"))
    except ValueError:
        return None


def parsuj_olx(it: dict) -> Oferta:
    cena, waluta, neg = None, "PLN", False
    for p in it.get("params") or []:
        if p.get("key") == "price":
            v = p.get("value") or {}
            if v.get("value") is not None and v.get("type", "price") == "price":
                cena = float(v["value"])
            waluta = v.get("currency") or "PLN"
            neg = bool(v.get("negotiable"))
    lok = ((it.get("location") or {}).get("city") or {}).get("name", "")
    return Oferta(
        zrodlo="OLX", tytul=(it.get("title") or "").strip(), cena=cena, waluta=waluta,
        url=it.get("url", ""), dodano=_data_iso(it.get("created_time")),
        lokalizacja=lok, do_negocjacji=neg,
    )


def pobierz_olx(zapytanie: str, limit: int, kategoria: str | None, debug: bool) -> list[Oferta]:
    opener = urllib.request.build_opener()
    oferty: list[Oferta] = []
    offset = 0
    while len(oferty) < limit and offset < 1000:
        ile = min(40, limit - len(oferty))
        params = {"offset": offset, "limit": ile, "query": zapytanie, "sort_by": "created_at:desc"}
        if kategoria:
            params["category_id"] = kategoria
        url = "https://www.olx.pl/api/v1/offers/?" + urllib.parse.urlencode(params)
        dane = _pobierz_json(opener, url, {"Accept": "application/json",
                                          "Accept-Language": "pl-PL,pl;q=0.9"}, debug)
        porcja = dane.get("data") or []
        oferty.extend(parsuj_olx(it) for it in porcja)
        if len(porcja) < ile:
            break
        offset += ile
    return oferty[:limit]


def _kwota(v):
    if v is None:
        return None, None
    if isinstance(v, dict):
        a = v.get("amount")
        return (float(a) if a is not None else None), v.get("currency_code")
    try:
        return float(str(v).replace(",", ".")), None
    except ValueError:
        return None, None


def parsuj_vinted(it: dict, baza: str) -> Oferta:
    cena, waluta = _kwota(it.get("price"))
    waluta = waluta or it.get("currency") or "PLN"
    ts = ((it.get("photo") or {}).get("high_resolution") or {}).get("timestamp")
    dodano = datetime.fromtimestamp(ts, tz=timezone.utc) if ts else None
    url = it.get("url") or f"{baza}/items/{it.get('id')}"
    if url.startswith("/"):
        url = baza + url
    return Oferta(zrodlo="Vinted", tytul=(it.get("title") or "").strip(), cena=cena,
                  waluta=waluta, url=url, dodano=dodano)


def pobierz_vinted(zapytanie: str, limit: int, katalog: str | None, domena: str,
                   debug: bool) -> list[Oferta]:
    baza = f"https://www.vinted.{domena}"
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    def sesja():
        # Vinted wymaga ciasteczka sesji, które dostaje się wchodząc na stronę główną.
        req = urllib.request.Request(baza + "/", headers={
            "User-Agent": UA, "Accept": "text/html", "Accept-Language": "pl-PL,pl;q=0.9"})
        try:
            with opener.open(req, timeout=TIMEOUT) as odp:
                odp.read()
        except urllib.error.HTTPError as e:
            raise BladZrodla(f"nie udało się otworzyć sesji: HTTP {e.code}") from e
        except urllib.error.URLError as e:
            raise BladZrodla(f"brak połączenia: {e.reason}") from e

    sesja()
    oferty: list[Oferta] = []
    strona = 1
    ponowiono = False
    while len(oferty) < limit:
        ile = min(96, limit - len(oferty))
        params = {"page": strona, "per_page": ile, "search_text": zapytanie,
                  "order": "newest_first"}
        if katalog:
            params["catalog_ids"] = katalog
        url = f"{baza}/api/v2/catalog/items?" + urllib.parse.urlencode(params)
        try:
            dane = _pobierz_json(opener, url, {"Accept": "application/json",
                                              "Accept-Language": "pl-PL,pl;q=0.9"}, debug)
        except BladZrodla as e:
            if "HTTP 401" in str(e) and not ponowiono:
                ponowiono = True
                sesja()
                continue
            raise
        porcja = dane.get("items") or []
        oferty.extend(parsuj_vinted(it, baza) for it in porcja)
        if len(porcja) < ile:
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


def ocen(oferty: list[Oferta], st: Statystyki | None, prog: float):
    for o in oferty:
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


def wiek_str(d: datetime | None) -> str:
    if not d:
        return "?"
    s = (datetime.now(timezone.utc) - d).total_seconds()
    if s < 60:
        return "teraz"
    if s < 3600:
        return f"{int(s // 60)} min"
    if s < 86400:
        return f"{int(s // 3600)} godz"
    return f"{int(s // 86400)} dni"


def ogloszen(n: int) -> str:
    if n == 1:
        return "ogłoszenie"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return "ogłoszenia"
    return "ogłoszeń"


def _utnij(t: str, n: int) -> str:
    return t if len(t) <= n else t[: max(1, n - 1)] + "…"


def drukuj_tabele(oferty: list[Oferta], tytul: str):
    if not oferty:
        return
    szer = shutil.get_terminal_size((120, 30)).columns
    szer_tytulu = max(20, szer - 52)
    print()
    print(K.b(tytul))
    print(K.szary(f"{'#':>3}  {'ŹRÓDŁO':<6}  {'CENA':>12}  {'OCENA':<17}  {'DODANO':>7}  TYTUŁ"))
    for n, o in enumerate(oferty, 1):
        if o.ocena:
            proc = f"{o.roznica * 100:+.0f}%"
            ocena = f"{proc:>5} {o.ocena:<11}"
            ocena = KOLOR_OCENY[o.ocena](ocena)
        else:
            ocena = f"{'':>5} {'—':<11}"
        cena = cena_str(o.cena, o.waluta) + ("*" if o.do_negocjacji else " ")
        zr = K.niebieski(f"{o.zrodlo:<6}")
        print(f"{n:>3}  {zr}  {cena:>12}  {ocena}  {wiek_str(o.dodano):>7}  "
              f"{_utnij(o.tytul, szer_tytulu)}")
        print(K.szary(f"      {o.url}" + (f"  ({o.lokalizacja})" if o.lokalizacja else "")))


def drukuj_podsumowanie(st: Statystyki | None, prog: float, wszystkich: int, pasujacych: int):
    print()
    print(K.b("Podsumowanie cen"))
    print(f"  Pobrane ogłoszenia: {wszystkich}, pasujące: {pasujacych}, "
          f"odrzucone: {wszystkich - pasujacych}")
    if not st:
        print("  Brak ofert z ceną — nie da się ocenić cen.")
        return
    w = st.waluta
    print(f"  Mediana: {K.b(cena_str(st.mediana, w))}   Średnia: {cena_str(st.srednia, w)}   "
          f"Zakres: {cena_str(st.minimum, w)} – {cena_str(st.maksimum, w)}   "
          f"(z {st.liczba} ofert, bez wartości odstających)")
    print(f"  Okazja = cena co najmniej {prog * 100:.0f}% poniżej mediany, "
          f"czyli ≤ {K.ziel(cena_str(st.mediana * (1 - prog), w))}")
    print(K.szary("  PODEJRZANA = mniej niż połowa mediany (możliwe oszustwo, uszkodzenie "
                  "albo inny przedmiot) — sprawdź dokładnie."))
    if st.liczba < 5:
        print(K.zolty("  Uwaga: mało ofert — ocena cen jest orientacyjna."))


# ───────────────────────────── CLI ─────────────────────────────

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
            "  perelka.py ps5\n"
            "  perelka.py \"iphone 13\" --min-cena 500 --wyklucz pro,mini\n"
            "  perelka.py ps5 --zrodla olx --limit 100 --okazje\n"
            "  perelka.py \"nintendo switch\" --wyklucz \"switch 2\" --odrzucone\n\n"
            "Profile (--typ): " + profile + "\n"
            "Słowa w --wyklucz/--wymagane mogą kończyć się gwiazdką: uszkodz* = uszkodzony, "
            "uszkodzona..."
        ),
    )
    p.add_argument("zapytanie", nargs="+", help="czego szukać, np. ps5 albo \"iphone 13\"")
    p.add_argument("-z", "--zrodla", default="olx,vinted",
                   help="źródła oddzielone przecinkiem: olx, vinted (domyślnie oba)")
    p.add_argument("-n", "--limit", type=int, default=40,
                   help="ile najnowszych ogłoszeń pobrać z każdego źródła (domyślnie 40)")
    p.add_argument("-t", "--typ", choices=["auto", *PROFILE], default="auto",
                   help="kategoria przedmiotu do filtrowania akcesoriów (domyślnie auto)")
    p.add_argument("-w", "--wyklucz", action="append", metavar="SŁOWA",
                   help="odrzuć ogłoszenia z tymi słowami (po przecinku, można powtarzać)")
    p.add_argument("-m", "--wymagane", action="append", metavar="SŁOWA",
                   help="dodatkowe słowa, które muszą być w tytule (po przecinku)")
    p.add_argument("--min-cena", type=float, help="minimalna cena")
    p.add_argument("--max-cena", type=float, help="maksymalna cena")
    p.add_argument("-p", "--prog", type=float, default=15,
                   help="o ile %% poniżej mediany oferta to okazja (domyślnie 15)")
    p.add_argument("-s", "--sortuj", choices=["data", "cena", "ocena"], default="data",
                   help="kolejność listy (domyślnie data — najnowsze na górze)")
    p.add_argument("-o", "--okazje", action="store_true", help="pokaż tylko okazje")
    p.add_argument("--top", type=int, default=5, help="ile najlepszych okazji wyróżnić (domyślnie 5)")
    p.add_argument("--odrzucone", action="store_true",
                   help="pokaż też odrzucone ogłoszenia z powodem (pomaga dobrać filtry)")
    p.add_argument("--bez-filtra", action="store_true",
                   help="wyłącz filtr akcesoriów (zostaje tylko dopasowanie słów)")
    p.add_argument("--olx-kategoria", metavar="ID", help="ID kategorii OLX (opcjonalnie)")
    p.add_argument("--vinted-katalog", metavar="ID", help="ID katalogu Vinted (opcjonalnie)")
    p.add_argument("--vinted-domena", default="pl", help="domena Vinted (domyślnie pl)")
    p.add_argument("--bez-kolorow", action="store_true", help="wyłącz kolory")
    p.add_argument("--debug", action="store_true", help="pokaż zapytania HTTP")
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
    prog = args.prog / 100

    profil = wybierz_profil(zapytanie) if args.typ == "auto" else args.typ
    filtr = Filtr(zapytanie, profil, _lista(args.wyklucz), _lista(args.wymagane), args.bez_filtra)

    print(K.b(f"Perełka v{WERSJA}") + f" — szukam „{zapytanie}” "
          f"(profil: {profil}, źródła: {', '.join(zrodla)}, po {args.limit} najnowszych)…")

    zadania = {}
    with ThreadPoolExecutor(max_workers=2) as pula:
        if "olx" in zrodla:
            zadania["OLX"] = pula.submit(pobierz_olx, zapytanie, args.limit,
                                         args.olx_kategoria, args.debug)
        if "vinted" in zrodla:
            zadania["Vinted"] = pula.submit(pobierz_vinted, zapytanie, args.limit,
                                            args.vinted_katalog, args.vinted_domena, args.debug)
        oferty: list[Oferta] = []
        bledy = 0
        for nazwa, z in zadania.items():
            try:
                wynik = z.result()
                oferty.extend(wynik)
                print(K.szary(f"  {nazwa}: pobrano {len(wynik)} {ogloszen(len(wynik))}"))
            except BladZrodla as e:
                bledy += 1
                print(K.czer(f"  {nazwa}: błąd — {e}"), file=sys.stderr)

    if not oferty:
        print("Nie znaleziono żadnych ogłoszeń.")
        return 1 if bledy == len(zadania) else 0

    # Usuń duplikaty (ten sam link).
    widziane = set()
    oferty = [o for o in oferty if not (o.url in widziane or widziane.add(o.url))]

    for o in oferty:
        o.pasuje, o.powod = filtr.sprawdz(o.tytul)
        if o.pasuje and o.cena is not None:
            if args.min_cena is not None and o.cena < args.min_cena:
                o.pasuje, o.powod = False, f"cena poniżej {cena_str(args.min_cena)}"
            elif args.max_cena is not None and o.cena > args.max_cena:
                o.pasuje, o.powod = False, f"cena powyżej {cena_str(args.max_cena)}"

    st = policz_statystyki(oferty)
    ocen(oferty, st, prog)

    pasujace = [o for o in oferty if o.pasuje]
    odrzucone = [o for o in oferty if not o.pasuje]

    najstarsza = datetime.min.replace(tzinfo=timezone.utc)
    if args.sortuj == "data":
        pasujace.sort(key=lambda o: o.dodano or najstarsza, reverse=True)
    elif args.sortuj == "cena":
        pasujace.sort(key=lambda o: (o.cena is None, o.cena or 0))
    else:
        pasujace.sort(key=lambda o: (o.roznica is None, o.roznica or 0))

    okazje = sorted((o for o in pasujace if o.ocena == "OKAZJA"), key=lambda o: o.cena)

    if args.okazje:
        drukuj_tabele(okazje, f"Okazje ({len(okazje)})")
        if not okazje:
            print("\nBrak ofert wyraźnie poniżej mediany.")
    else:
        drukuj_tabele(pasujace, f"Pasujące ogłoszenia ({len(pasujace)}) — sortowanie: {args.sortuj}")

    if args.odrzucone and odrzucone:
        print()
        print(K.b(f"Odrzucone ({len(odrzucone)})"))
        for o in odrzucone:
            print(K.szary(f"  {'[' + o.zrodlo + ']':<8} {cena_str(o.cena, o.waluta):>12}  "
                          f"{_utnij(o.tytul, 60):<60}  ← {o.powod}"))

    drukuj_podsumowanie(st, prog, len(oferty), len(pasujace))

    if okazje and not args.okazje:
        print()
        print(K.ziel(f"Najlepsze okazje (top {min(args.top, len(okazje))}):"))
        for o in okazje[: args.top]:
            print(f"  {K.ziel(cena_str(o.cena, o.waluta)):>12}  ({o.roznica * 100:+.0f}%)  "
                  f"{_utnij(o.tytul, 50)}")
            print(K.szary(f"      {o.url}"))
    elif st and not okazje:
        print(K.szary("\nBrak okazji poniżej progu w tej chwili."))

    print(K.szary("\n* = cena do negocjacji (OLX)"))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nPrzerwano.", file=sys.stderr)
        sys.exit(130)
