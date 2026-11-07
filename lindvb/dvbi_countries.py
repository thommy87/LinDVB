# SPDX-License-Identifier: GPL-2.0-or-later
"""DVB-I country profiles: what differs between the countries that run DVB-I.

DVB-I itself (ETSI TS 103 770) is the same everywhere, but each country adds a
profile on top: which service list registry the receiver asks, how the viewer's
region is found (postcode, region list), which consents the receiver asks for
before it goes online, the preferred languages and the youth protection steps.

* Germany follows the DTVP "DVB-I Implementierungsprofil" (postcode, privacy
  notice, IT-n requirements, see lindvb.dvbi).
* Italy follows HD Forum Italia's UHD Book 2.2 (section 7.3 Linear IP
  services, 7.4.1.3 DVB-I service discovery, 7.5.1.1 access to the DVB-I SLR)
  with the Italian DTT conventions: the region (regione, with the autonomous
  provinces of Trento and Bolzano on their own, as for local LCNs), an
  optional CAP, and separate, revocable consents in line with the GDPR and
  the Garante's guidance (nothing ticked in advance).
* New Zealand follows Freeview NextGen (DVB-I with HbbTV linked applications,
  broadcast and internet channels merged into one line-up): the Freeview
  region and an optional four-digit postcode.

The module has no GTK; the settings panel is lindvb.ui.dvbi.
"""

import re
from dataclasses import dataclass, field


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
    """One consent the viewer gives (or not) before LinDVB uses DVB-I.
    ``required``: DVB-I stays off without it. ``setting``: a LinDVB setting
    the consent switches (with ``invert`` when consent means False)."""
    key: str
    title: str
    text: str
    required: bool = False
    setting: str = ""
    invert: bool = False


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
    privacy_notice=(
        "Privacy information (art. 13 GDPR). To show the DVB-I channels LinDVB contacts the "
        "Italian service list registry, the server of the service list you choose, the "
        "servers that host the channel logos and, when the programme guide is switched on, "
        "the broadcasters' programme guide servers. They see your IP address and the time "
        "of the request. Your region and, only if you enter it, your CAP are used to pick "
        "the regional channels and their numbering; the CAP goes to the service list server "
        "only. LinDVB keeps the list, the logos and your choices only on this computer and "
        "sends nothing else. The broadcasters process the data of their streams and "
        "interactive services as data controllers under their own privacy policies. Each "
        "optional consent below can be withdrawn here at any time; without them DVB-I works "
        "all the same."),
    consents=(
        Consent("privacy", "I have read the privacy information", "", required=True),
        Consent("measurement", "Audience measurement",
                "Broadcasters' interactive services may measure which programmes are watched "
                "(audience research). Off: LinDVB asks the services not to track you "
                "(Do Not Track).", setting="hbbtv_do_not_track", invert=True),
        Consent("profiling", "Personalised advertising",
                "Interactive services may use third-party cookies to show advertising based "
                "on your viewing.", setting="hbbtv_third_party_cookies"),
    ),
    postcode_digits=5, postcode_example="00184", postcode_label="CAP",
    areas=ITALY_AREAS, area_label="Regione",
    # UHD Book: on an LCN conflict the best receivable variant is preferred,
    # so the terrestrial channel comes before the internet stream.
    prefer_broadcast=True,
    # Italian age classification: all, 6+, 14+, 18+
    age_limits=(0, 6, 14),
    notes=("The region selects the local channels (LCN 10-19 and 71-99 of your region) "
           "the way the terrestrial channel search does.",),
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
    privacy_notice=(
        "Freeview NextGen loads the channel list from the internet. LinDVB contacts the "
        "Freeview service list registry, the server of the service list, the servers that "
        "host the channel logos and, when the programme guide is switched on, the guide "
        "servers. They see your IP address and the time of the request. Your Freeview "
        "region and, only if you enter it, your postcode select the regional channels; the "
        "postcode goes to the service list server only. LinDVB keeps the list and logos on "
        "this computer and sends nothing else. Channels and the Freeview application are "
        "provided under the privacy policies of Freeview and the broadcasters (Privacy Act "
        "2020)."),
    consents=(
        Consent("privacy", "I have read the privacy notice", "", required=True),
        Consent("measurement", "Share anonymous viewing information",
                "The Freeview application and the broadcasters may count which channels are "
                "watched to improve the service. Off: LinDVB asks them not to track you.",
                setting="hbbtv_do_not_track", invert=True),
    ),
    postcode_digits=4, postcode_example="6011",
    areas=NZ_AREAS, area_label="Freeview region",
    prefer_broadcast=True,          # merged line-up: the broadcast copy first, no duplicates
    age_limits=(0, 13, 16),         # G / PGR / AO; R13, R16 for films
    notes=("Antenna, satellite and internet channels are merged into one list without "
           "duplicates; a channel you receive by antenna or dish uses that first.",),
)


COUNTRIES = (GERMANY, ITALY, NEW_ZEALAND)
DEFAULT = GERMANY.code


def get(code):
    """The profile for *code* (alpha-3), Germany when unknown."""
    code = (code or "").upper()
    return next((c for c in COUNTRIES if c.code == code), GERMANY)


def valid_postcode(country, text):
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
                "dvbi_area", "dvbi_consents", "dvbi_prefer_broadcast")


def fresh_state(country):
    return {"dvbi_list_url": "", "dvbi_list_name": "", "dvbi_postcode": "", "dvbi_region": "",
            "dvbi_last_modified": "", "dvbi_etag": "", "dvbi_privacy_ack": False,
            "dvbi_registries": list(country.registries), "dvbi_area": "", "dvbi_consents": {},
            "dvbi_prefer_broadcast": country.prefer_broadcast}


def switch_country(settings, code):
    """Make *code* the DVB-I country: keep the settings of the country in use
    and bring back those of *code* (fresh ones the first time). Also tells
    HbbTV applications the country. Returns the new Country."""
    old = get(settings.get("dvbi_country"))
    new = get(code)
    store = dict(settings.get("dvbi_country_state") or {})
    if old.code != new.code:
        store[old.code] = {k: settings.get(k) for k in COUNTRY_KEYS}
        state = fresh_state(new)
        state.update(store.get(new.code) or {})
        for k in COUNTRY_KEYS:
            settings[k] = state[k]
        settings["dvbi_last_check"] = 0
    settings["dvbi_country_state"] = store
    settings["dvbi_country"] = new.code
    settings["hbbtv_country"] = new.code
    # a consent never asked for counts as refused (nothing ticked in advance)
    given = settings.get("dvbi_consents") or {}
    for c in new.consents:
        if c.setting and c.key not in given:
            settings[c.setting] = bool(c.invert)
    return new


def set_consent(settings, country, key, given, now):
    """Record a consent (with the time, so it can be shown and withdrawn) and
    switch the setting it stands for."""
    consents = dict(settings.get("dvbi_consents") or {})
    consents[key] = {"given": bool(given), "time": int(now)}
    settings["dvbi_consents"] = consents
    c = next((c for c in country.consents if c.key == key), None)
    if c is not None:
        if c.key == "privacy":
            settings["dvbi_privacy_ack"] = bool(given)
        if c.setting:
            settings[c.setting] = (not given) if c.invert else bool(given)


def consent_given(settings, country, key):
    """Whether the consent stands. One that switches a setting follows that
    setting, so a change made elsewhere (Preferences › HbbTV) shows here."""
    if key == "privacy":
        return bool(settings.get("dvbi_privacy_ack"))
    c = next((c for c in country.consents if c.key == key), None)
    if c is not None and c.setting:
        return bool(settings.get(c.setting)) != c.invert
    return bool((settings.get("dvbi_consents") or {}).get(key, {}).get("given"))
