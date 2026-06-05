"""
xmr_miner_core.py — lightweight Monero CPU miner core.

Responsibilities:
  * Stratum v1 client (login / job / submit) over a raw TCP socket.
  * A pool of worker processes that hash the current job's blob over a nonce range.
  * A CPU-usage governor (psutil) that throttles workers via sleep intervals to
    keep total CPU under `max_cpu_percent`.
  * A thermal governor that parks workers when CPU temperature exceeds `temp_limit`.
  * A shared snapshot of live stats for the Rich dashboard to render.

NOTE ON HASHING
---------------
Real Monero proof-of-work is RandomX (rx/0), a full virtual machine that is not
practical to implement in pure Python. This core supports two backends, picked
automatically at import time:

  * RandomX (real PoW) — if the optional `pyrx` package is installed, each nonce
    is hashed with `pyrx.get_rx_hash(blob, seed_hash, height)`. Shares produced
    this way are valid and will be accepted by the pool.
  * BLAKE2b stand-in (demo) — if `pyrx` is unavailable, the core falls back to an
    iterated BLAKE2b over the real blob+nonce. Everything else (login, jobs,
    throttling, submit) runs for real, but the pool rejects these shares as
    invalid PoW.

Install the real backend with:  pip install pyrx   (needs a C/C++ toolchain).
Heads-up: RandomX allocates a per-process cache (~256 MB light mode), so memory
scales with the worker count.
"""

from __future__ import annotations

import hashlib
import json
import multiprocessing as mp
import socket
import threading
import time
from collections import deque
from dataclasses import dataclass, field

# Standard Monero block-blob nonce offset: 4 bytes at byte 39.
NONCE_OFFSET = 39
NONCE_LEN = 4

# How many hashes a worker computes before checking throttle/job/stop signals.
# RandomX is orders of magnitude slower per hash, so it uses a smaller batch to
# stay responsive to stop/job/throttle signals.
HASH_BATCH = 256
RANDOMX_BATCH = 64

# Optional real-PoW backend. Present -> valid shares; absent -> BLAKE2b stand-in.
#
# NOTE: the RandomX bindings are jtgrassie/pyrx, which is NOT on PyPI. (The PyPI
# package literally named "pyrx" is an unrelated JSON-schema validator.) So we
# don't just check that `import pyrx` works — we check that the module actually
# exposes get_rx_hash(). That way installing the wrong "pyrx" can't trick the
# workers into calling a function that doesn't exist.
try:
    import pyrx as _pyrx

    HAVE_RANDOMX = callable(getattr(_pyrx, "get_rx_hash", None))
except Exception:  # ImportError, or a broken/partial build
    _pyrx = None
    HAVE_RANDOMX = False


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
@dataclass
class MinerConfig:
    wallet_address: str
    worker_name: str
    threads: int
    max_cpu_percent: float
    temp_limit: float
    host: str
    port: int
    password: str = "x"

    @property
    def login(self) -> str:
        return f"{self.wallet_address}.{self.worker_name}"


# --------------------------------------------------------------------------- #
# Hashing (RandomX stand-in — see module docstring)
# --------------------------------------------------------------------------- #
def _insert_nonce(blob: bytes, nonce: int) -> bytes:
    n = nonce.to_bytes(NONCE_LEN, "little")
    return blob[:NONCE_OFFSET] + n + blob[NONCE_OFFSET + NONCE_LEN:]


def _hash_nonce(blob_with_nonce: bytes, seed: bytes) -> bytes:
    """Deterministic, CPU-heavy stand-in for RandomX. Returns 32 bytes."""
    h = hashlib.blake2b(blob_with_nonce, digest_size=32, key=seed[:16] or b"\x00").digest()
    # A few extra rounds so a worker actually consumes a CPU core.
    for _ in range(3):
        h = hashlib.blake2b(h, digest_size=32).digest()
    return h


def _compute_hash(blob_with_nonce: bytes, seed: bytes, height: int) -> bytes:
    """Hash one candidate, using RandomX when available else the stand-in.

    Returns 32 bytes. RandomX needs a non-empty seed_hash; without one we fall
    back to the stand-in so a worker never crashes on a malformed job.
    """
    if HAVE_RANDOMX and seed:
        return _pyrx.get_rx_hash(blob_with_nonce, seed, height)
    return _hash_nonce(blob_with_nonce, seed)


def _target_to_target64(target_hex: str) -> int:
    """Expand a stratum target to the 64-bit ceiling used for share checks.

    Pools send a little-endian compact target. A 4-byte target is the xmrig
    convention: difficulty = 0xFFFFFFFF / t32, and the 64-bit target is
    0xFFFFFFFFFFFFFFFF / difficulty. An 8-byte target is already 64-bit.
    """
    raw = bytes.fromhex(target_hex)
    if len(raw) == 4:
        t32 = int.from_bytes(raw, "little")
        if t32 == 0:
            return (1 << 64) - 1
        return 0xFFFFFFFFFFFFFFFF // (0xFFFFFFFF // t32)
    return int.from_bytes(raw[:8], "little")


def _target_to_difficulty(target_hex: str) -> int:
    """Pool difficulty implied by a stratum target (for display)."""
    raw = bytes.fromhex(target_hex)
    if len(raw) == 4:
        t32 = int.from_bytes(raw, "little")
        return (0xFFFFFFFF // t32) if t32 else 0
    t64 = int.from_bytes(raw[:8], "little")
    return (0xFFFFFFFFFFFFFFFF // t64) if t64 else 0


# --------------------------------------------------------------------------- #
# Worker process
# --------------------------------------------------------------------------- #
def _worker_loop(idx, job, control, hash_counts, found_q):
    """One mining worker. Reads the shared `job` dict and burns nonces."""
    nonce = idx * 0x10000000  # spread workers across the nonce space
    local_count = 0
    last_flush = time.time()
    last_job_ver = -1
    blob = b""
    seed = b""
    height = 0
    target64 = (1 << 64) - 1
    job_id = ""
    batch = RANDOMX_BATCH if HAVE_RANDOMX else HASH_BATCH

    while not control["stop"]:
        # Park if the governor disabled this worker (thermal / cpu) — but keep looping.
        if not control["active"][idx]:
            time.sleep(0.25)
            continue

        # Pick up a fresh job if the version changed.
        ver = control["job_ver"]
        if ver != last_job_ver:
            j = dict(job)  # snapshot of the Manager dict
            if j.get("blob"):
                blob = bytes.fromhex(j["blob"])
                seed = bytes.fromhex(j.get("seed_hash", "") or "")
                height = int(j.get("height") or 0)
                target64 = _target_to_target64(j["target"])
                job_id = j["job_id"]
                nonce = idx * 0x10000000  # restart nonce window for the new job
            last_job_ver = ver

        if not blob:
            time.sleep(0.2)
            continue

        # Hash a batch.
        for _ in range(batch):
            cand = _insert_nonce(blob, nonce & 0xFFFFFFFF)
            digest = _compute_hash(cand, seed, height)
            # Pool convention: low 64 bits live in the last 8 bytes, little-endian.
            if int.from_bytes(digest[24:32], "little") < target64:
                found_q.put({
                    "job_id": job_id,
                    "nonce": (nonce & 0xFFFFFFFF).to_bytes(NONCE_LEN, "little").hex(),
                    "result": digest.hex(),
                    "worker": idx,
                })
            nonce += 1
            local_count += 1

        # Flush hash count to the shared array roughly twice a second.
        now = time.time()
        if now - last_flush >= 0.5:
            hash_counts[idx] = local_count
            last_flush = now

        # Throttle: sleep proportional to the governor's directive.
        sleep_for = control["throttle_sleep"]
        if sleep_for > 0:
            time.sleep(sleep_for)

    hash_counts[idx] = local_count


# --------------------------------------------------------------------------- #
# Stratum client
# --------------------------------------------------------------------------- #
class StratumClient:
    def __init__(self, cfg: MinerConfig):
        self.cfg = cfg
        self.sock = None
        self.login_id = None
        self._rpc_id = 1
        self._buf = b""

    def connect(self):
        self.sock = socket.create_connection((self.cfg.host, self.cfg.port), timeout=30)
        self._send({
            "id": self._next_id(),
            "jsonrpc": "2.0",
            "method": "login",
            "params": {
                "login": self.cfg.login,
                "pass": self.cfg.password,
                "agent": "xmr-lottery-miner/0.1",
                "algo": ["rx/0"],
            },
        })
        resp = self._recv_one()
        if not resp or resp.get("error"):
            raise RuntimeError(f"Pool login failed: {resp.get('error') if resp else 'no response'}")
        result = resp["result"]
        self.login_id = result["id"]
        return result.get("job")

    def submit(self, share):
        self._send({
            "id": self._next_id(),
            "jsonrpc": "2.0",
            "method": "submit",
            "params": {
                "id": self.login_id,
                "job_id": share["job_id"],
                "nonce": share["nonce"],
                "result": share["result"],
            },
        })

    def poll(self, timeout=1.0):
        """Return the next decoded message from the pool, or None on timeout."""
        return self._recv_one(timeout=timeout)

    # -- internals ---------------------------------------------------------- #
    def _next_id(self):
        i = self._rpc_id
        self._rpc_id += 1
        return i

    def _send(self, obj):
        data = (json.dumps(obj) + "\n").encode()
        self.sock.sendall(data)

    def _recv_one(self, timeout=30):
        self.sock.settimeout(timeout)
        while b"\n" not in self._buf:
            try:
                chunk = self.sock.recv(4096)
            except socket.timeout:
                return None
            if not chunk:
                raise ConnectionError("Pool closed the connection")
            self._buf += chunk
        line, self._buf = self._buf.split(b"\n", 1)
        line = line.strip()
        if not line:
            return None
        return json.loads(line)

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass


# --------------------------------------------------------------------------- #
# Miner orchestrator
# --------------------------------------------------------------------------- #
@dataclass
class _Stats:
    accepted: int = 0
    rejected: int = 0
    found: int = 0
    height: int = 0
    job_id: str = ""
    difficulty: int = 0
    connected: bool = False
    pool_msg: str = "starting…"
    active_workers: int = 0
    cpu_percent: float = 0.0
    temp: float = 0.0
    hashrate: float = 0.0
    hr_history: deque = field(default_factory=lambda: deque(maxlen=60))


class Miner:
    def __init__(self, cfg: MinerConfig):
        self.cfg = cfg
        self.mgr = mp.Manager()
        self.job = self.mgr.dict(
            {"blob": "", "seed_hash": "", "target": "", "job_id": "", "height": 0}
        )
        self.control = self.mgr.dict({
            "stop": False,
            "throttle_sleep": 0.0,
            "job_ver": 0,
            "active": self.mgr.list([True] * cfg.threads),
        })
        self.hash_counts = mp.Array("Q", cfg.threads)
        self.found_q = mp.Queue()
        self.procs = []
        self.stats = _Stats()
        self._stratum = None
        self._threads = []
        self._last_counts = [0] * cfg.threads
        self._last_sample = time.time()
        self._lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------- #
    def start(self):
        for i in range(self.cfg.threads):
            p = mp.Process(
                target=_worker_loop,
                args=(i, self.job, self.control, self.hash_counts, self.found_q),
                daemon=True,
            )
            p.start()
            self.procs.append(p)

        self._threads = [
            threading.Thread(target=self._stratum_loop, daemon=True),
            threading.Thread(target=self._governor_loop, daemon=True),
            threading.Thread(target=self._sampler_loop, daemon=True),
            threading.Thread(target=self._submit_loop, daemon=True),
        ]
        for t in self._threads:
            t.start()

    def stop(self):
        # Ask workers to exit via the shared flag. The Manager may already be
        # gone (e.g. systemd SIGTERMs the whole process group at once), so don't
        # depend on this succeeding — we terminate the processes directly below.
        try:
            self.control["stop"] = True
        except Exception:
            pass
        # Terminate worker processes via their own handles (no Manager needed).
        for p in self.procs:
            p.join(timeout=2)
            if p.is_alive():
                p.terminate()
        for p in self.procs:  # escalate to SIGKILL for anything still alive
            p.join(timeout=2)
            if p.is_alive():
                p.kill()
        if self._stratum:
            self._stratum.close()
        try:
            self.mgr.shutdown()
        except Exception:
            pass

    def snapshot(self):
        with self._lock:
            s = self.stats
            return {
                "accepted": s.accepted, "rejected": s.rejected, "found": s.found,
                "height": s.height, "job_id": s.job_id, "difficulty": s.difficulty,
                "connected": s.connected, "pool_msg": s.pool_msg,
                "active_workers": s.active_workers, "total_workers": self.cfg.threads,
                "cpu_percent": s.cpu_percent, "temp": s.temp,
                "hashrate": s.hashrate, "hr_history": list(s.hr_history),
            }

    # -- background loops --------------------------------------------------- #
    def _apply_job(self, j):
        if not j:
            return
        self.job["blob"] = j.get("blob", "")
        self.job["seed_hash"] = j.get("seed_hash", "")
        self.job["target"] = j.get("target", "")
        self.job["job_id"] = j.get("job_id", "")
        self.job["height"] = int(j.get("height") or 0)
        self.control["job_ver"] = self.control["job_ver"] + 1
        with self._lock:
            self.stats.job_id = j.get("job_id", "")
            self.stats.height = j.get("height", self.stats.height)
            tgt = j.get("target", "")
            if tgt:
                self.stats.difficulty = _target_to_difficulty(tgt)

    def _stratum_loop(self):
        backoff = 2
        while not self.control["stop"]:
            try:
                self._stratum = StratumClient(self.cfg)
                with self._lock:
                    self.stats.pool_msg = f"connecting to {self.cfg.host}:{self.cfg.port}…"
                job = self._stratum.connect()
                with self._lock:
                    self.stats.connected = True
                    self.stats.pool_msg = "logged in — mining"
                self._apply_job(job)
                backoff = 2
                while not self.control["stop"]:
                    msg = self._stratum.poll(timeout=1.0)
                    if msg is None:
                        continue
                    self._handle_pool_msg(msg)
            except (OSError, ConnectionError, RuntimeError, ValueError) as e:
                with self._lock:
                    self.stats.connected = False
                    self.stats.pool_msg = f"disconnected: {e} — retrying in {backoff}s"
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)

    def _handle_pool_msg(self, msg):
        method = msg.get("method")
        if method == "job":
            self._apply_job(msg.get("params"))
        elif "result" in msg and msg.get("result") is not None:
            res = msg["result"]
            if isinstance(res, dict) and res.get("status") == "OK":
                with self._lock:
                    self.stats.accepted += 1
                    self.stats.pool_msg = "share accepted"
            elif isinstance(res, dict) and "job" in res:
                self._apply_job(res["job"])
        elif msg.get("error"):
            with self._lock:
                self.stats.rejected += 1
                self.stats.pool_msg = f"share rejected: {msg['error']}"

    def _submit_loop(self):
        while not self.control["stop"]:
            try:
                share = self.found_q.get(timeout=1.0)
            except Exception:
                continue
            with self._lock:
                self.stats.found += 1
            if self._stratum and self.stats.connected:
                try:
                    self._stratum.submit(share)
                except OSError:
                    pass

    def _governor_loop(self):
        """Throttle workers to honour max_cpu_percent and park them above temp_limit."""
        import psutil  # imported here so the core file imports even if psutil is missing
        while not self.control["stop"]:
            cpu = psutil.cpu_percent(interval=1.0)
            temp = _read_cpu_temp(psutil)

            # CPU governor: nudge the shared sleep interval toward the target.
            sleep = self.control["throttle_sleep"]
            if cpu > self.cfg.max_cpu_percent + 3:
                sleep = min(sleep + 0.01, 0.5)
            elif cpu < self.cfg.max_cpu_percent - 3:
                sleep = max(sleep - 0.01, 0.0)
            self.control["throttle_sleep"] = round(sleep, 4)

            # Thermal governor: park workers while over the limit, restore when cool.
            active = list(self.control["active"])
            if temp and temp > self.cfg.temp_limit:
                for i in range(len(active) - 1, 0, -1):  # keep at least worker 0
                    if active[i]:
                        active[i] = False
                        break
            elif temp and temp < self.cfg.temp_limit - 5:
                for i in range(len(active)):
                    if not active[i]:
                        active[i] = True
                        break
            self.control["active"] = self.mgr.list(active)

            with self._lock:
                self.stats.cpu_percent = cpu
                self.stats.temp = temp or 0.0
                self.stats.active_workers = sum(1 for a in active if a)

    def _sampler_loop(self):
        while not self.control["stop"]:
            time.sleep(2.0)
            now = time.time()
            dt = now - self._last_sample
            if dt <= 0:
                continue
            counts = list(self.hash_counts)
            delta = sum(c - p for c, p in zip(counts, self._last_counts))
            hr = delta / dt
            self._last_counts = counts
            self._last_sample = now
            with self._lock:
                self.stats.hashrate = hr
                self.stats.hr_history.append(hr)


def _read_cpu_temp(psutil):
    """Best-effort CPU temperature in Celsius, or None if unavailable."""
    fn = getattr(psutil, "sensors_temperatures", None)
    if not fn:
        return None
    try:
        temps = fn()
    except Exception:
        return None
    for key in ("coretemp", "k10temp", "cpu_thermal", "acpitz", "zenpower"):
        if key in temps and temps[key]:
            return max(t.current for t in temps[key] if t.current)
    # Fall back to the first sensor we can find.
    for entries in temps.values():
        for t in entries:
            if t.current:
                return t.current
    return None
