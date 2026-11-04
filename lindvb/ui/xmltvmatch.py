# SPDX-License-Identifier: GPL-2.0-or-later
"""Dialog to match IPTV channels to XMLTV guide channels."""

from gi.repository import Gtk

from .. import xmltvmatch
from .common import fill, make_column_view, scrolled, selected
from ..i18n import _

NONE_LABEL = "— no guide —"


class XMLTVMatchWindow(Gtk.Window):
    def __init__(self, app, parent):
        super().__init__(title=_("Match IPTV Channels to XMLTV"), transient_for=parent,
                         default_width=980, default_height=640)
        self.app = app
        self.guide = xmltvmatch.load_channels()
        self.matcher = xmltvmatch.Matcher(self.guide)
        self.chans = [c for c in app.db.channels if c.is_iptv]
        # pending choices: channel key -> [xmltv_id, manual, how]
        self.pending = {c.key: [c.xmltv_id, c.xmltv_manual,
                                "manual" if c.xmltv_manual else ("auto" if c.xmltv_id else "")]
                        for c in self.chans}

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_start=12,
                      margin_end=12, margin_top=12, margin_bottom=12)
        if not self.guide:
            box.append(Gtk.Label(
                label=_("No XMLTV guide loaded yet. Set the XMLTV URL in the EPG settings and "
                      "download the guide first; matching starts automatically after the download."),
                wrap=True, xalign=0))
            dl = Gtk.Button(label=_("Download XMLTV guide now"), halign=Gtk.Align.START)
            dl.connect("clicked", lambda *_: (self.app.update_xmltv(), self.close()))
            box.append(dl)
            self.set_child(box)
            return

        top = Gtk.Box(spacing=8)
        self.search = Gtk.SearchEntry(placeholder_text=_("Filter channels"), hexpand=True)
        self.search.connect("search-changed", lambda *_: self.refresh())
        self.only_open = Gtk.CheckButton(label=_("Only channels without guide"))
        self.only_open.connect("toggled", lambda *_: self.refresh())
        top.append(self.search)
        top.append(self.only_open)
        box.append(top)

        cols = [(_("Channel"), lambda c: c.name, True),
                ("tvg-id / tvg-name", lambda c: " / ".join(x for x in (c.tvg_id, c.tvg_name) if x), True),
                ("XMLTV channel", self._label, True),
                (_("Match"), lambda c: self.pending[c.key][2], False)]
        self.view, self.store, self.sel = make_column_view(cols)
        self.sel.connect("notify::selected-item", lambda *_: self._load(selected(self.sel)))
        box.append(scrolled(self.view))

        edit = Gtk.Box(spacing=8)
        edit.append(Gtk.Label(label=_("Guide channel:")))
        self._ids = [""] + sorted(self.guide, key=lambda i: self.matcher.label(i).lower())
        self.choice = Gtk.DropDown.new_from_strings(
            [NONE_LABEL] + [self.matcher.label(i) for i in self._ids[1:]])
        self.choice.set_enable_search(True)
        self.choice.set_expression(Gtk.PropertyExpression.new(Gtk.StringObject, None, "string"))
        self.choice.set_hexpand(True)
        edit.append(self.choice)
        apply = Gtk.Button(label=_("Assign"))
        apply.connect("clicked", lambda *_: self.on_assign())
        suggest = Gtk.Button(label=_("Suggest"))
        suggest.connect("clicked", lambda *_: self.on_suggest())
        edit.append(suggest)
        edit.append(apply)
        box.append(edit)

        self.summary = Gtk.Label(xalign=0, css_classes=["dim-label"])
        bar = Gtk.Box(spacing=6)
        auto = Gtk.Button(label=_("Auto-match all"))
        auto.set_tooltip_text(_("Match every channel that was not assigned by hand"))
        auto.connect("clicked", lambda *_: self.on_auto())
        reset = Gtk.Button(label=_("Reset to automatic"))
        reset.set_tooltip_text(_("Let the automatic matching decide for the selected channel again"))
        reset.connect("clicked", lambda *_: self.on_reset())
        bar.append(auto)
        bar.append(reset)
        bar.append(self.summary)
        self.summary.set_hexpand(True)
        cancel = Gtk.Button(label=_("Cancel"))
        cancel.connect("clicked", lambda *_: self.close())
        save = Gtk.Button(label=_("Save"), css_classes=["suggested-action"])
        save.connect("clicked", lambda *_: self.on_save())
        bar.append(cancel)
        bar.append(save)
        box.append(bar)
        self.set_child(box)
        self.refresh()

    # -- view ----------------------------------------------------------------------
    def _label(self, c):
        cid = self.pending[c.key][0]
        return self.matcher.label(cid) if cid else ""

    def refresh(self):
        text = self.search.get_text().lower()
        only = self.only_open.get_active()
        items = [c for c in self.chans
                 if (not text or text in c.name.lower()) and (not only or not self.pending[c.key][0])]
        fill(self.store, items)
        self._update_summary()

    def _update_summary(self):
        done = sum(1 for v in self.pending.values() if v[0])
        self.summary.set_text(_("{done} of {v1} channels have a guide channel").format(done=done, v1=len(self.pending)))

    def _changed(self, c):
        for i in range(self.store.get_n_items()):
            if self.store.get_item(i).data is c:
                self.store.items_changed(i, 1, 1)
                self.sel.set_selected(i)
                break
        self._update_summary()

    def _load(self, c):
        if c is None:
            return
        cid = self.pending[c.key][0]
        self.choice.set_selected(self._ids.index(cid) if cid in self._ids else 0)

    # -- actions -------------------------------------------------------------------
    def on_assign(self):
        c = selected(self.sel)
        if c is None:
            return
        cid = self._ids[self.choice.get_selected()]
        self.pending[c.key] = [cid, True, "manual"]
        self._changed(c)

    def on_suggest(self):
        c = selected(self.sel)
        if c is None:
            return
        cid, how, score = self.matcher.match(c.name, c.tvg_id, c.tvg_name)
        self.choice.set_selected(self._ids.index(cid) if cid in self._ids else 0)
        if not cid:
            self.summary.set_text(_("No suggestion for {name}").format(name=c.name))

    def on_reset(self):
        c = selected(self.sel)
        if c is None:
            return
        cid, how, score = self.matcher.match(c.name, c.tvg_id, c.tvg_name)
        self.pending[c.key] = [cid or "", False, self._how(how, score)]
        self._changed(c)
        self._load(c)

    def on_auto(self):
        for c in self.chans:
            if self.pending[c.key][1]:
                continue
            cid, how, score = self.matcher.match(c.name, c.tvg_id, c.tvg_name)
            self.pending[c.key] = [cid or "", False, self._how(how, score)]
        self.refresh()

    @staticmethod
    def _how(how, score):
        if not how:
            return ""
        return f"{how} {int(score * 100)}%" if how == "fuzzy" else how

    def on_save(self):
        for c in self.chans:
            cid, manual, _how = self.pending[c.key]
            c.xmltv_id, c.xmltv_manual = cid, manual
        self.app.db.save()
        self.app.epg.notify()
        self.close()
