# perelka
Perełka – inteligentna aplikacja do ciągłego monitorowania portali ogłoszeniowych, wykrywania nowych ofert i wyłapywania prawdziwych okazji, zanim zrobią to inni.

## Wersja 0.2

Skrypt terminalowy, który:

- pobiera **najnowsze** ogłoszenia z **OLX** i **Vinted** dla podanej frazy,
- sprawdza słowa kluczowe i odrzuca ogłoszenia, które nie są szukanym przedmiotem
  (np. przy `ps5` odrzuca pady, etui, gry, ładowarki, ogłoszenia „kupię”, uszkodzone itp.) —
  bez AI, na samych regułach,
- liczy cenę referencyjną (medianę bez wartości odstających) i ocenia każdą ofertę:
  `OKAZJA`, `DOBRA`, `W NORMIE`, `DROGO`, `PODEJRZANA`,
- wyświetla ceny, oceny, wiek ogłoszenia i linki w terminalu,
- **tryb ciągły** (`--czas 6h --co 5m`): przez podany czas odświeża wyniki na ekranie, oznacza
  nowe ogłoszenia (`NOWE`) i obniżki cen (`↓`), a przy nowej okazji daje sygnał dźwiękowy
  (opcjonalnie też powiadomienie na pulpicie).

### Co nowego w 0.2

- Tryb ciągły: flagi `--czas`, `--co`, `--powiadomienia`, `--cicho`, `--pokaz`.
- Naprawiony Vinted (błąd 404): stary endpoint `/api/v2/catalog/items` przestał istnieć,
  ogłoszenia są teraz czytane ze strony katalogu Vinted.
- Lepsze odrzucanie gier i akcesoriów: reguła wielu platform („Nioh 2 ps4/ps5”), filtr cenowy
  („Returnal PS5” za 90 zł przy konsolach po 2500 zł), akcesoria w językach CZ/SK/HU/RO/LT/HR.
- Gdy serwis chwilowo blokuje zapytania, skrypt czeka coraz dłużej (do 30 min) i wznawia,
  a drugie źródło działa normalnie.

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

Monitorowanie przez 6 godzin z odświeżaniem co 5 minut:

```bash
./perelka.py ps5 --czas 6h --co 5m
```

To samo z powiadomieniami na pulpicie (GNOME, `notify-send`):

```bash
./perelka.py ps5 --czas 6h --co 5m --powiadomienia
```

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
| `-c, --czas` | jak długo monitorować: `6h`, `90m`, `1h30m`, `2d`, sama liczba = minuty, `0` = do Ctrl+C |
| `-i, --co` | co ile odświeżać (domyślnie `5m`, minimum `1m`); samo `--co` też włącza tryb ciągły |
| `--powiadomienia` | powiadomienie na pulpicie o nowej okazji (`notify-send`) |
| `--cicho` | bez sygnału dźwiękowego |
| `--pokaz` | ile ogłoszeń pokazać (w trybie ciągłym domyślnie tyle, ile mieści się na ekranie) |
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
| `--bez-filtra-cen` | nie odrzucaj ofert dużo tańszych od typowej ceny przedmiotu |
| `--olx-kategoria`, `--vinted-katalog` | opcjonalne ID kategorii w serwisach |
| `--bez-kolorow`, `--debug`, `--version` | |

### Jak działa filtr słów kluczowych

1. Wszystkie słowa z zapytania muszą być w tytule (`PS5`, `PS 5`, `PlayStation 5` są traktowane tak samo).
2. Odrzucane są zawsze: kupno/skup, uszkodzone/na części, blokady, atrapy, wynajem, „samo pudełko”.
3. Jeśli **przed** nazwą stoi akcesorium albo przyimek („**Pad** DualSense PS5”, „Etui **na** PS5”) — odrzucone.
   Wyjątek: najpierw pada słowo typu „konsola” („Konsola do gier PS5”).
4. Jeśli akcesorium stoi **za** nazwą, ogłoszenie przechodzi tylko jako zestaw
   („PS5 **+** pad”, „PS5 **z** padem”, „Konsola PS5 2 pady”), a „PS5 pad” jest odrzucane.
5. Kilka platform w tytule („gra ps4/ps5”, „pad ps4 pc”) = gra lub akcesorium.
6. **Filtr cenowy:** ogłoszenia z pewnymi słowami (konsola, slim, digital, 825GB, 128GB…)
   wyznaczają typową cenę. Ogłoszenia bez takich słów, tańsze niż 35% tej ceny, są odrzucane.
   Wyłapuje to gry podpisane samym tytułem, np. „Returnal PS5”.

Jeśli szukasz akcesorium (np. `./perelka.py "pad ps5"`), filtr akcesoriów sam się wyłącza.

### Testy

```bash
python3 -m unittest discover -s tests -v
```

### Tryb ciągły — wskazówki

- Ekran jest czyszczony i rysowany od nowa po każdym odświeżeniu; na dole widać odliczanie.
- Ctrl+C kończy w dowolnym momencie i wypisuje podsumowanie.
- Żeby skrypt działał po zamknięciu terminala, uruchom go w `tmux`:

```bash
sudo dnf install tmux
```

```bash
tmux new -s perelka './perelka.py ps5 --czas 6h'
```

  Odłączenie: Ctrl+B, potem D. Powrót: `tmux attach -t perelka`.
- `notify-send` jest w Fedorze Workstation od razu; jeśli go brakuje: `sudo dnf install libnotify`.

### Uwagi

- OLX: publiczne JSON API strony olx.pl. Vinted: dane osadzone w stronie katalogu.
  Oba mogą się zmienić bez ostrzeżenia.
- Vinted jest chroniony przez Cloudflare i przy zbyt częstych zapytaniach zwraca 403. Skrypt
  wtedy czeka i ponawia. Nie ustawiaj `--co` poniżej kilku minut na wiele godzin.
- Vinted nie podaje daty dodania ogłoszenia. W trybie ciągłym nowe ogłoszenia z Vinted mają czas
  liczony od pierwszego wykrycia (`~5 min`), a przy pierwszym odczycie widać `—`.
- `PS5` pasuje też do `PS5 Pro`. Jeśli chcesz to rozdzielić, użyj `--wyklucz pro`.
- Ocena `PODEJRZANA` (poniżej połowy mediany) często oznacza oszustwo, uszkodzony sprzęt albo
  grę/akcesorium, którego filtr nie rozpoznał.
