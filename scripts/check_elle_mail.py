"""
Listen Gmail inbox via IMAP, hiển thị template email xác thực Elle.

Usage:
    py scripts/check_elle_mail.py                  # poll mỗi 5s + auto-verify (default)
    py scripts/check_elle_mail.py --once           # quét 1 lần rồi thoát
    py scripts/check_elle_mail.py --all            # bao gồm cả mail đã đọc (default chỉ UNSEEN)
    py scripts/check_elle_mail.py --no-verify      # chỉ in mail, KHÔNG click verify
    py scripts/check_elle_mail.py --once --no-verify  # 1 lần, chỉ xem
    py scripts/check_elle_mail.py --backfill-awaiting # quét cả mail đã đọc, verify lại các account awaiting_*

Env (Reg-Bot/.env):
    IMAP_USER         Gmail address (vd: yourname@gmail.com)
    IMAP_PASS         Gmail App Password (16 ký tự, KHÔNG dùng password chính)
    IMAP_FOLDER       optional, default "INBOX"
    IMAP_FROM         optional, default "elledigital@3480726.brevosend.com"
    IMAP_POLL_SEC     optional, default 5
    VERIFY_KEYWORDS   optional, comma-separated, default "xác nhận,xac nhan,verify,confirm,kích hoạt"
"""

from __future__ import annotations

import argparse
import asyncio
import email
import imaplib
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parseaddr
from pathlib import Path
from typing import Optional

import httpx
from dotenv import load_dotenv

# Allow `import accounts_db` when run as `py scripts/check_elle_mail.py`
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from accounts_db import (
        STATUS_AWAITING_MAIL,
        STATUS_AWAITING_VERIFY,
        STATUS_EXPORTED_FAILED_VERIFY,
        STATUS_EXPORTED_VERIFIED,
        STATUS_FAILED_VERIFY,
        STATUS_VERIFIED,
        append_event,
        get_account,
        update_status,
    )
    _DB_OK = True
except Exception as _db_e:  # noqa: BLE001
    print(f"[db] WARN không import được accounts_db ({_db_e}). Bỏ qua phần DB.")
    _DB_OK = False

IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993

DEFAULT_FROM = "elledigital@3480726.brevosend.com"
DEFAULT_FOLDER = "INBOX"
DEFAULT_POLL_SEC = 5
DEFAULT_VERIFY_KEYWORDS = "xác nhận,xac nhan,verify,confirm,kích hoạt,kich hoat,activate"


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _decode(s: Optional[str]) -> str:
    if not s:
        return ""
    try:
        return str(make_header(decode_header(s)))
    except Exception:
        return s


def _get_body(msg: Message) -> tuple[str, str]:
    """Return (text_body, html_body). Either may be ''."""
    text_body = ""
    html_body = ""
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition") or "")
            if "attachment" in disp.lower():
                continue
            try:
                payload = part.get_payload(decode=True)
                if payload is None:
                    continue
                charset = part.get_content_charset() or "utf-8"
                decoded = payload.decode(charset, errors="replace")
            except Exception:
                continue
            if ctype == "text/plain" and not text_body:
                text_body = decoded
            elif ctype == "text/html" and not html_body:
                html_body = decoded
    else:
        try:
            payload = msg.get_payload(decode=True)
            if payload is not None:
                charset = msg.get_content_charset() or "utf-8"
                decoded = payload.decode(charset, errors="replace")
                if msg.get_content_type() == "text/html":
                    html_body = decoded
                else:
                    text_body = decoded
        except Exception:
            pass
    return text_body, html_body


_HREF_RE = re.compile(
    r'<a\s[^>]*?href\s*=\s*"([^"]+)"[^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _strip_tags(html: str) -> str:
    txt = _TAG_RE.sub(" ", html)
    txt = _WS_RE.sub(" ", txt).strip()
    return txt


def _extract_links(html: str) -> list[tuple[str, str]]:
    """Return list of (url, anchor_text) from HTML."""
    out: list[tuple[str, str]] = []
    for m in _HREF_RE.finditer(html):
        url = m.group(1).strip()
        text = _strip_tags(m.group(2))
        out.append((url, text))
    return out


def _normalize(s: str) -> str:
    """Lowercase + strip Vietnamese accents for matching."""
    nfkd = unicodedata.normalize("NFKD", s)
    return "".join(c for c in nfkd if not unicodedata.combining(c)).lower()


def find_verify_link(links: list[tuple[str, str]], keywords: list[str]) -> Optional[tuple[str, str]]:
    """Find first link whose anchor text matches any keyword (accent-insensitive)."""
    norm_kws = [_normalize(k) for k in keywords if k.strip()]
    for url, text in links:
        if not text:
            continue
        ntext = _normalize(text)
        for kw in norm_kws:
            if kw and kw in ntext:
                return url, text
    return None


def click_verify_link(url: str, timeout_s: int = 30) -> tuple[bool, str, int]:
    """GET url, follow redirects. Returns (ok, final_url, status). [SYNC FALLBACK]"""
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,*/*",
                "Accept-Language": "vi-VN,vi;q=0.9,en;q=0.8",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            final_url = resp.geturl()
            status = resp.status
            return (200 <= status < 400, final_url, status)
    except urllib.error.HTTPError as e:
        return (False, getattr(e, "url", url), e.code)
    except Exception as e:
        print(f"[verify] HTTP error: {e}")
        return (False, url, 0)


async def click_verify_link_async(
    client: httpx.AsyncClient, url: str, timeout_s: int = 15
) -> tuple[bool, str, int]:
    """Async GET url, follow redirects. Returns (ok, final_url, status)."""
    try:
        resp = await client.get(url, timeout=timeout_s)
        status = resp.status_code
        return (200 <= status < 400, str(resp.url), status)
    except httpx.HTTPStatusError as e:
        resp = e.response
        return (False, str(resp.url) if resp is not None else url, resp.status_code if resp is not None else 0)
    except httpx.TimeoutException:
        return (False, url, 0)
    except Exception as e:
        print(f"[verify] HTTP error ({url[:60]}...): {e}")
        return (False, url, 0)


# --------------------------------------------------------------------------
# Mail fetch
# --------------------------------------------------------------------------

def fetch_messages(
    mail: imaplib.IMAP4_SSL,
    folder: str,
    sender: str,
    only_unseen: bool,
) -> list[bytes]:
    typ, _ = mail.select(folder)
    if typ != "OK":
        raise RuntimeError(f"Cannot select folder {folder!r}")

    criteria: list[str] = []
    if only_unseen:
        criteria.append("UNSEEN")
    criteria.extend(["FROM", f'"{sender}"'])

    typ, data = mail.search(None, *criteria)
    if typ != "OK":
        raise RuntimeError(f"IMAP search failed: {data!r}")

    ids = data[0].split() if data and data[0] else []
    return ids


def render_message(
    mail: imaplib.IMAP4_SSL,
    msg_id: bytes,
    expected_sender: str,
    quiet: bool = True,
) -> tuple[bool, list[tuple[str, str]], str]:
    """Print one message. Returns (matched, links, raw_to_addr_lower).

    quiet=True (default): in 1 dòng [mail] id=.. to=.. subject=.. links=N
    quiet=False: in đầy đủ TEXT/HTML body + danh sách links
    """
    # Use BODY.PEEK to avoid marking as seen
    typ, msg_data = mail.fetch(msg_id, "(BODY.PEEK[])")
    if typ != "OK" or not msg_data or not msg_data[0]:
        print(f"[mail] FAILED to fetch id={msg_id!r}")
        return False, [], ""

    raw = msg_data[0][1]
    msg = email.message_from_bytes(raw)

    sender = parseaddr(msg.get("From", ""))[1]
    if sender.lower() != expected_sender.lower():
        # IMAP FROM "..." có thể match rộng, verify lại exact
        return False, [], ""

    subject = _decode(msg.get("Subject", ""))
    date = msg.get("Date", "")
    to_header = _decode(msg.get("To", ""))
    to_addr = parseaddr(to_header)[1].strip().lower()

    text_body, html_body = _get_body(msg)
    links = _extract_links(html_body)

    if quiet:
        subj_short = (subject[:60] + "...") if len(subject) > 60 else subject
        print(f"[mail] id={msg_id.decode()} to={to_addr} links={len(links)} subj={subj_short!r}")
        return True, links, to_addr

    sep = "=" * 78
    print(f"\n{sep}")
    print(f"[mail] id={msg_id.decode()}  From: {sender}")
    print(f"       To:      {to_header}  (parsed={to_addr})")
    print(f"       Subject: {subject}")
    print(f"       Date:    {date}")

    if text_body:
        print(f"\n--- TEXT BODY ({len(text_body)} chars) ---")
        print(text_body[:2000])
        if len(text_body) > 2000:
            print(f"... [truncated, total {len(text_body)} chars]")

    if html_body:
        print(f"\n--- HTML BODY ({len(html_body)} chars) ---")
        print(html_body[:4000])
        if len(html_body) > 4000:
            print(f"... [truncated, total {len(html_body)} chars]")

    if links:
        print(f"\n--- LINKS ({len(links)}) ---")
        for i, (url, text) in enumerate(links, 1):
            label = text[:80] if text else "<no text>"
            print(f"  [{i}] {label!r}")
            print(f"      -> {url}")
    else:
        print("\n--- LINKS (none) ---")

    print(sep)
    return True, links, to_addr


def mark_seen(mail: imaplib.IMAP4_SSL, msg_id: bytes) -> None:
    try:
        mail.store(msg_id, "+FLAGS", "\\Seen")
        print(f"[mail] marked id={msg_id.decode()} as Seen")
    except Exception as e:
        print(f"[mail] mark seen failed: {e}")


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------

async def run_async(
    once: bool,
    include_seen: bool,
    verify: bool,
    quiet: bool = True,
) -> int:
    load_dotenv()
    user = os.environ.get("IMAP_USER")
    password = os.environ.get("IMAP_PASS")
    if not user or not password:
        print("[fatal] missing IMAP_USER / IMAP_PASS in environment (.env).")
        print("        Gmail: bật IMAP + tạo App Password (16 ký tự).")
        return 2

    folder = os.environ.get("IMAP_FOLDER", DEFAULT_FOLDER)
    sender = os.environ.get("IMAP_FROM", DEFAULT_FROM)
    poll_sec = int(os.environ.get("IMAP_POLL_SEC", str(DEFAULT_POLL_SEC)))
    keywords = [k.strip() for k in os.environ.get("VERIFY_KEYWORDS", DEFAULT_VERIFY_KEYWORDS).split(",") if k.strip()]

    # Async verify tuning — auto-compute concurrency từ BATCH_WORKERS (.env).
    # Formula: clamp(round(workers * 1.6), 4, 32). VERIFY_CONCURRENCY override nếu set.
    try:
        n_workers = max(1, int(os.environ.get("BATCH_WORKERS", "5")))
    except ValueError:
        n_workers = 5
    auto_conc = max(4, min(32, round(n_workers * 1.6)))
    conc_override = os.environ.get("VERIFY_CONCURRENCY", "").strip()
    if conc_override:
        try:
            concurrency = max(1, int(conc_override))
            conc_src = f"override VERIFY_CONCURRENCY={concurrency}"
        except ValueError:
            concurrency = auto_conc
            conc_src = f"auto (workers={n_workers})"
    else:
        concurrency = auto_conc
        conc_src = f"auto (workers={n_workers})"
    try:
        timeout_s = max(3, int(os.environ.get("VERIFY_TIMEOUT_SEC", "15")))
    except ValueError:
        timeout_s = 15

    print(f"[start] host={IMAP_HOST} user={user} folder={folder!r}")
    print(f"        sender filter: {sender}")
    print(f"        only_unseen={not include_seen} once={once} poll={poll_sec}s verify={verify} quiet={quiet}")
    print(f"        async concurrency={concurrency} timeout={timeout_s}s [{conc_src}]")
    if verify:
        print(f"        verify keywords: {keywords}")

    seen_ids: set[bytes] = set()
    sem = asyncio.Semaphore(concurrency)

    http_headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,*/*",
        "Accept-Language": "vi-VN,vi;q=0.9,en;q=0.8",
    }

    try:
        async with httpx.AsyncClient(
            headers=http_headers,
            follow_redirects=True,
            timeout=httpx.Timeout(timeout_s),
        ) as http_client:
            with imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT) as mail:
                mail.login(user, password)
                print("[login] ok")

                while True:
                    try:
                        ids = fetch_messages(mail, folder, sender, only_unseen=not include_seen)
                    except Exception as e:
                        print(f"[poll] search failed: {e}")
                        ids = []

                    new_ids = [i for i in ids if i not in seen_ids]
                    if new_ids:
                        print(f"\n[poll] {len(new_ids)} mail mới (total match: {len(ids)})")

                        # ---- Phase 1: render + DB flip awaiting_mail -> awaiting_verify (sync) ----
                        pending_clicks: list[tuple[bytes, str, str, str]] = []
                        for mid in new_ids:
                            try:
                                matched, links, to_addr = render_message(mail, mid, sender, quiet=quiet)
                            except Exception as e:
                                print(f"[mail] render failed id={mid!r}: {e}")
                                continue
                            if not matched:
                                continue
                            seen_ids.add(mid)

                            should_skip_verify = False
                            if _DB_OK and to_addr:
                                try:
                                    acc = get_account(to_addr)
                                    if acc is None:
                                        print(f"[db] WARN {to_addr} không có trong accounts.db (skip)")
                                        should_skip_verify = True
                                    else:
                                        cur_status = acc["status"]
                                        if cur_status == STATUS_AWAITING_MAIL:
                                            update_status(
                                                to_addr,
                                                STATUS_AWAITING_VERIFY,
                                                mail_received_ts=int(time.time()),
                                            )
                                            append_event(to_addr, "mail_received", ok=True)
                                            print(f"[db] {to_addr} -> awaiting_verify")
                                        elif cur_status == STATUS_AWAITING_VERIFY:
                                            print(f"[db] {to_addr} status=awaiting_verify (re-verify)")
                                        elif cur_status in (STATUS_VERIFIED, STATUS_EXPORTED_VERIFIED):
                                            print(f"[db] {to_addr} status={cur_status} (skip, already verified)")
                                            should_skip_verify = True
                                        elif cur_status in (STATUS_FAILED_VERIFY, STATUS_EXPORTED_FAILED_VERIFY):
                                            print(f"[db] {to_addr} status={cur_status} (skip, failed previously)")
                                            should_skip_verify = True
                                        else:
                                            print(f"[db] {to_addr} status={cur_status} (skip)")
                                            should_skip_verify = True
                                except Exception as e:
                                    print(f"[db] update mail_received failed: {e}")

                            if should_skip_verify:
                                continue
                            if not verify:
                                continue
                            hit = find_verify_link(links, keywords)
                            if hit is None:
                                print(f"[verify] {to_addr or '?'} không tìm thấy link match keywords {keywords}")
                                continue
                            vurl, vtext = hit
                            pending_clicks.append((mid, to_addr, vurl, vtext))

                        # ---- Phase 2: parallel async click ----
                        if pending_clicks:
                            t0 = time.time()
                            print(f"[verify] click {len(pending_clicks)} link song song (concurrency={concurrency}, timeout={timeout_s}s)")

                            async def _gated_click(item: tuple[bytes, str, str, str]):
                                _mid, _to, _vurl, _vtext = item
                                async with sem:
                                    ok, final_url, status = await click_verify_link_async(
                                        http_client, _vurl, timeout_s
                                    )
                                return (_mid, _to, _vurl, _vtext, ok, final_url, status)

                            results = await asyncio.gather(
                                *[_gated_click(it) for it in pending_clicks],
                                return_exceptions=False,
                            )
                            elapsed = time.time() - t0
                            print(f"[verify] batch done in {elapsed:.1f}s ({len(results)} click)")

                            # ---- Phase 3: DB finalize + mark_seen (sync, sequential) ----
                            for mid, to_addr, vurl, vtext, ok, final_url, status in results:
                                if ok:
                                    print(f"[verify] OK {to_addr} status={status} {vtext!r}")
                                    mark_seen(mail, mid)
                                    if _DB_OK and to_addr:
                                        try:
                                            if get_account(to_addr) is not None:
                                                update_status(
                                                    to_addr,
                                                    STATUS_VERIFIED,
                                                    verified_ts=int(time.time()),
                                                    error=None,
                                                )
                                                append_event(
                                                    to_addr, "verify", ok=True,
                                                    detail=f"status={status} final_url={final_url}",
                                                )
                                                print(f"[db] {to_addr} -> verified")
                                        except Exception as e:
                                            print(f"[db] update verified failed: {e}")
                                else:
                                    print(f"[verify] FAILED {to_addr} status={status} url={final_url}")
                                    if _DB_OK and to_addr:
                                        try:
                                            if get_account(to_addr) is not None:
                                                update_status(
                                                    to_addr,
                                                    STATUS_FAILED_VERIFY,
                                                    error=f"http {status}",
                                                    bump_attempts=True,
                                                )
                                                append_event(
                                                    to_addr, "verify", ok=False,
                                                    detail=f"status={status} url={final_url}",
                                                )
                                                print(f"[db] {to_addr} -> failed_verify")
                                        except Exception as e:
                                            print(f"[db] update failed_verify failed: {e}")
                    else:
                        print(f"[poll] no new mail (match: {len(ids)})", end="\r", flush=True)

                    if once:
                        return 0

                    await asyncio.sleep(poll_sec)

    except KeyboardInterrupt:
        print("\n[stop] user cancelled")
        return 130
    except imaplib.IMAP4.error as e:
        print(f"[fatal] IMAP error: {e}")
        print("        Gmail: kiểm tra App Password + IMAP đã bật trong settings.")
        return 1
    except Exception as e:
        print(f"[fatal] {e}")
        return 1


def main() -> int:
    p = argparse.ArgumentParser(description="Listen Gmail for Elle verification mails (async batch).")
    p.add_argument("--once", action="store_true", help="poll một lần rồi thoát")
    p.add_argument("--all", action="store_true", help="bao gồm cả mail đã đọc (default chỉ UNSEEN)")
    p.add_argument(
        "--backfill-awaiting",
        action="store_true",
        help="Quét cả mail đã đọc (implies --all) để xử lý các account còn awaiting_mail/awaiting_verify",
    )
    p.add_argument(
        "--verify",
        dest="verify",
        action="store_true",
        default=True,
        help="tự động GET link xác nhận và mark mail seen (DEFAULT: bật)",
    )
    p.add_argument(
        "--no-verify",
        dest="verify",
        action="store_false",
        help="tắt auto-verify, chỉ in nội dung mail",
    )
    p.add_argument(
        "--quiet",
        dest="quiet",
        action="store_true",
        default=True,
        help="chỉ in 1 dòng/mail (DEFAULT: bật)",
    )
    p.add_argument(
        "--verbose",
        dest="quiet",
        action="store_false",
        help="in đầy đủ TEXT/HTML body + links (debug)",
    )
    args = p.parse_args()
    include_seen = args.all or args.backfill_awaiting
    if args.backfill_awaiting and not args.all:
        print("[backfill] --backfill-awaiting bật → tự bật --all (quét cả mail đã đọc)")
    try:
        return asyncio.run(
            run_async(
                once=args.once,
                include_seen=include_seen,
                verify=args.verify,
                quiet=args.quiet,
            )
        )
    except KeyboardInterrupt:
        print("\n[stop] user cancelled")
        return 130


if __name__ == "__main__":
    sys.exit(main())
