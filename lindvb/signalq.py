# SPDX-License-Identifier: GPL-2.0-or-later
"""Quality of service for the on-screen info: one number from 0 to 1 and a level.

DVB channels use the frontend statistics that dvbsrc posts ("dvb-frontend-stats":
lock, signal, snr, ber, unc). Drivers scale signal and SNR differently: most
report 0..65535, some report the SNR in 0.1 dB steps (e.g. 132 = 13.2 dB). Blocks
the error correction could not repair (unc, a running counter) mean the picture
breaks up, whatever the SNR says, so new ones pull the quality down.

Internet channels have no tuner. For them the quality comes from the stream:
nothing yet or an error is no signal, buffering shows how full the buffer is,
and a stream that had to rebuffer a short while ago is marked as fair.
"""

import time

NONE, BAD, POOR, FAIR, GOOD = "none", "bad", "poor", "fair", "good"
LEVELS = (NONE, BAD, POOR, FAIR, GOOD)

# lower bounds of the levels (fraction 0..1)
POOR_FROM = 0.30
FAIR_FROM = 0.50
GOOD_FROM = 0.70

SNR_DB_FULL = 20.0           # an SNR in dB at or above this counts as 100 %
REBUFFER_PENALTY_SECONDS = 30.0
UNC_PENALTY_SECONDS = 10.0


def level_for(q):
    """The level of a quality fraction; 0 (or None) is no signal."""
    if q is None or q <= 0:
        return NONE
    if q < POOR_FROM:
        return BAD
    if q < FAIR_FROM:
        return POOR
    if q < GOOD_FROM:
        return FAIR
    return GOOD


def _fraction(v):
    """Signal or SNR value as a fraction. Values up to 1000 are taken as 0.1 dB
    steps (drivers that report dB); larger ones as the usual 0..65535 scale."""
    if v is None or v <= 0:
        return 0.0
    if v <= 1000:
        return min(1.0, v / 10.0 / SNR_DB_FULL)
    return min(1.0, v / 65535.0)


def dvb_quality(stats):
    """Quality fraction 0..1 from one dvb-frontend-stats dict (no unc history)."""
    if not stats or not stats.get("lock"):
        return 0.0
    snr = _fraction(stats.get("snr"))
    sig = _fraction(stats.get("signal"))
    if snr and sig:
        q = 0.7 * snr + 0.3 * sig      # SNR says more about the picture than level
    else:
        q = snr or sig
    if not q:
        q = 1.0 if stats.get("lock") else 0.0   # locked, but the driver reports nothing
    return q


class Meter:
    """Keeps the quality of the channel on screen, for DVB and for internet streams."""

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self.reset()

    def reset(self, kind=None):
        """New channel. *kind* is "dvb", "iptv" or None (nothing to measure)."""
        self.kind = kind
        self.quality = 0.0
        self._unc = None
        self._unc_at = None
        self._rebuffer_at = None
        self._buffering = None
        self._playing = False

    @property
    def level(self):
        return level_for(self.quality)

    # -- DVB -----------------------------------------------------------------
    def feed_stats(self, stats):
        q = dvb_quality(stats)
        unc = stats.get("unc") if stats else None
        now = self._clock()
        if unc is not None:
            if self._unc is not None and unc > self._unc:
                self._unc_at = now
            self._unc = unc
        if q and self._unc_at is not None and now - self._unc_at < UNC_PENALTY_SECONDS:
            q = min(q, POOR_FROM - 0.01)        # uncorrectable errors: the picture breaks
        self.quality = q
        return q

    # -- internet ------------------------------------------------------------
    def feed_buffering(self, pct):
        now = self._clock()
        if pct >= 100:
            self._buffering = None
        else:
            if self._playing and self._buffering is None:
                self._rebuffer_at = now         # had to stop to fill up again
            self._buffering = pct
        return self._update_stream(now)

    def stream_playing(self):
        self._playing = True
        return self._update_stream(self._clock())

    def stream_failed(self):
        self._playing = False
        self._buffering = None
        self.quality = 0.0
        return self.quality

    def _update_stream(self, now):
        if self._buffering is not None:
            # buffering: how full it is, but never green while the picture waits
            q = max(0.01, min(GOOD_FROM - 0.01, self._buffering / 100.0))
        elif self._playing:
            q = 1.0
            if self._rebuffer_at is not None and now - self._rebuffer_at < REBUFFER_PENALTY_SECONDS:
                q = FAIR_FROM               # plays, but stalled a short while ago
        else:
            q = 0.0
        self.quality = q
        return q

    def tick(self):
        """Re-evaluate time-based penalties (internet streams)."""
        if self.kind == "iptv":
            return self._update_stream(self._clock())
        return self.quality
