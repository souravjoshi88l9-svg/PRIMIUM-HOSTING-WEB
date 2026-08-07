#!/usr/bin/env python3
"""
MD Shanwaj Hosting — Premium Web Panel
Fast Flask dashboard with upload + GitHub deploy.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import re
import secrets
import shutil
import signal
import subprocess
import threading
import time
import zipfile
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from flask import (
    Flask,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
    abort,
    jsonify,
)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename
import urllib.parse
import urllib.request

# ── Paths ────────────────────────────────────────────────────────
BASE = Path(__file__).resolve().parent
# On Railway, set DATA_ROOT=/data and attach a Volume at /data to keep users/bots after restart
_DATA_ROOT = Path(os.environ.get("DATA_ROOT", str(BASE)))
DATA_DIR = _DATA_ROOT / "data"
UPLOAD_DIR = _DATA_ROOT / "uploads"
SANDBOX_DIR = _DATA_ROOT / "sandbox"
DB_FILE = DATA_DIR / "db.json"

for d in (DATA_DIR, UPLOAD_DIR, SANDBOX_DIR):
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception as _e:
        print(f"[warn] mkdir {d}: {_e}", flush=True)

app = Flask(
    __name__,
    template_folder=str(BASE / "templates"),
    static_folder=str(BASE / "static"),
)


def _stable_secret() -> str:
    """Keep the same secret across restarts so users stay logged in."""
    env = os.environ.get("SECRET_KEY", "").strip()
    if env:
        return env
    key_file = DATA_DIR / ".secret_key"
    try:
        if key_file.exists():
            k = key_file.read_text().strip()
            if len(k) >= 16:
                return k
        k = secrets.token_hex(32)
        key_file.write_text(k)
        return k
    except Exception:
        return secrets.token_hex(32)

app.secret_key = _stable_secret()
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024  # 25 MB
app.config["PERMANENT_SESSION_LIFETIME"] = 60 * 60 * 24 * 30  # 30 days
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
# Only force Secure cookies if explicitly requested
if os.environ.get("SESSION_COOKIE_SECURE", "").lower() in ("1", "true", "yes"):
    app.config["SESSION_COOKIE_SECURE"] = True
try:
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
except Exception:
    pass

# Google OAuth (set these env vars for real Google login)
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()
GOOGLE_ENABLED = bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)

@app.context_processor
def _inject_globals():
    """Always available in templates — prevents undefined errors."""
    return {
        "google_enabled": GOOGLE_ENABLED,
        "brand": "MD Shanwaj Hosting",
    }

@app.errorhandler(500)
def _err_500(e):
    import traceback
    tb = traceback.format_exc()
    print(f"[500] {e}\n{tb}", flush=True)
    if os.environ.get("SHOW_ERRORS", "").lower() in ("1", "true", "yes"):
        return (
            f"<h1>500 Error</h1><pre style='white-space:pre-wrap;color:#c00'>{tb}</pre>"
            f"<p>Unset SHOW_ERRORS after fixing.</p>",
            500,
        )
    return (
        "<!doctype html><html><body style='font-family:sans-serif;background:#0b0b12;color:#eee;padding:2rem'>"
        "<h1>Something went wrong</h1>"
        "<p>Server error. Set Railway variable <code>SHOW_ERRORS=1</code>, redeploy, open again, "
        "and check the error text. Also check Deploy Logs.</p>"
        "<p><a href='/' style='color:#818cf8'>Try home</a></p></body></html>",
        500,
    )

@app.errorhandler(404)
def _err_404(e):
    return (
        "<!doctype html><html><body style='font-family:sans-serif;background:#0b0b12;color:#eee;padding:2rem'>"
        "<h1>Not found</h1><p><a href='/' style='color:#818cf8'>Go home</a></p></body></html>",
        404,
    )


# ── Plans ────────────────────────────────────────────────────────
PLANS: Dict[str, Dict[str, Any]] = {
    "free":       {"name": "Free",       "max_bots": 50,  "ram": 8192, "auto_restart": True, "price": 0, "days": 0},
    "starter":    {"name": "Starter",    "max_bots": 50,  "ram": 8192, "auto_restart": True, "price": 0, "days": 30},
    "basic":      {"name": "Basic",      "max_bots": 50,  "ram": 8192, "auto_restart": True, "price": 0, "days": 30},
    "pro":        {"name": "Pro",        "max_bots": 50,  "ram": 8192, "auto_restart": True, "price": 0, "days": 30},
    "enterprise": {"name": "Enterprise", "max_bots": 50,  "ram": 8192, "auto_restart": True, "price": 0, "days": 30},
    "lifetime":   {"name": "Lifetime",   "max_bots": 100, "ram": 8192, "auto_restart": True, "price": 0, "days": 36500},
}

ENTRY_PY = ("bot.py", "main.py", "app.py", "run.py")
ENTRY_JS = ("index.js", "bot.js", "main.js", "app.js")
ALLOWED_EXT = {".py", ".js", ".zip"}

# Running processes: bot_id -> {proc, log_path, started}
RUNNING: Dict[str, Dict[str, Any]] = {}
_run_lock = threading.Lock()
_db_lock = threading.Lock()


# Pre-warm common bot packages in background (PythonAnywhere free-friendly)
def _prewarm_common_packages():
    try:
        common = [
            "python-telegram-bot", "pyTelegramBotAPI", "aiohttp", "urllib3",
            "requests", "python-dotenv", "httpx",
        ]
        subprocess.Popen(
            [*_pip_base(), "install", "--user", *common],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as e:
        print(f"[prewarm] {e}", flush=True)

try:
    if os.environ.get("SKIP_PREWARM", "").lower() not in ("1", "true", "yes"):
        threading.Thread(target=_prewarm_common_packages, daemon=True).start()
except Exception:
    pass


# ── DB helpers ───────────────────────────────────────────────────
def _default_db() -> dict:
    return {"users": {}, "bots": {}, "settings": {}}


def db_load() -> dict:
    with _db_lock:
        if not DB_FILE.exists():
            return _default_db()
        try:
            return json.loads(DB_FILE.read_text(encoding="utf-8"))
        except Exception:
            return _default_db()


def db_save(data: dict) -> None:
    with _db_lock:
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            tmp = DB_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
            tmp.replace(DB_FILE)
        except Exception as e:
            print(f"[db_save] {e}", flush=True)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def uid() -> str:
    return secrets.token_hex(6)


def safe_name(s: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9_-]+", "_", (s or "").strip())[:48]
    return s or "bot"


# ── Auth ─────────────────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            flash("Please sign in first.", "error")
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapped


def admin_required(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login"))
        if not session.get("is_admin"):
            flash("Admin only.", "error")
            return redirect(url_for("dashboard"))
        return f(*args, **kwargs)
    return wrapped


def current_user() -> Optional[dict]:
    uid_ = session.get("user_id")
    if not uid_:
        return None
    return db_load()["users"].get(uid_)


def get_user_bots(user_id: str) -> List[dict]:
    d = db_load()
    bots = []
    for bid, b in d["bots"].items():
        if str(b.get("owner")) == str(user_id):
            b = dict(b)
            b["id"] = bid
            b["running"] = _is_running(bid)
            bots.append(b)
    bots.sort(key=lambda x: x.get("created", ""), reverse=True)
    return bots


# ── Process runner ───────────────────────────────────────────────
def _is_running(bot_id: str) -> bool:
    with _run_lock:
        info = RUNNING.get(bot_id)
        if not info:
            return False
        proc = info.get("proc")
        if proc is None:
            return False
        return proc.poll() is None


def _append_log(log_path: Path, line: str) -> None:
    try:
        with log_path.open("a", encoding="utf-8", errors="ignore") as f:
            f.write(line)
            if not line.endswith("\n"):
                f.write("\n")
    except Exception:
        pass


def _read_log(bot_id: str, max_lines: int = 120) -> str:
    log_path = SANDBOX_DIR / bot_id / "bot.log"
    if not log_path.exists():
        return ""
    try:
        lines = log_path.read_text(encoding="utf-8", errors="ignore").splitlines()
        return "\n".join(lines[-max_lines:])
    except Exception:
        return ""


def detect_entry(bot_dir: Path) -> Tuple[Optional[str], Optional[str]]:
    for n in ENTRY_PY:
        if (bot_dir / n).exists():
            return "python", n
    for n in ENTRY_JS:
        if (bot_dir / n).exists():
            return "node", n
    py = sorted(bot_dir.rglob("*.py"))
    py = [p for p in py if ".deps" not in p.parts and "venv" not in p.parts]
    if py:
        return "python", str(py[0].relative_to(bot_dir))
    js = sorted(bot_dir.rglob("*.js"))
    js = [p for p in js if "node_modules" not in p.parts]
    if js:
        return "node", str(js[0].relative_to(bot_dir))
    return None, None



def _pip_base() -> list:
    """Return [python, '-m', 'pip'] — never uwsgi (PythonAnywhere web)."""
    exe = (sys.executable or "").lower()
    if not exe or "uwsgi" in exe or "gunicorn" in exe:
        return ["python3", "-m", "pip"]
    return [sys.executable, "-m", "pip"]


# Common import-name → pip package
_PIP_MAP = {
    "aiohttp": "aiohttp",
    "aiofiles": "aiofiles",
    "urllib3": "urllib3",
    "requests": "requests",
    "bs4": "beautifulsoup4",
    "telebot": "pyTelegramBotAPI",
    "telegram": "python-telegram-bot",
    "pyrogram": "pyrogram",
    "telethon": "telethon",
    "discord": "discord.py",
    "dotenv": "python-dotenv",
    "PIL": "Pillow",
    "cv2": "opencv-python-headless",
    "numpy": "numpy",
    "pandas": "pandas",
    "flask": "flask",
    "fastapi": "fastapi",
    "httpx": "httpx",
    "motor": "motor",
    "pymongo": "pymongo",
    "sqlalchemy": "SQLAlchemy",
    "redis": "redis",
    "yt_dlp": "yt-dlp",
    "gtts": "gTTS",
    "pydub": "pydub",
    "cryptography": "cryptography",
    "Crypto": "pycryptodome",
    "tqdm": "tqdm",
    "rich": "rich",
    "lxml": "lxml",
    "openai": "openai",
}


def _scan_imports(bot_dir: Path) -> list:
    """Collect third-party import names from .py files."""
    found = set()
    for py in bot_dir.rglob("*.py"):
        if any(x in py.parts for x in (".git", "venv", "__pycache__", ".venv")):
            continue
        try:
            text = py.read_text(encoding="utf-8", errors="ignore")[:80000]
        except Exception:
            continue
        for m in re.finditer(r"^(?:from|import)\s+([a-zA-Z0-9_]+)", text, re.M):
            name = m.group(1)
            if name in _PIP_MAP:
                found.add(name)
    return sorted(found)


def _user_site_paths() -> list:
    """All possible user site-packages dirs (PythonAnywhere)."""
    paths = []
    homes = []
    try:
        homes.append(Path.home())
    except Exception:
        pass
    for h in (os.environ.get("HOME"), "/home/" + os.environ.get("USER", "")):
        if h:
            homes.append(Path(h))
    # PA username folders
    try:
        for d in Path("/home").iterdir():
            if d.is_dir() and not d.name.startswith("."):
                homes.append(d)
    except Exception:
        pass
    seen = set()
    for home in homes:
        for ver in ("3.13", "3.12", "3.11", "3.10", "3.9"):
            cand = home / ".local" / "lib" / f"python{ver}" / "site-packages"
            s = str(cand)
            if cand.is_dir() and s not in seen:
                seen.add(s)
                paths.append(s)
    return paths


def _pip_install(packages: list, log_write) -> bool:
    """Install packages with python3 -m pip --user. Return True if ok."""
    if not packages:
        return True
    cmd = ["python3", "-m", "pip", "install", "--user", "--no-warn-script-location", *packages]
    log_write(f"[deps] RUN: {' '.join(cmd)}\n")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=500)
        out = (r.stdout or "")[-3000:]
        err = (r.stderr or "")[-2000:]
        if out:
            log_write(out + "\n")
        if err:
            log_write(err + "\n")
        if r.returncode != 0:
            log_write(f"[deps] pip exit {r.returncode}\n")
            return False
        log_write("[deps] pip OK\n")
        return True
    except Exception as e:
        log_write(f"[deps] pip exception: {e}\n")
        return False


def _auto_install_deps(bot_dir: Path, log_write) -> None:
    """Install requirements.txt + detected imports. Always use python3, not uwsgi."""
    pkgs = []
    req = bot_dir / "requirements.txt"
    if req.exists():
        log_write("[deps] Found requirements.txt\n")
        try:
            # parse requirements lines to list (skip git/complex)
            lines = []
            for line in req.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or line.startswith("-"):
                    continue
                if "://" in line or line.startswith("git+"):
                    continue
                lines.append(line.split(";")[0].strip())
            if lines:
                _pip_install(lines, log_write)
            else:
                # fallback full file
                cmd = ["python3", "-m", "pip", "install", "--user", "--no-warn-script-location", "-r", str(req)]
                log_write(f"[deps] RUN: {' '.join(cmd)}\n")
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=500)
                log_write(((r.stdout or "") + (r.stderr or ""))[-4000:] + "\n")
        except Exception as e:
            log_write(f"[deps] requirements error: {e}\n")

    # scan imports
    imports = _scan_imports(bot_dir)
    for name in imports:
        pkg = _PIP_MAP.get(name)
        if pkg:
            pkgs.append(pkg)
    try:
        blob = ""
        for py in list(bot_dir.rglob("*.py"))[:40]:
            if any(x in py.parts for x in (".git", "venv", "__pycache__")):
                continue
            blob += py.read_text(encoding="utf-8", errors="ignore")[:25000]
        low = blob.lower()
        if "aiohttp" in low:
            pkgs.append("aiohttp")
        if "from telegram" in low or "import telegram" in low:
            pkgs.append("python-telegram-bot")
        if "telebot" in low:
            pkgs.append("pyTelegramBotAPI")
        if "pycryptodome" in low or "from crypto" in low or "from Crypto" in blob:
            pkgs.append("pycryptodome")
        if "urllib3" in low:
            pkgs.append("urllib3")
        if "requests" in low:
            pkgs.append("requests")
    except Exception as e:
        log_write(f"[deps] scan error: {e}\n")

    pkgs = sorted(set(pkgs))
    if pkgs:
        log_write(f"[deps] Extra packages: {', '.join(pkgs)}\n")
        _pip_install(pkgs, log_write)
    else:
        log_write("[deps] No extra packages detected\n")

    # verify critical imports
    sites = _user_site_paths()
    pp = os.pathsep.join(sites)
    for mod in ("aiohttp", "telegram", "telebot", "requests"):
        r = subprocess.run(
            ["python3", "-c", f"import sys; sys.path[:0]={sites!r}; import {mod}; print('{mod} OK')"],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode == 0:
            log_write(f"[deps] verify {mod}: OK\n")
        else:
            log_write(f"[deps] verify {mod}: FAIL — {(r.stderr or r.stdout or '')[-300:]}\n")



def start_bot(bot_id: str) -> Tuple[bool, str]:
    d = db_load()
    b = d["bots"].get(bot_id)
    if not b:
        return False, "Bot not found"
    bot_dir = Path(b.get("dir", ""))
    if not bot_dir.exists():
        return False, "Bot directory missing"

    kind = b.get("kind") or "python"
    entry = b.get("entry") or "bot.py"
    entry_path = bot_dir / entry
    if not entry_path.exists():
        kind, entry = detect_entry(bot_dir)
        if not kind:
            return False, "No entry file found (bot.py / main.py / index.js)"
        b["kind"] = kind
        b["entry"] = entry
        d["bots"][bot_id] = b
        db_save(d)
        entry_path = bot_dir / entry

    if _is_running(bot_id):
        return True, "Already running"

    log_path = bot_dir / "bot.log"
    env = os.environ.copy()
    # Strip host secrets
    for k in ("SECRET_KEY", "BOT_TOKEN", "OWNER_ID", "GITHUB_TOKEN"):
        env.pop(k, None)
    # NEVER set HOME to bot_dir — breaks pip --user packages on PythonAnywhere
    env["PYTHONUNBUFFERED"] = "1"
    env.pop("PYTHONNOUSERSITE", None)
    _pp = _user_site_paths()
    if _pp:
        env["PYTHONPATH"] = os.pathsep.join(_pp + [p for p in env.get("PYTHONPATH", "").split(os.pathsep) if p])
        # log path for debug
        print(f"[start] PYTHONPATH={env['PYTHONPATH'][:200]}", flush=True)
    for k, v in (b.get("env") or {}).items():
        if str(k).upper() in ("HOME", "PYTHONPATH", "PYTHONNOUSERSITE"):
            continue
        env[str(k)] = str(v)

    if kind == "python":
        cmd = ["python3", "-u", str(entry_path)]
    else:
        cmd = ["node", str(entry_path)]

    try:
        log_f = open(log_path, "a", encoding="utf-8")
        log_f.write(f"\n--- started {now_iso()} ---\n")
        log_f.flush()
        if kind == "python":
            def _lw(msg: str) -> None:
                log_f.write(msg)
                log_f.flush()
            _auto_install_deps(bot_dir, _lw)
        proc = subprocess.Popen(
            cmd,
            cwd=str(bot_dir),
            env=env,
            stdout=log_f,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        with _run_lock:
            RUNNING[bot_id] = {"proc": proc, "log": log_path, "started": time.time(), "log_f": log_f}
        b["status"] = "running"
        d["bots"][bot_id] = b
        db_save(d)
        return True, "Started"
    except Exception as e:
        return False, str(e)


def stop_bot(bot_id: str) -> Tuple[bool, str]:
    with _run_lock:
        info = RUNNING.pop(bot_id, None)
    if info:
        proc = info.get("proc")
        if proc and proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except Exception:
                try:
                    proc.terminate()
                except Exception:
                    pass
            try:
                proc.wait(timeout=5)
            except Exception:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except Exception:
                    pass
        log_f = info.get("log_f")
        if log_f:
            try:
                log_f.close()
            except Exception:
                pass
    d = db_load()
    if bot_id in d["bots"]:
        d["bots"][bot_id]["status"] = "stopped"
        db_save(d)
    return True, "Stopped"


def delete_bot(bot_id: str, owner_id: str) -> Tuple[bool, str]:
    stop_bot(bot_id)
    d = db_load()
    b = d["bots"].get(bot_id)
    if not b:
        return False, "Not found"
    if str(b.get("owner")) != str(owner_id) and not session.get("is_admin"):
        return False, "Forbidden"
    bot_dir = Path(b.get("dir", ""))
    if bot_dir.exists() and str(SANDBOX_DIR) in str(bot_dir.resolve()):
        shutil.rmtree(bot_dir, ignore_errors=True)
    d["bots"].pop(bot_id, None)
    db_save(d)
    return True, "Deleted"


# ── Light security scan ──────────────────────────────────────────
_BAD_PATTERNS = [
    (r'os\.walk\s*\(\s*["\']/(?:root|etc|home)', "Sensitive directory walk"),
    (r'marshal\.loads\s*\(', "Marshalled bytecode"),
    (r'base64\.b64decode\s*\([^\n]+\)[^\n]*\bexec\b', "Base64 + exec"),
]


def quick_scan(path: Path) -> Tuple[str, List[str]]:
    """Return (verdict, threats). SAFE / SUSPICIOUS / DANGEROUS"""
    threats: List[str] = []
    files: List[Path] = []
    if path.is_file() and path.suffix == ".py":
        files = [path]
    elif path.is_dir():
        files = list(path.rglob("*.py"))[:15]
    for f in files:
        try:
            code = f.read_text(errors="ignore")
        except Exception:
            continue
        for pat, desc in _BAD_PATTERNS:
            if re.search(pat, code, re.I | re.M):
                threats.append(f"{f.name}: {desc}")
    if len(threats) >= 2:
        return "DANGEROUS", threats
    if threats:
        return "SUSPICIOUS", threats
    return "SAFE", []


# ── Routes: public ───────────────────────────────────────────────
@app.route("/")
def index():
    try:
        if session.get("user_id"):
            return redirect(url_for("dashboard"))
        return render_template("landing.html", google_enabled=GOOGLE_ENABLED)
    except Exception as e:
        print(f"[index] {e}", flush=True)
        # Absolute fallback so site never stays blank-500 without message
        return (
            "<!doctype html><html><body style='font-family:sans-serif;background:#07070f;color:#fff;padding:2rem'>"
            "<h1>MD Shanwaj Hosting</h1>"
            "<p>Panel is running.</p>"
            "<p><a href='/login' style='color:#818cf8'>Login</a> · "
            "<a href='/register' style='color:#818cf8'>Register</a> · "
            "<a href='/health' style='color:#818cf8'>Health</a></p>"
            f"<pre style='color:#f87171;margin-top:1rem'>{e}</pre>"
            "</body></html>"
        )


def _google_redirect_uri() -> str:
    # Prefer explicit public URL; else build from request
    base = os.environ.get("PUBLIC_URL", "").rstrip("/")
    if base:
        return f"{base}/auth/google/callback"
    return url_for("google_callback", _external=True)


@app.route("/auth/google")
def google_login():
    """Start Google OAuth flow."""
    if not GOOGLE_ENABLED:
        flash(
            "Google login not configured. Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET, "
            "or use email/password.",
            "error",
        )
        return redirect(url_for("login"))
    state = secrets.token_urlsafe(24)
    session["oauth_state"] = state
    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": _google_redirect_uri(),
        "response_type": "code",
        "scope": "openid email profile",
        "access_type": "online",
        "state": state,
        "prompt": "select_account",
    }
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(params)
    return redirect(url)


@app.route("/auth/google/callback")
def google_callback():
    """Handle Google OAuth callback."""
    if not GOOGLE_ENABLED:
        flash("Google login not configured.", "error")
        return redirect(url_for("login"))

    err = request.args.get("error")
    if err:
        flash(f"Google login cancelled: {err}", "error")
        return redirect(url_for("login"))

    state = request.args.get("state", "")
    if not state or state != session.pop("oauth_state", None):
        flash("Invalid OAuth state. Try again.", "error")
        return redirect(url_for("login"))

    code = request.args.get("code")
    if not code:
        flash("No authorization code from Google.", "error")
        return redirect(url_for("login"))

    try:
        # Exchange code for tokens
        token_data = urllib.parse.urlencode({
            "code": code,
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "redirect_uri": _google_redirect_uri(),
            "grant_type": "authorization_code",
        }).encode()
        req = urllib.request.Request(
            "https://oauth2.googleapis.com/token",
            data=token_data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            tokens = json.loads(resp.read().decode())
        access = tokens.get("access_token")
        if not access:
            flash("Failed to get Google access token.", "error")
            return redirect(url_for("login"))

        # Fetch user profile
        ureq = urllib.request.Request(
            "https://www.googleapis.com/oauth2/v2/userinfo",
            headers={"Authorization": f"Bearer {access}"},
        )
        with urllib.request.urlopen(ureq, timeout=15) as resp:
            profile = json.loads(resp.read().decode())
    except Exception as e:
        flash(f"Google login error: {e}", "error")
        return redirect(url_for("login"))

    email = (profile.get("email") or "").lower().strip()
    name = (profile.get("name") or profile.get("given_name") or "User").strip()[:48]
    google_id = str(profile.get("id") or "")
    picture = profile.get("picture") or ""

    if not email:
        flash("Google did not return an email.", "error")
        return redirect(url_for("login"))

    d = db_load()
    user = None
    user_id = None
    for uid_, u in d["users"].items():
        if u.get("email") == email or (google_id and u.get("google_id") == google_id):
            user = u
            user_id = uid_
            break

    if not user:
        # Auto-register
        user_id = uid()
        is_first = len(d["users"]) == 0
        # unique username from email
        base_un = re.sub(r"[^a-z0-9_]", "", email.split("@")[0].lower())[:16] or "user"
        uname = base_un
        n = 1
        existing = {u.get("username") for u in d["users"].values()}
        while uname in existing:
            uname = f"{base_un}{n}"
            n += 1
        d["users"][user_id] = {
            "id": user_id,
            "name": name,
            "email": email,
            "username": uname,
            "password": generate_password_hash(secrets.token_urlsafe(16)),
            "plan": "free",
            "wallet": 0,
            "joined": now_iso(),
            "is_admin": is_first,
            "google_id": google_id,
            "picture": picture,
            "auth_provider": "google",
        }
        db_save(d)
        flash(f"Welcome, {name}! Account created with Google.", "success")
    else:
        # Update google link / picture
        user["google_id"] = google_id or user.get("google_id")
        if picture:
            user["picture"] = picture
        if not user.get("name"):
            user["name"] = name
        d["users"][user_id] = user
        db_save(d)
        flash(f"Welcome back, {user.get('name', name)}!", "success")

    session.permanent = True
    session["user_id"] = user_id
    session["name"] = d["users"][user_id].get("name", name)
    session["is_admin"] = bool(d["users"][user_id].get("is_admin"))
    session["picture"] = d["users"][user_id].get("picture", "")
    return redirect(url_for("dashboard"))


@app.route("/register", methods=["GET", "POST"])
def register():
    if session.get("user_id"):
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()[:48]
        email = (request.form.get("email") or "").strip().lower().lower()
        username = (request.form.get("username") or "").strip().lower().lower()
        password = request.form.get("password") or ""

        if not name or not email or not username or len(password) < 6:
            flash("Please fill all fields (password min 6 chars).", "error")
            return render_template("register.html", google_enabled=GOOGLE_ENABLED)
        if not re.match(r"^[a-z0-9_]{3,24}$", username):
            flash("Username: 3–24 letters, numbers, underscore.", "error")
            return render_template("register.html", google_enabled=GOOGLE_ENABLED)

        d = db_load()
        for u in d["users"].values():
            if u.get("email") == email or u.get("username") == username:
                flash("Email or username already taken.", "error")
                return render_template("register.html", google_enabled=GOOGLE_ENABLED)

        user_id = uid()
        is_first = len(d["users"]) == 0
        d["users"][user_id] = {
            "id": user_id,
            "name": name,
            "email": email,
            "username": username,
            "password": generate_password_hash(password),
            "plan": "free",
            "wallet": 0,
            "joined": now_iso(),
            "is_admin": is_first,
        }
        db_save(d)
        session.permanent = True
        session["user_id"] = user_id
        session["name"] = name
        session["is_admin"] = is_first
        flash("Welcome! Your free account is ready.", "success")
        return redirect(url_for("dashboard"))
    return render_template("register.html", google_enabled=GOOGLE_ENABLED)


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("user_id"):
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        login_val = (request.form.get("login") or "").strip().lower()
        password = request.form.get("password") or ""
        d = db_load()
        user = None
        for u in d["users"].values():
            em = (u.get("email") or "").strip().lower()
            un = (u.get("username") or "").strip().lower()
            if login_val and (login_val == em or login_val == un):
                user = u
                break
        if not user:
            flash("Invalid login or password.", "error")
            return render_template("login.html", google_enabled=GOOGLE_ENABLED)
        stored = user.get("password") or ""
        if not stored or not check_password_hash(stored, password):
            flash("Invalid login or password.", "error")
            return render_template("login.html", google_enabled=GOOGLE_ENABLED)
        session.permanent = True
        session["user_id"] = user["id"]
        session["name"] = user.get("name", "User")
        session["is_admin"] = bool(user.get("is_admin"))
        session["picture"] = user.get("picture", "")
        flash("Signed in successfully.", "success")
        return redirect(url_for("dashboard"))
    return render_template("login.html", google_enabled=GOOGLE_ENABLED)


@app.route("/logout")
def logout():
    session.clear()
    flash("Logged out.", "info")
    return redirect(url_for("index"))


# ── Dashboard ────────────────────────────────────────────────────
@app.route("/dashboard")
@login_required
def dashboard():
    user = current_user()
    bots = get_user_bots(session["user_id"])
    stats = {
        "bots": len(bots),
        "running": sum(1 for b in bots if b.get("running")),
        "plan": (user or {}).get("plan", "free").title(),
        "wallet": (user or {}).get("wallet", 0),
    }
    return render_template("dashboard.html", stats=stats, recent_bots=bots[:5])


@app.route("/bots")
@login_required
def bots():
    return render_template("bots.html", bots=get_user_bots(session["user_id"]))


@app.route("/bots/<bot_id>")
@login_required
def bot_detail(bot_id: str):
    d = db_load()
    b = d["bots"].get(bot_id)
    if not b:
        flash("Bot not found.", "error")
        return redirect(url_for("bots"))
    if str(b.get("owner")) != str(session["user_id"]) and not session.get("is_admin"):
        abort(403)
    bot = dict(b)
    bot["id"] = bot_id
    bot["running"] = _is_running(bot_id)
    logs = _read_log(bot_id)
    return render_template("bot_detail.html", bot=bot, logs=logs)


@app.route("/bots/<bot_id>/<action>", methods=["POST"])
@login_required
def bot_action(bot_id: str, action: str):
    d = db_load()
    b = d["bots"].get(bot_id)
    if not b:
        flash("Bot not found.", "error")
        return redirect(url_for("bots"))
    if str(b.get("owner")) != str(session["user_id"]) and not session.get("is_admin"):
        abort(403)

    if action == "install":
        bot_dir = Path(b.get("dir", ""))
        log_path = bot_dir / "bot.log"
        try:
            with open(log_path, "a", encoding="utf-8") as lf:
                lf.write(f"\n--- deps install {now_iso()} ---\n")
                def lw(msg):
                    lf.write(msg)
                    lf.flush()
                if bot_dir.exists():
                    _auto_install_deps(bot_dir, lw)
                else:
                    lw("bot dir missing\n")
            flash("Package install finished — check logs.", "success")
        except Exception as e:
            flash(f"Install error: {e}", "error")
        return redirect(url_for("bot_detail", bot_id=bot_id))
    if action == "start":
        ok, msg = start_bot(bot_id)
        flash(msg if ok else f"Start failed: {msg}", "success" if ok else "error")
    elif action == "stop":
        ok, msg = stop_bot(bot_id)
        flash(msg, "success" if ok else "error")
    elif action == "restart":
        stop_bot(bot_id)
        time.sleep(0.4)
        ok, msg = start_bot(bot_id)
        flash("Restarted" if ok else f"Restart failed: {msg}", "success" if ok else "error")
    elif action == "delete":
        ok, msg = delete_bot(bot_id, session["user_id"])
        flash(msg, "success" if ok else "error")
        return redirect(url_for("bots"))
    elif action == "pull":
        repo = b.get("repo_url")
        if not repo:
            flash("Not a GitHub bot.", "error")
        else:
            bot_dir = Path(b.get("dir", ""))
            branch = b.get("branch") or "main"
            was_running = _is_running(bot_id)
            if was_running:
                stop_bot(bot_id)
            # Prefer git pull; else re-download ZIP
            pulled = False
            if _git_available() and (bot_dir / ".git").exists():
                try:
                    r = subprocess.run(
                        ["git", "pull"],
                        cwd=str(bot_dir),
                        capture_output=True,
                        text=True,
                        timeout=60,
                    )
                    if r.returncode == 0:
                        pulled = True
                        flash("Git pull OK. Restart the bot to apply.", "success")
                    else:
                        flash(f"Git pull failed: {(r.stderr or '')[:200]}", "error")
                except Exception as e:
                    flash(f"Pull error: {e}", "error")
            if not pulled:
                # Re-download via ZIP into a temp folder then swap
                tmp = bot_dir.parent / f"{bot_id}_tmp"
                shutil.rmtree(tmp, ignore_errors=True)
                tmp.mkdir(parents=True, exist_ok=True)
                ok, info = _clone_or_download(repo, branch, tmp, "")
                if ok:
                    # Keep bot.log if present
                    old_log = bot_dir / "bot.log"
                    log_backup = None
                    if old_log.exists():
                        log_backup = old_log.read_bytes()
                    shutil.rmtree(bot_dir, ignore_errors=True)
                    tmp.rename(bot_dir)
                    if log_backup is not None:
                        (bot_dir / "bot.log").write_bytes(log_backup)
                    flash("Updated from GitHub (ZIP). Restart the bot to apply.", "success")
                else:
                    shutil.rmtree(tmp, ignore_errors=True)
                    flash(f"Update failed: {info}", "error")
            if was_running:
                start_bot(bot_id)
    else:
        flash("Unknown action.", "error")
    return redirect(url_for("bot_detail", bot_id=bot_id))


# ── Upload ───────────────────────────────────────────────────────
@app.route("/upload", methods=["GET", "POST"])
@login_required
def upload():
    if request.method == "POST":
        f = request.files.get("botfile")
        name = (request.form.get("bot_name") or "").strip()
        if not f or not f.filename:
            flash("Please choose a file.", "error")
            return render_template("upload.html")

        filename = secure_filename(f.filename)
        ext = Path(filename).suffix.lower()
        if ext not in ALLOWED_EXT:
            flash("Only .py, .js, .zip allowed.", "error")
            return render_template("upload.html")

        bot_id = uid()
        bot_dir = SANDBOX_DIR / bot_id
        bot_dir.mkdir(parents=True, exist_ok=True)

        saved = bot_dir / filename
        f.save(str(saved))

        if ext == ".zip":
            try:
                with zipfile.ZipFile(saved, "r") as z:
                    for member in z.namelist():
                        if member.startswith("/") or ".." in member:
                            shutil.rmtree(bot_dir, ignore_errors=True)
                            flash("Unsafe zip rejected (path traversal).", "error")
                            return render_template("upload.html")
                    z.extractall(bot_dir)
                saved.unlink(missing_ok=True)
            except Exception as e:
                shutil.rmtree(bot_dir, ignore_errors=True)
                flash(f"Zip extract failed: {e}", "error")
                return render_template("upload.html")

        verdict, threats = quick_scan(bot_dir)
        if verdict == "DANGEROUS":
            shutil.rmtree(bot_dir, ignore_errors=True)
            flash(f"Upload rejected (security): {', '.join(threats[:3])}", "error")
            return render_template("upload.html")

        kind, entry = detect_entry(bot_dir)
        if not kind:
            shutil.rmtree(bot_dir, ignore_errors=True)
            flash("No entry file found (bot.py / main.py / index.js).", "error")
            return render_template("upload.html")

        display = name or Path(filename).stem
        d = db_load()
        d["bots"][bot_id] = {
            "name": safe_name(display),
            "owner": session["user_id"],
            "dir": str(bot_dir),
            "kind": kind,
            "entry": entry,
            "source": "upload",
            "status": "stopped",
            "created": now_iso(),
            "scan": verdict,
        }
        db_save(d)
        flash(f"Bot «{display}» uploaded ({verdict}). Start it from My Bots.", "success")
        return redirect(url_for("bot_detail", bot_id=bot_id))

    return render_template("upload.html")


# ── GitHub helpers (works WITHOUT system git) ────────────────────
def _parse_github_repo(url: str) -> Optional[Tuple[str, str]]:
    """Extract (owner, repo) from a github.com URL."""
    url = url.strip().rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]
    m = re.search(r"github\.com[/:]([^/\s]+)/([^/\s?#]+)", url, re.I)
    if not m:
        return None
    return m.group(1), m.group(2)


def _download_github_zip(owner: str, repo: str, branch: str, dest_dir: Path,
                         token: str = "") -> Tuple[bool, str]:
    """Download repo as ZIP via GitHub archive API — no git required."""
    import urllib.request
    import io as _io

    headers = {
        "User-Agent": "MD-Shanwaj-Hosting/1.0",
        "Accept": "application/vnd.github+json",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    # Try requested branch, then main, then master
    branches_to_try = [branch]
    for alt in ("main", "master"):
        if alt not in branches_to_try:
            branches_to_try.append(alt)

    last_err = "Unknown error"
    for br in branches_to_try:
        zip_url = f"https://codeload.github.com/{owner}/{repo}/zip/refs/heads/{br}"
        # Fallback API URL if codeload fails
        api_url = f"https://api.github.com/repos/{owner}/{repo}/zipball/{br}"
        for url in (zip_url, api_url):
            try:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=90) as resp:
                    data = resp.read()
                if len(data) < 100:
                    last_err = "Empty response from GitHub"
                    continue
                # Extract zip into dest_dir
                with zipfile.ZipFile(_io.BytesIO(data)) as zf:
                    for member in zf.namelist():
                        if member.startswith("/") or ".." in member:
                            return False, "Unsafe path in archive"
                    zf.extractall(dest_dir)
                # GitHub zips put files in owner-repo-hash/ — flatten one level
                children = [c for c in dest_dir.iterdir() if c.name != ".git"]
                if len(children) == 1 and children[0].is_dir():
                    nested = children[0]
                    for item in nested.iterdir():
                        target = dest_dir / item.name
                        if target.exists():
                            if target.is_dir():
                                shutil.rmtree(target, ignore_errors=True)
                            else:
                                target.unlink(missing_ok=True)
                        shutil.move(str(item), str(target))
                    try:
                        nested.rmdir()
                    except Exception:
                        pass
                return True, br
            except Exception as e:
                last_err = str(e)
                continue
    return False, last_err


def _git_available() -> bool:
    try:
        r = subprocess.run(["git", "--version"], capture_output=True, timeout=5)
        return r.returncode == 0
    except Exception:
        return False


def _clone_or_download(repo_url: str, branch: str, bot_dir: Path,
                       gh_token: str = "") -> Tuple[bool, str]:
    """Prefer git clone; fall back to ZIP download if git missing."""
    parsed = _parse_github_repo(repo_url)
    if not parsed:
        return False, "Invalid GitHub URL"

    owner, repo = parsed
    clean_url = f"https://github.com/{owner}/{repo}"

    # 1) Try git if available
    if _git_available():
        clone_url = clean_url + ".git"
        if gh_token:
            clone_url = clone_url.replace(
                "https://", f"https://x-access-token:{gh_token}@"
            )
        try:
            r = subprocess.run(
                ["git", "clone", "--depth", "1", "--branch", branch, clone_url, str(bot_dir)],
                capture_output=True, text=True, timeout=90,
            )
            if r.returncode != 0:
                shutil.rmtree(bot_dir, ignore_errors=True)
                bot_dir.mkdir(parents=True, exist_ok=True)
                r = subprocess.run(
                    ["git", "clone", "--depth", "1", clone_url, str(bot_dir)],
                    capture_output=True, text=True, timeout=90,
                )
            if r.returncode == 0:
                children = [c for c in bot_dir.iterdir() if c.name != ".git"]
                if len(children) == 1 and children[0].is_dir():
                    nested = children[0]
                    for item in nested.iterdir():
                        shutil.move(str(item), str(bot_dir / item.name))
                    try:
                        nested.rmdir()
                    except Exception:
                        pass
                return True, "git"
            # git failed — fall through to ZIP
        except Exception:
            pass

    # 2) ZIP download (no git needed)
    bot_dir.mkdir(parents=True, exist_ok=True)
    ok, info = _download_github_zip(owner, repo, branch, bot_dir, gh_token)
    if ok:
        return True, f"zip:{info}"
    return False, info


# ── GitHub host ──────────────────────────────────────────────────
@app.route("/github", methods=["GET", "POST"])
@login_required
def github_host():
    if request.method == "POST":
        repo_url = (request.form.get("repo_url") or "").strip()
        branch = (request.form.get("branch") or "main").strip() or "main"
        gh_token = (request.form.get("gh_token") or "").strip()
        name = (request.form.get("bot_name") or "").strip()

        if not repo_url or "github.com" not in repo_url.lower():
            flash("Enter a valid GitHub repository URL.", "error")
            return render_template("github.html")

        repo_url = repo_url.rstrip("/")
        bot_id = uid()
        bot_dir = SANDBOX_DIR / bot_id

        try:
            if bot_dir.exists():
                shutil.rmtree(bot_dir, ignore_errors=True)
            bot_dir.mkdir(parents=True, exist_ok=True)
            ok, info = _clone_or_download(repo_url, branch, bot_dir, gh_token)
            if not ok:
                shutil.rmtree(bot_dir, ignore_errors=True)
                flash(f"Deploy failed: {info}", "error")
                return render_template("github.html")
        except Exception as e:
            shutil.rmtree(bot_dir, ignore_errors=True)
            flash(f"Deploy error: {e}", "error")
            return render_template("github.html")

        verdict, threats = quick_scan(bot_dir)
        if verdict == "DANGEROUS":
            shutil.rmtree(bot_dir, ignore_errors=True)
            flash(f"Repo rejected (security): {', '.join(threats[:3])}", "error")
            return render_template("github.html")

        kind, entry = detect_entry(bot_dir)
        if not kind:
            shutil.rmtree(bot_dir, ignore_errors=True)
            flash("No entry file in repo (bot.py / main.py / index.js).", "error")
            return render_template("github.html")

        display = name or repo_url.rstrip("/").split("/")[-1].replace(".git", "")
        d = db_load()
        d["bots"][bot_id] = {
            "name": safe_name(display),
            "owner": session["user_id"],
            "dir": str(bot_dir),
            "kind": kind,
            "entry": entry,
            "source": "github",
            "repo_url": repo_url,
            "branch": branch,
            "status": "stopped",
            "created": now_iso(),
            "scan": verdict,
            "deploy_method": info,
        }
        db_save(d)
        flash(f"GitHub bot «{display}» ready ({verdict}). Start it from My Bots.", "success")
        return redirect(url_for("bot_detail", bot_id=bot_id))

    return render_template("github.html")


# ── Plans / Profile / Admin ──────────────────────────────────────
@app.route("/plans")
@login_required
def plans():
    user = current_user() or {}
    return render_template("plans.html", plans=PLANS, current_plan=user.get("plan", "free"))


@app.route("/plans/<plan_key>", methods=["POST"])
@login_required
def select_plan(plan_key: str):
    if plan_key not in PLANS:
        flash("Unknown plan.", "error")
        return redirect(url_for("plans"))
    d = db_load()
    u = d["users"].get(session["user_id"])
    if u:
        u["plan"] = plan_key
        db_save(d)
        flash(f"Plan set to {PLANS[plan_key]['name']}.", "success")
    return redirect(url_for("plans"))


@app.route("/profile")
@login_required
def profile():
    user = current_user()
    if not user:
        return redirect(url_for("login"))
    return render_template("profile.html", user=user)


@app.route("/admin")
@admin_required
def admin():
    d = db_load()
    users = []
    for u in d["users"].values():
        uc = dict(u)
        uc.pop("password", None)
        uc["bot_count"] = sum(1 for b in d["bots"].values() if str(b.get("owner")) == str(u["id"]))
        users.append(uc)
    users.sort(key=lambda x: x.get("joined", ""), reverse=True)
    running = sum(1 for bid in d["bots"] if _is_running(bid))
    stats = {"users": len(d["users"]), "bots": len(d["bots"]), "running": running}
    return render_template("admin.html", users=users, stats=stats)


# ── Health ───────────────────────────────────────────────────────

# ── Live status / logs API (smooth dashboard) ────────────────────
@app.route("/api/bots/<bot_id>/status")
@login_required
def api_bot_status(bot_id: str):
    d = db_load()
    b = d["bots"].get(bot_id)
    if not b:
        return jsonify({"ok": False, "error": "not found"}), 404
    if str(b.get("owner")) != str(session["user_id"]) and not session.get("is_admin"):
        return jsonify({"ok": False, "error": "forbidden"}), 403
    running = _is_running(bot_id)
    uptime = 0
    with _run_lock:
        info = RUNNING.get(bot_id)
        if info and running:
            uptime = int(time.time() - info.get("started", time.time()))
    return jsonify({
        "ok": True,
        "id": bot_id,
        "name": b.get("name"),
        "running": running,
        "status": "running" if running else "stopped",
        "uptime_s": uptime,
        "source": b.get("source"),
        "kind": b.get("kind"),
        "entry": b.get("entry"),
    })


@app.route("/api/bots/<bot_id>/logs")
@login_required
def api_bot_logs(bot_id: str):
    d = db_load()
    b = d["bots"].get(bot_id)
    if not b:
        return jsonify({"ok": False, "error": "not found"}), 404
    if str(b.get("owner")) != str(session["user_id"]) and not session.get("is_admin"):
        return jsonify({"ok": False, "error": "forbidden"}), 403
    lines = int(request.args.get("lines", 150) or 150)
    lines = max(20, min(lines, 500))
    return jsonify({
        "ok": True,
        "running": _is_running(bot_id),
        "logs": _read_log(bot_id, max_lines=lines),
    })


@app.route("/api/bots/summary")
@login_required
def api_bots_summary():
    bots = get_user_bots(session["user_id"])
    return jsonify({
        "ok": True,
        "total": len(bots),
        "running": sum(1 for b in bots if b.get("running")),
        "bots": [
            {"id": b["id"], "name": b.get("name"), "running": bool(b.get("running")),
             "source": b.get("source"), "status": b.get("status")}
            for b in bots[:50]
        ],
    })



@app.route("/api/bots/<bot_id>/install-deps", methods=["POST"])
@login_required
def api_bot_install_deps(bot_id: str):
    d = db_load()
    b = d["bots"].get(bot_id)
    if not b:
        return jsonify({"ok": False, "error": "not found"}), 404
    if str(b.get("owner")) != str(session["user_id"]) and not session.get("is_admin"):
        return jsonify({"ok": False, "error": "forbidden"}), 403
    bot_dir = Path(b.get("dir", ""))
    logs = []
    def lw(msg):
        logs.append(msg)
    if bot_dir.exists():
        _auto_install_deps(bot_dir, lw)
    return jsonify({"ok": True, "log": "".join(logs)})


@app.route("/health")
def health():
    return {"ok": True, "brand": "MD Shanwaj Hosting"}, 200




if __name__ == "__main__":
    import socket

    def _free_port(preferred: int) -> int:
        """Use preferred port, or next free one if busy."""
        for p in range(preferred, preferred + 20):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    s.bind(("0.0.0.0", p))
                    return p
                except OSError:
                    continue
        return preferred

    preferred = int(os.environ.get("PORT", 5000))
    port = _free_port(preferred)
    if port != preferred:
        print(f"[port] {preferred} busy — using {port}")
    print(f"✦ MD Shanwaj Hosting — http://0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
