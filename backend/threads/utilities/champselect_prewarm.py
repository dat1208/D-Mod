#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build the LTK overlay during ChampSelect, not during the countdown.

The loadout countdown is nine seconds. Choosing a champion the overlay has
never seen costs far more than that: LTK Manager rewrites the champion WAD and
Map11.wad.client (2.5 GB, the map's localised strings live in it), and in the
18:41 session that took 8.07s of wall clock -- the build finished after the
client had already launched and streamed the WAD, so the DLL attached too late
and logged "joined too late, not overlaying".

ChampSelect is sixty to ninety seconds long. Everything the countdown ticker
does can be done there instead, once per skin change, which is what this thread
is for. It watches last_hovered_skin_id / selected_chroma_id and calls
_ltk_prepare_countdown whenever they name a different archive, so by the time
the countdown starts the overlay is already on disk and the ticker's own call
is a no-op.

The thread never touches injection: it prepares the patcher and steps aside. If
it fails, the countdown path still runs exactly as before.
"""

import threading
import time

from utils.core.logging import get_logger

log = get_logger()

# ChampSelect allows this little time at worst (a dodge re-queues fast), so the
# poll has to be quick enough to see a skin change before the countdown starts.
_POLL_INTERVAL_S = 0.4


class ChampSelectPrewarm(threading.Thread):
    """Prepare the overlay for the current skin while ChampSelect is open.

    One instance per app run: it sleeps between phases instead of being joined
    and re-created, so a missed stop can never leave two threads racing for the
    same patcher.
    """

    def __init__(self, injection_manager):
        super().__init__(daemon=True, name="ChampSelectPrewarm")
        self.injection_manager = injection_manager
        self._stop = threading.Event()
        # What this thread last handed the manager. Kept here, not on the
        # manager, so a countdown pick and a prewarm pick cannot confuse each
        # other.
        self._prepared_for = None

    def stop(self):
        self._stop.set()

    def run(self):
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as exc:  # never let a poll kill the thread
                log.debug("[PREWARM] Poll failed: %s", exc)
            self._stop.wait(_POLL_INTERVAL_S)

    def _tick(self):
        manager = self.injection_manager
        state = getattr(manager, "shared_state", None)
        if state is None or getattr(state, "phase", None) != "ChampSelect":
            return
        if self._stop.is_set():
            return

        # A dodge or a re-queue starts a new ChampSelect: forget what was built
        # for the old one so the same skin is prepared again rather than
        # skipped by a stale key.
        generation = getattr(state, "champ_select_generation", 0)
        key = (
            generation,
            getattr(state, "locked_champ_id", None),
            getattr(state, "selected_chroma_id", None) or getattr(state, "last_hovered_skin_id", None),
        )
        if key == self._prepared_for:
            return
        if key[1] is None or key[2] is None:
            return

        # Same guard the countdown uses: a base skin needs no overlay, and
        # _ltk_prepare_countdown already returns False for one.
        self._prepared_for = key
        started = time.monotonic()
        try:
            prepared = manager._ltk_prepare_countdown()
        except Exception as exc:
            log.warning("[PREWARM] Could not prepare the overlay for %s: %s", key[2:], exc)
            # Let the next poll retry: the failure was most likely a manager
            # that was still starting up.
            self._prepared_for = None
            return
        if prepared:
            log.info(
                "[PREWARM] Overlay prepared for skin %s during ChampSelect "
                "(%.2fs); the countdown starts with it already built",
                key[2], time.monotonic() - started,
            )
