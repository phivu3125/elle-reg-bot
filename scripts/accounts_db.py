"""SQLite helper for the email-account pool.

Schema (v2):
- `accounts`: 1 row per RAW email (e.g. chi7vlog+e00001@gmail.com). PK = raw_email.
  `canonical_email` là cột indexed cho IMAP grouping/stats (e.g. mọi alias của
  chi7vlog đều có canonical_email='chi7vlog@gmail.com').
- `events`: append-only audit trail.

DB path resolves to `Reg-Bot/accounts.db` (parent của thư mục `scripts`).

Lý do v2 đổi PK từ canonical → raw_email: khi sinh 10k alias từ 1 inbox Gmail,
tất cả canonical về cùng 1 địa chỉ → INSERT OR IGNORE chỉ giữ 1 row. Bot cần
tracking từng alias riêng biệt vì mỗi cái = 1 ELLE account.
"""

from __future__ import annotations

import sqlite3
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator, Optional

# Reg-Bot/ is the parent of scripts/
DB_PATH = Path(__file__).resolve().parent.parent / "accounts.db"
ACCOUNTS_TXT_DEFAULT = Path(__file__).resolve().parent.parent / "accounts.txt"

SCHEMA_VERSION = 2

# Status values
STATUS_PENDING = "pending"
STATUS_REGISTERING = "registering"
STATUS_AWAITING_MAIL = "awaiting_mail"
STATUS_AWAITING_VERIFY = "awaiting_verify"
STATUS_VERIFIED = "verified"
STATUS_EXPORTED_VERIFIED = "exported_verified"
STATUS_SESSION_SAVED = "session_saved"
STATUS_FAILED_REGISTER = "failed_register"
STATUS_FAILED_VERIFY = "failed_verify"
STATUS_EXPORTED_FAILED_VERIFY = "exported_failed_verify"

ALL_STATUSES = {
    STATUS_PENDING,
    STATUS_REGISTERING,
    STATUS_AWAITING_MAIL,
    STATUS_AWAITING_VERIFY,
    STATUS_VERIFIED,
    STATUS_EXPORTED_VERIFIED,
    STATUS_SESSION_SAVED,
    STATUS_FAILED_REGISTER,
    STATUS_FAILED_VERIFY,
    STATUS_EXPORTED_FAILED_VERIFY,
}

# Mapping: source status → exported target status (cho export-unexported flow)
_EXPORT_STATUS_MAP = {
    STATUS_VERIFIED: STATUS_EXPORTED_VERIFIED,
    STATUS_FAILED_VERIFY: STATUS_EXPORTED_FAILED_VERIFY,
}

# Columns on `accounts` that callers may update via update_status(**fields)
_UPDATABLE_FIELDS = {
    "canonical_email",
    "error",
    "register_ts",
    "mail_received_ts",
    "verified_ts",
    "vote_sid",
    "session_json",
    "session_ts",
    "attempts",
    "exported_ts",
}


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS accounts (
    raw_email TEXT PRIMARY KEY,
    canonical_email TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    error TEXT,
    register_ts INTEGER,
    mail_received_ts INTEGER,
    verified_ts INTEGER,
    vote_sid TEXT,
    session_json TEXT,
    session_ts INTEGER,
    attempts INTEGER NOT NULL DEFAULT 0,
    exported_ts INTEGER
);

CREATE INDEX IF NOT EXISTS idx_status ON accounts(status);
CREATE INDEX IF NOT EXISTS idx_canonical ON accounts(canonical_email);
CREATE INDEX IF NOT EXISTS idx_status_attempts ON accounts(status, attempts);
CREATE INDEX IF NOT EXISTS idx_status_exported ON accounts(status, exported_ts);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_email TEXT NOT NULL,
    event TEXT NOT NULL,
    ok INTEGER,
    detail TEXT,
    ts INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_email ON events(raw_email);

CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _norm_raw(email: str) -> str:
    """Lowercase + strip whitespace. Preserves dots and +tags."""
    return (email or "").strip().lower()


def canonical(email: str) -> str:
    """Canonicalize a Gmail-ish address: lowercase, strip dots in local part,
    map googlemail.com → gmail.com, drop +tag.

    Non-Gmail addresses get lowercased and trimmed only — no dot-stripping.
    """
    e = _norm_raw(email)
    if "@" not in e:
        return e
    local, domain = e.split("@", 1)
    if "+" in local:
        local = local.split("+", 1)[0]
    if domain == "googlemail.com":
        domain = "gmail.com"
    if domain == "gmail.com":
        local = local.replace(".", "")
    return f"{local}@{domain}"


def _migrate_if_old_schema(conn: sqlite3.Connection) -> None:
    """If DB still has v1 schema (PK=email, no canonical_email column), drop and
    recreate. Safe because v1 was only used for smoke testing — no production
    data had been imported.
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(accounts)").fetchall()}
    if not cols:
        return  # fresh DB
    if "canonical_email" not in cols:
        # v1 schema → drop and recreate
        conn.executescript(
            """
            DROP INDEX IF EXISTS idx_status;
            DROP INDEX IF EXISTS idx_status_attempts;
            DROP INDEX IF EXISTS idx_events_email;
            DROP TABLE IF EXISTS events;
            DROP TABLE IF EXISTS accounts;
            """
        )
        conn.commit()
        return

    if "exported_ts" not in cols:
        conn.execute("ALTER TABLE accounts ADD COLUMN exported_ts INTEGER")
        conn.commit()

    # Backfill DBs created before explicit exported_* statuses existed.
    conn.execute(
        "UPDATE accounts SET status = ? WHERE status = ? AND exported_ts IS NOT NULL",
        (STATUS_EXPORTED_VERIFIED, STATUS_VERIFIED),
    )
    conn.execute(
        "UPDATE accounts SET status = ? WHERE status = ? AND exported_ts IS NOT NULL",
        (STATUS_EXPORTED_FAILED_VERIFY, STATUS_FAILED_VERIFY),
    )
    conn.commit()


def get_db() -> sqlite3.Connection:
    """Return a connection with schema applied. Caller is responsible for closing
    (or use the `connect()` context manager)."""
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    _migrate_if_old_schema(conn)
    conn.executescript(SCHEMA_SQL)
    conn.execute(
        "INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('version', ?)",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()
    return conn


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    conn = get_db()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _row_to_dict(row: sqlite3.Row) -> dict:
    return {k: row[k] for k in row.keys()}


def import_pool(txt_path: str | Path | None = None) -> int:
    """Read `accounts.txt` (1 email/line, '#' comments + blank lines skipped) và
    INSERT OR IGNORE vào DB. Returns số dòng mới chèn được.

    Mỗi raw_email = 1 row riêng (không dedupe theo canonical).
    """
    path = Path(txt_path) if txt_path else ACCOUNTS_TXT_DEFAULT
    if not path.exists():
        raise FileNotFoundError(f"accounts file không tồn tại: {path}")

    inserted = 0
    with connect() as conn:
        cur = conn.cursor()
        with path.open("r", encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                # Cho phép TSV/CSV → chỉ lấy field đầu
                first = line.split()[0].split(",")[0].strip()
                raw = _norm_raw(first)
                if "@" not in raw:
                    continue
                canon = canonical(raw)
                if not canon:
                    continue
                cur.execute(
                    "INSERT OR IGNORE INTO accounts(raw_email, canonical_email, status) VALUES (?, ?, ?)",
                    (raw, canon, STATUS_PENDING),
                )
                if cur.rowcount > 0:
                    inserted += 1
    return inserted


def import_emails(emails: Iterable[str]) -> int:
    """Insert raw email iterable directly into DB as pending. Returns inserted count.

    Dùng cho terminal UI để generate alias rồi add thẳng vào DB, không cần
    ghi qua accounts.txt.
    """
    inserted = 0
    with connect() as conn:
        cur = conn.cursor()
        for item in emails:
            raw = _norm_raw(str(item))
            if "@" not in raw:
                continue
            canon = canonical(raw)
            if not canon:
                continue
            cur.execute(
                "INSERT OR IGNORE INTO accounts(raw_email, canonical_email, status) VALUES (?, ?, ?)",
                (raw, canon, STATUS_PENDING),
            )
            if cur.rowcount > 0:
                inserted += 1
    return inserted


def claim_pending(n: int) -> list[dict]:
    """Atomically chuyển n accounts từ 'pending' → 'registering' và trả về list
    rows đã claim. Sắp theo `attempts ASC, rowid ASC` (oldest, fewest retries first).
    """
    if n <= 0:
        return []
    now = int(time.time())
    with connect() as conn:
        cur = conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        rows = cur.execute(
            """
            SELECT raw_email FROM accounts
            WHERE status = ?
            ORDER BY attempts ASC, rowid ASC
            LIMIT ?
            """,
            (STATUS_PENDING, n),
        ).fetchall()
        emails = [r["raw_email"] for r in rows]
        if not emails:
            return []
        placeholders = ",".join("?" * len(emails))
        cur.execute(
            f"UPDATE accounts SET status = ?, register_ts = ? WHERE raw_email IN ({placeholders})",
            (STATUS_REGISTERING, now, *emails),
        )
        claimed = cur.execute(
            f"SELECT * FROM accounts WHERE raw_email IN ({placeholders})",
            emails,
        ).fetchall()
    return [_row_to_dict(r) for r in claimed]


def claim_awaiting_verify(emails_in_inbox: Iterable[str]) -> list[dict]:
    """Khi mail listener thấy mail mới, gọi với danh sách `To:` (raw form).
    Bất cứ row nào status='awaiting_mail' và raw_email khớp sẽ chuyển sang
    'awaiting_verify' (cùng update mail_received_ts).
    """
    raws = [_norm_raw(e) for e in emails_in_inbox if e]
    raws = [e for e in raws if "@" in e]
    if not raws:
        return []
    now = int(time.time())
    placeholders = ",".join("?" * len(raws))
    with connect() as conn:
        cur = conn.cursor()
        cur.execute(
            f"""
            UPDATE accounts
            SET status = ?, mail_received_ts = ?
            WHERE status = ? AND raw_email IN ({placeholders})
            """,
            (STATUS_AWAITING_VERIFY, now, STATUS_AWAITING_MAIL, *raws),
        )
        moved = cur.execute(
            f"""
            SELECT * FROM accounts
            WHERE status = ? AND raw_email IN ({placeholders})
            """,
            (STATUS_AWAITING_VERIFY, *raws),
        ).fetchall()
    return [_row_to_dict(r) for r in moved]


def update_status(
    email: str,
    status: str,
    error: Optional[str] = None,
    bump_attempts: bool = False,
    **fields,
) -> None:
    """Update status + tùy ý các field con. Key = raw_email.
    `bump_attempts=True` để +1 attempts.
    """
    if status not in ALL_STATUSES:
        raise ValueError(f"Unknown status: {status}")
    raw = _norm_raw(email)
    sets = ["status = ?"]
    args: list = [status]
    if error is not None:
        sets.append("error = ?")
        args.append(error)
    if bump_attempts:
        sets.append("attempts = attempts + 1")
    for key, val in fields.items():
        if key not in _UPDATABLE_FIELDS:
            raise ValueError(f"Field '{key}' không cho update qua update_status")
        sets.append(f"{key} = ?")
        args.append(val)
    args.append(raw)
    with connect() as conn:
        conn.execute(
            f"UPDATE accounts SET {', '.join(sets)} WHERE raw_email = ?",
            args,
        )


def append_event(email: str, event: str, ok: Optional[bool] = None, detail: Optional[str] = None) -> None:
    raw = _norm_raw(email)
    ok_int = None if ok is None else (1 if ok else 0)
    with connect() as conn:
        conn.execute(
            "INSERT INTO events(raw_email, event, ok, detail, ts) VALUES (?, ?, ?, ?, ?)",
            (raw, event, ok_int, detail, int(time.time())),
        )


def get_account(email: str) -> Optional[dict]:
    raw = _norm_raw(email)
    with connect() as conn:
        row = conn.execute("SELECT * FROM accounts WHERE raw_email = ?", (raw,)).fetchone()
    return _row_to_dict(row) if row else None


def stats() -> dict:
    """Trả dict {status: count, ...} + total + canonical_count."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS c FROM accounts GROUP BY status"
        ).fetchall()
        total = conn.execute("SELECT COUNT(*) AS c FROM accounts").fetchone()["c"]
        canonical_count = conn.execute(
            "SELECT COUNT(DISTINCT canonical_email) AS c FROM accounts"
        ).fetchone()["c"]
    out = {r["status"]: r["c"] for r in rows}
    out["_total"] = total
    out["_canonical_inboxes"] = canonical_count
    return out


def export_by_status(statuses: Iterable[str], use_raw: bool = True) -> list[dict]:
    """Trả list rows (dict) có status thuộc `statuses`.

    `use_raw=True` trả raw_email (đúng như user gõ trong accounts.txt /
    đúng alias đã đăng ký). `use_raw=False` trả canonical_email (gom theo inbox).
    """
    status_list = [s for s in statuses if s in ALL_STATUSES]
    if not status_list:
        return []
    placeholders = ",".join("?" * len(status_list))
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT raw_email, canonical_email, status, verified_ts, session_ts
            FROM accounts
            WHERE status IN ({placeholders})
            ORDER BY verified_ts DESC NULLS LAST, rowid DESC
            """,
            tuple(status_list),
        ).fetchall()
    out = []
    for r in rows:
        d = _row_to_dict(r)
        d["display_email"] = d["raw_email"] if use_raw else d["canonical_email"]
        out.append(d)
    return out


def export_unexported_by_status(
    source_status: str,
    out_path: str | Path,
    use_raw: bool = True,
) -> int:
    """Append accounts có status=`source_status` và exported_ts IS NULL vào
    `out_path` (append mode), rồi đổi status sang target tương ứng và set
    exported_ts. Idempotent: chạy nhiều lần không double-export.

    Hỗ trợ:
      - verified       → exported_verified
      - failed_verify  → exported_failed_verify

    Returns số account export được trong lần chạy này.
    """
    if source_status not in _EXPORT_STATUS_MAP:
        raise ValueError(
            f"Status '{source_status}' không hỗ trợ export-unexported. "
            f"Chỉ hỗ trợ: {sorted(_EXPORT_STATUS_MAP)}"
        )
    target_status = _EXPORT_STATUS_MAP[source_status]

    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    now = int(time.time())

    with connect() as conn:
        cur = conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        rows = cur.execute(
            """
            SELECT raw_email, canonical_email
            FROM accounts
            WHERE status = ? AND exported_ts IS NULL
            ORDER BY verified_ts ASC NULLS LAST, rowid ASC
            """,
            (source_status,),
        ).fetchall()

        if not rows:
            return 0

        emails = [r["raw_email"] if use_raw else r["canonical_email"] for r in rows]
        needs_leading_newline = False
        if path.exists() and path.stat().st_size > 0:
            with path.open("rb") as f:
                f.seek(-1, 2)
                needs_leading_newline = f.read(1) != b"\n"

        with path.open("a", encoding="utf-8", newline="\n") as f:
            if needs_leading_newline:
                f.write("\n")
            f.write("\n".join(emails) + "\n")

        raw_emails = [r["raw_email"] for r in rows]
        placeholders = ",".join("?" * len(raw_emails))
        cur.execute(
            f"UPDATE accounts SET status = ?, exported_ts = ? WHERE raw_email IN ({placeholders})",
            (target_status, now, *raw_emails),
        )
        event_name = f"export_{source_status}"
        cur.executemany(
            "INSERT INTO events(raw_email, event, ok, detail, ts) VALUES (?, ?, ?, ?, ?)",
            [(email, event_name, 1, f"out={path}", now) for email in raw_emails],
        )
        return len(rows)


def export_verified_unexported(out_path: str | Path, use_raw: bool = True) -> int:
    """Backward-compat wrapper: export verified → exported_verified."""
    return export_unexported_by_status(STATUS_VERIFIED, out_path, use_raw=use_raw)


def export_failed_verify_unexported(out_path: str | Path, use_raw: bool = True) -> int:
    """Export failed_verify (HTTP 400 verify nhưng login vẫn được) →
    exported_failed_verify. Cùng cơ chế append-only như export verified."""
    return export_unexported_by_status(STATUS_FAILED_VERIFY, out_path, use_raw=use_raw)


# ---------------------------------------------------------------------------
# CLI: hỗ trợ `py accounts_db.py import` / `stats` / `reset-stuck` / `export`
# ---------------------------------------------------------------------------

def _cli() -> int:
    import argparse

    p = argparse.ArgumentParser(description="accounts.db helper CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_imp = sub.add_parser("import", help="Import accounts.txt → DB")
    p_imp.add_argument("--file", default=str(ACCOUNTS_TXT_DEFAULT))

    sub.add_parser("stats", help="Print status breakdown")

    p_reset = sub.add_parser("reset-stuck", help="Đẩy registering → pending (sau crash)")
    p_reset.add_argument("--max-age-min", type=int, default=15)

    p_show = sub.add_parser("show", help="Show 1 account")
    p_show.add_argument("email")

    p_exp = sub.add_parser("export", help="Xuất email theo status (mặc định verified+session_saved)")
    p_exp.add_argument(
        "--status",
        default="verified,session_saved",
        help="Comma-separated. VD: verified | verified,session_saved | failed_register",
    )
    p_exp.add_argument("--out", help="File output. Mặc định in stdout.")
    p_exp.add_argument(
        "--canonical",
        action="store_true",
        help="Dùng canonical_email (gom theo inbox) thay vì raw_email gốc.",
    )

    p_exp_verified = sub.add_parser(
        "export-verified",
        help="Append verified chưa export vào file .txt rồi đổi status exported_verified",
    )
    p_exp_verified.add_argument(
        "--out",
        default="verified.txt",
        help="File .txt output append mode (default verified.txt)",
    )
    p_exp_verified.add_argument(
        "--canonical",
        action="store_true",
        help="Dùng canonical_email thay vì raw_email gốc.",
    )

    p_exp_failed = sub.add_parser(
        "export-failed-verify",
        help="Append failed_verify chưa export vào file .txt rồi đổi status exported_failed_verify",
    )
    p_exp_failed.add_argument(
        "--out",
        default="failed-verify.txt",
        help="File .txt output append mode (default failed-verify.txt)",
    )
    p_exp_failed.add_argument(
        "--canonical",
        action="store_true",
        help="Dùng canonical_email thay vì raw_email gốc.",
    )

    args = p.parse_args()

    if args.cmd == "import":
        n = import_pool(args.file)
        s = stats()
        print(f"[import] inserted {n} new account(s)")
        print(f"[stats] {s}")
        return 0

    if args.cmd == "stats":
        print(stats())
        return 0

    if args.cmd == "reset-stuck":
        cutoff = int(time.time()) - args.max_age_min * 60
        with connect() as conn:
            cur = conn.execute(
                """
                UPDATE accounts
                SET status = ?, error = 'reset-stuck'
                WHERE status = ? AND (register_ts IS NULL OR register_ts < ?)
                """,
                (STATUS_PENDING, STATUS_REGISTERING, cutoff),
            )
            print(f"[reset-stuck] reverted {cur.rowcount} row(s)")
        return 0

    if args.cmd == "show":
        acc = get_account(args.email)
        print(acc)
        return 0

    if args.cmd == "export":
        statuses = [s.strip() for s in args.status.split(",") if s.strip()]
        unknown = [s for s in statuses if s not in ALL_STATUSES]
        if unknown:
            print(f"[export] WARN status không hợp lệ (bỏ qua): {unknown}")
        rows = export_by_status(statuses, use_raw=not args.canonical)
        lines = [r["display_email"] for r in rows]
        if args.out:
            Path(args.out).write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
            print(f"[export] {len(lines)} email -> {args.out}  (status={statuses})")
        else:
            for line in lines:
                print(line)
            print(f"[export] {len(lines)} email (status={statuses})", file=sys.stderr)
        return 0

    if args.cmd == "export-verified":
        n = export_verified_unexported(args.out, use_raw=not args.canonical)
        print(f"[export-verified] appended {n} verified account(s) -> {args.out}")
        if n == 0:
            print("[export-verified] no new verified accounts to export")
        return 0

    if args.cmd == "export-failed-verify":
        n = export_failed_verify_unexported(args.out, use_raw=not args.canonical)
        print(f"[export-failed-verify] appended {n} failed_verify account(s) -> {args.out}")
        if n == 0:
            print("[export-failed-verify] no new failed_verify accounts to export")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(_cli())
