# perelka
Perełka – inteligentna aplikacja do ciągłego monitorowania portali ogłoszeniowych, wykrywania nowych ofert i wyłapywania prawdziwych okazji, zanim zrobią to inni.

## Wersja 0.1

Skrypt terminalowy, który:

- pobiera **najnowsze** ogłoszenia z **OLX** i **Vinted** dla podanej frazy,
- sprawdza słowa kluczowe i odrzuca ogłoszenia, które nie są szukanym przedmiotem
  (np. przy `ps5` odrzuca pady, etui, gry, ładowarki, ogłoszenia „kupię”, uszkodzone itp.) —
  bez AI, na samych regułach,
- liczy cenę referencyjną (medianę bez wartości odstających) i ocenia każdą ofertę:
  `OKAZJA`, `DOBRA`, `W NORMIE`, `DROGO`, `PODEJRZANA`,
- wyświetla ceny, oceny, wiek ogłoszenia i linki w terminalu.

### Wymagania (Fedora)

Wystarczy Python 3, który jest w Fedorze od razu. Nie trzeba instalować bibliotek.

```bash
sudo dnf install python3
```

### Uruchomienie

```bash
chmod +x perelka.py
```

```bash
./perelka.py ps5
```

### Przykłady

```bash
./perelka.py ps5 --okazje
```

```bash
./perelka.py "iphone 13" --min-cena 500 --wyklucz pro,mini
```

```bash
./perelka.py ps5 --zrodla olx --limit 100 --sortuj cena
```

```bash
./perelka.py "nintendo switch" --wyklucz "switch 2" --odrzucone
```

### Flagi

| Flaga | Opis |
|---|---|
| `-z, --zrodla` | `olx`, `vinted` lub oba po przecinku (domyślnie oba) |
| `-n, --limit` | ile najnowszych ogłoszeń pobrać z każdego źródła (domyślnie 40) |
| `-t, --typ` | kategoria do filtrowania akcesoriów: `auto`, `konsola`, `telefon`, `laptop`, `ogolny` |
| `-w, --wyklucz` | słowa, które odrzucają ogłoszenie (np. `pro,mini`; `uszkodz*` = dowolna końcówka) |
| `-m, --wymagane` | dodatkowe słowa, które muszą być w tytule |
| `--min-cena`, `--max-cena` | zakres cen |
| `-p, --prog` | o ile % poniżej mediany oferta to okazja (domyślnie 15) |
| `-s, --sortuj` | `data` (domyślnie), `cena`, `ocena` |
| `-o, --okazje` | pokaż tylko okazje |
| `--odrzucone` | pokaż też odrzucone ogłoszenia z powodem odrzucenia |
| `--bez-filtra` | wyłącz filtr akcesoriów |
| `--olx-kategoria`, `--vinted-katalog` | opcjonalne ID kategorii w serwisach |
| `--bez-kolorow`, `--debug`, `--version` | |

### Jak działa filtr słów kluczowych

1. Wszystkie słowa z zapytania muszą być w tytule (`PS5`, `PS 5`, `PlayStation 5` są traktowane tak samo).
2. Odrzucane są zawsze: kupno/skup, uszkodzone/na części, blokady, atrapy, wynajem, „samo pudełko”.
3. Jeśli **przed** nazwą stoi akcesorium albo przyimek („**Pad** DualSense PS5”, „Etui **na** PS5”) — odrzucone.
   Wyjątek: najpierw pada słowo typu „konsola” („Konsola do gier PS5”).
4. Jeśli akcesorium stoi **za** nazwą, ogłoszenie przechodzi tylko jako zestaw
   („PS5 **+** pad”, „PS5 **z** padem”, „Konsola PS5 2 pady”), a „PS5 pad” jest odrzucane.

Jeśli szukasz akcesorium (np. `./perelka.py "pad ps5"`), filtr akcesoriów sam się wyłącza.

### Testy

```bash
python3 -m unittest discover -s tests -v
```

### Uwagi

- Korzysta z publicznych endpointów JSON, z których korzystają strony OLX i Vinted. Mogą się one zmienić
  bez ostrzeżenia, a Vinted czasem blokuje zapytania automatyczne — wtedy skrypt wypisze błąd
  i pokaże wyniki z drugiego źródła.
- `PS5` pasuje też do `PS5 Pro`. Jeśli chcesz to rozdzielić, użyj `--wyklucz pro`.
- Ocena `PODEJRZANA` (poniżej połowy mediany) często oznacza oszustwo, uszkodzony sprzęt albo
  grę/akcesorium, którego filtr nie rozpoznał.
