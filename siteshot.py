#!/usr/bin/env python3
"""
SiteShot — screenshot a local web project from one Python file.

Run it inside a project (or point --root at one):

    pip install playwright
    playwright install chromium
    python siteshot.py

Every run writes a new directory under <project>/.siteshot/runs/ and leaves
older runs untouched. .siteshot/latest.txt points at the newest run.

    python siteshot_v2.py --url http://127.0.0.1:3000
    python siteshot_v2.py --headful
    python siteshot_v2.py --route /dashboard
    python siteshot_v2.py --no-auth

Optional .siteshot.json in the project root:

    {
      "base_url": "http://127.0.0.1:3000",
      "routes": ["/dashboard"],
      "dynamic_values": {"id": "1", "slug": "demo"},
      "auth": {"email": "test@example.com", "password": "set-with-SITESHOT_PASSWORD"}
    }

Priority is command line, then environment, then .siteshot.json, then defaults.
Auth environment variables: SITESHOT_EMAIL, SITESHOT_PASSWORD, SITESHOT_NAME.
SITESHOT_URL and SITESHOT_NO_AUTH and SITESHOT_MAX_ROUTES are also read.

Exit codes: 0 screenshots saved, 1 run failed or nothing was captured,
2 usage or configuration error, 130 interrupted.

SiteShot crawls one origin, does not run commands it finds in source files,
and only submits forms that look like login or signup. Passwords are never
written to the manifest, the HTML report, or normal log lines.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import html
import ipaddress
import json
import os
import platform
import re
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import threading
import time
import traceback
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlparse, urlunparse


VERSION = "2.1.0"
TOOL_NAME = "SiteShot"

# Ports remembered only so a server we start is not confused with one that
# was already listening. Presence here does not make the port reusable.
SNAPSHOT_PORTS = (
    1313, 24678, 3000, 3001, 4173, 4200, 4321, 5000, 5173, 5174,
    8000, 8080, 8081, 8888,
)
FRAMEWORK_PORTS = {
    "next": (3000,),
    "nuxt": (3000,),
    "sveltekit": (5173,),
    "astro": (4321,),
    "angular": (4200,),
    "vite": (5173,),
    "django": (8000,),
}
GENERIC_PROJECT_NAMES = {
    "app", "web", "www", "client", "server", "frontend", "website", "test",
    "src", "site", "demo", "my-app", "myapp", "webapp", "ui",
}
PORT_ENV_KEYS = {
    "PORT", "VITE_PORT", "DEV_PORT", "ASTRO_PORT", "NUXT_PORT", "NITRO_PORT",
}
SKIP_DIRS = {
    ".git", ".next", ".nuxt", ".svelte-kit", ".output", ".turbo", ".vercel",
    ".parcel-cache", ".cache", ".siteshot", ".idea", "dist", "build", "out",
    "node_modules", ".venv", "venv", "__pycache__", "coverage", "site-packages",
    "dist-packages", "storybook-static", "playwright-report", "test-results",
}
SKIP_FILES = {"siteshot.py", "siteshot_v2.py"}
FRONTEND_SUFFIXES = {
    ".js", ".jsx", ".ts", ".tsx", ".vue", ".svelte", ".astro", ".html", ".htm",
    ".md", ".mdx",
}
ASSET_SUFFIXES = (
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".avif", ".pdf",
    ".zip", ".gz", ".tgz", ".mp4", ".mp3", ".webm", ".woff", ".woff2", ".ttf",
    ".eot", ".otf", ".css", ".js", ".mjs", ".cjs", ".map", ".json", ".xml",
    ".txt", ".wasm", ".csv",
)
SKIP_PREFIXES = (
    "/api", "/graphql", "/_next", "/__nextjs", "/_nuxt", "/__nuxt", "/_astro",
    "/@vite", "/@fs", "/@id", "/@react-refresh", "/node_modules", "/__webpack",
    "/.well-known",
)
SKIP_EXACT = {
    "/favicon.ico", "/robots.txt", "/sitemap.xml", "/manifest.json",
    "/manifest.webmanifest", "/service-worker.js", "/sw.js",
}
WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
LOGIN_HINTS = (
    "/login", "/log-in", "/signin", "/sign-in", "/sign_in", "/auth/login",
    "/auth/signin",
)
SIGNUP_HINTS = (
    "/signup", "/sign-up", "/sign_up", "/register", "/create-account",
    "/auth/register", "/auth/signup",
)
LOGIN_WORDS = ("log in", "login", "sign in", "signin", "دخول", "تسجيل الدخول")
SIGNUP_WORDS = (
    "sign up", "signup", "register", "create account", "create an account",
    "إنشاء حساب", "انشاء حساب", "تسجيل جديد",
)
BUTTON_DENY = (
    "google", "github", "gitlab", "facebook", "apple", "microsoft", "discord",
    "twitter", "sso", "oauth", "saml", "magic", "forgot", "reset password",
    "delete", "remove", "destroy", "deactivate", "unsubscribe", "log out",
    "logout", "sign out", "close account",
)
TERMS_WORDS = ("terms", "agree", "privacy", "consent", "policy", "شرط", "موافق")
MARKETING_WORDS = ("newsletter", "marketing", "promo", "subscribe", "offers")
AUTH_FAILURE_PHRASES = (
    "invalid email", "invalid password", "incorrect password", "wrong password",
    "invalid credentials", "incorrect email or password", "doesn't match",
    "does not match", "already exists", "already registered", "already been taken",
    "account locked", "too many attempts", "too many requests", "captcha",
    "csrf", "password is too", "password must", "weak password",
    "check your email", "verify your email", "verification link", "magic link",
    "sign-in link", "email me a link", "user not found", "no account",
    "كلمة المرور غير", "البريد غير", "الحساب موجود",
)
STORAGE_HINTS = ("token", "auth", "session", "user", "jwt", "credential")
COOKIE_IGNORE = ("csrf", "xsrf", "consent", "locale", "lang", "theme", "gdpr")
TRACKING_PARAMS = {"fbclid", "gclid", "mc_cid", "mc_eid", "ref", "spm"}
KNOWN_CONFIG_KEYS = {
    "base_url", "routes", "dynamic_values", "auth", "max_routes", "max_depth",
    "max_source_files", "max_discovered", "timeout", "server_timeout",
    "settle_ms", "width", "height", "locale", "headful", "no_auth", "scroll",
    "output",
}
AUTH_CONFIG_KEYS = {"email", "password", "name", "username"}
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
LOCAL_URL_RE = re.compile(
    r"https?://(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\]|\[::\]|::1):\d{2,5}(?:/[^\s\"'<>]*)?",
    re.I,
)
USERINFO_RE = re.compile(r"(https?://)[^/\s:@]+:[^/\s@]+@")
CREDENTIAL_RE = re.compile(
    r"(?i)\b(password|passwd|secret|api[_-]?key|authorization|token)\b\s*[=:]\s*\S+"
)
HREF_RE = re.compile(r"""(?:href|to)\s*=\s*(?P<q>["'`])(?P<v>.{1,2048}?)(?P=q)""", re.I)
PATH_RE = re.compile(
    r"""(?:\bpath|\broute)\s*[:=]\s*(?P<q>["'`])(?P<v>/.{0,2048}?)(?P=q)""",
    re.I,
)
DECORATOR_RE = re.compile(
    r"""@(?:[\w.]+\.)?(?:route|get)\(\s*r?["']([^"']+)["']""",
    re.I,
)
DJANGO_PATH_RE = re.compile(r"""\bpath\(\s*r?(?P<q>['"])(?P<p>.*?)(?P=q)""")
RE_PATH_RE = re.compile(r"""\bre_path\(\s*r?['"]([^'"]+)['"]""")
PORT_FLAG_RE = re.compile(r"(?:--port(?:=|\s+)|(?:^|\s)-p\s+)(\d{2,5})")
PORT_ASSIGN_RE = re.compile(r"\b(?:PORT|VITE_PORT|DEV_PORT)\s*=\s*(\d{2,5})")
CONFIG_PORT_RE = re.compile(r"""(?<![A-Za-z0-9_])['"]?port['"]?\s*[:=]\s*(\d{2,5})""")
LOCALE_RE = re.compile(r"^[A-Za-z]{2,3}([_-][A-Za-z0-9]{2,8})?$")

ANIMATION_JS = """
() => {
  const apply = () => {
    const root = document.documentElement;
    if (!root || root.querySelector("style[data-siteshot]")) return;
    const style = document.createElement("style");
    style.setAttribute("data-siteshot", "1");
    style.textContent = "*{animation-duration:0.001s !important;animation-iteration-count:1 !important;transition-duration:0.001s !important;caret-color:transparent !important;scroll-behavior:auto !important;}";
    root.appendChild(style);
  };
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", apply, {once: true});
  }
  apply();
}
"""

SCROLL_JS = """
async () => {
  const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const fonts = document.fonts && document.fonts.ready ? document.fonts.ready : Promise.resolve();
  await Promise.race([fonts, delay(1200)]);
  document.querySelectorAll("img[loading='lazy']").forEach((img) => { img.loading = "eager"; });
  const height = Math.max(
    document.body ? document.body.scrollHeight : 0,
    document.documentElement ? document.documentElement.scrollHeight : 0
  );
  const step = Math.max(500, window.innerHeight * 0.8);
  for (let i = 0, y = 0; i < 16 && y < height; i += 1, y += step) {
    window.scrollTo(0, y);
    await delay(40);
  }
  window.scrollTo(0, 0);
}
"""

FORM_SNAPSHOT_JS = r"""
() => {
  const captchaSelector = [
    "iframe[src*='recaptcha']",
    "iframe[src*='hcaptcha']",
    ".cf-turnstile",
    ".g-recaptcha",
    "[data-sitekey]"
  ].join(",");
  const visible = (el) => {
    const style = window.getComputedStyle(el);
    if (style.display === "none" || style.visibility === "hidden") return false;
    return el.getClientRects().length > 0;
  };
  const labelFor = (el, root) => {
    if (el.id) {
      const label = root.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (label) return (label.innerText || "").trim().slice(0, 180);
    }
    const wrap = el.closest("label");
    if (wrap) return (wrap.innerText || "").trim().slice(0, 180);
    return "";
  };
  const describe = (root) => {
    const controls = [...root.querySelectorAll("input, textarea, select, button")];
    return {
      text: (root.innerText || "").slice(0, 1200),
      captcha: !!root.querySelector(captchaSelector),
      controls: controls.map((el, index) => {
        const tag = el.tagName.toLowerCase();
        let type = (el.getAttribute("type") || "").toLowerCase();
        if (!type && tag === "button") type = "submit";
        if (!type && tag === "textarea") type = "textarea";
        if (!type && tag === "select") type = "select";
        if (!type) type = "text";
        const options = tag === "select"
          ? [...el.options].map((option) => (option.textContent || "").trim()).filter(Boolean).slice(0, 20)
          : [];
        return {
          index,
          tag,
          type,
          name: el.getAttribute("name") || "",
          id: el.id || "",
          placeholder: el.getAttribute("placeholder") || "",
          aria: el.getAttribute("aria-label") || "",
          autocomplete: (el.getAttribute("autocomplete") || "").toLowerCase(),
          required: !!el.required,
          disabled: !!el.disabled || el.getAttribute("aria-disabled") === "true",
          visible: visible(el),
          label: labelFor(el, root),
          text: (el.innerText || el.getAttribute("value") || "").trim().slice(0, 180),
          options,
        };
      }),
    };
  };
  const forms = [...document.querySelectorAll("form")];
  forms.forEach((form, index) => form.setAttribute("data-siteshot-form", String(index)));
  const described = forms.map((form, index) => ({...describe(form), id: String(index), synthetic: false}));
  let extra = 0;
  for (const input of document.querySelectorAll("input[type='password']")) {
    if (input.closest("form")) continue;
    let node = input.parentElement;
    let chosen = null;
    while (node && node !== document.body) {
      if (node.querySelector("button, input[type='submit']")) {
        chosen = node;
        break;
      }
      node = node.parentElement;
    }
    if (!chosen || chosen.querySelector("form")) continue;
    const id = `s${extra}`;
    extra += 1;
    chosen.setAttribute("data-siteshot-form", id);
    described.push({...describe(chosen), id, synthetic: true});
  }
  return described;
}
"""


class SiteShotError(Exception):
    """Expected, user-facing failure. `exit_code` is the process status."""

    def __init__(self, message: str, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code


@dataclass
class Log:
    """Process-local logger. Secrets passed to add_secret are scrubbed."""

    secrets: list[str] = field(default_factory=list)

    def add_secret(self, value: str | None) -> None:
        if value and value not in self.secrets:
            self.secrets.append(value)

    def scrub(self, text: str) -> str:
        return scrub(text, self.secrets)

    def emit(self, category: str, message: str) -> None:
        print(f"[{category}] {self.scrub(message)}", flush=True)


@dataclass(frozen=True, slots=True)
class Route:
    path: str
    query: str = ""
    fragment: str = ""

    def key(self) -> str:
        text = self.path or "/"
        if self.query:
            text += "?" + self.query
        if self.fragment:
            marker = self.fragment if self.fragment.startswith("!") or self.fragment.startswith("/") else "/" + self.fragment
            text += "#" + marker
        return text


@dataclass
class RouteSet:
    max_stored: int
    max_depth: int
    routes: dict[str, Route] = field(default_factory=dict)
    depth: dict[str, int] = field(default_factory=dict)
    priority: set[str] = field(default_factory=set)
    unresolved: list[dict[str, str]] = field(default_factory=list)
    ignored: list[dict[str, str]] = field(default_factory=list)
    dropped_depth: int = 0
    dropped_cap: int = 0
    _seen_unresolved: set[str] = field(default_factory=set)
    _seen_ignored: set[str] = field(default_factory=set)

    def add(self, route: Route, depth: int, *, priority: bool = False) -> bool:
        key = route.key()
        if priority:
            self.priority.add(key)
        if key in self.routes:
            if depth < self.depth.get(key, depth):
                self.depth[key] = depth
            return False
        if not priority and depth > self.max_depth:
            self.dropped_depth += 1
            return False
        if len(self.routes) >= self.max_stored:
            self.dropped_cap += 1
            return False
        self.routes[key] = route
        self.depth[key] = depth
        return True

    def add_unresolved(self, pattern: str, reason: str) -> None:
        if pattern in self._seen_unresolved or len(self.unresolved) >= 200:
            self._seen_unresolved.add(pattern)
            return
        self._seen_unresolved.add(pattern)
        self.unresolved.append({"pattern": pattern, "reason": reason})

    def add_ignored(self, pattern: str, reason: str) -> None:
        if pattern in self._seen_ignored or len(self.ignored) >= 80:
            self._seen_ignored.add(pattern)
            return
        self._seen_ignored.add(pattern)
        self.ignored.append({"pattern": pattern, "reason": reason})

    def pending(self, completed: set[str]) -> list[Route]:
        items = [route for key, route in self.routes.items() if key not in completed]
        items.sort(key=lambda route: (
            0 if route.key() in self.priority else 1,
            self.depth.get(route.key(), 0),
            route.path.count("/"),
            route.key(),
        ))
        return items


@dataclass
class Probe:
    url: str
    status: int
    body_sample: str
    headers: dict[str, str]
    port: int


@dataclass
class ProjectInfo:
    framework: str = "unknown"
    package_manager: str | None = None
    dev_command: list[str] | None = None
    declared_ports: list[int] = field(default_factory=list)
    default_ports: list[int] = field(default_factory=list)
    name: str | None = None
    dev_script: str | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass
class Settings:
    root: Path
    output_name: str
    base_url: str | None
    extra_routes: list[str]
    config_routes: list[str]
    dynamic_values: dict[str, str]
    max_routes: int
    max_depth: int
    max_source_files: int
    max_discovered: int
    timeout_ms: int
    server_timeout_s: int
    settle_ms: int
    width: int
    height: int
    locale: str
    headful: bool
    no_auth: bool
    scroll: bool
    email: str | None
    password: str | None
    name: str | None
    username: str | None
    user_supplied_credentials: bool


@dataclass
class Shot:
    requested_path: str
    final_url: str = ""
    file: str = ""
    title: str = ""
    status: str = "pending"
    http_status: int | None = None
    error: str = ""
    redirected_to_auth: bool = False
    redirect_count: int = 0
    elapsed_ms: int = 0
    notes: list[str] = field(default_factory=list)
    console_errors: list[str] = field(default_factory=list)


@dataclass
class AuthResult:
    attempted: bool = False
    succeeded: bool = False
    mode: str = "none"
    reason: str = ""
    notes: list[str] = field(default_factory=list)


def scrub(text: str, secrets: Iterable[str]) -> str:
    """Remove known secrets without destroying unrelated text for short values."""
    if not text:
        return text
    for secret in secrets:
        if not secret:
            continue
        if len(secret) >= 4:
            text = text.replace(secret, "[redacted]")
        elif text == secret:
            text = "[redacted]"
    text = USERINFO_RE.sub(r"\1", text)
    text = CREDENTIAL_RE.sub(lambda match: f"{match.group(1)}=[redacted]", text)
    return text


def scrub_obj(value: Any, secrets: Iterable[str]) -> Any:
    if isinstance(value, str):
        return scrub(value, secrets)
    if isinstance(value, list):
        return [scrub_obj(item, secrets) for item in value]
    if isinstance(value, dict):
        return {key: scrub_obj(item, secrets) for key, item in value.items()}
    return value


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(errors="replace")
        except (OSError, ValueError):
            return


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(text, encoding="utf-8", newline="\n")
    try:
        temporary.replace(path)
    except OSError:
        if temporary.exists():
            temporary.unlink()
        raise


def is_within(root: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def dedupe(items: Iterable[Any]) -> list[Any]:
    seen: set[Any] = set()
    result = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def parse_port(value: str) -> int | None:
    if not value.isdigit():
        return None
    port = int(value)
    if 1 <= port <= 65535:
        return port
    return None


def effective_port(parsed: Any) -> int:
    if parsed.port:
        return int(parsed.port)
    if parsed.scheme == "https":
        return 443
    if parsed.scheme == "http":
        return 80
    return -1


def format_origin(parsed: Any) -> str:
    host = parsed.hostname or "localhost"
    if ":" in host:
        host = f"[{host}]"
    port = parsed.port
    netloc = host if port is None else f"{host}:{port}"
    return f"{parsed.scheme}://{netloc}"


def hosts_equal(left: str | None, right: str | None) -> bool:
    a = (left or "").lower().rstrip(".")
    b = (right or "").lower().rstrip(".")
    if a == b:
        return True
    return a in LOOPBACK_HOSTS and b in LOOPBACK_HOSTS


def same_site(url: str, origin: str) -> bool:
    left, right = urlparse(url), urlparse(origin)
    if left.scheme not in {"http", "https"} or right.scheme not in {"http", "https"}:
        return False
    if not hosts_equal(left.hostname, right.hostname):
        return False
    return effective_port(left) == effective_port(right)


def assert_safe_target(hostname: str) -> None:
    try:
        address = ipaddress.ip_address(hostname.strip("[]"))
    except ValueError:
        return
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        address = mapped
    # Python 3.13+ reports link-local and 0.0.0.0 as private. Reject those
    # before allowing the rest of the private ranges.
    if address.is_loopback:
        return
    if address.is_unspecified or address.is_link_local or address.is_multicast or address.is_reserved:
        raise SiteShotError(
            f"Refusing to connect to {hostname}. Pass a loopback, private, or named host.",
            2,
        )
    if address.is_private:
        return
    raise SiteShotError(
        f"Refusing to connect to {hostname}. Pass a loopback, private, or named host.",
        2,
    )


def canonicalize_user_url(value: str) -> str:
    raw = value.strip()
    if not raw:
        raise SiteShotError("URL is empty.", 2)
    if "://" not in raw:
        head = raw.split("/", 1)[0]
        if head.startswith("["):
            _bracket, sep, port = head.partition("]")
            if not sep or (port and not (port.startswith(":") and port[1:].isdigit())):
                raise SiteShotError("URL must start with http:// or https://.", 2)
        else:
            _host, sep, port = head.rpartition(":")
            if sep and not port.isdigit():
                raise SiteShotError("URL must start with http:// or https://.", 2)
        raw = "http://" + raw
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"}:
        raise SiteShotError("URL must start with http:// or https://.", 2)
    if parsed.username or parsed.password:
        raise SiteShotError("Refusing a URL that contains credentials. Use SITESHOT_PASSWORD.", 2)
    if not parsed.hostname:
        raise SiteShotError(f"URL is missing a host: {value}", 2)
    try:
        assert_safe_target(parsed.hostname)
        origin = format_origin(parsed)
    except ValueError as exc:
        raise SiteShotError("URL is not a valid http(s) address.", 2) from exc
    path = parsed.path or ""
    if path.endswith("/") and path != "/":
        path = path[:-1]
    if path and path != "/":
        return origin + path
    return origin


def split_base(url: str) -> tuple[str, str]:
    parsed = urlparse(url)
    origin = format_origin(parsed)
    path = parsed.path or ""
    if path.endswith("/") and path != "/":
        path = path[:-1]
    if path == "/":
        path = ""
    return origin, path


def clean_path(path: str) -> str | None:
    if len(path) > 2048 or any(ord(char) < 32 for char in path):
        return None
    parts: list[str] = []
    for part in path.replace("\\", "/").split("/"):
        part = part.strip()
        if part in {"", "."}:
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        if any(char in part for char in "?#"):
            return None
        parts.append(part.replace(" ", "%20"))
    return "/" + "/".join(parts)


def canonical_query(query: str) -> str:
    pairs = parse_qsl(query, keep_blank_values=False, errors="replace")
    kept: list[tuple[str, str]] = []
    for key, value in pairs:
        lowered = key.lower()
        if lowered.startswith("utm_") or lowered in TRACKING_PARAMS:
            continue
        if len(key) > 80 or len(value) > 180:
            continue
        kept.append((key, value))
    kept.sort()
    if not kept:
        return ""
    return urlencode(kept, doseq=True, quote_via=quote)


def canonical_fragment(fragment: str) -> str:
    fragment = fragment.strip()
    if fragment.startswith("!/"):
        path = clean_path("/" + fragment[2:])
        if not path or path == "/":
            return ""
        return "!" + path
    if fragment.startswith("/"):
        path = clean_path(fragment)
        if not path or path == "/":
            return ""
        return path
    return ""


def has_skipped_prefix(path: str) -> bool:
    lowered = path.lower()
    if lowered in SKIP_EXACT:
        return True
    for prefix in SKIP_PREFIXES:
        if lowered == prefix or lowered.startswith(prefix + "/"):
            return True
    return False


def is_asset_path(path: str) -> bool:
    lowered = path.lower()
    return lowered.endswith(ASSET_SUFFIXES) or lowered.endswith(".min.js")


def looks_like_pattern(path: str) -> bool:
    if any(token in path for token in "[]{}<>"):
        return True
    return any(part.startswith(":") for part in path.split("/") if part)


def normalize_route(raw: str, origin: str, *, keep_query: bool) -> Route | None:
    if not raw or len(raw) > 4096:
        return None
    text = raw.strip()
    lowered = text.lower()
    if lowered.startswith((
        "mailto:", "tel:", "javascript:", "data:", "blob:", "file:", "sms:", "ftp:", "about:",
    )):
        return None
    if text.startswith("#") and not text.startswith(("#/", "#!/")):
        return None
    if any(ord(char) < 32 for char in text):
        return None
    absolute = urljoin(origin.rstrip("/") + "/", text)
    parsed = urlparse(absolute)
    if parsed.scheme not in {"http", "https"}:
        return None
    if not same_site(absolute, origin):
        return None
    path = clean_path(unquote_safe(parsed.path or "/"))
    if path is None or has_skipped_prefix(path) or is_asset_path(path):
        return None
    query = canonical_query(parsed.query) if keep_query else ""
    fragment = canonical_fragment(unquote_safe(parsed.fragment))
    return Route(path=path, query=query, fragment=fragment)


def unquote_safe(value: str) -> str:
    # One pass. A second pass would turn %252e%252e into "..".
    from urllib.parse import unquote
    return unquote(value)


def navigate_url(origin: str, base_path: str, route: Route) -> str:
    path = route.path or "/"
    prefix = base_path.rstrip("/")
    if prefix:
        if path == "/":
            path = prefix + "/"
        elif path != prefix and not path.startswith(prefix + "/"):
            path = prefix + path
    url = origin.rstrip("/") + path
    if route.query:
        url += "?" + route.query
    if route.fragment:
        marker = route.fragment
        if not marker.startswith("!") and not marker.startswith("/"):
            marker = "/" + marker
        url += "#" + marker
    return url


def _dynamic_value(values: Mapping[str, str], key: str, *, allow_slash: bool) -> tuple[str | None, str | None]:
    if key not in values or values[key] == "":
        return None, f"dynamic segment '{key}' has no configured example"
    raw = str(values[key]).strip()
    pieces = raw.split("/") if allow_slash else [raw]
    if not allow_slash and "/" in raw:
        return None, f"configured value for '{key}' is not a single path segment"
    if any(part in {"", ".", ".."} or "\\" in part or "://" in part for part in pieces):
        return None, f"configured value for '{key}' is not a safe path"
    return raw, None


def resolve_pattern(pattern: str, values: Mapping[str, str]) -> tuple[str | None, str | None]:
    """Resolve a framework pattern. Missing required params stay unresolved.

    Optional catch-all segments are omitted when no example is configured.
    No sample ids are invented.
    """
    text = pattern.strip()
    if not text.startswith("/"):
        text = "/" + text
    output: list[str] = []
    for segment in text.split("/"):
        if segment == "":
            continue
        if re.fullmatch(r"\([^.)][^)]*\)", segment):
            continue
        if segment.startswith("(") and re.match(r"\(\.+\)", segment):
            return None, f"intercepting route '{pattern}' is not crawled"
        if segment.startswith("@"):
            continue
        if segment.startswith("_"):
            return None, f"private segment '{segment}' is not a public route"
        optional_catch = re.fullmatch(r"\[\[\.\.\.(.+)\]\]", segment)
        required_catch = re.fullmatch(r"\[\.\.\.(.+)\]", segment)
        optional_one = re.fullmatch(r"\[\[(.+)\]\]", segment)
        required_one = re.fullmatch(r"\[([^\[\]]+)\]", segment)
        colon = re.fullmatch(r":([A-Za-z_][\w-]*)", segment)
        brace = re.fullmatch(r"\{([A-Za-z_][\w-]*)\}", segment)
        django = re.fullmatch(r"<(?:(?P<conv>[A-Za-z_][\w]*):)?(?P<name>[A-Za-z_][\w]*)>", segment)
        if optional_catch or optional_one:
            key = (optional_catch or optional_one).group(1).strip(".")
            allow_slash = optional_catch is not None
            if key not in values or values[key] == "":
                continue
            concrete, reason = _dynamic_value(values, key, allow_slash=allow_slash)
            if concrete is None:
                return None, reason
            output.extend(part for part in concrete.split("/") if part)
            continue
        if required_catch or required_one or colon or brace or django:
            if required_catch:
                key = required_catch.group(1).strip(".")
                allow_slash = True
            elif required_one:
                key = required_one.group(1).strip(".")
                allow_slash = False
            elif colon:
                key = colon.group(1)
                allow_slash = False
            elif brace:
                key = brace.group(1)
                allow_slash = False
            else:
                key = django.group("name")
                allow_slash = False
            concrete, reason = _dynamic_value(values, key, allow_slash=allow_slash)
            if concrete is None:
                return None, reason or f"dynamic segment '{key}' has no configured example"
            output.extend(part for part in concrete.split("/") if part)
            continue
        if any(char in segment for char in "*{}[]()<>"):
            return None, f"unsupported pattern segment '{segment}'"
        output.append(segment)
    return "/" + "/".join(output) if output else "/", None


def screenshot_name(route: str, used: set[str]) -> str:
    label = "home" if route in {"", "/"} else route.lstrip("/")
    label = (
        label.replace("/", "__")
        .replace("?", "__q__")
        .replace("#", "__h__")
        .replace("&", "_")
        .replace("=", "-")
    )
    cleaned = []
    for char in label:
        if char in '<>:"\\|?*' or ord(char) < 32:
            cleaned.append("_")
        else:
            cleaned.append(char)
    stem = "".join(cleaned).strip(" ._") or "page"
    if stem.split(".", 1)[0].upper() in WINDOWS_RESERVED:
        stem += "_page"
    if len(stem) > 80:
        digest = hashlib.sha256(route.encode("utf-8")).hexdigest()[:8]
        stem = stem[:70].rstrip(" ._") + "_" + digest
    name = stem + ".png"
    number = 2
    while True:
        reserved = Path(name).stem.split(".", 1)[0].upper() in WINDOWS_RESERVED
        if name.lower() not in {item.lower() for item in used} and not reserved:
            break
        name = f"{stem}_{number}.png"
        number += 1
        if number > 80:
            digest = hashlib.sha256(f"{route}:{number}".encode("utf-8")).hexdigest()[:10]
            name = f"route_{digest}.png"
            break
    used.add(name)
    return name


def build_spawn_argv(command: list[str], resolved: str | None, os_name: str, comspec: str = "cmd.exe") -> list[str]:
    """Build a shell-free argument vector for ``Popen``.

    Batch files are the executable itself. On Windows, CreateProcess launches
    a ``.cmd`` or ``.bat`` through ``cmd.exe`` with its own quoting, including
    paths that contain spaces. Wrapping that in another ``cmd /c`` breaks the
    path. ``os_name`` and ``comspec`` stay in the signature so callers can
    record the platform; they do not select a shell.
    """
    if not command:
        return []
    if resolved:
        return [resolved, *command[1:]]
    return list(command)


def prefer_windows_launcher(resolved: str) -> str:
    path = Path(resolved)
    if path.suffix.lower() != ".ps1":
        return resolved
    command = path.with_suffix(".cmd")
    if command.exists():
        return str(command)
    return resolved


def control_meta(control: Mapping[str, Any]) -> str:
    parts = (
        control.get("name"), control.get("id"), control.get("placeholder"),
        control.get("aria"), control.get("label"), control.get("autocomplete"),
        control.get("type"),
    )
    return " ".join(str(part or "") for part in parts).lower()


def is_password_control(control: Mapping[str, Any]) -> bool:
    autocomplete = str(control.get("autocomplete") or "")
    return control.get("type") == "password" or autocomplete in {"current-password", "new-password"}


def is_button(control: Mapping[str, Any]) -> bool:
    if control.get("tag") == "button":
        return True
    return control.get("type") in {"submit", "button", "image"}


def button_denied(control: Mapping[str, Any]) -> bool:
    text = str(control.get("text") or "").lower()
    return any(word in text for word in BUTTON_DENY)


def button_matches(control: Mapping[str, Any], mode: str) -> bool:
    if button_denied(control):
        return False
    text = str(control.get("text") or "").lower()
    words = SIGNUP_WORDS if mode == "signup" else LOGIN_WORDS
    return any(word in text for word in words)


def is_auth_path(path: str) -> bool:
    return path_mode(path) is not None


def path_mode(path: str) -> str | None:
    lowered = "/" + path.lower().strip("/")
    if lowered == "/":
        lowered = "/"
    padded = lowered if lowered == "/" else lowered
    if _path_has_hint(padded, SIGNUP_HINTS):
        return "signup"
    if _path_has_hint(padded, LOGIN_HINTS):
        return "login"
    return None


def _path_has_hint(path: str, hints: tuple[str, ...]) -> bool:
    lowered = path.lower().rstrip("/") or "/"
    return any(lowered == hint or lowered.endswith(hint) for hint in hints)


def classify_auth_form(url_path: str, form: Mapping[str, Any]) -> str | None:
    controls = list(form.get("controls") or [])
    passwords = [control for control in controls if is_password_control(control)]
    buttons = [control for control in controls if is_button(control)]
    mode = path_mode(url_path)
    if form.get("captcha") and (passwords or buttons):
        return "captcha"
    if buttons and all(button_denied(button) for button in buttons) and not passwords:
        return "oauth-only"
    if not passwords:
        text = " ".join(str(button.get("text") or "") for button in buttons).lower()
        if any(word in text for word in ("magic link", "email me", "email a link", "passwordless")):
            return "passwordless"
        if any(button_denied(button) for button in buttons):
            return "oauth-only"
        return None
    autos = {str(control.get("autocomplete") or "") for control in passwords}
    if "current-password" in autos and "new-password" in autos and mode != "signup":
        return "change-password"
    if buttons and all(button_denied(button) for button in buttons):
        return "oauth-only"
    # A login URL stays login even if it also has a sign-up button.
    # Button text is the fallback only when the path is not an auth path.
    if mode == "login":
        return "login"
    if mode == "signup":
        # The path says signup, but a current-password form with no signup
        # action is a login. Do not submit a generated password there.
        has_signup = any(button_matches(button, "signup") for button in buttons)
        has_login = any(button_matches(button, "login") for button in buttons)
        if "new-password" in autos or has_signup:
            return "signup"
        if "current-password" in autos or has_login:
            return "login"
        return "signup"
    if any(button_matches(button, "signup") for button in buttons):
        return "signup"
    if any(button_matches(button, "login") for button in buttons) or "current-password" in autos:
        return "login"
    return None


def choose_submit(controls: Iterable[Mapping[str, Any]], mode: str) -> int | None:
    buttons = [
        control for control in controls
        if is_button(control) and control.get("visible", True) and not control.get("disabled")
    ]
    if not buttons or all(button_denied(button) for button in buttons):
        return None
    preferred = [button for button in buttons if button_matches(button, mode)]
    if preferred:
        return int(preferred[0]["index"])
    opposite = SIGNUP_WORDS if mode == "login" else LOGIN_WORDS
    submits = [
        button for button in buttons
        if not button_denied(button)
        and str(button.get("type") or "") == "submit"
        and not any(word in str(button.get("text") or "").lower() for word in opposite)
    ]
    if len(submits) == 1:
        return int(submits[0]["index"])
    return None


def plan_fills(
    controls: Iterable[Mapping[str, Any]],
    mode: str,
    identity: Mapping[str, str],
) -> tuple[list[dict[str, Any]], str | None]:
    """Plan form fills. An abort reason means the form must not be submitted."""
    actions: list[dict[str, Any]] = []
    saw_password = False
    for control in controls:
        ctype = str(control.get("type") or "text")
        tag = str(control.get("tag") or "input")
        if ctype in {"hidden", "submit", "button", "reset", "image"} or tag == "button":
            continue
        if not control.get("visible", True):
            continue
        if control.get("disabled"):
            if control.get("required"):
                return [], "a required field is disabled"
            continue
        meta = control_meta(control)
        autocomplete = str(control.get("autocomplete") or "")
        if ctype == "file":
            if control.get("required"):
                return [], "a required file upload cannot be filled"
            continue
        if autocomplete == "one-time-code" or any(token in meta for token in ("one-time", "otp", "2fa", "mfa", "verification code")):
            if control.get("required"):
                return [], "a one-time code is required"
            continue
        if ctype == "checkbox":
            marketing = any(word in meta for word in MARKETING_WORDS)
            terms = any(word in meta for word in TERMS_WORDS)
            if marketing and not terms:
                if control.get("required"):
                    return [], "a required marketing checkbox was not accepted"
                continue
            if control.get("required") or terms:
                actions.append({"index": int(control["index"]), "kind": "check"})
            continue
        if ctype == "radio" or tag == "select":
            if tag == "select" and control.get("required"):
                options = [
                    option for option in control.get("options") or []
                    if option and str(option).strip().lower() not in {"select", "choose", "select one"}
                ]
                if len(options) == 1:
                    actions.append({"index": int(control["index"]), "kind": "select", "value": options[0]})
                    continue
                return [], "a required dropdown could not be filled safely"
            if control.get("required"):
                return [], "a required choice could not be filled safely"
            continue
        if is_password_control(control):
            secret = identity.get("password") or ""
            if not secret:
                return [], "password is missing"
            actions.append({"index": int(control["index"]), "kind": "text", "value": secret})
            saw_password = True
            continue
        value, missing = _text_value(control, meta, autocomplete, mode, identity)
        if missing:
            return [], missing
        if value is None:
            if control.get("required"):
                label = str(control.get("label") or control.get("name") or control.get("placeholder") or "field")
                label = " ".join(label.split())[:80]
                return [], f"unrecognized required field ({label})"
            continue
        actions.append({"index": int(control["index"]), "kind": "text", "value": value})
    if not saw_password:
        return [], "no password field"
    return actions, None


def _text_value(
    control: Mapping[str, Any],
    meta: str,
    autocomplete: str,
    mode: str,
    identity: Mapping[str, str],
) -> tuple[str | None, str | None]:
    email = identity.get("email") or ""
    username = identity.get("username") or ""
    name = identity.get("name") or ""
    if control.get("type") == "email" or autocomplete == "email" or "email" in meta or "بريد" in meta:
        if not email:
            return None, "email is missing"
        return email, None
    if autocomplete == "username" or any(token in meta for token in ("username", "user name", "user_name", "اسم المستخدم")):
        if not username:
            return None, "username is missing"
        return username, None
    if control.get("type") == "tel" or autocomplete == "tel" or any(token in meta for token in ("phone", "mobile", "جوال")):
        if control.get("required"):
            return "5550100199", None
        return None, None
    if control.get("type") == "date":
        if control.get("required"):
            return "2000-01-01", None
        return None, None
    if mode != "signup":
        return None, None
    if autocomplete == "given-name" or "first" in meta:
        return (name.split() or ["SiteShot"])[0], None
    if autocomplete == "family-name" or any(token in meta for token in ("last", "surname")):
        parts = name.split()
        return (parts[-1] if len(parts) > 1 else "Bot"), None
    if autocomplete in {"name", "nickname"} or re.search(r"\b(full[\s_-]?name|display[\s_-]?name|name|الاسم)\b", meta):
        if "user" in meta or "file" in meta:
            return None, None
        return name or "SiteShot Bot", None
    return None, None


def select_auth_target(
    forms: Iterable[Mapping[str, Any]],
    url_path: str,
    mode: str,
) -> tuple[Mapping[str, Any] | None, str | None]:
    forms = list(forms)
    for form in forms:
        if classify_auth_form(url_path, form) == mode:
            return form, None
    kinds = {classify_auth_form(url_path, form) for form in forms}
    page_mode = path_mode(url_path)
    if "captcha" in kinds and (page_mode in {None, mode}):
        return None, "captcha is present"
    if "oauth-only" in kinds and (page_mode in {None, mode}):
        return None, "only a social or OAuth login was found"
    if "passwordless" in kinds:
        return None, "passwordless or magic-link login was not submitted"
    if "change-password" in kinds:
        return None, "a change-password form was left untouched"
    opposite = "login" if mode == "signup" else "signup"
    if opposite in kinds:
        if mode == "signup":
            return None, "the signup page presented a login form; a generated password was not submitted"
        return None, "this page is a signup form, and signup was not requested"
    return None, None


def judge_auth(
    *,
    url: str,
    password_fields: int,
    body_text: str,
    new_session: bool,
    captcha: bool,
    invalid_fields: int,
) -> tuple[bool, str]:
    """Require more than a URL change before calling authentication successful."""
    if captcha:
        return False, "captcha is present"
    lowered = body_text.lower()
    for phrase in AUTH_FAILURE_PHRASES:
        if phrase in lowered:
            return False, f"the page reported '{phrase}'"
    if invalid_fields:
        return False, "the form still has invalid fields"
    if password_fields:
        return False, "the password field is still on the page"
    if is_auth_path(urlparse(url).path):
        return False, "still on an authentication page"
    if not new_session:
        return False, "the URL changed but no session cookie or session storage key was set"
    return True, "a session was stored and the password field is gone"


def _looks_like_session_cookie(name: str) -> bool:
    """True for session-shaped names. A new analytics cookie is not a login."""
    lowered = name.lower()
    if any(skip in lowered for skip in COOKIE_IGNORE):
        return False
    if any(hint in lowered for hint in ("session", "sess", "auth", "token", "jwt", "credential")):
        return True
    # Express uses connect.sid. Do not treat "sidebar" as a session cookie.
    return lowered.endswith(".sid") or lowered.endswith("-sid") or lowered.endswith("_sid")


def session_cookie_changed(before: Iterable[Mapping[str, Any]], after: Iterable[Mapping[str, Any]]) -> bool:
    previous = {
        (item.get("name"), item.get("domain"), item.get("path")): item.get("value")
        for item in before
    }
    for cookie in after:
        name = str(cookie.get("name") or "")
        if not _looks_like_session_cookie(name):
            continue
        ident = (cookie.get("name"), cookie.get("domain"), cookie.get("path"))
        if ident not in previous or previous[ident] != cookie.get("value"):
            return True
    return False


def storage_session_changed(before: Iterable[str], after: Iterable[str]) -> bool:
    for key in set(after) - set(before):
        lowered = key.lower()
        if any(hint in lowered for hint in STORAGE_HINTS):
            return True
    return False


def framework_match(framework: str, probe: Probe) -> bool:
    body = probe.body_sample.lower()
    powered = probe.headers.get("x-powered-by", "").lower()
    if framework == "next":
        return "next.js" in powered or "/_next/" in body or "__next_data__" in body
    if framework == "vite":
        return "/@vite/client" in body or "/@vite/env" in body or "/@react-refresh" in body
    if framework == "nuxt":
        return "/_nuxt/" in body or "__nuxt" in body
    if framework == "sveltekit":
        return "__sveltekit" in body or "/_app/immutable/" in body
    if framework == "astro":
        return "/_astro/" in body or "astro-island" in body
    if framework == "angular":
        return "ng-version=" in body
    if framework == "django":
        return "csrfmiddlewaretoken" in body
    return False


def project_name_match(project: ProjectInfo, probe: Probe) -> bool:
    name = (project.name or "").strip().lower()
    if len(name) < 3 or name in GENERIC_PROJECT_NAMES:
        return False
    return re.search(rf"(?<![a-z0-9]){re.escape(name)}(?![a-z0-9])", probe.body_sample.lower()) is not None


def probe_matches_project(project: ProjectInfo, probe: Probe) -> bool:
    return project_name_match(project, probe) or framework_match(project.framework, probe)


def acceptable_probe(probe: Probe) -> bool:
    return 200 <= probe.status < 500 or probe.status in {401, 403}


def choose_existing_server(project: ProjectInfo, probes: Iterable[Probe]) -> tuple[str | None, str]:
    """Pick a server that plausibly belongs to this project, or refuse to guess."""
    by_port: dict[int, Probe] = {}
    for probe in probes:
        if not acceptable_probe(probe):
            continue
        current = by_port.get(probe.port)
        if current is None or probe_matches_project(project, probe) and not probe_matches_project(project, current):
            by_port[probe.port] = probe
        elif "127.0.0.1" in probe.url and "127.0.0.1" not in current.url:
            by_port[probe.port] = probe
    declared = [probe for port, probe in sorted(by_port.items()) if port in set(project.declared_ports)]
    if len(declared) == 1:
        return declared[0].url, "configured port for this project"
    if len(declared) > 1:
        strong = [probe for probe in declared if probe_matches_project(project, probe)]
        if len(strong) == 1:
            return strong[0].url, "configured port with a project fingerprint"
        return None, "multiple configured ports responded; pass --url to choose one"
    hinted = [probe for port, probe in sorted(by_port.items()) if port in set(project.default_ports)]
    strong = [probe for probe in hinted if probe_matches_project(project, probe)]
    if len(strong) == 1:
        return strong[0].url, "framework fingerprint on the default port"
    if len(strong) > 1:
        return None, "multiple servers match this framework; pass --url to choose one"
    if hinted:
        return None, "a default port responded but does not look like this project"
    return None, "no project server was found"


def normalize_logged_url(url: str) -> str | None:
    cleaned = url.rstrip(").,]}>\"'")
    parsed = urlparse(cleaned)
    host = (parsed.hostname or "").lower()
    if host not in {"localhost", "127.0.0.1", "0.0.0.0", "::1", "::"}:
        return None
    # localhost on Windows may resolve to ::1 while the server bound 127.0.0.1.
    if host in {"0.0.0.0", "::", "localhost"}:
        host = "127.0.0.1"
        netloc = host if parsed.port is None else f"{host}:{parsed.port}"
        parsed = parsed._replace(netloc=netloc)
    if host == "::1":
        netloc = "[::1]" if parsed.port is None else f"[::1]:{parsed.port}"
        parsed = parsed._replace(netloc=netloc)
    # A log line may mention a deep link. The server itself is the origin.
    return urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))


def urls_from_dev_log(text: str) -> list[str]:
    cleaned = ANSI_RE.sub("", text)
    found: list[str] = []
    for match in LOCAL_URL_RE.finditer(cleaned):
        url = normalize_logged_url(match.group(0))
        if url and url not in found:
            found.append(url)
    return found


def port_of(url: str) -> int:
    return effective_port(urlparse(url))


def select_started_endpoint(log_text: str, pre_open: set[int], now_open: set[int]) -> str | None:
    """Trust the child process log, and never a port that was already open."""
    fresh = [url for url in urls_from_dev_log(log_text) if port_of(url) not in pre_open]
    if fresh:
        return fresh[-1]
    new_ports = sorted(now_open - pre_open)
    if len(new_ports) == 1:
        return f"http://127.0.0.1:{new_ports[0]}"
    return None


def load_config_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise SiteShotError(f"Could not read {path.name}: {exc.strerror or exc}", 2) from exc
    if size > 256_000:
        raise SiteShotError(f"{path.name} is larger than 256 KB.", 2)
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise SiteShotError(f"Could not read {path.name}: {exc.strerror or exc}", 2) from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        # JSONDecodeError.msg does not include the document. str(exc) can.
        raise SiteShotError(
            f"Invalid JSON in {path.name} at line {exc.lineno}, column {exc.colno}: {exc.msg}",
            2,
        ) from exc
    if not isinstance(data, dict):
        raise SiteShotError(f"{path.name} must contain a JSON object.", 2)
    return data


def validate_config(data: Mapping[str, Any]) -> list[str]:
    warnings: list[str] = []
    unknown = sorted(set(data) - KNOWN_CONFIG_KEYS)
    if unknown:
        warnings.append("Ignoring unknown .siteshot.json keys: " + ", ".join(unknown))
    integer_keys = (
        "max_routes", "max_depth", "max_source_files", "max_discovered",
        "timeout", "server_timeout", "settle_ms", "width", "height",
    )
    for key in integer_keys:
        value = data.get(key)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
            raise SiteShotError(f".siteshot.json {key} must be an integer.", 2)
    for key in ("headful", "no_auth", "scroll"):
        value = data.get(key)
        if value is not None and not isinstance(value, bool):
            raise SiteShotError(f".siteshot.json {key} must be true or false.", 2)
    for key in ("base_url", "locale", "output"):
        value = data.get(key)
        if value is not None and not isinstance(value, str):
            raise SiteShotError(f".siteshot.json {key} must be a string.", 2)
    routes = data.get("routes", [])
    if routes is None:
        routes = []
    if not isinstance(routes, list) or any(not isinstance(item, str) for item in routes):
        raise SiteShotError(".siteshot.json routes must be a list of strings.", 2)
    if len(routes) > 2000:
        raise SiteShotError(".siteshot.json routes has more than 2000 entries.", 2)
    dynamic = data.get("dynamic_values", {})
    if dynamic is None:
        dynamic = {}
    if not isinstance(dynamic, dict):
        raise SiteShotError(".siteshot.json dynamic_values must be an object.", 2)
    for key, value in dynamic.items():
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z_][\w-]*", key):
            raise SiteShotError("dynamic_values keys must be identifiers.", 2)
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise SiteShotError(f"dynamic_values.{key} must be a string or integer.", 2)
        text = str(value).strip()
        if not text or len(text) > 120:
            raise SiteShotError(f"dynamic_values.{key} must be a short non-empty value.", 2)
        pieces = text.split("/")
        if text.startswith(("/", "\\")) or ".." in pieces or "\\" in text or "://" in text:
            raise SiteShotError(f"dynamic_values.{key} is not a safe path segment.", 2)
        if any(part in {"", ".", ".."} for part in pieces):
            raise SiteShotError(f"dynamic_values.{key} is not a safe path segment.", 2)
    auth = data.get("auth", {})
    if auth is None:
        auth = {}
    if not isinstance(auth, dict):
        raise SiteShotError(".siteshot.json auth must be an object.", 2)
    extra_auth = sorted(set(auth) - AUTH_CONFIG_KEYS)
    if extra_auth:
        warnings.append("Ignoring unknown auth keys: " + ", ".join(extra_auth))
    for key in ("email", "password", "name", "username"):
        if key in auth and auth[key] is not None and not isinstance(auth[key], str):
            raise SiteShotError(f"auth.{key} must be a string.", 2)
    if isinstance(auth.get("password"), str) and len(auth["password"]) > 256:
        raise SiteShotError("auth.password is too long.", 2)
    return warnings


def _pick_int(
    cli: int | None,
    env_value: str | None,
    config_value: Any,
    default: int,
    *,
    name: str,
    low: int,
    high: int,
) -> int:
    if cli is not None:
        value: Any = cli
        source = f"--{name.replace('_', '-')}"
    elif env_value is not None:
        text = env_value.strip()
        if not text.lstrip("-").isdigit():
            raise SiteShotError(f"SITESHOT_{name.upper()} must be an integer.", 2)
        value = int(text)
        source = f"SITESHOT_{name.upper()}"
    elif config_value is not None:
        if isinstance(config_value, bool) or not isinstance(config_value, int):
            raise SiteShotError(f".siteshot.json {name} must be an integer.", 2)
        value = config_value
        source = f".siteshot.json {name}"
    else:
        return default
    if not low <= int(value) <= high:
        raise SiteShotError(f"{source} must be between {low} and {high}.", 2)
    return int(value)


def _pick_bool(cli: bool | None, env_value: bool | None, config_value: Any, default: bool, name: str) -> bool:
    if cli is not None:
        return bool(cli)
    if env_value is not None:
        return env_value
    if config_value is not None:
        if not isinstance(config_value, bool):
            raise SiteShotError(f".siteshot.json {name} must be true or false.", 2)
        return config_value
    return default


def _pick_text(cli: str | None, env_value: str | None, config_value: Any, name: str, limit: int) -> str | None:
    if cli is not None:
        value: Any = cli
    elif env_value not in (None, ""):
        value = env_value
    elif config_value not in (None, ""):
        if not isinstance(config_value, str):
            raise SiteShotError(f"{name} must be a string.", 2)
        value = config_value
    else:
        return None
    text = str(value).strip()
    if not text:
        return None
    if len(text) > limit:
        raise SiteShotError(f"{name} is too long.", 2)
    return text


def env_flag(env: Mapping[str, str], name: str) -> bool | None:
    if name not in env or env[name] == "":
        return None
    text = env[name].strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    raise SiteShotError(f"{name} must be true or false.", 2)


def merge_settings(
    args: argparse.Namespace,
    config: Mapping[str, Any],
    env: Mapping[str, str],
    root: Path,
) -> Settings:
    auth = config.get("auth") if isinstance(config.get("auth"), dict) else {}
    email = _pick_text(args.email, env.get("SITESHOT_EMAIL"), auth.get("email"), "email", 254)
    password = _pick_text(args.password, env.get("SITESHOT_PASSWORD"), auth.get("password"), "password", 256)
    name = _pick_text(args.name, env.get("SITESHOT_NAME"), auth.get("name"), "name", 120)
    username = _pick_text(None, None, auth.get("username"), "username", 120)
    url_value = _pick_text(args.url, env.get("SITESHOT_URL"), config.get("base_url"), "base_url", 500)
    base_url = canonicalize_user_url(url_value) if url_value else None
    no_auth = _pick_bool(args.no_auth, env_flag(env, "SITESHOT_NO_AUTH"), config.get("no_auth"), False, "no_auth")
    headful = _pick_bool(args.headful, None, config.get("headful"), False, "headful")
    scroll = _pick_bool(
        args.scroll,
        None,
        config.get("scroll") if "scroll" in config else None,
        True,
        "scroll",
    )
    locale = _pick_text(args.locale, None, config.get("locale"), "locale", 32) or "en-US"
    if not LOCALE_RE.fullmatch(locale):
        raise SiteShotError("locale must look like en or en-US.", 2)
    output_name = _pick_text(args.output, None, config.get("output"), "output", 240) or ".siteshot"
    extra = [item for item in (args.route or []) if isinstance(item, str)]
    configured = [item for item in (config.get("routes") or []) if isinstance(item, str)]
    if len(extra) > 2000:
        raise SiteShotError("Too many --route values.", 2)
    dynamic_raw = config.get("dynamic_values") or {}
    dynamic = {str(key): str(value).strip() for key, value in dynamic_raw.items()} if isinstance(dynamic_raw, dict) else {}
    max_routes = _pick_int(args.max_routes, env.get("SITESHOT_MAX_ROUTES"), config.get("max_routes"), 150, name="max_routes", low=1, high=2000)
    return Settings(
        root=root,
        output_name=output_name,
        base_url=base_url,
        extra_routes=extra,
        config_routes=configured,
        dynamic_values=dynamic,
        max_routes=max_routes,
        max_depth=_pick_int(args.max_depth, None, config.get("max_depth"), 4, name="max_depth", low=0, high=20),
        max_source_files=_pick_int(args.max_source_files, None, config.get("max_source_files"), 1200, name="max_source_files", low=1, high=20000),
        max_discovered=_pick_int(
            args.max_discovered,
            None,
            config.get("max_discovered"),
            min(5000, max(600, max_routes * 4)),
            name="max_discovered",
            low=1,
            high=5000,
        ),
        timeout_ms=_pick_int(args.timeout, None, config.get("timeout"), 12000, name="timeout", low=1000, high=120000),
        server_timeout_s=_pick_int(args.server_timeout, None, config.get("server_timeout"), 30, name="server_timeout", low=3, high=180),
        settle_ms=_pick_int(args.settle_ms, None, config.get("settle_ms"), 400, name="settle_ms", low=0, high=10000),
        width=_pick_int(args.width, None, config.get("width"), 1440, name="width", low=320, high=3840),
        height=_pick_int(args.height, None, config.get("height"), 1000, name="height", low=320, high=2160),
        locale=locale,
        headful=headful,
        no_auth=no_auth,
        scroll=scroll,
        email=email,
        password=password,
        name=name,
        username=username,
        user_supplied_credentials=bool(email or password),
    )


def _read_limited(path: Path, limit: int) -> str | None:
    try:
        if not path.is_file() or path.stat().st_size > limit:
            return None
        return path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return None


def _ports_in_text(text: str) -> list[int]:
    found: list[int] = []
    for regex in (PORT_FLAG_RE, PORT_ASSIGN_RE):
        for match in regex.finditer(text):
            port = parse_port(match.group(1))
            if port and port >= 1024:
                found.append(port)
    return found


def _config_ports(root: Path) -> list[int]:
    names = (
        "vite.config.js", "vite.config.ts", "vite.config.mjs", "vite.config.cjs", "vite.config.mts",
        "astro.config.mjs", "astro.config.js", "astro.config.ts",
        "nuxt.config.ts", "nuxt.config.js", "nuxt.config.mjs",
        "svelte.config.js", "svelte.config.ts",
        "next.config.js", "next.config.mjs", "next.config.ts",
        "angular.json",
    )
    found: list[int] = []
    for name in names:
        text = _read_limited(root / name, 200_000)
        if not text:
            continue
        for match in CONFIG_PORT_RE.finditer(text):
            port = parse_port(match.group(1))
            if port and port >= 1024:
                found.append(port)
    return found


def _env_file_ports(root: Path) -> list[int]:
    found: list[int] = []
    for name in (".env", ".env.local", ".env.development", ".env.development.local"):
        text = _read_limited(root / name, 64_000)
        if not text:
            continue
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.startswith("export "):
                stripped = stripped[7:].strip()
            if "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            if key.strip() not in PORT_ENV_KEYS:
                continue
            port = parse_port(value.strip().strip("'\""))
            if port and port >= 1024:
                found.append(port)
    return found


def _framework_from_script(script: str) -> str | None:
    lowered = script.lower()
    if re.search(r"\bnext\b", lowered):
        return "next"
    if re.search(r"\bnuxt\b", lowered):
        return "nuxt"
    if "svelte" in lowered:
        return "sveltekit"
    if re.search(r"\bastro\b", lowered):
        return "astro"
    if "ng serve" in lowered or "angular" in lowered:
        return "angular"
    if re.search(r"\bvite\b", lowered):
        return "vite"
    return None


def _dependency_names(package: Mapping[str, Any]) -> set[str]:
    names: set[str] = set()
    for section in ("dependencies", "devDependencies", "peerDependencies"):
        value = package.get(section)
        if isinstance(value, dict):
            names.update(str(name) for name in value)
    return names


def detect_package_manager(root: Path, package: Mapping[str, Any] | None, which: Callable[[str], str | None]) -> tuple[str | None, str | None]:
    if not package:
        return None, None
    declared = ""
    raw = package.get("packageManager")
    if isinstance(raw, str) and raw.strip():
        declared = raw.split("@", 1)[0].strip().lower()
        if declared not in {"npm", "pnpm", "yarn", "bun"}:
            declared = ""
    lock_order: list[str] = []
    if (root / "pnpm-lock.yaml").exists():
        lock_order.append("pnpm")
    if (root / "yarn.lock").exists():
        lock_order.append("yarn")
    if (root / "bun.lock").exists() or (root / "bun.lockb").exists():
        lock_order.append("bun")
    if (root / "package-lock.json").exists() or (root / "npm-shrinkwrap.json").exists():
        lock_order.append("npm")
    warning = None
    preferred = dedupe([*([declared] if declared else []), *lock_order, "npm", "pnpm", "yarn", "bun"])
    for name in preferred:
        if which(name):
            if declared and name != declared:
                warning = f"packageManager is {declared}, but {name} was used because {declared} is not on PATH."
            return name, warning
    if declared:
        warning = f"packageManager is {declared}, but it is not on PATH."
    return None, warning


def inspect_project(root: Path, *, which: Callable[[str], str | None] = shutil.which) -> ProjectInfo:
    info = ProjectInfo()
    package: dict[str, Any] | None = None
    package_path = root / "package.json"
    if package_path.exists():
        try:
            loaded = json.loads(package_path.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as exc:
            raise SiteShotError(
                f"Invalid JSON in package.json at line {exc.lineno}, column {exc.colno}: {exc.msg}",
                2,
            ) from exc
        except OSError as exc:
            raise SiteShotError(f"Could not read package.json: {exc.strerror or exc}", 2) from exc
        if not isinstance(loaded, dict):
            raise SiteShotError("package.json must contain an object.", 2)
        package = loaded
        info.name = _public_project_name(package.get("name"))
    deps = _dependency_names(package or {})
    if "next" in deps or any((root / name).exists() for name in ("next.config.js", "next.config.mjs", "next.config.ts")):
        info.framework = "next"
    elif "nuxt" in deps or any((root / name).exists() for name in ("nuxt.config.js", "nuxt.config.ts", "nuxt.config.mjs")):
        info.framework = "nuxt"
    elif "@sveltejs/kit" in deps or (root / "svelte.config.js").exists() or (root / "svelte.config.ts").exists():
        info.framework = "sveltekit"
    elif "astro" in deps or any((root / name).exists() for name in ("astro.config.mjs", "astro.config.js", "astro.config.ts")):
        info.framework = "astro"
    elif "@angular/core" in deps or (root / "angular.json").exists():
        info.framework = "angular"
    elif (root / "manage.py").is_file():
        info.framework = "django"
    elif "vite" in deps or any((root / name).exists() for name in ("vite.config.ts", "vite.config.js", "vite.config.mjs")):
        info.framework = "vite"
    elif package is None and any(root.glob("*.html")):
        info.framework = "html"
    scripts = package.get("scripts") if isinstance(package, dict) else None
    if isinstance(scripts, dict) and isinstance(scripts.get("dev"), str) and scripts["dev"].strip():
        info.dev_script = scripts["dev"]
        inferred = _framework_from_script(info.dev_script)
        if info.framework == "unknown" and inferred:
            info.framework = inferred
        manager, warning = detect_package_manager(root, package, which)
        if warning:
            info.warnings.append(warning)
        if manager:
            info.package_manager = manager
            info.dev_command = [manager, "run", "dev"]
    elif (root / "manage.py").is_file() and info.framework in {"django", "unknown"}:
        info.framework = "django"
        info.dev_command = [sys.executable, "manage.py", "runserver", "127.0.0.1:8000", "--noreload"]
        info.declared_ports.append(8000)
    if info.dev_script:
        info.declared_ports.extend(_ports_in_text(info.dev_script))
    info.declared_ports.extend(_config_ports(root))
    info.declared_ports.extend(_env_file_ports(root))
    info.declared_ports = dedupe(info.declared_ports)
    info.default_ports = list(FRAMEWORK_PORTS.get(info.framework, ()))
    return info


def _public_project_name(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    name = value.strip()
    if name.startswith("@") and "/" in name:
        name = name.split("/", 1)[1]
    name = name.strip()
    if len(name) < 3:
        return None
    return name


def _pages_pattern(group: str | None) -> str | None:
    if not group:
        return "/"
    parts = [part for part in group.split("/") if part]
    if not parts:
        return "/"
    if parts[0] == "api" or any(part.startswith("_") for part in parts):
        return None
    if parts[-1] == "index":
        parts = parts[:-1]
    return "/" + "/".join(parts) if parts else "/"


def route_pattern_from_relative(relative: str) -> str | None:
    rel = relative.replace("\\", "/")
    app = re.search(r"(?:^|/)app/(?:(.+)/)?page\.(?:js|jsx|ts|tsx|md|mdx)$", rel)
    if app:
        group = app.group(1)
        if group and (group == "api" or group.startswith("api/") or any(part.startswith("_") for part in group.split("/"))):
            return None
        return "/" + group if group else "/"
    svelte = re.search(r"(?:^|/)routes/(?:(.+)/)?\+page\.(?:js|ts|svelte)$", rel)
    if svelte and not rel.endswith(".server.ts") and not rel.endswith(".server.js"):
        group = svelte.group(1)
        if group and any(part.startswith("_") for part in group.split("/")):
            return None
        return "/" + group if group else "/"
    vue = re.search(r"(?:^|/)pages/(.+)\.vue$", rel)
    if vue:
        return _pages_pattern(vue.group(1))
    astro = re.search(r"(?:^|/)pages/(.+)\.astro$", rel)
    if astro:
        return _pages_pattern(astro.group(1))
    pages = re.search(r"(?:^|/)pages/(.+)\.(?:js|jsx|ts|tsx|md|mdx)$", rel)
    if pages:
        return _pages_pattern(pages.group(1))
    return None


def html_route(relative: str, framework: str) -> str | None:
    path = PurePosixPath(relative.replace("\\", "/"))
    if path.suffix.lower() not in {".html", ".htm"}:
        return None
    parts = list(path.parts)
    spa = framework in {"next", "nuxt", "sveltekit", "astro", "vite", "angular", "django"}
    if spa:
        if "public" in parts:
            parts = parts[parts.index("public") + 1:]
        elif "static" in parts:
            parts = parts[parts.index("static") + 1:]
        elif framework == "vite" and relative.replace("\\", "/") == "index.html":
            return "/"
        else:
            return None
    if parts and parts[-1].lower() in {"index.html", "index.htm"}:
        parts = parts[:-1]
    if not parts:
        return "/"
    return "/" + "/".join(parts)


def discover_routes(
    root: Path,
    *,
    origin: str,
    framework: str,
    dynamic_values: Mapping[str, str],
    explicit_routes: Iterable[str],
    max_source_files: int,
    max_stored: int,
    max_depth: int,
    skip_dir_names: Iterable[str] = (),
) -> tuple[RouteSet, int]:
    routes = RouteSet(max_stored=max(1, max_stored), max_depth=max_depth)
    explicit = list(explicit_routes)
    for raw in explicit:
        _add_candidate(routes, raw, origin, dynamic_values, depth=0, keep_query=True, priority=True)
    _add_candidate(routes, "/", origin, dynamic_values, depth=0, keep_query=False, priority=False)
    scanned = 0
    skip_names = set(SKIP_DIRS)
    skip_names.update(skip_dir_names)
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(dirpath)
        try:
            depth = len(current.resolve().relative_to(root.resolve()).parts)
        except (OSError, ValueError):
            dirnames[:] = []
            continue
        if depth >= 20:
            dirnames[:] = []
            continue
        kept: list[str] = []
        for name in sorted(dirnames):
            child = current / name
            if name in skip_names or name.startswith("."):
                continue
            if (child / "pyvenv.cfg").exists():
                continue
            kept.append(name)
        dirnames[:] = kept
        for name in sorted(filenames):
            if scanned >= max_source_files:
                return routes, scanned
            if name in SKIP_FILES or name.endswith(".min.js"):
                continue
            path = current / name
            if not is_within(root, path):
                continue
            suffix = path.suffix.lower()
            useful = suffix in FRONTEND_SUFFIXES or suffix == ".py" or name == "urls.py"
            if not useful:
                continue
            try:
                if path.stat().st_size > 1_000_000:
                    continue
            except OSError:
                continue
            scanned += 1
            relative = path.relative_to(root).as_posix()
            pattern = route_pattern_from_relative(relative)
            if pattern:
                _add_candidate(routes, pattern, origin, dynamic_values, depth=0, keep_query=False, priority=False)
            html_pattern = html_route(relative, framework)
            if html_pattern:
                _add_candidate(routes, html_pattern, origin, dynamic_values, depth=0, keep_query=False, priority=False)
            if suffix in {".html", ".htm"} and html_pattern is None:
                continue
            text = _read_limited(path, 1_000_000)
            if not text or "\x00" in text[:1024]:
                continue
            if name == "urls.py" or suffix == ".py":
                _scan_python(routes, text, origin, dynamic_values)
            if suffix in FRONTEND_SUFFIXES:
                _scan_frontend(routes, text, origin, dynamic_values)
    return routes, scanned


def _scan_frontend(routes: RouteSet, text: str, origin: str, dynamic_values: Mapping[str, str]) -> None:
    found = 0
    for regex in (HREF_RE, PATH_RE):
        for match in regex.finditer(text):
            if found >= 80:
                return
            value = match.group("v").strip()
            if not value or "${" in value or value.startswith(("http://", "https://", "mailto:", "tel:", "javascript:")):
                if value.startswith(("http://", "https://")):
                    _add_candidate(routes, value, origin, dynamic_values, depth=0, keep_query=False, priority=False)
                    found += 1
                continue
            _add_candidate(routes, value, origin, dynamic_values, depth=0, keep_query=False, priority=False)
            found += 1


def _scan_python(routes: RouteSet, text: str, origin: str, dynamic_values: Mapping[str, str]) -> None:
    found = 0
    for match in RE_PATH_RE.finditer(text):
        routes.add_unresolved(match.group(1), "regular-expression route was not expanded")
    for match in DJANGO_PATH_RE.finditer(text):
        if found >= 80:
            return
        raw = match.group("p")
        if "(?" in raw or raw.startswith("^"):
            routes.add_unresolved(raw, "regular-expression route was not expanded")
            continue
        pattern = raw if raw.startswith("/") else "/" + raw
        _add_candidate(routes, pattern, origin, dynamic_values, depth=0, keep_query=False, priority=False)
        found += 1
    for match in DECORATOR_RE.finditer(text):
        if found >= 120:
            return
        raw = match.group(1)
        if not raw.startswith("/"):
            continue
        _add_candidate(routes, raw, origin, dynamic_values, depth=0, keep_query=False, priority=False)
        found += 1


def _add_candidate(
    routes: RouteSet,
    raw: str,
    origin: str,
    dynamic_values: Mapping[str, str],
    *,
    depth: int,
    keep_query: bool,
    priority: bool,
) -> None:
    text = raw.strip()
    if not text or len(text) > 2048:
        return
    path_part = text.split("?", 1)[0].split("#", 1)[0]
    remainder = text[len(path_part):]
    if looks_like_pattern(path_part):
        concrete, reason = resolve_pattern(path_part, dynamic_values)
        if concrete is None:
            if reason and reason.startswith("private"):
                routes.add_ignored(path_part, reason)
            elif reason and "intercepting" in reason:
                routes.add_ignored(path_part, reason)
            else:
                routes.add_unresolved(path_part, reason or "dynamic route could not be resolved")
            return
        text = concrete + remainder
    route = normalize_route(text, origin, keep_query=keep_query)
    if route is not None:
        routes.add(route, depth, priority=priority)
    elif priority:
        routes.add_ignored(text, "route was not a same-origin page URL")


def probe_http(url: str, timeout: float = 1.2) -> Probe | None:
    parsed = urlparse(url)
    host = parsed.hostname
    if not host:
        return None
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    path = parsed.path or "/"
    sock: socket.socket | ssl.SSLSocket | None = None
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.settimeout(timeout)
        if parsed.scheme == "https":
            context = ssl._create_unverified_context()
            sock = context.wrap_socket(sock, server_hostname=host)
        host_header = f"[{host}]" if ":" in host else host
        if parsed.port:
            host_header += f":{parsed.port}"
        request = (
            f"GET {path or '/'} HTTP/1.1\r\n"
            f"Host: {host_header}\r\n"
            f"User-Agent: SiteShot/{VERSION}\r\n"
            "Accept: text/html\r\n"
            "Connection: close\r\n\r\n"
        )
        sock.sendall(request.encode("ascii", "ignore"))
        chunks: list[bytes] = []
        total = 0
        while total < 48_000:
            try:
                block = sock.recv(8192)
            except socket.timeout:
                break
            if not block:
                break
            chunks.append(block)
            total += len(block)
    except OSError:
        return None
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
    raw = b"".join(chunks)
    header_end = raw.find(b"\r\n\r\n")
    if header_end < 0:
        return None
    head = raw[:header_end].decode("iso-8859-1", "replace")
    body = raw[header_end + 4:].decode("utf-8", "replace")
    lines = head.split("\r\n")
    parts = lines[0].split()
    if len(parts) < 2 or not parts[1].isdigit():
        return None
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        headers[key.strip().lower()] = value.strip()
    return Probe(url=url, status=int(parts[1]), body_sample=body[:20000], headers=headers, port=port)


def probe_loopback(port: int, timeout: float = 1.2) -> Probe | None:
    """Find which loopback address actually accepts HTTP.

    A dev server that prints ``http://localhost:5173/`` may be bound only to
    ``::1``. Connecting to ``127.0.0.1`` then hangs instead of refusing, and
    waiting on that address alone burns the whole server timeout.
    """
    if not 1 <= int(port) <= 65535:
        return None
    targets = (
        f"http://127.0.0.1:{port}/",
        f"http://[::1]:{port}/",
    )
    found: list[Probe] = []
    lock = threading.Lock()

    def attempt(url: str) -> None:
        probe = probe_http(url, timeout=timeout)
        if probe is not None:
            with lock:
                found.append(probe)

    threads = [threading.Thread(target=attempt, args=(url,), daemon=True) for url in targets]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + timeout + 0.25
    while time.monotonic() < deadline:
        with lock:
            ready = [probe for probe in found if probe.status < 400]
        if ready:
            return _prefer_loopback(ready)
        if all(not thread.is_alive() for thread in threads):
            break
        time.sleep(0.05)
    for thread in threads:
        thread.join(timeout=0.2)
    with lock:
        usable = [probe for probe in found if probe.status < 500]
    if not usable:
        return None
    return _prefer_loopback(usable)


def _prefer_loopback(probes: list[Probe]) -> Probe:
    for probe in probes:
        if "127.0.0.1" in probe.url:
            return probe
    return probes[0]


def tcp_open(port: int, timeout: float = 0.2) -> bool:
    for host in ("127.0.0.1", "::1"):
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            continue
    return False


def snapshot_open_ports(ports: Iterable[int]) -> set[int]:
    return {port for port in dedupe(ports) if tcp_open(port)}


class OutputBuffer:
    def __init__(self) -> None:
        self._lines: list[str] = []
        self._lock = threading.Lock()

    def append(self, line: str) -> None:
        cleaned = ANSI_RE.sub("", line).rstrip()
        with self._lock:
            self._lines.append(cleaned)
            if len(self._lines) > 500:
                del self._lines[:250]

    def text(self) -> str:
        with self._lock:
            return "\n".join(self._lines)


def _drain_pipe(pipe: Any, buffer: OutputBuffer) -> None:
    try:
        for line in iter(pipe.readline, ""):
            if not line:
                break
            buffer.append(line)
    except (OSError, ValueError):
        return
    finally:
        try:
            pipe.close()
        except OSError:
            pass


def windows_job_for(pid: int) -> int | None:
    """Keep the handle. Closing it kills the job when KILL_ON_JOB_CLOSE is set."""
    if os.name != "nt":
        return None
    try:
        kernel32 = ctypes_kernel32()
    except OSError:
        return None
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return None
    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = 0x00002000
    if not kernel32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
        kernel32.CloseHandle(job)
        return None
    process = kernel32.OpenProcess(0x0101, False, pid)
    if not process:
        kernel32.CloseHandle(job)
        return None
    assigned = kernel32.AssignProcessToJobObject(job, process)
    kernel32.CloseHandle(process)
    if not assigned:
        kernel32.CloseHandle(job)
        return None
    return int(job)


def close_windows_handle(handle: int | None) -> None:
    if not handle or os.name != "nt":
        return
    try:
        ctypes_kernel32().CloseHandle(handle)
    except OSError:
        return


def ctypes_kernel32() -> Any:
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    kernel32.CreateJobObjectW.restype = ctypes.c_void_p
    kernel32.SetInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = wintypes.BOOL
    return kernel32


def ctypes_structure() -> type:
    import ctypes
    return ctypes.Structure


def ctypes_c_int64() -> Any:
    import ctypes
    return ctypes.c_int64


def ctypes_c_uint64() -> Any:
    import ctypes
    return ctypes.c_uint64


def ctypes_size() -> Any:
    import ctypes
    return ctypes.c_size_t


def ctypes_dword() -> Any:
    import ctypes
    from ctypes import wintypes
    return wintypes.DWORD


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes_structure()):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes_c_int64()),
        ("PerJobUserTimeLimit", ctypes_c_int64()),
        ("LimitFlags", ctypes_dword()),
        ("MinimumWorkingSetSize", ctypes_size()),
        ("MaximumWorkingSetSize", ctypes_size()),
        ("ActiveProcessLimit", ctypes_dword()),
        ("Affinity", ctypes_size()),
        ("PriorityClass", ctypes_dword()),
        ("SchedulingClass", ctypes_dword()),
    ]


class IO_COUNTERS(ctypes_structure()):
    _fields_ = [
        ("ReadOperationCount", ctypes_c_uint64()),
        ("WriteOperationCount", ctypes_c_uint64()),
        ("OtherOperationCount", ctypes_c_uint64()),
        ("ReadTransferCount", ctypes_c_uint64()),
        ("WriteTransferCount", ctypes_c_uint64()),
        ("OtherTransferCount", ctypes_c_uint64()),
    ]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes_structure()):
    _fields_ = [
        ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes_size()),
        ("JobMemoryLimit", ctypes_size()),
        ("PeakProcessMemoryUsed", ctypes_size()),
        ("PeakJobMemoryUsed", ctypes_size()),
    ]


# windows_job_for calls ctypes.byref. The structures above import ctypes too.
import ctypes  # noqa: E402


def kill_process_tree(process: subprocess.Popen[Any]) -> None:
    """Stop a server SiteShot started, including children. Never targets an adopted server."""
    if process.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
        try:
            process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            return
        return
    try:
        group = os.getpgid(process.pid)
    except OSError:
        try:
            process.kill()
        except OSError:
            return
        return
    own_group = os.getpgrp()
    if group <= 1 or group == own_group:
        _signal_process(process, signal.SIGTERM)
        return
    try:
        os.killpg(group, signal.SIGTERM)
    except OSError:
        _signal_process(process, signal.SIGTERM)
        return
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(group, signal.SIGKILL)
        except OSError:
            _signal_process(process, signal.SIGKILL)
        try:
            process.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            return


def _signal_process(process: subprocess.Popen[Any], sig: int) -> None:
    try:
        process.send_signal(sig)
        process.wait(timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        try:
            process.kill()
        except OSError:
            return


def allocate_run_directory(output_root: Path) -> Path:
    runs = output_root / "runs"
    try:
        runs.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SiteShotError(f"Could not create {runs}: {exc.strerror or exc}", 1) from exc
    stamp = time.strftime("%Y%m%d-%H%M%S")
    millis = int((time.time() % 1) * 1000)
    base = f"{stamp}-{millis:03d}"
    for number in range(0, 1000):
        name = base if number == 0 else f"{base}-{number:02d}"
        candidate = runs / name
        try:
            candidate.mkdir(parents=False, exist_ok=False)
        except FileExistsError:
            continue
        pointer = output_root / "latest.txt"
        relative = candidate.relative_to(output_root).as_posix()
        try:
            atomic_write(pointer, relative + "\n")
        except OSError:
            pass
        return candidate
    raise SiteShotError("Could not allocate a unique run directory.", 1)


def redirect_count(response: Any) -> int:
    if response is None:
        return 0
    count = 0
    request = getattr(response, "request", None)
    seen: set[int] = set()
    while request is not None and getattr(request, "redirected_from", None) is not None and count < 10:
        request = request.redirected_from
        marker = id(request)
        if marker in seen:
            break
        seen.add(marker)
        count += 1
    return count


def public_url(url: str, secrets: Iterable[str]) -> str:
    parsed = urlparse(url)
    if parsed.username or parsed.password:
        host = parsed.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        netloc = host if parsed.port is None else f"{host}:{parsed.port}"
        parsed = parsed._replace(netloc=netloc)
        url = urlunparse(parsed)
    return scrub(url, secrets)


def status_for(http_status: int | None, *, redirected_auth: bool, soft_404: bool) -> str:
    if redirected_auth and (http_status is None or http_status < 400):
        return "redirect-to-auth"
    if http_status == 404:
        return "http-404"
    if http_status is not None and 500 <= http_status <= 599:
        return "http-5xx"
    if http_status is not None and 400 <= http_status <= 499:
        return f"http-{http_status}"
    if soft_404:
        return "soft-404"
    return "ok"


def build_manifest(
    *,
    started_at: str,
    finished_at: str,
    elapsed_ms: int,
    root: Path,
    run_dir: Path,
    origin: str,
    base_url: str,
    project: ProjectInfo,
    settings: Settings,
    server_started: bool,
    server_reused: bool,
    server_command: list[str] | None,
    auth: AuthResult,
    routes: RouteSet,
    results: list[Shot],
    skipped: list[dict[str, str]],
    source_files: int,
    playwright_version: str | None,
    fatal_error: str,
    secrets: Iterable[str],
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "tool": TOOL_NAME,
        "version": VERSION,
        "generated_at": finished_at,
        "started_at": started_at,
        "elapsed_ms": elapsed_ms,
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "python": platform.python_version(),
        },
        "playwright": playwright_version,
        "project_root": str(root),
        "project_name": project.name,
        "run_directory": str(run_dir),
        "base_url": base_url,
        "origin": origin,
        "server": {
            "started_by_siteshot": server_started,
            "reused_existing": server_reused,
            "command": server_command or [],
            "framework": project.framework,
            "package_manager": project.package_manager,
        },
        "crawl": {
            "max_routes": settings.max_routes,
            "max_depth": settings.max_depth,
            "max_source_files": settings.max_source_files,
            "max_discovered": settings.max_discovered,
            "timeout_ms": settings.timeout_ms,
            "settle_ms": settings.settle_ms,
            "scroll": settings.scroll,
            "headful": settings.headful,
            "viewport": {"width": settings.width, "height": settings.height},
            "locale": settings.locale,
            "source_files_scanned": source_files,
            "dropped_by_depth": routes.dropped_depth,
            "dropped_by_cap": routes.dropped_cap,
        },
        "auth": {
            "attempted": auth.attempted,
            "succeeded": auth.succeeded,
            "likely_authenticated": auth.succeeded,
            "mode": auth.mode,
            "reason": auth.reason,
            "notes": auth.notes,
        },
        "routes_discovered": sorted(routes.routes),
        "routes_attempted": [shot.requested_path for shot in results],
        "routes_skipped": skipped,
        "ignored_patterns": routes.ignored,
        "unresolved_dynamic_routes": routes.unresolved,
        "results": [asdict(shot) for shot in results],
        "fatal_error": fatal_error,
    }
    return scrub_obj(payload, secrets)


def render_report_html(payload: Mapping[str, Any]) -> str:
    results = list(payload.get("results") or [])
    counts: dict[str, int] = {}
    for item in results:
        status = str(item.get("status") or "pending")
        counts[status] = counts.get(status, 0) + 1
    count_text = " · ".join(f"{html.escape(key)} {value}" for key, value in sorted(counts.items()))
    cards: list[str] = []
    for item in results:
        image = str(item.get("file") or "")
        image_html = (
            f'<a href="{html.escape(image)}"><img loading="lazy" src="{html.escape(image)}" alt=""></a>'
            if image else '<div class="missing">No screenshot</div>'
        )
        notes = item.get("notes") or []
        console = item.get("console_errors") or []
        extra = []
        if item.get("redirected_to_auth"):
            extra.append('<div class="warn">Redirected to auth</div>')
        if item.get("redirect_count"):
            extra.append(f'<div class="muted">Redirects: {int(item["redirect_count"])}</div>')
        if item.get("http_status"):
            extra.append(f'<div class="muted">HTTP {int(item["http_status"])}</div>')
        if item.get("error"):
            extra.append(f'<pre>{html.escape(str(item["error"]))}</pre>')
        for note in list(notes)[:4]:
            extra.append(f'<div class="muted">{html.escape(str(note))}</div>')
        for line in list(console)[:3]:
            extra.append(f'<pre>{html.escape(str(line))}</pre>')
        status = html.escape(str(item.get("status") or ""))
        cards.append(
            "<article>"
            f"{image_html}"
            '<div class="meta">'
            f'<h2>{html.escape(str(item.get("requested_path") or ""))}</h2>'
            f'<div class="status {status_class(str(item.get("status") or ""))}">{status}</div>'
            f'<div class="url">{html.escape(str(item.get("final_url") or ""))}</div>'
            f'{"".join(extra)}'
            "</div></article>"
        )
    auth = payload.get("auth") or {}
    unresolved = payload.get("unresolved_dynamic_routes") or []
    skipped = payload.get("routes_skipped") or []
    side = []
    if payload.get("fatal_error"):
        side.append(f'<p class="warn">{html.escape(str(payload["fatal_error"]))}</p>')
    if unresolved:
        items = "".join(
            f'<li>{html.escape(str(item.get("pattern")))} — {html.escape(str(item.get("reason")))}</li>'
            for item in unresolved[:30]
        )
        side.append(f"<h2>Unresolved dynamic routes</h2><ul>{items}</ul>")
    if skipped:
        items = "".join(
            f'<li>{html.escape(str(item.get("route")))} — {html.escape(str(item.get("reason")))}</li>'
            for item in skipped[:30]
        )
        side.append(f"<h2>Not captured</h2><ul>{items}</ul>")
    reason = html.escape(str(auth.get("reason") or ""))
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SiteShot report</title>
<style>
:root {{ color-scheme: light dark; font-family: ui-sans-serif, system-ui, sans-serif; }}
body {{ margin: 0; padding: 28px; background: #111; color: #eee; }}
header, .side {{ max-width: 1100px; margin: 0 auto 20px; }}
.muted, .url {{ color: #aaa; }}
.grid {{ max-width: 1600px; margin: auto; display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 16px; }}
article {{ background: #191919; border: 1px solid #2c2c2c; border-radius: 14px; overflow: hidden; }}
img {{ width: 100%; height: 260px; object-fit: cover; object-position: top; display: block; background: white; }}
.meta {{ padding: 14px; }}
h1, h2 {{ margin: 0 0 8px; }}
h2 {{ font-size: 16px; }}
.status {{ display: inline-block; padding: 3px 7px; border-radius: 999px; background: #292929; font-size: 12px; margin-bottom: 8px; }}
.ok {{ background: #1d3b2a; }}
.bad {{ background: #4a2222; }}
.warn {{ color: #ffca73; }}
pre {{ white-space: pre-wrap; color: #ffb4b4; font-size: 12px; }}
.missing {{ height: 260px; display: grid; place-items: center; color: #888; }}
ul {{ padding-left: 18px; }}
</style>
</head>
<body>
<header>
  <h1>SiteShot</h1>
  <div class="muted">{html.escape(str(payload.get("base_url") or ""))} · {len(results)} captured · auth {str(bool(auth.get("succeeded"))).lower()}</div>
  <div class="muted">{count_text}</div>
  <div class="muted">{reason}</div>
</header>
<section class="side">{"".join(side)}</section>
<section class="grid">{"".join(cards)}</section>
</body>
</html>
"""


def status_class(status: str) -> str:
    if status in {"ok", "duplicate"}:
        return "ok"
    if status.startswith("http-4") or status.startswith("http-5") or status in {"timeout", "browser-error", "screenshot-failed"}:
        return "bad"
    return ""


def playwright_version() -> str | None:
    try:
        from importlib.metadata import version
        return version("playwright")
    except Exception:
        return None


def load_playwright() -> tuple[Any, type[Exception], type[Exception]]:
    try:
        from playwright.async_api import Error as PlaywrightError
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise SiteShotError(
            "Missing dependency: playwright\n\n"
            "Install it with:\n"
            "  pip install playwright\n"
            "  playwright install chromium",
            2,
        ) from exc
    return async_playwright, PlaywrightTimeoutError, PlaywrightError


class SiteShot:
    def __init__(self, settings: Settings, log: Log | None = None) -> None:
        self.settings = settings
        self.log = log or Log()
        self.root = settings.root
        self.run_dir: Path | None = None
        self.shot_dir: Path | None = None
        self.project = ProjectInfo()
        self.routes = RouteSet(max_stored=settings.max_discovered, max_depth=settings.max_depth)
        self.results: list[Shot] = []
        self.auth = AuthResult()
        self.origin = ""
        self.base_path = ""
        self.base_url = settings.base_url or ""
        self.server_started = False
        self.server_reused = False
        self.server_command: list[str] | None = None
        self.server_proc: subprocess.Popen[Any] | None = None
        self.server_buffer: OutputBuffer | None = None
        self._job_handle: int | None = None
        self.source_files = 0
        self.started_at = ""
        self.fatal_error = ""
        self._names: set[str] = set()
        self._captured_final: dict[str, str] = {}
        self._console: list[str] = []
        self._page_crashed = False
        if settings.password:
            self.log.add_secret(settings.password)
        if settings.email:
            self.log.add_secret(settings.email)

    def prepare(self) -> None:
        if not self.root.is_dir():
            raise SiteShotError(f"Project root does not exist: {self.root}", 2)
        output = Path(self.settings.output_name)
        if not output.is_absolute():
            output = self.root / output
        output_root = output.resolve()
        if output_root == self.root.resolve():
            raise SiteShotError("Refusing to use the project root as the output directory.", 2)
        self.project = inspect_project(self.root)
        for warning in self.project.warnings:
            self.log.emit("warn", warning)
        self.log.emit("env", " | ".join(
            part for part in (platform.system(), platform.release(), platform.machine(), f"Python {platform.python_version()}") if part
        ))
        manager = self.project.package_manager or "none"
        self.log.emit("project", f"framework={self.project.framework} package_manager={manager}")
        self.run_dir = allocate_run_directory(output_root)
        self.shot_dir = self.run_dir / "screenshots"
        self.shot_dir.mkdir(parents=True, exist_ok=True)
        self.started_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        self.log.emit("project", f"run {self.run_dir}")

    async def resolve_base_url(self) -> None:
        if self.settings.base_url:
            self.base_url = self.settings.base_url
            self.origin, self.base_path = split_base(self.base_url)
            probe = await asyncio.to_thread(probe_http, self.origin + "/")
            if probe is None:
                self.log.emit("warn", f"No HTTP response yet from {self.origin}; Playwright will try the URL.")
            else:
                self.log.emit("server", f"using explicit {self.base_url}")
            self.server_reused = True
            return
        self.project = self.project if self.project.framework or self.project.dev_command else inspect_project(self.root)
        probes = await asyncio.to_thread(self._probe_candidates, self._reuse_ports())
        chosen, reason = choose_existing_server(self.project, probes)
        if chosen:
            self.base_url = chosen
            self.origin, self.base_path = split_base(chosen)
            self.server_reused = True
            self.log.emit("server", f"reusing {chosen} ({reason})")
            return
        if "multiple" in reason or "does not look" in reason:
            self.log.emit("warn", reason)
        if not self.project.dev_command:
            raise SiteShotError(self._startup_help(reason), 1)
        await self._start_server()

    def _reuse_ports(self) -> list[int]:
        return dedupe([*self.project.declared_ports, *self.project.default_ports])

    def _snapshot_ports(self) -> list[int]:
        return dedupe([*SNAPSHOT_PORTS, *self._reuse_ports()])

    def _probe_candidates(self, ports: Iterable[int]) -> list[Probe]:
        probes: list[Probe] = []
        for port in ports:
            probe = probe_loopback(port)
            if probe is not None:
                probes.append(probe)
        return probes

    def _startup_help(self, reason: str) -> str:
        script = Path(__file__).name
        detail = reason or "No dev server was detected."
        if self.project.framework != "unknown" and not self.project.dev_script and self.project.framework != "django":
            detail += f" Detected {self.project.framework}, but there is no package.json \"dev\" script."
        return (
            f"{detail}\n"
            "Start the project yourself, then rerun with an explicit URL:\n"
            f"  python {script} --url http://127.0.0.1:PORT"
        )

    async def _start_server(self) -> None:
        command = list(self.project.dev_command or [])
        if not command:
            raise SiteShotError(self._startup_help("No safe dev command was detected."), 1)
        resolved = command[0]
        if command[0] != sys.executable:
            found = shutil.which(command[0])
            if not found:
                raise SiteShotError(f"'{command[0]}' is not on PATH. Pass --url instead.", 1)
            resolved = prefer_windows_launcher(found) if os.name == "nt" else found
        pre_open = await asyncio.to_thread(snapshot_open_ports, self._snapshot_ports())
        argv = build_spawn_argv(command, resolved, os.name, os.environ.get("COMSPEC", "cmd.exe"))
        env = os.environ.copy()
        env.pop("SITESHOT_PASSWORD", None)
        self.log.emit("server", "starting " + " ".join(command))
        popen_kwargs: dict[str, Any] = {
            "cwd": str(self.root),
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "bufsize": 1,
            "env": env,
        }
        if os.name == "nt":
            flags = subprocess.CREATE_NEW_PROCESS_GROUP
            create_no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            popen_kwargs["creationflags"] = flags | create_no_window
        else:
            popen_kwargs["start_new_session"] = True
        try:
            process = subprocess.Popen(argv, **popen_kwargs)
        except OSError as exc:
            raise SiteShotError(f"Could not start the dev server: {exc.strerror or exc}", 1) from exc
        self.server_proc = process
        self.server_started = True
        self.server_command = command
        self.server_reused = False
        self._job_handle = windows_job_for(process.pid)
        buffer = OutputBuffer()
        self.server_buffer = buffer
        if process.stdout is not None:
            thread = threading.Thread(target=_drain_pipe, args=(process.stdout, buffer), daemon=True)
            thread.start()
        deadline = time.monotonic() + self.settings.server_timeout_s
        while time.monotonic() < deadline:
            if process.poll() is not None:
                tail = self._server_tail()
                raise SiteShotError(
                    f"Dev server exited with code {process.returncode}.\n{tail}".rstrip(),
                    1,
                )
            now_open = await asyncio.to_thread(snapshot_open_ports, self._snapshot_ports())
            chosen = select_started_endpoint(buffer.text(), pre_open, now_open)
            if chosen:
                probe = await asyncio.to_thread(probe_loopback, port_of(chosen))
                if probe is not None:
                    self.base_url = probe.url.rstrip("/")
                    self.origin, self.base_path = split_base(self.base_url)
                    self.log.emit("server", f"ready at {self.base_url}")
                    return
            await asyncio.sleep(0.35)
        tail = self._server_tail()
        seen = urls_from_dev_log(buffer.text())
        hint = ""
        if seen:
            hint = "Saw " + ", ".join(seen[-3:]) + " in the log, but neither 127.0.0.1 nor ::1 returned a page.\n"
        raise SiteShotError(
            "Dev server stayed up, but SiteShot could not find its local URL.\n"
            f"{hint}{tail}\n"
            "Pass --url http://127.0.0.1:PORT if it uses a custom port.".rstrip(),
            1,
        )

    def _server_tail(self) -> str:
        if not self.server_buffer:
            return ""
        lines = [self.log.scrub(line) for line in self.server_buffer.text().splitlines() if line.strip()]
        if not lines:
            return ""
        return "Last server output:\n" + "\n".join(lines[-20:])

    def stop_server(self) -> None:
        process = self.server_proc
        self.server_proc = None
        if process is None:
            close_windows_handle(self._job_handle)
            self._job_handle = None
            return
        if not self.server_started:
            close_windows_handle(self._job_handle)
            self._job_handle = None
            return
        try:
            kill_process_tree(process)
        finally:
            close_windows_handle(self._job_handle)
            self._job_handle = None
            if process.stdout is not None:
                try:
                    process.stdout.close()
                except OSError:
                    pass

    def discover(self) -> None:
        if not self.origin:
            raise SiteShotError("No base URL was resolved.", 1)
        explicit = [*self.settings.config_routes, *self.settings.extra_routes]
        output_name = Path(self.settings.output_name).name
        routes, scanned = discover_routes(
            self.root,
            origin=self.origin,
            framework=self.project.framework,
            dynamic_values=self.settings.dynamic_values,
            explicit_routes=explicit,
            max_source_files=self.settings.max_source_files,
            max_stored=max(self.settings.max_discovered, len(explicit), self.settings.max_routes),
            max_depth=self.settings.max_depth,
            skip_dir_names=[output_name],
        )
        self.routes = routes
        self.source_files = scanned
        self.log.emit("routes", f"{len(routes.routes)} seed route(s) from the project")
        for item in routes.unresolved[:8]:
            self.log.emit("skip", f"{item['pattern']} — {item['reason']}")
        if len(routes.unresolved) > 8:
            self.log.emit("skip", f"{len(routes.unresolved) - 8} more unresolved dynamic route(s)")
        if routes.unresolved:
            self.log.emit("routes", "Set dynamic_values in .siteshot.json to supply example ids.")

    async def run(self) -> int:
        try:
            self.prepare()
            await self.resolve_base_url()
            self.discover()
            await self.crawl()
        except Exception as exc:
            self.fatal_error = self.log.scrub(str(exc))
            raise
        finally:
            self.stop_server()
            if self.run_dir is not None:
                try:
                    self.write_reports()
                except Exception as exc:
                    self.log.emit("error", f"Could not write the report: {exc}")
        return 0 if any(shot.file for shot in self.results) else 1

    async def crawl(self) -> None:
        async_playwright, timeout_error, playwright_error = load_playwright()
        self._timeout_error = timeout_error
        self._playwright_error = playwright_error
        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=not self.settings.headful)
                try:
                    context = await self._new_context(browser)
                    try:
                        page = await self._new_page(context)
                        await self._preload(page)
                        await self.attempt_auth(page, context)
                        await self._capture_all(context, page)
                    finally:
                        await context.close()
                finally:
                    await browser.close()
        except self._playwright_error as exc:
            message = str(exc)
            if "Executable doesn't exist" in message or "Please run the following command" in message:
                raise SiteShotError(
                    "Chromium is not installed for Playwright. Run:\n  playwright install chromium",
                    2,
                ) from exc
            raise

    async def _new_context(self, browser: Any) -> Any:
        kwargs: dict[str, Any] = {
            "viewport": {"width": self.settings.width, "height": self.settings.height},
            "device_scale_factor": 1,
            "ignore_https_errors": True,
            "locale": self.settings.locale,
            "service_workers": "block",
        }
        try:
            context = await browser.new_context(**kwargs)
        except TypeError:
            kwargs.pop("service_workers", None)
            context = await browser.new_context(**kwargs)
        context.set_default_navigation_timeout(self.settings.timeout_ms)
        context.set_default_timeout(min(5000, self.settings.timeout_ms))
        await context.add_init_script(ANIMATION_JS)
        return context

    async def _discard_page(self, page: Any) -> None:
        self._page_crashed = False
        if page is None:
            return None
        try:
            await page.close()
        except Exception:
            return None
        return None

    async def _new_page(self, context: Any) -> Any:
        page = await context.new_page()
        await page.emulate_media(reduced_motion="reduce")
        self._console = []
        self._page_crashed = False

        def on_console(message: Any) -> None:
            if getattr(message, "type", "") != "error" or len(self._console) >= 15:
                return
            self._console.append(self.log.scrub(str(getattr(message, "text", "")))[:300])

        def on_page_error(error: Any) -> None:
            if len(self._console) >= 15:
                return
            self._console.append("pageerror: " + self.log.scrub(str(error))[:300])

        def on_crash(_: Any) -> None:
            self._page_crashed = True

        def on_dialog(dialog: Any) -> None:
            asyncio.create_task(dialog.dismiss())

        def on_download(download: Any) -> None:
            asyncio.create_task(download.cancel())

        page.on("console", on_console)
        page.on("pageerror", on_page_error)
        page.on("crash", on_crash)
        page.on("dialog", on_dialog)
        page.on("download", on_download)
        return page

    async def _preload(self, page: Any) -> None:
        try:
            await page.goto(navigate_url(self.origin, self.base_path, Route("/")), wait_until="domcontentloaded", timeout=self.settings.timeout_ms)
            await self._settle(page, scroll=False)
            await self._extract_links(page, depth=1)
        except Exception as exc:
            self.log.emit("warn", "The home page did not render before crawling: " + self.log.scrub(str(exc))[:240])

    async def _capture_all(self, context: Any, page: Any) -> None:
        completed: set[str] = set()
        while len(completed) < self.settings.max_routes:
            pending = self.routes.pending(completed)
            if not pending:
                break
            route = pending[0]
            completed.add(route.key())
            try:
                if page is None or page.is_closed() or self._page_crashed:
                    page = await self._new_page(context)
                shot = await self.capture_one(page, route)
                if shot.status in {"browser-error", "timeout"} or self._page_crashed:
                    # A refused connection can leave the page on chrome-error://.
                    # The next route needs a new page or its navigation is cancelled.
                    page = await self._discard_page(page)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                shot = Shot(
                    requested_path=route.key(),
                    status="browser-error",
                    error=self.log.scrub(str(exc))[:500],
                )
                self.log.emit("error", f"{route.key()} {shot.error}")
                page = await self._discard_page(page)
            self.results.append(shot)
        unseen = [
            {"route": key, "reason": "not captured because the route limit was reached"}
            for key in sorted(self.routes.routes)
            if key not in completed
        ]
        self._skipped = unseen
        if unseen:
            self.log.emit("warn", f"Route limit reached ({self.settings.max_routes}); {len(unseen)} route(s) were not captured.")

    async def capture_one(self, page: Any, route: Route) -> Shot:
        started = time.monotonic()
        shot = Shot(requested_path=route.key())
        self._console = []
        url = navigate_url(self.origin, self.base_path, route)
        response = None
        try:
            response = await self._goto(page, url)
        except self._timeout_error:
            shot.status = "timeout"
            shot.error = f"Navigation exceeded {self.settings.timeout_ms} ms"
            shot.elapsed_ms = int((time.monotonic() - started) * 1000)
            self.log.emit("error", f"{route.key()} timed out")
            return shot
        except Exception as exc:
            full = self.log.scrub(str(exc))
            lowered = full.lower()
            message = (full.splitlines()[0].strip() if full else "")[:500]
            if "download" in lowered:
                shot.status = "download"
            elif "closed" in lowered or self._page_crashed:
                shot.status = "browser-error"
                self._page_crashed = True
            else:
                shot.status = "browser-error"
            shot.error = message
            shot.elapsed_ms = int((time.monotonic() - started) * 1000)
            self.log.emit("error", f"{route.key()} {message}")
            return shot
        try:
            await self._settle(page, scroll=self.settings.scroll)
        except Exception:
            shot.notes.append("the page did not settle")
        final = ""
        try:
            final = page.url or ""
        except Exception:
            final = ""
        shot.final_url = public_url(final, self.log.secrets)
        shot.redirect_count = redirect_count(response)
        shot.http_status = getattr(response, "status", None) if response is not None else None
        if final and not same_site(final, self.origin):
            shot.status = "redirect-external"
            shot.error = "Final URL left the local origin; not captured."
            shot.elapsed_ms = int((time.monotonic() - started) * 1000)
            self.log.emit("skip", f"{route.key()} left the origin")
            return shot
        shot.redirected_to_auth = bool(final) and is_auth_path(urlparse(final).path) and not is_auth_path(route.path)
        final_route = normalize_route(final, self.origin, keep_query=False) if final else None
        final_key = final_route.key() if final_route is not None else shot.final_url
        if final_key in self._captured_final:
            shot.file = self._captured_final[final_key]
            shot.status = "redirect-to-auth" if shot.redirected_to_auth else "duplicate"
            shot.notes.append("same final page as an earlier route")
            shot.console_errors = list(self._console)
            shot.elapsed_ms = int((time.monotonic() - started) * 1000)
            self.log.emit("skip", f"{route.key()} duplicate of {final_key}")
            return shot
        try:
            await self._extract_links(page, depth=self.routes.depth.get(route.key(), 0) + 1)
        except Exception:
            shot.notes.append("links were not read")
        try:
            shot.title = self.log.scrub(await page.title())[:180]
        except Exception:
            shot.title = ""
        soft = False
        if shot.http_status == 200:
            soft = await self._looks_soft_404(page)
        filename = screenshot_name(route.key(), self._names)
        assert self.shot_dir is not None
        target = self.shot_dir / filename
        if not is_within(self.shot_dir, target):
            shot.status = "screenshot-failed"
            shot.error = "screenshot path escaped the run directory"
            return shot
        saved = await self._save_screenshot(page, target, shot)
        if saved:
            shot.file = target.relative_to(self.run_dir or target.parent).as_posix() if self.run_dir else filename
            self._captured_final[final_key] = shot.file
            shot.status = status_for(shot.http_status, redirected_auth=shot.redirected_to_auth, soft_404=soft)
            self.log.emit("shot", f"{route.key()} -> {shot.file} ({shot.status})")
        else:
            shot.status = "screenshot-failed"
            self.log.emit("error", f"{route.key()} screenshot failed")
        if await self._body_blank(page) and shot.status == "ok":
            shot.notes.append("page body looks blank")
        shot.console_errors = list(self._console)
        shot.elapsed_ms = int((time.monotonic() - started) * 1000)
        return shot

    async def _goto(self, page: Any, url: str) -> Any:
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                return await page.goto(url, wait_until="domcontentloaded", timeout=self.settings.timeout_ms)
            except self._timeout_error:
                raise
            except Exception as exc:
                last_error = exc
                message = str(exc).lower()
                retryable = any(token in message for token in ("err_connection", "err_aborted", "err_network", "closed"))
                if attempt == 0 and retryable and not page.is_closed():
                    continue
                raise
        if last_error:
            raise last_error
        return None

    async def _settle(self, page: Any, *, scroll: bool) -> None:
        try:
            await page.wait_for_load_state("networkidle", timeout=min(2500, self.settings.timeout_ms))
        except Exception:
            # networkidle is an optimization. Websocket apps often never reach it.
            pass
        if self.settings.settle_ms:
            await page.wait_for_timeout(self.settings.settle_ms)
        if scroll:
            try:
                await page.evaluate(SCROLL_JS)
            except Exception:
                return

    async def _extract_links(self, page: Any, depth: int) -> None:
        hrefs = await page.eval_on_selector_all(
            "a[href]",
            "elements => elements.map((anchor) => anchor.getAttribute('href')).filter(Boolean).slice(0, 150)",
        )
        for href in hrefs or []:
            _add_candidate(
                self.routes,
                str(href),
                self.origin,
                self.settings.dynamic_values,
                depth=depth,
                keep_query=False,
                priority=False,
            )

    async def _looks_soft_404(self, page: Any) -> bool:
        try:
            if await page.locator("h1").count() == 0:
                return False
            heading = (await page.locator("h1").first.inner_text(timeout=500)).strip().lower()
        except Exception:
            return False
        return heading in {"404", "not found", "page not found", "404 not found"}

    async def _body_blank(self, page: Any) -> bool:
        try:
            text = await page.locator("body").inner_text(timeout=800)
        except Exception:
            return False
        return not text.strip()

    async def _save_screenshot(self, page: Any, target: Path, shot: Shot) -> bool:
        try:
            await page.screenshot(path=str(target), full_page=True, timeout=15000, animations="disabled")
            return True
        except TypeError:
            try:
                await page.screenshot(path=str(target), full_page=True, timeout=15000)
                return True
            except Exception as exc:
                shot.error = self.log.scrub(str(exc))[:500]
                return False
        except Exception:
            try:
                await page.screenshot(path=str(target), full_page=False, timeout=10000)
                shot.notes.append("viewport screenshot; full-page capture failed")
                return True
            except Exception as exc:
                shot.error = self.log.scrub(str(exc))[:500]
                return False

    async def attempt_auth(self, page: Any, context: Any) -> None:
        if self.settings.no_auth:
            self.auth.notes.append("Disabled by --no-auth.")
            self.log.emit("auth", "skipped (--no-auth)")
            return
        self.auth.attempted = True
        if self.settings.user_supplied_credentials and not self.settings.password:
            self.auth.reason = "email was provided without a password"
            self.auth.notes.append("Login was not submitted because the password is missing.")
            self.log.emit("auth", self.auth.reason)
            return
        if self.settings.user_supplied_credentials:
            self.auth.mode = "login"
            identity = self._identity(generated=False)
            ok, reason = await self._try_auth(page, context, "login", identity)
        else:
            self.auth.mode = "signup"
            identity = self._identity(generated=True)
            ok, reason = await self._try_auth(page, context, "signup", identity)
        self.auth.succeeded = ok
        self.auth.reason = reason
        self.log.emit("auth", reason or ("authenticated" if ok else "continuing without authentication"))

    def _identity(self, *, generated: bool) -> dict[str, str]:
        if not generated:
            email = self.settings.email or ""
            username = self.settings.username or (email.split("@", 1)[0] if "@" in email else "")
            return {
                "email": email,
                "password": self.settings.password or "",
                "name": self.settings.name or "",
                "username": username,
            }
        stamp = time.strftime("%Y%m%d%H%M%S")
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
        import secrets
        password = "Aa1!" + "".join(secrets.choice(alphabet) for _ in range(18))
        email = f"siteshot+{stamp}@example.com"
        self.log.add_secret(password)
        self.log.add_secret(email)
        return {
            "email": email,
            "password": password,
            "name": self.settings.name or "SiteShot Bot",
            "username": f"siteshot{stamp[-6:]}",
        }

    def _auth_routes(self, mode: str) -> list[str]:
        hints = SIGNUP_HINTS if mode == "signup" else LOGIN_HINTS
        found = [route.key() for route in self.routes.pending(set()) if _path_has_hint(route.path, hints)]
        ordered: list[str] = []
        for item in [*found, *hints]:
            if item not in ordered:
                ordered.append(item)
            if len(ordered) >= 5:
                break
        return ordered

    async def _try_auth(self, page: Any, context: Any, mode: str, identity: Mapping[str, str]) -> tuple[bool, str]:
        notes: list[str] = []
        saw_form = False
        for route in self._auth_routes(mode):
            target = navigate_url(self.origin, self.base_path, Route(route if route.startswith("/") else "/" + route))
            try:
                await page.goto(target, wait_until="domcontentloaded", timeout=self.settings.timeout_ms)
            except Exception as exc:
                notes.append(self.log.scrub(str(exc))[:180])
                continue
            if page.url and not same_site(page.url, self.origin):
                notes.append("authentication page left the local origin")
                continue
            try:
                forms = await page.evaluate(FORM_SNAPSHOT_JS)
            except Exception:
                notes.append("could not inspect the authentication page")
                continue
            path = urlparse(page.url or target).path
            selected, blocked = select_auth_target(forms or [], path, mode)
            if blocked:
                notes.append(blocked)
                if blocked.startswith("captcha") or "OAuth" in blocked or "magic-link" in blocked:
                    break
                continue
            if selected is None:
                continue
            saw_form = True
            actions, abort = plan_fills(selected.get("controls") or [], mode, identity)
            if abort:
                notes.append(abort)
                continue
            submit_index = choose_submit(selected.get("controls") or [], mode)
            if submit_index is None:
                notes.append("no safe submit button")
                continue
            ok, reason = await self._submit_auth(page, context, str(selected.get("id")), actions, submit_index)
            self.auth.notes.extend(notes)
            self.auth.notes.append(reason)
            if ok:
                try:
                    await self._extract_links(page, depth=1)
                except Exception:
                    pass
                return True, reason
            notes.append(reason)
        self.auth.notes.extend(notes)
        if not saw_form and not notes:
            reason = "no authentication form was found"
        elif notes:
            reason = notes[-1]
        else:
            reason = "authentication did not succeed"
        if not self.settings.user_supplied_credentials and not saw_form:
            reason = "no signup form was found; login was not attempted without credentials"
        return False, reason

    async def _submit_auth(
        self,
        page: Any,
        context: Any,
        form_id: str,
        actions: list[dict[str, Any]],
        submit_index: int,
    ) -> tuple[bool, str]:
        before_cookies = await context.cookies()
        before_storage = await self._storage_keys(page)
        scope = page.locator(f'[data-siteshot-form="{form_id}"]')
        controls = scope.locator("input, textarea, select, button")
        for action in actions:
            target = controls.nth(int(action["index"]))
            try:
                if action["kind"] == "check":
                    await target.check(timeout=1000)
                elif action["kind"] == "select":
                    await target.select_option(label=str(action["value"]), timeout=1000)
                else:
                    await target.fill(str(action["value"]), timeout=1500)
            except Exception as exc:
                return False, "could not fill the form: " + self.log.scrub(str(exc))[:180]
        button = controls.nth(submit_index)
        try:
            await button.click(timeout=2500)
        except Exception as exc:
            # A navigation that removes the button still leaves a page to judge.
            message = str(exc).lower()
            if "timeout" in message and "closed" not in message:
                return False, "the submit button did not respond"
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=4000)
        except Exception:
            pass
        await page.wait_for_timeout(500)
        judged, reason = await self._collect_auth_judgement(page, context, before_cookies, before_storage)
        if not judged:
            return False, reason
        home = navigate_url(self.origin, self.base_path, Route("/"))
        try:
            await page.goto(home, wait_until="domcontentloaded", timeout=self.settings.timeout_ms)
            await page.wait_for_timeout(300)
        except Exception:
            return False, "could not confirm the session"
        if page.url and is_auth_path(urlparse(page.url).path):
            try:
                still = await page.locator('input[type="password"]').count()
            except Exception:
                still = 0
            if still:
                return False, "redirected back to login"
        return True, reason

    async def _collect_auth_judgement(
        self,
        page: Any,
        context: Any,
        before_cookies: list[dict[str, Any]],
        before_storage: set[str],
    ) -> tuple[bool, str]:
        try:
            body = await page.locator("body").inner_text(timeout=1500)
        except Exception:
            body = ""
        try:
            passwords = await page.locator('input[type="password"]').count()
        except Exception:
            passwords = 0
        try:
            invalid = await page.locator("[aria-invalid='true']").count()
        except Exception:
            invalid = 0
        try:
            captcha = await page.locator("iframe[src*='recaptcha'], iframe[src*='hcaptcha'], .cf-turnstile, .g-recaptcha").count()
        except Exception:
            captcha = 0
        after_cookies = await context.cookies()
        after_storage = await self._storage_keys(page)
        new_session = session_cookie_changed(before_cookies, after_cookies) or storage_session_changed(before_storage, after_storage)
        return judge_auth(
            url=page.url or "",
            password_fields=passwords,
            body_text=body,
            new_session=new_session,
            captcha=bool(captcha),
            invalid_fields=invalid,
        )

    async def _storage_keys(self, page: Any) -> set[str]:
        try:
            keys = await page.evaluate("() => { try { return Object.keys(window.localStorage || {}); } catch (error) { return []; } }")
        except Exception:
            return set()
        return {str(key) for key in keys or []}

    def write_reports(self) -> None:
        if self.run_dir is None:
            return
        finished = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        started = self.started_at or finished
        try:
            started_stamp = time.mktime(time.strptime(started[:19], "%Y-%m-%dT%H:%M:%S"))
            elapsed = max(0, int((time.time() - started_stamp) * 1000))
        except (ValueError, OverflowError):
            elapsed = 0
        skipped = getattr(self, "_skipped", [])
        payload = build_manifest(
            started_at=started,
            finished_at=finished,
            elapsed_ms=elapsed,
            root=self.root,
            run_dir=self.run_dir,
            origin=self.origin,
            base_url=self.base_url,
            project=self.project,
            settings=self.settings,
            server_started=self.server_started,
            server_reused=self.server_reused,
            server_command=self.server_command,
            auth=self.auth,
            routes=self.routes,
            results=self.results,
            skipped=skipped,
            source_files=self.source_files,
            playwright_version=playwright_version(),
            fatal_error=self.fatal_error,
            secrets=self.log.secrets,
        )
        atomic_write(self.run_dir / "manifest.json", json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        atomic_write(self.run_dir / "report.html", render_report_html(payload))
        captured = sum(1 for shot in self.results if shot.file)
        failed = len(self.results) - captured
        self.log.emit("shot", f"screenshots {captured}; failed {failed}")
        self.log.emit("shot", f"report {self.run_dir / 'report.html'}")
        self.log.emit("shot", f"manifest {self.run_dir / 'manifest.json'}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="siteshot",
        description="Discover and screenshot local website routes, with best-effort auth.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "priority: command-line flags, then environment variables, then .siteshot.json, then defaults.\n"
            "environment: SITESHOT_URL, SITESHOT_EMAIL, SITESHOT_PASSWORD, SITESHOT_NAME,\n"
            "             SITESHOT_NO_AUTH, SITESHOT_MAX_ROUTES.\n"
            "exit codes: 0 screenshots saved, 1 failed or nothing captured, 2 usage/config, 130 interrupted.\n"
            "Auto-start runs the project's dev script (or Django runserver). It does not run \"start\".\n"
            "Dynamic routes are visited only when dynamic_values supplies an example."
        ),
    )
    parser.add_argument("--version", action="version", version=f"SiteShot {VERSION}")
    parser.add_argument("--root", default=".", help="Project root. Default: current directory.")
    parser.add_argument("--url", default=None, help="Existing local origin, for example http://127.0.0.1:3000.")
    parser.add_argument("--output", default=None, help="Output root. Default: .siteshot inside the project.")
    parser.add_argument("--route", action="append", default=None, help="Additional route to capture. Repeatable.")
    parser.add_argument("--max-routes", dest="max_routes", type=int, default=None, help="Maximum pages to capture. Default: 150.")
    parser.add_argument("--max-depth", dest="max_depth", type=int, default=None, help="Maximum link depth beyond seed routes. Default: 4.")
    parser.add_argument("--max-source-files", dest="max_source_files", type=int, default=None, help="Maximum source files to scan. Default: 1200.")
    parser.add_argument("--max-discovered", dest="max_discovered", type=int, default=None, help="Maximum routes kept from discovery. Default: at least 600.")
    parser.add_argument("--timeout", type=int, default=None, help="Per-page navigation timeout in milliseconds. Default: 12000.")
    parser.add_argument("--server-timeout", dest="server_timeout", type=int, default=None, help="Seconds to wait for an auto-started server. Default: 30.")
    parser.add_argument("--settle-ms", dest="settle_ms", type=int, default=None, help="Extra quiet time after load, in milliseconds. Default: 400.")
    parser.add_argument("--width", type=int, default=None, help="Viewport width. Default: 1440.")
    parser.add_argument("--height", type=int, default=None, help="Viewport height. Default: 1000.")
    parser.add_argument("--locale", default=None, help="Browser locale. Default: en-US.")
    parser.add_argument("--headful", action="store_const", const=True, default=None, help="Show the browser window.")
    parser.add_argument("--no-auth", dest="no_auth", action="store_const", const=True, default=None, help="Do not try login or signup.")
    scroll = parser.add_mutually_exclusive_group()
    scroll.add_argument("--scroll", dest="scroll", action="store_const", const=True, help="Scroll pages to load lazy content. This is the default.")
    scroll.add_argument("--no-scroll", dest="scroll", action="store_const", const=False, help="Do not scroll pages for lazy content.")
    parser.set_defaults(scroll=None)
    parser.add_argument("--email", default=None, help="Login email. Prefer SITESHOT_EMAIL.")
    parser.add_argument("--password", default=None, help="Login password. Prefer SITESHOT_PASSWORD; the command line is visible to other local users.")
    parser.add_argument("--name", default=None, help="Signup display name. Prefer SITESHOT_NAME.")
    return parser


def main(argv: list[str] | None = None) -> int:
    configure_stdio()
    args = build_parser().parse_args(argv)
    try:
        root = Path(args.root).expanduser().resolve()
        if not root.is_dir():
            raise SiteShotError(f"Project root does not exist or is not a directory: {root}", 2)
        config_path = root / ".siteshot.json"
        config = load_config_file(config_path)
        warnings = validate_config(config) if config else []
        settings = merge_settings(args, config, os.environ, root)
        log = Log()
        if args.password:
            log.emit("warn", "Prefer SITESHOT_PASSWORD. A password in the command is visible to other local users.")
        for warning in warnings:
            log.emit("warn", warning)
        app = SiteShot(settings, log)
        return asyncio.run(app.run())
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except SiteShotError as exc:
        print(f"\n{exc}", file=sys.stderr)
        return exc.exit_code
    except Exception:
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
