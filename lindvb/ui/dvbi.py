# SPDX-License-Identifier: GPL-2.0-or-later
"""DVB-I settings: the country, its privacy notice where the profile asks for one, the service
list from the registry, postcode and region (see lindvb.dvbi and
lindvb.dvbi_countries)."""

import threading
import time
import urllib.parse

from gi.repository import GLib, Gtk

from .. import channels, dvbi
from .. import dvbi_countries as countries
from .common import message
from ..i18n import _

# Country names, consent and region labels live in lindvb.dvbi_countries in
# English; these markers keep them in the translation catalogue.
_N_ = (_("Germany"), _("Italy"), _("New Zealand"), _("Postcode"), _("CAP"), _("Region"),
       _("Regione"), _("Freeview region"))


def _check(label, active=False):
    """A check button whose label wraps, so long texts do not widen the page."""
    btn = Gtk.CheckButton(active=active)
    btn.set_child(Gtk.Label(label=label, xalign=0, wrap=True))
    return btn


def _renew(box):
    """Put an empty copy of ``box`` in its place and return it.

    Removing and adding children of a box that stays in place can leave GTK with
    the box's old height (seen on Wayland after a country change): a long
    wrapped text then runs over the widgets below it. A new box is measured
    from scratch."""
    new = Gtk.Box(orientation=box.get_orientation(), spacing=box.get_spacing())
    parent = box.get_parent()
    parent.insert_child_after(new, box)
    parent.remove(box)
    return new


class DVBIPanel(Gtk.Box):
    """The DVB-I section of Preferences › Internet. Its settings apply at once,
    the Save button of the preferences is not needed for them."""

    def __init__(self, app):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10,
                         css_classes=["lv-group"])
        self.app = app
        self.s = app.settings
        self.country = countries.get(self.s.get("dvbi_country"))
        self.offers = []
        self.region_ids = []
        self.consent_checks = {}
        self.ack = None
        self.postcode = self.nationwide = self.area_dd = None
        self._started = False
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_top=10,
                      margin_bottom=10)
        self.append(box)
        self.box = box

        self.hint(box, _("DVB-I gets the channel list of your region from the internet: "
                         "numbering, names and logos, and for each channel the ways to receive "
                         "it. LinDVB uses the antenna, cable or satellite channels from your "
                         "channel search where it can and the internet stream otherwise."))

        # the country decides registry, consents and how the region is found
        self.title(box, _("Country"))
        row = Gtk.Box(spacing=12)
        row.append(Gtk.Label(label=_("Country"), xalign=0))
        self.country_dd = Gtk.DropDown.new_from_strings([_(c.name) for c in countries.COUNTRIES])
        self.country_dd.set_selected([c.code for c in countries.COUNTRIES].index(self.country.code))
        self.country_dd.connect("notify::selected", lambda *_: self._country_changed())
        row.append(self.country_dd)
        box.append(row)
        self.profile_lbl = self.hint(box, "")

        # country part 1: privacy notice and consents (IT-17: before any request)
        self.privacy_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.append(self.privacy_box)

        self.title(box, _("Options"))
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
        self.prefer = _check(
            label=_("Prefer antenna, cable and satellite to the internet stream when a channel "
                    "has both"))
        self.prefer.connect("toggled", lambda b: self._set("dvbi_prefer_broadcast",
                                                          b.get_active()))
        box.append(self.prefer)
        # UHD Book 7.5 / 7.5.1.5: the viewer may switch the automatic update
        # off, with a warning
        self.auto = _check(label=_("Update the channel list automatically (daily)"),
                           active=bool(self.s.get("dvbi_auto_update", True)))
        self.auto.connect("toggled", lambda b: self._auto_toggled(b.get_active()))
        box.append(self.auto)
        self.auto_warn = self.hint(box, _("Warning! With this switched off, LinDVB can no "
                                          "longer keep your channel list up to date with the "
                                          "channels on air."))
        self.auto_warn.set_margin_start(28)
        self.auto_warn.set_visible(not self.auto.get_active())

        # IT-7/IT-8: lists from the national registry, default list first
        self.title(box, _("Service list"))
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
        self.current_lbl = self.hint(box, "")
        exp = Gtk.Expander(label=_("Registry address"))
        ebox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_top=6)
        self.registry_entry = Gtk.Entry(hexpand=True)
        self.registry_entry.connect("activate", lambda *_: self._registry_edited())
        ebox.append(self.registry_entry)
        self.hint(ebox, _("Asked in this order, separated by spaces. Press Enter to keep a "
                          "change; empty brings back the country's registries."))
        # a service list without a registry (any country, e.g. one a broadcaster
        # or a community publishes)
        ebox.append(Gtk.Label(label=_("Service list address"), xalign=0, margin_top=8))
        lrow = Gtk.Box(spacing=8)
        self.list_entry = Gtk.Entry(hexpand=True, placeholder_text="https://…/servicelist.xml")
        self.list_entry.connect("activate", lambda *_: self._use_list_url())
        lrow.append(self.list_entry)
        use = Gtk.Button(label=_("Use This List"))
        use.connect("clicked", lambda *_: self._use_list_url())
        lrow.append(use)
        ebox.append(lrow)
        self.hint(ebox, _("Loads this service list directly, without asking a registry."))
        exp.set_child(ebox)
        self.registry_exp = exp
        box.append(exp)

        # country part 2: postcode and fixed region list (IT-5)
        self.title(box, _("Region"))
        self.region_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.append(self.region_box)
        grid = Gtk.Grid(column_spacing=12, row_spacing=8)
        self.region_lbl = Gtk.Label(label=_("Region in the list"), xalign=0)
        self.region_dd = Gtk.DropDown.new_from_strings([])
        grid.attach(self.region_lbl, 0, 0, 1, 1)
        grid.attach(self.region_dd, 1, 0, 2, 1)
        # subscription packages of the list (UHD Book 7.5.1.2, IP2)
        self.package_lbl = Gtk.Label(label=_("Package"), xalign=0)
        self.package_dd = Gtk.DropDown.new_from_strings([])
        self.package_dd.connect("notify::selected", lambda *_: self._package_changed())
        self.package_names = []
        grid.attach(self.package_lbl, 0, 1, 1, 1)
        grid.attach(self.package_dd, 1, 1, 2, 1)
        box.append(grid)
        self.notes_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.append(self.notes_box)

        self.status = Gtk.Label(xalign=0, wrap=True, selectable=True, margin_top=6)
        box.append(self.status)

        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END, margin_top=8)
        self.remove_btn = Gtk.Button(label=_("Remove DVB-I Channels"))
        self.remove_btn.connect("clicked", lambda *_: self._remove())
        self.update_btn = Gtk.Button(label=_("Update Now"))
        self.update_btn.set_tooltip_text(_("Load the chosen list again (it is also checked daily)"))
        self.update_btn.connect("clicked", lambda *_: self._update())
        self.install_btn = Gtk.Button(label=_("Install Channel List"),
                                      css_classes=["suggested-action"])
        self.install_btn.connect("clicked", lambda *_: self._install())
        for b in (self.remove_btn, self.update_btn, self.install_btn):
            buttons.append(b)
        box.append(buttons)
        self._build_country()

    # ------------------------------------------------------------------ widgets
    @staticmethod
    def title(box, text):
        box.append(Gtk.Label(label=text, xalign=0, css_classes=["heading"], margin_top=6))

    @staticmethod
    def hint(box, text):
        lbl = Gtk.Label(label=text, xalign=0, wrap=True, css_classes=["lv-hint"])
        box.append(lbl)
        return lbl

    def _build_country(self):
        """(Re)build what depends on the country: notice, consents, postcode,
        region list, notes, registry and the list in use."""
        c, s = self.country, self.s
        self.profile_lbl.set_text(_("Settings follow: {profile}").format(profile=c.profile))

        # only what the country's profile asks for (Germany: IT-17)
        self.privacy_box = _renew(self.privacy_box)
        self.privacy_box.set_visible(bool(c.privacy_notice or c.consents))
        if c.privacy_notice:
            self.title(self.privacy_box, _("Privacy"))
            self.privacy_box.append(Gtk.Label(label=_(c.privacy_notice), xalign=0, wrap=True))
        self.consent_checks = {}
        for consent in c.consents:
            chk = _check(label=_(consent.title),
                         active=countries.consent_given(s, c, consent.key))
            chk.connect("toggled", lambda b, k=consent.key: self._consent_toggled(k, b))
            self.privacy_box.append(chk)
            if consent.text:
                lbl = self.hint(self.privacy_box, _(consent.text))
                lbl.set_margin_start(28)
            self.consent_checks[consent.key] = chk
        self.ack = self.consent_checks.get("privacy")

        self.region_box = _renew(self.region_box)
        grid = Gtk.Grid(column_spacing=12, row_spacing=8)
        self.region_box.append(grid)
        y = 0
        self.area_dd = None
        if c.areas:
            self.hint(self.region_box, _("Your region selects the regional channels and "
                                         "their numbering."))
            self.region_box.reorder_child_after(self.region_box.get_last_child(), None)
            grid.attach(Gtk.Label(label=_(c.area_label), xalign=0), 0, y, 1, 1)
            names = [_("Choose…")] + [a.name for a in c.areas]
            self.area_dd = Gtk.DropDown.new_from_strings(names)
            ids = [a.id for a in c.areas]
            cur = s.get("dvbi_area", "")
            self.area_dd.set_selected(ids.index(cur) + 1 if cur in ids else 0)
            self.area_dd.connect("notify::selected", lambda *_: self._area_changed())
            grid.attach(self.area_dd, 1, y, 2, 1)
            y += 1
        pc_hint = (_("Optional. Your {label} is sent to the service list server only.")
                   if c.areas else
                   _("Your postcode selects the regional channels and their numbering. It is "
                     "sent to the service list server only.")).format(label=_(c.postcode_label))
        grid.attach(Gtk.Label(label=_(c.postcode_label), xalign=0), 0, y, 1, 1)
        self.postcode = Gtk.Entry(text=s.get("dvbi_postcode", ""), max_length=c.postcode_digits,
                                  width_chars=8, input_purpose=Gtk.InputPurpose.DIGITS,
                                  placeholder_text=_("e.g. {example}").format(
                                      example=c.postcode_example))
        self.postcode.connect("changed", lambda *_: self._postcode_changed())
        grid.attach(self.postcode, 1, y, 1, 1)
        self.nationwide = _check(label=_("No postcode: nationwide list"))
        self.nationwide.set_active(bool(s.get("dvbi_list_url")) and not s.get("dvbi_postcode")
                                   and not c.areas)
        self.nationwide.connect("toggled", lambda *_: self._sensitive())
        self.nationwide.set_visible(not c.areas)
        grid.attach(self.nationwide, 2, y, 1, 1)
        self.hint(self.region_box, pc_hint)

        self.notes_box = _renew(self.notes_box)
        for note in c.notes:
            self.hint(self.notes_box, _(note))

        self.prefer.set_active(bool(s.get("dvbi_prefer_broadcast", c.prefer_broadcast)))
        self.registry_entry.set_text(" ".join(s.get("dvbi_registries") or c.registries))
        self.list_entry.set_text("")
        if not c.registries:
            self.registry_exp.set_expanded(True)
        self._clear_lists()
        current = s.get("dvbi_list_name") or s.get("dvbi_list_url")
        self.current_lbl.set_text(_("In use: {current}").format(current=current) if current
                                  else _("No DVB-I list is in use yet."))
        self._show_regions([])
        self._show_packages([])
        self._sensitive()

    def _show_packages(self, names):
        self.package_names = []
        self.package_dd.set_model(Gtk.StringList.new([_("Free channels only")] + list(names)))
        cur = self.s.get("dvbi_package", "")
        self.package_dd.set_selected(list(names).index(cur) + 1 if cur in names else 0)
        self.package_names = list(names)
        self.package_lbl.set_visible(bool(names))
        self.package_dd.set_visible(bool(names))

    def _package_changed(self):
        if not self.package_names and self.package_dd.get_selected() == 0:
            return                       # the model is being filled
        i = self.package_dd.get_selected()
        new = self.package_names[i - 1] if 0 < i <= len(self.package_names) else ""
        if new != self.s.get("dvbi_package", ""):
            self.s["dvbi_package"] = new
            self.s.save()
            self.status.set_text(_("Press Install again to use the channel numbers of this "
                                   "package."))

    def _auto_toggled(self, on):
        self._set("dvbi_auto_update", on)
        self.auto_warn.set_visible(not on)

    # ------------------------------------------------------------------ country
    def _country_changed(self):
        code = countries.COUNTRIES[self.country_dd.get_selected()].code
        if code == self.country.code:
            return
        had = bool(self.app.db.dvbi_channels())
        self.country = countries.switch_country(self.s, code)
        dvbi.use_country(code)
        self.s.save()
        self._build_country()
        self.status.set_text(
            _("The DVB-I channels of the previous country stay until you install a list for "
              "{country} or remove them.").format(country=_(self.country.name)) if had else "")
        if self._ack_ok():
            self._load_registry()

    def _consent_toggled(self, key, btn):
        countries.set_consent(self.s, self.country, key, btn.get_active(), time.time())
        self.s.save()
        self._sensitive()

    def _area_changed(self):
        i = self.area_dd.get_selected()
        new = self.country.areas[i - 1].id if i > 0 else ""
        if new != self.s.get("dvbi_area", ""):
            self.s["dvbi_area"] = new
            self.s["dvbi_region"] = ""        # the list's region is chosen again
            self.s.save()
        self._sensitive()

    def _postcode_changed(self):
        """Suggest the fixed region from the postcode while it is still open."""
        pc = self.postcode.get_text().strip()
        if self.area_dd is not None and self.area_dd.get_selected() == 0 and \
                countries.valid_postcode(self.country, pc):
            a = countries.area_for_postcode(self.country, pc)
            if a is not None:
                self.area_dd.set_selected(self.country.areas.index(a) + 1)
        self._sensitive()

    def _registry_edited(self):
        urls = [u for u in self.registry_entry.get_text().split()
                if u.startswith(("http://", "https://"))]
        self.s["dvbi_registries"] = urls or list(self.country.registries)
        self.s.save()
        self.registry_entry.set_text(" ".join(self.s["dvbi_registries"]))
        if self._ack_ok():
            self._load_registry()

    def _use_list_url(self):
        url = self.list_entry.get_text().strip()
        if not url:
            return
        if "://" not in url:
            url = "https://" + url
        if not url.startswith(("http://", "https://")):
            self.status.set_text(_("Please enter a web address (https://…)."))
            return
        if not self._ack_ok():
            self.status.set_text(_("Please confirm the privacy notice first."))
            return
        s = self.s
        s["dvbi_list_url"] = url
        s["dvbi_list_name"] = urllib.parse.urlsplit(url).netloc or url
        s["dvbi_last_modified"] = s["dvbi_etag"] = s["dvbi_region"] = ""
        s["dvbi_privacy_ack"] = True
        s.save()
        self.lists.unselect_all()
        self._run(force=True, region=None)

    def start(self):
        """Called when the Internet page is shown: ask the registry only then,
        not each time the preferences open."""
        if not self._started:
            self._started = True
            if self._ack_ok():
                self._load_registry()

    # ------------------------------------------------------------------ state
    def _selected_offer(self):
        row = self.lists.get_selected_row()
        if row is not None and 0 <= row.get_index() < len(self.offers):
            return self.offers[row.get_index()]
        return None

    def _region_ok(self):
        """A postcode, a region from the fixed list, or the nationwide list."""
        pc = self.postcode.get_text().strip()
        if pc:
            return countries.valid_postcode(self.country, pc)
        if self.area_dd is not None:
            return self.area_dd.get_selected() > 0
        return self.nationwide.get_active()

    def _ack_ok(self):
        """The confirmation the profile asks for is given (or none is asked)."""
        return self.ack is None or self.ack.get_active()

    def _sensitive(self):
        ok = self._ack_ok()
        self.load_btn.set_sensitive(ok)
        self.postcode.set_sensitive(ok and not self.nationwide.get_active())
        if self.area_dd is not None:
            self.area_dd.set_sensitive(ok)
        has_list = self._selected_offer() is not None or bool(self.s.get("dvbi_list_url"))
        busy = self.app._dvbi_busy
        self.install_btn.set_sensitive(ok and has_list and self._region_ok() and not busy)
        configured = bool(self.s.get("dvbi_list_url"))
        self.update_btn.set_sensitive(ok and configured and not busy)
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

    def _clear_lists(self):
        self.offers = []
        while (row := self.lists.get_row_at_index(0)) is not None:
            self.lists.remove(row)

    # ------------------------------------------------------------------ registry
    def _load_registry(self):
        self.s["dvbi_privacy_ack"] = True
        self.load_btn.set_sensitive(False)
        self._busy(True, _("Asking the service list registry…"))
        registries = self.s.get("dvbi_registries") or list(self.country.registries)
        code = self.country.code
        if not registries:                     # "Other": nothing entered yet
            self.registry_exp.set_expanded(True)
            self._busy(False, _("Enter the address of a registry or of a service list."))
            return

        def work():
            try:
                offers, used = dvbi.query_registry(registries, country=code)
                GLib.idle_add(self._registry_done, code, offers, used, "")
            except dvbi.DVBIError as e:
                GLib.idle_add(self._registry_done, code, [], "", str(e))
        threading.Thread(target=work, daemon=True).start()

    def _registry_done(self, code, offers, used, error):
        self._busy(False)
        if code != self.country.code:          # the country changed meanwhile
            return False
        self.load_btn.set_sensitive(self._ack_ok())
        if error:
            self.status.set_text(_("The registry could not be reached: {error}").format(error=error))
            return False
        self._clear_lists()
        self.offers = offers
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
        self.status.set_text(
            _("{v0} service lists from {netloc}").format(
                v0=len(offers), netloc=urllib.parse.urlsplit(used).netloc) if offers else
            _("The registry offers no service list for {country}.").format(
                country=_(self.country.name)))
        self._sensitive()
        return False

    # ------------------------------------------------------------------ install
    def _install(self, nationwide=None):
        if nationwide is not None:
            if nationwide:
                self.postcode.set_text("")
            if not self.country.areas:         # else the region from the fixed list
                self.nationwide.set_active(nationwide)
        offer = self._selected_offer()
        postcode = "" if self.nationwide.get_active() else self.postcode.get_text().strip()
        if postcode and not countries.valid_postcode(self.country, postcode):
            message(self.get_root(), _("Please enter a postcode with {n} digits").format(
                n=self.country.postcode_digits))
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
        self.current_lbl.set_text(_("In use: {v0}").format(
            v0=self.s.get('dvbi_list_name') or self.s.get('dvbi_list_url')))
        if res.error_code:
            self._ask_after_error(res.error_code)
            return
        if res.service_list is not None:
            cands = res.regions if len(res.regions) > 1 else []
            if cands:
                self.status.set_text(msg + _("\nChoose your region and press Install again."))
            self._show_regions(cands, res.region)
            self._show_packages(res.packages)

    def _ask_after_error(self, code):
        """IT-5: tell the user and offer to try again or the nationwide list."""
        if code == "ERROR_INVALID_POSTCODE":
            text, first = _("The service list does not know this postcode."), _("Enter Again")
        else:
            text, first = _("The service list server answered {code}.").format(code=code), \
                _("Try Again")
        dlg = Gtk.AlertDialog(message=text, detail=_("You can try again or use the nationwide "
                              "list without a postcode."),
                              buttons=[first, _("Nationwide List"), _("Cancel")],
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
