"""Translations: the .po reader, language choice and the catalogues themselves."""

import re

import pytest

from lindvb import i18n

PLACEHOLDER = re.compile(r"(?<!\{)\{[^{}]*\}(?!\})")


def test_parse_po_skips_fuzzy_and_empty():
    po = ('msgid ""\nmsgstr "Content-Type: text/plain; charset=UTF-8\\n"\n\n'
          'msgid "Save"\nmsgstr "Speichern"\n\n'
          '#, fuzzy\nmsgid "Close"\nmsgstr "Zu"\n\n'
          'msgid "Open"\nmsgstr ""\n\n'
          'msgid ""\n"Two "\n"lines"\nmsgstr ""\n"Zwei "\n"Zeilen"\n')
    cat = i18n.parse_po(po)
    assert cat == {"Save": "Speichern", "Two lines": "Zwei Zeilen"}


def test_pick_language(monkeypatch):
    for var in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("LANG", "xx_YY.UTF-8")
    assert i18n.pick_language() == "en"
    assert i18n.pick_language("en") == "en"
    for code in i18n.LANGUAGES:
        if code not in ("", "en"):
            monkeypatch.setenv("LANG", f"{code}_XX.UTF-8")
            assert i18n.pick_language() == code
            assert i18n.pick_language(code) == code
    if "zh_TW" in i18n.LANGUAGES:
        # Hong Kong and Macau write traditional characters too
        assert i18n.pick_language("zh_HK.UTF-8") == "zh_TW"
        assert i18n.pick_language("zh_MO") == "zh_TW"


def test_setup_translates_and_falls_back():
    try:
        langs = [c for c in i18n.LANGUAGES if c not in ("", "en")]
        if not langs:
            pytest.skip("no catalogues")
        i18n.setup(langs[0])
        cat = i18n.parse_po((i18n.PO_DIR / f"{langs[0]}.po").read_text(encoding="utf-8"))
        msgid, msgstr = next(iter(cat.items()))
        assert i18n._(msgid) == msgstr
        assert i18n._("A text nobody translated") == "A text nobody translated"
    finally:
        i18n.setup("en")
    assert i18n._("Save") == "Save"


@pytest.mark.parametrize("po", sorted(i18n.PO_DIR.glob("*.po")), ids=lambda p: p.stem)
def test_catalogue_keeps_placeholders(po):
    """A translation must keep every {placeholder}, line breaks at the ends and the
    mnemonic underscore, or the text breaks at run time."""
    for msgid, msgstr in i18n.parse_po(po.read_text(encoding="utf-8")).items():
        assert sorted(PLACEHOLDER.findall(msgid)) == sorted(PLACEHOLDER.findall(msgstr)), \
            (po.stem, msgid, msgstr)
        msgstr.format(**{m[1:-1].split(":")[0].split("!")[0]: 0
                         for m in PLACEHOLDER.findall(msgid)})
        assert msgid.startswith("\n") == msgstr.startswith("\n"), (po.stem, msgid)
        assert msgid.endswith("\n") == msgstr.endswith("\n"), (po.stem, msgid)
        if re.search(r"(^|[^ ])_[A-Za-z]", msgid):
            assert "_" in msgstr, (po.stem, msgid, msgstr)


def test_every_catalogue_is_complete():
    """Each catalogue translates (nearly) every text of lindvb.pot."""
    pot = (i18n.PO_DIR / "lindvb.pot").read_text(encoding="utf-8")
    total = len(re.findall(r"^msgid ", pot, re.M)) - 1          # minus the header
    for po in sorted(i18n.PO_DIR.glob("*.po")):
        done = len(i18n.parse_po(po.read_text(encoding="utf-8")))
        assert done >= total * 0.95, f"{po.stem}: {done} of {total} translated"


def test_context_keeps_words_apart():
    """msgctxt entries: "Recording" as a preferences page is not the timer status."""
    po = ('msgid "Recording"\nmsgstr "Aufnahme läuft"\n\n'
          '#, fuzzy\nmsgctxt "x"\nmsgid "Old"\nmsgstr "Alt"\n\n'
          'msgctxt "preferences page"\nmsgid "Recording"\nmsgstr "Aufnahme"\n')
    cat = i18n.parse_po(po)
    assert cat == {"Recording": "Aufnahme läuft", "preferences page\x04Recording": "Aufnahme"}
    try:
        if "de" in i18n.LANGUAGES:
            i18n.setup("de")
            assert i18n.C_("preferences page", "Recording") == "Aufnahme"
            assert i18n._("Recording") == "Aufnahme läuft"
        i18n.setup("en")
        assert i18n.C_("preferences page", "Recording") == "Recording"
    finally:
        i18n.setup("en")
