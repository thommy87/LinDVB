"""The watchdog writes the stacks when the main loop stops answering."""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from lindvb.watchdog import Watchdog  # noqa: E402


def test_watchdog_writes_stacks_when_the_main_loop_stalls(tmp_path):
    log = tmp_path / "hang.log"
    dog = Watchdog(log, stall=0.5)
    assert dog.start()
    try:
        time.sleep(2.5)            # no main loop runs here: a "hang"
        text = log.read_text()
        assert "not responding" in text
        assert "test_watchdog_writes_stacks" in text
    finally:
        dog.stop()
        import faulthandler
        faulthandler.disable()
