from __future__ import annotations

import json
import hashlib
import hmac
import os
import random
import re
import signal
import string
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from email import policy
from email.parser import Parser
from html import unescape as html_unescape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote_plus, unquote, urlencode, urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
APP_NAME = "fuckoai"
CONFIG_PATH = ROOT / "config.json"
PURCHASE_CONFIG_PATH = ROOT / "data/purchase_config.json"
CONTROL_PANEL_PATH = ROOT / "control_panel.html"
EMAIL_QUEUE_PATH = ROOT / "data/email_queue.json"
CATALOG_CACHE_PATH = ROOT / "data/catalog_cache.json"
SIGNUP_URL = "https://chatgpt.com/auth/login?intent=signup"
DEFAULT_SERVICE_NAME = "OpenAI"
DEFAULT_SERVICE_CODE = "dr"
PURCHASE_FILTER_KEYS = (
    "serviceName",
    "serviceCode",
    "countryName",
    "countryCode",
    "operator",
    "maxPrice",
    "exactPrice",
    "fixedPrice",
)


APP_SETTING_FIELDS = (
    "HOST",
    "PORT",
    "HERO_SMS_API_KEY",
    "HERO_SMS_API_URL",
    "TEMP_MAIL_API_URL",
    "TEMP_MAIL_ADMIN_PASSWORD",
    "CPA_BASE_URL",
    "CPA_MANAGEMENT_KEY",
    "SIGNUP_PASSWORD",
    "SIGNUP_NAME",
    "SIGNUP_AGE",
    "BROWSER_DISPLAY",
    "BROWSER_PROXY",
    "UC_SIGNUP_PROXY",
    "UC_SIGNUP_PHONE_RETRIES",
    "UC_SIGNUP_SMS_TIMEOUT_SECONDS",
    "UC_SIGNUP_SMS_POLL_INTERVAL_SECONDS",
    "UC_SIGNUP_PHONE_PASSWORD_PAGE_TIMEOUT",
    "UC_SIGNUP_CHROME_BINARY",
    "UC_SIGNUP_CHROME_VERSION",
    "REQUEST_TIMEOUT_MS",
    "ENABLE_CORS",
    "STORE_FILE",
    "PURCHASE_CONFIG_FILE",
)

DEFAULT_APP_SETTINGS: dict[str, Any] = {
    "HOST": "0.0.0.0",
    "PORT": "3030",
    "HERO_SMS_API_KEY": "",
    "HERO_SMS_API_URL": "https://hero-sms.com/stubs/handler_api.php",
    "TEMP_MAIL_API_URL": "",
    "TEMP_MAIL_ADMIN_PASSWORD": "",
    "CPA_BASE_URL": "",
    "CPA_MANAGEMENT_KEY": "",
    "SIGNUP_PASSWORD": "FuckOAI123456!",
    "SIGNUP_NAME": "Fuck OAI",
    "SIGNUP_AGE": "18",
    "BROWSER_DISPLAY": ":1",
    "BROWSER_PROXY": "",
    "UC_SIGNUP_PROXY": "",
    "UC_SIGNUP_PHONE_RETRIES": "0",
    "UC_SIGNUP_SMS_TIMEOUT_SECONDS": "135",
    "UC_SIGNUP_SMS_POLL_INTERVAL_SECONDS": "10",
    "UC_SIGNUP_PHONE_PASSWORD_PAGE_TIMEOUT": "25",
    "UC_SIGNUP_CHROME_BINARY": "",
    "UC_SIGNUP_CHROME_VERSION": "",
    "REQUEST_TIMEOUT_MS": "15000",
    "ENABLE_CORS": "true",
    "STORE_FILE": "./data/activations.json",
    "PURCHASE_CONFIG_FILE": "./data/purchase_config.json",
}


def load_env_file(path: Path, *, allowed_keys: set[str] | None = None) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if allowed_keys is not None and key not in allowed_keys:
            continue
        if key and key not in os.environ:
            os.environ[key] = value


load_env_file(ROOT / ".env", allowed_keys={"ADMIN_PASSWORD"})


def load_json_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_json_file(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp_path.replace(path)


def load_config_values() -> dict[str, str]:
    file_config = load_json_file(CONFIG_PATH)
    values = {key: str(DEFAULT_APP_SETTINGS.get(key, "")) for key in APP_SETTING_FIELDS}
    for key in APP_SETTING_FIELDS:
        if key in file_config and file_config[key] is not None:
            values[key] = str(file_config[key])
    return values


def write_config_values(values: dict[str, Any]) -> None:
    serialized = {key: str(values.get(key, DEFAULT_APP_SETTINGS.get(key, ""))) for key in APP_SETTING_FIELDS}
    save_json_file(CONFIG_PATH, serialized)


APP_CONFIG_VALUES = load_config_values()


def app_config_value(key: str, default: Any = "") -> str:
    return str(APP_CONFIG_VALUES.get(key, DEFAULT_APP_SETTINGS.get(key, default)))


def parse_bool_flag(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def normalize_fixed_price_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value or "").strip().lower()
    return "true" if text in {"1", "true", "yes", "on"} else "false" if text in {"0", "false", "no", "off"} else text


@dataclass
class UcSignupState:
    running: bool = False
    stop_requested: bool = False
    total: int = 0
    completed: int = 0
    success: int = 0
    failed: int = 0
    current_index: int = 0
    current_email: str = ""
    current_phone: str = ""
    current_step: str = ""
    phase: str = "idle"
    started_at: str = ""
    updated_at: str = ""
    current_pid: int | None = None
    results: list[dict[str, Any]] = None
    errors: list[dict[str, str]] = None
    log_lines: list[dict[str, str]] = None

    def __post_init__(self) -> None:
        if self.results is None:
            self.results = []
        if self.errors is None:
            self.errors = []
        if self.log_lines is None:
            self.log_lines = []

    def to_dict(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "stopRequested": self.stop_requested,
            "total": self.total,
            "completed": self.completed,
            "success": self.success,
            "failed": self.failed,
            "currentIndex": self.current_index,
            "currentEmail": self.current_email,
            "currentPhone": self.current_phone,
            "currentStep": self.current_step,
            "phase": self.phase,
            "startedAt": self.started_at,
            "updatedAt": self.updated_at,
            "currentPid": self.current_pid,
            "results": list(self.results),
            "errors": list(self.errors),
            "logLines": list(self.log_lines),
        }


@dataclass
class Config:
    host: str = app_config_value("HOST", "0.0.0.0")
    port: int = int(app_config_value("PORT", "3030"))
    api_key: str = app_config_value("HERO_SMS_API_KEY", "")
    api_url: str = app_config_value("HERO_SMS_API_URL", "https://hero-sms.com/stubs/handler_api.php")
    default_service_name: str = DEFAULT_SERVICE_NAME
    default_service_code: str = DEFAULT_SERVICE_CODE
    default_service_aliases: list[str] = None
    default_country_name: str = ""
    default_country_code: str = ""
    default_country_aliases: list[str] = None
    default_operator: str = "any"
    default_max_price: str = ""
    default_exact_price: str = ""
    default_fixed_price: str = ""
    timeout_ms: int = int(app_config_value("REQUEST_TIMEOUT_MS", "15000"))
    enable_cors: bool = app_config_value("ENABLE_CORS", "true").lower() == "true"
    store_file: Path = ROOT / app_config_value("STORE_FILE", "./data/activations.json")
    purchase_config_file: Path = PURCHASE_CONFIG_PATH
    temp_mail_api_url: str = app_config_value("TEMP_MAIL_API_URL", "")
    temp_mail_admin_password: str = app_config_value("TEMP_MAIL_ADMIN_PASSWORD", "")
    cpa_base_url: str = app_config_value("CPA_BASE_URL", "")
    cpa_management_key: str = app_config_value("CPA_MANAGEMENT_KEY", "")
    browser_display: str = app_config_value("BROWSER_DISPLAY", ":1")
    browser_proxy: str = app_config_value("BROWSER_PROXY", "")
    uc_signup_proxy: str = app_config_value("UC_SIGNUP_PROXY", "")
    uc_signup_phone_retries: str = app_config_value("UC_SIGNUP_PHONE_RETRIES", "0")
    uc_signup_sms_timeout_seconds: str = app_config_value("UC_SIGNUP_SMS_TIMEOUT_SECONDS", "135")
    uc_signup_sms_poll_interval_seconds: str = app_config_value("UC_SIGNUP_SMS_POLL_INTERVAL_SECONDS", "10")
    uc_signup_phone_password_page_timeout: str = app_config_value("UC_SIGNUP_PHONE_PASSWORD_PAGE_TIMEOUT", "25")
    uc_signup_chrome_binary: str = app_config_value("UC_SIGNUP_CHROME_BINARY", "")
    uc_signup_chrome_version: str = app_config_value("UC_SIGNUP_CHROME_VERSION", "")
    signup_password: str = app_config_value("SIGNUP_PASSWORD", "FuckOAI123456!")
    signup_name: str = app_config_value("SIGNUP_NAME", "Fuck OAI")
    signup_age: str = app_config_value("SIGNUP_AGE", "18")
    admin_password: str = os.getenv("ADMIN_PASSWORD", "")

    def __post_init__(self) -> None:
        self.default_service_aliases = [
            item.strip()
            for item in "OpenAI,ChatGPT".split(",")
            if item.strip()
        ]
        self.default_country_aliases = []
        self.store_file = (ROOT / app_config_value("STORE_FILE", "./data/activations.json")).resolve()
        self.purchase_config_file = (ROOT / app_config_value("PURCHASE_CONFIG_FILE", "./data/purchase_config.json")).resolve()
        self.temp_mail_api_url = app_config_value("TEMP_MAIL_API_URL", "").rstrip("/")
        self.temp_mail_admin_password = app_config_value("TEMP_MAIL_ADMIN_PASSWORD", "")
        self.cpa_base_url = app_config_value("CPA_BASE_URL", "").rstrip("/")
        self.cpa_management_key = app_config_value("CPA_MANAGEMENT_KEY", "")
        self.browser_display = app_config_value("BROWSER_DISPLAY", ":1")
        self.browser_proxy = app_config_value("BROWSER_PROXY", "")
        self.uc_signup_proxy = app_config_value("UC_SIGNUP_PROXY", "")
        self.uc_signup_phone_retries = app_config_value("UC_SIGNUP_PHONE_RETRIES", "0")
        self.uc_signup_sms_timeout_seconds = app_config_value("UC_SIGNUP_SMS_TIMEOUT_SECONDS", "135")
        self.uc_signup_sms_poll_interval_seconds = app_config_value("UC_SIGNUP_SMS_POLL_INTERVAL_SECONDS", "10")
        self.uc_signup_phone_password_page_timeout = app_config_value("UC_SIGNUP_PHONE_PASSWORD_PAGE_TIMEOUT", "25")
        self.uc_signup_chrome_binary = app_config_value("UC_SIGNUP_CHROME_BINARY", "")
        self.uc_signup_chrome_version = app_config_value("UC_SIGNUP_CHROME_VERSION", "")
        self.signup_password = app_config_value("SIGNUP_PASSWORD", "FuckOAI123456!")
        self.signup_name = app_config_value("SIGNUP_NAME", "Fuck OAI")
        self.signup_age = app_config_value("SIGNUP_AGE", "18")
        self.admin_password = os.getenv("ADMIN_PASSWORD", "")


CONFIG = Config()

ENV_PATH = ROOT / ".env"


STATUS_LABELS = {
    "STATUS_WAIT_CODE": "等待验证码",
    "STATUS_WAIT_RETRY": "等待重发",
    "STATUS_WAIT_RESEND": "等待再次发送",
    "STATUS_WAIT_ACTIVATION": "等待激活",
    "STATUS_WAIT_GET": "号码已下发",
    "STATUS_OK": "收到验证码",
    "STATUS_CANCEL": "已取消",
    "FULL_SMS": "短信已满",
}

NORMALIZED_STATES = {
    "STATUS_WAIT_CODE": "waiting_for_code",
    "STATUS_WAIT_RETRY": "waiting_for_retry",
    "STATUS_WAIT_RESEND": "waiting_for_resend",
    "STATUS_WAIT_ACTIVATION": "waiting_for_activation",
    "STATUS_WAIT_GET": "number_issued",
    "STATUS_OK": "code_received",
    "STATUS_CANCEL": "canceled",
    "FULL_SMS": "finished",
}

ACTIVE_STATUS_MAP = {
    "1": ("waiting_for_code", "等待验证码", "STATUS_WAIT_CODE"),
    "3": ("waiting_for_retry", "等待重发", "STATUS_WAIT_RETRY"),
    "4": ("number_issued", "号码已下发", "STATUS_WAIT_GET"),
    "6": ("finished", "已完成", "FULL_SMS"),
    "8": ("canceled", "已取消", "STATUS_CANCEL"),
}

ZH_COUNTRY_CHAR_MAP = str.maketrans(
    {
        "國": "国",
        "爾": "尔",
        "亞": "亚",
        "達": "达",
        "蘭": "兰",
        "義": "义",
        "羅": "罗",
        "馬": "马",
        "維": "维",
        "貝": "贝",
        "麥": "麦",
        "臘": "腊",
        "盧": "卢",
        "門": "门",
        "臺": "台",
        "灣": "湾",
        "烏": "乌",
        "魯": "鲁",
        "薩": "萨",
        "聖": "圣",
        "幾": "几",
        "納": "纳",
        "剛": "刚",
        "島": "岛",
        "裡": "里",
        "蘇": "苏",
        "聯": "联",
        "長": "长",
        "茲": "兹",
        "團": "团",
        "圓": "圆",
        "贊": "赞",
        "歐": "欧",
        "愛": "爱",
        "倫": "伦",
        "屬": "属",
        "與": "与",
        "內": "内",
        "庫": "库",
        "錫": "锡",
    }
)

COUNTRY_ALIASES_BY_NAME = {
    "bhutan": ["不丹"],
    "france": ["法国"],
    "italy": ["意大利"],
    "reunion": ["留尼汪"],
    "georgia": ["格鲁吉亚"],
    "england": ["英国", "英格兰"],
    "united kingdom": ["英国"],
    "united states": ["美国"],
    "uae": ["阿联酋"],
    "united arab emirates": ["阿联酋"],
    "ivory coast": ["科特迪瓦"],
    "laos": ["老挝"],
    "syria": ["叙利亚"],
    "vietnam": ["越南"],
    "south korea": ["韩国"],
    "north macedonia": ["北马其顿"],
    "bosnia and herzegovina": ["波黑", "波斯尼亚和黑塞哥维那"],
    "democratic republic of the congo": ["刚果金"],
    "republic of the congo": ["刚果布"],
}


def normalize_text(value: Any) -> str:
    text = str(value or "").translate(ZH_COUNTRY_CHAR_MAP).lower()
    return "".join(ch for ch in text if ch.isalnum())


def collect_string_values(value: Any) -> list[str]:
    values: list[str] = []
    if isinstance(value, dict):
        for item in value.values():
            values.extend(collect_string_values(item))
    elif isinstance(value, list):
        for item in value:
            values.extend(collect_string_values(item))
    elif isinstance(value, str):
        text = value.strip()
        if text:
            values.append(text)
    return values


def now_iso() -> str:
    return datetime.now().astimezone().isoformat()


def html_to_text(value: str) -> str:
    text = str(value or "")
    text = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", text)
    text = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", text)
    text = re.sub(r"(?is)<!--.*?-->", " ", text)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p\s*>", "\n", text)
    text = re.sub(r"(?is)<[^>]+>", " ", text)
    text = html_unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def decode_mail_payload(raw: str) -> dict[str, str]:
    payload = {"subject": "", "text": "", "html": ""}
    source = str(raw or "").strip()
    if not source:
        return payload
    try:
        message = Parser(policy=policy.default).parsestr(source)
    except Exception:
        payload["text"] = source
        return payload

    payload["subject"] = str(message.get("subject") or "").strip()
    text_parts: list[str] = []
    html_parts: list[str] = []
    parts = message.walk() if message.is_multipart() else [message]
    for part in parts:
        if getattr(part, "is_multipart", lambda: False)():
            continue
        content_type = str(part.get_content_type() or "").lower()
        if content_type not in {"text/plain", "text/html"}:
            continue
        try:
            content = part.get_content()
        except Exception:
            try:
                content = part.get_payload(decode=True)
            except Exception:
                content = ""
            if isinstance(content, bytes):
                charset = part.get_content_charset() or "utf-8"
                content = content.decode(charset, errors="replace")
        if not isinstance(content, str):
            content = str(content or "")
        if content_type == "text/plain":
            text_parts.append(content)
        else:
            html_parts.append(content)
    payload["text"] = re.sub(r"\s+", " ", " ".join(text_parts)).strip()
    payload["html"] = " ".join(html_parts).strip()
    return payload


def extract_verification_code_from_mail(item: dict[str, Any] | None) -> tuple[str | None, str, str]:
    if not isinstance(item, dict):
        return None, "", ""
    decoded = decode_mail_payload(str(item.get("raw") or ""))
    subject = decoded["subject"]
    visible_text = html_to_text(decoded["html"]) or decoded["text"]
    for source in (visible_text, decoded["text"], subject):
        match = re.search(r"(?<!\d)(\d{6})(?!\d)", source or "")
        if match:
            return match.group(1), subject, visible_text
    combined = f"{subject}\n{visible_text}\n{decoded['text']}"
    targeted_patterns = [
        r"(?:verification code|temporary code|验证码|临时验证码|输入此临时验证码以继续)[^\d]{0,40}(\d{6})",
        r"(?<!\d)(\d{6})(?!\d)",
    ]
    for pattern in targeted_patterns:
        match = re.search(pattern, combined, flags=re.IGNORECASE)
        if match:
            return match.group(1), subject, visible_text
    return None, subject, visible_text


def enrich_temp_mail_item(item: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return item
    code, subject, visible_text = extract_verification_code_from_mail(item)
    enriched: dict[str, Any] = {}
    if code:
        enriched["verificationCode"] = code
    if subject:
        enriched["decodedSubject"] = subject
    if visible_text:
        enriched["decodedText"] = visible_text
    enriched.update(item)
    return enriched


class ActivationStore:
    def __init__(self, file_path: Path) -> None:
        self.file_path = file_path
        self.ensure_store()

    def ensure_store(self) -> None:
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.file_path.exists():
            self.file_path.write_text("[]\n", encoding="utf-8")

    def read_all(self) -> list[dict[str, Any]]:
        self.ensure_store()
        raw = self.file_path.read_text(encoding="utf-8").strip()
        if not raw:
            return []
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []

    def write_all(self, records: list[dict[str, Any]]) -> None:
        self.ensure_store()
        self.file_path.write_text(json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def list(self) -> list[dict[str, Any]]:
        return sorted(
            self.read_all(),
            key=lambda item: item.get("updatedAt") or item.get("purchasedAt") or "",
            reverse=True,
        )

    def get(self, activation_id: str) -> dict[str, Any] | None:
        for record in self.read_all():
            if str(record.get("id")) == str(activation_id):
                return record
        return None

    def upsert(self, next_record: dict[str, Any]) -> dict[str, Any]:
        records = self.read_all()
        now = now_iso()
        merged = {
            "purchasedAt": now,
            "updatedAt": now,
            "codes": [],
        }
        index = -1
        for i, record in enumerate(records):
            if str(record.get("id")) == str(next_record.get("id")):
                index = i
                merged.update(record)
                break
        merged.update(next_record)
        merged["updatedAt"] = now
        merged.setdefault("purchasedAt", now)
        merged["codes"] = [str(code) for code in merged.get("codes", []) if str(code)]
        if index == -1:
            records.append(merged)
        else:
            records[index] = merged
        self.write_all(records)
        return merged

    def append_code(self, activation_id: str, code: str | None) -> dict[str, Any] | None:
        if not code:
            return self.get(activation_id)
        record = self.get(activation_id)
        if not record:
            return None
        codes = [str(item) for item in record.get("codes", []) if str(item)]
        code_str = str(code)
        if code_str not in codes:
            codes.insert(0, code_str)
        record["codes"] = codes
        record["lastCode"] = code_str
        return self.upsert(record)


STORE = ActivationStore(CONFIG.store_file)


class HeroSmsError(Exception):
    pass


class TempMailError(Exception):
    pass


class CpaError(Exception):
    pass


class HeroSmsClient:
    def __init__(self, api_key: str, api_url: str, timeout_ms: int) -> None:
        self.api_key = api_key
        self.api_url = api_url
        self.timeout_seconds = timeout_ms / 1000
        self.cache_ttl_seconds = 600
        self._cache: dict[str, dict[str, Any]] = {}

    def _get_cached(self, key: str) -> Any | None:
        cached = self._cache.get(key)
        if not cached:
            return None
        expires_at = cached.get("expiresAt", 0)
        if datetime.now().timestamp() >= expires_at:
            self._cache.pop(key, None)
            return None
        return cached.get("value")

    def _set_cached(self, key: str, value: Any) -> Any:
        self._cache[key] = {
            "value": value,
            "expiresAt": datetime.now().timestamp() + self.cache_ttl_seconds,
        }
        return value

    def request(self, action: str, **params: Any) -> Any:
        if not self.api_key:
            raise HeroSmsError("未配置 HERO_SMS_API_KEY")

        query = {"api_key": self.api_key, "action": action}
        for key, value in params.items():
            if value in (None, ""):
                continue
            query[key] = str(value)

        request_url = f"{self.api_url}?{urlencode(query)}"
        request = Request(
            request_url,
            headers={"Accept": "application/json,text/plain;q=0.9,*/*;q=0.8", "User-Agent": "python-herosms-client/1.0"},
        )

        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                text = response.read().decode("utf-8", errors="replace").strip()
        except HTTPError as error:
            body = error.read().decode("utf-8", errors="replace").strip()
            raise HeroSmsError(f"上游请求失败: HTTP {error.code} {body}".strip())
        except URLError as error:
            raise HeroSmsError(f"上游连接失败: {error.reason}")

        if not text:
            return ""
        if text.startswith("{") or text.startswith("["):
            payload = json.loads(text)
        else:
            payload = text

        if isinstance(payload, str) and payload.startswith(("BAD_", "ERROR_", "NO_", "WRONG_", "SQL_")):
            raise HeroSmsError(payload)
        return payload

    def get_balance(self) -> Any:
        return self.request("getBalance")

    def get_balance_cached(self, force: bool = False) -> Any:
        if not force:
            cached = self._get_cached("balance")
            if cached is not None:
                return cached
        balance = self.get_balance()
        return self._set_cached("balance", balance)

    def get_services(self) -> list[dict[str, str]]:
        cached = self._get_cached("services")
        if cached is not None:
            return cached
        services = self._normalize_services(self.request("getServicesList"))
        return self._set_cached("services", services)

    def get_countries(self, force: bool = False) -> list[dict[str, Any]]:
        cached = None if force else self._get_cached("countries")
        if cached is not None:
            return cached
        countries = self._normalize_countries(self.request("getCountries"))
        return self._set_cached("countries", countries)

    def resolve_service(self, name: str, aliases: list[str]) -> tuple[dict[str, str], list[dict[str, str]]]:
        services = self.get_services()
        match = self._pick_by_name(services, name, aliases, ("name", "code"))
        if not match:
            raise HeroSmsError(f"找不到服务: {name}")
        return match, services

    def resolve_country(self, name: str, aliases: list[str]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        countries = self.get_countries()
        match = self._pick_by_name(countries, name, aliases, ("name", "localName", "code"))
        if not match:
            raise HeroSmsError(f"找不到国家/地区: {name}")
        return match, countries

    def get_pricing(self, service_code: str, country_code: str) -> dict[str, Any]:
        cache_key = f"pricing:{service_code}:{country_code}"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached
        payload = self.request("getPrices", service=service_code, country=country_code)
        parsed = self._extract_price_info(payload, country_code, service_code)
        if parsed:
            return self._set_cached(cache_key, parsed)
        fallback = self.request("getPricesVerification", service=service_code, country=country_code)
        result = self._extract_price_info(fallback, country_code, service_code) or {"price": None, "count": None, "raw": fallback}
        return self._set_cached(cache_key, result)

    def get_operators(self, service_code: str, country_code: str, force: bool = False) -> list[str]:
        cache_key = f"operators:{service_code}:{country_code}"
        cached = None if force else self._get_cached(cache_key)
        if cached is not None:
            return cached
        try:
            payload = self.request("getOperators", country=country_code)
        except HeroSmsError:
            try:
                payload = self.request("getPricesVerification", service=service_code, country=country_code)
            except HeroSmsError:
                return self._set_cached(cache_key, ["any"])
        if isinstance(payload, dict):
            source = (
                payload.get("operators")
                or payload.get("countryOperators")
                or payload.get("data")
                or payload.get("items")
                or payload
            )
        else:
            source = payload
        if isinstance(source, dict):
            source = source.get(country_code) or source.get(str(int(country_code)) if str(country_code).isdigit() else country_code) or source
        if not source:
            return self._set_cached(cache_key, ["any"])
        values = []
        if isinstance(source, dict):
            iterator = source.values()
        else:
            iterator = source
        for item in iterator:
            if isinstance(item, dict):
                value = item.get("name") or item.get("code") or item.get("value")
            else:
                value = item
            if value:
                values.append(str(value))
        result = sorted(set(values))
        return self._set_cached(cache_key, result or ["any"])

    def buy_activation(self, *, service_code: str, country_code: str, operator: str, max_price: str | None) -> dict[str, Any]:
        payload = self.request(
            "getNumberV2",
            service=service_code,
            country=country_code,
            operator=operator or "any",
            maxPrice=max_price or "",
        )
        return self._parse_purchase_payload(payload, service_code, country_code, operator)

    def buy_activation_fixed_price(
        self,
        *,
        service_code: str,
        country_code: str,
        operator: str,
        exact_price: str,
    ) -> dict[str, Any]:
        payload = self.request(
            "getNumber",
            service=service_code,
            country=country_code,
            operator=operator or "any",
            maxPrice=exact_price,
            fixedPrice="true",
        )
        return self._parse_purchase_payload(payload, service_code, country_code, operator)

    def get_status(self, activation_id: str) -> dict[str, Any]:
        payload = self.request("getStatus", id=activation_id)
        return self._parse_status_payload(payload)

    def set_status(self, activation_id: str, status: int) -> dict[str, Any]:
        payload = self.request("setStatus", id=activation_id, status=status)
        return {"raw": payload, "result": str(payload)}

    def get_active_activations(self) -> list[dict[str, Any]]:
        payload = self.request("getActiveActivations")
        if isinstance(payload, dict):
            active = payload.get("activeActivations")
            if isinstance(active, dict) and isinstance(active.get("rows"), list):
                return active["rows"]
            if isinstance(payload.get("data"), list):
                return payload["data"]
        return []

    @staticmethod
    def _normalize_services(payload: Any) -> list[dict[str, str]]:
        if isinstance(payload, dict) and isinstance(payload.get("services"), list):
            items = payload.get("services", [])
        elif isinstance(payload, dict):
            items = [{"code": key, **value} if isinstance(value, dict) else {"code": key, "name": value} for key, value in payload.items()]
        elif isinstance(payload, list):
            items = payload
        else:
            items = []
        result = []
        for item in items:
            code = str(item.get("code") or item.get("id") or item.get("value") or item.get("shortName") or "")
            name = str(item.get("name") or item.get("title") or item.get("text") or item.get("service") or "").strip()
            if code and name:
                result.append({"code": code, "name": name})
        return sorted(result, key=lambda item: item["name"])

    @staticmethod
    def _normalize_countries(payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, dict) and isinstance(payload.get("countries"), list):
            items = payload.get("countries", [])
        elif isinstance(payload, dict):
            items = [{"code": key, **value} if isinstance(value, dict) else {"code": key, "name": value} for key, value in payload.items()]
        elif isinstance(payload, list):
            items = payload
        else:
            items = []
        result = []
        for item in items:
            code = str(item.get("code") or item.get("id") or item.get("value") or "")
            name = str(item.get("eng") or item.get("name") or item.get("text") or "").strip()
            local_name = str(
                item.get("chn")
                or item.get("cn")
                or item.get("chinese")
                or item.get("name_cn")
                or item.get("rus")
                or item.get("localName")
                or item.get("name")
                or ""
            ).strip()
            search_terms = []
            seen_terms: set[str] = set()
            for value in [code, *collect_string_values(item)]:
                normalized = normalize_text(value)
                if normalized and normalized not in seen_terms:
                    seen_terms.add(normalized)
                    search_terms.append(str(value).strip())
            if code and (name or local_name):
                result.append(
                    {
                        "code": code,
                        "name": name,
                        "localName": local_name,
                        "searchTerms": search_terms,
                        "retry": bool(item.get("retry")),
                        "rent": bool(item.get("rent")),
                        "multiService": bool(item.get("multiService")),
                    }
                )
        return sorted(result, key=lambda item: item["name"] or item["localName"])

    @staticmethod
    def _pick_by_name(items: list[dict[str, Any]], preferred: str, aliases: list[str], fields: tuple[str, ...]) -> dict[str, Any] | None:
        targets = [normalize_text(value) for value in [preferred, *aliases] if value]
        for target in targets:
            for item in items:
                search_terms = [item.get(field) for field in fields]
                if isinstance(item.get("searchTerms"), list):
                    search_terms.extend(item.get("searchTerms"))
                if any(normalize_text(term) == target for term in search_terms):
                    return item
        for target in targets:
            for item in items:
                search_terms = [item.get(field) for field in fields]
                if isinstance(item.get("searchTerms"), list):
                    search_terms.extend(item.get("searchTerms"))
                if any(
                    target in normalize_text(term) or normalize_text(term) in target
                    for term in search_terms
                    if term
                ):
                    return item
        return items[0] if items else None

    @staticmethod
    def _extract_price_info(payload: Any, country_code: str, service_code: str) -> dict[str, Any] | None:
        if not isinstance(payload, dict):
            return None
        country_entry = payload.get(country_code) or payload.get(str(int(country_code))) if str(country_code).isdigit() else payload.get(country_code)
        country_entry = country_entry or payload.get("country") or payload.get("countries", {}).get(country_code) or payload
        service_entry = None
        if isinstance(country_entry, dict):
            service_entry = country_entry.get(service_code) or country_entry.get("services", {}).get(service_code) or country_entry.get("services", {}).get("full")
        service_entry = service_entry or payload.get("services", {}).get(service_code) if isinstance(payload.get("services"), dict) else service_entry
        service_entry = service_entry or payload
        if not isinstance(service_entry, dict):
            return None
        price = service_entry.get("cost") or service_entry.get("price") or service_entry.get("activationCost")
        count = service_entry.get("count") or service_entry.get("quant") or service_entry.get("qty") or service_entry.get("available")
        if price is None and count is None:
            return None
        return {
            "price": float(price) if price is not None else None,
            "count": int(count) if count is not None else None,
            "raw": payload,
        }

    @staticmethod
    def _parse_purchase_payload(payload: Any, service_code: str, country_code: str, operator: str) -> dict[str, Any]:
        if isinstance(payload, dict):
            return {
                "id": str(payload.get("activationId") or payload.get("id") or payload.get("activationID") or ""),
                "phoneNumber": str(payload.get("phoneNumber") or payload.get("phone") or payload.get("number") or ""),
                "activationCost": payload.get("activationCost") or payload.get("cost"),
                "countryCode": str(payload.get("countryCode") or country_code),
                "serviceCode": str(payload.get("activationService") or service_code),
                "operator": str(payload.get("activationOperator") or payload.get("operator") or operator or "any"),
                "canGetAnotherSms": bool(payload.get("canGetAnotherSms")),
                "raw": payload,
            }
        text = str(payload).strip()
        if text.startswith("ACCESS_NUMBER"):
            _, activation_id, phone_number = text.split(":", 2)
            return {
                "id": activation_id,
                "phoneNumber": phone_number,
                "activationCost": None,
                "countryCode": country_code,
                "serviceCode": service_code,
                "operator": operator or "any",
                "canGetAnotherSms": False,
                "raw": text,
            }
        raise HeroSmsError(f"无法解析购号响应: {text}")

    @staticmethod
    def _parse_status_payload(payload: Any) -> dict[str, Any]:
        if isinstance(payload, dict):
            upstream_status = payload.get("status") or payload.get("code") or payload.get("state") or "UNKNOWN"
            sms_code = payload.get("smsCode") or payload.get("codeValue") or payload.get("sms")
            return {
                "raw": payload,
                "upstreamStatus": upstream_status,
                "localStatus": NORMALIZED_STATES.get(upstream_status, "unknown"),
                "label": STATUS_LABELS.get(upstream_status, upstream_status),
                "code": str(sms_code) if sms_code else None,
            }
        text = str(payload).strip()
        parts = text.split(":", 1)
        upstream_status = parts[0] if parts else "UNKNOWN"
        sms_code = parts[1] if len(parts) > 1 else None
        return {
            "raw": text,
            "upstreamStatus": upstream_status,
            "localStatus": NORMALIZED_STATES.get(upstream_status, "unknown"),
            "label": STATUS_LABELS.get(upstream_status, upstream_status),
            "code": sms_code,
        }


class TempMailClient:
    def __init__(self, base_url: str, admin_password: str, timeout_ms: int) -> None:
        self.base_url = base_url.rstrip("/")
        self.admin_password = admin_password
        self.timeout_seconds = timeout_ms / 1000

    def _request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:
        if not self.base_url:
            raise TempMailError("未配置 TEMP_MAIL_API_URL")
        url = f"{self.base_url}{path}"
        request_headers = {
            "Accept": "application/json,text/plain;q=0.9,*/*;q=0.8",
            "User-Agent": "python-tempmail-client/1.0",
        }
        if headers:
            request_headers.update(headers)
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        request = Request(url, data=data, method=method, headers=request_headers)
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                text = response.read().decode("utf-8", errors="replace").strip()
        except HTTPError as error:
            body_text = error.read().decode("utf-8", errors="replace").strip()
            raise TempMailError(f"临时邮箱请求失败: HTTP {error.code} {body_text}".strip())
        except URLError as error:
            raise TempMailError(f"临时邮箱连接失败: {error.reason}")
        if not text:
            return {}
        if text.startswith("{") or text.startswith("["):
            return json.loads(text)
        return text

    def _admin_headers(self) -> dict[str, str]:
        if not self.admin_password:
            raise TempMailError("未配置 TEMP_MAIL_ADMIN_PASSWORD")
        return {"x-admin-auth": self.admin_password}

    def _user_headers(self, jwt: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {jwt}"}

    def get_settings(self) -> dict[str, Any]:
        payload = self._request("GET", "/open_api/settings")
        return payload if isinstance(payload, dict) else {}

    def create_address(self, name: str, domain: str, enable_prefix: bool = True) -> dict[str, Any]:
        payload = self._request(
            "POST",
            "/admin/new_address",
            headers=self._admin_headers(),
            body={
                "enablePrefix": enable_prefix,
                "name": name,
                "domain": domain,
            },
        )
        if not isinstance(payload, dict):
            raise TempMailError("创建邮箱返回格式异常")
        return payload

    def show_address_password(self, address: str) -> dict[str, Any]:
        payload = self._request("GET", f"/admin/show_password/{address}", headers=self._admin_headers())
        if not isinstance(payload, dict):
            raise TempMailError("获取邮箱凭证返回格式异常")
        return payload

    def list_mails(self, address: str, limit: int = 20, offset: int = 0) -> dict[str, Any]:
        payload = self._request(
            "GET",
            f"/admin/mails?limit={int(limit)}&offset={int(offset)}&address={quote_plus(address)}",
            headers=self._admin_headers(),
        )
        if not isinstance(payload, dict):
            raise TempMailError("邮件列表返回格式异常")
        return payload

    def latest_mail(self, address: str) -> dict[str, Any] | None:
        payload = self.list_mails(address, limit=1, offset=0)
        results = payload.get("results") or []
        return results[0] if results else None

    def delete_address(self, address: str) -> dict[str, Any]:
        payload = self._request("DELETE", f"/admin/delete_address/{address}", headers=self._admin_headers())
        if isinstance(payload, dict):
            return payload
        return {"success": str(payload).lower() == "true", "raw": payload}


class CpaClient:
    def __init__(self, base_url: str, management_key: str, timeout_ms: int) -> None:
        self.base_url = base_url.rstrip("/")
        self.management_key = management_key
        self.timeout_seconds = timeout_ms / 1000

    def _headers(self) -> dict[str, str]:
        if not self.management_key:
            raise CpaError("未配置 CPA_MANAGEMENT_KEY")
        return {
            "Authorization": f"Bearer {self.management_key}",
            "X-Management-Key": self.management_key,
            "Content-Type": "application/json",
            "Accept": "application/json,text/plain;q=0.9,*/*;q=0.8",
            "User-Agent": "python-cpa-client/1.0",
        }

    def _request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:
        if not self.base_url:
            raise CpaError("未配置 CPA_BASE_URL")
        url = f"{self.base_url}{path}"
        if query:
            query_string = urlencode({key: value for key, value in query.items() if value not in (None, "")})
            if query_string:
                url = f"{url}?{query_string}"
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
        request = Request(url, data=data, method=method, headers=self._headers())
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                text = response.read().decode("utf-8", errors="replace").strip()
        except HTTPError as error:
            body_text = error.read().decode("utf-8", errors="replace").strip()
            raise CpaError(f"CPA 请求失败: HTTP {error.code} {body_text}".strip())
        except URLError as error:
            raise CpaError(f"CPA 连接失败: {error.reason}")
        if not text:
            return {}
        if text.startswith("{") or text.startswith("["):
            return json.loads(text)
        return text

    def get_codex_auth_url(self) -> dict[str, Any]:
        payload = self._request("GET", "/v0/management/codex-auth-url")
        return payload if isinstance(payload, dict) else {"raw": payload}

    def oauth_callback(
        self,
        *,
        provider: str = "codex",
        redirect_url: str = "",
        code: str = "",
        state: str = "",
    ) -> dict[str, Any]:
        body = {"provider": provider}
        if redirect_url:
            body["redirect_url"] = redirect_url
        else:
            body["code"] = code
            body["state"] = state
        payload = self._request("POST", "/v0/management/oauth-callback", body=body)
        return payload if isinstance(payload, dict) else {"raw": payload}

    def get_auth_status(self, state: str) -> dict[str, Any]:
        payload = self._request("GET", "/v0/management/get-auth-status", query={"state": state})
        return payload if isinstance(payload, dict) else {"raw": payload}

    def get_auth_files(self) -> Any:
        return self._request("GET", "/v0/management/auth-files")


CLIENT = HeroSmsClient(CONFIG.api_key, CONFIG.api_url, CONFIG.timeout_ms)
TEMP_MAIL = TempMailClient(CONFIG.temp_mail_api_url, CONFIG.temp_mail_admin_password, CONFIG.timeout_ms)
CPA = CpaClient(CONFIG.cpa_base_url, CONFIG.cpa_management_key, CONFIG.timeout_ms)
PURCHASE_GROUP_CURSOR_LOCK = threading.Lock()
PURCHASE_GROUP_NEXT_INDEX = 0


def reload_runtime_config() -> None:
    global APP_CONFIG_VALUES, CLIENT, TEMP_MAIL, CPA

    APP_CONFIG_VALUES = load_config_values()
    CONFIG.host = app_config_value("HOST", "0.0.0.0")
    CONFIG.port = int(app_config_value("PORT", str(CONFIG.port)))
    CONFIG.api_key = app_config_value("HERO_SMS_API_KEY", "")
    CONFIG.api_url = app_config_value("HERO_SMS_API_URL", "https://hero-sms.com/stubs/handler_api.php")
    CONFIG.default_service_name = DEFAULT_SERVICE_NAME
    CONFIG.default_service_code = DEFAULT_SERVICE_CODE
    CONFIG.default_country_name = ""
    CONFIG.default_country_code = ""
    CONFIG.default_operator = "any"
    CONFIG.default_max_price = ""
    CONFIG.default_exact_price = ""
    CONFIG.default_fixed_price = ""
    CONFIG.timeout_ms = int(app_config_value("REQUEST_TIMEOUT_MS", str(CONFIG.timeout_ms)))
    CONFIG.enable_cors = app_config_value("ENABLE_CORS", "true").lower() == "true"
    CONFIG.store_file = (ROOT / app_config_value("STORE_FILE", "./data/activations.json")).resolve()
    CONFIG.purchase_config_file = (ROOT / app_config_value("PURCHASE_CONFIG_FILE", "./data/purchase_config.json")).resolve()
    CONFIG.temp_mail_api_url = app_config_value("TEMP_MAIL_API_URL", "").rstrip("/")
    CONFIG.temp_mail_admin_password = app_config_value("TEMP_MAIL_ADMIN_PASSWORD", "")
    CONFIG.cpa_base_url = app_config_value("CPA_BASE_URL", "").rstrip("/")
    CONFIG.cpa_management_key = app_config_value("CPA_MANAGEMENT_KEY", "")
    CONFIG.browser_display = app_config_value("BROWSER_DISPLAY", ":1")
    CONFIG.browser_proxy = app_config_value("BROWSER_PROXY", "")
    CONFIG.uc_signup_proxy = app_config_value("UC_SIGNUP_PROXY", "")
    CONFIG.uc_signup_phone_retries = app_config_value("UC_SIGNUP_PHONE_RETRIES", "0")
    CONFIG.uc_signup_sms_timeout_seconds = app_config_value("UC_SIGNUP_SMS_TIMEOUT_SECONDS", "135")
    CONFIG.uc_signup_sms_poll_interval_seconds = app_config_value("UC_SIGNUP_SMS_POLL_INTERVAL_SECONDS", "10")
    CONFIG.uc_signup_phone_password_page_timeout = app_config_value("UC_SIGNUP_PHONE_PASSWORD_PAGE_TIMEOUT", "25")
    CONFIG.uc_signup_chrome_binary = app_config_value("UC_SIGNUP_CHROME_BINARY", "")
    CONFIG.uc_signup_chrome_version = app_config_value("UC_SIGNUP_CHROME_VERSION", "")
    CONFIG.signup_password = app_config_value("SIGNUP_PASSWORD", "FuckOAI123456!")
    CONFIG.signup_name = app_config_value("SIGNUP_NAME", "Fuck OAI")
    CONFIG.signup_age = app_config_value("SIGNUP_AGE", "18")
    CONFIG.admin_password = os.getenv("ADMIN_PASSWORD", "")

    CLIENT = HeroSmsClient(CONFIG.api_key, CONFIG.api_url, CONFIG.timeout_ms)
    TEMP_MAIL = TempMailClient(CONFIG.temp_mail_api_url, CONFIG.temp_mail_admin_password, CONFIG.timeout_ms)
    CPA = CpaClient(CONFIG.cpa_base_url, CONFIG.cpa_management_key, CONFIG.timeout_ms)


def make_admin_session_token() -> str:
    password = CONFIG.admin_password
    if not password:
        return ""
    return hmac.new(password.encode("utf-8"), b"fuckoai-admin-session", hashlib.sha256).hexdigest()


def load_control_panel_html() -> str:
    try:
        return CONTROL_PANEL_PATH.read_text(encoding="utf-8")
    except OSError:
        return CONTROL_PANEL_HTML


def get_app_settings() -> dict[str, Any]:
    values = load_config_values()
    return {
        "configFile": str(CONFIG_PATH),
        "settings": {key: values.get(key, "") for key in APP_SETTING_FIELDS},
    }


def update_app_settings(payload: dict[str, Any]) -> dict[str, Any]:
    settings = payload.get("settings") if isinstance(payload.get("settings"), dict) else payload
    if not isinstance(settings, dict):
        raise ValueError("settings 必须是对象")

    current = load_config_values()
    next_values = {
        key: str(settings[key]).strip() if key in settings and settings[key] is not None else current.get(key, "")
        for key in APP_SETTING_FIELDS
    }
    write_config_values(next_values)
    reload_runtime_config()
    return get_app_settings()


def parse_positive_int(value: Any, default: int = 1) -> int:
    try:
        parsed = int(str(value).strip())
        return parsed if parsed > 0 else default
    except (TypeError, ValueError):
        return default


def generate_random_local_part(length: int = 10) -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "".join(random.choices(alphabet, k=length))


def normalize_email_lines(value: Any) -> list[str]:
    if isinstance(value, list):
        raw_items = value
    else:
        raw_items = str(value or "").splitlines()
    emails: list[str] = []
    seen: set[str] = set()
    for raw_item in raw_items:
        email = str(raw_item or "").strip()
        if not email:
            continue
        if "@" not in email:
            raise ValueError(f"邮箱格式不正确: {email}")
        if email in seen:
            continue
        seen.add(email)
        emails.append(email)
    return emails


def generate_random_emails(domain: str, total: int) -> list[str]:
    normalized_domain = domain.strip()
    if not normalized_domain:
        raise ValueError("请填写邮箱后缀域名，例如 example.com")
    results: list[str] = []
    seen: set[str] = set()
    while len(results) < total:
        address = f"{generate_random_local_part()}@{normalized_domain}"
        if address in seen:
            continue
        seen.add(address)
        results.append(address)
    return results


def default_email_queue() -> dict[str, Any]:
    return {
        "emails": [],
        "cursor": 0,
        "activeEmail": "",
        "activeStartedAt": "",
        "lastMail": None,
    }


def load_email_queue() -> dict[str, Any]:
    data = load_json_file(EMAIL_QUEUE_PATH)
    queue = default_email_queue()
    emails = normalize_email_lines(data.get("emails", [])) if isinstance(data, dict) else []
    cursor = parse_positive_int(data.get("cursor", 0), default=0) if isinstance(data, dict) else 0
    queue.update(
        {
            "emails": emails,
            "cursor": min(max(cursor, 0), max(len(emails) - 1, 0)),
            "activeEmail": str(data.get("activeEmail") or "").strip() if isinstance(data, dict) else "",
            "activeStartedAt": str(data.get("activeStartedAt") or "").strip() if isinstance(data, dict) else "",
            "lastMail": data.get("lastMail") if isinstance(data, dict) else None,
        }
    )
    return queue


def save_email_queue(queue: dict[str, Any]) -> dict[str, Any]:
    queue = {
        **default_email_queue(),
        **queue,
        "emails": normalize_email_lines(queue.get("emails", [])),
    }
    queue["cursor"] = min(max(int(queue.get("cursor") or 0), 0), max(len(queue["emails"]) - 1, 0))
    save_json_file(EMAIL_QUEUE_PATH, queue)
    return queue


def update_email_queue(payload: dict[str, Any]) -> dict[str, Any]:
    current = load_email_queue()
    emails = normalize_email_lines(payload.get("emailsText", payload.get("emails", [])))
    cursor = parse_positive_int(payload.get("cursor", current.get("cursor", 0)), default=0)
    return save_email_queue({**current, "emails": emails, "cursor": cursor})


def generate_email_queue(payload: dict[str, Any]) -> dict[str, Any]:
    total = parse_positive_int(payload.get("total"), default=1)
    emails = generate_random_emails(str(payload.get("domain") or ""), total)
    return save_email_queue({
        **load_email_queue(),
        "emails": emails,
        "cursor": 0,
        "activeEmail": "",
        "activeStartedAt": "",
        "lastMail": None,
    })


def refresh_active_email_mail(address: str | None = None) -> dict[str, Any]:
    queue = load_email_queue()
    email = str(address or queue.get("activeEmail") or "").strip()
    if not email:
        raise TempMailError("当前没有活动邮箱")
    mail = enrich_temp_mail_item(TEMP_MAIL.latest_mail(email))
    queue = save_email_queue({**queue, "activeEmail": email, "lastMail": mail})
    return {"emailQueue": queue, "address": email, "item": mail}


UC_SIGNUP_LOG_MAX_LINES = 200

class UcSignupManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._process: subprocess.Popen | None = None
        self._state = UcSignupState()
        self._log_buffer: list[dict[str, str]] = []

    def get_state(self) -> dict[str, Any]:
        with self._lock:
            return self._state.to_dict()

    def get_logs(self) -> list[dict[str, str]]:
        with self._lock:
            return list(self._log_buffer)

    def append_log(self, message: str, level: str = "info") -> None:
        entry = {
            "time": datetime.now().astimezone().isoformat(timespec="seconds"),
            "message": str(message),
            "level": level,
        }
        with self._lock:
            self._log_buffer.append(entry)
            while len(self._log_buffer) > UC_SIGNUP_LOG_MAX_LINES:
                self._log_buffer.pop(0)
            self._state.log_lines = list(self._log_buffer)
            self._state.updated_at = now_iso()

    def _update_state(self, **kwargs: Any) -> None:
        with self._lock:
            for key, value in kwargs.items():
                if hasattr(self._state, key):
                    setattr(self._state, key, value)
            self._state.updated_at = now_iso()

    def _add_error(self, message: str) -> None:
        with self._lock:
            self._state.errors.append({"time": now_iso(), "message": str(message)})
            if len(self._state.errors) > 50:
                self._state.errors = self._state.errors[-50:]

    def start(self, emails: list[str], **options: Any) -> dict[str, Any]:
        if not (ROOT / "uc_signup.py").exists():
            return {"error": "未找到 uc_signup.py", "ucSignupState": self.get_state()}

        with self._lock:
            if self._state.running:
                return {"error": "UC 注册任务已在运行中", "ucSignupState": self._state.to_dict()}
            self._stop_event.clear()
            self._process = None
            self._log_buffer = []
            self._state = UcSignupState(
                running=True,
                total=len(emails),
                phase="running",
                started_at=now_iso(),
                updated_at=now_iso(),
            )

        self._thread = threading.Thread(target=self._run, args=(emails, options), daemon=True)
        self._thread.start()
        return {"ucSignupState": self.get_state()}

    def stop(self) -> dict[str, Any]:
        process: subprocess.Popen | None = None
        with self._lock:
            if not self._state.running:
                return {"ucSignupState": self._state.to_dict(), "message": "没有运行中的 UC 注册任务"}
            self._state.stop_requested = True
            self._state.phase = "stopping"
            self._state.updated_at = now_iso()
            process = self._process
        self._stop_event.set()
        self._terminate_process(process)
        return {"ucSignupState": self.get_state()}

    def _run(self, emails: list[str], options: dict[str, Any]) -> None:
        self.append_log(f"UC 最终版注册任务启动: {len(emails)} 个邮箱")
        for index, email in enumerate(emails):
            if self._stop_event.is_set():
                self.append_log("收到停止信号，结束 UC 注册任务", level="warn")
                break

            self._update_state(
                current_index=index,
                current_email=email,
                current_phone="",
                current_step="starting",
                current_pid=None,
            )
            self.append_log("")
            self.append_log(f"===== UC 第 {index + 1}/{len(emails)} 个: {email} =====")
            started_at = now_iso()
            result, error, return_code = self._run_one(email, options)
            finished_at = now_iso()

            with self._lock:
                self._state.completed += 1
                if result == "success":
                    self._state.success += 1
                elif result == "fail":
                    self._state.failed += 1
                self._state.results.append({
                    "email": email,
                    "status": result,
                    "error": error or "",
                    "returnCode": return_code,
                    "startedAt": started_at,
                    "finishedAt": finished_at,
                })
                self._state.results = self._state.results[-500:]
                self._state.current_pid = None
                self._state.updated_at = now_iso()

            if result == "success":
                self.append_log(f"UC 第 {index + 1}/{len(emails)} 个完成: {email}")
                self._advance_queue_cursor(index)
            elif result == "stopped":
                self.append_log(f"UC 第 {index + 1}/{len(emails)} 个已停止: {email}", level="warn")
                break
            else:
                self.append_log(f"UC 第 {index + 1}/{len(emails)} 个失败: {email} ({error or return_code})", level="error")
                if error:
                    self._add_error(error)

        with self._lock:
            self._state.running = False
            self._state.phase = "stopped" if self._stop_event.is_set() else "done"
            self._state.current_step = ""
            self._state.current_email = ""
            self._state.current_phone = ""
            self._state.current_pid = None
            self._state.updated_at = now_iso()
        self.append_log(f"UC 任务结束: 成功 {self._state.success} / 失败 {self._state.failed}")

    def _run_one(self, email: str, options: dict[str, Any]) -> tuple[str, str | None, int | None]:
        command = [
            sys.executable,
            "-u",
            str(ROOT / "uc_signup.py"),
            "--api-base",
            str(options.get("apiBase") or f"http://127.0.0.1:{CONFIG.port}"),
            "--display",
            str(options.get("display") or CONFIG.browser_display),
        ]
        if email:
            command.extend(["--email", email])
        proxy = str(
            options.get("proxy")
            or CONFIG.uc_signup_proxy
            or CONFIG.browser_proxy
        ).strip()
        if proxy:
            command.extend(["--proxy", proxy])
        chrome_binary = str(options.get("chromeBinary") or CONFIG.uc_signup_chrome_binary).strip()
        if chrome_binary:
            command.extend(["--chrome-binary", chrome_binary])
        chrome_version = str(options.get("chromeVersion") or CONFIG.uc_signup_chrome_version).strip()
        if chrome_version:
            command.extend(["--chrome-version", chrome_version])

        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env["UC_SIGNUP_API_BASE"] = str(options.get("apiBase") or f"http://127.0.0.1:{CONFIG.port}")
        env["UC_SIGNUP_DISPLAY"] = str(options.get("display") or CONFIG.browser_display)
        if CONFIG.admin_password:
            env["UC_SIGNUP_ADMIN_PASSWORD"] = CONFIG.admin_password
        if proxy:
            env["UC_SIGNUP_PROXY"] = proxy
        for env_key, value in (
            ("UC_SIGNUP_PHONE_RETRIES", CONFIG.uc_signup_phone_retries),
            ("UC_SIGNUP_SMS_TIMEOUT_SECONDS", CONFIG.uc_signup_sms_timeout_seconds),
            ("UC_SIGNUP_SMS_POLL_INTERVAL_SECONDS", CONFIG.uc_signup_sms_poll_interval_seconds),
            ("UC_SIGNUP_PHONE_PASSWORD_PAGE_TIMEOUT", CONFIG.uc_signup_phone_password_page_timeout),
        ):
            if str(value).strip():
                env[env_key] = str(value).strip()
        for key, env_key in (
            ("password", "SIGNUP_PASSWORD"),
            ("name", "SIGNUP_NAME"),
            ("age", "SIGNUP_AGE"),
        ):
            fallback = {
                "password": CONFIG.signup_password,
                "name": CONFIG.signup_name,
                "age": CONFIG.signup_age,
            }.get(key, "")
            value = str(options.get(key) or fallback).strip()
            if value:
                env[env_key] = value

        try:
            process = subprocess.Popen(
                command,
                cwd=str(ROOT),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                start_new_session=True,
            )
        except Exception as error:
            return ("fail", f"启动 uc_signup.py 失败: {error}", None)

        with self._lock:
            self._process = process
            self._state.current_pid = process.pid
            self._state.updated_at = now_iso()

        if process.stdout:
            for raw_line in process.stdout:
                line = raw_line.rstrip("\r\n")
                if line:
                    self._handle_process_line(line)

        return_code = process.wait()
        with self._lock:
            if self._process is process:
                self._process = None

        if self._stop_event.is_set():
            return ("stopped", "已停止", return_code)
        if return_code == 0:
            return ("success", None, return_code)
        return ("fail", f"uc_signup.py 退出码 {return_code}", return_code)

    def _handle_process_line(self, line: str) -> None:
        level = "error" if any(token in line for token in ("❌", "💀")) else "warn" if "⚠" in line else "info"
        self.append_log(line, level=level)
        phone_match = re.search(r"📱\s*([+\d][^\s]*)", line)
        if phone_match:
            self._update_state(current_phone=phone_match.group(1))
        step = self._infer_step(line)
        if step:
            self._update_state(current_step=step)

    def _infer_step(self, line: str) -> str:
        checks = [
            ("全部完成", "completed"),
            ("回填CPA", "cpa_callback"),
            ("等待回调", "waiting_oauth_callback"),
            ("授权", "authorizing"),
            ("OAuth", "oauth"),
            ("邮箱验证", "filling_email_code"),
            ("邮箱码", "filling_email_code"),
            ("绑定邮箱", "filling_email"),
            ("注册完成", "oauth"),
            ("姓名年龄", "filling_account_details"),
            ("填密码", "filling_password"),
            ("短信验证", "filling_sms_code"),
            ("SMS:", "filling_sms_code"),
            ("SMS ", "waiting_sms"),
            ("填手机号", "filling_phone"),
            ("展开手机表单", "filling_phone"),
            ("Cookie", "accepting_cookie"),
            ("注册:", "opening_signup"),
            ("📱", "buying_phone"),
        ]
        for needle, step in checks:
            if needle in line:
                return step
        return ""

    def _advance_queue_cursor(self, index: int) -> None:
        try:
            queue = load_email_queue()
            emails = queue.get("emails") or []
            if emails:
                save_email_queue({**queue, "cursor": min(index + 1, len(emails) - 1)})
        except Exception:
            pass

    def _terminate_process(self, process: subprocess.Popen | None) -> None:
        if not process or process.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        except Exception:
            try:
                process.terminate()
            except Exception:
                return
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            except Exception:
                process.kill()
            process.wait(timeout=5)


UC_SIGNUP_MANAGER = UcSignupManager()


CONTROL_PANEL_HTML = """<!doctype html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>fuckoai Linux</title></head>
<body><p>control_panel.html is missing. Restore it and reload /ui.</p></body>
</html>
"""


def get_purchase_defaults() -> dict[str, Any]:
    return {
        "serviceName": DEFAULT_SERVICE_NAME,
        "serviceCode": DEFAULT_SERVICE_CODE,
        "countryName": "",
        "countryCode": "",
        "operator": "any",
        "maxPrice": "",
        "exactPrice": "",
        "fixedPrice": "true",
    }


def get_purchase_config() -> dict[str, Any]:
    file_config = load_json_file(CONFIG.purchase_config_file)
    defaults = get_purchase_defaults()
    settings = get_purchase_settings(file_config=file_config, env_defaults=defaults)
    groups = get_enabled_purchase_groups(settings)
    if groups:
        return dict(groups[0])
    fallback = normalize_purchase_group(defaults, defaults)
    return fallback


def get_purchase_group_start_index(group_count: int) -> int:
    if group_count <= 0:
        return 0
    with PURCHASE_GROUP_CURSOR_LOCK:
        return PURCHASE_GROUP_NEXT_INDEX % group_count


def advance_purchase_group_cursor(group_count: int, next_index: int) -> None:
    if group_count <= 0:
        return
    with PURCHASE_GROUP_CURSOR_LOCK:
        global PURCHASE_GROUP_NEXT_INDEX
        PURCHASE_GROUP_NEXT_INDEX = next_index % group_count


def advance_purchase_group_cursor_after_group(group_index: Any) -> None:
    try:
        current_group_index = int(group_index)
    except (TypeError, ValueError):
        return
    groups = get_enabled_purchase_groups(get_purchase_settings())
    if not groups:
        return
    advance_purchase_group_cursor(len(groups), current_group_index)


def is_early_cancel_denied_error(error: Exception | str) -> bool:
    text = str(error or "")
    return "EARLY_CANCEL_DENIED" in text and "minActivationTime" in text


def get_purchase_settings(
    *, file_config: dict[str, Any] | None = None, env_defaults: dict[str, Any] | None = None
) -> dict[str, Any]:
    file_config = file_config if isinstance(file_config, dict) else load_json_file(CONFIG.purchase_config_file)
    env_defaults = env_defaults or get_purchase_defaults()
    root_defaults = dict(env_defaults)
    for key in PURCHASE_FILTER_KEYS:
        value = file_config.get(key)
        if value not in (None, ""):
            root_defaults[key] = value

    raw_groups = file_config.get("purchaseGroups")
    if file_config and not isinstance(raw_groups, list):
        raise HeroSmsError("购买配置必须使用新格式，并提供 purchaseGroups 数组")
    groups: list[dict[str, Any]] = []
    if isinstance(raw_groups, list):
        for index, item in enumerate(raw_groups, start=1):
            if not isinstance(item, dict):
                continue
            groups.append(normalize_purchase_group(item, root_defaults, index=index))

    return {
        "serviceName": str(root_defaults.get("serviceName") or DEFAULT_SERVICE_NAME),
        "serviceCode": str(root_defaults.get("serviceCode") or DEFAULT_SERVICE_CODE),
        "purchaseGroups": groups,
    }


def normalize_purchase_group(source: dict[str, Any] | None, defaults: dict[str, Any], *, index: int = 1) -> dict[str, Any]:
    source = source if isinstance(source, dict) else {}
    merged = {**defaults, **source}
    group = {
        "label": str(source.get("label") or "").strip(),
        "enabled": parse_bool_flag(source.get("enabled", True), default=True),
        "serviceName": str(merged.get("serviceName") or DEFAULT_SERVICE_NAME).strip(),
        "serviceCode": str(merged.get("serviceCode") or DEFAULT_SERVICE_CODE).strip(),
        "countryName": str(merged.get("countryName") or "").strip(),
        "countryCode": str(merged.get("countryCode") or "").strip(),
        "operator": str(merged.get("operator") or "any").strip() or "any",
        "maxPrice": str(merged.get("maxPrice") or "").strip(),
        "exactPrice": str(merged.get("exactPrice") or "").strip(),
        "fixedPrice": normalize_fixed_price_value(merged.get("fixedPrice")),
    }
    if not group["label"]:
        group["label"] = build_purchase_group_label(group, index=index)
    return group


def build_purchase_group_label(group: dict[str, Any], *, index: int = 1) -> str:
    country = str(group.get("countryCode") or group.get("countryName") or "").strip()
    operator = str(group.get("operator") or "any").strip() or "any"
    if str(group.get("fixedPrice") or "").lower() == "true" and str(group.get("exactPrice") or "").strip():
        price = f"exact {str(group.get('exactPrice')).strip()}"
    elif str(group.get("maxPrice") or "").strip():
        price = f"max {str(group.get('maxPrice')).strip()}"
    else:
        price = "market"
    parts = [part for part in (country, operator, price) if part]
    return " / ".join(parts) if parts else f"Group {index}"


def is_purchase_group_configured(group: dict[str, Any]) -> bool:
    return bool(str(group.get("countryCode") or "").strip() or str(group.get("countryName") or "").strip())


def get_enabled_purchase_groups(settings: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    settings = settings or get_purchase_settings()
    groups = settings.get("purchaseGroups") if isinstance(settings, dict) else []
    return [
        dict(group)
        for group in groups
        if isinstance(group, dict) and parse_bool_flag(group.get("enabled", True), default=True) and is_purchase_group_configured(group)
    ]


def serialize_purchase_settings(settings: dict[str, Any]) -> dict[str, Any]:
    groups = []
    for index, group in enumerate(settings.get("purchaseGroups") or [], start=1):
        if not isinstance(group, dict):
            continue
        normalized = normalize_purchase_group(group, settings, index=index)
        groups.append(
            {
                "label": normalized["label"],
                "enabled": parse_bool_flag(normalized.get("enabled", True), default=True),
                "countryName": normalized["countryName"],
                "countryCode": normalized["countryCode"],
                "operator": normalized["operator"],
                "fixedPrice": normalized["fixedPrice"] == "true",
                "exactPrice": normalized["exactPrice"],
                "maxPrice": normalized["maxPrice"],
            }
        )
    return {
        "serviceName": str(settings.get("serviceName") or DEFAULT_SERVICE_NAME).strip() or DEFAULT_SERVICE_NAME,
        "serviceCode": str(settings.get("serviceCode") or DEFAULT_SERVICE_CODE).strip() or DEFAULT_SERVICE_CODE,
        "purchaseGroups": groups,
    }


def update_purchase_settings(payload: dict[str, Any]) -> dict[str, Any]:
    defaults = get_purchase_defaults()
    settings = get_purchase_settings(file_config=payload, env_defaults=defaults)
    serialized = serialize_purchase_settings(settings)
    save_json_file(CONFIG.purchase_config_file, serialized)
    return get_purchase_settings(file_config=serialized, env_defaults=defaults)


def get_display_name(source: dict[str, Any], *, name_key: str, code_key: str, default_name: str) -> str:
    explicit_name = str(source.get(name_key) or "").strip()
    if explicit_name:
        return explicit_name
    if str(source.get(code_key) or "").strip():
        return ""
    return default_name


def get_filters(source: dict[str, Any] | None = None, defaults: dict[str, Any] | None = None) -> dict[str, str]:
    source = source or {}
    base = {**(defaults or get_purchase_config()), **source}
    return {
        "serviceName": get_display_name(
            base, name_key="serviceName", code_key="serviceCode", default_name=DEFAULT_SERVICE_NAME
        ),
        "serviceCode": str(base.get("serviceCode") or ""),
        "countryName": get_display_name(
            base, name_key="countryName", code_key="countryCode", default_name=""
        ),
        "countryCode": str(base.get("countryCode") or ""),
        "operator": str(base.get("operator") or "any"),
        "maxPrice": str(base.get("maxPrice") or ""),
        "exactPrice": str(base.get("exactPrice") or ""),
        "fixedPrice": normalize_fixed_price_value(base.get("fixedPrice")),
    }


def get_country_search_fields(item: dict[str, Any]) -> list[str]:
    fields = [str(item.get("name") or ""), str(item.get("localName") or ""), str(item.get("code") or "")]
    if isinstance(item.get("searchTerms"), list):
        fields.extend(str(term) for term in item.get("searchTerms") if term)
    fields.extend(COUNTRY_ALIASES_BY_NAME.get(str(item.get("name") or "").lower(), []))
    return fields


def search_countries_by_name(name: str, limit: int = 8) -> list[dict[str, Any]]:
    query = normalize_text(name)
    if not query:
        return []
    ranked: list[tuple[int, str, dict[str, Any]]] = []
    for item in CLIENT.get_countries():
        fields = get_country_search_fields(item)
        score: int | None = None
        for field in fields:
            normalized = normalize_text(field)
            if not normalized:
                continue
            if normalized == query:
                score = 0 if score is None else min(score, 0)
            elif normalized.startswith(query) or query.startswith(normalized):
                score = 1 if score is None else min(score, 1)
            elif query in normalized or normalized in query:
                score = 2 if score is None else min(score, 2)
        if score is not None:
            label = str(item.get("name") or item.get("localName") or item.get("code") or "")
            ranked.append((score, label.lower(), item))
    ranked.sort(key=lambda entry: (entry[0], entry[1]))
    return [item for _, _, item in ranked[:limit]]


def load_catalog_cache() -> dict[str, Any]:
    return load_json_file(CATALOG_CACHE_PATH)


def save_catalog_cache(cache: dict[str, Any]) -> dict[str, Any]:
    save_json_file(CATALOG_CACHE_PATH, cache)
    return cache


def get_cached_countries(*, refresh: bool = False) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    cache = load_catalog_cache()
    countries = cache.get("countries")
    if refresh or not isinstance(countries, list):
        countries = CLIENT.get_countries(force=True)
        cache["countries"] = countries
        cache["countriesCachedAt"] = now_iso()
        save_catalog_cache(cache)
    return countries, cache


def search_country_items(items: list[dict[str, Any]], query_text: str, limit: int = 20) -> list[dict[str, Any]]:
    query = normalize_text(query_text)
    ranked: list[tuple[int, str, dict[str, Any]]] = []
    for item in items:
        fields = get_country_search_fields(item)
        if not query:
            label = str(item.get("name") or item.get("localName") or item.get("code") or "")
            ranked.append((3, label.lower(), item))
            continue
        score: int | None = None
        for field in fields:
            normalized = normalize_text(field)
            if not normalized:
                continue
            if normalized == query:
                score = 0 if score is None else min(score, 0)
            elif normalized.startswith(query) or query.startswith(normalized):
                score = 1 if score is None else min(score, 1)
            elif query in normalized or normalized in query:
                score = 2 if score is None else min(score, 2)
        if score is not None:
            label = str(item.get("name") or item.get("localName") or item.get("code") or "")
            ranked.append((score, label.lower(), item))
    ranked.sort(key=lambda entry: (entry[0], entry[1]))
    return [item for _, _, item in ranked[:limit]]


def get_cached_operators(service_code: str, country_code: str, *, refresh: bool = False) -> tuple[list[str], dict[str, Any]]:
    cache = load_catalog_cache()
    operators_cache = cache.get("operators")
    if not isinstance(operators_cache, dict):
        operators_cache = {}
        cache["operators"] = operators_cache
    cache_key = f"{service_code}:{country_code}"
    cached_entry = operators_cache.get(cache_key)
    operators = cached_entry.get("items") if isinstance(cached_entry, dict) else None
    if refresh or not isinstance(operators, list):
        operators = CLIENT.get_operators(service_code, country_code, force=True)
        operators_cache[cache_key] = {"items": operators, "cachedAt": now_iso()}
        save_catalog_cache(cache)
    return operators, cache


def normalize_record(record: dict[str, Any]) -> dict[str, Any]:
    item = dict(record)
    item["isClosed"] = item.get("status") in {"finished", "canceled"}
    return item


def find_by_code(items: list[dict[str, Any]], code: str) -> dict[str, Any] | None:
    target = str(code or "").strip()
    if not target:
        return None
    for item in items:
        if str(item.get("code")) == target:
            return item
    return None


def resolve_selections(filters: dict[str, str]) -> dict[str, Any]:
    service_code = str(filters.get("serviceCode") or "").strip()
    country_code = str(filters.get("countryCode") or "").strip()
    if service_code and country_code:
        return {
            "service": {"code": service_code, "name": filters.get("serviceName") or service_code},
            "services": [],
            "country": {
                "code": country_code,
                "name": filters.get("countryName") or country_code,
                "localName": filters.get("countryName") or country_code,
            },
            "countries": [],
        }

    services = CLIENT.get_services()
    countries = CLIENT.get_countries()
    service = find_by_code(services, service_code) or CLIENT._pick_by_name(
        services, filters["serviceName"], CONFIG.default_service_aliases, ("name", "code")
    )
    country = find_by_code(countries, country_code) or CLIENT._pick_by_name(
        countries, filters["countryName"], CONFIG.default_country_aliases, ("name", "localName", "code")
    )
    if not service:
        raise HeroSmsError(f"找不到服务: {filters.get('serviceCode') or filters['serviceName']}")
    if not country:
        raise HeroSmsError(f"找不到国家/地区: {filters.get('countryCode') or filters['countryName']}")
    return {"service": service, "services": services, "country": country, "countries": countries}


def build_service_lookup() -> dict[str, dict[str, Any]]:
    return {str(item.get("code")): item for item in CLIENT.get_services()}


def build_country_lookup() -> dict[str, dict[str, Any]]:
    return {str(item.get("code")): item for item in CLIENT.get_countries()}


def import_active_activations() -> list[dict[str, Any]]:
    service_lookup = build_service_lookup()
    country_lookup = build_country_lookup()
    imported = []
    for item in CLIENT.get_active_activations():
        activation_id = str(item.get("activationId") or item.get("id") or "")
        if not activation_id:
            continue
        service_code = str(item.get("serviceCode") or item.get("service") or "")
        country_code = str(item.get("countryCode") or item.get("country") or "")
        status_code = str(item.get("activationStatus") or item.get("status") or "4")
        local_status, label, upstream_status = ACTIVE_STATUS_MAP.get(
            status_code, ("number_issued", "号码已下发", "STATUS_WAIT_GET")
        )
        service = service_lookup.get(service_code, {"code": service_code, "name": service_code or "--"})
        country = country_lookup.get(country_code, {"code": country_code, "name": country_code or "--", "localName": ""})

        record = STORE.upsert(
            {
                "id": activation_id,
                "phoneNumber": str(item.get("phoneNumber") or item.get("phone") or ""),
                "activationCost": item.get("activationCost") or item.get("cost"),
                "countryCode": country_code,
                "countryName": country.get("name") or country.get("localName") or country_code,
                "serviceCode": service_code,
                "serviceName": service.get("name") or service_code,
                "operator": str(item.get("operator") or "any"),
                "status": local_status,
                "statusLabel": label,
                "upstreamStatus": upstream_status,
                "lastCode": item.get("smsCode") or None,
                "codes": [str(item.get("smsCode"))] if item.get("smsCode") else [],
                "purchasedAt": item.get("activationTime") or item.get("createDate") or now_iso(),
                "rawImport": item,
            }
        )
        imported.append(normalize_record(record))
    return imported


def fetch_upstream_activations() -> list[dict[str, Any]]:
    service_lookup = build_service_lookup()
    country_lookup = build_country_lookup()
    items = []
    for item in CLIENT.get_active_activations():
        activation_id = str(item.get("activationId") or item.get("id") or "")
        if not activation_id:
            continue
        service_code = str(item.get("serviceCode") or item.get("service") or "")
        country_code = str(item.get("countryCode") or item.get("country") or "")
        status_code = str(item.get("activationStatus") or item.get("status") or "4")
        local_status, label, upstream_status = ACTIVE_STATUS_MAP.get(
            status_code, ("number_issued", "号码已下发", "STATUS_WAIT_GET")
        )
        service = service_lookup.get(service_code, {"code": service_code, "name": service_code or "--"})
        country = country_lookup.get(country_code, {"code": country_code, "name": country_code or "--", "localName": ""})
        sms_code = item.get("smsCode") or item.get("code")
        record = normalize_record(
            {
                "id": activation_id,
                "phoneNumber": str(item.get("phoneNumber") or item.get("phone") or ""),
                "activationCost": item.get("activationCost") or item.get("cost"),
                "countryCode": country_code,
                "countryName": country.get("name") or country.get("localName") or country_code,
                "serviceCode": service_code,
                "serviceName": service.get("name") or service_code,
                "operator": str(item.get("operator") or "any"),
                "status": local_status,
                "statusLabel": label,
                "upstreamStatus": upstream_status,
                "lastCode": str(sms_code) if sms_code else None,
                "codes": [str(sms_code)] if sms_code else [],
                "purchasedAt": item.get("activationTime") or item.get("createDate") or now_iso(),
                "updatedAt": now_iso(),
                "rawUpstream": item,
            }
        )
        items.append(record)
    items.sort(key=lambda record: record.get("purchasedAt") or "", reverse=True)
    return items


def filter_activations(
    items: list[dict[str, Any]],
    *,
    service_code: str = "",
    country_code: str = "",
    operator: str = "",
    price: str = "",
) -> list[dict[str, Any]]:
    result = items
    if service_code:
        result = [item for item in result if str(item.get("serviceCode")) == str(service_code)]
    if country_code:
        result = [item for item in result if str(item.get("countryCode")) == str(country_code)]
    if operator:
        known_operator_items = [item for item in result if str(item.get("operator", "")).lower() not in {"", "any"}]
        if known_operator_items:
            result = [item for item in result if str(item.get("operator", "")).lower() == str(operator).lower()]
    if price:
        try:
            target = round(float(price), 4)
            result = [
                item
                for item in result
                if item.get("activationCost") is not None and round(float(item.get("activationCost")), 4) == target
            ]
        except ValueError:
            pass
    return result


def get_current_filtered_activations(filters: dict[str, str] | None = None) -> list[dict[str, Any]]:
    items = fetch_upstream_activations()
    if filters:
        price = filters.get("exactPrice") or filters.get("price") or ""
        return filter_activations(
            items,
            service_code=filters.get("serviceCode", ""),
            country_code=filters.get("countryCode", ""),
            operator=filters.get("operator", ""),
            price=price,
        )

    settings = get_purchase_settings()
    matched: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for group in get_enabled_purchase_groups(settings):
        group_filters = get_filters(group, defaults=group)
        price = group_filters.get("exactPrice") or group_filters.get("price") or ""
        for item in filter_activations(
            items,
            service_code=group_filters.get("serviceCode", ""),
            country_code=group_filters.get("countryCode", ""),
            operator=group_filters.get("operator", ""),
            price=price,
        ):
            item_id = str(item.get("id") or "")
            if item_id and item_id in seen_ids:
                continue
            if item_id:
                seen_ids.add(item_id)
            matched.append(item)
    matched.sort(key=lambda record: record.get("purchasedAt") or "", reverse=True)
    return matched


def build_purchase_attempts(source: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    source = source if isinstance(source, dict) else {}
    if any(key in source for key in PURCHASE_FILTER_KEYS):
        filters = get_filters(source)
        return [
            {
                "label": str(source.get("label") or build_purchase_group_label(filters, index=1)),
                "filters": filters,
            }
        ]

    settings = get_purchase_settings()
    groups = get_enabled_purchase_groups(settings)
    if not groups:
        raise HeroSmsError("未配置可用的 purchaseGroups")
    attempts = []
    group_count = len(groups)
    start_index = get_purchase_group_start_index(group_count)
    ordered_groups = groups[start_index:] + groups[:start_index]
    for offset, group in enumerate(ordered_groups, start=1):
        group_index = (start_index + offset - 1) % group_count
        attempts.append(
            {
                "label": str(group.get("label") or build_purchase_group_label(group, index=group_index + 1)),
                "filters": get_filters(group, defaults=group),
                "groupIndex": group_index + 1,
            }
        )
    return attempts


def execute_purchase(filters: dict[str, str]) -> tuple[dict[str, Any], dict[str, Any]]:
    resolved = resolve_selections(filters)
    if filters["fixedPrice"] == "true" and filters["exactPrice"]:
        purchase = CLIENT.buy_activation_fixed_price(
            service_code=resolved["service"]["code"],
            country_code=resolved["country"]["code"],
            operator=filters["operator"],
            exact_price=filters["exactPrice"],
        )
    else:
        purchase = CLIENT.buy_activation(
            service_code=resolved["service"]["code"],
            country_code=resolved["country"]["code"],
            operator=filters["operator"],
            max_price=filters["maxPrice"],
        )
    return purchase, resolved


def build_purchase_item(
    purchase: dict[str, Any],
    resolved: dict[str, Any],
    *,
    include_raw: bool = False,
    purchase_group_index: int | None = None,
) -> dict[str, Any]:
    item = {
        "id": purchase["id"],
        "phoneNumber": purchase["phoneNumber"],
        "activationCost": purchase["activationCost"],
        "countryCode": resolved["country"]["code"],
        "countryName": resolved["country"]["name"] or resolved["country"]["localName"],
        "serviceCode": resolved["service"]["code"],
        "serviceName": resolved["service"]["name"],
        "operator": purchase["operator"],
        "canGetAnotherSms": purchase["canGetAnotherSms"],
        "status": "number_issued",
        "statusLabel": "号码已下发",
        "upstreamStatus": "STATUS_WAIT_GET",
        "purchasedAt": now_iso(),
        "updatedAt": now_iso(),
        "codes": [],
    }
    if purchase_group_index is not None:
        item["purchaseGroupIndex"] = int(purchase_group_index)
    if include_raw:
        item["rawPurchase"] = purchase["raw"]
    return normalize_record(item)


def purchase_with_fallback(source: dict[str, Any] | None = None) -> dict[str, Any]:
    attempts_summary = []
    last_error: HeroSmsError | None = None
    for index, attempt in enumerate(build_purchase_attempts(source), start=1):
        filters = attempt["filters"]
        label = str(attempt.get("label") or build_purchase_group_label(filters, index=index))
        try:
            purchase, resolved = execute_purchase(filters)
            return {
                "filters": filters,
                "item": build_purchase_item(
                    purchase,
                    resolved,
                    purchase_group_index=int(attempt["groupIndex"]) if attempt.get("groupIndex") is not None else None,
                ),
                "rawPurchase": purchase["raw"],
                "attempts": attempts_summary
                + [
                    {
                        "index": index,
                        "label": label,
                        "filters": filters,
                        "success": True,
                        "groupIndex": attempt.get("groupIndex"),
                    }
                ],
            }
        except HeroSmsError as error:
            last_error = error
            attempts_summary.append(
                {
                    "index": index,
                    "label": label,
                    "filters": filters,
                    "success": False,
                    "groupIndex": attempt.get("groupIndex"),
                    "error": str(error),
                }
            )
            continue

    detail = "；".join(f"{item['index']}. {item['label']}: {item['error']}" for item in attempts_summary) or "没有可执行的购买组"
    raise HeroSmsError(f"所有购买配置都失败: {detail}") from last_error


def find_activation_by_phone(phone_number: str) -> dict[str, Any] | None:
    normalized = str(phone_number or "").strip()
    if not normalized:
        return None
    items = fetch_upstream_activations()
    return next((item for item in items if str(item.get("phoneNumber")) == normalized), None)


def sync_record_status(record: dict[str, Any]) -> dict[str, Any]:
    status = CLIENT.get_status(str(record["id"]))
    next_record = STORE.upsert(
        {
            **record,
            "status": status["localStatus"],
            "statusLabel": status["label"],
            "upstreamStatus": status["upstreamStatus"],
            "rawStatus": status["raw"],
        }
    )
    if status.get("code"):
        next_record = STORE.append_code(str(record["id"]), status["code"]) or next_record
    return {"record": normalize_record(next_record), "status": status}


class AppHandler(BaseHTTPRequestHandler):
    server_version = "fuckoai/1.0"

    def is_authenticated(self) -> bool:
        if not CONFIG.admin_password:
            return True
        header_password = self.headers.get("X-Admin-Password", "")
        if header_password and hmac.compare_digest(header_password, CONFIG.admin_password):
            return True
        cookie_header = self.headers.get("Cookie", "")
        cookies: dict[str, str] = {}
        for part in cookie_header.split(";"):
            if "=" not in part:
                continue
            key, value = part.split("=", 1)
            cookies[key.strip()] = value.strip()
        return hmac.compare_digest(cookies.get("fuckoai_admin", ""), make_admin_session_token())

    def require_authenticated(self) -> bool:
        if self.is_authenticated():
            return True
        self.send_json(401, {"error": "需要管理员密码", "authenticated": False})
        return False

    def handle_auth_api(self, method: str, path: str) -> bool:
        if method == "GET" and path == "/api/auth/status":
            self.send_json(
                200,
                {
                    "authRequired": bool(CONFIG.admin_password),
                    "authenticated": self.is_authenticated(),
                },
            )
            return True
        if method == "POST" and path == "/api/auth/login":
            body = self.read_json_body()
            password = str(body.get("password") or "")
            if not CONFIG.admin_password or hmac.compare_digest(password, CONFIG.admin_password):
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                if CONFIG.admin_password:
                    self.send_header(
                        "Set-Cookie",
                        f"fuckoai_admin={make_admin_session_token()}; Path=/; HttpOnly; SameSite=Lax",
                    )
                self.end_headers()
                self.wfile.write(json.dumps({"authenticated": True}, ensure_ascii=False).encode("utf-8"))
                return True
            self.send_json(401, {"error": "管理员密码错误", "authenticated": False})
            return True
        if method == "POST" and path == "/api/auth/logout":
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Set-Cookie", "fuckoai_admin=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax")
            self.end_headers()
            self.wfile.write(json.dumps({"authenticated": False}, ensure_ascii=False).encode("utf-8"))
            return True
        return False

    def end_headers(self) -> None:
        if CONFIG.enable_cors:
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
        super().end_headers()

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.end_headers()

    def do_GET(self) -> None:
        self.handle_request("GET")

    def do_POST(self) -> None:
        self.handle_request("POST")

    def do_DELETE(self) -> None:
        self.handle_request("DELETE")

    def handle_request(self, method: str) -> None:
        parsed = urlparse(self.path)
        try:
            if method == "GET" and parsed.path in {"/ui", "/panel"}:
                self.send_html(200, load_control_panel_html())
                return
            if parsed.path.startswith("/api"):
                self.handle_api(method, parsed)
                return
            if parsed.path == "/":
                self.send_json(
                    200,
                    {
                        "name": APP_NAME,
                        "apiBase": "/api",
                        "health": "/api/health",
                        "purchase": "/api/purchase",
                    },
                )
                return
            self.send_json(404, {"error": "接口不存在"})
        except HeroSmsError as error:
            self.send_json(500, {"error": str(error)})
        except Exception as error:
            self.send_json(500, {"error": str(error)})

    def handle_api(self, method: str, parsed: Any) -> None:
        path = parsed.path
        query = {key: values[-1] for key, values in parse_qs(parsed.query).items()}

        if self.handle_auth_api(method, path):
            return
        if not self.require_authenticated():
            return

        if method == "GET" and path == "/api/health":
            self.send_json(
                200,
                {
                    "ok": True,
                    "configured": bool(CONFIG.api_key),
                    "tempMailConfigured": bool(CONFIG.temp_mail_api_url and CONFIG.temp_mail_admin_password),
                    "cpaConfigured": bool(CONFIG.cpa_base_url and CONFIG.cpa_management_key),
                    "apiUrl": CONFIG.api_url,
                    "purchaseConfigFile": str(CONFIG.purchase_config_file),
                    "purchaseConfig": get_purchase_config(),
                    "purchaseSettings": get_purchase_settings(),
                },
            )
            return

        if method == "GET" and path == "/api/config":
            self.send_json(
                200,
                {
                    "appSettings": get_app_settings(),
                    "purchaseConfig": get_purchase_config(),
                    "purchaseSettings": get_purchase_settings(),
                },
            )
            return

        if method == "GET" and path == "/api/app-settings":
            self.send_json(200, get_app_settings())
            return

        if method == "POST" and path == "/api/app-settings":
            body = self.read_json_body()
            settings = update_app_settings(body)
            self.send_json(200, settings)
            return

        if method == "GET" and path == "/api/email-queue":
            self.send_json(200, {"emailQueue": load_email_queue()})
            return

        if method == "POST" and path == "/api/email-queue":
            body = self.read_json_body()
            self.send_json(200, {"emailQueue": update_email_queue(body)})
            return

        if method == "POST" and path == "/api/email-queue/generate":
            body = self.read_json_body()
            self.send_json(200, {"emailQueue": generate_email_queue(body)})
            return

        if method == "GET" and path == "/api/email-queue/mail/latest":
            self.send_json(200, refresh_active_email_mail(query.get("address")))
            return

        if method == "GET" and path == "/api/purchase-settings":
            self.send_json(200, {"purchaseSettings": get_purchase_settings()})
            return

        if method == "POST" and path == "/api/purchase-settings":
            body = self.read_json_body()
            settings = update_purchase_settings(body)
            self.send_json(200, {"purchaseSettings": settings, "purchaseConfig": get_purchase_config()})
            return

        if method == "GET" and path == "/api/purchase-catalog/countries":
            refresh = parse_bool_flag(query.get("refresh"), default=False)
            countries, cache = get_cached_countries(refresh=refresh)
            limit = parse_positive_int(query.get("limit"), default=len(countries))
            matches = search_country_items(countries, query.get("query", ""), limit=min(max(limit, 1), len(countries)))
            self.send_json(
                200,
                {
                    "items": matches,
                    "total": len(countries),
                    "cachedAt": cache.get("countriesCachedAt", ""),
                    "refreshed": refresh,
                },
            )
            return

        if method == "POST" and path == "/api/purchase-catalog/countries/refresh":
            countries, cache = get_cached_countries(refresh=True)
            self.send_json(
                200,
                {
                    "items": search_country_items(countries, "", limit=len(countries)),
                    "total": len(countries),
                    "cachedAt": cache.get("countriesCachedAt", ""),
                    "refreshed": True,
                },
            )
            return

        if method == "GET" and path == "/api/purchase-catalog/operators":
            service_code = str(query.get("serviceCode") or get_purchase_settings().get("serviceCode") or DEFAULT_SERVICE_CODE).strip()
            country_code = str(query.get("countryCode") or "").strip()
            if not country_code:
                self.send_json(400, {"error": "缺少 countryCode"})
                return
            refresh = parse_bool_flag(query.get("refresh"), default=False)
            operators, cache = get_cached_operators(service_code, country_code, refresh=refresh)
            operator_entry = (cache.get("operators") or {}).get(f"{service_code}:{country_code}") or {}
            self.send_json(
                200,
                {
                    "items": operators,
                    "serviceCode": service_code,
                    "countryCode": country_code,
                    "cachedAt": operator_entry.get("cachedAt", ""),
                    "refreshed": refresh,
                },
            )
            return

        if method == "GET" and path == "/api/temp-mail/settings":
            self.send_json(200, {"settings": TEMP_MAIL.get_settings()})
            return

        if method == "GET" and path == "/api/codex-oauth/url":
            if str(query.get("is_webui", "")).lower() == "true":
                self.send_json(400, {"error": "当前仅支持远程回调模式，不支持同机自动回调模式"})
                return
            result = CPA.get_codex_auth_url()
            self.send_json(200, result if isinstance(result, dict) else {"result": result})
            return

        if method == "POST" and path == "/api/codex-oauth/callback":
            body = self.read_json_body()
            result = CPA.oauth_callback(
                provider=str(body.get("provider") or "codex"),
                redirect_url=str(body.get("redirect_url") or ""),
                code=str(body.get("code") or ""),
                state=str(body.get("state") or ""),
            )
            self.send_json(200, result if isinstance(result, dict) else {"result": result})
            return

        if method == "GET" and path == "/api/codex-oauth/status":
            state = str(query.get("state", ""))
            result = CPA.get_auth_status(state)
            self.send_json(200, result if isinstance(result, dict) else {"result": result})
            return

        if method == "GET" and path == "/api/codex-oauth/files":
            result = CPA.get_auth_files()
            if isinstance(result, dict):
                self.send_json(200, result)
            elif isinstance(result, list):
                self.send_json(200, {"files": result})
            else:
                self.send_json(200, {"result": result})
            return

        if method == "POST" and path == "/api/temp-mail/address":
            body = self.read_json_body()
            settings = TEMP_MAIL.get_settings()
            domain = str(body.get("domain") or (settings.get("defaultDomains") or settings.get("domains") or [""])[0])
            name = str(body.get("name") or f"mail{int(datetime.now().timestamp())}")
            enable_prefix = bool(body.get("enablePrefix", True))
            result = TEMP_MAIL.create_address(name=name, domain=domain, enable_prefix=enable_prefix)
            self.send_json(201, {"item": result})
            return

        if method == "GET" and path.startswith("/api/temp-mail/address/") and path.endswith("/mails/latest"):
            address = unquote(path.split("/")[-3])
            mail = enrich_temp_mail_item(TEMP_MAIL.latest_mail(address))
            self.send_json(200, {"address": address, "item": mail})
            return

        if method == "GET" and path.startswith("/api/temp-mail/address/") and path.endswith("/mails"):
            address = unquote(path.split("/")[-2])
            limit = int(query.get("limit", "20"))
            offset = int(query.get("offset", "0"))
            mails = TEMP_MAIL.list_mails(address, limit=limit, offset=offset)
            if isinstance(mails.get("results"), list):
                mails["results"] = [enrich_temp_mail_item(item) for item in mails["results"]]
            self.send_json(200, {"address": address, **mails})
            return

        if method == "DELETE" and path.startswith("/api/temp-mail/address/"):
            parts = path.strip("/").split("/")
            if len(parts) == 4 and parts[0] == "api" and parts[1] == "temp-mail" and parts[2] == "address":
                address = unquote(parts[3])
                result = TEMP_MAIL.delete_address(address)
                self.send_json(200, {"address": address, **result})
                return

        if method == "GET" and path == "/api/options":
            defaults = get_purchase_config()
            self.send_json(
                200,
                {
                    "services": CLIENT.get_services(),
                    "countries": CLIENT.get_countries(),
                    "defaults": {
                        "serviceName": defaults.get("serviceName", ""),
                        "serviceCode": defaults.get("serviceCode", ""),
                        "countryName": defaults.get("countryName", ""),
                        "countryCode": defaults.get("countryCode", ""),
                        "operator": defaults.get("operator", CONFIG.default_operator),
                    },
                },
            )
            return

        if method == "GET" and path == "/api/country-lookup":
            name = str(query.get("name") or "").strip()
            if not name:
                self.send_json(400, {"error": "缺少国家名称 name"})
                return
            service_code = str(query.get("serviceCode") or get_purchase_settings().get("serviceCode") or DEFAULT_SERVICE_CODE).strip()
            matches = search_countries_by_name(name)
            if not matches:
                self.send_json(404, {"error": f"找不到国家/地区: {name}"})
                return
            country = matches[0]
            operators = CLIENT.get_operators(service_code, str(country.get("code") or ""))
            self.send_json(
                200,
                {
                    "query": name,
                    "serviceCode": service_code,
                    "country": country,
                    "operators": operators,
                    "matches": matches,
                },
            )
            return

        if method == "GET" and path == "/api/balance":
            try:
                balance = CLIENT.get_balance_cached()
            except HeroSmsError:
                balance = None
            self.send_json(200, {"balance": balance})
            return

        if method == "GET" and path == "/api/pricing":
            filters = get_filters(query)
            resolved = resolve_selections(filters)
            pricing = CLIENT.get_pricing(resolved["service"]["code"], resolved["country"]["code"])
            self.send_json(
                200,
                {
                    "filters": filters,
                    "service": resolved["service"],
                    "country": resolved["country"],
                    "operators": ["any"],
                    "pricing": pricing,
                },
            )
            return

        if method == "GET" and path == "/api/catalog":
            filters = get_filters(query)
            resolved = resolve_selections(filters)
            pricing = CLIENT.get_pricing(resolved["service"]["code"], resolved["country"]["code"])
            try:
                balance = CLIENT.get_balance_cached()
            except HeroSmsError:
                balance = None
            self.send_json(
                200,
                {
                    "filters": filters,
                    "service": resolved["service"],
                    "country": resolved["country"],
                    "operators": ["any"],
                    "pricing": pricing,
                    "balance": balance,
                    "note": "当前兼容 API 主要返回国家维度价格，运营商选择用于下单通道。",
                },
            )
            return

        if method == "GET" and path == "/api/activations":
            items = fetch_upstream_activations()
            items = filter_activations(
                items,
                service_code=query.get("serviceCode", ""),
                country_code=query.get("countryCode", ""),
                operator=query.get("operator", ""),
                price=query.get("price", ""),
            )
            self.send_json(200, {"items": items})
            return

        if method == "GET" and path == "/api/current-phone":
            items = get_current_filtered_activations()
            self.send_json(
                200,
                {
                    "purchaseSettings": get_purchase_settings(),
                    "item": items[0] if items else None,
                    "items": items,
                },
            )
            return

        if method == "GET" and path == "/api/activations/latest":
            items = fetch_upstream_activations()
            items = filter_activations(
                items,
                service_code=query.get("serviceCode", ""),
                country_code=query.get("countryCode", ""),
                operator=query.get("operator", ""),
                price=query.get("price", ""),
            )
            self.send_json(200, {"item": items[0] if items else None})
            return

        if method == "POST" and path == "/api/activations/import":
            items = fetch_upstream_activations()
            self.send_json(200, {"items": items})
            return

        if method == "POST" and path == "/api/activations":
            body = self.read_json_body()
            result = purchase_with_fallback(body)
            item = dict(result["item"])
            item["rawPurchase"] = result["rawPurchase"]
            self.send_json(201, {"item": item, "filters": result["filters"], "attempts": result["attempts"]})
            return

        if method == "POST" and path == "/api/purchase":
            body = self.read_json_body()
            result = purchase_with_fallback(body)
            self.send_json(
                201,
                {
                    "filters": result["filters"],
                    "item": result["item"],
                    "attempts": result["attempts"],
                },
            )
            return

        if method == "POST" and path == "/api/activations/sync":
            self.send_json(200, {"items": fetch_upstream_activations()})
            return

        if method == "GET" and path.startswith("/api/activations/") and path.endswith("/code"):
            activation_id = path.split("/")[-2]
            status = CLIENT.get_status(activation_id)
            upstream_items = fetch_upstream_activations()
            matched = next((item for item in upstream_items if str(item.get("id")) == str(activation_id)), None)
            if matched is None:
                matched = normalize_record(
                    {
                        "id": activation_id,
                        "phoneNumber": "--",
                        "serviceName": "--",
                        "countryName": "--",
                        "operator": "any",
                        "activationCost": None,
                        "status": status["localStatus"],
                        "statusLabel": status["label"],
                        "upstreamStatus": status["upstreamStatus"],
                        "lastCode": status.get("code"),
                        "codes": [status["code"]] if status.get("code") else [],
                        "updatedAt": now_iso(),
                    }
                )
            elif status.get("code"):
                matched["lastCode"] = status["code"]
                matched["codes"] = [status["code"]]
                matched["status"] = status["localStatus"]
                matched["statusLabel"] = status["label"]
                matched["upstreamStatus"] = status["upstreamStatus"]
                matched["updatedAt"] = now_iso()
            self.send_json(200, {"record": matched, "status": status})
            return

        if method == "GET" and path.startswith("/api/phones/") and path.endswith("/code"):
            phone_number = path.split("/")[-2]
            matched = find_activation_by_phone(phone_number)
            if not matched:
                self.send_json(404, {"error": "上游当前活跃号码中找不到该手机号"})
                return
            status = CLIENT.get_status(str(matched["id"]))
            if status.get("code"):
                matched["lastCode"] = status["code"]
                matched["codes"] = [status["code"]]
                matched["status"] = status["localStatus"]
                matched["statusLabel"] = status["label"]
                matched["upstreamStatus"] = status["upstreamStatus"]
                matched["updatedAt"] = now_iso()
            self.send_json(200, {"phoneNumber": phone_number, "record": matched, "status": status})
            return

        if method == "GET" and path.startswith("/api/phones/"):
            parts = path.strip("/").split("/")
            if len(parts) == 3 and parts[0] == "api" and parts[1] == "phones":
                phone_number = parts[2]
                matched = find_activation_by_phone(phone_number)
                if not matched:
                    self.send_json(404, {"error": "上游当前活跃号码中找不到该手机号"})
                    return
                self.send_json(200, {"item": matched})
                return

        if method == "GET" and path.startswith("/api/activations/"):
            parts = path.strip("/").split("/")
            if len(parts) == 3 and parts[0] == "api" and parts[1] == "activations":
                activation_id = parts[2]
                upstream_items = fetch_upstream_activations()
                matched = next((item for item in upstream_items if str(item.get("id")) == str(activation_id)), None)
                if matched is None:
                    status = CLIENT.get_status(activation_id)
                    matched = normalize_record(
                        {
                            "id": activation_id,
                            "phoneNumber": "--",
                            "serviceName": "--",
                            "countryName": "--",
                            "operator": "any",
                            "activationCost": None,
                            "status": status["localStatus"],
                            "statusLabel": status["label"],
                            "upstreamStatus": status["upstreamStatus"],
                            "lastCode": status.get("code"),
                            "codes": [status["code"]] if status.get("code") else [],
                            "updatedAt": now_iso(),
                        }
                    )
                self.send_json(200, {"item": matched})
                return

        if method == "POST" and path.startswith("/api/activations/"):
            parts = path.strip("/").split("/")
            if len(parts) == 4 and parts[0] == "api" and parts[1] == "activations":
                activation_id = parts[2]
                action = parts[3]
                if action in {"cancel", "finish", "ready"}:
                    action_map = {
                        "cancel": {"status": 8, "localStatus": "canceled", "label": "已取消"},
                        "finish": {"status": 6, "localStatus": "finished", "label": "已完成"},
                        "ready": {"status": 1, "localStatus": "waiting_for_code", "label": "等待验证码"},
                    }
                    current = action_map[action]
                    existing_record = STORE.get(activation_id)
                    if action == "cancel" and existing_record:
                        advance_purchase_group_cursor_after_group(existing_record.get("purchaseGroupIndex"))
                    try:
                        upstream = CLIENT.set_status(activation_id, current["status"])
                    except HeroSmsError as error:
                        if action != "cancel" or not is_early_cancel_denied_error(error):
                            raise
                        item = normalize_record(
                            {
                                **(existing_record or {"id": activation_id}),
                                "lastAction": "cancel_denied",
                                "cancelDeferred": True,
                                "cancelWarning": str(error),
                                "updatedAt": now_iso(),
                            }
                        )
                        item = STORE.upsert(item)
                        self.send_json(
                            200,
                            {
                                "item": normalize_record(item),
                                "upstream": {"raw": None, "result": "cancel_deferred"},
                                "warning": str(error),
                            },
                        )
                        return
                    item = normalize_record(
                        {
                            "id": activation_id,
                            "status": current["localStatus"],
                            "statusLabel": current["label"],
                            "lastAction": action,
                            "updatedAt": now_iso(),
                        }
                    )
                    self.send_json(200, {"item": item, "upstream": upstream})
                    return

        if method == "POST" and path.startswith("/api/phones/"):
            parts = path.strip("/").split("/")
            if len(parts) == 4 and parts[0] == "api" and parts[1] == "phones":
                phone_number = parts[2]
                action = parts[3]
                matched = find_activation_by_phone(phone_number)
                if not matched:
                    self.send_json(404, {"error": "上游当前活跃号码中找不到该手机号"})
                    return
                if action not in {"cancel", "finish", "ready"}:
                    self.send_json(404, {"error": "接口不存在"})
                    return
                action_map = {
                    "cancel": {"status": 8, "localStatus": "canceled", "label": "已取消"},
                    "finish": {"status": 6, "localStatus": "finished", "label": "已完成"},
                    "ready": {"status": 1, "localStatus": "waiting_for_code", "label": "等待验证码"},
                }
                current = action_map[action]
                if action == "cancel":
                    advance_purchase_group_cursor_after_group(matched.get("purchaseGroupIndex"))
                try:
                    upstream = CLIENT.set_status(str(matched["id"]), current["status"])
                except HeroSmsError as error:
                    if action != "cancel" or not is_early_cancel_denied_error(error):
                        raise
                    item = STORE.upsert(
                        {
                            **matched,
                            "lastAction": "cancel_denied",
                            "cancelDeferred": True,
                            "cancelWarning": str(error),
                            "updatedAt": now_iso(),
                        }
                    )
                    self.send_json(
                        200,
                        {
                            "phoneNumber": phone_number,
                            "item": normalize_record(item),
                            "upstream": {"raw": None, "result": "cancel_deferred"},
                            "warning": str(error),
                        },
                    )
                    return
                item = normalize_record(
                    {
                        **matched,
                        "status": current["localStatus"],
                        "statusLabel": current["label"],
                        "lastAction": action,
                        "updatedAt": now_iso(),
                    }
                )
                self.send_json(200, {"phoneNumber": phone_number, "item": item, "upstream": upstream})
                return

        if method == "GET" and path == "/api":
            self.send_json(
                200,
                {
                    "endpoints": {
                        "health": "GET /api/health",
                        "config": "GET /api/config",
                        "appSettings": "GET /api/app-settings",
                        "saveAppSettings": "POST /api/app-settings",
                        "balance": "GET /api/balance",
                        "purchaseCountries": "GET /api/purchase-catalog/countries?query=中国",
                        "refreshPurchaseCountries": "POST /api/purchase-catalog/countries/refresh",
                        "purchaseOperators": "GET /api/purchase-catalog/operators?countryCode=33&serviceCode=dr",
                        "purchase": "POST /api/purchase",
                        "listActive": "GET /api/activations",
                        "emailQueue": "GET /api/email-queue",
                        "saveEmailQueue": "POST /api/email-queue",
                        "generateEmailQueue": "POST /api/email-queue/generate",
                        "latestEmailMail": "GET /api/email-queue/mail/latest",
                        "tempMailSettings": "GET /api/temp-mail/settings",
                        "tempMailCreate": "POST /api/temp-mail/address",
                        "tempMailListMails": "GET /api/temp-mail/address/:address/mails",
                        "tempMailLatestMail": "GET /api/temp-mail/address/:address/mails/latest",
                        "tempMailDelete": "DELETE /api/temp-mail/address/:address",
                        "codexAuthUrl": "GET /api/codex-oauth/url",
                        "codexOauthCallback": "POST /api/codex-oauth/callback",
                        "codexAuthStatus": "GET /api/codex-oauth/status?state=xxxx",
                        "codexAuthFiles": "GET /api/codex-oauth/files",
                        "ucSignupStatus": "GET /api/uc-signup/status",
                        "ucSignupStart": "POST /api/uc-signup/start",
                        "ucSignupStop": "POST /api/uc-signup/stop",
                        "ucSignupLogs": "GET /api/uc-signup/logs",
                    }
                },
            )
            return

        if method == "GET" and path == "/api/uc-signup/status":
            self.send_json(200, {"ucSignupState": UC_SIGNUP_MANAGER.get_state()})
            return

        if method == "POST" and path == "/api/uc-signup/start":
            body = self.read_json_body()
            emails = normalize_email_lines(body.get("emails", []))
            if not emails:
                queue = load_email_queue()
                emails = normalize_email_lines(queue.get("emails", []))
            if not emails:
                self.send_json(400, {"error": "没有可注册的邮箱，请先生成邮箱列表"})
                return
            result = UC_SIGNUP_MANAGER.start(
                emails,
                apiBase=body.get("apiBase"),
                display=body.get("display"),
                proxy=body.get("proxy"),
                chromeBinary=body.get("chromeBinary"),
                chromeVersion=body.get("chromeVersion"),
                password=body.get("password"),
                name=body.get("name"),
                age=body.get("age"),
            )
            if "error" in result:
                self.send_json(409, result)
                return
            queue = load_email_queue()
            queue = save_email_queue({**queue, "cursor": 0, "activeEmail": emails[0] if emails else ""})
            self.send_json(200, {"ucSignupState": result["ucSignupState"], "emailQueue": queue})
            return

        if method == "POST" and path == "/api/uc-signup/stop":
            self.send_json(200, UC_SIGNUP_MANAGER.stop())
            return

        if method == "GET" and path == "/api/uc-signup/logs":
            self.send_json(200, {"logs": UC_SIGNUP_MANAGER.get_logs()})
            return

        self.send_json(404, {"error": "接口不存在"})

    def read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length > 0 else b""
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            raise HeroSmsError("请求体不是合法 JSON")

    def send_json(self, status_code: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_html(self, status_code: int, html: str) -> None:
        data = html.encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main() -> None:
    server = ThreadingHTTPServer((CONFIG.host, CONFIG.port), AppHandler)
    print(f"{APP_NAME} listening on http://{CONFIG.host}:{CONFIG.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
