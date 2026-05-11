from __future__ import annotations

import json
import os
import re
import sys
import time
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from typing import Any, Optional

from dotenv import load_dotenv

try:
    from camoufox.sync_api import Camoufox
except ImportError:
    Camoufox = None

Locator = Any
Page = Any
Response = Any

TARGET_URL = "https://events.elle.vn/register"


def _env_int(name: str, default: int) -> int:
    """Đọc int từ env, trả default nếu thiếu/không hợp lệ. Min 0."""
    try:
        return max(0, int(os.environ.get(name, str(default))))
    except ValueError:
        return default


@dataclass
class Input:
    username: str
    email: str
    password: str
    headless: bool
    slow_mo_ms: int


def load_input() -> Input:
    load_dotenv()
    required = ["ELLE_USERNAME", "ELLE_EMAIL", "ELLE_PASSWORD"]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        raise SystemExit(f"Missing required env vars: {', '.join(missing)}")

    return Input(
        username=os.environ["ELLE_USERNAME"],
        email=os.environ["ELLE_EMAIL"],
        password=os.environ["ELLE_PASSWORD"],
        headless=os.environ.get("HEADLESS", "false").lower() == "true",
        slow_mo_ms=int(os.environ.get("SLOW_MO_MS", "50")),
    )


def mask_email(email: str) -> str:
    if "@" not in email: return "***"
    local, domain = email.split("@", 1)
    return f"{local[:2]}***@{domain}"


def wait_visible(page: Page, selector: str, timeout_ms: int = 15_000) -> Locator:
    loc = page.locator(selector).first
    loc.wait_for(state="visible", timeout=timeout_ms)
    return loc


def fill_form(page: Page, data: Input) -> None:
    page.wait_for_selector("form", timeout=20_000)
    wait_visible(page, 'input[name="username"]').fill(data.username)
    print("[fill] username -> ok")
    wait_visible(page, 'input[name="email"]').fill(data.email)
    print("[fill] email -> ok")
    wait_visible(page, 'input[name="password"]').fill(data.password)
    print("[fill] password -> ok")
    wait_visible(page, 'input[name="passwordConfirmation"]').fill(data.password)
    print("[fill] passwordConfirmation -> ok")


def detect_turnstile(page: Page) -> bool:
    for f in page.frames:
        if "challenges.cloudflare.com" in (f.url or ""): return True
    try:
        return page.locator('.cf-turnstile, [data-sitekey], input[name="cf-turnstile-response"]').count() > 0
    except Exception:
        return False


def solve_on_real_page(page: Page, max_attempts: int = 30) -> Optional[str]:
    """Solve Turnstile DIRECTLY on real page (no fake page).

    Token sẽ gắn đúng action/cdata/origin context mà real page widget
    đã khai báo → server elle.vn validate sẽ accept.

    Cloudflare có thể nest iframe nhiều cấp → dùng `page.frames` (flat list
    mọi depth) tìm frame có URL `challenges.cloudflare.com`, lấy
    `frame_element()` ra ElementHandle ở parent frame rồi click lên đó.
    Browser forward hit-test vào checkbox cross-origin. Cloudflare tự set
    hidden input + gọi callback nội tại → KHÔNG cần inject token.
    """
    # Chờ cloudflare frame attach (poll page.frames thay vì page.locator)
    deadline = time.time() + 10.0
    cf_frame = None
    while time.time() < deadline:
        for f in page.frames:
            if "challenges.cloudflare.com" in (f.url or ""):
                cf_frame = f
                break
        if cf_frame:
            break
        page.wait_for_timeout(200)

    if not cf_frame:
        print("[solver-real] no cloudflare frame attached within 10s")
        return None
    print(f"[solver-real] cf frame found: {cf_frame.url[:80]}")

    # Lấy ElementHandle của iframe trong parent frame
    try:
        elem = cf_frame.frame_element()
    except Exception as e:
        print(f"[solver-real] frame_element() failed: {e}")
        return None

    # Shrink để click landing trên checkbox (cùng trick fake-mode dùng)
    try:
        elem.evaluate("""
            el => {
                el.style.width = '70px';
                if (el.parentElement) el.parentElement.style.width = '70px';
            }
        """)
    except Exception as e:
        print(f"[solver-real] shrink failed: {e}")

    for attempt in range(1, max_attempts + 1):
        try:
            val = page.input_value('input[name="cf-turnstile-response"]', timeout=2000)
        except Exception:
            val = ""
        if val:
            print(f"[solver-real] token len={len(val)} after {attempt} attempt(s)")
            return val
        try:
            elem.click(force=True, timeout=1000)
        except Exception:
            pass
        page.wait_for_timeout(500)

    print(f"[solver-real] timeout after {max_attempts} attempts")
    return None


# RSC line: "1:{\"error\":\"...\",\"success\":\"...\"}"
_RSC_RESULT_RE = re.compile(r'^\d+:(\{[^\n]*"error"[^\n]*"success"[^\n]*\})', re.MULTILINE)


def parse_register_result(body: str) -> Optional[dict]:
    """Tìm dòng RSC chứa {"error": "...", "success": "..."} và parse JSON."""
    if not body:
        return None
    m = _RSC_RESULT_RE.search(body)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except Exception:
        return None


def main() -> int:
    data = load_input()
    print(f"[start] target={TARGET_URL} username={data.username} email={mask_email(data.email)} headless={data.headless}")

    keep_open = int(os.environ.get("KEEP_OPEN_SEC", "30"))

    with build_browser_context(headless=data.headless, slow_mo_ms=data.slow_mo_ms) as ctx:
        result = register_one(ctx, data.email)
        print(f"[result] ok={result.ok} err={result.err!r} latency={result.latency_ms}ms")
        if keep_open > 0:
            print(f"\nBrowser stays open {keep_open}s for inspection.")
            try:
                ctx.pages[0].wait_for_timeout(keep_open * 1000) if ctx.pages else None
            except Exception:
                pass
        return 0 if result.ok else 1


# ---------------------------------------------------------------------------
# Reusable building blocks for batch processing
# ---------------------------------------------------------------------------

@dataclass
class RegisterResult:
    ok: bool
    err: Optional[str]
    latency_ms: int
    raw_response: Optional[str] = None
    final_url: Optional[str] = None


@contextmanager
def build_browser_context(headless: bool = True, slow_mo_ms: int = 50, use_camoufox: Optional[bool] = None):
    """Context manager: mở persistent Camoufox browser context.

    Không fallback sang Chrome/Chromium. Đóng tự động khi exit block.
    """
    if use_camoufox is False:
        raise RuntimeError("Chrome/Chromium fallback đã bị tắt. Bot chỉ dùng Camoufox.")

    user_data_dir = os.environ.get(
        "CAMOUFOX_USER_DATA_DIR",
        os.path.abspath("./camoufox-profile"),
    )

    with ExitStack() as stack:
        if Camoufox is None:
            raise RuntimeError("Camoufox chưa được cài. Chạy: pip install -r requirements.txt")
        os.makedirs(user_data_dir, exist_ok=True)
        context = stack.enter_context(
            Camoufox(
                persistent_context=True,
                user_data_dir=user_data_dir,
                headless=headless,
                humanize=True,
                geoip=True,
                os="windows",
                locale="vi-VN",
                window=(1280, 900),
                slow_mo=slow_mo_ms,
            )
        )
        yield context


def register_one(context, email: str) -> RegisterResult:
    """Register 1 account dùng `email` cho cả username/email/password.

    Mở 1 page mới trên `context`, làm full flow, đóng page, trả RegisterResult.
    Context được giữ nguyên (caller chịu trách nhiệm reset/close).
    """
    started = time.time()
    page = context.new_page()
    register_responses: list[str] = []
    register_statuses: list[int] = []

    def _on_response(resp: Response) -> None:
        try:
            if resp.request.method != "POST":
                return
            if "events.elle.vn" not in resp.url or "/register" not in resp.url:
                return
            register_statuses.append(resp.status)
            body = resp.text()
            register_responses.append(body)
            print(f"[net] POST {resp.url} -> {resp.status}, body[:200]={body[:200]!r}")
        except Exception as e:
            print(f"[net] response read failed: {e}")

    page.on("response", _on_response)

    # KHÔNG đóng các page khác — đặc biệt là `about:blank` mặc định của
    # persistent_context. Camoufox đóng context khi page count về 0, gây
    # TargetClosedError cho account kế tiếp trong batch.

    try:
        page.bring_to_front()
    except Exception:
        pass

    print(f"[register] email={mask_email(email)} -> {TARGET_URL}")
    t_phase = {"goto": 0, "fill": 0, "captcha": 0, "submit": 0}
    try:
        # ── Phase 1: goto ── chỉ chờ DOMContentLoaded, KHÔNG chờ networkidle
        # (analytics/3rd-party scripts có thể không bao giờ idle).
        t0 = time.time()
        page.goto(TARGET_URL, wait_until="domcontentloaded",
                  timeout=_env_int("REG_GOTO_TIMEOUT_MS", 45_000))
        t_phase["goto"] = int((time.time() - t0) * 1000)

        # ── Phase 2: fill form ── wait_for_selector("form") trong fill_form
        # đã đảm bảo form sẵn sàng, không cần networkidle.
        t0 = time.time()
        form_data = Input(username=email, email=email, password=email, headless=True, slow_mo_ms=0)
        fill_form(page, form_data)
        t_phase["fill"] = int((time.time() - t0) * 1000)

        # ── Phase 3: Turnstile (nếu có) — giải trực tiếp trên real page ──
        if detect_turnstile(page):
            t0 = time.time()
            max_attempts = int(os.environ.get("TURNSTILE_MAX_ATTEMPTS", "30"))
            token = solve_on_real_page(page, max_attempts=max_attempts)
            if not token:
                raise RuntimeError("Không lấy được Turnstile token")

            # Debug pause: xem widget real page state khi đã có token
            _dbg_after_captcha = _env_int("REG_DEBUG_PAUSE_AFTER_CAPTCHA_MS", 0)
            if _dbg_after_captcha > 0:
                print(f"[debug-pause] after-captcha {_dbg_after_captcha}ms — xem widget state")
                page.wait_for_timeout(_dbg_after_captcha)

            t_phase["captcha"] = int((time.time() - t0) * 1000)

        # Debug pause: xem widget có nhận token / React state đã update chưa
        _dbg_after_inject = _env_int("REG_DEBUG_PAUSE_AFTER_INJECT_MS", 0)
        if _dbg_after_inject > 0:
            print(f"[debug-pause] after-inject {_dbg_after_inject}ms — xem widget checkmark / React state")
            page.wait_for_timeout(_dbg_after_inject)

        # ── Phase 4: submit + chờ response /register ──
        # Thay vì wait_for_url + networkidle + sleep cố định 2.5s, poll
        # register_responses/register_statuses (đã capture qua _on_response).
        t0 = time.time()
        url_before_submit = page.url
        page.evaluate("""
            () => {
                const form = document.querySelector('form');
                if (!form) throw new Error("Không tìm thấy thẻ <form>.");
                const fakeBtn = document.createElement('button');
                fakeBtn.type = 'submit';
                fakeBtn.style.display = 'none';
                form.appendChild(fakeBtn);
                fakeBtn.click();
            }
        """)
        print("[submit] Đã ép Submit qua HTML Form.")

        submit_deadline = time.time() + _env_int("REG_SUBMIT_RESPONSE_TIMEOUT_MS", 20_000) / 1000
        while time.time() < submit_deadline:
            # 1) parsed RSC result (success/error rõ ràng)?
            for body in reversed(register_responses):
                if parse_register_result(body) is not None:
                    break
            else:
                # 2) HTTP status code đã bắt được + là 429/5xx → dừng sớm
                if register_statuses:
                    last = register_statuses[-1]
                    if last == 429 or last >= 500:
                        break
                # 3) URL đã đổi (success không trả RSC parseable)?
                if page.url != url_before_submit:
                    page.wait_for_timeout(250)  # cho response cuối cùng kịp về
                    break
                page.wait_for_timeout(120)
                continue
            break
        t_phase["submit"] = int((time.time() - t0) * 1000)
        print(f"[timing] goto={t_phase['goto']}ms fill={t_phase['fill']}ms "
              f"captcha={t_phase['captcha']}ms submit={t_phase['submit']}ms")

        # Debug pause: xem response / error overlay trên real page trước khi browser close
        _dbg_after_submit = _env_int("REG_DEBUG_PAUSE_AFTER_SUBMIT_MS", 0)
        if _dbg_after_submit > 0:
            print(f"[debug-pause] after-submit {_dbg_after_submit}ms — xem response/error trên page")
            page.wait_for_timeout(_dbg_after_submit)

        # Parse RSC response
        register_result = None
        raw_body = None
        for body in reversed(register_responses):
            parsed = parse_register_result(body)
            if parsed is not None:
                register_result = parsed
                raw_body = body
                break

        latency_ms = int((time.time() - started) * 1000)
        if register_result is not None:
            err = (register_result.get("error") or "").strip()
            if not err:
                return RegisterResult(ok=True, err=None, latency_ms=latency_ms,
                                      raw_response=raw_body, final_url=page.url)
            return RegisterResult(ok=False, err=err, latency_ms=latency_ms,
                                  raw_response=raw_body, final_url=page.url)
        # Không bắt được response → kiểm tra status code đã ghi (429/5xx) trước
        last_status = register_statuses[-1] if register_statuses else None
        if last_status is not None and (last_status == 429 or last_status >= 500):
            return RegisterResult(ok=False, err=f"http-{last_status}",
                                  latency_ms=latency_ms, raw_response=None, final_url=page.url)
        # Không bắt được response → chấp nhận URL change làm proxy
        if page.url != url_before_submit:
            return RegisterResult(ok=True, err=None, latency_ms=latency_ms,
                                  raw_response=None, final_url=page.url)
        return RegisterResult(ok=False, err="no-response-and-url-unchanged",
                              latency_ms=latency_ms, raw_response=None, final_url=page.url)

    except Exception as e:
        latency_ms = int((time.time() - started) * 1000)
        return RegisterResult(ok=False, err=f"exception: {e}", latency_ms=latency_ms,
                              raw_response=None, final_url=getattr(page, "url", None))
    finally:
        try:
            page.remove_listener("response", _on_response)
        except Exception:
            pass
        # Trước khi đóng page chính: đảm bảo context vẫn còn ít nhất 1 page
        # khác để Camoufox/persistent_context không tự terminate.
        try:
            others = [p for p in context.pages if p is not page and not p.is_closed()]
            if not others:
                try:
                    context.new_page()  # blank keep-alive
                except Exception:
                    pass
        except Exception:
            pass
        try:
            page.close()
        except Exception:
            pass

if __name__ == "__main__":
    sys.exit(main())
