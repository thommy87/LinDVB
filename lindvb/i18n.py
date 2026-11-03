"""Translations of the user interface.

The catalogues are plain gettext ``.po`` files in ``lindvb/po`` (``de.po`` ...).
They are read directly, so no ``msgfmt`` step is needed when building or
running from the source tree. The language follows the desktop (LANGUAGE,
LC_ALL, LC_MESSAGES, LANG) unless Preferences > General sets one.

Use ``_("text")`` for strings and ``_("{n} channels").format(n=...)`` with
named placeholders, so translators can move them.
"""

import ast
import gettext
import locale
import os
from pathlib import Path

PO_DIR = Path(__file__).with_name("po")
# Preferences lists these when a catalogue exists ("" follows the desktop);
# the official EU languages, Arabic, Russian and Ukrainian
NATIVE_NAMES = {
    "": "System", "en": "English", "bg": "Български", "cs": "Čeština", "da": "Dansk",
    "de": "Deutsch", "el": "Ελληνικά", "es": "Español", "et": "Eesti", "fi": "Suomi",
    "fr": "Français", "ga": "Gaeilge", "hr": "Hrvatski", "hu": "Magyar", "it": "Italiano",
    "lt": "Lietuvių", "lv": "Latviešu", "mt": "Malti", "nl": "Nederlands", "pl": "Polski",
    "pt": "Português", "ro": "Română", "sk": "Slovenčina", "sl": "Slovenščina",
    "sv": "Svenska", "ar": "العربية", "ru": "Русский", "uk": "Українська",
}
RTL_LANGUAGES = {"ar"}
LANGUAGES = {code: name for code, name in NATIVE_NAMES.items()
             if code in ("", "en") or (PO_DIR / f"{code}.po").exists()}


def parse_po(text):
    """Return {msgid: msgstr} of a .po file (fuzzy and empty entries are left out)."""
    out = {}
    entry = {"flags": "", "id": None, "str": None}
    cur = None

    def flush():
        if entry["id"] and entry["str"] and "fuzzy" not in entry["flags"]:
            out[entry["id"]] = entry["str"]

    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#,") or \
                line.startswith(("msgctxt ", "msgid ")) and entry["str"] is not None:
            flush()
            entry = {"flags": "", "id": None, "str": None}
            cur = None
        if line.startswith("#,"):
            entry["flags"] = line
        elif line.startswith("msgctxt "):
            entry["ctx"], cur = ast.literal_eval(line[8:]), "ctx"
        elif line.startswith("msgid ") and entry.get("ctx") is not None:
            entry["id"], cur = entry["ctx"] + "\x04" + ast.literal_eval(line[6:]), "id"
        elif line.startswith("msgid "):
            entry["id"], cur = ast.literal_eval(line[6:]), "id"
        elif line.startswith("msgstr "):
            entry["str"], cur = ast.literal_eval(line[7:]), "str"
        elif line.startswith('"') and cur:
            entry[cur] += ast.literal_eval(line)
    flush()
    return out


class POTranslations(gettext.NullTranslations):
    def __init__(self, catalog):
        super().__init__()
        self.catalog = catalog

    def gettext(self, message):
        return self.catalog.get(message) or message


def desktop_languages():
    langs = []
    for var in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        val = os.environ.get(var)
        if val:
            langs += [v for v in val.split(":") if v]
            break
    if not langs:
        try:
            loc = locale.getlocale(locale.LC_MESSAGES)[0]
        except (AttributeError, ValueError):
            loc = None
        if loc:
            langs.append(loc)
    return langs


def pick_language(setting=""):
    """The catalogue to use: the setting, else the first desktop language we have."""
    for lang in ([setting] if setting else desktop_languages()):
        code = lang.split(".")[0].split("@")[0]
        for cand in (code, code.split("_")[0]):
            if cand == "en" or cand in ("C", "POSIX"):
                return "en"
            if (PO_DIR / f"{cand}.po").exists():
                return cand
    return "en"


_translation = None
current = "en"


def setup(language=""):
    """Load the catalogue; call once at start, before any window is built."""
    global _translation, current
    current = pick_language(language)
    po = PO_DIR / f"{current}.po"
    if current != "en" and po.exists():
        _translation = POTranslations(parse_po(po.read_text(encoding="utf-8")))
    else:
        _translation = gettext.NullTranslations()
    return current


def _configured_language():
    """The language from settings.json, read without building the whole Settings."""
    try:
        import json
        from .config import CONFIG_DIR
        return json.loads((CONFIG_DIR / "settings.json").read_text()).get("language", "")
    except Exception:
        return ""


def _(message):
    if _translation is None:
        # strings in module-level tables are translated at import time, which can be
        # before the application calls setup()
        setup(_configured_language())
    return _translation.gettext(message)


_lookup = _      # not spelled _(...) below, so xgettext does not take the key as a text


def C_(context, message):
    """Translate *message* in *context*, for a word that needs different
    translations in different places (msgctxt in the catalogues)."""
    key = f"{context}\x04{message}"
    found = _lookup(key)
    return message if found == key else found


def is_rtl():
    """True when the chosen language is written right to left (Arabic)."""
    return current in RTL_LANGUAGES


def N_(message):
    """Mark a string for translation without translating it (translate at use)."""
    return message
