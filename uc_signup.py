#!/usr/bin/env python3
"""
ChatGPT 注册 + OAuth CPA 回调（最终版）
用法: python3 uc_signup.py
"""
import argparse, json, os, re, shutil, signal, subprocess, sys, time
from datetime import datetime
from pathlib import Path
from urllib.request import Request, urlopen

import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import TimeoutException, StaleElementReferenceException

# ── 配置 ────────────────────────────────────────────────
API    = os.getenv("UC_SIGNUP_API_BASE", os.getenv("API_BASE", "http://127.0.0.1:3030"))
PROXY  = os.getenv("UC_SIGNUP_PROXY", os.getenv("BROWSER_PROXY", os.getenv("PROXY", ""))).strip()
ROOT   = Path(__file__).resolve().parent
MAX_RETRIES    = 3   # 每步最大重试次数
MAX_ERROR_REFRESH = 5  # 错误页刷新次数
PHONE_RETRY_LIMIT = int(os.getenv("UC_SIGNUP_PHONE_RETRIES", "0"))
SMS_TIMEOUT_SECONDS = int(os.getenv("UC_SIGNUP_SMS_TIMEOUT_SECONDS", "135"))
SMS_POLL_INTERVAL_SECONDS = int(os.getenv("UC_SIGNUP_SMS_POLL_INTERVAL_SECONDS", "10"))
PHONE_PASSWORD_PAGE_TIMEOUT = int(os.getenv("UC_SIGNUP_PHONE_PASSWORD_PAGE_TIMEOUT", "25"))

# 注册参数
PW   = os.getenv("SIGNUP_PASSWORD", "ChangeMe123456!")
NAME = os.getenv("SIGNUP_NAME", "Test User")
AGE  = os.getenv("SIGNUP_AGE", "18")
DISPLAY = os.getenv("UC_SIGNUP_DISPLAY", os.getenv("BROWSER_DISPLAY", ":1"))
def detect_chrome_binary():
    configured = os.getenv("UC_SIGNUP_CHROME_BINARY", os.getenv("CHROME_BINARY", "")).strip()
    if configured:
        return configured
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        path = shutil.which(name)
        if path:
            return path
    return "/usr/bin/google-chrome"

def detect_chrome_version(binary):
    configured = os.getenv("UC_SIGNUP_CHROME_VERSION", "").strip()
    if configured:
        try:
            return int(configured)
        except ValueError:
            pass
    try:
        out = subprocess.check_output([binary, "--version"], text=True, stderr=subprocess.STDOUT, timeout=5)
        m = re.search(r"(\d+)\.", out)
        if m:
            return int(m.group(1))
    except Exception:
        pass
    return 149

CHROME_BINARY = detect_chrome_binary()
CHROME_VERSION = detect_chrome_version(CHROME_BINARY)

# ── 工具函数 ────────────────────────────────────────────
def log(msg, level="info"):
    p = {"error":"❌","warn":"⚠️","info":"  "}.get(level,"  ")
    print(f"{p} [{datetime.now():%H:%M:%S}] {msg}", flush=True)

def api(method, path, body=None):
    url = f"{API}{path}"
    h = {"Accept": "application/json"}
    admin_password = os.getenv("UC_SIGNUP_ADMIN_PASSWORD", os.getenv("ADMIN_PASSWORD", "")).strip()
    if admin_password:
        h["X-Admin-Password"] = admin_password
    data = json.dumps(body).encode() if body else None
    if data: h["Content-Type"] = "application/json"
    resp = urlopen(Request(url, data=data, method=method, headers=h), timeout=30)
    return json.loads(resp.read().decode())

# 加载 .env
env_file = ROOT / ".env"
if env_file.exists():
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            key = k.strip()
            if key == "ADMIN_PASSWORD":
                os.environ.setdefault(key, v.strip())

API = os.getenv("UC_SIGNUP_API_BASE", os.getenv("API_BASE", API)).rstrip("/")
PROXY = os.getenv("UC_SIGNUP_PROXY", os.getenv("BROWSER_PROXY", os.getenv("PROXY", PROXY)))
PW = os.getenv("SIGNUP_PASSWORD", PW)
NAME = os.getenv("SIGNUP_NAME", NAME)
AGE = os.getenv("SIGNUP_AGE", AGE)
DISPLAY = os.getenv("UC_SIGNUP_DISPLAY", os.getenv("BROWSER_DISPLAY", DISPLAY))
CHROME_BINARY = detect_chrome_binary()
CHROME_VERSION = detect_chrome_version(CHROME_BINARY)

# ── 异常类 ──────────────────────────────────────────────
class StepError(Exception):
    """可重试的步骤错误"""
    pass

class FatalError(Exception):
    """不可恢复的错误"""
    pass

class PhoneRetry(Exception):
    """当前手机号不可用，需要同一邮箱换号重试"""
    def __init__(self, message, *, cancel_phone=False):
        super().__init__(message)
        self.cancel_phone = cancel_phone

# ── 主类 ────────────────────────────────────────────────
class SignupBot:
    def __init__(self, email=""):
        self.d = None
        self.requested_email = str(email or "").strip()

    def launch(self):
        os.environ["DISPLAY"] = DISPLAY
        opts = uc.ChromeOptions()
        opts.binary_location = CHROME_BINARY
        args = ["--no-sandbox","--disable-dev-shm-usage","--disable-gpu",
                "--lang=zh-CN","--window-size=1440,900"]
        if PROXY:
            args.append(f"--proxy-server={PROXY}")
        for a in args:
            opts.add_argument(a)
        self.d = uc.Chrome(options=opts, version_main=CHROME_VERSION)
        log(f"  webdriver={self.d.execute_script('return navigator.webdriver')}")

    # ── 页面等待 ────────────────────────────────────────
    def wait_ready(self, timeout=10):
        """等页面完全加载"""
        try:
            WebDriverWait(self.d, timeout).until(
                lambda d: d.execute_script("return document.readyState") == "complete"
            )
        except TimeoutException: pass
        time.sleep(1)

    def wait_url_contains(self, keyword, timeout=30):
        """等 URL 包含关键字"""
        for _ in range(timeout):
            if keyword in self.d.current_url: return
            time.sleep(1)
        raise StepError(f"URL等待超时: {keyword}")

    def is_error_page(self):
        """检测是否错误页"""
        t = self.d.title
        return any(k in t for k in ("Oops","error","出错了","Something went wrong"))

    # ── 元素操作（带重试）────────────────────────────────
    def _find_button(self, text):
        """找到匹配的按钮元素"""
        # 精确匹配
        for b in self.d.find_elements(By.TAG_NAME, "button"):
            try:
                bt = (b.text or "").strip()
                if bt == text: return b
            except StaleElementReferenceException: continue
        # 包含匹配（排除 "Continue with xxx"）
        for b in self.d.find_elements(By.TAG_NAME, "button"):
            try:
                bt = (b.text or "").strip()
                if text in bt and not bt.startswith("Continue with"): return b
            except StaleElementReferenceException: continue
        return None

    def click(self, text, retries=MAX_RETRIES, refresh_on_fail=True):
        """点击按钮，带重试和刷新"""
        for attempt in range(retries):
            self.wait_ready()
            btn = self._find_button(text)
            if btn:
                try:
                    log(f"  点击: {btn.text.strip()[:50]}")
                    ActionChains(self.d).move_to_element(btn).click().perform()
                    time.sleep(3)
                    return
                except Exception as e:
                    log(f"  点击失败: {e}", "warn")
            else:
                log(f"  未找到按钮: {text}", "warn")

            if attempt < retries - 1:
                if refresh_on_fail and attempt >= 1:
                    log(f"  刷新页面重试...", "warn")
                    self.d.refresh(); time.sleep(8)
                else:
                    time.sleep(2)
        raise StepError(f"点击失败(已重试{retries}次): {text}")

    def fill(self, selector, value, retries=MAX_RETRIES):
        """填输入框"""
        for attempt in range(retries):
            self.wait_ready()
            try:
                el = self.d.find_element(By.CSS_SELECTOR, selector)
                ActionChains(self.d).move_to_element(el).click().perform()
                time.sleep(0.2)
                try: el.clear()
                except: pass
                for ch in value: el.send_keys(ch); time.sleep(0.03)
                log(f"  填入: {value}")
                return
            except Exception as e:
                if attempt == retries - 1:
                    raise StepError(f"填框失败: {selector}")
                time.sleep(2)

    def fill_any(self, selectors, value):
        """尝试多个选择器"""
        for sel in selectors:
            try: self.fill(sel, value); return
            except StepError: continue
        # 兜底：找任意 input
        for inp in self.d.find_elements(By.CSS_SELECTOR, "input:not([type=hidden]):not([type=submit])"):
            try:
                ActionChains(self.d).move_to_element(inp).click().perform()
                time.sleep(0.2)
                try: inp.clear()
                except: pass
                for ch in value: inp.send_keys(ch); time.sleep(0.03)
                log(f"  填入(fb): {value}")
                return
            except: pass
        raise StepError("找不到任何输入框")

    # ── SMS/邮箱轮询 ─────────────────────────────────────
    def poll_sms(self, phone):
        deadline = time.time() + SMS_TIMEOUT_SECONDS
        attempt = 0
        while time.time() < deadline:
            attempt += 1
            try:
                r = api("GET", f"/api/phones/{phone}/code")
                code = r.get("status", {}).get("code")
                if code: return str(code)
            except: pass
            remaining = max(0, int(deadline - time.time()))
            if attempt == 1 or attempt % 3 == 0:
                log(f"  SMS 等待中，剩余约 {remaining}s")
            time.sleep(min(SMS_POLL_INTERVAL_SECONDS, max(1, deadline - time.time())))
        return None

    def poll_email(self, addr):
        for i in range(15):
            try:
                r = api("GET", f"/api/email-queue/mail/latest?address={addr}")
                item = r.get("item", {}) or r.get("mail", {})
                txt = str(item.get("decodedText","")) + " " + str(item.get("decodedSubject",""))
                m = re.search(r'\b(\d{6})\b', txt)
                if m: return m.group(1)
                if item.get("verificationCode"): return str(item["verificationCode"])
            except: pass
            if i % 3 == 0: log(f"  邮箱 {i+1}/15")
            time.sleep(10)
        return None

    def prepare_email(self):
        if not self.requested_email:
            return api("POST", "/api/temp-mail/address", {}).get("item", {}).get("address", "")

        if "@" not in self.requested_email:
            raise FatalError(f"邮箱格式无效: {self.requested_email}")

        name, domain = self.requested_email.split("@", 1)
        try:
            api("POST", "/api/temp-mail/address", {
                "name": name,
                "domain": domain,
                "enablePrefix": False,
            })
            log(f"  邮箱已创建/确认: {self.requested_email}")
        except Exception as e:
            log(f"  邮箱创建确认失败，继续使用传入邮箱: {e}", "warn")
        return self.requested_email

    def close_browser(self):
        if self.d:
            try: self.d.quit()
            except: pass
            self.d = None

    def cancel_phone(self, phone, reason=""):
        if not phone:
            return False
        try:
            result = api("POST", f"/api/phones/{phone}/cancel")
            warning = str(result.get("warning") or "").strip() if isinstance(result, dict) else ""
            if warning:
                log(f"  手机号 {phone} 取消已提交但上游暂不允许立即取消: {warning}", "warn")
            else:
                log(f"  已取消手机号 {phone}{'：' + reason if reason else ''}", "warn")
            return True
        except Exception as e:
            log(f"  取消手机号失败 {phone}: {e}", "warn")
            return False

    def wait_password_input_after_phone(self):
        deadline = time.time() + PHONE_PASSWORD_PAGE_TIMEOUT
        last_url = ""
        while time.time() < deadline:
            self.wait_ready(timeout=2)
            last_url = self.d.current_url
            try:
                if self.d.find_elements(By.CSS_SELECTOR, "input[name=new-password], input[autocomplete='new-password']"):
                    return
            except Exception:
                pass
            time.sleep(1)
        raise PhoneRetry(
            f"手机号提交后未进入创建密码页，可能已被使用: {last_url[:120]}",
            cancel_phone=False,
        )

    def register_with_phone(self, phone, email):
        full_phone = "+" + re.sub(r'\D', '', phone)
        log(f"📱 {phone}  📧 {email}")

        self.launch()

        self.d.get("https://chatgpt.com/auth/login?intent=signup")
        time.sleep(12)
        log(f"注册: {self.d.title}")

        self._step("Cookie", lambda: self.click("Accept all"))

        self._step("展开手机表单", lambda: (
            self.click("Continue with phone"), time.sleep(4)
        ))

        self._step("填手机号", lambda: (
            self.fill("input[name=phoneNumberInput]", full_phone),
            self.click("Continue")
        ))
        self.wait_password_input_after_phone()
        log(f"→ {self.d.title}")

        self._step("填密码", lambda: (
            self.fill("input[name=new-password]", PW),
            self.click("Continue")
        ))
        self.wait_url_contains("contact-verification")
        log(f"→ {self.d.title}")

        code = self.poll_sms(phone)
        if not code:
            raise PhoneRetry(f"短信验证码 {SMS_TIMEOUT_SECONDS}s 超时", cancel_phone=True)
        log(f"  SMS: {code}")

        self._step("短信验证", lambda: (
            self.fill("input[name=code]", code),
            self.click("Continue")
        ))
        time.sleep(3)
        log(f"→ {self.d.title}")

        self._step("姓名年龄", lambda: (
            self.fill("input[name=name]", NAME),
            self.fill("input[name=age]", AGE),
            self.click("Finish creating account")
        ))
        time.sleep(8)
        log(f"✅ 注册完成: {self.d.title}")
        return full_phone

    # ── 步骤执行器（带错误恢复）──────────────────────────
    def _step(self, name, fn):
        """执行一个步骤，出错时刷新并从当前页重试"""
        for attempt in range(MAX_RETRIES):
            try:
                self.wait_ready()
                # 检查是否错误页
                if self.is_error_page():
                    log(f"  [{name}] 检测到错误页，刷新...", "warn")
                    self.d.refresh(); time.sleep(8)
                    continue
                fn()
                return
            except StepError as e:
                log(f"  [{name}] {e} (attempt {attempt+1}/{MAX_RETRIES})", "warn")
                if attempt < MAX_RETRIES - 1:
                    self.d.refresh(); time.sleep(8)
            except Exception as e:
                log(f"  [{name}] {e}", "error")
                raise
        raise FatalError(f"步骤 [{name}] 失败，已重试{MAX_RETRIES}次")

    # ── 主流程 ───────────────────────────────────────────
    def run(self):
        log("=" * 55)
        log("ChatGPT 注册 → OAuth → CPA 回调")
        log("=" * 55)

        phone = email = full_phone = ""
        completed_success = False
        try:
            # ═══ 准备 ═══
            email = self.prepare_email()
            last_phone_error = ""
            phone_attempt = 0
            while True:
                phone_attempt += 1
                if PHONE_RETRY_LIMIT > 0:
                    attempt_label = f"{phone_attempt}/{PHONE_RETRY_LIMIT}"
                else:
                    attempt_label = f"{phone_attempt}/不限"
                if PHONE_RETRY_LIMIT > 0 and phone_attempt > PHONE_RETRY_LIMIT:
                    raise FatalError(f"同一邮箱换号重试已达上限: {last_phone_error}")

                phone = api("POST", "/api/purchase", {})["item"]["phoneNumber"]
                log(f"  手机号尝试 {attempt_label}")
                try:
                    full_phone = self.register_with_phone(phone, email)
                    break
                except PhoneRetry as e:
                    last_phone_error = str(e)
                    log(f"  当前手机号不可用: {e}", "warn")
                    if e.cancel_phone:
                        log("  准备取消旧手机号，随后购买新手机号并从注册页重新开始", "warn")
                        self.cancel_phone(phone, str(e))
                    else:
                        log(f"  未使用短信验证码，不取消手机号 {phone}", "warn")
                    phone = ""
                    full_phone = ""
                    self.close_browser()
                    log(f"  继续使用同一邮箱换下一个手机号，从头开始注册: {email}", "warn")
                    continue

            # ═══ Part 2: OAuth（同一浏览器，保持登录态）═══
            oa = api("GET", "/api/codex-oauth/url")
            oa_url = oa.get("url", "")
            oa_state = oa.get("state", "")
            log(f"🔗 OAuth: {oa_state}")

            self.d.get(oa_url)
            time.sleep(8)
            log(f"OAuth: {self.d.title} | {self.d.current_url[:80]}")

            url = self.d.current_url

            # 已登录 → 可能直接到 choose-account 或 consent
            if "choose-an-account" in url:
                self._step("选账户", lambda: self._click_account_button())

            elif "log-in" in url:
                # prompt=login 强制重新验证
                self._step("OAuth手机号", lambda: (
                    self.d.get("https://auth.openai.com/log-in?usernameKind=phone_number"),
                    time.sleep(5),
                    self.fill_any(["input[type=tel]"], full_phone),
                    self.click("Continue"), time.sleep(5)
                ))
                log(f"  → {self.d.title}")

                self._step("OAuth密码", lambda: (
                    self.fill_any(["input[type=password]", "input[name=current-password]"], PW),
                    self.click("Continue"), time.sleep(5)
                ))
                log(f"  → {self.d.title}")

            # 绑定邮箱
            if "add-email" in self.d.current_url.lower():
                self._step("绑定邮箱", lambda: (
                    self.fill_any(["input[type=email]", "input[name=email]"], email),
                    self.click("Continue"), time.sleep(5)
                ))
                log(f"  → {self.d.title}")

                code2 = self.poll_email(email)
                if not code2: raise FatalError("邮箱码超时")
                log(f"  邮箱码: {code2}")

                self._step("邮箱验证", lambda: (
                    self.fill("input[name=code]", code2),
                    self.click("Continue"), time.sleep(5)
                ))
                log(f"  → {self.d.title}")

            # 授权
            log(f"授权页: {self.d.title}")
            self._step("授权", lambda: self.click("Continue"))

            # ═══ Part 3: 捕获回调 → CPA ═══
            log("等待回调 localhost:1455...")
            callback_url = ""
            for _ in range(15):
                url = self.d.current_url
                if "localhost:1455" in url or "code=" in url:
                    callback_url = url
                    log(f"  ✅ 回调: {url[:120]}")
                    break
                time.sleep(2)

            if not callback_url:
                # 可能在 consent 页没点到
                self._step("重试授权", lambda: self.click("Continue"))
                time.sleep(5)
                for _ in range(10):
                    url = self.d.current_url
                    if "localhost:1455" in url or "code=" in url:
                        callback_url = url
                        log(f"  ✅ 回调: {url[:120]}")
                        break
                    time.sleep(2)

            if not callback_url:
                raise FatalError("OAuth回调超时")

            # CPA 回填
            log("📤 回填CPA...")
            result = api("POST", "/api/codex-oauth/callback",
                         {"provider": "codex", "redirect_url": callback_url})
            log(f"  回填: {json.dumps(result, ensure_ascii=False)[:200]}")

            status = api("GET", f"/api/codex-oauth/status?state={oa_state}")
            log(f"  状态: {json.dumps(status, ensure_ascii=False)[:200]}")

            files = api("GET", "/api/codex-oauth/files")
            log(f"  凭证: {json.dumps(files, ensure_ascii=False)[:500]}")

            # 清理
            try: api("POST", f"/api/phones/{phone}/finish")
            except: pass

            log("=" * 55)
            log(f"✅ 全部完成! {email}")
            completed_success = True
            return True

        except FatalError as e:
            log(f"💀 {e}", "error")
        except Exception as e:
            log(f"❌ {e}", "error")
        finally:
            if phone and not completed_success:
                self.cancel_phone(phone, "任务未完成")
            if self.d:
                try: self.d.save_screenshot("/tmp/uc_error.png")
                except: pass
            self.close_browser()
        return False

    def _click_account_button(self):
        """choose-account 页面：点第一个账户"""
        for b in self.d.find_elements(By.TAG_NAME, "button"):
            try:
                bt = (b.text or "").strip()
                if "Select account" in bt or ("+" in bt and len(bt) > 10):
                    log(f"  点击: {bt[:60]}")
                    ActionChains(self.d).move_to_element(b).click().perform()
                    time.sleep(5)
                    return
            except: pass
        raise StepError("找不到账户按钮")

# ── 入口 ────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ChatGPT 注册 + OAuth CPA 回调")
    parser.add_argument("--email", default="", help="指定本次注册使用的邮箱")
    parser.add_argument("--api-base", default="", help="本地 fuckoai API 地址")
    parser.add_argument("--proxy", default="", help="Chrome 代理地址")
    parser.add_argument("--display", default="", help="X11 DISPLAY")
    parser.add_argument("--chrome-binary", default="", help="Chrome 可执行文件路径")
    parser.add_argument("--chrome-version", type=int, default=0, help="Chrome 主版本号")
    args = parser.parse_args()

    if args.api_base:
        API = args.api_base.rstrip("/")
    if args.proxy:
        PROXY = args.proxy
    if args.display:
        DISPLAY = args.display
    if args.chrome_binary:
        CHROME_BINARY = args.chrome_binary
    if args.chrome_version:
        CHROME_VERSION = args.chrome_version

    signal.signal(signal.SIGINT, lambda s, f: sys.exit(1))
    bot = SignupBot(email=args.email)
    ok = bot.run()
    sys.exit(0 if ok else 1)
