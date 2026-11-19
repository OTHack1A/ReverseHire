"""Italian provinces and regions, countries and a few foreign towns: static tables.

- PROVINCES: every Italian province / metropolitan city / free consortium (107), with
  its sigla (the two letters of car plates and addresses, "BO"), the official name,
  common variants and old names, the region key (as in geo/regions.py REGIONS) and the
  capital's coordinates (rounded to ~100 m: enough for a search centre).
- REGION_NAMES: region key -> names (Italian, English, local variants).
- COUNTRIES: ISO 3166-1 alpha-2 -> names in Italian, English, Ukrainian and local forms.
  San Marino and the Vatican are countries of their own.
- FOREIGN_TOWNS / SAN_MARINO_TOWNS: towns that name a country by themselves ("Lugano",
  "Vienna", "Dogana").

Sources: ISTAT list of provinces (sigle automobilistiche, 2026: 107 units, the
Sardinian reform of 2016 with "Sud Sardegna"); coordinates of the capitals from
OpenStreetMap (city nodes). Old Sardinian provinces (CI, VS, OG, OT) map to the
current ones.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache


def fold(text: str) -> str:
    """Lowercase, no accents, "'" and "-" as spaces, collapsed spaces."""
    decomposed = unicodedata.normalize("NFKD", str(text or "").casefold())
    plain = "".join(c for c in decomposed if not unicodedata.combining(c))
    for char in "'’`´-–—_/.":
        plain = plain.replace(char, " ")
    return " ".join(plain.split())


@dataclass(frozen=True)
class ItalianProvince:
    code: str                      # sigla, "BO"
    name: str                      # "Bologna"
    region: str                    # region key, "emilia-romagna"
    lat: float                     # the capital
    lon: float
    capital: str                   # "Bologna" ("Forlì" for Forlì-Cesena)
    aliases: tuple[str, ...] = field(default_factory=tuple)

    @property
    def names(self) -> tuple[str, ...]:
        return (self.name, self.capital) + self.aliases


def _p(code, name, region, lat, lon, capital=None, *aliases) -> ItalianProvince:
    return ItalianProvince(code, name, region, lat, lon, capital or name, tuple(aliases))


_ALL = (
    # Abruzzo
    _p("AQ", "L'Aquila", "abruzzo", 42.350, 13.399, None, "Aquila"),
    _p("CH", "Chieti", "abruzzo", 42.351, 14.168),
    _p("PE", "Pescara", "abruzzo", 42.461, 14.216),
    _p("TE", "Teramo", "abruzzo", 42.659, 13.704),
    # Basilicata
    _p("MT", "Matera", "basilicata", 40.666, 16.604),
    _p("PZ", "Potenza", "basilicata", 40.640, 15.806),
    # Calabria
    _p("CZ", "Catanzaro", "calabria", 38.905, 16.594),
    _p("CS", "Cosenza", "calabria", 39.298, 16.254),
    _p("KR", "Crotone", "calabria", 39.081, 17.127),
    _p("RC", "Reggio Calabria", "calabria", 38.111, 15.647, None, "Reggio di Calabria"),
    _p("VV", "Vibo Valentia", "calabria", 38.676, 16.101),
    # Campania
    _p("AV", "Avellino", "campania", 40.914, 14.790),
    _p("BN", "Benevento", "campania", 41.130, 14.782),
    _p("CE", "Caserta", "campania", 41.074, 14.333),
    _p("NA", "Napoli", "campania", 40.852, 14.268, None, "Naples", "Neapel"),
    _p("SA", "Salerno", "campania", 40.682, 14.769),
    # Emilia-Romagna
    _p("BO", "Bologna", "emilia-romagna", 44.494, 11.343),
    _p("FE", "Ferrara", "emilia-romagna", 44.836, 11.620),
    _p("FC", "Forlì-Cesena", "emilia-romagna", 44.222, 12.041, "Forlì", "Forli Cesena", "Forlì e Cesena"),
    _p("MO", "Modena", "emilia-romagna", 44.647, 10.925),
    _p("PR", "Parma", "emilia-romagna", 44.801, 10.328),
    _p("PC", "Piacenza", "emilia-romagna", 45.052, 9.693),
    _p("RA", "Ravenna", "emilia-romagna", 44.418, 12.204),
    _p("RE", "Reggio Emilia", "emilia-romagna", 44.698, 10.631, None, "Reggio nell'Emilia"),
    _p("RN", "Rimini", "emilia-romagna", 44.060, 12.566),
    # Friuli-Venezia Giulia
    _p("GO", "Gorizia", "friuli-venezia-giulia", 45.940, 13.621),
    _p("PN", "Pordenone", "friuli-venezia-giulia", 45.956, 12.660),
    _p("TS", "Trieste", "friuli-venezia-giulia", 45.650, 13.777),
    _p("UD", "Udine", "friuli-venezia-giulia", 46.062, 13.235),
    # Lazio
    _p("FR", "Frosinone", "lazio", 41.640, 13.340),
    _p("LT", "Latina", "lazio", 41.467, 12.904),
    _p("RI", "Rieti", "lazio", 42.404, 12.857),
    _p("RM", "Roma", "lazio", 41.903, 12.496, None, "Rome", "Rom"),
    _p("VT", "Viterbo", "lazio", 42.417, 12.105),
    # Liguria
    _p("GE", "Genova", "liguria", 44.405, 8.946, None, "Genoa"),
    _p("IM", "Imperia", "liguria", 43.889, 8.039),
    _p("SP", "La Spezia", "liguria", 44.102, 9.824, None, "Spezia"),
    _p("SV", "Savona", "liguria", 44.309, 8.477),
    # Lombardia
    _p("BG", "Bergamo", "lombardia", 45.698, 9.677),
    _p("BS", "Brescia", "lombardia", 45.541, 10.212),
    _p("CO", "Como", "lombardia", 45.808, 9.085),
    _p("CR", "Cremona", "lombardia", 45.133, 10.023),
    _p("LC", "Lecco", "lombardia", 45.856, 9.393),
    _p("LO", "Lodi", "lombardia", 45.314, 9.503),
    _p("MN", "Mantova", "lombardia", 45.156, 10.791, None, "Mantua"),
    _p("MI", "Milano", "lombardia", 45.464, 9.190, None, "Milan", "Mailand"),
    _p("MB", "Monza e della Brianza", "lombardia", 45.584, 9.274, "Monza", "Monza e Brianza", "Monza Brianza",
       "Brianza"),
    _p("PV", "Pavia", "lombardia", 45.185, 9.155),
    _p("SO", "Sondrio", "lombardia", 46.170, 9.872),
    _p("VA", "Varese", "lombardia", 45.820, 8.825),
    # Marche
    _p("AN", "Ancona", "marche", 43.616, 13.519),
    _p("AP", "Ascoli Piceno", "marche", 42.854, 13.575, None, "Ascoli"),
    _p("FM", "Fermo", "marche", 43.160, 13.718),
    _p("MC", "Macerata", "marche", 43.300, 13.453),
    _p("PU", "Pesaro e Urbino", "marche", 43.910, 12.913, "Pesaro", "Pesaro Urbino", "Pesaro-Urbino"),
    # Molise
    _p("CB", "Campobasso", "molise", 41.561, 14.668),
    _p("IS", "Isernia", "molise", 41.594, 14.233),
    # Piemonte
    _p("AL", "Alessandria", "piemonte", 44.912, 8.615),
    _p("AT", "Asti", "piemonte", 44.900, 8.207),
    _p("BI", "Biella", "piemonte", 45.566, 8.054),
    _p("CN", "Cuneo", "piemonte", 44.384, 7.543),
    _p("NO", "Novara", "piemonte", 45.446, 8.622),
    _p("TO", "Torino", "piemonte", 45.070, 7.687, None, "Turin"),
    _p("VB", "Verbano-Cusio-Ossola", "piemonte", 45.922, 8.551, "Verbania", "VCO", "Verbano Cusio Ossola"),
    _p("VC", "Vercelli", "piemonte", 45.320, 8.419),
    # Puglia
    _p("BA", "Bari", "puglia", 41.117, 16.872),
    _p("BT", "Barletta-Andria-Trani", "puglia", 41.320, 16.282, "Barletta", "BAT", "Barletta Andria Trani"),
    _p("BR", "Brindisi", "puglia", 40.632, 17.936),
    _p("FG", "Foggia", "puglia", 41.462, 15.544),
    _p("LE", "Lecce", "puglia", 40.353, 18.172),
    _p("TA", "Taranto", "puglia", 40.464, 17.247),
    # Sardegna
    _p("CA", "Cagliari", "sardegna", 39.224, 9.122),
    _p("NU", "Nuoro", "sardegna", 40.321, 9.330, None, "Ogliastra"),
    _p("OR", "Oristano", "sardegna", 39.904, 8.592),
    _p("SS", "Sassari", "sardegna", 40.727, 8.560, None, "Olbia-Tempio", "Gallura"),
    _p("SU", "Sud Sardegna", "sardegna", 39.167, 8.522, "Carbonia", "South Sardinia", "Carbonia-Iglesias",
       "Medio Campidano"),
    # Sicilia
    _p("AG", "Agrigento", "sicilia", 37.311, 13.576),
    _p("CL", "Caltanissetta", "sicilia", 37.490, 14.062),
    _p("CT", "Catania", "sicilia", 37.502, 15.087),
    _p("EN", "Enna", "sicilia", 37.567, 14.279),
    _p("ME", "Messina", "sicilia", 38.193, 15.554),
    _p("PA", "Palermo", "sicilia", 38.116, 13.361),
    _p("RG", "Ragusa", "sicilia", 36.926, 14.725),
    _p("SR", "Siracusa", "sicilia", 37.075, 15.287, None, "Syracuse"),
    _p("TP", "Trapani", "sicilia", 38.018, 12.515),
    # Toscana
    _p("AR", "Arezzo", "toscana", 43.463, 11.880),
    _p("FI", "Firenze", "toscana", 43.770, 11.256, None, "Florence", "Florenz"),
    _p("GR", "Grosseto", "toscana", 42.760, 11.113),
    _p("LI", "Livorno", "toscana", 43.548, 10.311),
    _p("LU", "Lucca", "toscana", 43.843, 10.507),
    _p("MS", "Massa-Carrara", "toscana", 44.035, 10.140, "Massa", "Massa Carrara", "Carrara"),
    _p("PI", "Pisa", "toscana", 43.716, 10.402),
    _p("PT", "Pistoia", "toscana", 43.933, 10.917),
    _p("PO", "Prato", "toscana", 43.880, 11.097),
    _p("SI", "Siena", "toscana", 43.318, 11.331),
    # Trentino-Alto Adige
    _p("BZ", "Bolzano", "trentino-alto-adige", 46.498, 11.354, None, "Bozen", "Bolzano/Bozen", "Bolzano-Bozen",
       "Alto Adige", "Südtirol", "South Tyrol"),
    _p("TN", "Trento", "trentino-alto-adige", 46.070, 11.121, None, "Trient", "Trentino"),
    # Umbria
    _p("PG", "Perugia", "umbria", 43.111, 12.389),
    _p("TR", "Terni", "umbria", 42.563, 12.643),
    # Valle d'Aosta
    _p("AO", "Aosta", "valle-d-aosta", 45.737, 7.315, None, "Aoste"),
    # Veneto
    _p("BL", "Belluno", "veneto", 46.142, 12.217),
    _p("PD", "Padova", "veneto", 45.406, 11.877, None, "Padua"),
    _p("RO", "Rovigo", "veneto", 45.070, 11.790),
    _p("TV", "Treviso", "veneto", 45.666, 12.243),
    _p("VE", "Venezia", "veneto", 45.440, 12.316, None, "Venice", "Venedig", "Mestre"),
    _p("VR", "Verona", "veneto", 45.438, 10.992),
    _p("VI", "Vicenza", "veneto", 45.548, 11.546),
)

PROVINCES: dict[str, ItalianProvince] = {p.code: p for p in _ALL}
# Codes of abolished provinces that still appear in addresses.
OLD_CODES = {"CI": "SU", "VS": "SU", "OG": "NU", "OT": "SS", "FO": "FC", "PS": "PU"}

REGION_NAMES: dict[str, tuple[str, ...]] = {
    "abruzzo": ("Abruzzo", "Abruzzi"),
    "basilicata": ("Basilicata", "Lucania"),
    "calabria": ("Calabria",),
    "campania": ("Campania",),
    "emilia-romagna": ("Emilia-Romagna", "Emilia Romagna", "Emilia", "Romagna"),
    "friuli-venezia-giulia": ("Friuli-Venezia Giulia", "Friuli Venezia Giulia", "Friuli"),
    "lazio": ("Lazio", "Latium"),
    "liguria": ("Liguria",),
    "lombardia": ("Lombardia", "Lombardy"),
    "marche": ("Marche", "The Marches", "Marches"),
    "molise": ("Molise",),
    "piemonte": ("Piemonte", "Piedmont"),
    "puglia": ("Puglia", "Apulia"),
    "sardegna": ("Sardegna", "Sardinia"),
    "sicilia": ("Sicilia", "Sicily"),
    "toscana": ("Toscana", "Tuscany"),
    "trentino-alto-adige": ("Trentino-Alto Adige", "Trentino Alto Adige", "Trentino-Südtirol",
                            "Trentino-Alto Adige/Südtirol"),
    "umbria": ("Umbria",),
    "valle-d-aosta": ("Valle d'Aosta", "Aosta Valley", "Vallée d'Aoste", "Val d'Aosta"),
    "veneto": ("Veneto",),
}

# ISO code -> (Italian, English, Ukrainian, *other forms)
COUNTRIES: dict[str, tuple[str, ...]] = {
    "IT": ("Italia", "Italy", "Італія", "Italien", "Italie", "Repubblica Italiana", "ITA"),
    "SM": ("San Marino", "San Marino", "Сан-Марино", "Repubblica di San Marino", "RSM", "Rep. San Marino"),
    "VA": ("Città del Vaticano", "Vatican City", "Ватикан", "Vaticano", "Vatican", "Holy See"),
    "CH": ("Svizzera", "Switzerland", "Швейцарія", "Schweiz", "Suisse", "Svizra", "Confederazione Svizzera"),
    "AT": ("Austria", "Austria", "Австрія", "Österreich", "Oesterreich"),
    "DE": ("Germania", "Germany", "Німеччина", "Deutschland"),
    "FR": ("Francia", "France", "Франція"),
    "ES": ("Spagna", "Spain", "Іспанія", "España", "Espana"),
    "PT": ("Portogallo", "Portugal", "Португалія"),
    "GB": ("Regno Unito", "United Kingdom", "Велика Британія", "UK", "U.K.", "Great Britain", "Gran Bretagna",
           "England", "Inghilterra", "Scotland", "Scozia", "Wales", "Galles"),
    "IE": ("Irlanda", "Ireland", "Ірландія", "Éire"),
    "NL": ("Paesi Bassi", "Netherlands", "Нідерланди", "Olanda", "Holland", "Nederland", "The Netherlands"),
    "BE": ("Belgio", "Belgium", "Бельгія", "Belgique", "België"),
    "LU": ("Lussemburgo", "Luxembourg", "Люксембург"),
    "LI": ("Liechtenstein", "Liechtenstein", "Ліхтенштейн"),
    "MC": ("Principato di Monaco", "Monaco", "Монако", "Monaco Principality"),
    "DK": ("Danimarca", "Denmark", "Данія", "Danmark"),
    "SE": ("Svezia", "Sweden", "Швеція", "Sverige"),
    "NO": ("Norvegia", "Norway", "Норвегія", "Norge"),
    "FI": ("Finlandia", "Finland", "Фінляндія", "Suomi"),
    "PL": ("Polonia", "Poland", "Польща", "Polska"),
    "CZ": ("Repubblica Ceca", "Czech Republic", "Чехія", "Czechia", "Česko", "Cechia"),
    "SK": ("Slovacchia", "Slovakia", "Словаччина", "Slovensko"),
    "SI": ("Slovenia", "Slovenia", "Словенія", "Slovenija"),
    "HR": ("Croazia", "Croatia", "Хорватія", "Hrvatska"),
    "HU": ("Ungheria", "Hungary", "Угорщина", "Magyarország"),
    "RO": ("Romania", "Romania", "Румунія", "România"),
    "BG": ("Bulgaria", "Bulgaria", "Болгарія"),
    "GR": ("Grecia", "Greece", "Греція", "Hellas"),
    "MT": ("Malta", "Malta", "Мальта"),
    "CY": ("Cipro", "Cyprus", "Кіпр"),
    "UA": ("Ucraina", "Ukraine", "Україна"),
    "MD": ("Moldavia", "Moldova", "Молдова"),
    "AL": ("Albania", "Albania", "Албанія", "Shqipëria"),
    "RS": ("Serbia", "Serbia", "Сербія", "Srbija"),
    "TR": ("Turchia", "Turkey", "Туреччина", "Türkiye"),
    "US": ("Stati Uniti", "United States", "США", "USA", "U.S.A.", "United States of America", "Stati Uniti d'America"),
    "CA": ("Canada", "Canada", "Канада"),
    "MX": ("Messico", "Mexico", "Мексика", "México"),
    "BR": ("Brasile", "Brazil", "Бразилія", "Brasil"),
    "AR": ("Argentina", "Argentina", "Аргентина"),
    "AU": ("Australia", "Australia", "Австралія"),
    "CN": ("Cina", "China", "Китай"),
    "JP": ("Giappone", "Japan", "Японія"),
    "IN": ("India", "India", "Індія"),
    "AE": ("Emirati Arabi Uniti", "United Arab Emirates", "ОАЕ", "UAE", "Emirati"),
    "IL": ("Israele", "Israel", "Ізраїль"),
    "MA": ("Marocco", "Morocco", "Марокко", "Maroc"),
    "TN": ("Tunisia", "Tunisia", "Туніс", "Tunisie"),
    "EG": ("Egitto", "Egypt", "Єгипет"),
}

SAN_MARINO_TOWNS = ("Città di San Marino", "Dogana", "Serravalle", "Borgo Maggiore", "Domagnano", "Fiorentino",
                    "Acquaviva", "Chiesanuova", "Faetano", "Montegiardino", "Falciano", "Rovereta",
                    "Galazzano", "Cailungo", "Murata", "Gualdicciolo", "Ventoso")

# town -> country code (towns that are enough to name the country)
FOREIGN_TOWNS: dict[str, str] = {
    "London": "GB", "Londra": "GB", "Manchester": "GB", "Edinburgh": "GB", "Edimburgo": "GB",
    "Paris": "FR", "Parigi": "FR", "Lyon": "FR", "Lione": "FR", "Nice": "FR", "Nizza": "FR", "Marseille": "FR",
    "Marsiglia": "FR", "Berlin": "DE", "Berlino": "DE", "München": "DE", "Munich": "DE", "Monaco di Baviera": "DE",
    "Frankfurt": "DE", "Francoforte": "DE", "Hamburg": "DE", "Amburgo": "DE", "Stuttgart": "DE",
    "Stoccarda": "DE", "Köln": "DE", "Colonia": "DE", "Düsseldorf": "DE",
    "Wien": "AT", "Vienna": "AT", "Innsbruck": "AT", "Salzburg": "AT", "Salisburgo": "AT", "Graz": "AT",
    "Zürich": "CH", "Zurich": "CH", "Zurigo": "CH", "Genève": "CH", "Geneva": "CH", "Ginevra": "CH",
    "Lugano": "CH", "Bern": "CH", "Berna": "CH", "Basel": "CH", "Basilea": "CH", "Lausanne": "CH",
    "Losanna": "CH", "Chiasso": "CH", "Mendrisio": "CH", "Bellinzona": "CH", "Locarno": "CH", "Stabio": "CH",
    "Madrid": "ES", "Barcelona": "ES", "Barcellona": "ES", "Valencia": "ES", "Lisboa": "PT", "Lisbon": "PT",
    "Lisbona": "PT", "Porto": "PT", "Amsterdam": "NL", "Rotterdam": "NL", "Eindhoven": "NL",
    "Bruxelles": "BE", "Brussels": "BE", "Brussel": "BE", "Antwerpen": "BE", "Anversa": "BE",
    "Dublin": "IE", "Dublino": "IE", "Praha": "CZ", "Prague": "CZ", "Praga": "CZ", "Warszawa": "PL",
    "Warsaw": "PL", "Varsavia": "PL", "Kraków": "PL", "Cracovia": "PL", "Kyiv": "UA", "Kiev": "UA",
    "Київ": "UA", "Lviv": "UA", "Leopoli": "UA", "Львів": "UA", "Kharkiv": "UA", "Odesa": "UA",
    "Odessa": "UA", "Budapest": "HU", "București": "RO", "Bucharest": "RO", "Bucarest": "RO",
    "Ljubljana": "SI", "Lubiana": "SI", "Koper": "SI", "Capodistria": "SI", "Nova Gorica": "SI",
    "Zagreb": "HR", "Zagabria": "HR", "Rijeka": "HR", "Fiume": "HR", "Split": "HR", "Spalato": "HR",
    "København": "DK", "Copenhagen": "DK", "Copenaghen": "DK", "Stockholm": "SE", "Stoccolma": "SE",
    "Oslo": "NO", "Helsinki": "FI", "Athens": "GR", "Atene": "GR", "Valletta": "MT", "Tirana": "AL",
    "Belgrade": "RS", "Belgrado": "RS", "Istanbul": "TR", "Dubai": "AE", "Abu Dhabi": "AE",
    "New York": "US", "San Francisco": "US", "Boston": "US", "Chicago": "US", "Los Angeles": "US",
    "Seattle": "US", "Toronto": "CA", "Montreal": "CA", "Vancouver": "CA", "Sydney": "AU", "Melbourne": "AU",
    "Vaduz": "LI", "Montecarlo": "MC", "Monte Carlo": "MC", "Luxembourg City": "LU",
}


# --- lookups --------------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _province_index() -> dict[str, str]:
    index: dict[str, str] = {}
    for province in _ALL:
        for name in province.names:
            index.setdefault(fold(name), province.code)
    return index


@lru_cache(maxsize=1)
def _region_index() -> dict[str, str]:
    index: dict[str, str] = {}
    for key, names in REGION_NAMES.items():
        for name in names + (key,):
            index.setdefault(fold(name), key)
    index.pop("emilia", None)    # "Emilia" / "Romagna" alone are not regions for matching
    index.pop("romagna", None)
    return index


@lru_cache(maxsize=1)
def _country_index() -> dict[str, str]:
    index: dict[str, str] = {}
    for code, names in COUNTRIES.items():
        for name in names:
            index.setdefault(fold(name), code)
        index.setdefault(fold(code), code)
    return index


@lru_cache(maxsize=1)
def _foreign_town_index() -> dict[str, tuple[str, str]]:
    index = {fold(name): (name, code) for name, code in FOREIGN_TOWNS.items()}
    for name in SAN_MARINO_TOWNS:
        index[fold(name)] = (name, "SM")
    return index


def province(code: str | None) -> ItalianProvince | None:
    """The province of a sigla ("bo", "FC"; old codes "FO", "CI" too); None if unknown."""
    value = (code or "").strip().upper()
    value = OLD_CODES.get(value, value)
    return PROVINCES.get(value)


def province_by_name(name: str | None) -> ItalianProvince | None:
    """By name, capital or variant ("Forlì-Cesena", "forli'", "Monza", "Bozen")."""
    code = _province_index().get(fold(name or ""))
    return PROVINCES.get(code) if code else None


def provinces_of_region(region_key: str | None) -> list[ItalianProvince]:
    return sorted((p for p in _ALL if p.region == region_key), key=lambda p: fold(p.name))


def region_key(name: str | None) -> str | None:
    """The region key of a name ("Lombardy", "Emilia Romagna", "emilia-romagna")."""
    return _region_index().get(fold(name or ""))


def region_name(key: str | None) -> str:
    names = REGION_NAMES.get(key or "")
    return names[0] if names else ""


def country_code(name: str | None) -> str | None:
    """ISO code of a country name or code in any of the listed languages."""
    return _country_index().get(fold(name or ""))


def country_name(code: str | None, language: str = "it") -> str:
    names = COUNTRIES.get((code or "").upper())
    if not names:
        return code or ""
    return {"it": names[0], "en": names[1], "uk": names[2]}.get(language, names[1])


def foreign_town(name: str | None) -> tuple[str, str] | None:
    """(display name, country code) of a town that names a foreign country (San Marino too)."""
    return _foreign_town_index().get(fold(name or ""))


@lru_cache(maxsize=None)
def centroid(key: str | None) -> tuple[float, float] | None:
    """Search centre of a province (its capital) or of a region (the mean of its
    province capitals); None when unknown."""
    found = province(key) if key and len(key.strip()) == 2 else None
    if found is not None:
        return found.lat, found.lon
    region = key if key in REGION_NAMES else region_key(key)
    members = [p for p in _ALL if p.region == region]
    if not members:
        return None
    return (round(sum(p.lat for p in members) / len(members), 3),
            round(sum(p.lon for p in members) / len(members), 3))


def in_san_marino(lat: float, lon: float) -> bool:
    return 43.893 <= lat <= 43.993 and 12.403 <= lon <= 12.517


def in_italy_box(lat: float, lon: float) -> bool:
    """Rough box of Italy (also contains San Marino, the Vatican and border strips)."""
    return 35.4 <= lat <= 47.1 and 6.6 <= lon <= 18.6
