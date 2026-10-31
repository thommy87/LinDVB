"""Example LinDVB plugin: a trivial channel-list importer.

Copy this file (or the whole ``examples`` folder's files) into your plugin
directory to try it:

    ~/.local/share/lindvb/plugins/

or point the LINDVB_PLUGIN_PATH environment variable at a folder of plugins.
Then "Import Channel List…" will offer "Example list (*.example)".

A plugin is ordinary Python running in the LinDVB process, so only install
plugins you trust.
"""

NAME = "Example importer"
VERSION = "1.0"


def _load(path):
    """Read one 'name,url' pair per line into LinDVB channels."""
    from lindvb.channels import Channel
    channels = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "," not in line:
                continue
            name, url = line.split(",", 1)
            channels.append(Channel(name=name.strip(), delsys="iptv", url=url.strip()))
    return channels


def register(api):
    api.add_importer("Example list (*.example)", ".example", _load)
    api.add_menu_action("Example: say hello",
                        lambda app: app.window.set_status("Hello from the example plugin!"))
