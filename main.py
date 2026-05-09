from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
SCRIPTS = ROOT / "scripts"
ENV_PATH = ROOT / ".env"
sys.path.insert(0, str(SCRIPTS))

load_dotenv(ENV_PATH)

import accounts_db  # noqa: E402
from gen_aliases import generate_aliases  # noqa: E402


def _print_header(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def _input_int(prompt: str, default: int | None = None, min_value: int = 0) -> int | None:
    suffix = f" [{default}]" if default is not None else ""
    while True:
        raw = input(f"{prompt}{suffix}: ").strip()
        if not raw and default is not None:
            return default
        if not raw and default is None:
            return None
        try:
            val = int(raw)
        except ValueError:
            print("Nhập số hợp lệ.")
            continue
        if val < min_value:
            print(f"Giá trị phải >= {min_value}.")
            continue
        return val


def _input_text(prompt: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    raw = input(f"{prompt}{suffix}: ").strip()
    return raw or (default or "")


def _run_script(script: str, *args: str) -> int:
    env = os.environ.copy()
    env.setdefault("PYTHONIOENCODING", "utf-8")
    cmd = [sys.executable, str(SCRIPTS / script), *args]
    print("\n[run] " + " ".join(cmd))
    try:
        return subprocess.call(cmd, cwd=str(ROOT), env=env)
    except KeyboardInterrupt:
        print("\n[run] Ctrl+C — đợi script con thoát...")
        return 130


def show_stats() -> None:
    _print_header("DB Stats")
    s = accounts_db.stats()
    for key, value in sorted(s.items()):
        print(f"{key:>20}: {value}")


def run_register() -> None:
    _print_header("Register batch")
    show_stats()
    default_total = int(os.environ.get("UI_DEFAULT_TOTAL", "100"))
    total = _input_int("Total account muốn chạy (blank = default)", default_total, min_value=1)
    extra: list[str] = []
    if total:
        extra.extend(["--total", str(total)])

    print("\nMail listener sẽ chạy chung nếu register_batch.py đang bật default.")
    code = _run_script("register_batch.py", *extra)
    print(f"\n[done] register_batch exit code={code}")
    show_stats()


def run_mail_listener() -> None:
    _print_header("Mail verifier")
    print("Ctrl+C để dừng listener.")
    _run_script("check_elle_mail.py")


def export_verified() -> None:
    _print_header("Export verified chưa export")
    default_out = os.environ.get("UI_EXPORT_OUT", str(ROOT / "output" / "verified-accounts.txt"))
    out = _input_text("Output .txt", default_out)
    n = accounts_db.export_verified_unexported(out)
    print(f"[export] appended {n} verified account(s) -> {out}")
    show_stats()


def _existing_raw_emails() -> set[str]:
    with accounts_db.connect() as conn:
        rows = conn.execute("SELECT raw_email FROM accounts").fetchall()
    return {r["raw_email"] for r in rows}


def generate_aliases_to_db() -> None:
    _print_header("Generate aliases → DB trực tiếp")
    base = _input_text("Base Gmail (blank = IMAP_USER trong .env)", os.environ.get("IMAP_USER", ""))
    if not base:
        print("[gen] thiếu base email. Set IMAP_USER trong .env hoặc nhập ở prompt.")
        return
    count = _input_int("Số alias mới cần thêm", 1000, min_value=1)
    if not count:
        return

    existing = _existing_raw_emails()
    target_pool = len(existing) + count + 512
    print(f"[gen] existing={len(existing)} cần thêm={count}; generating candidate pool={target_pool}")

    aliases = generate_aliases(base_email=base, count=target_pool)
    new_aliases = [a for a in aliases if a not in existing][:count]
    if len(new_aliases) < count:
        target_pool = len(existing) + count * 3 + 2048
        print(f"[gen] chưa đủ alias mới, retry candidate pool={target_pool}")
        aliases = generate_aliases(base_email=base, count=target_pool)
        new_aliases = [a for a in aliases if a not in existing][:count]

    if not new_aliases:
        print("[gen] không có alias mới để insert.")
        return

    inserted = accounts_db.import_emails(new_aliases)
    print(f"[gen] inserted {inserted}/{len(new_aliases)} alias mới vào DB")
    print(f"[gen] sample: {new_aliases[0]} ... {new_aliases[-1]}")
    show_stats()


def reset_stuck() -> None:
    _print_header("Reset stuck registering")
    max_age = _input_int("Max age phút", 15, min_value=0)
    code = _run_script("accounts_db.py", "reset-stuck", "--max-age-min", str(max_age or 0))
    print(f"[done] reset-stuck exit code={code}")
    show_stats()


def menu() -> int:
    while True:
        _print_header("ELLE Reg-Bot Terminal UI")
        print("1) Register batch")
        print("2) Mail verifier only")
        print("3) Export verified → txt")
        print("4) Generate aliases → DB")
        print("5) Show DB stats")
        print("6) Reset stuck registering")
        print("0) Exit")
        try:
            choice = input("Chọn: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n[main] thoát.")
            return 0
        try:
            if choice == "1":
                run_register()
            elif choice == "2":
                run_mail_listener()
            elif choice == "3":
                export_verified()
            elif choice == "4":
                generate_aliases_to_db()
            elif choice == "5":
                show_stats()
            elif choice == "6":
                reset_stuck()
            elif choice == "0":
                return 0
            else:
                print("Chọn không hợp lệ.")
        except KeyboardInterrupt:
            print("\n[main] Ctrl+C — quay lại menu. Bấm 0 để thoát.")


if __name__ == "__main__":
    raise SystemExit(menu())
