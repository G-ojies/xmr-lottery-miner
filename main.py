"""
main.py — entry point for xmr-lottery-miner.

Loads config.toml, starts the miner core, and runs the Rich dashboard live loop.
Press Ctrl-C (or q) to stop.
"""

from __future__ import annotations

import select
import sys
import time
from contextlib import contextmanager
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:
    import tomli as tomllib

from rich.console import Console
from rich.live import Live

import dashboard
from xmr_miner_core import Miner, MinerConfig

CONFIG_PATH = Path(__file__).parent / "config.toml"


@contextmanager
def raw_keys():
    """Put the terminal in cbreak mode so single keypresses (e.g. 'q') are
    readable without Enter. No-ops on non-TTY stdin (pipes, redirected input)."""
    if not sys.stdin.isatty():
        yield lambda: None
        return
    import termios
    import tty

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    tty.setcbreak(fd)

    def poll_key():
        """Return the next pending keypress, or None if none is waiting."""
        if select.select([sys.stdin], [], [], 0)[0]:
            return sys.stdin.read(1)
        return None

    try:
        yield poll_key
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def load_config(path: Path = CONFIG_PATH) -> MinerConfig:
    with open(path, "rb") as f:
        data = tomllib.load(f)
    wallet = data["wallet"]
    mining = data["mining"]
    pool = data["pool"]
    return MinerConfig(
        wallet_address=wallet["wallet_address"],
        worker_name=wallet["worker_name"],
        threads=int(mining["threads"]),
        max_cpu_percent=float(mining["max_cpu_percent"]),
        temp_limit=float(mining["temp_limit"]),
        host=pool["host"],
        port=int(pool["port"]),
        password=pool.get("password", "x"),
    )


def main():
    console = Console()
    try:
        cfg = load_config()
    except FileNotFoundError:
        console.print(f"[red]config.toml not found at {CONFIG_PATH}[/]")
        sys.exit(1)
    except (KeyError, ValueError) as e:
        console.print(f"[red]config.toml is missing or has a bad field: {e}[/]")
        sys.exit(1)

    console.print(f"[#4C9EE8]Starting xmr-lottery-miner[/] — {cfg.threads} workers → "
                  f"{cfg.host}:{cfg.port}, cap {cfg.max_cpu_percent:.0f}% CPU, "
                  f"temp limit {cfg.temp_limit:.0f}°C")

    miner = Miner(cfg)
    miner.start()
    layout = dashboard.build_layout()

    try:
        with raw_keys() as poll_key, \
                Live(layout, console=console, refresh_per_second=4, screen=True) as live:
            while True:
                dashboard.render(layout, miner.snapshot())
                live.refresh()
                key = poll_key()
                if key and key.lower() == "q":
                    break
                time.sleep(0.25)
    except KeyboardInterrupt:
        pass
    finally:
        console.print("[#4C9EE8]Stopping workers…[/]")
        miner.stop()
        console.print("[#7ee787]Stopped.[/]")


if __name__ == "__main__":
    main()
