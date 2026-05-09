"""Batch register loop. Claim pending accounts from `accounts.db`, register
mỗi cái bằng `register_elle.register_one(context, raw_email)`, restart browser
context mỗi batch_size accounts.

Status flow:
    pending --(claim_pending)--> registering --(register_one)-->
        awaiting_mail   (success: chờ IMAP listener nhận mail xác nhận)
        failed_register (fail: error stored, attempts++)

Multi-worker (Tier B):
    --workers N spawns N child processes, mỗi worker dùng `camoufox-profile-w<id>`
    riêng (qua env CAMOUFOX_USER_DATA_DIR). SQLite `claim_pending` đã atomic
    (BEGIN IMMEDIATE) nên các worker không double-claim. --total chia đều cho
    N worker (làm tròn lên).

Graceful shutdown:
- Ctrl+C ở parent: signal lan xuống children → mỗi worker revert
  'registering' của mình về 'pending', parent đợi join + final stats.
- Crash/kill: chạy lại với `--reset-stuck-min 15` để revert rows kẹt > 15 phút.

Usage:
    py scripts/register_batch.py                       # 1 worker
    py scripts/register_batch.py --workers 3           # 3 worker song song
    py scripts/register_batch.py --total 100 --workers 4
    py scripts/register_batch.py --batch-size 5 --sleep 8
"""

from __future__ import annotations

import argparse
import multiprocessing as mp
import os
import subprocess
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

REG_BOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Load .env SỚM (trước khi tính DEFAULT_*) để các giá trị BATCH_* thật sự áp dụng.
load_dotenv(REG_BOT_DIR / ".env")

import accounts_db  # noqa: E402  (after sys.path tweak)
from accounts_db import (  # noqa: E402
    STATUS_AWAITING_MAIL,
    STATUS_FAILED_REGISTER,
    STATUS_PENDING,
    STATUS_REGISTERING,
    append_event,
    claim_pending,
    connect,
    stats,
    update_status,
)
from register_elle import build_browser_context, register_one  # noqa: E402

try:
    DEFAULT_BATCH_SIZE = max(1, int(os.environ.get("BATCH_SIZE", "10")))
except ValueError:
    DEFAULT_BATCH_SIZE = 10
try:
    DEFAULT_SLEEP_SEC = max(0.0, float(os.environ.get("BATCH_SLEEP_SEC", "5")))
except ValueError:
    DEFAULT_SLEEP_SEC = 5.0
DEFAULT_PROFILE_PREFIX = os.environ.get("BATCH_PROFILE_PREFIX") or f"camoufox-profile-{os.getpid()}-w"
BATCH_LOG_LEVEL = os.environ.get("BATCH_LOG_LEVEL", "normal").strip().lower()
try:
    DEFAULT_WORKERS = max(1, int(os.environ.get("BATCH_WORKERS", "5")))
except ValueError:
    DEFAULT_WORKERS = 5


# --------------------------------------------------------------------------
# DB helpers
# --------------------------------------------------------------------------

def _reset_stuck(max_age_min: int) -> int:
    """Revert rows ở trạng thái 'registering' quá `max_age_min` phút về 'pending'."""
    cutoff = int(time.time()) - max_age_min * 60
    with connect() as conn:
        cur = conn.execute(
            """
            UPDATE accounts
            SET status = ?, error = 'reset-stuck-on-startup'
            WHERE status = ? AND (register_ts IS NULL OR register_ts < ?)
            """,
            (STATUS_PENDING, STATUS_REGISTERING, cutoff),
        )
        return cur.rowcount


def _release_claimed(emails: list[str]) -> int:
    """Trả 'registering' về 'pending' (cho graceful shutdown)."""
    if not emails:
        return 0
    placeholders = ",".join("?" * len(emails))
    with connect() as conn:
        cur = conn.execute(
            f"""
            UPDATE accounts SET status = ?
            WHERE status = ? AND raw_email IN ({placeholders})
            """,
            (STATUS_PENDING, STATUS_REGISTERING, *emails),
        )
        return cur.rowcount


def _format_secs(s: float) -> str:
    s = int(s)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m{sec:02d}s"
    if m:
        return f"{m}m{sec:02d}s"
    return f"{sec}s"


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _start_mail_listener(enabled: bool) -> subprocess.Popen | None:
    """Start check_elle_mail.py as a sibling process for this batch run."""
    if not enabled:
        return None
    env = os.environ.copy()
    env.setdefault("PYTHONIOENCODING", "utf-8")
    cmd = [sys.executable, str(REG_BOT_DIR / "scripts" / "check_elle_mail.py")]
    print(f"[mail-listener] starting: {' '.join(cmd)}")
    proc = subprocess.Popen(cmd, cwd=str(REG_BOT_DIR), env=env)
    time.sleep(1.0)
    if proc.poll() is not None:
        print(f"[mail-listener] WARN exited early code={proc.returncode}")
    return proc


def _stop_mail_listener(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    print("[mail-listener] stopping...")
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        print("[mail-listener] still alive, killing")
        proc.kill()
        proc.wait(timeout=5)


# --------------------------------------------------------------------------
# Worker stdout prefixing (multiprocess log clarity)
# --------------------------------------------------------------------------

class _PrefixedStream:
    """Wrap a stream, prepend `prefix` at every line start. Thread-safe enough
    cho child process đơn luồng (mỗi worker là 1 process độc lập)."""

    def __init__(self, base, prefix: str, log_level: str = "normal"):
        self._base = base
        self._prefix = prefix
        self._at_line_start = True
        self._log_level = log_level
        self._buf = ""

    def _should_write_line(self, line: str) -> bool:
        if self._log_level != "quiet":
            return True
        text = line.strip()
        if not text:
            return False
        keep_tokens = (
            "FATAL",
            "EXCEPTION",
            "FAIL",
            "TRANSIENT",
            "RELEASED",
            "WARN",
            "DONE",
            "progress",
            "reached --total",
            "pool empty",
            "KeyboardInterrupt",
        )
        return any(tok in text for tok in keep_tokens)

    def write(self, s: str) -> int:
        if not s:
            return 0
        if self._log_level == "quiet":
            self._buf += s
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                if self._should_write_line(line):
                    self._base.write(f"{self._prefix}{line}\n")
            return len(s)
        out: list[str] = []
        for ch in s:
            if self._at_line_start and ch != "\n":
                out.append(self._prefix)
                self._at_line_start = False
            out.append(ch)
            if ch == "\n":
                self._at_line_start = True
        return self._base.write("".join(out))

    def flush(self) -> None:
        if self._log_level == "quiet" and self._buf:
            line = self._buf
            self._buf = ""
            if self._should_write_line(line):
                self._base.write(f"{self._prefix}{line}")
        self._base.flush()

    def __getattr__(self, name):
        return getattr(self._base, name)


# --------------------------------------------------------------------------
# Core loop (used by single-process mode AND each worker)
# --------------------------------------------------------------------------

def _run_loop(args: argparse.Namespace, headless: bool, slow_mo_ms: int) -> int:
    """Single-worker register loop. Hết --total hoặc cạn pending thì dừng."""
    if args.reset_stuck_min > 0:
        n = _reset_stuck(args.reset_stuck_min)
        if n:
            print(f"[batch] reset-stuck: reverted {n} stale 'registering' row(s)")

    s0 = stats()
    print(f"[batch] start. pool stats: {s0}")
    print(f"[batch] config: batch_size={args.batch_size} sleep={args.sleep}s "
          f"total={args.total or 'unbounded'} headless={headless} log={BATCH_LOG_LEVEL}")

    done_ok = 0
    done_fail = 0
    start_ts = time.time()
    current_batch_emails: list[str] = []
    exit_code = 0

    try:
        while True:
            if args.total is not None:
                remaining = args.total - done_ok - done_fail
                if remaining <= 0:
                    print(f"[batch] reached --total {args.total}")
                    break
                n_to_claim = min(args.batch_size, remaining)
            else:
                n_to_claim = args.batch_size

            claimed = claim_pending(n_to_claim)
            if not claimed:
                print("[batch] pool empty (no pending rows). Done.")
                break

            current_batch_emails = [r["raw_email"] for r in claimed]
            batch_idx = (done_ok + done_fail) // args.batch_size + 1
            print(f"\n[batch #{batch_idx}] claimed {len(claimed)} account(s) — opening browser...")

            with build_browser_context(headless=headless, slow_mo_ms=slow_mo_ms) as ctx:
                for i, row in enumerate(claimed, 1):
                    raw = row["raw_email"]
                    print(f"[batch #{batch_idx}] [{i}/{len(claimed)}] -> {raw}")
                    try:
                        result = register_one(ctx, raw)
                    except KeyboardInterrupt:
                        raise
                    except Exception as e:
                        err_msg = f"exception: {type(e).__name__}: {e}"
                        print(f"[batch #{batch_idx}] EXCEPTION {raw}: {err_msg}")
                        update_status(raw, STATUS_FAILED_REGISTER, error=err_msg, bump_attempts=True)
                        append_event(raw, "register", ok=False, detail=err_msg)
                        done_fail += 1
                        if raw in current_batch_emails:
                            current_batch_emails.remove(raw)
                        continue

                    if result.ok:
                        update_status(
                            raw,
                            STATUS_AWAITING_MAIL,
                            error=None,
                            bump_attempts=True,
                            register_ts=int(time.time()),
                        )
                        append_event(raw, "register", ok=True, detail=f"latency_ms={result.latency_ms}")
                        done_ok += 1
                        print(f"[batch #{batch_idx}] OK {raw} ({result.latency_ms}ms)")
                    else:
                        err_str = (result.err or "unknown")
                        # 429 / 5xx = transient server-side, retry inline với backoff
                        is_transient = ("429" in err_str) or ("http-5" in err_str) or ("rate" in err_str.lower())
                        if is_transient:
                            retried_ok = False
                            for retry_idx in range(2):
                                backoff = 15 + retry_idx * 15  # 15s, 30s
                                print(f"[batch #{batch_idx}] TRANSIENT {raw}: {err_str[:80]} — sleep {backoff}s + retry {retry_idx+1}/2")
                                time.sleep(backoff)
                                try:
                                    result = register_one(ctx, raw)
                                except Exception as e:
                                    err_str = f"exception during retry: {e}"
                                    break
                                if result.ok:
                                    update_status(
                                        raw,
                                        STATUS_AWAITING_MAIL,
                                        error=None,
                                        bump_attempts=True,
                                        register_ts=int(time.time()),
                                    )
                                    append_event(raw, "register", ok=True,
                                                 detail=f"latency_ms={result.latency_ms} retry={retry_idx+1}")
                                    done_ok += 1
                                    print(f"[batch #{batch_idx}] OK (retry {retry_idx+1}) {raw} ({result.latency_ms}ms)")
                                    retried_ok = True
                                    break
                                err_str = (result.err or "unknown")
                                if not (("429" in err_str) or ("http-5" in err_str)):
                                    break  # non-transient, dừng retry
                            if not retried_ok:
                                # Vẫn 429 sau retry → release về pending để thử lại sau (KHÔNG mark failed)
                                _release_claimed([raw])
                                append_event(raw, "register", ok=False,
                                             detail=f"released-after-429-retries: {err_str[:200]}")
                                done_fail += 1
                                print(f"[batch #{batch_idx}] RELEASED (429) {raw} -> back to pending")
                        else:
                            update_status(
                                raw,
                                STATUS_FAILED_REGISTER,
                                error=err_str[:500],
                                bump_attempts=True,
                            )
                            append_event(raw, "register", ok=False, detail=result.err)
                            done_fail += 1
                            print(f"[batch #{batch_idx}] FAIL {raw}: {result.err}")

                    if raw in current_batch_emails:
                        current_batch_emails.remove(raw)

                    if args.sleep > 0 and i < len(claimed):
                        time.sleep(args.sleep)

            current_batch_emails = []
            elapsed = time.time() - start_ts
            done_total = done_ok + done_fail
            rate = done_total / max(elapsed, 1)
            total_part = f" done={done_total}/{args.total}" if args.total is not None else f" done={done_total}"
            print(f"[batch] progress{total_part} ok={done_ok} fail={done_fail} "
                  f"elapsed={_format_secs(elapsed)} rate={rate*60:.1f}/min")

    except KeyboardInterrupt:
        print(f"\n[batch] KeyboardInterrupt — releasing {len(current_batch_emails)} unfinished claim(s)")
        n = _release_claimed(current_batch_emails)
        print(f"[batch] released {n} row(s) back to 'pending'")
        exit_code = 130

    elapsed = time.time() - start_ts
    print(f"\n[batch] DONE ok={done_ok} fail={done_fail} elapsed={_format_secs(elapsed)}")
    return exit_code


# --------------------------------------------------------------------------
# Multi-worker pool
# --------------------------------------------------------------------------

def _worker_entry(
    worker_id: int,
    args: argparse.Namespace,
    profile_dir: str,
    headless: bool,
    slow_mo_ms: int,
) -> int:
    """Child process entry. Sets profile env, prefixes stdout, runs loop."""
    os.environ["CAMOUFOX_USER_DATA_DIR"] = profile_dir
    sys.stdout = _PrefixedStream(sys.__stdout__, f"[w{worker_id}] ", BATCH_LOG_LEVEL)
    sys.stderr = _PrefixedStream(sys.__stderr__, f"[w{worker_id}] ", BATCH_LOG_LEVEL)
    print(f"start. profile={profile_dir}  total={args.total}")
    try:
        return _run_loop(args, headless, slow_mo_ms)
    except KeyboardInterrupt:
        print("KeyboardInterrupt in worker")
        return 130
    except Exception as e:
        import traceback
        print(f"FATAL {type(e).__name__}: {e}")
        traceback.print_exc()
        return 1


def _run_pool(
    args: argparse.Namespace,
    n_workers: int,
    headless: bool,
    slow_mo_ms: int,
) -> int:
    """Spawn N workers, distribute --total, wait for join."""
    # Reset stuck ONCE in parent (workers skip)
    if args.reset_stuck_min > 0:
        n = _reset_stuck(args.reset_stuck_min)
        if n:
            print(f"[pool] reset-stuck: reverted {n} stale 'registering' row(s)")

    s0 = stats()
    print(f"[pool] start. pool stats: {s0}")
    print(f"[pool] config: workers={n_workers} batch_size={args.batch_size} "
          f"sleep={args.sleep}s total={args.total or 'unbounded'} headless={headless} log={BATCH_LOG_LEVEL}")

    # Distribute --total: ceil division so total accounts not under-counted
    per_worker_total: int | None = None
    if args.total is not None:
        per_worker_total = max(1, -(-args.total // n_workers))  # ceil div
        print(f"[pool] --total {args.total} split across {n_workers} => "
              f"~{per_worker_total}/worker (parent stops when pool empty either way)")

    worker_ns_dict = vars(args).copy()
    worker_ns_dict["reset_stuck_min"] = 0  # done in parent
    worker_ns_dict["total"] = per_worker_total
    worker_args = argparse.Namespace(**worker_ns_dict)

    procs: list[mp.Process] = []
    for wid in range(n_workers):
        profile_dir = str(REG_BOT_DIR / f"{args.profile_prefix}{wid}")
        proc = mp.Process(
            target=_worker_entry,
            args=(wid, worker_args, profile_dir, headless, slow_mo_ms),
            name=f"worker-{wid}",
        )
        proc.start()
        procs.append(proc)
        print(f"[pool] spawned worker {wid} pid={proc.pid} profile={profile_dir}")

    exit_code = 0
    try:
        for proc in procs:
            proc.join()
    except KeyboardInterrupt:
        # SIGINT đã được broadcast cho children (cùng process group),
        # nên child đã bắt đầu graceful release. Parent chỉ đợi.
        print("\n[pool] KeyboardInterrupt — waiting workers to release claims (max 30s each)...")
        for proc in procs:
            proc.join(timeout=30)
            if proc.is_alive():
                print(f"[pool] worker {proc.name} still alive, terminate")
                proc.terminate()
                proc.join(timeout=5)
                if proc.is_alive():
                    print(f"[pool] worker {proc.name} kill")
                    proc.kill()
        exit_code = 130

    survivors = [p for p in procs if p.exitcode != 0]
    if survivors:
        print(f"[pool] {len(survivors)} worker(s) exited non-zero: "
              + ", ".join(f"{p.name}={p.exitcode}" for p in survivors))

    print("\n[pool] all workers exited")
    print(f"[pool] final stats: {stats()}")
    return exit_code


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description="Batch register loop")
    p.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE,
                   help=f"Số account / lần restart browser (default {DEFAULT_BATCH_SIZE})")
    p.add_argument("--sleep", type=float, default=DEFAULT_SLEEP_SEC,
                   help=f"Sleep giữa mỗi account, giây (default {DEFAULT_SLEEP_SEC})")
    p.add_argument("--total", type=int,
                   help="Tổng số account run trong cycle này (chia đều cho workers). "
                        "Bỏ qua = chạy đến khi pool cạn.")
    p.add_argument("--reset-stuck-min", type=int, default=15,
                   help="Revert 'registering' rows > N phút về 'pending' khi start (default 15). 0 = bỏ qua.")
    p.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                   help=f"Số worker process song song (default {DEFAULT_WORKERS}, đọc BATCH_WORKERS từ .env). "
                        "Mỗi worker dùng profile riêng `<prefix><id>`.")
    p.add_argument("--profile-prefix", default=DEFAULT_PROFILE_PREFIX,
                    help=f"Prefix dir cho profile worker (default {DEFAULT_PROFILE_PREFIX!r}). "
                         "Mặc định có PID để nhiều PowerShell không dùng trùng profile. "
                         "Worker 0 sẽ dùng `<prefix>0`, ...")
    p.add_argument(
        "--no-mail-listener",
        action="store_true",
        help="Không tự chạy check_elle_mail.py song song (default: chạy chung).",
    )
    p.add_argument(
        "--with-mail-listener",
        action="store_true",
        help="Compat flag: mail listener đã bật mặc định.",
    )
    p.add_argument(
        "--verify-wait-sec",
        type=int,
        default=_env_int("BATCH_VERIFY_WAIT_SEC", 120),
        help="Sau khi batch xong, chờ mail listener xử lý thêm N giây rồi dừng (default đọc BATCH_VERIFY_WAIT_SEC hoặc 120).",
    )
    args = p.parse_args()

    if args.batch_size <= 0:
        print("[batch] --batch-size phải > 0", file=sys.stderr)
        return 2
    if args.workers < 1:
        print("[batch] --workers phải >= 1", file=sys.stderr)
        return 2

    # .env đã load ở module top — không cần load lại.
    headless = os.environ.get("HEADLESS", "true").lower() != "false"
    slow_mo_ms = int(os.environ.get("SLOW_MO_MS", "50"))

    auto_listener = _env_bool("BATCH_START_MAIL_LISTENER", True) and not args.no_mail_listener
    mail_proc = _start_mail_listener(auto_listener)
    exit_code = 0
    interrupted = False
    try:
        try:
            if args.workers == 1:
                exit_code = _run_loop(args, headless, slow_mo_ms)
            else:
                exit_code = _run_pool(args, args.workers, headless, slow_mo_ms)
        except KeyboardInterrupt:
            interrupted = True
            exit_code = 130

        if exit_code == 130:
            interrupted = True

        if not interrupted and mail_proc is not None and args.verify_wait_sec > 0:
            try:
                print(f"[mail-listener] wait {args.verify_wait_sec}s for pending verification...")
                time.sleep(args.verify_wait_sec)
            except KeyboardInterrupt:
                print("[mail-listener] wait interrupted by Ctrl+C")
                interrupted = True
                exit_code = 130

        return exit_code
    finally:
        _stop_mail_listener(mail_proc)


if __name__ == "__main__":
    # Required on Windows + PyInstaller-safe
    mp.freeze_support()
    raise SystemExit(main())
