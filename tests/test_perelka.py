import os
import sys
import unittest

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

    def test_vinted_oba_formaty(self):
        baza = "https://www.vinted.pl"
        a = p.parsuj_vinted({"id": 1, "title": "PS5", "price": {"amount": "1500.0", "currency_code": "PLN"},
                             "photo": {"high_resolution": {"timestamp": 1763800000}}}, baza)
        b = p.parsuj_vinted({"id": 2, "title": "PS5", "price": "1450.0", "currency": "PLN",
                             "url": "/items/2-ps5"}, baza)
        self.assertEqual(a.cena, 1500.0)
        self.assertEqual(a.url, "https://www.vinted.pl/items/1")
        self.assertEqual(b.cena, 1450.0)
        self.assertEqual(b.url, "https://www.vinted.pl/items/2-ps5")


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
