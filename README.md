# Kakovost zraka v Sloveniji — arhiv urnih podatkov

Ta repozitorij vsebuje arhiv urnih podatkov o kakovosti zunanjega zraka v Sloveniji, ki jih objavlja Agencija Republike Slovenije za okolje (ARSO). Podatki so na voljo v obliki CSV datotek, ki zajemajo merilna mesta in njihove meritve za različne onesnaževalce.

## Datoteke

```
data/urni/YYYY-MM.csv         urni podatki, ena vrstica na merilno mesto in uro
data/dnevni/YYYY-MM.csv       dnevne vrednosti in največja 8-urna povprečja
data/merilna-mesta.csv        seznam merilnih mest s koordinatami
```

Imena stolpcev so enaka kot v izvorni datoteki. Izjema je le obdobje pri urnih podatkih, ki je
poimenovano `datum_cas` (v izvoru `datum_do`).

Urni podatki: `datum_cas`, `sifra`, `merilno_mesto`, `pm10`, `pm2.5`, `o3`, `no2`, `nox`, `so2`, `co`, `benzen`

Dnevni podatki: `datum`, `sifra`, `merilno_mesto`, `pm10_dnevna`, `pm2.5_dnevna`, `co_max_8urna`, `o3_max_urna`, `o3_max_8urna`, `no2_max_urna`, `so2_dnevna`, `so2_max_urna`

## Opombe

- Časi so v lokalnem času z zapisanim odmikom, npr. `2026-11-22T13:00:00+01:00`.
- Prazen stolpec pomeni, da vrednosti ni bilo.
- Enote so µg/m³, razen za CO, ki je v mg/m³.
- Možne so tudi vrednosti oblike `<N` (npr. `<1`, `<2`), kar pomeni, da je bila izmerjena vrednost pod mejo detekcije (LOD).

## Navajanje vira

Podatki o kakovosti zunanjega zraka, Agencija Republike Slovenije za okolje.

Vir in opis podatkov: <https://www.arso.gov.si/zrak/kakovost%20zraka/podatki/kakovost_zraka_xml.html>
