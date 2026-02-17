
import base64
import hashlib
import json
import os
import re
import secrets
import stat
import traceback
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, unquote, quote_plus

import paramiko
import psycopg
from psycopg.rows import dict_row

# =========================
# Heroku runtime guard
# =========================
# Heroku Python buildpack may set WEB_CONCURRENCY>1 automatically.
# NiceGUI (uvicorn) cannot start multiple workers when launched via `python main.py`,
# because uvicorn requires an import string for multi-worker/reload mode.
# To avoid dyno crash (H10 / 503), force single worker on Heroku.
if os.getenv("DYNO"):
    try:
        _wc = int(os.getenv("WEB_CONCURRENCY", "1"))
    except Exception:
        _wc = 1
    if _wc > 1:
        os.environ["WEB_CONCURRENCY"] = "1"

from nicegui import app, ui


# =========================
# Global (Japan time)
# =========================

JST = timezone(timedelta(hours=9))


def now_jst_iso() -> str:
    """現在時刻（日本時間）をISO文字列で返す（秒まで）。"""
    return datetime.now(JST).replace(microsecond=0).isoformat()


def parse_iso_datetime(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        v = str(value).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(v)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def to_jst(dt: datetime) -> datetime:
    try:
        return dt.astimezone(JST)
    except Exception:
        return dt


def fmt_jst(value, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """datetime/ISO文字列を日本時間で表示用フォーマットにする。"""
    if value is None:
        return ""
    if isinstance(value, datetime):
        return to_jst(value).strftime(fmt)
    dt = parse_iso_datetime(str(value))
    if dt:
        return to_jst(dt).strftime(fmt)
    return str(value)


def sanitize_error_text(text: str) -> str:
    """例外メッセージにURL等が混じっても画面に出さないための簡易マスク。"""
    if not text:
        return ""
    s = str(text)
    s = re.sub(r"[a-zA-Z][a-zA-Z0-9+.-]*://[^\s]+", "[REDACTED_URL]", s)
    # ついでに長すぎるのも切る
    if len(s) > 300:
        s = s[:300] + "…"
    return s


# =========================
# Global UI styles (v0.6.1)
# =========================

def inject_global_styles() -> None:
    """全ページ共通の見た目（左右分割/カード/選択UI）を安定させるCSS。
    - flex-wrap だと「ちょっと足りない」時に右が下へ落ちて空白ができやすい
    - grid + minmax で「入るなら左右、無理なら縦」に安定させる
    """
    ui.add_head_html(
        """
<style>
  /* ====== Page base ====== */
  .cvhb-page {
    background: #f5f5f5;
    min-height: calc(100vh - 64px);
  }
  .cvhb-container {
    max-width: 1680px;
    margin: 0 auto;
    padding: 16px;
  }

  /* ====== Split layout (PC builder) ====== */
  .cvhb-split {
    display: grid;
    grid-template-columns: minmax(360px, 620px) minmax(360px, 1fr);
    gap: 16px;
    align-items: start;
  }
  .cvhb-left-col,
  .cvhb-right-col {
    width: 100%;
  }

  /* 右プレビューはデスクトップ時に追従 */
  @media (min-width: 761px) {
    .cvhb-right-col {
      position: sticky;
      top: 88px;
      align-self: start;
    }
  }

  /* スマホ：縦並び */
  @media (max-width: 760px) {
    .cvhb-container { padding: 8px; }
    .cvhb-split { grid-template-columns: 1fr; }
    .cvhb-right-col { position: static; }
  }

  /* ====== Common ====== */
  .cvhb-card-title {
    font-weight: 700;
    letter-spacing: .02em;
  }
  .cvhb-muted {
    color: rgba(0,0,0,.60);
    font-size: 12px;
  }

  /* 左カラム：カード同士の間隔 */
  .cvhb-left-stack {
    display: flex;
    flex-direction: column;
    gap: 14px;
  }

  /* ====== Step menu (left) ====== */
  .cvhb-step-tabs { width: 100%; }
  .cvhb-step-tabs .q-tabs__content { align-items: stretch; }
  .cvhb-step-tabs .q-tab {
    width: 100%;
    justify-content: flex-start;
    text-align: left;
    border-radius: 12px;
    margin: 0 0 8px 0;
    padding: 10px 12px;
    background: rgba(0,0,0,.03);
    border: 1px solid rgba(0,0,0,.10);
  }
  .cvhb-step-tabs .q-tab__content { justify-content: flex-start; }
  .cvhb-step-tabs .q-tab__label {
    white-space: normal;
    line-height: 1.3;
  }
  .cvhb-step-tabs .q-tab--active {
    background: rgba(25,118,210,0.08);
    border-color: rgba(25,118,210,0.35);
    font-weight: 700;
  }

  /* ====== Step3 block tabs ====== */
  .cvhb-block-tabs .q-tabs__content { flex-wrap: wrap; }
  .cvhb-block-tabs .q-tab {
    min-height: 34px;
    padding: 0 12px;
  }
  .cvhb-block-tabs .q-tab__label {
    white-space: normal;
    line-height: 1.2;
  }

  /* ====== Choice cards (industry/color) ====== */
  .cvhb-choice {
    border-radius: 12px;
    transition: transform .08s ease, box-shadow .08s ease, border-color .08s ease;
    cursor: pointer;
  }
  .cvhb-choice:hover {
    transform: translateY(-1px);
    box-shadow: 0 6px 20px rgba(0,0,0,.08);
  }
  .cvhb-choice.is-selected {
    border: 2px solid var(--q-primary);
    background: rgba(25,118,210,0.06);
  }
  .cvhb-swatch {
    width: 18px;
    height: 18px;
    border-radius: 4px;
    border: 1px solid rgba(0,0,0,.20);
    display: inline-block;
  }

  /* ====== Projects page ====== */
  .cvhb-projects-actions {
    display: flex;
    align-items: flex-end;
    justify-content: space-between;
    gap: 12px;
    flex-wrap: wrap;
  }
  .cvhb-project-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
    gap: 16px;
  }
  .cvhb-project-card { height: 100%; }
  .cvhb-project-meta {
    font-size: 12px;
    color: rgba(0,0,0,.60);
    line-height: 1.4;
  }

  /* ====== Preview inside builder ====== */
  .cvhb-preview .q-card { width: 100%; }
/* ====== Preview PC（実サイト寄り） ====== */
.cvhb-preview-pc { background: #f5f5f5; }
.cvhb-pc-header {
  position: sticky;
  top: 0;
  z-index: 10;
  border-bottom: 1px solid rgba(0,0,0,.10);
}
.cvhb-pc-container {
  max-width: 1100px;
  margin: 0 auto;
  padding: 16px 24px;
}
.cvhb-pc-logo {
  font-weight: 800;
  letter-spacing: .02em;
}
.cvhb-pc-hero {
  height: 380px;
  background-size: cover;
  background-position: center;
}
.cvhb-pc-hero-overlay {
  height: 100%;
  background: rgba(0,0,0,.52);
}
.cvhb-pc-hero-inner {
  height: 100%;
  display: flex;
  flex-direction: column;
  justify-content: flex-end;
  padding-bottom: 40px;
}
.cvhb-pc-section { padding: 28px 0; }
.cvhb-pc-grid-2 {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 16px;
}
.cvhb-pc-grid-2-uneven {
  display: grid;
  grid-template-columns: minmax(0, 2fr) minmax(0, 1fr);
  gap: 16px;
}
.cvhb-pc-card { width: 100%; border-radius: 14px; }

@media (max-width: 900px) {
  .cvhb-pc-container { padding: 16px; }
  .cvhb-pc-hero { height: 320px; }
  .cvhb-pc-grid-2,
  .cvhb-pc-grid-2-uneven { grid-template-columns: 1fr; }
}


  /* ====== Preview (Glassmorphism) ====== */
  /* [BLK-02] Preview CSS: Glassmorphism theme */
  .cvhb-preview-glass {
    /* CSS variables are set inline from Python for each project (accent/background) */
    background: linear-gradient(135deg, var(--pv-bg1, #f8fafc), var(--pv-bg2, #eef2ff));
    color: var(--pv-text, #0b1220);
  }

  .cvhb-preview-glass .pv-topbar {
    position: sticky;
    top: 0;
    z-index: 20;
    background: rgba(255,255,255,.58);
    border-bottom: 1px solid rgba(255,255,255,.68);
    backdrop-filter: blur(14px);
  }
  .cvhb-preview-glass .pv-topbar-inner {
    height: 48px;
    padding: 0 12px;
    display: flex;
    align-items: center;
    justify-content: space-between;
  }
  .cvhb-preview-glass .pv-topbar-title {
    font-weight: 800;
    font-size: 14px;
    letter-spacing: .02em;
  }

  .cvhb-preview-glass .pv-hero {
    border-radius: 18px;
    overflow: hidden;
    box-shadow: 0 18px 40px rgba(15,23,42,.12);
  }
  .cvhb-preview-glass .pv-hero-overlay {
    height: 100%;
    background: linear-gradient(180deg, rgba(0,0,0,.18), rgba(0,0,0,.58));
  }

  .cvhb-preview-glass .pv-section {
    padding: 16px;
  }

  .cvhb-preview-glass .pv-card,
  .cvhb-preview-glass .pv-section .q-card {
    background: rgba(255,255,255,.70);
    border: 1px solid rgba(255,255,255,.64);
    border-radius: 16px;
    box-shadow: 0 16px 40px rgba(15,23,42,.10);
    backdrop-filter: blur(14px);
  }
  .cvhb-preview-glass .pv-muted {
    color: rgba(15,23,42,.66);
  }

  .cvhb-preview-glass .pv-pill {
    border: 1px solid rgba(15,23,42,.10);
    background: rgba(255,255,255,.55);
    padding: 4px 10px;
    border-radius: 999px;
  }

  .cvhb-preview-glass .pv-btn-primary {
    background: var(--pv-accent, #1976d2) !important;
    color: var(--pv-accent-contrast, #ffffff) !important;
    border-radius: 999px;
    box-shadow: 0 12px 24px rgba(15,23,42,.18);
  }
  .cvhb-preview-glass .pv-btn-secondary {
    background: rgba(255,255,255,.30) !important;
    border: 1px solid rgba(255,255,255,.74) !important;
    color: var(--pv-text, #0b1220) !important;
    border-radius: 999px;
  }
  .cvhb-preview-glass .pv-linkbtn {
    color: var(--pv-text, #0b1220) !important;
  }

  /* ====== PC preview (Glass override) ====== */
  .cvhb-preview-glass.cvhb-preview-pc {
    background: linear-gradient(135deg, var(--pv-bg1, #f8fafc), var(--pv-bg2, #eef2ff));
  }
  .cvhb-preview-glass .cvhb-pc-header {
    background: rgba(255,255,255,.60);
    border-bottom: 1px solid rgba(255,255,255,.72);
    backdrop-filter: blur(14px);
  }
  .cvhb-preview-glass .cvhb-pc-hero-overlay {
    background: linear-gradient(180deg, rgba(0,0,0,.20), rgba(0,0,0,.64));
  }
  .cvhb-preview-glass .cvhb-pc-section {
    padding: 44px 0;
  }
  .cvhb-preview-glass .cvhb-pc-card {
    background: rgba(255,255,255,.74);
    border: 1px solid rgba(255,255,255,.66);
    box-shadow: 0 16px 40px rgba(15,23,42,.10);
    backdrop-filter: blur(14px);
  }
  .cvhb-preview-glass .pv-nav-btn {
    border-radius: 999px;
    background: rgba(255,255,255,.10);
  }
  .cvhb-preview-glass .pv-footer {
    margin-top: 24px;
    background: rgba(255,255,255,.60);
    border-top: 1px solid rgba(255,255,255,.72);
    backdrop-filter: blur(14px);
  }

</style>
"""
    )
# =========================
# Config
# =========================

def read_text_file(path: str, default: str = "") -> str:
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except Exception:
        return default


VERSION = read_text_file("VERSION", "0.0.0")
APP_ENV = (os.getenv("APP_ENV") or "prod").lower().strip()

STORAGE_SECRET = os.getenv("STORAGE_SECRET")
if not STORAGE_SECRET:
    raise RuntimeError("STORAGE_SECRET が未設定です。HerokuのConfig Varsに追加してください。")

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL が未設定です。Heroku Postgres を追加してください。")

# 一部の環境で postgres:// が来る場合があるので正規化
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

SFTP_BASE_DIR = (os.getenv("SFTP_BASE_DIR") or "/cvhb").rstrip("/")
SFTPTOGO_URL = os.getenv("SFTPTOGO_URL")
if not SFTPTOGO_URL:
    raise RuntimeError("SFTPTOGO_URL が未設定です。HerokuのConfig Varsに追加してください。")

SFTP_PROJECTS_DIR = f"{SFTP_BASE_DIR}/projects"


# =========================
# Small utils
# =========================

def google_maps_url(address: str) -> str:
    address = (address or "").strip()
    if not address:
        return ""
    return f"https://www.google.com/maps/search/?api=1&query={quote_plus(address)}"


# =========================
# DB helpers
# =========================

def db_connect() -> psycopg.Connection:
    conn = psycopg.connect(DATABASE_URL, sslmode="require")
    conn.autocommit = True
    return conn


def db_execute(sql: str, params: Optional[tuple] = None) -> None:
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)


def db_fetchone(sql: str, params: Optional[tuple] = None) -> Optional[dict]:
    with db_connect() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            return dict(row) if row else None


def db_fetchall(sql: str, params: Optional[tuple] = None) -> list[dict]:
    with db_connect() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
            return [dict(r) for r in rows]


def init_db_schema() -> None:
    db_execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id BIGSERIAL PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL,
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )
    db_execute(
        """
        CREATE TABLE IF NOT EXISTS audit_logs (
            id BIGSERIAL PRIMARY KEY,
            user_id BIGINT NULL REFERENCES users(id),
            username TEXT NULL,
            role TEXT NULL,
            action TEXT NOT NULL,
            details TEXT NOT NULL DEFAULT '{}',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )
    db_execute("CREATE INDEX IF NOT EXISTS idx_audit_logs_created_at ON audit_logs(created_at DESC);")


# =========================
# Password hashing (PBKDF2)
# =========================

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    iterations = 210_000
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "pbkdf2_sha256${}${}${}".format(
        iterations,
        base64.b64encode(salt).decode("utf-8"),
        base64.b64encode(dk).decode("utf-8"),
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, b64_salt, b64_hash = stored.split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        iterations = int(iters)
        salt = base64.b64decode(b64_salt.encode("utf-8"))
        expected = base64.b64decode(b64_hash.encode("utf-8"))
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
        return secrets.compare_digest(dk, expected)
    except Exception:
        return False


# =========================
# Users / Auth
# =========================

@dataclass
class User:
    id: int
    username: str
    role: str


def get_user_by_username(username: str) -> Optional[dict]:
    return db_fetchone("SELECT * FROM users WHERE username = %s AND is_active = TRUE", (username,))


def count_users() -> int:
    row = db_fetchone("SELECT COUNT(*) AS cnt FROM users", None)
    return int(row["cnt"]) if row else 0


def create_user(username: str, password: str, role: str) -> None:
    pw_hash = hash_password(password)
    db_execute(
        """
        INSERT INTO users (username, password_hash, role)
        VALUES (%s, %s, %s)
        ON CONFLICT (username) DO NOTHING
        """,
        (username, pw_hash, role),
    )


def log_action(user: Optional[User], action: str, details: str = "{}") -> None:
    if user:
        db_execute(
            """
            INSERT INTO audit_logs (user_id, username, role, action, details)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (user.id, user.username, user.role, action, details),
        )
    else:
        db_execute(
            """
            INSERT INTO audit_logs (user_id, username, role, action, details)
            VALUES (NULL, NULL, NULL, %s, %s)
            """,
            (action, details),
        )


def safe_log_action(user: Optional[User], action: str, details: str = "{}") -> None:
    try:
        log_action(user, action, details)
    except Exception as e:
        # ここでURL等が出る可能性があるので、画面には出さない
        print(f"[audit_log] failed: {sanitize_error_text(e)}")


def current_user() -> Optional[User]:
    try:
        uid = app.storage.user.get("user_id")
        username = app.storage.user.get("username")
        role = app.storage.user.get("role")
        if uid and username and role:
            return User(id=int(uid), username=str(username), role=str(role))
        return None
    except Exception:
        return None


def set_logged_in(user_row: dict) -> None:
    app.storage.user["user_id"] = int(user_row["id"])
    app.storage.user["username"] = str(user_row["username"])
    app.storage.user["role"] = str(user_row["role"])


def cleanup_user_storage() -> None:
    """旧バージョンで入れていた大きいデータが残ると、表示がおかしくなったり500になることがあるため掃除。"""
    try:
        # 旧キー（大きい辞書を入れていた場合）
        app.storage.user.pop("project", None)
    except Exception:
        pass


def navigate_to(path: str) -> None:
    safe_path = (path or "/").replace("'", "\'")
    try:
        ui.navigate.to(path)
        return
    except Exception:
        pass
    try:
        ui.open(path)
        return
    except Exception:
        pass
    try:
        ui.run_javascript(f"window.location.href='{safe_path}'")
    except Exception:
        pass


# =========================
# Session / Project state (avoid storing big dict in cookie)
# =========================

PROJECT_CACHE: dict[int, dict] = {}


def clear_current_project(user: Optional[User]) -> None:
    try:
        app.storage.user.pop("current_project_id", None)
        app.storage.user.pop("current_project_name", None)
        app.storage.user.pop("project", None)  # 念のため
    except Exception:
        pass
    if user:
        PROJECT_CACHE.pop(user.id, None)


def logout() -> None:
    u = current_user()
    if u:
        safe_log_action(u, "logout")
        clear_current_project(u)
    app.storage.user.clear()
    navigate_to("/")


def ensure_stg_test_users() -> tuple[bool, str]:
    if APP_ENV != "stg":
        return (False, "not stg")
    pwd = os.getenv("STG_TEST_PASSWORD")
    if not pwd:
        return (False, "STG_TEST_PASSWORD が未設定です（stgのみ必要）")
    create_user("admin_test", pwd, "admin")
    create_user("subadmin_test", pwd, "subadmin")
    for i in range(1, 6):
        create_user(f"user{i:02d}", pwd, "user")
    return (True, "stg test users seeded")


# =========================
# SFTP (SFTP To Go)
# =========================

def parse_sftp_url(url: str) -> tuple[str, int, str, str]:
    u = urlparse(url)
    if u.scheme not in {"sftp"}:
        raise RuntimeError("SFTPTOGO_URL の scheme が sftp ではありません")
    host = u.hostname or ""
    port = u.port or 22
    user = unquote(u.username or "")
    pwd = unquote(u.password or "")
    if not host or not user or not pwd:
        raise RuntimeError("SFTPTOGO_URL から host/user/password を取得できません")
    return host, port, user, pwd


@contextmanager
def sftp_client():
    host, port, user, pwd = parse_sftp_url(SFTPTOGO_URL)
    transport = paramiko.Transport((host, port))
    try:
        transport.connect(username=user, password=pwd)
        sftp = paramiko.SFTPClient.from_transport(transport)
        yield sftp
    finally:
        try:
            transport.close()
        except Exception:
            pass


def sftp_mkdirs(sftp: paramiko.SFTPClient, remote_dir: str) -> None:
    remote_dir = remote_dir.rstrip("/")
    if remote_dir == "":
        return
    parts = remote_dir.strip("/").split("/")
    path = ""
    for p in parts:
        path = f"{path}/{p}"
        try:
            sftp.stat(path)
        except Exception:
            try:
                sftp.mkdir(path)
            except Exception:
                pass


def sftp_write_text(sftp: paramiko.SFTPClient, remote_path: str, text: str) -> None:
    remote_dir = "/".join(remote_path.split("/")[:-1])
    sftp_mkdirs(sftp, remote_dir)
    with sftp.open(remote_path, "w") as f:
        f.write(text)


def sftp_read_text(sftp: paramiko.SFTPClient, remote_path: str) -> str:
    with sftp.open(remote_path, "r") as f:
        return f.read()


def sftp_list_dirs(sftp: paramiko.SFTPClient, remote_dir: str) -> list[str]:
    try:
        items = sftp.listdir_attr(remote_dir)
    except Exception:
        return []
    dirs = []
    for it in items:
        if stat.S_ISDIR(it.st_mode):
            dirs.append(it.filename)
    return sorted(dirs)


# =========================
# Projects (v0.6.1)
# =========================

INDUSTRY_PRESETS = [
    {
        "value": "会社サイト（企業）",
        "label": "会社・企業サイト",
        "features": "特徴：6ブロック（ヒーロー / 理念 / お知らせ / FAQ / アクセス / お問い合わせ）",
    },
    {
        "value": "福祉事業所",
        "label": "福祉事業所",
        "features": "特徴：準備中（次のバージョンで拡張予定）",
    },
    {
        "value": "個人事業",
        "label": "個人事業",
        "features": "特徴：準備中（次のバージョンで拡張予定）",
    },
    {
        "value": "その他",
        "label": "その他",
        "features": "特徴：準備中（次のバージョンで拡張予定）",
    },
]
INDUSTRY_OPTIONS = [x["value"] for x in INDUSTRY_PRESETS]

COLOR_PRESETS = [
    {"value": "blue", "label": "青", "impression": "信頼感"},
    {"value": "red", "label": "赤", "impression": "情熱"},
    {"value": "green", "label": "緑", "impression": "安心感"},
    {"value": "orange", "label": "オレンジ", "impression": "親しみ"},
    {"value": "purple", "label": "紫", "impression": "上品"},
    {"value": "grey", "label": "灰", "impression": "落ち着き"},
    {"value": "black", "label": "黒", "impression": "高級感"},
    {"value": "white", "label": "白", "impression": "清潔感"},
    {"value": "yellow", "label": "黄", "impression": "明るさ"},
]
COLOR_OPTIONS = [x["value"] for x in COLOR_PRESETS]

# 色スウォッチ用（だいたいのイメージ色）
COLOR_HEX = {
    "blue": "#1976d2",
    "red": "#c62828",
    "green": "#2e7d32",
    "orange": "#ef6c00",
    "purple": "#6a1b9a",
    "grey": "#546e7a",
    "black": "#212121",
    "white": "#ffffff",
    "yellow": "#f9a825",
}

# 旧データの色名が来ても崩れないように吸収
COLOR_MIGRATION = {
    "indigo": "blue",
    "teal": "green",
    "deep-orange": "orange",
}


# =========================
# Blocks / Template presets (v0.6.1)
# =========================

HERO_IMAGE_PRESET_URLS = {
    "A: オフィス": "https://images.unsplash.com/photo-1486406146926-c627a92ad1ab?auto=format&fit=crop&w=1200&q=80",
    "B: チーム": "https://images.unsplash.com/photo-1521737604893-d14cc237f11d?auto=format&fit=crop&w=1200&q=80",
    "C: 街並み": "https://images.unsplash.com/photo-1504384308090-c894fdcc538d?auto=format&fit=crop&w=1200&q=80",
}
HERO_IMAGE_OPTIONS = list(HERO_IMAGE_PRESET_URLS.keys())


def project_dir(project_id: str) -> str:
    return f"{SFTP_PROJECTS_DIR}/{project_id}"


def project_json_path(project_id: str) -> str:
    return f"{project_dir(project_id)}/project.json"


def new_project_id() -> str:
    ts = datetime.now(JST).strftime("%Y%m%d%H%M%S")
    rnd = secrets.token_hex(3)
    return f"p{ts}_{rnd}"


def normalize_project(p: dict) -> dict:
    """project.json をアプリ内で扱いやすい形に整える（足りない項目を補う）。"""
    if not isinstance(p, dict):
        p = {}

    p["schema_version"] = "0.6.1"
    p.setdefault("project_id", new_project_id())
    p.setdefault("project_name", "(no name)")

    # 旧データがUTCでも、ここでJSTへ寄せる（表示も保存もブレないように）
    created_raw = p.get("created_at") or now_jst_iso()
    updated_raw = p.get("updated_at") or now_jst_iso()
    created_dt = parse_iso_datetime(str(created_raw)) or datetime.now(JST)
    updated_dt = parse_iso_datetime(str(updated_raw)) or datetime.now(JST)
    p["created_at"] = to_jst(created_dt).replace(microsecond=0).isoformat()
    p["updated_at"] = to_jst(updated_dt).replace(microsecond=0).isoformat()

    p.setdefault("created_by", p.get("created_by") or "")
    p.setdefault("updated_by", p.get("updated_by") or "")

    data = p.setdefault("data", {})
    step1 = data.setdefault("step1", {})
    step2 = data.setdefault("step2", {})
    blocks = data.setdefault("blocks", {})

    # step1
    industry = step1.get("industry", "会社サイト（企業）")
    if industry not in INDUSTRY_OPTIONS:
        industry = "会社サイト（企業）"
    step1["industry"] = industry

    color = step1.get("primary_color", "blue")
    color = COLOR_MIGRATION.get(color, color)
    if color not in COLOR_OPTIONS:
        color = "blue"
    step1["primary_color"] = color

    # step2
    step2.setdefault("company_name", "")
    step2.setdefault("catch_copy", "")
    step2.setdefault("phone", "")
    step2.setdefault("address", "")
    step2.setdefault("email", "")

    # blocks
    hero = blocks.setdefault("hero", {})
    hero.setdefault("sub_catch", "地域に寄り添い、安心できるサービスを届けます")
    hero.setdefault("hero_image", "A: オフィス")
    hero.setdefault("hero_image_url", "")
    hero.setdefault("primary_button_text", "お問い合わせ")
    hero.setdefault("secondary_button_text", "見学・相談")

    philosophy = blocks.setdefault("philosophy", {})
    philosophy.setdefault("title", "私たちの想い")
    philosophy.setdefault("body", "ここに理念や会社の紹介文を書きます。\n（あとで自由に書き換えできます）")
    pts = philosophy.setdefault("points", ["地域密着", "丁寧な対応", "安心の体制"])
    if not isinstance(pts, list):
        pts = ["地域密着", "丁寧な対応", "安心の体制"]
    while len(pts) < 3:
        pts.append("")
    philosophy["points"] = pts[:3]

    news = blocks.setdefault("news", {})
    news_items = news.setdefault(
        "items",
        [
            {
                "date": datetime.now(JST).strftime("%Y-%m-%d"),
                "category": "お知らせ",
                "title": "サンプル：ホームページを公開しました",
                "body": "ここにお知らせ本文を書きます。\n（あとで自由に書き換えできます）",
            }
        ],
    )
    if not isinstance(news_items, list):
        news_items = []
    for it in news_items:
        if not isinstance(it, dict):
            continue
        it.setdefault("date", "")
        it.setdefault("category", "お知らせ")
        it.setdefault("title", "")
        it.setdefault("body", "")
    news["items"] = news_items

    faq = blocks.setdefault("faq", {})
    faq_items = faq.setdefault(
        "items",
        [
            {"q": "サンプル：見学はできますか？", "a": "はい。お電話またはメールでお気軽にご連絡ください。"},
            {"q": "サンプル：費用はどのくらいですか？", "a": "内容により異なります。まずはご要望をお聞かせください。"},
        ],
    )
    if not isinstance(faq_items, list):
        faq_items = []
    for it in faq_items:
        if not isinstance(it, dict):
            continue
        it.setdefault("q", "")
        it.setdefault("a", "")
    faq["items"] = faq_items

    access = blocks.setdefault("access", {})
    access.setdefault("map_url", "")
    access.setdefault("notes", "（例）〇〇駅から徒歩5分 / 駐車場あり")

    contact = blocks.setdefault("contact", {})
    contact.setdefault("hours", "平日 9:00〜18:00")
    contact.setdefault("message", "まずはお気軽にご相談ください。")

    return p


def get_current_project(user: Optional[User]) -> Optional[dict]:
    """現在選択中の案件を返す（Cookieに巨大な辞書を入れず、キャッシュ + 必要ならSFTPから読む）。"""
    if not user:
        return None

    pid = app.storage.user.get("current_project_id")
    if not pid:
        return None

    cached = PROJECT_CACHE.get(user.id)
    if isinstance(cached, dict) and cached.get("project_id") == pid:
        return normalize_project(cached)

    # キャッシュが無い場合だけロード
    try:
        p = load_project_from_sftp(pid, user)
        PROJECT_CACHE[user.id] = p
        app.storage.user["current_project_name"] = p.get("project_name", "")
        cleanup_user_storage()
        return p
    except Exception as e:
        print(f"[project] load failed: {sanitize_error_text(e)}")
        return None


def set_current_project(p: dict, user: Optional[User]) -> None:
    """現在の案件を「選択状態」にする（ID/名前だけをstorageに、実体はキャッシュへ）。"""
    p = normalize_project(p)
    if user:
        PROJECT_CACHE[user.id] = p
        app.storage.user["current_project_id"] = p.get("project_id")
        app.storage.user["current_project_name"] = p.get("project_name", "")
    cleanup_user_storage()


def create_project(name: str, created_by: Optional[User]) -> dict:
    pid = new_project_id()
    p = {
        "schema_version": "0.6.1",
        "project_id": pid,
        "project_name": name,
        "created_at": now_jst_iso(),
        "updated_at": now_jst_iso(),
        "created_by": created_by.username if created_by else "",
        "updated_by": created_by.username if created_by else "",
        "data": {
            "step1": {"industry": "会社サイト（企業）", "primary_color": "blue"},
            "step2": {"company_name": "", "catch_copy": "", "phone": "", "address": "", "email": ""},
            "blocks": {},
        },
    }
    p = normalize_project(p)

    if created_by:
        safe_log_action(created_by, "project_create", details=json.dumps({"project_id": pid, "name": name}, ensure_ascii=False))
    return p


def save_project_to_sftp(p: dict, user: Optional[User]) -> None:
    p = normalize_project(p)
    p["updated_at"] = now_jst_iso()
    if user:
        p["updated_by"] = user.username

    remote = project_json_path(p["project_id"])
    body = json.dumps(p, ensure_ascii=False, indent=2)
    with sftp_client() as sftp:
        sftp_write_text(sftp, remote, body)

    if user:
        safe_log_action(user, "project_save", details=json.dumps({"project_id": p["project_id"]}, ensure_ascii=False))


def load_project_from_sftp(project_id: str, user: Optional[User]) -> dict:
    remote = project_json_path(project_id)
    with sftp_client() as sftp:
        body = sftp_read_text(sftp, remote)
    p = normalize_project(json.loads(body))
    if user:
        safe_log_action(user, "project_load", details=json.dumps({"project_id": project_id}, ensure_ascii=False))
    return p


def list_projects_from_sftp() -> list[dict]:
    projects: list[dict] = []
    with sftp_client() as sftp:
        dirs = sftp_list_dirs(sftp, SFTP_PROJECTS_DIR)
        for d in dirs:
            try:
                body = sftp_read_text(sftp, project_json_path(d))
                p = normalize_project(json.loads(body))
                projects.append({
                    "project_id": p.get("project_id", d),
                    "project_name": p.get("project_name", "(no name)"),
                    "updated_at": p.get("updated_at", ""),
                    "created_at": p.get("created_at", ""),
                    "updated_by": p.get("updated_by", ""),
                })
            except Exception:
                projects.append({"project_id": d, "project_name": "(broken project.json)", "updated_at": "", "created_at": "", "updated_by": ""})

    projects.sort(key=lambda x: x.get("updated_at", ""), reverse=True)
    return projects


# =========================
# UI parts
# =========================

def render_header(u: Optional[User]) -> None:
    with ui.element("div").classes("w-full bg-white shadow-1").style("position: sticky; top: 0; z-index: 1000;"):
        with ui.row().classes("w-full items-center justify-between q-pa-md").style("gap: 12px;"):
            with ui.row().classes("items-center q-gutter-sm"):
                ui.icon("home").classes("text-grey-8")
                ui.label(f"CV-HomeBuilder v{VERSION}").classes("text-h6")

            with ui.row().classes("items-center q-gutter-sm").style("flex-wrap: wrap; justify-content: flex-end;"):
                ui.badge(APP_ENV.upper()).props("outline")
                ui.badge(f"SFTP_BASE_DIR: {SFTP_BASE_DIR}").props("outline")

                pname = app.storage.user.get("current_project_name")
                if pname:
                    ui.badge(f"案件: {str(pname)[:18]}").props("outline")

                if u:
                    ui.badge(f"{u.username} ({u.role})").props("outline")
                    ui.button("案件", on_click=lambda: navigate_to("/projects")).props("flat")
                    if u.role in {"admin", "subadmin"}:
                        ui.button("操作ログ", on_click=lambda: navigate_to("/audit")).props("flat")
                    ui.button("ログアウト", on_click=logout).props("color=negative flat")


def render_login(root_refresh) -> None:
    with ui.element("div").classes("w-full").style("min-height: calc(100vh - 0px); background: #f5f5f5;"):
        with ui.column().classes("w-full items-center q-pa-xl"):
            with ui.card().classes("q-pa-lg rounded-borders").style("width: 520px; max-width: 92vw;").props("bordered"):
                ui.label("ログイン").classes("text-h5 q-mb-md")

                if APP_ENV == "stg":
                    seeded, msg = ensure_stg_test_users()
                    with ui.card().classes("q-pa-md q-mb-md rounded-borders").style("background:#eeeeee;").props("flat bordered"):
                        ui.label("stg（検証環境）テストアカウント").classes("text-subtitle1")
                        if seeded:
                            ui.label("ユーザー名：admin_test / subadmin_test / user01〜user05")
                            ui.label("パスワード：STG_TEST_PASSWORD").classes("text-caption text-grey")
                        else:
                            ui.label("STG_TEST_PASSWORD が未設定のため、テストアカウントは作成されていません。")
                            ui.label(f"理由: {msg}").classes("text-caption text-grey")

                username = ui.input("ユーザー名").props("outlined").classes("w-full")
                password = ui.input("パスワード", password=True, password_toggle_button=True).props("outlined").classes("w-full")

                def do_login() -> None:
                    un = (username.value or "").strip()
                    pw = (password.value or "")
                    if not un or not pw:
                        ui.notify("ユーザー名とパスワードを入力してください", type="warning")
                        return

                    row = get_user_by_username(un)
                    if not row or not verify_password(pw, row["password_hash"]):
                        safe_log_action(None, "login_failed", details=json.dumps({"username": un}, ensure_ascii=False))
                        ui.notify("ユーザー名またはパスワードが違います", type="negative")
                        return

                    set_logged_in(row)
                    cleanup_user_storage()
                    u = current_user()
                    if u:
                        safe_log_action(u, "login_success")

                    ui.notify("ログインしました", type="positive")
                    root_refresh()

                ui.button("ログイン", on_click=do_login).props("color=primary unelevated").classes("q-mt-md w-full")


def render_first_admin_setup(root_refresh) -> None:
    with ui.element("div").classes("w-full").style("min-height: calc(100vh - 0px); background:#f5f5f5;"):
        with ui.column().classes("w-full items-center q-pa-xl"):
            with ui.card().classes("q-pa-lg rounded-borders").style("width: 620px; max-width: 92vw;").props("bordered"):
                ui.label("初期設定：管理者アカウント作成（本番用）").classes("text-h5 q-mb-md")
                ui.label("※この画面は、ユーザーが1人もいない時だけ表示されます。").classes("text-caption text-grey q-mb-md")

                username = ui.input("管理者ユーザー名").props("outlined").classes("w-full")
                password = ui.input("パスワード", password=True, password_toggle_button=True).props("outlined").classes("w-full")
                password2 = ui.input("パスワード（確認）", password=True, password_toggle_button=True).props("outlined").classes("w-full")

                def create_admin() -> None:
                    un = (username.value or "").strip()
                    pw = (password.value or "")
                    pw2 = (password2.value or "")
                    if not un or not pw:
                        ui.notify("ユーザー名とパスワードを入力してください", type="warning")
                        return
                    if pw != pw2:
                        ui.notify("パスワードが一致しません", type="negative")
                        return
                    if len(pw) < 10:
                        ui.notify("パスワードは10文字以上がおすすめです", type="warning")
                        return

                    create_user(un, pw, "admin")
                    row = get_user_by_username(un)
                    if row:
                        set_logged_in(row)
                        cleanup_user_storage()
                        u = current_user()
                        if u:
                            safe_log_action(u, "first_admin_created")
                        ui.notify("管理者を作成しました。ログインしました。", type="positive")
                        root_refresh()
                    else:
                        ui.notify("作成に失敗しました（同名ユーザーがいる可能性）", type="negative")

                ui.button("管理者を作成", on_click=create_admin).props("color=primary unelevated").classes("q-mt-md w-full")



# =========================
# Preview rendering
# =========================
# [BLK-11] Preview: Glassmorphism theme (SP/PC)

def _is_light_color(color_value: str) -> bool:
    return str(color_value) in {"white", "yellow"}

def _safe_primary_text_class(primary: str) -> str:
    return "text-black" if _is_light_color(primary) else "text-white"

def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    h = (hex_color or "").strip().lstrip("#")
    if len(h) == 3:
        h = "".join([c * 2 for c in h])
    if len(h) != 6:
        return (25, 118, 210)  # fallback (Quasar primary blue)
    r = int(h[0:2], 16)
    g = int(h[2:4], 16)
    b = int(h[4:6], 16)
    return (r, g, b)

def _rgb_to_hex(r: int, g: int, b: int) -> str:
    r = max(0, min(255, int(r)))
    g = max(0, min(255, int(g)))
    b = max(0, min(255, int(b)))
    return f"#{r:02x}{g:02x}{b:02x}"

def _blend_hex(c1: str, c2: str, t: float) -> str:
    """c1 と c2 を t(0..1) でブレンド"""
    t = max(0.0, min(1.0, float(t)))
    r1, g1, b1 = _hex_to_rgb(c1)
    r2, g2, b2 = _hex_to_rgb(c2)
    r = r1 + (r2 - r1) * t
    g = g1 + (g2 - g1) * t
    b = b1 + (b2 - b1) * t
    return _rgb_to_hex(r, g, b)

def _is_light_hex(hex_color: str) -> bool:
    r, g, b = _hex_to_rgb(hex_color)
    # 相対輝度（ざっくり）
    y = (r * 0.299) + (g * 0.587) + (b * 0.114)
    return y >= 165

def _preview_accent_hex(primary: str) -> str:
    """プレビュー用のアクセント色（ガラステーマ用）"""
    # 「白」はアクセントが白だと見えないので、濃い色に寄せる
    if primary == "white":
        return "#0f172a"
    # 「黒」は真っ黒より少し柔らかい方が見やすい
    if primary == "black":
        return "#111827"
    # それ以外は既存の COLOR_HEX を採用
    return COLOR_HEX.get(primary, "#1976d2")

def _preview_glass_style(primary: str) -> str:
    accent = _preview_accent_hex(primary)
    # 背景は「ほぼ白 + うっすらアクセント」の2色グラデ
    bg1 = _blend_hex(accent, "#ffffff", 0.94)
    bg2 = _blend_hex(accent, "#ffffff", 0.86)
    accent_contrast = "#000000" if _is_light_hex(accent) else "#ffffff"
    # Quasar の primary もプレビュー内だけ差し替える（text-primary等が効く）
    return (
        f"--pv-accent:{accent};"
        f"--pv-accent-contrast:{accent_contrast};"
        f"--pv-bg1:{bg1};"
        f"--pv-bg2:{bg2};"
        f"--pv-text:#0b1220;"
        f"--q-primary:{accent};"
    )

def render_preview(p: dict, mode: str = "mobile") -> None:
    """右側プレビュー: mode='mobile' or 'pc'"""
    if not p:
        ui.label("案件が未選択です").classes("text-caption text-grey")
        return

    data = normalize_project(p)["data"]
    step1 = data["step1"]
    step2 = data["step2"]
    blocks = data["blocks"]

    primary = step1.get("page_color", "blue") or "blue"
    accent_hex = _preview_accent_hex(primary)
    header_text = _safe_primary_text_class(primary)

    company = (step2.get("company_name") or "会社名（未入力）").strip()
    catch = (step2.get("catch_copy") or "キャッチコピー（未入力）").strip()
    subcatch = (blocks.get("hero", {}).get("subcatch") or "").strip()
    phone = (step2.get("phone") or "").strip()
    email = (step2.get("email") or "").strip()
    addr = (step2.get("address") or "").strip()

    # 画像（未入力ならテンプレ）
    hero_img = (blocks.get("hero", {}).get("image_url") or "").strip()
    hero_preset = blocks.get("hero", {}).get("image_preset", "A")
    if not hero_img:
        hero_img = HERO_IMAGE_PRESET_URLS.get(hero_preset, HERO_IMAGE_PRESET_URLS["A"])

    # ボタン文言
    btn_primary = (blocks.get("hero", {}).get("button_primary_text") or "お問い合わせ").strip()
    btn_secondary = (blocks.get("hero", {}).get("button_secondary_text") or "見学・相談").strip()

    # 理念
    ph_title = (blocks.get("philosophy", {}).get("title") or "私たちの想い").strip()
    ph_body = (blocks.get("philosophy", {}).get("body") or "ここに理念や会社の紹介文を書きます。\n（あとで自由に書き換えてできます）").strip()
    tags = blocks.get("philosophy", {}).get("tags") or ["地域密着", "丁寧な対応", "安心の体制"]

    # お知らせ
    news_items = blocks.get("news", {}).get("items") or []
    # FAQ
    faq_items = blocks.get("faq", {}).get("items") or []

    # アクセス
    access_notes = (blocks.get("access", {}).get("notes") or "（例）○○駅から徒歩5分 / 駐車場あり").strip()
    map_url = google_maps_url(addr) if addr else ""

    # お問い合わせ
    message = (blocks.get("contact", {}).get("message") or "まずはお気軽にご相談ください。").strip()
    hours = (blocks.get("contact", {}).get("hours") or "平日 9:00〜18:00").strip()

    preview_style = _preview_glass_style(primary)

    def section_title(icon_name: str, title: str) -> None:
        with ui.row().classes("items-center q-gutter-sm"):
            ui.icon(icon_name).classes("text-primary")
            ui.label(title).classes("text-subtitle1")

    def pv_button_primary(label: str, on_click=None) -> ui.element:
        btn = ui.button(label, on_click=on_click).props("unelevated no-caps")
        btn.classes("pv-btn-primary")
        return btn

    def pv_button_secondary(label: str, on_click=None) -> ui.element:
        btn = ui.button(label, on_click=on_click).props("unelevated no-caps")
        btn.classes("pv-btn-secondary")
        return btn

    # -------------------------
    # Mobile preview
    # -------------------------
    if mode == "mobile":
        with ui.column().classes("w-full cvhb-preview cvhb-preview-glass").style(preview_style):
            # topbar
            with ui.element("div").classes("w-full pv-topbar"):
                with ui.element("div").classes("pv-topbar-inner"):
                    ui.label(company).classes("pv-topbar-title")
                    ui.icon("menu").classes("text-primary")

            # body scroll
            with ui.element("div").classes("w-full").style("height: calc(100vh - 140px); overflow: auto; padding: 16px;"):
                # hero
                with ui.element("div").classes("w-full pv-hero").style(
                    f"height: 240px; background-image: url('{hero_img}'); background-size: cover; background-position: center;"
                ):
                    with ui.element("div").classes("pv-hero-overlay"):
                        with ui.column().classes("q-pa-md").style("height: 100%; justify-content: flex-end;"):
                            ui.label(catch).classes("text-h6 text-white")
                            if subcatch:
                                ui.label(subcatch).classes("text-body2 text-white")
                            with ui.row().classes("q-gutter-sm q-mt-md"):
                                pv_button_primary(btn_primary)
                                pv_button_secondary(btn_secondary)

                # philosophy
                with ui.element("div").classes("w-full pv-section"):
                    with ui.card().classes("w-full q-pa-md pv-card").props("flat"):
                        section_title("favorite", ph_title)
                        label_pre(ph_body, "text-body2 q-mt-sm")
                        if tags:
                            with ui.row().classes("q-gutter-xs q-mt-sm"):
                                for t in tags[:6]:
                                    ui.badge(t).classes("pv-pill")

                # news
                with ui.element("div").classes("w-full pv-section"):
                    with ui.card().classes("w-full q-pa-md pv-card").props("flat"):
                        section_title("campaign", "お知らせ")
                        if not news_items:
                            ui.label("まだお知らせはありません").classes("text-caption pv-muted q-mt-sm")
                        else:
                            with ui.column().classes("q-gutter-sm q-mt-sm"):
                                for it in news_items[:3]:
                                    title = (it.get("title") or "（タイトル未入力）").strip()
                                    date = (it.get("date") or "").strip()
                                    cat = (it.get("category") or "").strip()
                                    body = (it.get("body") or "").strip()
                                    with ui.card().classes("q-pa-sm").props("flat"):
                                        with ui.row().classes("items-start justify-between q-gutter-sm"):
                                            ui.label(title).classes("text-body1")
                                            if cat:
                                                ui.badge(cat).classes("pv-pill")
                                        if date:
                                            ui.label(date).classes("text-caption pv-muted")
                                        if body:
                                            snippet = body.replace("\n", " ")
                                            if len(snippet) > 80:
                                                snippet = snippet[:80] + "…"
                                            ui.label(snippet).classes("text-caption")

                # faq
                with ui.element("div").classes("w-full pv-section"):
                    with ui.card().classes("w-full q-pa-md pv-card").props("flat"):
                        section_title("help", "よくある質問")
                        if not faq_items:
                            ui.label("まだFAQはありません").classes("text-caption pv-muted q-mt-sm")
                        else:
                            with ui.column().classes("q-gutter-sm q-mt-sm"):
                                for it in faq_items[:4]:
                                    q = (it.get("q") or "").strip() or "（質問 未入力）"
                                    a = (it.get("a") or "").strip() or "（回答 未入力）"
                                    with ui.card().classes("q-pa-sm").props("flat"):
                                        ui.label(q).classes("text-body1")
                                        ui.separator().classes("q-my-xs")
                                        label_pre(a, "text-caption pv-muted")

                # access
                with ui.element("div").classes("w-full pv-section"):
                    with ui.card().classes("w-full q-pa-md pv-card").props("flat"):
                        section_title("place", "アクセス")
                        ui.label(f"住所：{addr if addr else '未入力'}").classes("text-body2")
                        ui.label(access_notes).classes("text-caption pv-muted q-mt-xs")
                        if map_url:
                            pv_button_primary("地図を開く", on_click=lambda u=map_url: ui.run_javascript(f"window.open('{u}','_blank')")).classes("q-mt-sm")
                        else:
                            ui.label("住所を入力すると地図ボタンが出ます").classes("text-caption pv-muted q-mt-sm")

                # contact
                with ui.element("div").classes("w-full pv-section"):
                    with ui.card().classes("w-full q-pa-md pv-card").props("flat"):
                        section_title("call", "お問い合わせ")
                        label_pre(message, "text-body2")
                        ui.separator().classes("q-my-sm")
                        ui.label(f"TEL：{phone if phone else '未入力'}").classes("text-body1")
                        ui.label(f"Email：{email if email else '未入力'}").classes("text-body2")
                        ui.label(f"受付時間：{hours}").classes("text-caption pv-muted")
                        pv_button_primary(btn_primary).classes("q-mt-sm")

                # footer
                with ui.element("div").classes("w-full pv-footer"):
                    with ui.element("div").classes("q-pa-md"):
                        ui.label(company).classes("text-subtitle2")
                        ui.label("© CoreVistaJP").classes("text-caption pv-muted")

        return

    # -------------------------
    # PC preview
    # -------------------------
    def pc_heading(icon_name: str, title: str) -> None:
        with ui.row().classes("items-center justify-center q-gutter-sm q-mb-md"):
            ui.icon(icon_name).classes("text-primary")
            ui.label(title).classes("text-h6")

    def jump(anchor: str) -> None:
        # anchor: '#pv-news' 等
        ui.run_javascript(f"document.querySelector('{anchor}')?.scrollIntoView({{behavior:'smooth', block:'start'}});")

    with ui.element("div").classes("w-full cvhb-preview cvhb-preview-pc cvhb-preview-glass").style(preview_style):
        # Header (glass)
        with ui.element("div").classes("cvhb-pc-header"):
            with ui.element("div").classes("cvhb-pc-container"):
                with ui.row().classes("items-center justify-between"):
                    ui.label(company).classes("cvhb-pc-logo text-subtitle1")
                    with ui.row().classes("items-center q-gutter-xs"):
                        ui.button("想い", on_click=lambda: jump("#pv-philosophy")).props("flat dense no-caps").classes("pv-nav-btn pv-linkbtn")
                        ui.button("お知らせ", on_click=lambda: jump("#pv-news")).props("flat dense no-caps").classes("pv-nav-btn pv-linkbtn")
                        ui.button("FAQ", on_click=lambda: jump("#pv-faq")).props("flat dense no-caps").classes("pv-nav-btn pv-linkbtn")
                        ui.button("アクセス", on_click=lambda: jump("#pv-access")).props("flat dense no-caps").classes("pv-nav-btn pv-linkbtn")
                        pv_button_primary("お問い合わせ", on_click=lambda: jump("#pv-contact")).props("dense")

        # Hero
        with ui.element("div").classes("cvhb-pc-hero").style(
            f"background-image: url('{hero_img}'); background-size: cover; background-position: center;"
        ):
            with ui.element("div").classes("cvhb-pc-hero-overlay"):
                with ui.element("div").classes("cvhb-pc-container"):
                    with ui.element("div").classes("cvhb-pc-hero-inner"):
                        with ui.element("div").classes("cvhb-pc-hero-grid"):
                            # Left: text
                            with ui.element("div"):
                                ui.label(catch).classes("text-h4 text-white")
                                if subcatch:
                                    ui.label(subcatch).classes("text-body1 text-white q-mt-sm")
                                with ui.row().classes("q-gutter-sm q-mt-md"):
                                    pv_button_primary(btn_primary)
                                    pv_button_secondary(btn_secondary)
                            # Right: small glass card
                            with ui.card().classes("cvhb-pc-card q-pa-md").props("flat"):
                                ui.label(company).classes("text-subtitle1")
                                ui.label("会社サイト（企業）").classes("text-caption pv-muted")
                                ui.separator().classes("q-my-sm")
                                ui.label("ここに会社の強みや一言PRを入れます。").classes("text-body2")
                                with ui.row().classes("q-gutter-xs q-mt-sm"):
                                    for t in tags[:3]:
                                        ui.badge(t).classes("pv-pill")

        # Philosophy / Overview
        with ui.element("div").classes("cvhb-pc-section").props("id=pv-philosophy"):
            with ui.element("div").classes("cvhb-pc-container"):
                pc_heading("favorite", ph_title)
                with ui.card().classes("cvhb-pc-card q-pa-md").props("flat"):
                    with ui.element("div").classes("cvhb-pc-grid-2"):
                        with ui.element("div"):
                            label_pre(ph_body, "text-body1")
                        with ui.element("div"):
                            ui.label("特徴").classes("text-subtitle2")
                            with ui.row().classes("q-gutter-xs q-mt-sm"):
                                for t in tags[:6]:
                                    ui.badge(t).classes("pv-pill")

        # News + FAQ (2 column)
        with ui.element("div").classes("cvhb-pc-section").props("id=pv-news"):
            with ui.element("div").classes("cvhb-pc-container"):
                with ui.element("div").classes("cvhb-pc-grid-2"):
                    # News
                    with ui.card().classes("cvhb-pc-card q-pa-md").props("flat"):
                        pc_heading("campaign", "お知らせ")
                        if not news_items:
                            ui.label("まだお知らせはありません").classes("text-caption pv-muted")
                        else:
                            with ui.column().classes("q-gutter-sm"):
                                for it in news_items[:4]:
                                    title = (it.get("title") or "（タイトル未入力）").strip()
                                    date = (it.get("date") or "").strip()
                                    cat = (it.get("category") or "").strip()
                                    body = (it.get("body") or "").strip()
                                    with ui.card().classes("q-pa-sm").props("flat"):
                                        with ui.row().classes("items-start justify-between q-gutter-sm"):
                                            ui.label(title).classes("text-body1")
                                            if cat:
                                                ui.badge(cat).classes("pv-pill")
                                        if date:
                                            ui.label(date).classes("text-caption pv-muted")
                                        if body:
                                            snippet = body.replace("\n", " ")
                                            if len(snippet) > 110:
                                                snippet = snippet[:110] + "…"
                                            ui.label(snippet).classes("text-caption")

                    # FAQ
                    with ui.element("div").props("id=pv-faq"):
                        with ui.card().classes("cvhb-pc-card q-pa-md").props("flat"):
                            pc_heading("help", "よくある質問")
                            if not faq_items:
                                ui.label("まだFAQはありません").classes("text-caption pv-muted")
                            else:
                                with ui.column().classes("q-gutter-sm"):
                                    for it in faq_items[:6]:
                                        q = (it.get("q") or "").strip() or "（質問 未入力）"
                                        a = (it.get("a") or "").strip() or "（回答 未入力）"
                                        with ui.card().classes("q-pa-sm").props("flat"):
                                            ui.label(q).classes("text-body1")
                                            ui.separator().classes("q-my-xs")
                                            label_pre(a, "text-caption pv-muted")

        # Access / Contact
        with ui.element("div").classes("cvhb-pc-section").props("id=pv-access"):
            with ui.element("div").classes("cvhb-pc-container"):
                with ui.element("div").classes("cvhb-pc-grid-2"):
                    # Access
                    with ui.card().classes("cvhb-pc-card q-pa-md").props("flat"):
                        pc_heading("place", "アクセス")
                        ui.label(f"住所：{addr if addr else '未入力'}").classes("text-body2")
                        if access_notes:
                            label_pre(access_notes, "text-caption pv-muted")
                        if map_url:
                            pv_button_primary("地図を開く", on_click=lambda u=map_url: ui.run_javascript(f"window.open('{u}','_blank')")).classes("q-mt-sm")
                        else:
                            ui.label("住所を入力すると地図ボタンが出ます").classes("text-caption pv-muted")

                    # Contact
                    with ui.element("div").props("id=pv-contact"):
                        with ui.card().classes("cvhb-pc-card q-pa-md").props("flat"):
                            pc_heading("call", "お問い合わせ")
                            if message:
                                label_pre(message, "text-body2")
                                ui.separator().classes("q-my-sm")
                            ui.label(f"TEL：{phone if phone else '未入力'}").classes("text-body1")
                            if hours:
                                ui.label(f"受付時間：{hours}").classes("text-caption pv-muted")
                            ui.label(f"Email：{email if email else '未入力'}").classes("text-body2")
                            pv_button_primary(btn_primary).classes("q-mt-sm")

        # Footer
        with ui.element("div").classes("w-full pv-footer"):
            with ui.element("div").classes("cvhb-pc-container"):
                with ui.row().classes("items-center justify-between q-pa-md"):
                    ui.label(company).classes("text-subtitle2")
                    ui.label("© CoreVistaJP").classes("text-caption pv-muted")
# =========================
# Builder (Main)
# =========================

def render_main(u: User) -> None:
    inject_global_styles()
    cleanup_user_storage()

    render_header(u)

    p = get_current_project(u)

    preview_ref = {"refresh_mobile": (lambda: None), "refresh_pc": (lambda: None)}

    def refresh_preview() -> None:
        # 表示中のタブがどちらでも更新されるように両方叩く（軽い）
        try:
            preview_ref["refresh_mobile"]()
        except Exception:
            pass
        try:
            preview_ref["refresh_pc"]()
        except Exception:
            pass

    def save_now() -> None:
        nonlocal p
        if not p:
            ui.notify("案件が選択されていません", type="warning")
            return
        try:
            save_project_to_sftp(p, u)
            set_current_project(p, u)
            ui.notify("保存しました（project.json）", type="positive")
        except Exception as e:
            ui.notify(f"保存に失敗しました: {sanitize_error_text(e)}", type="negative")

    with ui.element("div").classes("cvhb-page"):
        with ui.element("div").classes("cvhb-container"):
            with ui.element("div").classes("cvhb-split"):

                # -----------------
                # LEFT (cards)
                # -----------------
                with ui.element("div").classes("cvhb-left-col"):
                    with ui.element("div").classes("cvhb-left-stack"):

                        # Card 1: current project
                        with ui.card().classes("q-pa-md rounded-borders").props("bordered"):
                            with ui.row().classes("items-center justify-between"):
                                ui.label("現在の案件").classes("cvhb-card-title")
                                ui.button("案件一覧", on_click=lambda: navigate_to("/projects")).props("flat")

                            if not p:
                                ui.label("案件が未選択です。まずは「案件一覧」から開いてください。").classes("q-mt-sm")
                                ui.button("案件一覧へ", on_click=lambda: navigate_to("/projects")).props("color=primary unelevated").classes("q-mt-sm")
                            else:
                                ui.separator().classes("q-my-sm")
                                ui.label(p.get("project_name", "")).classes("text-subtitle1")
                                ui.label(f"ID：{p.get('project_id','')}").classes("cvhb-muted")

                                ui.separator().classes("q-my-sm")
                                ui.label(f"案件開始日：{fmt_jst(p.get('created_at'))}").classes("cvhb-muted")
                                ui.label(f"最新更新日：{fmt_jst(p.get('updated_at'))}").classes("cvhb-muted")
                                ui.label(f"更新担当者：{p.get('updated_by') or u.username}").classes("cvhb-muted")

                                ui.button("保存（PROJECT.JSON）", on_click=save_now).props("color=primary unelevated").classes("q-mt-sm")

                        # Card 2: step nav
                        with ui.card().classes("q-pa-md rounded-borders").props("bordered"):
                            ui.label("作成ステップ").classes("cvhb-card-title")
                            ui.label("ステップを選ぶと、下の入力画面が切り替わります。").classes("cvhb-muted q-mb-sm")

                            with ui.tabs().props("vertical dense").classes("w-full cvhb-step-tabs") as step_tabs:
                                ui.tab("s1", label="1. 業種設定・ページカラー設定")
                                ui.tab("s2", label="2. 基本情報設定")
                                ui.tab("s3", label="3. ページ内容詳細設定（ブロックごと）")
                                ui.tab("s4", label="4. 承認・最終チェック")
                                ui.tab("s5", label="5. 公開（管理者権限のみ）")

                        # Card 3: step contents
                        with ui.card().classes("q-pa-md rounded-borders").props("bordered"):
                            if not p:
                                ui.label("案件を選択すると、ここに入力項目が表示されます。").classes("text-body2")
                            else:
                                # local shortcuts
                                data = p["data"]
                                step1 = data["step1"]
                                step2 = data["step2"]
                                blocks = data["blocks"]

                                def update_and_refresh() -> None:
                                    set_current_project(p, u)
                                    refresh_preview()

                                def bind_step2_input(label: str, key: str, hint: str = "") -> None:
                                    val = step2.get(key, "")
                                    def _on_change(e):
                                        step2[key] = e.value or ""
                                        update_and_refresh()
                                    inp = ui.input(label, value=val, on_change=_on_change).props("outlined dense").classes("w-full q-mb-sm")
                                    if hint:
                                        inp.props(f"hint={hint}")

                                def update_block(block_key: str, field: str, value) -> None:
                                    b = blocks.setdefault(block_key, {})
                                    b[field] = value
                                    update_and_refresh()

                                def bind_block_input(block_key: str, label: str, field: str, *, textarea: bool = False, hint: str = "") -> None:
                                    b = blocks.setdefault(block_key, {})
                                    val = b.get(field, "")
                                    def _on_change(e):
                                        update_block(block_key, field, e.value or "")
                                    props = "outlined"
                                    if textarea:
                                        props += " type=textarea autogrow"
                                    inp = ui.input(label, value=val, on_change=_on_change).props(props).classes("w-full q-mb-sm")
                                    if hint:
                                        inp.props(f"hint={hint}")

                                with ui.tab_panels(step_tabs, value="s1").classes("w-full"):

                                    # -----------------
                                    # Step 1
                                    # -----------------
                                    with ui.tab_panel("s1"):
                                        ui.label("1. 業種設定・ページカラー設定").classes("text-h6 q-mb-sm")
                                        ui.label("最初にここを決めると、右の完成イメージが一気に整います。").classes("cvhb-muted q-mb-md")

                                        # Industry
                                        with ui.card().classes("q-pa-sm rounded-borders q-mb-sm w-full").props("flat bordered"):
                                            ui.label("業種を選んでください").classes("text-subtitle1")
                                            ui.label("※v1.0 は「会社・企業サイト」をまず商用ラインまで仕上げます。").classes("cvhb-muted q-mb-sm")

                                            @ui.refreshable
                                            def industry_selector():
                                                current_industry = step1.get("industry", "会社サイト（企業）")

                                                def set_industry(value: str) -> None:
                                                    step1["industry"] = value
                                                    update_and_refresh()
                                                    industry_selector.refresh()

                                                for opt in INDUSTRY_PRESETS:
                                                    selected = (opt["value"] == current_industry)
                                                    card = ui.card().classes(
                                                        "q-pa-sm q-mb-xs cvhb-choice " + ("is-selected" if selected else "")
                                                    ).props("flat bordered").style("width: 100%;")
                                                    with card:
                                                        with ui.row().classes("items-start justify-between"):
                                                            with ui.column().classes("q-gutter-xs"):
                                                                ui.label(opt["label"]).classes("text-body1")
                                                                ui.label(opt["features"]).classes("cvhb-muted")
                                                            if selected:
                                                                ui.icon("check_circle").classes("text-primary")
                                                    card.on("click", lambda e, v=opt["value"]: set_industry(v))

                                            industry_selector()

                                        # Color
                                        with ui.card().classes("q-pa-sm rounded-borders w-full").props("flat bordered"):
                                            ui.label("ページカラー（テーマ色）を選んでください").classes("text-subtitle1")
                                            ui.label("ヘッダー・ボタン・アイコンなどの雰囲気が変わります。").classes("cvhb-muted q-mb-sm")

                                            @ui.refreshable
                                            def color_selector():
                                                current_color = step1.get("primary_color", "blue")

                                                def set_color(value: str) -> None:
                                                    step1["primary_color"] = value
                                                    update_and_refresh()
                                                    color_selector.refresh()

                                                for opt in COLOR_PRESETS:
                                                    selected = (opt["value"] == current_color)
                                                    sw = COLOR_HEX.get(opt["value"], "#999")
                                                    card = ui.card().classes(
                                                        "q-pa-sm q-mb-xs cvhb-choice " + ("is-selected" if selected else "")
                                                    ).props("flat bordered").style("width: 100%;")
                                                    with card:
                                                        with ui.row().classes("items-center justify-between"):
                                                            with ui.row().classes("items-center q-gutter-sm"):
                                                                ui.element("span").classes("cvhb-swatch").style(f"background:{sw};")
                                                                ui.label(f"{opt['label']}").classes("text-body1")
                                                            with ui.row().classes("items-center q-gutter-sm"):
                                                                ui.label(f"印象：{opt['impression']}").classes("cvhb-muted")
                                                                if selected:
                                                                    ui.icon("check_circle").classes("text-primary")
                                                    card.on("click", lambda e, v=opt["value"]: set_color(v))

                                            color_selector()

                                    # -----------------
                                    # Step 2
                                    # -----------------
                                    with ui.tab_panel("s2"):
                                        ui.label("2. 基本情報設定").classes("text-h6 q-mb-sm")
                                        ui.label("入力すると右のプレビューに反映されます。").classes("cvhb-muted q-mb-md")

                                        with ui.card().classes("q-pa-sm rounded-borders w-full").props("flat bordered"):
                                            ui.label("会社の基本情報").classes("text-subtitle1 q-mb-sm")

                                            bind_step2_input("会社名", "company_name")
                                            bind_step2_input("キャッチコピー", "catch_copy")
                                            bind_step2_input("電話番号", "phone")
                                            bind_step2_input("メール（任意）", "email")
                                            bind_step2_input("住所（地図リンクは自動生成）", "address", hint="住所を入力すると、プレビューの「地図を開く」が使えるようになります。")

                                    # -----------------
                                    # Step 3
                                    # -----------------
                                    with ui.tab_panel("s3"):
                                        ui.label("3. ページ内容詳細設定（ブロックごと）").classes("text-h6 q-mb-sm")
                                        ui.label("各ブロックを切り替えて編集できます。迷わないように整理してあります。").classes("cvhb-muted q-mb-md")

                                        with ui.card().classes("q-pa-sm rounded-borders w-full").props("flat bordered"):
                                            ui.label("ブロック編集（会社テンプレ：6ブロック）").classes("text-subtitle1")
                                            ui.label("ヒーロー / 理念 / お知らせ / FAQ / アクセス / お問い合わせ").classes("cvhb-muted q-mb-sm")

                                            with ui.tabs().props("dense").classes("w-full cvhb-block-tabs") as block_tabs:
                                                ui.tab("hero", label="ヒーロー")
                                                ui.tab("philosophy", label="理念/概要")
                                                ui.tab("news", label="お知らせ")
                                                ui.tab("faq", label="FAQ")
                                                ui.tab("access", label="アクセス")
                                                ui.tab("contact", label="お問い合わせ")

                                            with ui.tab_panels(block_tabs, value="hero").classes("w-full q-mt-md"):

                                                with ui.tab_panel("hero"):
                                                    ui.label("ヒーロー（ページ最上部）").classes("text-subtitle1 q-mb-sm")

                                                    # hero image preset
                                                    hero = blocks.setdefault("hero", {})
                                                    current_preset = hero.get("hero_image", "A: オフィス")

                                                    def _on_preset_change(e) -> None:
                                                        hero["hero_image"] = e.value
                                                        update_and_refresh()

                                                    ui.label("大きい写真 + キャッチコピーのエリアです").classes("cvhb-muted q-mb-sm")
                                                    ui.radio(HERO_IMAGE_OPTIONS, value=current_preset, on_change=_on_preset_change).props("inline")
                                                    bind_block_input("hero", "画像URL（任意：貼るだけ）", "hero_image_url", hint="URLを入れると、上のプリセットより優先されます。")
                                                    bind_block_input("hero", "サブキャッチ（任意）", "sub_catch")
                                                    bind_block_input("hero", "ボタン1の文言", "primary_button_text")
                                                    bind_block_input("hero", "ボタン2の文言（任意）", "secondary_button_text")

                                                with ui.tab_panel("philosophy"):
                                                    ui.label("理念 / 会社概要").classes("text-subtitle1 q-mb-sm")
                                                    bind_block_input("philosophy", "見出し", "title")
                                                    bind_block_input("philosophy", "本文", "body", textarea=True)

                                                    # points
                                                    ph = blocks.setdefault("philosophy", {})
                                                    points = ph.setdefault("points", ["", "", ""])
                                                    if not isinstance(points, list):
                                                        points = ["", "", ""]
                                                    while len(points) < 3:
                                                        points.append("")
                                                    ph["points"] = points[:3]

                                                    ui.label("ポイント（3つまで）").classes("cvhb-muted q-mt-sm")

                                                    def update_point(idx: int, val: str) -> None:
                                                        ph["points"][idx] = val
                                                        update_and_refresh()

                                                    for i in range(3):
                                                        v = ph["points"][i]
                                                        ui.input(f"ポイント{i+1}", value=v, on_change=lambda e, idx=i: update_point(idx, e.value or "")).props("outlined").classes("w-full q-mb-sm")

                                                with ui.tab_panel("news"):
                                                    ui.label("お知らせ").classes("text-subtitle1 q-mb-sm")
                                                    ui.label("最大3件がスマホ側に表示されます（PCは4件まで表示）。").classes("cvhb-muted q-mb-sm")

                                                    @ui.refreshable
                                                    def news_editor():
                                                        items = blocks.setdefault("news", {}).setdefault("items", [])
                                                        if not isinstance(items, list):
                                                            items = []
                                                            blocks["news"]["items"] = items

                                                        def add_item():
                                                            items.insert(0, {"date": datetime.now(JST).strftime("%Y-%m-%d"), "category": "お知らせ", "title": "", "body": ""})
                                                            update_and_refresh()
                                                            news_editor.refresh()

                                                        def delete_item(i: int):
                                                            try:
                                                                del items[i]
                                                            except Exception:
                                                                pass
                                                            update_and_refresh()
                                                            news_editor.refresh()

                                                        def set_field(i: int, key: str, val: str):
                                                            if i < 0 or i >= len(items):
                                                                return
                                                            items[i][key] = val
                                                            update_and_refresh()

                                                        ui.button("＋ 追加", on_click=add_item).props("color=primary outline").classes("q-mb-sm")
                                                        if not items:
                                                            ui.label("まだお知らせがありません").classes("cvhb-muted")
                                                        for i, it in enumerate(items):
                                                            with ui.card().classes("w-full q-pa-md q-mb-sm rounded-borders").props("flat bordered"):
                                                                with ui.row().classes("items-center justify-between"):
                                                                    ui.label(f"お知らせ #{i+1}").classes("text-body1")
                                                                    ui.button("削除", on_click=lambda idx=i: delete_item(idx)).props("flat color=negative")
                                                                ui.input("日付", value=it.get("date",""), on_change=lambda e, idx=i: set_field(idx, "date", e.value or "")).props("outlined type=date").classes("w-full q-mb-sm")
                                                                ui.input("カテゴリ", value=it.get("category",""), on_change=lambda e, idx=i: set_field(idx, "category", e.value or "")).props("outlined").classes("w-full q-mb-sm")
                                                                ui.input("タイトル", value=it.get("title",""), on_change=lambda e, idx=i: set_field(idx, "title", e.value or "")).props("outlined").classes("w-full q-mb-sm")
                                                                ui.input("本文", value=it.get("body",""), on_change=lambda e, idx=i: set_field(idx, "body", e.value or "")).props("outlined type=textarea autogrow").classes("w-full")
                                                    news_editor()
                                                    # refresh hook not needed; update_and_refresh will refresh preview

                                                with ui.tab_panel("faq"):
                                                    ui.label("FAQ").classes("text-subtitle1 q-mb-sm")
                                                    ui.label("Q&Aを編集できます。").classes("cvhb-muted q-mb-sm")

                                                    @ui.refreshable
                                                    def faq_editor():
                                                        items = blocks.setdefault("faq", {}).setdefault("items", [])
                                                        if not isinstance(items, list):
                                                            items = []
                                                            blocks["faq"]["items"] = items

                                                        def add_item():
                                                            items.append({"q": "", "a": ""})
                                                            update_and_refresh()
                                                            faq_editor.refresh()

                                                        def delete_item(i: int):
                                                            try:
                                                                del items[i]
                                                            except Exception:
                                                                pass
                                                            update_and_refresh()
                                                            faq_editor.refresh()

                                                        def set_field(i: int, key: str, val: str):
                                                            if i < 0 or i >= len(items):
                                                                return
                                                            items[i][key] = val
                                                            update_and_refresh()

                                                        ui.button("＋ 追加", on_click=add_item).props("color=primary outline").classes("q-mb-sm")
                                                        if not items:
                                                            ui.label("まだFAQがありません").classes("cvhb-muted")
                                                        for i, it in enumerate(items):
                                                            with ui.card().classes("w-full q-pa-md q-mb-sm rounded-borders").props("flat bordered"):
                                                                with ui.row().classes("items-center justify-between"):
                                                                    ui.label(f"FAQ #{i+1}").classes("text-body1")
                                                                    ui.button("削除", on_click=lambda idx=i: delete_item(idx)).props("flat color=negative")
                                                                ui.input("質問（Q）", value=it.get("q",""), on_change=lambda e, idx=i: set_field(idx, "q", e.value or "")).props("outlined").classes("w-full q-mb-sm")
                                                                ui.input("回答（A）", value=it.get("a",""), on_change=lambda e, idx=i: set_field(idx, "a", e.value or "")).props("outlined type=textarea autogrow").classes("w-full")
                                                    faq_editor()

                                                with ui.tab_panel("access"):
                                                    ui.label("アクセス").classes("text-subtitle1 q-mb-sm")
                                                    bind_block_input("access", "地図URL（任意）", "map_url", hint="空欄なら「住所」から自動生成します。")
                                                    bind_block_input("access", "補足（任意）", "notes", textarea=True)

                                                with ui.tab_panel("contact"):
                                                    ui.label("お問い合わせ").classes("text-subtitle1 q-mb-sm")
                                                    bind_block_input("contact", "受付時間（任意）", "hours")
                                                    bind_block_input("contact", "メッセージ（任意）", "message", textarea=True)

                                    # -----------------
                                    # Step 4 / 5
                                    # -----------------
                                    with ui.tab_panel("s4"):
                                        ui.label("4. 承認・最終チェック").classes("text-h6")
                                        ui.label("v0.7.0で承認フロー（OK/差戻し）を実装します。").classes("cvhb-muted q-mt-sm")

                                    with ui.tab_panel("s5"):
                                        ui.label("5. 公開（管理者権限のみ）").classes("text-h6")
                                        ui.label("v0.7.0で公開（アップロード）を実装します。").classes("cvhb-muted q-mt-sm")

                # -----------------
                # RIGHT (preview)
                # -----------------
                with ui.element("div").classes("cvhb-right-col"):
                    with ui.card().classes("q-pa-md rounded-borders").props("bordered"):
                        with ui.row().classes("items-center justify-between"):
                            ui.label("プレビュー").classes("cvhb-card-title")
                            ui.label("スマホ / PC 切替").classes("cvhb-muted")

                        with ui.tabs().props("dense").classes("q-mt-sm") as pv_tabs:
                            ui.tab("mobile", label="スマホ", icon="smartphone")
                            ui.tab("pc", label="PC", icon="desktop_windows")

                        with ui.tab_panels(pv_tabs, value="mobile").classes("w-full q-mt-sm"):

                            with ui.tab_panel("mobile"):
                                with ui.card().style(
                                    "width: clamp(320px, 38vw, 420px); height: clamp(560px, 75vh, 740px); overflow: hidden; border-radius: 18px; margin: 0 auto;"
                                ).props("flat bordered"):
                                    with ui.element("div").style("height: 100%; overflow: auto;"):
                                        @ui.refreshable
                                        def preview_mobile_panel():
                                            if not p:
                                                ui.label("案件を選ぶとプレビューが出ます").classes("cvhb-muted q-pa-md")
                                                return
                                            render_preview(p, mode="mobile")

                                        preview_ref["refresh_mobile"] = preview_mobile_panel.refresh
                                        preview_mobile_panel()

                            with ui.tab_panel("pc"):
                                with ui.card().style(
                                    "width: min(100%, 1024px); height: clamp(560px, 75vh, 740px); overflow: hidden; border-radius: 14px; margin: 0 auto;"
                                ).props("flat bordered"):
                                    with ui.element("div").style("height: 100%; overflow: auto; background: #f5f5f5;"):
                                        @ui.refreshable
                                        def preview_pc_panel():
                                            if not p:
                                                ui.label("案件を選ぶとプレビューが出ます").classes("cvhb-muted q-pa-md")
                                                return
                                            render_preview(p, mode="pc")

                                        preview_ref["refresh_pc"] = preview_pc_panel.refresh
                                        preview_pc_panel()


# =========================
# Pages
# =========================

@ui.page("/projects")
def projects_page():
    inject_global_styles()
    cleanup_user_storage()
    ui.page_title("案件一覧 | CV-HomeBuilder")

    u = current_user()
    if not u:
        navigate_to("/")
        return

    render_header(u)

    with ui.element("div").classes("cvhb-container"):

        # --- 新規作成ダイアログ ---
        with ui.dialog() as new_project_dialog, ui.card().classes("q-pa-md rounded-borders").props("bordered"):
            ui.label("新規案件を作成").classes("text-subtitle1 q-mb-sm")
            ui.label("案件名を入力して作成してください。").classes("cvhb-muted q-mb-sm")
            dialog_name = ui.input("案件名（例：〇〇株式会社サイト）").props("outlined").classes("w-full")

            def create_new_project() -> None:
                name = (dialog_name.value or "").strip()
                if not name:
                    ui.notify("案件名を入力してください", type="warning")
                    return
                try:
                    p = create_project(name, u)
                    save_project_to_sftp(p, u)
                    set_current_project(p, u)
                    ui.notify("案件を作成しました", type="positive")
                    new_project_dialog.close()
                    navigate_to("/")
                except Exception as e:
                    ui.notify(f"作成に失敗しました: {sanitize_error_text(e)}", type="negative")

            with ui.row().classes("q-gutter-sm q-mt-md"):
                ui.button("キャンセル", on_click=new_project_dialog.close).props("flat")
                ui.button("作成", on_click=create_new_project).props("color=primary unelevated")

        # --- 画面ヘッダー（タイトル＋ボタン） ---
        with ui.element("div").classes("cvhb-projects-actions q-mb-md"):
            with ui.column():
                ui.label("案件一覧").classes("text-h5 cvhb-card-title")
                ui.label("案件を開いて編集します。新規作成もここからできます。").classes("cvhb-muted")
            with ui.row().classes("q-gutter-sm"):
                ui.button("新規作成", on_click=lambda: new_project_dialog.open()).props("color=primary unelevated")
                ui.button("ビルダーへ戻る", on_click=lambda: navigate_to("/")).props("flat")

        ui.separator().classes("q-my-md")

        def open_project(project_id: str) -> None:
            try:
                p = load_project_from_sftp(project_id, u)
                set_current_project(p, u)
                ui.notify("案件を開きました", type="positive")
                navigate_to("/")
            except Exception as e:
                ui.notify(f"開けませんでした: {sanitize_error_text(e)}", type="negative")

        @ui.refreshable
        def list_refresh():
            items = []
            try:
                items = list_projects_from_sftp()
            except Exception as e:
                ui.notify(f"一覧取得に失敗しました: {sanitize_error_text(e)}", type="negative")

            if not items:
                with ui.card().classes("q-pa-md rounded-borders").props("bordered"):
                    ui.label("まだ案件がありません").classes("text-subtitle1")
                    ui.label("「新規作成」から最初の案件を作れます。").classes("cvhb-muted q-mt-xs")
                    ui.button("新規作成", on_click=lambda: new_project_dialog.open()).props("color=primary unelevated").classes("q-mt-md")
                return

            # --- グリッド表示（PCで見栄えUP） ---
            with ui.element("div").classes("cvhb-project-grid"):
                for it in items:
                    pid = it.get("project_id", "")
                    pname = it.get("project_name", "")
                    updated_at = fmt_jst(it.get("updated_at"))
                    created_at = fmt_jst(it.get("created_at"))
                    updated_by = it.get("updated_by", "")

                    with ui.card().classes("q-pa-md rounded-borders cvhb-project-card").props("bordered"):
                        ui.label(pname).classes("text-subtitle1")
                        ui.label(f"最終更新: {updated_at}").classes("cvhb-project-meta q-mt-xs")
                        ui.label(f"案件開始: {created_at}").classes("cvhb-project-meta")
                        if updated_by:
                            ui.label(f"更新担当者: {updated_by}").classes("cvhb-project-meta")
                        ui.label(f"ID: {pid}").classes("cvhb-project-meta q-mt-xs")

                        ui.button("開く", on_click=lambda project_id=pid: open_project(project_id)).props("color=primary unelevated").classes("w-full q-mt-md")

        list_refresh()


@ui.page("/audit")
def audit_page():
    inject_global_styles()
    cleanup_user_storage()
    ui.page_title("操作ログ | CV-HomeBuilder")

    u = current_user()
    if not u:
        navigate_to("/")
        return
    if u.role not in {"admin", "subadmin"}:
        ui.notify("権限がありません", type="negative")
        navigate_to("/")
        return

    render_header(u)

    with ui.element("div").classes("cvhb-container"):
        ui.label("操作ログ").classes("text-h5 q-mb-md")

        logs = db_fetchall("SELECT * FROM audit_logs ORDER BY created_at DESC LIMIT 200", None)

        rows = []
        for r in logs:
            rows.append({
                "日時(JST)": fmt_jst(r.get("created_at")),
                "ユーザー": r.get("username") or "",
                "権限": r.get("role") or "",
                "操作": r.get("action") or "",
                "詳細": r.get("details") or "",
            })

        ui.table(
            columns=[
                {"name": "日時(JST)", "label": "日時(JST)", "field": "日時(JST)"},
                {"name": "ユーザー", "label": "ユーザー", "field": "ユーザー"},
                {"name": "権限", "label": "権限", "field": "権限"},
                {"name": "操作", "label": "操作", "field": "操作"},
                {"name": "詳細", "label": "詳細", "field": "詳細"},
            ],
            rows=rows,
            row_key="日時(JST)",
        ).classes("w-full")


@ui.page("/")
def index():
    ui.page_title("CV-HomeBuilder")
    inject_global_styles()

    root = ui.element("div").classes("w-full")

    @ui.refreshable
    def root_refresh():
        root.clear()
        with root:
            try:
                cleanup_user_storage()
                u = current_user()

                # 本番：初回のみ admin を作らせる
                if APP_ENV != "stg" and count_users() == 0:
                    render_first_admin_setup(root_refresh)
                    return

                if not u:
                    render_login(root_refresh)
                    return

                render_main(u)

            except Exception as e:
                # ここに来る時点で「画面500」になりがちなので、落とさず画面に出す（秘密情報は出さない）
                print("[fatal] render failed:", sanitize_error_text(e))
                print(traceback.format_exc())

                render_header(current_user())

                with ui.element("div").classes("cvhb-container"):
                    with ui.card().classes("q-pa-md rounded-borders").props("bordered"):
                        ui.label("表示でエラーが起きました").classes("text-h6")
                        ui.label("まずはブラウザの更新（F5）をしてください。").classes("q-mt-sm")
                        ui.label("直らない場合は「ログアウト → ログイン」を試してください。").classes("cvhb-muted q-mt-sm")
                        ui.separator().classes("q-my-md")
                        ui.label(f"エラー種別：{type(e).__name__}").classes("text-caption")
                        ui.label(f"内容：{sanitize_error_text(e)}").classes("text-caption text-grey")
                        with ui.row().classes("q-gutter-sm q-mt-md"):
                            ui.button("ログアウト", on_click=logout).props("color=negative flat")
                            ui.button("トップへ戻る", on_click=lambda: navigate_to("/")).props("flat")

    root_refresh()


# =========================
# Boot
# =========================

init_db_schema()

if __name__ in {"__main__", "__mp_main__"}:
    ui.run(
        title=f"CV-HomeBuilder v{VERSION}",
        storage_secret=STORAGE_SECRET,
        reload=False,
        port=int(os.getenv("PORT", "8080")),
        show=False,
    )