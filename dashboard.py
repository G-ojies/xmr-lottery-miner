"""
dashboard.py — Rich TUI for xmr-lottery-miner.

Dark theme with an XMR-blue (#4C9EE8) accent. Renders a live layout:
  * header        — title + connection state
  * hashrate      — big number + sparkline of recent samples
  * shares        — accepted / rejected / blocks-found
  * lottery       — solo-block odds, framed as a lottery ticket
  * job           — current job id, height, difficulty
  * footer        — CPU / temp / worker governor state
"""

from __future__ import annotations

from rich.align import Align
from rich.console import Group
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

# Palette -------------------------------------------------------------------- #
XMR_BLUE = "#4C9EE8"
DIM = "#5c6370"
GOOD = "#7ee787"
WARN = "#e3b341"
BAD = "#f85149"
FG = "#c9d1d9"
BG = "#0d1117"

SPARK = "▁▂▃▄▅▆▇█"

# Rough Monero network hashrate (H/s) for solo-odds framing. ~2.5 GH/s.
NETWORK_HASHRATE = 2_500_000_000
BLOCK_TIME_S = 120  # Monero targets a 2-minute block.


def _fmt_hashrate(hr: float) -> str:
    for unit in ("H/s", "kH/s", "MH/s", "GH/s"):
        if hr < 1000:
            return f"{hr:,.1f} {unit}"
        hr /= 1000
    return f"{hr:,.1f} TH/s"


def _fmt_duration(seconds: float) -> str:
    if seconds <= 0 or seconds != seconds:  # NaN guard
        return "—"
    units = [("y", 31_536_000), ("d", 86_400), ("h", 3_600), ("m", 60)]
    for name, size in units:
        if seconds >= size:
            return f"{seconds / size:,.1f}{name}"
    return f"{seconds:,.0f}s"


def _sparkline(values, width=48):
    if not values:
        return Text("(collecting samples…)", style=DIM)
    vals = list(values)[-width:]
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1.0
    chars = []
    for v in vals:
        idx = int((v - lo) / span * (len(SPARK) - 1))
        chars.append(SPARK[idx])
    return Text("".join(chars), style=XMR_BLUE)


def build_layout() -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="hashrate", size=7),
        Layout(name="middle", size=9),
        Layout(name="job", size=6),
        Layout(name="footer", size=3),
    )
    layout["middle"].split_row(Layout(name="shares"), Layout(name="lottery"))
    return layout


def _panel(body, title, border=XMR_BLUE):
    return Panel(body, title=f"[{XMR_BLUE}]{title}[/]", border_style=border,
                 style=f"on {BG}", padding=(0, 1))


def render(layout: Layout, snap: dict):
    layout["header"].update(_render_header(snap))
    layout["hashrate"].update(_render_hashrate(snap))
    layout["shares"].update(_render_shares(snap))
    layout["lottery"].update(_render_lottery(snap))
    layout["job"].update(_render_job(snap))
    layout["footer"].update(_render_footer(snap))
    return layout


def _render_header(snap):
    connected = snap["connected"]
    dot = f"[{GOOD}]●[/]" if connected else f"[{BAD}]●[/]"
    state = "CONNECTED" if connected else "OFFLINE"
    title = Text.assemble(
        ("⛏  ", XMR_BLUE),
        ("XMR ", f"bold {XMR_BLUE}"),
        ("LOTTERY MINER", "bold white"),
    )
    line = Text.assemble(title, ("   ", ""), Text.from_markup(f"{dot} {state}"))
    sub = Text(snap["pool_msg"], style=DIM)
    return _panel(Group(Align.center(line), Align.center(sub)), "status")


def _render_hashrate(snap):
    big = Text(_fmt_hashrate(snap["hashrate"]), style=f"bold {XMR_BLUE}")
    big.stylize("bold")
    spark = _sparkline(snap["hr_history"])
    peak = max(snap["hr_history"]) if snap["hr_history"] else 0.0
    body = Group(
        Align.center(big),
        Align.center(spark),
        Align.center(Text(f"peak {_fmt_hashrate(peak)}", style=DIM)),
    )
    return _panel(body, "hashrate")


def _render_shares(snap):
    t = Table.grid(padding=(0, 2))
    t.add_column(justify="right", style=DIM)
    t.add_column(justify="left", style=f"bold {FG}")
    t.add_row("accepted", f"[{GOOD}]{snap['accepted']}[/]")
    t.add_row("rejected", f"[{BAD}]{snap['rejected']}[/]")
    t.add_row("blocks found", f"[{XMR_BLUE}]{snap['found']}[/]")
    total = snap["accepted"] + snap["rejected"]
    ratio = (snap["accepted"] / total * 100) if total else 0.0
    t.add_row("accept rate", f"{ratio:.1f}%")
    return _panel(t, "shares")


def _render_lottery(snap):
    hr = snap["hashrate"] or 0.0
    share = hr / NETWORK_HASHRATE if NETWORK_HASHRATE else 0.0
    # Expected time to solo-find a block at this hashrate.
    eta = (BLOCK_TIME_S / share) if share > 0 else float("inf")
    odds = f"1 in {NETWORK_HASHRATE / hr:,.0f}" if hr > 0 else "—"
    daily = (86_400 / eta) if eta not in (0, float("inf")) else 0.0
    t = Table.grid(padding=(0, 2))
    t.add_column(justify="right", style=DIM)
    t.add_column(justify="left", style=f"bold {FG}")
    t.add_row("network", _fmt_hashrate(NETWORK_HASHRATE))
    t.add_row("your share", f"{share * 100:.8f}%")
    t.add_row("block odds", f"[{XMR_BLUE}]{odds}[/]")
    t.add_row("expected block", _fmt_duration(eta))
    t.add_row("blocks/day", f"{daily:.2e}")
    ticket = Text("🎟  every hash is a ticket", style=f"italic {WARN}")
    return _panel(Group(t, Align.center(ticket)), "lottery / odds")


def _render_job(snap):
    t = Table.grid(padding=(0, 2))
    t.add_column(justify="right", style=DIM)
    t.add_column(justify="left", style=f"bold {FG}")
    jid = snap["job_id"] or "—"
    t.add_row("job id", f"[{XMR_BLUE}]{jid}[/]")
    t.add_row("height", f"{snap['height']:,}" if snap["height"] else "—")
    t.add_row("difficulty", f"{snap['difficulty']:,}" if snap["difficulty"] else "—")
    return _panel(t, "current job")


def _render_footer(snap):
    cpu = snap["cpu_percent"]
    temp = snap["temp"]
    cpu_style = GOOD if cpu < 60 else (WARN if cpu < 85 else BAD)
    temp_style = GOOD if temp < 60 else (WARN if temp < 75 else BAD)
    temp_str = f"{temp:.0f}°C" if temp else "n/a"
    workers = f"{snap['active_workers']}/{snap['total_workers']}"
    line = Text.from_markup(
        f"[{DIM}]cpu[/] [{cpu_style}]{cpu:.0f}%[/]   "
        f"[{DIM}]temp[/] [{temp_style}]{temp_str}[/]   "
        f"[{DIM}]workers[/] [{XMR_BLUE}]{workers}[/]   "
        f"[{DIM}]q to quit[/]"
    )
    return _panel(Align.center(line), "system")
