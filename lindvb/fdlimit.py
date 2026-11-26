# SPDX-License-Identifier: GPL-2.0-or-later
"""The limit of open files, raised at the start as a safety net.

Most desktops start programs with a soft limit of 1024 open files and allow
far more (the hard limit). With VA-API and its deinterlacer, graphics buffers
stay open below LinDVB with every channel change (being traced, see the
concept); at 1024 the sound could no longer start. Raising the soft limit
towards the hard one is what systemd recommends for programs that need more;
it does not fix the cause. Descriptors above 1023 work with poll (lindvb.fdwait),
not with select().
"""

TARGET = 16384      # plenty for hours of channel changes, small for programs started from here

_raised_from = None  # the soft limit before raise_limit() changed it


def raise_limit(target=TARGET):
    """(old soft limit, new soft limit); unchanged where the system allows no
    more or has no resource module."""
    global _raised_from
    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    except (ImportError, OSError, ValueError):
        return None, None
    want = target if hard == resource.RLIM_INFINITY else min(target, hard)
    if soft != resource.RLIM_INFINITY and soft < want:
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (want, hard))
            _raised_from = soft
            return soft, want
        except (OSError, ValueError):
            pass
    return soft, soft


def describe():
    """For the debug log: "16384 (raised from 1024; system maximum 524288)"."""
    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    except (ImportError, OSError, ValueError):
        return "unknown"

    def show(n):
        return "unlimited" if n == resource.RLIM_INFINITY else str(n)
    raised = f"raised from {_raised_from}; " if _raised_from is not None else ""
    return f"{show(soft)} ({raised}system maximum {show(hard)})"
