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


SITEKEY_RE = re.compile(r'(0x4[A-Za-z0-9_-]{20,})')


def extract_sitekey(page: Page) -> Optional[str]:
    # 1) data-sitekey attribute (fast path)
    try:
        loc = page.locator('[data-sitekey]').first
        if loc.count() > 0:
            sk = loc.get_attribute('data-sitekey')
            if sk and sk.startswith('0x'):
                print(f"[sitekey] via data-sitekey attr: {sk}")
                return sk
    except Exception as e:
        print(f"[sitekey] attr probe failed: {e}")

    # 2) regex on main HTML
    try:
        html = page.content()
        m = SITEKEY_RE.search(html)
        if m:
            print(f"[sitekey] via main HTML regex: {m.group(1)}")
            return m.group(1)
    except Exception as e:
        print(f"[sitekey] HTML regex failed: {e}")

    # 3) regex on every script src + inline script body via JS
    try:
        sk = page.evaluate(r"""
            () => {
                const re = /0x4[A-Za-z0-9_-]{20,}/;
                const scripts = Array.from(document.querySelectorAll('script'));
                for (const s of scripts) {
                    const txt = s.textContent || '';
                    const m = txt.match(re);
                    if (m) return m[0];
                }
                const html = document.documentElement.outerHTML;
                const m2 = html.match(re);
                return m2 ? m2[0] : null;
            }
        """)
        if sk:
            print(f"[sitekey] via JS scan: {sk}")
            return sk
    except Exception as e:
        print(f"[sitekey] JS scan failed: {e}")

    # 4) iframe URL param (Cloudflare challenge frame embeds sitekey in URL)
    try:
        for f in page.frames:
            url = f.url or ""
            if "challenges.cloudflare.com" in url:
                m = re.search(r'[?&/]sitekey[=/]([^&/?]+)', url)
                if m:
                    print(f"[sitekey] via iframe URL param: {m.group(1)}")
                    return m.group(1)
                m = SITEKEY_RE.search(url)
                if m:
                    print(f"[sitekey] via iframe URL regex: {m.group(1)}")
                    return m.group(1)
    except Exception as e:
        print(f"[sitekey] iframe probe failed: {e}")

    return None


# In-context Turnstile solver. Runs inside the SAME browser context as the
# register page so cookies / TLS / fingerprint match. Mirrors Theyka's
# Turnstile-Solver approach (page.route + minimal HTML host + shrink widget +
# click via locator) but reuses our context instead of spawning a separate browser.
TURNSTILE_HOST_HTML = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ts</title>
<script src="https://challenges.cloudflare.com/turnstile/v0/api.js" async defer></script>
</head><body>
<div class="cf-turnstile" style="background: white;" data-sitekey="__SITEKEY__"></div>
</body></html>"""


def solve_in_context(context, sitekey: str, origin: str = "https://events.elle.vn",
                     max_attempts: int = 30) -> Optional[str]:
    """Open a fake page in the same browser context, embed Turnstile widget,
    shrink it, click the .cf-turnstile div until the hidden response input
    has a value. Returns token or None.
    """
    fake_url = origin.rstrip("/") + "/"
    html = TURNSTILE_HOST_HTML.replace("__SITEKEY__", sitekey)
    page = context.new_page()
    try:
        page.route(fake_url, lambda route: route.fulfill(
            status=200, content_type="text/html; charset=utf-8", body=html))
        try:
            page.goto(fake_url, timeout=30_000)
        except Exception as e:
            print(f"[solver-ctx] goto error: {e}")
            return None

        # Shrink widget so the click lands on the checkbox iframe area.
        try:
            page.eval_on_selector("//div[@class='cf-turnstile']",
                                  "el => el.style.width = '70px'")
        except Exception as e:
            print(f"[solver-ctx] shrink failed: {e}")

        for attempt in range(1, max_attempts + 1):
            try:
                val = page.input_value("[name=cf-turnstile-response]", timeout=2000)
            except Exception:
                val = ""
            if val:
                print(f"[solver-ctx] token len={len(val)} after {attempt} attempt(s)")
                return val
            try:
                page.locator("//div[@class='cf-turnstile']").click(timeout=1000)
            except Exception:
                pass
            page.wait_for_timeout(500)

        print(f"[solver-ctx] timeout after {max_attempts} attempts")
        return None
    finally:
        try: page.close()
        except Exception: pass


# JS injection: set hidden input value via React-aware native setter,
# fire input/change events, and trigger common framework callbacks.
INJECT_TOKEN_JS = r"""
(token) => {
  let touched = 0;
  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
  const inputs = document.querySelectorAll('input[name="cf-turnstile-response"], input[name="g-recaptcha-response"], input[id^="cf-chl-widget-"][id$="_response"]');
  inputs.forEach(inp => {
    setter.call(inp, token);
    inp.dispatchEvent(new Event('input', { bubbles: true }));
    inp.dispatchEvent(new Event('change', { bubbles: true }));
    touched++;
  });
  // If page hadn't rendered the hidden input yet, create one inside the .cf-turnstile div.
  if (touched === 0) {
    const host = document.querySelector('.cf-turnstile, [data-sitekey]');
    if (host) {
      const inp = document.createElement('input');
      inp.type = 'hidden';
      inp.name = 'cf-turnstile-response';
      inp.value = token;
      host.appendChild(inp);
      touched++;
    }
  }
  let called = 0;
  // Fallback 1: article-style global window.tsCallback
  if (typeof window.tsCallback === 'function') {
    try { window.tsCallback(token); called++; } catch (e) { console.error('[tsCallback]', e); }
  }
  // Fallback 2: data-callback="funcName" attribute on .cf-turnstile host
  document.querySelectorAll('.cf-turnstile[data-callback], [data-sitekey][data-callback]').forEach(el => {
    const fnName = el.getAttribute('data-callback');
    if (fnName && typeof window[fnName] === 'function') {
      try { window[fnName](token); called++; } catch (e) { console.error('[data-callback ' + fnName + ']', e); }
    }
  });
  // Fallback 3: dispatch a synthetic event some libs listen for
  try {
    document.dispatchEvent(new CustomEvent('turnstile-success', { detail: { token } }));
  } catch (e) {}
  return { touched, called };
}
"""


def inject_turnstile_token(page: Page, token: str) -> dict:
    result = page.evaluate(INJECT_TOKEN_JS, token)
    print(f"[inject] inputs touched={result.get('touched')} callbacks invoked={result.get('called')}")
    return result


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

        # ── Phase 3: Turnstile (nếu có) ──
        if detect_turnstile(page):
            t0 = time.time()

            # 3a. Poll sitekey (thay wait_for_timeout(2500) cố định)
            sitekey = os.environ.get("TURNSTILE_SITEKEY")
            if not sitekey:
                mount_deadline = time.time() + _env_int("REG_CAPTCHA_MOUNT_TIMEOUT_MS", 5_000) / 1000
                while time.time() < mount_deadline:
                    sitekey = extract_sitekey(page)
                    if sitekey:
                        break
                    page.wait_for_timeout(150)
            if not sitekey:
                raise RuntimeError("Không tìm được sitekey Turnstile")
            print(f"[captcha] sitekey={sitekey}")

            # 3b. Solve token
            token = os.environ.get("TURNSTILE_TOKEN")
            if not token:
                from urllib.parse import urlsplit
                sp = urlsplit(TARGET_URL)
                origin = f"{sp.scheme}://{sp.netloc}"
                max_attempts = int(os.environ.get("TURNSTILE_MAX_ATTEMPTS", "30"))
                token = solve_in_context(context, sitekey, origin=origin, max_attempts=max_attempts)
            if not token:
                raise RuntimeError("Không lấy được Turnstile token")

            # 3c. Inject + poll input value (thay wait_for_timeout(500) cố định)
            inject_turnstile_token(page, token)
            inject_deadline = time.time() + _env_int("REG_TOKEN_INJECT_TIMEOUT_MS", 2_000) / 1000
            while time.time() < inject_deadline:
                try:
                    val = page.input_value('input[name="cf-turnstile-response"]', timeout=300)
                except Exception:
                    val = ""
                if val:
                    break
                page.wait_for_timeout(80)
            t_phase["captcha"] = int((time.time() - t0) * 1000)

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
