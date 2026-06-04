# xmr-lottery-miner

A small, readable Monero (XMR) CPU miner with a live terminal dashboard. It runs
the full mining pipeline for real — Stratum login, real jobs, a CPU-usage
governor, a thermal governor, and share submission — and frames solo mining as a
"lottery": every hash is a ticket.

It has two hashing backends, selected automatically at startup:

| Backend | When | Result |
|---|---|---|
| **RandomX** (real rx/0 PoW) | `pyrx` is installed | Shares are valid; the pool **accepts** them |
| **BLAKE2b stand-in** (demo) | `pyrx` is absent | Everything runs, but the pool **rejects** shares as invalid PoW |

## Configure

Edit `config.toml` — set `wallet_address` to **your own** Monero address (the
shipped one is a placeholder the pool bans), pick `threads`, and tune the
`max_cpu_percent` / `temp_limit` governors.

## Quick start (demo mode)

```bash
python3 -m venv .venv
.venv/bin/pip install rich psutil
.venv/bin/python setup.py        # verifies deps + config; reports the backend
.venv/bin/python main.py         # press q (or Ctrl-C) to quit
```

In demo mode you'll see the dashboard, real jobs, and real CPU burn, but
`accepted` stays 0 and `rejected` may climb — the stand-in isn't valid RandomX.

## Real mining (RandomX)

`pip install pyrx` does **not** work: that PyPI name is an unrelated schema
validator, and the real bindings (github.com/jtgrassie/pyrx) need source build
fixes on Python 3.11+. The included script handles all of it:

```bash
./build_randomx.sh               # clones, patches, builds pyrx into ./.venv
.venv/bin/python setup.py        # should now print: pyrx (RandomX) installed
.venv/bin/python main.py
```

Notes:
- **Memory:** RandomX runs in fast mode (~2 GB dataset **per worker process**).
  Size `threads` to your RAM.
- **Hashrate vs. odds:** pure-Python orchestration plus the CPU cap yields a few
  hundred H/s. Pool share difficulty is ~1.28M, so an *accepted* share is a
  matter of letting it run (tens of minutes to hours) — it's a lottery, by
  design. The dashboard's "lottery / odds" panel shows the live estimate.

## Files

- `main.py` — entry point: loads config, starts the miner, drives the dashboard.
- `xmr_miner_core.py` — Stratum client, worker pool, CPU/thermal governors, both
  hash backends.
- `dashboard.py` — Rich TUI (hashrate sparkline, shares, lottery odds, job, system).
- `setup.py` — preflight: checks Python, deps, the active backend, and config.
- `build_randomx.sh` — one-shot builder for the real RandomX backend.
- `config.toml` — wallet, mining, and pool settings.

## License

[MIT](LICENSE).
