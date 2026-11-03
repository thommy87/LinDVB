"""DVB-I settings: privacy notice, service list from the registry, postcode and
region (German DTVP profile, see lindvb.dvbi)."""

import threading
import urllib.parse

from gi.repository import GLib, Gtk

from .. import channels, dvbi
from .common import message
from ..i18n import _


def _check(label, active=False):
    """A check button whose label wraps, so long texts do not widen the page."""
    btn = Gtk.CheckButton(active=active)
    btn.set_child(Gtk.Label(label=label, xalign=0, wrap=True))
    return btn


class DVBIPanel(Gtk.Box):
    """The DVB-I section of Preferences › Internet. Its settings apply at once,
    the Save button of the preferences is not needed for them."""

    def __init__(self, app):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10,
                         css_classes=["lv-group"])
        self.app = app
        self.s = app.settings
        self.offers = []
        self.region_ids = []
        self._started = False
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_top=10,
                      margin_bottom=10)
        self.append(box)

        def title(text):
            box.append(Gtk.Label(label=text, xalign=0, css_classes=["heading"],
                                 margin_top=6))

        def hint(text):
            lbl = Gtk.Label(label=text, xalign=0, wrap=True, css_classes=["lv-hint"])
            box.append(lbl)
            return lbl

        hint(_("DVB-I gets the channel list of your region from the internet: numbering, "
             "names and logos, and for each channel the ways to receive it. LinDVB uses the "
             "antenna, cable or satellite channels from your channel search where it can and "
             "the internet stream otherwise."))

        # IT-17: privacy notice before any request
        title(_("Privacy"))
        notice = Gtk.Label(label=dvbi.PRIVACY_NOTICE, xalign=0, wrap=True)
        box.append(notice)
        self.ack = _check(label=_("I have read the privacy notice"),
                                   active=bool(self.s.get("dvbi_privacy_ack")))
        self.ack.connect("toggled", lambda *_: self._sensitive())
        box.append(self.ack)
        self.guide = _check(label=_("Load the programme guide (EPG) of the DVB-I channels"),
                                     active=bool(self.s.get("dvbi_guide", True)))
        self.guide.connect("toggled", lambda b: self._guide_toggled(b.get_active()))
        box.append(self.guide)
        failover = _check(
            label=_("Switch to another reception path (internet, antenna, cable, satellite) "
                  "when one fails"), active=bool(self.s.get("dvbi_failover", True)))
        failover.connect("toggled", lambda b: self._set("dvbi_failover", b.get_active()))
        box.append(failover)
        sat = _check(
            label=_("Tune satellite channels from the list without a channel search "
                  "(needs satellite channels in your list)"),
            active=bool(self.s.get("dvbi_sat_direct", True)))
        sat.connect("toggled", lambda b: self._set("dvbi_sat_direct", b.get_active()))
        box.append(sat)

        # IT-7/IT-8: lists from the national registry, default list first
        title(_("Service list"))
        row = Gtk.Box(spacing=8)
        self.load_btn = Gtk.Button(label=_("Load Lists from Registry"))
        self.load_btn.connect("clicked", lambda *_: self._load_registry())
        row.append(self.load_btn)
        self.spinner = Gtk.Spinner()
        row.append(self.spinner)
        box.append(row)
        self.lists = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE,
                                 css_classes=["boxed-list"])
        self.lists.connect("row-selected", lambda *_: self._sensitive())
        box.append(self.lists)
        current = self.s.get("dvbi_list_name") or self.s.get("dvbi_list_url")
        self.current_lbl = hint(_("In use: {current}").format(current=current) if current else
                                _("No DVB-I list is in use yet."))

        # IT-5: postcode for the region
        title(_("Region"))
        hint(_("Your postcode selects the regional channels and their numbering. It is sent "
             "to the service list server only."))
        grid = Gtk.Grid(column_spacing=12, row_spacing=8)
        grid.attach(Gtk.Label(label=_("Postcode"), xalign=0), 0, 0, 1, 1)
        self.postcode = Gtk.Entry(text=self.s.get("dvbi_postcode", ""), max_length=5,
                                  width_chars=8, input_purpose=Gtk.InputPurpose.DIGITS,
                                  placeholder_text="e.g. 80331")
        self.postcode.connect("changed", lambda *_: self._sensitive())
        grid.attach(self.postcode, 1, 0, 1, 1)
        self.nationwide = _check(label=_("No postcode: nationwide list"))
        self.nationwide.set_active(bool(self.s.get("dvbi_list_url")) and
                                   not self.s.get("dvbi_postcode"))
        self.nationwide.connect("toggled", lambda *_: self._sensitive())
        grid.attach(self.nationwide, 2, 0, 1, 1)
        self.region_lbl = Gtk.Label(label=_("Region"), xalign=0)
        self.region_dd = Gtk.DropDown.new_from_strings([])
        grid.attach(self.region_lbl, 0, 1, 1, 1)
        grid.attach(self.region_dd, 1, 1, 2, 1)
        box.append(grid)
        self._show_regions([])

        self.status = Gtk.Label(xalign=0, wrap=True, selectable=True, margin_top=6)
        box.append(self.status)

        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END, margin_top=8)
        self.remove_btn = Gtk.Button(label=_("Remove DVB-I Channels"))
        self.remove_btn.connect("clicked", lambda *_: self._remove())
        self.update_btn = Gtk.Button(label=_("Update Now"))
        self.update_btn.set_tooltip_text(_("Load the chosen list again (it is also checked daily)"))
        self.update_btn.connect("clicked", lambda *_: self._update())
        self.install_btn = Gtk.Button(label=_("Install Channel List"), css_classes=["suggested-action"])
        self.install_btn.connect("clicked", lambda *_: self._install())
        for b in (self.remove_btn, self.update_btn, self.install_btn):
            buttons.append(b)
        box.append(buttons)
        self._sensitive()

    def start(self):
        """Called when the Internet page is shown: ask the registry only then,
        not each time the preferences open."""
        if not self._started:
            self._started = True
            if self.ack.get_active():
                self._load_registry()

    # ------------------------------------------------------------------ state
    def _selected_offer(self):
        row = self.lists.get_selected_row()
        if row is not None and 0 <= row.get_index() < len(self.offers):
            return self.offers[row.get_index()]
        return None

    def _sensitive(self):
        ok = self.ack.get_active()
        self.load_btn.set_sensitive(ok)
        self.postcode.set_sensitive(ok and not self.nationwide.get_active())
        pc_ok = self.nationwide.get_active() or dvbi.valid_postcode(self.postcode.get_text())
        has_list = self._selected_offer() is not None or bool(self.s.get("dvbi_list_url"))
        self.install_btn.set_sensitive(ok and has_list and pc_ok and not self.app._dvbi_busy)
        configured = bool(self.s.get("dvbi_list_url"))
        self.update_btn.set_sensitive(ok and configured and not self.app._dvbi_busy)
        self.remove_btn.set_sensitive(bool(self.app.db.dvbi_channels()) or configured)

    def _show_regions(self, regions, selected=""):
        self.region_ids = [r.id for r in regions]
        self.region_dd.set_model(Gtk.StringList.new([r.name for r in regions]))
        if selected in self.region_ids:
            self.region_dd.set_selected(self.region_ids.index(selected))
        visible = len(regions) > 1
        self.region_lbl.set_visible(visible)
        self.region_dd.set_visible(visible)

    def _busy(self, on, text=""):
        if on:
            self.spinner.start()
        else:
            self.spinner.stop()
        if text:
            self.status.set_text(text)
        self._sensitive()

    # ------------------------------------------------------------------ registry
    def _load_registry(self):
        self.s["dvbi_privacy_ack"] = True
        self.load_btn.set_sensitive(False)
        self._busy(True, _("Asking the service list registry…"))
        registries = self.s.get("dvbi_registries") or list(dvbi.REGISTRIES)

        def work():
            try:
                offers, used = dvbi.query_registry(registries)
                GLib.idle_add(self._registry_done, offers, used, "")
            except dvbi.DVBIError as e:
                GLib.idle_add(self._registry_done, [], "", str(e))
        threading.Thread(target=work, daemon=True).start()

    def _registry_done(self, offers, used, error):
        self._busy(False)
        self.load_btn.set_sensitive(self.ack.get_active())
        if error:
            self.status.set_text(_("The registry could not be reached: {error}").format(error=error))
            return False
        self.offers = offers
        while (row := self.lists.get_row_at_index(0)) is not None:
            self.lists.remove(row)
        current = self.s.get("dvbi_list_url")
        select = None
        for i, o in enumerate(offers):
            r = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin_start=8,
                        margin_end=8, margin_top=6, margin_bottom=6)
            head = Gtk.Box(spacing=8)
            head.append(Gtk.Label(label=o.name, xalign=0, hexpand=True,
                                  css_classes=["heading"] if o.default else []))
            tags = []
            if o.default:
                tags.append(_("Default"))
            if o.official:
                tags.append("✓ official")          # IT-8: mark checked lists
            if tags:
                head.append(Gtk.Label(label=" · ".join(tags), css_classes=["lv-hint"]))
            r.append(head)
            sub = o.provider
            if o.delivery:
                sub += (" — " if sub else "") + ", ".join(d.upper() for d in o.delivery)
            if sub:
                r.append(Gtk.Label(label=sub, xalign=0, css_classes=["lv-hint"]))
            self.lists.append(r)
            if o.url == current or (select is None and not current and i == 0):
                select = i
        if select is not None:
            self.lists.select_row(self.lists.get_row_at_index(select))
        self.status.set_text(_("{v0} service lists from {netloc}").format(v0=len(offers), netloc=urllib.parse.urlsplit(used).netloc)
                             if offers else _("The registry offers no service list for Germany."))
        self._sensitive()
        return False

    # ------------------------------------------------------------------ install
    def _install(self, nationwide=None):
        if nationwide is not None:
            self.nationwide.set_active(nationwide)
        offer = self._selected_offer()
        postcode = "" if self.nationwide.get_active() else self.postcode.get_text().strip()
        if postcode and not dvbi.valid_postcode(postcode):
            message(self.get_root(), _("Please enter a postcode with five digits"))
            return
        region = None
        if self.region_dd.get_visible() and self.region_ids:
            region = self.region_ids[self.region_dd.get_selected()]
        s = self.s
        changed = offer is not None and offer.url != s.get("dvbi_list_url") or \
            postcode != s.get("dvbi_postcode", "")
        if offer is not None:
            s["dvbi_list_url"], s["dvbi_list_name"] = offer.url, offer.name
        if changed:
            # a new list or postcode: the first request goes without the
            # caching headers (IT-18), the region is chosen again
            s["dvbi_last_modified"] = s["dvbi_etag"] = ""
            s["dvbi_region"] = ""
            region = None
        s["dvbi_postcode"] = postcode
        s["dvbi_privacy_ack"] = True
        s.save()
        self._run(force=True, region=region)

    def _update(self):
        self._run(force=True, region=None)

    def _run(self, force, region):
        if self.app.update_dvbi(force=force, region=region, done=self._done):
            self._busy(True, _("Loading the service list…"))

    def _done(self, res, msg):
        self._busy(False, msg)
        self.current_lbl.set_text(_("In use: {v0}").format(v0=self.s.get('dvbi_list_name') or self.s.get('dvbi_list_url')))
        if res.error_code:
            self._ask_after_error(res.error_code)
            return
        if res.service_list is not None:
            cands = res.regions if len(res.regions) > 1 else []
            if cands:
                self.status.set_text(msg + _("\nChoose your region and press Install again."))
            self._show_regions(cands, res.region)

    def _ask_after_error(self, code):
        """IT-5: tell the user and offer to try again or the nationwide list."""
        if code == "ERROR_INVALID_POSTCODE":
            text, first = _("The service list does not know this postcode."), _("Enter Again")
        else:
            text, first = _("The service list server answered {code}.").format(code=code), _("Try Again")
        dlg = Gtk.AlertDialog(message=text, detail=_("You can try again or use the nationwide "
                              "list without a postcode."), buttons=[first, _("Nationwide List"), _("Cancel")],
                              cancel_button=2, default_button=0)

        def chosen(d, result):
            try:
                i = d.choose_finish(result)
            except GLib.Error:
                return
            if i == 0:
                if code == "ERROR_INVALID_POSTCODE":
                    self.postcode.grab_focus()
                else:
                    self._install()
            elif i == 1:
                self._install(nationwide=True)
        dlg.choose(self.get_root(), None, chosen)

    def _set(self, key, value):
        self.s[key] = value
        self.s.save()

    def _guide_toggled(self, on):
        self.s["dvbi_guide"] = on
        self.s.save()
        channels.DVBI_GUIDE_FIRST = on      # XMLTV again for these channels when off
        self.app.epg.notify()
        if on and self.s.get("dvbi_privacy_ack") and self.app.db.dvbi_channels():
            self.app.update_dvbi_guide()

    def _remove(self):
        removed = self.app.db.replace_dvbi([])[2]
        for k in ("dvbi_list_url", "dvbi_list_name", "dvbi_region", "dvbi_last_modified",
                  "dvbi_etag"):
            self.s[k] = ""
        self.s["dvbi_last_check"] = 0
        self.s.save()
        self.app.db.save()
        if self.app.window:
            self.app.window.reload_channels()
        self.current_lbl.set_text(_("No DVB-I list is in use."))
        self.status.set_text(_("{removed} DVB-I channels removed").format(removed=removed))
        self._sensitive()

