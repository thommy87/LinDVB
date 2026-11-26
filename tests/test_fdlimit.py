"""The limit of open files is raised at the start (safety net, see
lindvb.fdlimit), and the places that waited with select() now use poll, so
tuners, CI modules and SAT>IP sockets numbered above 1023 still work."""

import os
import resource
import socket
import subprocess
import sys

import pytest

from lindvb import fdlimit, fdwait


def test_raises_the_soft_limit_towards_the_system_maximum(monkeypatch):
    calls = []
    monkeypatch.setattr(resource, "getrlimit", lambda what: (1024, 524288))
    monkeypatch.setattr(resource, "setrlimit", lambda what, lim: calls.append(lim))
    monkeypatch.setattr(fdlimit, "_raised_from", None)
    assert fdlimit.raise_limit() == (1024, 16384)
    assert calls == [(16384, 524288)]                  # the hard limit stays as it is
    assert "raised from 1024" in fdlimit.describe()


@pytest.mark.parametrize("soft, hard, want", [
    (1024, 4096, 4096),                                # no more than the system allows
    (65536, 524288, None),                             # never lowered
    (1024, 1024, None),                                # nothing to raise
    (resource.RLIM_INFINITY, resource.RLIM_INFINITY, None),
])
def test_never_beyond_the_hard_limit_and_never_lowered(monkeypatch, soft, hard, want):
    calls = []
    monkeypatch.setattr(resource, "getrlimit", lambda what: (soft, hard))
    monkeypatch.setattr(resource, "setrlimit", lambda what, lim: calls.append(lim))
    fdlimit.raise_limit()
    assert calls == ([(want, hard)] if want else [])


def test_a_refusal_changes_nothing(monkeypatch):
    def refuse(what, lim):
        raise ValueError("not allowed")
    monkeypatch.setattr(resource, "getrlimit", lambda what: (1024, 524288))
    monkeypatch.setattr(resource, "setrlimit", refuse)
    assert fdlimit.raise_limit() == (1024, 1024)


def test_the_real_raise_in_a_child():
    code = ("import resource; from lindvb import fdlimit; "
            "resource.setrlimit(resource.RLIMIT_NOFILE, (256, resource.getrlimit("
            "resource.RLIMIT_NOFILE)[1])); print(fdlimit.raise_limit(1000)); "
            "print(resource.getrlimit(resource.RLIMIT_NOFILE)[0]); print(fdlimit.describe())")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         cwd=os.path.dirname(os.path.dirname(__file__)), check=True).stdout
    first, soft, desc = out.split("\n")[:3]
    hard = resource.getrlimit(resource.RLIMIT_NOFILE)[1]
    want = 1000 if hard == resource.RLIM_INFINITY else min(1000, hard)
    assert first == f"(256, {want})" and soft == str(want)
    assert desc.startswith(f"{want} (raised from 256; system maximum ")


def test_readable_answers_like_select():
    a, b = socket.socketpair()
    try:
        assert fdwait.readable([a, b.fileno()], 0) == []
        b.send(b"x")
        assert fdwait.readable([a, b.fileno()], 1) == [a]       # the object given comes back
        b.close()
        a.recv(1)
        assert fdwait.readable([a], 1) == [a]                   # hang-up reads as ready, as in select
    finally:
        a.close()
    with pytest.raises(ValueError):
        fdwait.readable([-1], 0)
    r, w = os.pipe()
    os.close(r)
    os.close(w)
    with pytest.raises(OSError):                                # closed meanwhile, as select
        fdwait.readable([r], 0)


def test_readable_above_1023():
    """select() refuses these (ValueError); after the raise LinDVB may get them."""
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if hard != resource.RLIM_INFINITY and hard < 1100:
        pytest.skip("the system allows no descriptor above 1023")
    if soft < 1100:
        resource.setrlimit(resource.RLIMIT_NOFILE, (1100, hard))
    r, w = os.pipe()
    high_r, high_w = 1050, 1051
    try:
        os.dup2(r, high_r)
        os.dup2(w, high_w)
        os.write(high_w, b"x")
        assert fdwait.readable([high_r], 1) == [high_r]
    finally:
        for fd in (r, w, high_r, high_w):
            os.close(fd)
        if soft < 1100:
            resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))


def test_no_select_left():
    """select() would fail again for descriptors above 1023."""
    root = os.path.join(os.path.dirname(os.path.dirname(__file__)), "lindvb")
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            if name.endswith(".py") and name != "fdwait.py":
                with open(os.path.join(dirpath, name), encoding="utf-8") as f:
                    assert "select.select(" not in f.read(), name
