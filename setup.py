"""
setup.py — configure & verify xmr-lottery-miner.

This is NOT a packaging script. It:
  1. checks the Python version,
  2. verifies (and offers to install) the required dependencies,
  3. validates config.toml loads and its fields are sane,
  4. prints a readiness summary.

Run:  python setup.py
Add --install to auto pip-install anything missing.
"""

from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "config.toml"
REQUIREMENTS = ROOT / "requirements.txt"

# import name -> pip name
DEPS = {"rich": "rich", "psutil": "psutil", "tomli": "tomli"}

# Shipped as a placeholder; this is the Monero Developers' donation address,
# which mining pools permanently ban. Mining requires the user's own wallet.
DEV_WALLET = (
    "44AFFq5kSiGBoZ4NMDwYtN18obc8AemS33DBLWs3H7otXft3XjrpDtQGv7SqSsaBYBb98uNbr2VBBEt7f2wfn3RVGQBEP3A"
)

GREEN, RED, BLUE, YELLOW, RESET = "\033[92m", "\033[91m", "\033[94m", "\033[93m", "\033[0m"


def ok(msg):
    print(f"{GREEN}✓{RESET} {msg}")


def fail(msg):
    print(f"{RED}✗{RESET} {msg}")


def info(msg):
    print(f"{BLUE}•{RESET} {msg}")


def check_python():
    v = sys.version_info
    if v < (3, 8):
        fail(f"Python {v.major}.{v.minor} too old — need 3.8+")
        return False
    ok(f"Python {v.major}.{v.minor}.{v.micro}")
    return True


def check_deps(auto_install: bool):
    # tomllib ships with 3.11+, so tomli is only needed below that.
    needed = dict(DEPS)
    if sys.version_info >= (3, 11):
        needed.pop("tomli", None)
        ok("tomllib available (stdlib) — tomli optional")

    missing = []
    for mod in needed:
        try:
            importlib.import_module(mod)
            ok(f"{mod} installed")
        except ImportError:
            fail(f"{mod} missing")
            missing.append(DEPS[mod])

    if missing:
        if auto_install:
            info(f"installing: {', '.join(missing)}")
            rc = subprocess.call([sys.executable, "-m", "pip", "install", *missing])
            if rc != 0:
                fail("pip install failed")
                return False
            ok("dependencies installed")
        else:
            fail(f"missing deps — run: pip install {' '.join(missing)}  (or: python setup.py --install)")
            return False
    return True


def check_randomx():
    """Report which hashing backend will be used. Never fails setup."""
    try:
        import pyrx  # noqa: F401
        ok("pyrx (RandomX) installed — shares are valid PoW")
    except ImportError:
        print(f"{YELLOW}!{RESET} pyrx not installed — DEMO mode (BLAKE2b stand-in, "
              f"pool will reject shares). For real mining: pip install pyrx")
    return True


def check_config():
    if not CONFIG_PATH.exists():
        fail("config.toml not found")
        return False
    try:
        try:
            import tomllib
        except ModuleNotFoundError:
            import tomli as tomllib
        with open(CONFIG_PATH, "rb") as f:
            cfg = tomllib.load(f)
    except Exception as e:
        fail(f"config.toml failed to parse: {e}")
        return False

    try:
        wallet = cfg["wallet"]["wallet_address"]
        worker = cfg["wallet"]["worker_name"]
        threads = int(cfg["mining"]["threads"])
        cap = float(cfg["mining"]["max_cpu_percent"])
        temp = float(cfg["mining"]["temp_limit"])
        host = cfg["pool"]["host"]
        port = int(cfg["pool"]["port"])
    except (KeyError, ValueError) as e:
        fail(f"config.toml missing/invalid field: {e}")
        return False

    ok("config.toml valid")
    info(f"wallet  : {wallet[:8]}…{wallet[-6:]} ({worker})")
    info(f"pool    : {host}:{port}")
    info(f"workers : {threads}   cpu cap : {cap:.0f}%   temp limit : {temp:.0f}°C")

    if not (1 <= threads <= 256):
        fail(f"threads={threads} looks wrong (expected 1..256)")
        return False
    if not (1 <= cap <= 100):
        fail(f"max_cpu_percent={cap} out of range (1..100)")
        return False
    if len(wallet) < 90:
        print(f"{YELLOW}!{RESET} wallet_address looks short — double-check before real mining")
    if wallet == DEV_WALLET:
        fail("wallet_address is the placeholder Monero dev address — pools ban it. "
             "Set your own XMR address in config.toml before mining.")
        return False
    return True


def main():
    auto_install = "--install" in sys.argv
    print(f"{BLUE}== xmr-lottery-miner setup =={RESET}")
    results = [
        check_python(),
        check_deps(auto_install),
        check_randomx(),
        check_config(),
    ]
    print()
    if all(results):
        print(f"{GREEN}Ready.{RESET} Start mining with: {BLUE}python main.py{RESET}")
        return 0
    print(f"{RED}Setup incomplete — resolve the ✗ items above.{RESET}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
