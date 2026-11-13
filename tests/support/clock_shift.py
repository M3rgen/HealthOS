"""Run the suite at another wall-clock moment: ``make test-clock`` (tests/support/clock_matrix.sh).

Started from the worker's first import through ``-p clock_shift``, so the whole application sees
the shifted clock. ``time-machine`` patches datetime and time inside CPython; it needs no
``datetime`` subclass, which is what broke a plugin that replaced the class. It is a hash-locked
development dependency; clock_matrix.sh puts only this plugin on PYTHONPATH.
"""

from __future__ import annotations

import os
from datetime import datetime

import time_machine

moment = os.environ.get("HEALTHOS_TEST_CLOCK")
if moment:  # empty: the real time, for a run that only changes the host time zone
    # tick=True: time keeps running from the moment; tests with a monotonic budget still work.
    time_machine.travel(datetime.fromisoformat(moment), tick=True).start()
