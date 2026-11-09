# SPDX-License-Identifier: GPL-2.0-or-later
"""Favourites lists (no GTK).

The user keeps any number of named favourites lists ("Favourites", "Sport",
"Kids" ...). Each list holds channel keys in its own order, so a list can be
sorted freely without touching the channel numbers of the full list. The
lists live in favourites.json next to channels.json.

``Channel.favourite`` stays as a quick "in any list" flag: ``sync`` sets it
from the lists. The first start after the update makes one list of the
channels marked as favourites before, in channel list order.
"""

import json

from .config import CONFIG_DIR
from .i18n import _


class FavouriteLists:
    path = CONFIG_DIR / "favourites.json"

    def __init__(self, db):
        self.db = db
        self.lists = []          # [{"name": str, "keys": [channel key, ...]}]
        self.load()

    # -- storage ----------------------------------------------------------------
    def load(self):
        try:
            data = json.loads(self.path.read_text())
            self.lists = [{"name": str(item["name"]),
                           "keys": [str(k) for k in item.get("keys", [])]}
                          for item in data.get("lists", [])
                          if isinstance(item, dict) and "name" in item]
        except (OSError, ValueError, TypeError, AttributeError):
            # first start: the channels marked as favourites become one list
            keys = list(dict.fromkeys(c.key for c in self.db.channels if c.favourite))
            self.lists = [{"name": _("Favourites"), "keys": keys}]
        self.sync()

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"lists": self.lists}, indent=1, ensure_ascii=False))
        tmp.replace(self.path)

    def sync(self):
        """Set ``Channel.favourite`` to "in any list" for every channel."""
        keys = {k for fav in self.lists for k in fav["keys"]}
        for c in self.db.channels:
            c.favourite = c.key in keys

    # -- the lists ----------------------------------------------------------------
    def __len__(self):
        return len(self.lists)

    def names(self):
        return [fav["name"] for fav in self.lists]

    def valid(self, i):
        return isinstance(i, int) and 0 <= i < len(self.lists)

    def create(self, name):
        """A new empty list; returns its index."""
        self.lists.append({"name": self.unique_name(name), "keys": []})
        return len(self.lists) - 1

    def unique_name(self, name, skip=None):
        name = (name or "").strip() or _("Favourites")
        taken = {fav["name"] for i, fav in enumerate(self.lists) if i != skip}
        if name not in taken:
            return name
        n = 2
        while f"{name} {n}" in taken:
            n += 1
        return f"{name} {n}"

    def rename(self, i, name):
        if self.valid(i):
            self.lists[i]["name"] = self.unique_name(name, skip=i)

    def delete(self, i):
        if self.valid(i):
            del self.lists[i]
            self.sync()

    def move_list(self, i, step):
        """Move list *i* up (-1) or down (+1); returns its new index."""
        j = i + step
        if self.valid(i) and self.valid(j):
            self.lists[i], self.lists[j] = self.lists[j], self.lists[i]
            return j
        return i

    def ensure_one(self):
        """Index of the first list, made if there is none."""
        if not self.lists:
            self.create(_("Favourites"))
        return 0

    # -- channels in a list -----------------------------------------------------
    def channels(self, i):
        """The channels of list *i* in the list's order. Keys of channels not in
        the channel list (for now) are skipped but kept in the list."""
        if not self.valid(i):
            return []
        index = {}
        for c in self.db.channels:
            index.setdefault(c.key, c)
        return [index[k] for k in self.lists[i]["keys"] if k in index]

    def contains(self, i, c):
        return self.valid(i) and c.key in self.lists[i]["keys"]

    def member_of(self, c):
        """Indexes of the lists that hold *c*."""
        return [i for i, fav in enumerate(self.lists) if c.key in fav["keys"]]

    def add(self, i, c):
        if self.valid(i) and c.key not in self.lists[i]["keys"]:
            self.lists[i]["keys"].append(c.key)
            c.favourite = True

    def remove(self, i, c):
        if self.valid(i) and c.key in self.lists[i]["keys"]:
            self.lists[i]["keys"].remove(c.key)
            c.favourite = any(c.key in fav["keys"] for fav in self.lists)

    def set_member(self, i, c, on):
        (self.add if on else self.remove)(i, c)

    def toggle(self, i, c):
        self.set_member(i, c, not self.contains(i, c))

    def move(self, i, c, step):
        """Move *c* by *step* places among the channels of list *i* that are in
        the channel list; returns True when it moved."""
        shown = self.channels(i)
        try:
            pos = next(n for n, x in enumerate(shown) if x.key == c.key)
        except StopIteration:
            return False
        new = max(0, min(len(shown) - 1, pos + step))
        if new == pos:
            return False
        return self.move_to(i, c, shown[new])

    def move_to(self, i, c, target, after=None):
        """Put *c* at the place of *target* in list *i*: before it when moving
        up, after it when moving down (or as *after* says). Returns True when
        the order changed."""
        if not self.valid(i) or c.key == target.key:
            return False
        keys = self.lists[i]["keys"]
        if c.key not in keys or target.key not in keys:
            return False
        src = keys.index(c.key)
        dst = keys.index(target.key)
        if after is None:
            after = dst > src
        keys.pop(src)
        dst = keys.index(target.key) + (1 if after else 0)
        keys.insert(dst, c.key)
        return True

    def move_to_end(self, i, c, top=True):
        if not self.contains(i, c):
            return False
        keys = self.lists[i]["keys"]
        if keys[0 if top else -1] == c.key:
            return False
        keys.remove(c.key)
        if top:
            keys.insert(0, c.key)
        else:
            keys.append(c.key)
        return True

    def rekey(self, old, new):
        """A channel got another key (edited tuning data): keep its places."""
        if old == new:
            return
        for fav in self.lists:
            fav["keys"] = [new if k == old else k for k in fav["keys"]]
            fav["keys"] = list(dict.fromkeys(fav["keys"]))


# -- the channel list filter ------------------------------------------------------
# Filter keys of the channel list: the plain filters and one per favourites
# list ("fav:<index>"). Before the favourites lists the setting held the index
# into a fixed list where 1 was the one favourites list.
_OLD_FILTERS = ("all", "fav:0", "fta", "enc", "hd", "iptv")


def filter_keys(n_lists):
    """The channel list filters in dropdown order."""
    return ["all"] + [f"fav:{i}" for i in range(n_lists)] + ["fta", "enc", "hd", "iptv"]


def filter_setting(value):
    """The "list_filter" setting as a filter key (older settings held an index)."""
    if isinstance(value, bool):
        return "all"
    if isinstance(value, int):
        return _OLD_FILTERS[value] if 0 <= value < len(_OLD_FILTERS) else "all"
    return value if isinstance(value, str) and value else "all"


def fav_index(key):
    """The favourites list index of a filter key, None for the other filters."""
    if isinstance(key, str) and key.startswith("fav:") and key[4:].isdigit():
        return int(key[4:])
    return None
