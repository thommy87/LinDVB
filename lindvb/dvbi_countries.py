# SPDX-License-Identifier: GPL-2.0-or-later
"""DVB-I country profiles: what differs between the countries that run DVB-I.

DVB-I itself (ETSI TS 103 770) is the same everywhere, but each country adds a
profile on top: which service list registry the receiver asks, how the viewer's
region is found (postcode, region list), which consents the receiver asks for
before it goes online, the preferred languages and the youth protection steps.

* Germany follows the DTVP "DVB-I Implementierungsprofil" (postcode, privacy
  notice, IT-n requirements, see lindvb.dvbi).
* Italy follows HD Forum Italia's UHD Book 2.2: the Italian SLR is queried
  with TargetCountry=ITA and the RF delivery (7.5.1.1), the LCN table of the
  region (TargetRegion) and subscription package the viewer chose is used
  (7.5.1.2), services without an LCN go to the Main Overflow 850-999, the
  instances follow the list's precedence (7.3, TS 103 770 5.2.13), the
  automatic list update can be switched off with a warning (7.5, 7.5.1.5),
  youth protection has at least the 14 and 18 year steps (9.2.3). The region
  list (regione, with the autonomous provinces of Trento and Bolzano on their
  own) and the optional CAP select the TargetRegion. The UHD Book asks for no
  privacy notice or consent, so none is shown.
* New Zealand follows Freeview NextGen (DVB-I with HbbTV linked applications,
  broadcast and internet channels merged into one line-up): the Freeview
  region and an optional four-digit postcode. No notice or consent is
  specified.

The module has no GTK; the settings panel is lindvb.ui.dvbi.
"""

import os
import re
import unicodedata
from dataclasses import dataclass, field

from .i18n import N_, _


@dataclass(frozen=True)
class Area:
    """A region the viewer picks from a fixed list (Italian regione, Freeview
    region). ``prefixes`` are postcode beginnings that lie in it (used to
    suggest the area from a postcode); ``aliases`` help to find the area in a
    service list's own regions (region ids and names)."""
    id: str
    name: str
    prefixes: tuple = ()
    aliases: tuple = ()


@dataclass(frozen=True)
class Consent:
    """A confirmation the national profile asks for before DVB-I is used
    (Germany: the privacy notice, IT-17). ``required``: DVB-I stays off
    without it."""
    key: str
    title: str
    text: str
    required: bool = False


@dataclass(frozen=True)
class Country:
    code: str                         # ISO 3166-1 alpha-3, the registry's TargetCountry
    name: str
    profile: str                      # the national specification the settings follow
    registries: tuple
    languages: tuple                  # preferred xml:lang values for names and guides
    privacy_notice: str
    consents: tuple = ()
    postcode_digits: int = 5
    postcode_example: str = ""
    postcode_label: str = "Postcode"
    postcode_optional: bool = True    # the nationwide list is offered without one
    areas: tuple = ()                 # fixed region list ("" = none, the list's regions only)
    area_label: str = "Region"
    prefer_broadcast: bool = False    # default for "antenna/cable/satellite before internet"
    age_limits: tuple = (0, 6, 12, 16)
    overflow: tuple = None            # (first, last) numbers for services without an LCN
    notes: tuple = field(default=())  # short hints shown with the settings


# ---------------------------------------------------------------- Germany

GERMANY = Country(
    code="DEU", name="Germany",
    profile="DTVP DVB-I Implementierungsprofil V1.1.0 (TS 103 770 V1.2.1, IP2)",
    registries=("https://deu.csr.dvbservices.org/query", "https://csr.dvb-i-slr.de/query"),
    languages=("de", "deu", "ger", "en", "eng"),
    privacy_notice=(
        "DVB-I loads the channel list from the internet. To do so, LinDVB contacts the "
        "German service list registry (csr.dvb-i-slr.de), the server of the service list "
        "you choose, the servers that host the channel logos and, when the programme "
        "guide is switched on, the broadcasters' programme guide servers. These servers "
        "see your "
        "IP address and the time of the request. If you enter a postcode, it is sent to "
        "the service list server so that it can return the channels of your region. "
        "LinDVB sends no other personal data and keeps the list and logos only on this "
        "computer. Internet channels are streamed from the broadcasters' servers, which "
        "may log your requests according to their own privacy policies."),
    consents=(Consent("privacy", "I have read the privacy notice", "", required=True),),
    postcode_digits=5, postcode_example="80331",
    age_limits=(0, 6, 12, 16),
)


# ---------------------------------------------------------------- Italy

# Regions as used for local DTT channel numbering; Trentino-Alto Adige is two
# areas (the autonomous provinces). Prefixes: the first two CAP digits of the
# provinces (00-04 Lazio, 05-06 Umbria, ...); aliases: ISTAT region code and
# ISO 3166-2 code, so service list regions named either way are found.
ITALY_AREAS = (
    Area("IT-65", "Abruzzo", ("64", "65", "66", "67"), ("13", "abruzzo")),
    Area("IT-77", "Basilicata", ("75", "85"), ("17", "basilicata")),
    Area("IT-78", "Calabria", ("87", "88", "89"), ("18", "calabria")),
    Area("IT-72", "Campania", ("80", "81", "82", "83", "84"), ("15", "campania")),
    Area("IT-45", "Emilia-Romagna", ("29", "40", "41", "42", "43", "44", "47", "48"),
         ("08", "emilia", "romagna")),
    Area("IT-36", "Friuli-Venezia Giulia", ("33", "34"), ("06", "friuli", "venezia giulia")),
    Area("IT-62", "Lazio", ("00", "01", "02", "03", "04"), ("12", "lazio")),
    Area("IT-42", "Liguria", ("16", "17", "18", "19"), ("07", "liguria")),
    Area("IT-25", "Lombardia", ("20", "21", "22", "23", "24", "25", "26", "27", "46"),
         ("03", "lombardia", "lombardy")),
    Area("IT-57", "Marche", ("60", "61", "62", "63"), ("11", "marche")),
    Area("IT-67", "Molise", ("86",), ("14", "molise")),
    Area("IT-21", "Piemonte", ("10", "12", "13", "14", "15", "28"), ("01", "piemonte", "piedmont")),
    Area("IT-BZ", "Provincia autonoma di Bolzano", ("39",), ("21", "bolzano", "bozen", "südtirol",
                                                            "alto adige")),
    Area("IT-TN", "Provincia autonoma di Trento", ("38",), ("22", "trento", "trentino")),
    Area("IT-75", "Puglia", ("70", "71", "72", "73", "74", "76"), ("16", "puglia", "apulia")),
    Area("IT-88", "Sardegna", ("07", "08", "09"), ("20", "sardegna", "sardinia")),
    Area("IT-82", "Sicilia", ("90", "91", "92", "93", "94", "95", "96", "97", "98"),
         ("19", "sicilia", "sicily")),
    Area("IT-52", "Toscana", ("50", "51", "52", "53", "54", "55", "56", "57", "58", "59"),
         ("09", "toscana", "tuscany")),
    Area("IT-55", "Umbria", ("05", "06"), ("10", "umbria")),
    Area("IT-23", "Valle d'Aosta", ("11",), ("02", "valle d'aosta", "aosta", "vallée d'aoste")),
    Area("IT-34", "Veneto", ("30", "31", "32", "35", "36", "37", "45"), ("05", "veneto")),
)

ITALY = Country(
    code="ITA", name="Italy",
    profile="HD Forum Italia UHD Book 2.2 (DVB BlueBook A177r7, HbbTV 2.0.5)",
    # The national CSR (run for the Italian broadcasters, domain by CRTV) is
    # reached through the DVB service domain; the central DVB registry with
    # TargetCountry=ITA is the fallback. Both can be changed in the settings.
    registries=("https://ita.csr.dvbservices.org/query", "https://csr.dvbservices.org/query"),
    languages=("it", "ita", "en", "eng"),
    # The UHD Book asks for no privacy notice or consent before DVB-I is used
    privacy_notice="",
    postcode_digits=5, postcode_example="00184", postcode_label="CAP",
    areas=ITALY_AREAS, area_label="Regione",
    # UHD Book 7.3: service instance precedence as in TS 103 770 5.2.13, so
    # the list's priorities decide (the viewer can still prefer broadcast)
    prefer_broadcast=False,
    # UHD Book 9.2.3: at least the 14 and 18 year thresholds (the limit is
    # the youngest viewer's age: 13 asks the PIN from 14, 17 from 18)
    age_limits=(0, 13, 17),
    overflow=(850, 999),            # UHD Book 7.5.1.2: the Main Overflow
    notes=("The region selects the LCN table of your area, with the local channels, the way "
           "the terrestrial channel search does. Channels without a number go to 850-999.",),
)


# ---------------------------------------------------------------- New Zealand

# Freeview regions (regional bouquets) with the four-digit postcodes that lie
# in them; Central covers the Bay of Plenty, Hawke's Bay, Taranaki and Manawatū.
NZ_AREAS = (
    Area("NZ-AUK", "Auckland & Northland", tuple(f"{i:02d}" for i in range(1, 27)),
         ("auckland", "northland", "0x1000")),
    Area("NZ-WKO", "Waikato", ("32", "33", "34", "35", "36", "37", "38", "39"),
         ("waikato", "0x1100")),
    Area("NZ-CEN", "Central", ("30", "31", "40", "41", "42", "43", "44", "45", "46", "47", "48", "49"),
         ("central", "hawke", "taranaki", "manawatu", "bay of plenty", "0x1200")),
    Area("NZ-WGN", "Wellington", ("50", "51", "52", "53", "54", "55", "56", "57", "58", "59",
                                  "60", "61", "62", "63", "64", "65", "66", "67", "68", "69"),
         ("wellington", "0x1300")),
    Area("NZ-CAN", "Christchurch & upper South Island", tuple(str(i) for i in range(70, 90)),
         ("christchurch", "canterbury", "nelson", "marlborough", "0x1400")),
    Area("NZ-OTA", "Dunedin & lower South Island", tuple(str(i) for i in range(90, 100)),
         ("dunedin", "otago", "southland", "0x1500")),
)

NEW_ZEALAND = Country(
    code="NZL", name="New Zealand",
    profile="Freeview NextGen (DVB-I with HbbTV 2.0.4 Annex O)",
    registries=("https://nzl.csr.dvbservices.org/query", "https://csr.dvbservices.org/query"),
    languages=("en", "eng", "mi", "mri"),
    privacy_notice="",               # no notice or consent is specified
    postcode_digits=4, postcode_example="6011",
    areas=NZ_AREAS, area_label="Freeview region",
    prefer_broadcast=True,          # merged line-up: the broadcast copy first, no duplicates
    age_limits=(0, 13, 16),         # G / PGR / AO; R13, R16 for films
    notes=("Antenna, satellite and internet channels are merged into one list without "
           "duplicates; a channel you receive by antenna or dish uses that first.",),
)


# ---------------------------------------------------------------- any other country

# No national profile: the viewer enters a registry (CSR) or the address of a
# service list. "ZZZ" is a user-assigned ISO 3166 code; it is not sent to
# registries as TargetCountry and not given to HbbTV applications.
OTHER = Country(
    code="ZZZ", name="Other (own registry or list)",
    profile="DVB-I (ETSI TS 103 770) without a national profile",
    registries=(),
    languages=("en", "eng"),
    privacy_notice=(
        "DVB-I loads the channel list from the internet. To do so, LinDVB contacts the "
        "registry or service list server whose address you enter, the servers that host "
        "the channel logos and, when the programme guide is switched on, the broadcasters' "
        "programme guide servers. These servers see your IP address and the time of the "
        "request. If you enter a postcode, it is sent to the service list server."),
    consents=(Consent("privacy", "I have read the privacy notice", "", required=True),),
    postcode_digits=10, postcode_example="",
    notes=("Enter the address of a service list registry, or of a service list directly, "
           "below.",),
)

COUNTRIES = (GERMANY, ITALY, NEW_ZEALAND, OTHER)
DEFAULT = GERMANY.code

# The countries with DVB as their terrestrial television standard (the DVB
# Project's DVB-T/T2 deployment list), offered in the DVB-I country list and
# for the HbbTV country: (ISO 3166-1 alpha-2, alpha-3, English common name).
# Only those in COUNTRIES have a DVB-I profile; the others are shown greyed
# out. The names are translated through the catalogues (ISO 3166 names).
DVB_COUNTRIES = (
    ("AL", "ALB", N_("Albania")), ("AD", "AND", N_("Andorra")), ("AM", "ARM", N_("Armenia")),
    ("AT", "AUT", N_("Austria")), ("AZ", "AZE", N_("Azerbaijan")), ("BY", "BLR", N_("Belarus")),
    ("BE", "BEL", N_("Belgium")), ("BA", "BIH", N_("Bosnia and Herzegovina")),
    ("BG", "BGR", N_("Bulgaria")), ("HR", "HRV", N_("Croatia")), ("CY", "CYP", N_("Cyprus")),
    ("CZ", "CZE", N_("Czechia")), ("DK", "DNK", N_("Denmark")), ("EE", "EST", N_("Estonia")),
    ("FI", "FIN", N_("Finland")), ("FR", "FRA", N_("France")), ("GE", "GEO", N_("Georgia")),
    ("DE", "DEU", N_("Germany")), ("GR", "GRC", N_("Greece")), ("HU", "HUN", N_("Hungary")),
    ("IS", "ISL", N_("Iceland")), ("IE", "IRL", N_("Ireland")), ("IT", "ITA", N_("Italy")),
    ("LV", "LVA", N_("Latvia")), ("LI", "LIE", N_("Liechtenstein")),
    ("LT", "LTU", N_("Lithuania")), ("LU", "LUX", N_("Luxembourg")), ("MT", "MLT", N_("Malta")),
    ("MD", "MDA", N_("Moldova")), ("MC", "MCO", N_("Monaco")), ("ME", "MNE", N_("Montenegro")),
    ("NL", "NLD", N_("Netherlands")), ("MK", "MKD", N_("North Macedonia")),
    ("NO", "NOR", N_("Norway")), ("PL", "POL", N_("Poland")), ("PT", "PRT", N_("Portugal")),
    ("RO", "ROU", N_("Romania")), ("RU", "RUS", N_("Russia")), ("SM", "SMR", N_("San Marino")),
    ("RS", "SRB", N_("Serbia")), ("SK", "SVK", N_("Slovakia")), ("SI", "SVN", N_("Slovenia")),
    ("ES", "ESP", N_("Spain")), ("SE", "SWE", N_("Sweden")), ("CH", "CHE", N_("Switzerland")),
    ("TR", "TUR", N_("Türkiye")), ("UA", "UKR", N_("Ukraine")),
    ("GB", "GBR", N_("United Kingdom")), ("DZ", "DZA", N_("Algeria")),
    ("BH", "BHR", N_("Bahrain")), ("EG", "EGY", N_("Egypt")), ("IR", "IRN", N_("Iran")),
    ("IQ", "IRQ", N_("Iraq")), ("IL", "ISR", N_("Israel")), ("JO", "JOR", N_("Jordan")),
    ("KW", "KWT", N_("Kuwait")), ("LB", "LBN", N_("Lebanon")), ("LY", "LBY", N_("Libya")),
    ("MA", "MAR", N_("Morocco")), ("OM", "OMN", N_("Oman")),
    ("PS", "PSE", N_("Palestine, State of")), ("QA", "QAT", N_("Qatar")),
    ("SA", "SAU", N_("Saudi Arabia")), ("SY", "SYR", N_("Syria")), ("TN", "TUN", N_("Tunisia")),
    ("AE", "ARE", N_("United Arab Emirates")), ("YE", "YEM", N_("Yemen")),
    ("BJ", "BEN", N_("Benin")), ("BF", "BFA", N_("Burkina Faso")), ("BI", "BDI", N_("Burundi")),
    ("CM", "CMR", N_("Cameroon")), ("CV", "CPV", N_("Cabo Verde")),
    ("CF", "CAF", N_("Central African Republic")), ("TD", "TCD", N_("Chad")),
    ("KM", "COM", N_("Comoros")), ("CD", "COD", N_("Congo, The Democratic Republic of the")),
    ("CG", "COG", N_("Congo")), ("CI", "CIV", N_("Côte d'Ivoire")), ("DJ", "DJI", N_("Djibouti")),
    ("GQ", "GNQ", N_("Equatorial Guinea")), ("ER", "ERI", N_("Eritrea")),
    ("SZ", "SWZ", N_("Eswatini")), ("ET", "ETH", N_("Ethiopia")), ("GA", "GAB", N_("Gabon")),
    ("GM", "GMB", N_("Gambia")), ("GH", "GHA", N_("Ghana")), ("GN", "GIN", N_("Guinea")),
    ("GW", "GNB", N_("Guinea-Bissau")), ("KE", "KEN", N_("Kenya")), ("LS", "LSO", N_("Lesotho")),
    ("LR", "LBR", N_("Liberia")), ("MG", "MDG", N_("Madagascar")), ("MW", "MWI", N_("Malawi")),
    ("ML", "MLI", N_("Mali")), ("MR", "MRT", N_("Mauritania")), ("MU", "MUS", N_("Mauritius")),
    ("MZ", "MOZ", N_("Mozambique")), ("NA", "NAM", N_("Namibia")), ("NE", "NER", N_("Niger")),
    ("NG", "NGA", N_("Nigeria")), ("RW", "RWA", N_("Rwanda")),
    ("ST", "STP", N_("Sao Tome and Principe")), ("SN", "SEN", N_("Senegal")),
    ("SC", "SYC", N_("Seychelles")), ("SL", "SLE", N_("Sierra Leone")),
    ("SO", "SOM", N_("Somalia")), ("ZA", "ZAF", N_("South Africa")),
    ("SS", "SSD", N_("South Sudan")), ("SD", "SDN", N_("Sudan")), ("TZ", "TZA", N_("Tanzania")),
    ("TG", "TGO", N_("Togo")), ("UG", "UGA", N_("Uganda")), ("ZM", "ZMB", N_("Zambia")),
    ("ZW", "ZWE", N_("Zimbabwe")), ("AF", "AFG", N_("Afghanistan")),
    ("BD", "BGD", N_("Bangladesh")), ("KH", "KHM", N_("Cambodia")), ("IN", "IND", N_("India")),
    ("ID", "IDN", N_("Indonesia")), ("KZ", "KAZ", N_("Kazakhstan")),
    ("KG", "KGZ", N_("Kyrgyzstan")), ("MY", "MYS", N_("Malaysia")), ("MN", "MNG", N_("Mongolia")),
    ("MM", "MMR", N_("Myanmar")), ("NP", "NPL", N_("Nepal")), ("PK", "PAK", N_("Pakistan")),
    ("SG", "SGP", N_("Singapore")), ("TW", "TWN", N_("Taiwan")), ("TH", "THA", N_("Thailand")),
    ("TJ", "TJK", N_("Tajikistan")), ("TM", "TKM", N_("Turkmenistan")),
    ("UZ", "UZB", N_("Uzbekistan")), ("VN", "VNM", N_("Vietnam")),
    ("KP", "PRK", N_("North Korea")), ("BN", "BRN", N_("Brunei Darussalam")),
    ("AU", "AUS", N_("Australia")), ("NZ", "NZL", N_("New Zealand")), ("FJ", "FJI", N_("Fiji")),
    ("PG", "PNG", N_("Papua New Guinea")), ("CO", "COL", N_("Colombia")),
    ("PA", "PAN", N_("Panama")), ("GL", "GRL", N_("Greenland")),
)
_ALPHA3 = {a2: a3 for a2, a3, _n in DVB_COUNTRIES}
_NAMES = {a3: n for _a2, a3, n in DVB_COUNTRIES}


def get(code):
    """The profile for *code* (alpha-3), Germany when unknown."""
    code = (code or "").upper()
    return next((c for c in COUNTRIES if c.code == code), GERMANY)


def available(code):
    """Whether LinDVB has a DVB-I profile for *code* ("ZZZ": own registry)."""
    return any(c.code == (code or "").upper() for c in COUNTRIES)


def country_name(code):
    """The translated name of the country *code* (alpha-3)."""
    code = (code or "").upper()
    if code == OTHER.code:
        return _(OTHER.name)
    if code in _NAMES:
        return _(_NAMES[code])
    return code


def _sort_key(text):
    """Sort "Österreich" with "O", not after "Z"."""
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()


def country_choices():
    """[(code, translated name, has a DVB-I profile)] of all DVB countries
    in alphabetical order of the names, "Other (own registry or list)" last."""
    rows = sorted(((a3, country_name(a3), available(a3)) for _a2, a3, _n in DVB_COUNTRIES),
                  key=lambda r: _sort_key(r[1]))
    return rows + [(OTHER.code, country_name(OTHER.code), True)]


def system_country(environ=None, fallback="DEU"):
    """The country of the desktop's locale (de_AT.UTF-8 -> "AUT") when DVB is
    its standard, else *fallback*."""
    env = os.environ if environ is None else environ
    for var in ("LC_ALL", "LC_MESSAGES", "LANG", "LANGUAGE"):
        val = (env.get(var) or "").split(":")[0]
        if not val:
            continue
        region = val.split(".")[0].split("@")[0].partition("_")[2].upper()
        if region in _ALPHA3:
            return _ALPHA3[region]
        if region == "UK":
            return "GBR"
        if val not in ("C", "POSIX") and region:
            break
    return fallback


def valid_postcode(country, text):
    if country.code == OTHER.code:         # no national format to check
        return bool(re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z -]{1,9}", (text or "").strip()))
    return bool(re.fullmatch(r"\d{%d}" % country.postcode_digits, (text or "").strip()))


def area(country, area_id):
    return next((a for a in country.areas if a.id == area_id), None)


def area_for_postcode(country, postcode):
    """The fixed area a postcode lies in (longest prefix wins), or None."""
    pc = (postcode or "").strip()
    best, length = None, 0
    for a in country.areas:
        for p in a.prefixes:
            if pc.startswith(p) and len(p) > length:
                best, length = a, len(p)
    return best


def _norm(text):
    return re.sub(r"[^0-9a-zà-ÿ']+", " ", (text or "").lower()).strip()


def match_area(regions, a):
    """The service list regions (lindvb.dvbi.Region) that stand for the fixed
    area *a*: same id, an alias as the whole id, or the name / an alias in the
    region name. Selectable regions only, most specific first."""
    if a is None:
        return []
    keys = [_norm(a.name)] + [_norm(x) for x in a.aliases]
    ids = {a.id.lower()} | {x.lower() for x in a.aliases}
    hits = []
    for r in regions:
        if not r.selectable:
            continue
        rid, name = (r.id or "").lower(), _norm(r.name)
        tail = re.split(r"[:/#.\-]", rid)[-1] if rid else ""
        if rid in ids or tail in ids or \
                any(k and not k.isdigit() and (k == name or f" {k} " in f" {name} ")
                    for k in keys):
            hits.append(r)
    return sorted(hits, key=lambda r: -r.depth)


# ---------------------------------------------------------------- per-country settings

# Settings that belong to the country in use. They live as the plain dvbi_*
# keys for the country in use; the others are kept in "dvbi_country_state"
# and come back when the viewer switches to that country again.
COUNTRY_KEYS = ("dvbi_list_url", "dvbi_list_name", "dvbi_postcode", "dvbi_region",
                "dvbi_last_modified", "dvbi_etag", "dvbi_privacy_ack", "dvbi_registries",
                "dvbi_area", "dvbi_consents", "dvbi_prefer_broadcast", "dvbi_package")


def fresh_state(country):
    return {"dvbi_list_url": "", "dvbi_list_name": "", "dvbi_postcode": "", "dvbi_region": "",
            "dvbi_last_modified": "", "dvbi_etag": "", "dvbi_privacy_ack": False,
            "dvbi_registries": list(country.registries), "dvbi_area": "", "dvbi_consents": {},
            "dvbi_prefer_broadcast": country.prefer_broadcast, "dvbi_package": ""}


def switch_country(settings, code):
    """Make *code* the DVB-I country: keep the settings of the country in use
    and bring back those of *code* (fresh ones the first time). Returns the
    new Country. The HbbTV country is a setting of its own (Preferences ›
    HbbTV); a DVB-I channel uses the country of its list while it plays
    (lindvb.hbbtv.country_for)."""
    old = get(settings.get("dvbi_country"))
    new = get(code)
    store = dict(settings.get("dvbi_country_state") or {})
    if old.code != new.code:
        base = fresh_state(old)
        store[old.code] = {k: settings.get(k, base[k]) for k in COUNTRY_KEYS}
        state = fresh_state(new)
        state.update(store.get(new.code) or {})
        for k in COUNTRY_KEYS:
            settings[k] = state[k]
        settings["dvbi_last_check"] = 0
    settings["dvbi_country_state"] = store
    settings["dvbi_country"] = new.code
    return new


def installed_country(settings, channels=()):
    """The country of the DVB-I list in the channel list: the one its
    channels name; for a list from before 0.23 (channels without a country)
    the DVB-I country, unless that has no list chosen and exactly one other
    country has (the list stayed when the country was changed, as 0.22 did).
    "" when the list has no country (own registry or list)."""
    named = {c.dvbi_country for c in channels if c.dvbi_id and c.dvbi_country}
    if len(named) == 1:
        return named.pop()
    code = (settings.get("dvbi_country") or "").upper()
    if not settings.get("dvbi_list_url"):
        others = [k for k, st in (settings.get("dvbi_country_state") or {}).items()
                  if k != code and (st or {}).get("dvbi_list_url")]
        if len(others) == 1:
            code = others[0]
    return "" if code == OTHER.code else code


def label_old_channels(settings, channels):
    """Give the DVB-I channels of a list from before 0.23 the country of their
    list (installed_country): the HbbTV country and youth protection follow
    it. Returns how many were labelled."""
    old = [c for c in channels if c.dvbi_id and not c.dvbi_country]
    code = installed_country(settings, old) if old else ""
    if not code:
        return 0
    for c in old:
        c.dvbi_country = code
    return len(old)


def set_consent(settings, country, key, given, now):
    """Record a confirmation with its time."""
    consents = dict(settings.get("dvbi_consents") or {})
    consents[key] = {"given": bool(given), "time": int(now)}
    settings["dvbi_consents"] = consents
    if key == "privacy":
        settings["dvbi_privacy_ack"] = bool(given)


def consent_given(settings, country, key):
    if key == "privacy":
        return bool(settings.get("dvbi_privacy_ack"))
    return bool((settings.get("dvbi_consents") or {}).get(key, {}).get("given"))


def needs_ack(country):
    """Whether DVB-I waits for a confirmation the profile asks for."""
    return any(c.required for c in country.consents)
