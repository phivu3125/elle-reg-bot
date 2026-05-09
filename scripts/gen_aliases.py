"""Generate Gmail aliases from a base inbox và ghi ra accounts.txt.

Cách sinh: kết hợp **dot variants × domain swap × +tag counter** để mỗi raw
alias là duy nhất nhưng đều route về cùng 1 Gmail inbox.

Ví dụ với base `chi7vlog@gmail.com`:
    chi7vlog+e00000@gmail.com
    c.hi7vlog+e00000@gmail.com
    ch.i7vlog+e00000@gmail.com
    ...                                          (128 dot variants)
    chi7vlog+e00000@googlemail.com
    ...                                          (× 2 domains = 256)
    chi7vlog+e00001@gmail.com                    (counter+1, lặp lại)

Output 1 alias / dòng vào accounts.txt (UTF-8). Mặc định base lấy từ
`IMAP_USER` trong `.env` của Reg-Bot.

Usage:
    py scripts/gen_aliases.py --count 10000
    py scripts/gen_aliases.py --count 100 --base your@gmail.com --out alt.txt
    py scripts/gen_aliases.py --count 10000 --shuffle --seed 42
"""

from __future__ import annotations

import argparse
import os
import random
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None  # type: ignore

REG_BOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_OUT = REG_BOT_DIR / "accounts.txt"
DEFAULT_ENV = REG_BOT_DIR / ".env"

GMAIL_DOMAINS = ("gmail.com", "googlemail.com")
MAX_DOT_BITS = 7  # cap dot variants at 2^7 = 128 to bound combinatorics


def _clean_local(local: str) -> str:
    """Strip dots and any +tag from a local-part."""
    local = local.split("+", 1)[0]
    local = local.replace(".", "")
    return local


def _dot_variants(local: str, max_bits: int = MAX_DOT_BITS) -> list[str]:
    """Sinh tối đa 2^max_bits dot variants của `local`. Nếu local có < max_bits+1
    ký tự, sinh đủ 2^(len-1) (tránh out-of-bound bit)."""
    n = len(local)
    if n <= 1:
        return [local]
    bits = min(n - 1, max_bits)
    count = 1 << bits
    seen: set[str] = set()
    variants: list[str] = []
    for i in range(count):
        out = [local[0]]
        for j in range(1, n):
            if j - 1 < bits and (i >> (j - 1)) & 1:
                out.append(".")
            out.append(local[j])
        v = "".join(out)
        if v not in seen:
            seen.add(v)
            variants.append(v)
    return variants


def generate_aliases(
    base_email: str,
    count: int,
    use_googlemail: bool = True,
) -> list[str]:
    """Trả list các raw alias unique. Đảm bảo `count` alias hoặc raise nếu base
    quá ngắn để sinh đủ.
    """
    base = base_email.strip().lower()
    if "@" not in base:
        raise ValueError(f"Base email không hợp lệ: {base_email!r}")
    local, domain = base.split("@", 1)
    local = _clean_local(local)
    if not local:
        raise ValueError("Local-part rỗng sau khi strip dots/+tag.")

    if domain in GMAIL_DOMAINS:
        domains = list(GMAIL_DOMAINS) if use_googlemail else ["gmail.com"]
    else:
        domains = [domain]

    dot_vars = _dot_variants(local)

    aliases: list[str] = []
    seen: set[str] = set()
    counter = 0
    # Outer loop = counter (tag), inner = dot × domain để xen kẽ giữa các bucket
    while len(aliases) < count:
        for dv in dot_vars:
            for d in domains:
                a = f"{dv}+e{counter:05d}@{d}"
                if a in seen:
                    continue
                seen.add(a)
                aliases.append(a)
                if len(aliases) >= count:
                    return aliases
        counter += 1
        if counter > 999_999:  # 1M safety guard
            raise RuntimeError(
                f"Counter exceeded 999999 mà chưa đủ {count} alias. "
                f"dot_vars={len(dot_vars)} domains={len(domains)}"
            )
    return aliases


def main() -> int:
    p = argparse.ArgumentParser(description="Generate Gmail aliases for the register pool.")
    p.add_argument("--count", type=int, default=10000, help="Số alias cần sinh (default 10000)")
    p.add_argument("--base", help="Base email. Mặc định lấy từ IMAP_USER trong .env")
    p.add_argument("--out", default=str(DEFAULT_OUT), help=f"File output (default {DEFAULT_OUT})")
    p.add_argument("--shuffle", action="store_true", help="Xáo trộn thứ tự để tránh pattern.")
    p.add_argument("--seed", type=int, help="Seed cho shuffle (để reproducible).")
    p.add_argument("--no-googlemail", action="store_true", help="Chỉ dùng gmail.com (không googlemail.com).")
    p.add_argument("--append", action="store_true", help="Ghi nối vào file thay vì overwrite.")
    p.add_argument("--dry-run", action="store_true", help="Chỉ in 5 sample, không ghi file.")
    args = p.parse_args()

    if args.count <= 0:
        print(f"[gen] count phải > 0 (got {args.count})", file=sys.stderr)
        return 2

    base = args.base
    if not base:
        if load_dotenv and DEFAULT_ENV.exists():
            load_dotenv(DEFAULT_ENV)
        base = os.environ.get("IMAP_USER", "").strip()
        if not base:
            print(
                "[gen] Không tìm được base email. Set IMAP_USER trong .env hoặc dùng --base.",
                file=sys.stderr,
            )
            return 2
        print(f"[gen] base từ IMAP_USER (.env) = {base}")

    aliases = generate_aliases(
        base_email=base,
        count=args.count,
        use_googlemail=not args.no_googlemail,
    )

    if args.shuffle:
        rng = random.Random(args.seed)
        rng.shuffle(aliases)
        print(f"[gen] shuffled (seed={args.seed})")

    if args.dry_run:
        print(f"[gen] dry-run → {len(aliases)} alias generated. Sample:")
        for a in aliases[:5]:
            print(f"  {a}")
        print(f"  ... ({len(aliases) - 5} more)" if len(aliases) > 5 else "")
        return 0

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if args.append else "w"
    with out_path.open(mode, encoding="utf-8") as f:
        if args.append and out_path.exists() and out_path.stat().st_size > 0:
            f.write("\n")
        f.write("\n".join(aliases))
        f.write("\n")
    action = "appended" if args.append else "wrote"
    print(f"[gen] {action} {len(aliases)} alias -> {out_path}")
    print(f"[gen] base={base}  domains={'gmail.com,googlemail.com' if not args.no_googlemail else 'gmail.com'}")
    print(f"[gen] sample: {aliases[0]}  ...  {aliases[-1]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
