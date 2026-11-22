import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import perelka as p  # noqa: E402


class TestFiltrKonsola(unittest.TestCase):
    def setUp(self):
        self.f = p.Filtr("ps5", "konsola", [], [])

    def ok(self, tytul):
        wynik, powod = self.f.sprawdz(tytul)
        self.assertTrue(wynik, f"powinno pasować: {tytul!r} ({powod})")

    def nie(self, tytul):
        wynik, _ = self.f.sprawdz(tytul)
        self.assertFalse(wynik, f"nie powinno pasować: {tytul!r}")

    def test_konsole(self):
        for t in [
            "Konsola PS5 Slim Digital",
            "Sony PlayStation 5 z napędem",
            "PlayStation5 825GB + pad",
            "PS 5 Disc Edition stan idealny",
            "Sony PlayStation 5 Digital Edition",
            "Nowa konsola do gier PS5",
            "PS5 + 2 pady + gry",
            "Konsola Sony PS5 Slim 2 pady",
            "Sprzedam PS5",
            "PS5 z padem i grą FIFA",
            "PS5 Slim 1TB",
            "PS5, nie uszkodzona, gwarancja",
        ]:
            self.ok(t)

    def test_akcesoria_i_smieci(self):
        for t in [
            "Pad DualSense PS5 biały",
            "Etui na PS5",
            "Gra Spider-Man 2 PS5",
            "Pad do konsoli PS5",
            "Ładowarka do padów PS5",
            "PS5 pad",
            "PS5 kontroler DualSense",
            "Obudowa PS5 Slim czarna",
            "Słuchawki Pulse 3D do PS5",
            "PlayStation Portal PS5",
            "Kupię PS5",
            "PS5 uszkodzona na części",
            "Samo pudełko PS5",
            "PS4 Pro 5 gier",
            "Xbox Series X",
            "Wynajem PS5 na weekend",
            "Stand vertical original PlayStation 5 PS5",
            "2 gałki 2x grzybek na analog pada DualSense od kontrolera PS5",
            "Borderlands 3 Super Deluxe Edition PS5",
            "Nioh 2 ps4/ps5",
            "Guitar Hero gitara ps4 ps5 pc",
            "Ieškau pirkti geros būklės Playstation 5.",
            "3D printana Vice City dekoracija za PS5",
        ]:
            self.nie(t)

    def test_wyklucz_i_wymagane(self):
        f = p.Filtr("ps5", "konsola", ["pro"], ["slim"])
        self.assertTrue(f.sprawdz("PS5 Slim Digital")[0])
        self.assertFalse(f.sprawdz("PS5 Pro Slim")[0])
        self.assertFalse(f.sprawdz("PS5 Digital")[0])

    def test_szukanie_akcesorium_wylacza_filtr(self):
        f = p.Filtr("pad ps5", "konsola", [], [])
        self.assertTrue(f.sprawdz("Pad DualSense PS5 biały")[0])


class TestFiltrTelefon(unittest.TestCase):
    def test_iphone(self):
        f = p.Filtr("iphone 13", "telefon", [], [])
        self.assertTrue(f.sprawdz("iPhone 13 128GB czarny")[0])
        self.assertTrue(f.sprawdz("Apple iPhone13 + etui")[0])
        self.assertFalse(f.sprawdz("Etui iPhone 13")[0])
        self.assertFalse(f.sprawdz("Szkło hartowane do iPhone 13")[0])
        self.assertFalse(f.sprawdz("iPhone 13 blokada iCloud")[0])
        self.assertFalse(f.sprawdz("iPhone 12")[0])
        self.assertFalse(f.sprawdz("Kryt na IPhone 13 Pro Max")[0])
        self.assertFalse(f.sprawdz("Telefontok iPhone 13 pro")[0])
        g = p.Filtr("iphone 13", "telefon", ["pro"], [])
        self.assertFalse(g.sprawdz("Iphone 13 ProMax 256GB")[0])


class TestFiltrCenowy(unittest.TestCase):
    def test_gry_bez_slowa_gra(self):
        f = p.Filtr("ps5", "konsola", [], [])
        dane = [("Playstation 5 slim 1TB", 2800), ("PS5 825GB Digital Edition", 1850),
                ("Ps5 slim", 3000), ("PS5 pro 2T", 3999), ("Returnal PS5", 90),
                ("F1 22 PlayStation 5", 40), ("Sprzedam PS5", 2100)]
        oferty = [p.Oferta("Vinted", t, c, "PLN", t) for t, c in dane]
        for o in oferty:
            o.pasuje, o.powod = f.sprawdz(o.tytul)
        self.assertTrue(all(o.pasuje for o in oferty))  # same słowa ich nie odróżnią
        p.filtr_cenowy(oferty, f)
        wynik = {o.tytul: o.pasuje for o in oferty}
        self.assertFalse(wynik["Returnal PS5"])
        self.assertFalse(wynik["F1 22 PlayStation 5"])
        self.assertTrue(wynik["PS5 pro 2T"])
        self.assertTrue(wynik["Sprzedam PS5"])

    def test_za_malo_kotwic(self):
        f = p.Filtr("ps5", "konsola", [], [])
        oferty = [p.Oferta("OLX", "PS5 slim", 2000, "PLN", "a"), p.Oferta("OLX", "Returnal PS5", 90, "PLN", "b")]
        p.filtr_cenowy(oferty, f)
        self.assertTrue(all(o.pasuje for o in oferty))


class TestProfil(unittest.TestCase):
    def test_auto(self):
        self.assertEqual(p.wybierz_profil("ps5"), "konsola")
        self.assertEqual(p.wybierz_profil("iphone 13"), "telefon")
        self.assertEqual(p.wybierz_profil("macbook air"), "laptop")
        self.assertEqual(p.wybierz_profil("rower górski"), "ogolny")


class TestParsery(unittest.TestCase):
    def test_olx(self):
        it = {
            "title": "Konsola PS5", "url": "https://www.olx.pl/d/oferta/x.html",
            "created_time": "2026-11-22T13:05:11+01:00",
            "params": [{"key": "price", "value": {"value": 1899, "type": "price",
                                                  "currency": "PLN", "negotiable": True}}],
            "location": {"city": {"name": "Kraków"}},
        }
        o = p.parsuj_olx(it)
        self.assertEqual((o.cena, o.waluta, o.do_negocjacji, o.lokalizacja), (1899.0, "PLN", True, "Kraków"))
        self.assertIsNotNone(o.dodano)

    def test_olx_zamiana(self):
        it = {"title": "PS5", "url": "u", "params": [{"key": "price", "value": {"type": "exchange"}}]}
        self.assertIsNone(p.parsuj_olx(it).cena)

    def test_olx_cena_z_etykiety(self):
        it = {"title": "PS5", "url": "u", "params": [{"key": "price", "value": {
            "value": None, "type": "arranged", "label": "1 299 zł", "negotiable": True}}]}
        self.assertEqual(p.parsuj_olx(it).cena, 1299.0)

    def test_vinted_strona_katalogu(self):
        elementy = [
            {"id": 11, "productItem": {"id": 11, "title": "PS5 Slim", "url": "/items/11-ps5-slim",
                                       "price": {"amount": "1500.00", "currencyCode": "PLN"},
                                       "isPromoted": False}},
            {"id": 12, "productItem": {"id": 12, "title": "PS5 promo", "url": "/items/12",
                                       "price": {"amount": "1.00", "currencyCode": "PLN"},
                                       "isPromoted": True}},
        ]
        dane = ('{"x":1,"items":{"items":' + json.dumps(elementy) +
                ',"pagination":{"current_page":1,"total_pages":3}}}')
        # Next.js dzieli dane na kawałki zapisane jako napisy JS
        polowa = len(dane) // 2
        html = "".join(f'<script>self.__next_f.push([1,{json.dumps(k)}])</script>'
                       for k in (dane[:polowa], dane[polowa:]))
        el, pag = p.wyciagnij_vinted(html)
        self.assertEqual(len(el), 2)
        self.assertEqual(pag["total_pages"], 3)
        oferty = [o for o in (p.parsuj_vinted(e, "https://www.vinted.pl") for e in el) if o]
        self.assertEqual(len(oferty), 1)  # promowane pominięte
        self.assertEqual((oferty[0].cena, oferty[0].url, oferty[0].id_num),
                         (1500.0, "https://www.vinted.pl/items/11-ps5-slim", 11))

    def test_vinted_brak_danych(self):
        with self.assertRaises(p.BladZrodla):
            p.wyciagnij_vinted("<html>nic</html>")


class TestCzas(unittest.TestCase):
    def test_czas_arg(self):
        self.assertEqual(p.czas_arg("6h"), 6 * 3600)
        self.assertEqual(p.czas_arg("30m"), 1800)
        self.assertEqual(p.czas_arg("1h30m"), 5400)
        self.assertEqual(p.czas_arg("45"), 2700)
        self.assertEqual(p.czas_arg("90s"), 90)
        self.assertEqual(p.czas_arg("0"), 0)
        with self.assertRaises(Exception):
            p.czas_arg("abc")


class TestMonitor(unittest.TestCase):
    def test_nowe_i_obnizki(self):
        args = p.zbuduj_parser().parse_args(["ps5", "--czas", "1h"])
        filtr = p.Filtr("ps5", "konsola", [], [])
        w = p.Wyszukiwanie(args, filtr, ["olx", "vinted"])
        teraz = datetime.now(timezone.utc)
        runda = {"n": 0}

        def olx(*a):
            oferty = [p.Oferta("OLX", f"Konsola PS5 {i}", 2000 + i * 10, "PLN", f"o{i}",
                               teraz - timedelta(minutes=10)) for i in range(6)]
            if runda["n"] >= 1:
                oferty.append(p.Oferta("OLX", "PS5 Slim nowa", 1500, "PLN", "nowa", teraz))
                oferty[0].cena = 1600  # obniżka
            return oferty

        def vinted(*a):
            o = [p.Oferta("Vinted", "PS5 digital", 1950, "PLN", "v1", id_num=1)]
            if runda["n"] >= 1:
                o.append(p.Oferta("Vinted", "PS5 slim", 1980, "PLN", "v2", id_num=2))
            return o

        p.pobierz_olx, stare_olx = olx, p.pobierz_olx
        p.pobierz_vinted, stare_v = vinted, p.pobierz_vinted
        try:
            self.assertEqual(w.skanuj(), [])
            runda["n"] = 1
            nowe_okazje = w.skanuj()
        finally:
            p.pobierz_olx, p.pobierz_vinted = stare_olx, stare_v
        self.assertEqual({o.url for o in nowe_okazje}, {"nowa", "o0"})
        self.assertTrue(w.baza["nowa"].nowa)
        self.assertEqual(w.baza["o0"].poprzednia_cena, 2000)
        v2 = w.baza["v2"]
        self.assertTrue(v2.nowa and v2.data_przyblizona)
        self.assertFalse(w.baza["v1"].nowa)

    def test_pierwszy_udany_odczyt_to_nie_nowosci(self):
        args = p.zbuduj_parser().parse_args(["ps5", "--czas", "1h", "--zrodla", "vinted"])
        w = p.Wyszukiwanie(args, p.Filtr("ps5", "konsola", [], []), ["vinted"])
        stan = {"blokada": True}

        def vinted(*a):
            if stan["blokada"]:
                raise p.BladZrodla("HTTP 403")
            return [p.Oferta("Vinted", "PS5 slim", 2000, "PLN", "v1", id_num=1)]

        p.pobierz_vinted, stare = vinted, p.pobierz_vinted
        try:
            w.skanuj()
            stan["blokada"] = False
            w.wstrzymane.clear()
            w.skanuj()
        finally:
            p.pobierz_vinted = stare
        self.assertIn("v1", w.baza)
        self.assertFalse(w.baza["v1"].nowa)


class TestOcena(unittest.TestCase):
    def test_statystyki_i_okazje(self):
        ceny = [2000, 2100, 1900, 2050, 1950, 1600, 50, 9999]
        oferty = [p.Oferta("OLX", f"PS5 {i}", c, "PLN", f"u{i}") for i, c in enumerate(ceny)]
        st = p.policz_statystyki(oferty)
        self.assertEqual(st.liczba, 6)  # 50 i 9999 odcięte jako wartości odstające
        self.assertAlmostEqual(st.mediana, 1975)
        p.ocen(oferty, st, 0.15)
        oc = {o.cena: o.ocena for o in oferty}
        self.assertEqual(oc[1600], "OKAZJA")
        self.assertEqual(oc[50], "PODEJRZANA")
        self.assertEqual(oc[9999], "DROGO")
        self.assertEqual(oc[2000], "W NORMIE")


if __name__ == "__main__":
    unittest.main()
