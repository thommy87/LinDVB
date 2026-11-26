# SPDX-License-Identifier: GPL-2.0-or-later
"""Waiting for file descriptors to become readable, without select().

select() only takes descriptors below 1024 (FD_SETSIZE; Python raises
ValueError beyond). LinDVB raises its limit of open files above 1024
(lindvb.fdlimit), so a tuner or socket opened late may get a higher number:
poll() has no such limit. readable() answers like select.select(fds, [], [],
timeout)[0], including OSError(EBADF) for a descriptor that is not open.
"""

import errno
import select

_EVENTS = select.POLLIN | select.POLLPRI


def readable(fds, timeout):
    """The ones of *fds* (ints or objects with fileno()) that can be read, or
    that have an error or hang-up (as select reports them), within *timeout*
    seconds."""
    poller = select.poll()
    by_fd = {}
    for f in fds:
        fd = f if isinstance(f, int) else f.fileno()
        if fd < 0:
            raise ValueError("file descriptor cannot be a negative integer")
        poller.register(fd, _EVENTS)
        by_fd[fd] = f
    ready = []
    for fd, ev in poller.poll(max(0, int(timeout * 1000))):
        if ev & select.POLLNVAL:
            raise OSError(errno.EBADF, "Bad file descriptor")
        ready.append(by_fd[fd])
    return ready
