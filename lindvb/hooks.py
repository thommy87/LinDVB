# SPDX-License-Identifier: GPL-2.0-or-later
"""Script hooks: run your own programs when something happens in LinDVB.

Put an executable file named after the event into ``~/.config/lindvb/hooks/``
(for example ``channel-tuned`` or ``recording-started.sh``). LinDVB runs it
in the background with details in environment variables, like DVBViewer's
``channelchange.vbs``, ``startrec.vbs`` and ``stoprec.vbs``:

========================  ==========================================================
Event                     Variables (besides ``LINDVB_EVENT``)
========================  ==========================================================
``startup``               –
``shutdown``              –
``channel-tuned``         ``LINDVB_CHANNEL``, ``LINDVB_CHANNEL_NUMBER``, ``LINDVB_CHANNEL_KEY``
``playback-stopped``      ``LINDVB_CHANNEL`` (empty for files)
``recording-started``     ``LINDVB_CHANNEL``, ``LINDVB_TITLE``, ``LINDVB_FILE``
``recording-stopped``     ``LINDVB_CHANNEL``, ``LINDVB_TITLE``, ``LINDVB_FILE``
``reminder``              ``LINDVB_CHANNEL``, ``LINDVB_TITLE``, ``LINDVB_START`` (unix time)
========================  ==========================================================
"""

import os
import subprocess
from pathlib import Path

from .config import CONFIG_DIR

HOOK_DIR = CONFIG_DIR / "hooks"
EVENTS = ("startup", "shutdown", "channel-tuned", "playback-stopped", "recording-started",
          "recording-stopped", "reminder")


def scripts_for(event, folder=None):
    """Executable files in the hook folder named ``event`` or ``event.<ext>``."""
    folder = Path(folder or HOOK_DIR)
    if not folder.is_dir():
        return []
    out = []
    for f in sorted(folder.iterdir()):
        if f.is_file() and (f.name == event or f.name.startswith(event + ".")) and \
                os.access(f, os.X_OK):
            out.append(f)
    return out


def environment(event, args):
    """Environment variables for a hook from the event's arguments."""
    env = {"LINDVB_EVENT": event}
    for a in args:
        if a is None:
            continue
        if hasattr(a, "epg_key") and hasattr(a, "name"):            # a Channel
            env["LINDVB_CHANNEL"] = a.name
            env["LINDVB_CHANNEL_KEY"] = a.key
            env["LINDVB_CHANNEL_NUMBER"] = str(getattr(a, "number", "") or "")
        elif hasattr(a, "channel_name") and hasattr(a, "start"):    # a Reminder
            env["LINDVB_CHANNEL"] = a.channel_name
            env["LINDVB_TITLE"] = a.title
            env["LINDVB_START"] = str(a.start)
        elif isinstance(a, dict):
            env.update({f"LINDVB_{k.upper()}": str(v) for k, v in a.items() if v is not None})
    return env


def run(event, args=(), folder=None):
    """Start the scripts for ``event``; returns the started processes."""
    procs = []
    scripts = scripts_for(event, folder)
    if not scripts:
        return procs
    env = {**os.environ, **environment(event, args)}
    for script in scripts:
        try:
            procs.append(subprocess.Popen([str(script)], env=env, stdin=subprocess.DEVNULL,
                                          start_new_session=True))
        except OSError:
            pass
    return procs
