"""
run_headless.py — headless entry point for xmr-lottery-miner (no TUI).

Starts the miner core and logs periodic stats to stdout, which systemd captures
into the journal. Designed to run 24/7 as a service: it has no terminal UI, reads
no keys, and shuts the workers down cleanly on SIGTERM/SIGINT (the signals
`systemctl stop` and Ctrl-C send).

Run directly:   .venv/bin/python run_headless.py
As a service:   see xmr-lottery-miner.service
"""

from __future__ import annotations

import logging
import signal
import sys
import threading
import time

from main import load_config  # reuse the same config loader as the TUI entry point
from xmr_miner_core import HAVE_RANDOMX, Miner

# How often to emit a stats line (seconds). Journald keeps these; keep it modest.
LOG_INTERVAL = 30.0

log = logging.getLogger("xmr-miner")


def _setup_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s",
                                            datefmt="%Y-%m-%d %H:%M:%S"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)


def main() -> int:
    _setup_logging()
    cfg = load_config()

    backend = "RandomX (valid PoW)" if HAVE_RANDOMX else "BLAKE2b stand-in (shares rejected)"
    log.info("starting headless miner — %d workers -> %s:%d, cpu cap %.0f%%, temp limit %.0f C, backend: %s",
             cfg.threads, cfg.host, cfg.port, cfg.max_cpu_percent, cfg.temp_limit, backend)

    miner = Miner(cfg)
    miner.start()

    # Clean shutdown on SIGTERM (systemctl stop) and SIGINT (Ctrl-C).
    stop_event = threading.Event()

    def _handle_signal(signum, _frame):
        log.info("received signal %d — shutting down", signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    try:
        while not stop_event.wait(LOG_INTERVAL):
            s = miner.snapshot()
            status = "connected" if s["connected"] else "DISCONNECTED"
            log.info(
                "%s | %.1f H/s | accepted=%d rejected=%d found=%d | "
                "height=%s diff=%s | cpu=%.0f%% temp=%.0fC | workers=%d/%d | %s",
                status, s["hashrate"], s["accepted"], s["rejected"], s["found"],
                s["height"], s["difficulty"], s["cpu_percent"], s["temp"],
                s["active_workers"], s["total_workers"], s["pool_msg"],
            )
    finally:
        log.info("stopping workers...")
        miner.stop()
        log.info("stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
