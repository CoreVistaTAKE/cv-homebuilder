import base64
import hashlib
import json
import os
import secrets
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, unquote, quote_plus

import paramiko
import psycopg
from psycopg.rows import dict_row
from nicegui import app, ui


# =========================
# Global UI styles (v0.6.1)
# =========================

_STYLES_INJECTED = False


def inject_global_styles() -> None:
    """見た目用のCSS（※左右レイアウトはQuasarのrowで制御して、CSS依存を減らす）
    - v0.6.0で「広くしても縦並び」になっていた原因は、CSSが効かない/想定外の幅判定が起きるケースがあるため。
    - ここでは“装飾”だけにCSSを使い、“左右/上下の切替”は ui.row の標準挙動（wrap）を使う。
    """
    global _STYLES_INJECTED
    if _STYLES_INJECTED:
        return

    ui.add_head_html(
        """
<style>
  .cvhb-page {
    background: #f5f5f5;
    min-height: calc(100vh - 64px);
  }
  .cvhb-container {
    max-width: 1400px;
    margin: 0 auto;
    padding: 16px;
  }

  /* 選択カード（業種/カラー） */
  .cvhb-option-card {
    cursor: pointer;
    transition: background 0.12s ease-in-out, transform 0.12s ease-in-out;
  }
  .cvhb-option-card:hover {
    background: #fafafa;
  }
  .cvhb-swatch {
    width: 18px;
    height: 18px;
    border-radius: 4px;
    border: 1px solid rgba(0,0,0,0.22);
    flex: 0 0 auto;
  }

  /* 右プレビューを“できるだけ”上に残す（広い画面だけ） */
  @media (min-width: 1024px) {
    .cvhb-preview-sticky {
      position: sticky;
      top: 88px; /* ヘッダー + 余白 */
      align-self: flex-start;
    }
  }
</style>
"""
    )
    _STYLES_INJECTED = True


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

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_project_id() -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    rnd = secrets.token_hex(3)
    return f"p{ts}_{rnd}"


def google_maps_url(address: str) -> str:
    address = (address or "").strip()
    if not address:
        return ""
    return f"https://www.google.com/maps/search/?api=1&query={quote_plus(address)}"


def fmt_dt(iso_str: str) -> str:
    """ISOっぽい日時を、見やすい表示にする（失敗したらそのまま）"""
    s = (iso_str or "").strip()
    if not s:
        return ""
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return s


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
        print(f"[audit_log] failed: {e}")


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


def navigate_to(path: str) -> None:
    safe_path = (path or "/").replace("'", "\\'")
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


def logout() -> None:
    u = current_user()
    if u:
        safe_log_action(u, "logout")
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
# Projects / Template presets (v0.6.1)
# =========================

INDUSTRY_PRESETS = [
    {
        "value": "会社・企業サイト",
        "label": "会社・企業サイト",
        "feature": "会社テンプレ（6ブロック）を使います。まずここを商用ラインまで仕上げます。",
    },
    {
        "value": "福祉事業所",
        "label": "福祉事業所",
        "feature": "準備中（後で専用テンプレ追加予定）。今は表示名だけ切替できます。",
    },
    {
        "value": "個人事業",
        "label": "個人事業",
        "feature": "準備中（後で専用テンプレ追加予定）。今は表示名だけ切替できます。",
    },
    {
        "value": "その他",
        "label": "その他",
        "feature": "準備中（自由テンプレ予定）。今は表示名だけ切替できます。",
    },
]
INDUSTRY_OPTIONS = [x["value"] for x in INDUSTRY_PRESETS]

THEME_PRESETS = [
    {
        "key": "white",
        "label": "白",
        "impression": "清潔・シンプル",
        "header_bg": "white",
        "header_text": "grey-10",
        "accent": "indigo",
        "accent_text": "white",
        "swatch_bg": "white",
    },
    {
        "key": "black",
        "label": "黒",
        "impression": "高級・信頼",
        "header_bg": "grey-10",
        "header_text": "white",
        "accent": "deep-orange",
        "accent_text": "white",
        "swatch_bg": "grey-10",
    },
    {
        "key": "red",
        "label": "赤",
        "impression": "情熱・行動",
        "header_bg": "red-6",
        "header_text": "white",
        "accent": "red-6",
        "accent_text": "white",
        "swatch_bg": "red-6",
    },
    {
        "key": "blue",
        "label": "青",
        "impression": "信頼・誠実",
        "header_bg": "indigo",
        "header_text": "white",
        "accent": "indigo",
        "accent_text": "white",
        "swatch_bg": "indigo",
    },
    {
        "key": "yellow",
        "label": "黄",
        "impression": "明るい・親しみ",
        "header_bg": "amber-6",
        "header_text": "grey-10",
        "accent": "amber-6",
        "accent_text": "grey-10",
        "swatch_bg": "amber-6",
    },
    {
        "key": "green",
        "label": "緑",
        "impression": "安心・自然",
        "header_bg": "green",
        "header_text": "white",
        "accent": "green",
        "accent_text": "white",
        "swatch_bg": "green",
    },
    {
        "key": "purple",
        "label": "紫",
        "impression": "上品・落ち着き",
        "header_bg": "purple",
        "header_text": "white",
        "accent": "purple",
        "accent_text": "white",
        "swatch_bg": "purple",
    },
    {
        "key": "grey",
        "label": "灰",
        "impression": "堅実・中立",
        "header_bg": "blue-grey",
        "header_text": "white",
        "accent": "blue-grey",
        "accent_text": "white",
        "swatch_bg": "blue-grey",
    },
    {
        "key": "orange",
        "label": "オレンジ",
        "impression": "元気・あたたかい",
        "header_bg": "deep-orange",
        "header_text": "white",
        "accent": "deep-orange",
        "accent_text": "white",
        "swatch_bg": "deep-orange",
    },
]
THEME_KEYS = [x["key"] for x in THEME_PRESETS]
THEME_BY_KEY = {x["key"]: x for x in THEME_PRESETS}


def normalize_industry(value: str) -> str:
    v = (value or "").strip()
    mapping = {
        "会社サイト（企業）": "会社・企業サイト",
        "会社サイト(企業)": "会社・企業サイト",
    }
    v = mapping.get(v, v)
    if v in INDUSTRY_OPTIONS:
        return v
    return "会社・企業サイト"


def normalize_theme_key(value: str) -> str:
    v = (value or "").strip()

    # 新方式（white/black/red/...）ならそのまま
    if v in THEME_KEYS:
        return v

    # 旧方式（blue/indigo/teal/deep-orange/purple...）の救済
    legacy_map = {
        "blue": "blue",
        "indigo": "blue",
        "teal": "green",
        "green": "green",
        "deep-orange": "orange",
        "orange": "orange",
        "purple": "purple",
        "grey": "grey",
        "gray": "grey",
        "blue-grey": "grey",
        "black": "black",
        "white": "white",
        "red": "red",
        "yellow": "yellow",
        "amber": "yellow",
    }
    return legacy_map.get(v, "blue")


def get_theme(theme_key: str) -> dict:
    key = normalize_theme_key(theme_key)
    return THEME_BY_KEY.get(key, THEME_BY_KEY["blue"])


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


def normalize_project(p: dict) -> dict:
    p["schema_version"] = "0.6.1"
    p.setdefault("project_id", new_project_id())
    p.setdefault("project_name", "(no name)")
    p.setdefault("created_at", now_iso())
    p.setdefault("updated_at", now_iso())

    # 表示用（任意）
    p.setdefault("created_by", "")
    p.setdefault("updated_by", "")

    data = p.setdefault("data", {})
    step1 = data.setdefault("step1", {})
    step2 = data.setdefault("step2", {})
    blocks = data.setdefault("blocks", {})

    step1.setdefault("industry", "会社・企業サイト")
    step1.setdefault("primary_color", "blue")  # theme key

    # 旧データ救済
    step1["industry"] = normalize_industry(step1.get("industry", "会社・企業サイト"))
    step1["primary_color"] = normalize_theme_key(step1.get("primary_color", "blue"))

    step2.setdefault("company_name", "")
    step2.setdefault("catch_copy", "")
    step2.setdefault("phone", "")
    step2.setdefault("address", "")
    step2.setdefault("email", "")

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
                "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
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


def get_current_project() -> Optional[dict]:
    try:
        p = app.storage.user.get("project")
        if isinstance(p, dict) and p.get("project_id"):
            return normalize_project(p)
        return None
    except Exception:
        return None


def set_current_project(p: dict) -> None:
    app.storage.user["project"] = normalize_project(p)


def create_project(name: str, created_by: Optional[User]) -> dict:
    pid = new_project_id()
    p = {
        "schema_version": "0.6.1",
        "project_id": pid,
        "project_name": name,
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "created_by": created_by.username if created_by else "",
        "updated_by": created_by.username if created_by else "",
        "data": {
            "step1": {"industry": "会社・企業サイト", "primary_color": "blue"},
            "step2": {"company_name": "", "catch_copy": "", "phone": "", "address": "", "email": ""},
            "blocks": {},
        },
    }
    p = normalize_project(p)

    if created_by:
        safe_log_action(
            created_by,
            "project_create",
            details=json.dumps({"project_id": pid, "name": name}, ensure_ascii=False),
        )
    return p


def save_project_to_sftp(p: dict, user: Optional[User]) -> None:
    p = normalize_project(p)
    p["updated_at"] = now_iso()
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
                projects.append(
                    {
                        "project_id": p.get("project_id", d),
                        "project_name": p.get("project_name", "(no name)"),
                        "updated_at": p.get("updated_at", ""),
                        "created_at": p.get("created_at", ""),
                    }
                )
            except Exception:
                projects.append({"project_id": d, "project_name": "(broken project.json)", "updated_at": "", "created_at": ""})

    projects.sort(key=lambda x: x.get("updated_at", ""), reverse=True)
    return projects


# =========================
# UI parts
# =========================

def render_header(u: Optional[User]) -> None:
    # stickyにして、スクロールしても上に残る
    with ui.element("div").classes("w-full bg-white shadow-1").style("position: sticky; top: 0; z-index: 1000;"):
        with ui.row().classes("w-full items-center justify-between q-pa-md").style("gap: 12px;"):
            with ui.row().classes("items-center q-gutter-sm"):
                ui.icon("home").classes("text-grey-8")
                ui.label(f"CV-HomeBuilder v{VERSION}").classes("text-h6")

            with ui.row().classes("items-center q-gutter-sm").style("flex-wrap: wrap; justify-content: flex-end;"):
                ui.badge(APP_ENV.upper()).props("outline")
                ui.badge(f"SFTP_BASE_DIR: {SFTP_BASE_DIR}").props("outline")

                p = get_current_project()
                if p:
                    ui.badge(f"案件: {p.get('project_name','')}"[:18]).props("outline")

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
                    u2 = current_user()
                    if u2:
                        safe_log_action(u2, "login_success")

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
                        u2 = current_user()
                        if u2:
                            safe_log_action(u2, "first_admin_created")
                        ui.notify("管理者を作成しました。ログインしました。", type="positive")
                        root_refresh()
                    else:
                        ui.notify("作成に失敗しました（同名ユーザーがいる可能性）", type="negative")

                ui.button("管理者を作成", on_click=create_admin).props("color=primary unelevated").classes("q-mt-md w-full")


def render_preview(p: dict) -> None:
    p = normalize_project(p)
    step1 = p["data"]["step1"]
    step2 = p["data"]["step2"]
    blocks = p["data"]["blocks"]

    industry = step1.get("industry", "会社・企業サイト")
    theme = get_theme(step1.get("primary_color", "blue"))
    header_bg = theme["header_bg"]
    header_text = theme["header_text"]
    accent = theme["accent"]
    accent_text = theme["accent_text"]

    company = (step2.get("company_name") or "").strip() or "（会社名 未入力）"
    catch = (step2.get("catch_copy") or "").strip() or "（キャッチコピー 未入力）"
    phone = (step2.get("phone") or "").strip()
    addr = (step2.get("address") or "").strip()
    email = (step2.get("email") or "").strip()

    hero = blocks.get("hero", {})
    sub_catch = (hero.get("sub_catch") or "").strip()
    hero_image = (hero.get("hero_image") or "A: オフィス").strip()
    hero_image_url = (hero.get("hero_image_url") or "").strip()
    btn_primary = (hero.get("primary_button_text") or "お問い合わせ").strip() or "お問い合わせ"
    btn_secondary = (hero.get("secondary_button_text") or "").strip()

    if hero_image_url:
        hero_url = hero_image_url
    else:
        hero_url = HERO_IMAGE_PRESET_URLS.get(hero_image, HERO_IMAGE_PRESET_URLS["A: オフィス"])

    philosophy = blocks.get("philosophy", {})
    ph_title = (philosophy.get("title") or "").strip() or "（見出し 未入力）"
    ph_body = (philosophy.get("body") or "").strip()
    ph_points = philosophy.get("points") or []
    if not isinstance(ph_points, list):
        ph_points = []

    news = blocks.get("news", {})
    news_items = news.get("items") or []
    if not isinstance(news_items, list):
        news_items = []

    faq = blocks.get("faq", {})
    faq_items = faq.get("items") or []
    if not isinstance(faq_items, list):
        faq_items = []

    access = blocks.get("access", {})
    map_url = (access.get("map_url") or "").strip() or google_maps_url(addr)
    access_notes = (access.get("notes") or "").strip()

    contact = blocks.get("contact", {})
    hours = (contact.get("hours") or "").strip()
    message = (contact.get("message") or "").strip()

    def label_pre(text: str, classes: str = "") -> None:
        ui.label(text).classes(classes).style("white-space: pre-wrap;")

    def section_title(icon_name: str, title: str) -> None:
        with ui.row().classes("items-center q-gutter-sm q-mb-sm"):
            ui.icon(icon_name).classes(f"text-{accent}")
            ui.label(title).classes("text-subtitle1")

    with ui.column().classes("w-full"):
        # Top bar
        with ui.element("div").classes(f"w-full bg-{header_bg}").style("border-bottom: 1px solid rgba(0,0,0,0.08);"):
            with ui.row().classes(f"items-center justify-between q-px-md text-{header_text}").style("height: 52px;"):
                ui.label(company).classes("text-subtitle2")
                ui.icon("menu").classes(f"text-{header_text}")

        # Hero
        with ui.element("div").style(
            f"height: 260px; background-image: url('{hero_url}'); background-size: cover; background-position: center;"
        ).classes("w-full"):
            with ui.element("div").style("height:100%; background: rgba(0,0,0,0.48);"):
                with ui.column().classes("q-pa-md text-white").style("height:100%; justify-content:flex-end;"):
                    ui.label(catch).classes("text-h6").style("line-height: 1.2;")
                    if sub_catch:
                        label_pre(sub_catch, "text-body2")
                    ui.label(industry).classes("text-caption").style("opacity: 0.9;")

                    with ui.row().classes("q-gutter-sm q-mt-sm"):
                        ui.button(btn_primary).props(f"color={accent} text-color={accent_text} unelevated")
                        if btn_secondary:
                            ui.button(btn_secondary).props(f"outline color={accent}")

        # Philosophy
        with ui.element("div").classes("w-full bg-white"):
            with ui.column().classes("q-pa-md"):
                section_title("favorite", ph_title)
                if ph_body:
                    label_pre(ph_body, "text-body2")
                else:
                    ui.label("（本文 未入力）").classes("text-caption text-grey")

                pts = [str(x).strip() for x in ph_points][:3]
                pts = [x for x in pts if x]
                if pts:
                    with ui.row().classes("q-gutter-xs q-mt-sm"):
                        for t in pts:
                            ui.badge(t).props("outline").classes(f"text-{accent}")

        # News
        with ui.element("div").classes("w-full bg-grey-1"):
            with ui.column().classes("q-pa-md"):
                section_title("campaign", "お知らせ")
                if not news_items:
                    ui.label("まだお知らせはありません").classes("text-caption text-grey")
                else:
                    for it in news_items[:3]:
                        date = (it.get("date") or "").strip()
                        cat = (it.get("category") or "").strip() or "お知らせ"
                        title = (it.get("title") or "").strip() or "（タイトル 未入力）"
                        body = (it.get("body") or "").strip()

                        with ui.card().classes("q-pa-md q-mb-sm rounded-borders").props("flat bordered"):
                            with ui.row().classes("items-start justify-between q-gutter-sm"):
                                ui.label(title).classes("text-body1")
                                ui.badge(cat).props("outline").classes(f"text-{accent}")
                            if date:
                                ui.label(date).classes("text-caption text-grey")
                            if body:
                                snippet = body.replace("\n", " ")
                                if len(snippet) > 70:
                                    snippet = snippet[:70] + "…"
                                ui.label(snippet).classes("text-caption")

                    ui.button("お知らせ一覧（仮）").props(f"flat color={accent}").classes("q-mt-xs")

        # FAQ
        with ui.element("div").classes("w-full bg-white"):
            with ui.column().classes("q-pa-md"):
                section_title("help", "よくある質問")
                if not faq_items:
                    ui.label("まだFAQはありません").classes("text-caption text-grey")
                else:
                    for it in faq_items[:6]:
                        q = (it.get("q") or "").strip() or "（質問 未入力）"
                        a = (it.get("a") or "").strip() or "（回答 未入力）"
                        with ui.card().classes("q-pa-md q-mb-sm rounded-borders").props("flat bordered"):
                            ui.label(q).classes("text-body1")
                            ui.separator().classes("q-my-sm")
                            label_pre(a, "text-body2")

        # Access
        with ui.element("div").classes("w-full bg-grey-1"):
            with ui.column().classes("q-pa-md"):
                section_title("place", "アクセス")
                ui.label(f"住所：{addr if addr else '未入力'}").classes("text-body2")
                if access_notes:
                    label_pre(access_notes, "text-caption text-grey")
                if map_url:
                    ui.button(
                        "地図を開く",
                        on_click=lambda u=map_url: ui.run_javascript(f"window.open('{u}','_blank')")
                    ).props(f"color={accent} text-color={accent_text} unelevated").classes("q-mt-sm")
                else:
                    ui.label("住所を入力すると地図ボタンが出ます").classes("text-caption text-grey")

        # Contact
        with ui.element("div").classes("w-full bg-white"):
            with ui.column().classes("q-pa-md"):
                section_title("call", "お問い合わせ")
                with ui.card().classes("q-pa-md rounded-borders").props("flat bordered"):
                    if message:
                        label_pre(message, "text-body2")
                        ui.separator().classes("q-my-sm")

                    ui.label(f"TEL：{phone if phone else '未入力'}").classes("text-body1")
                    if hours:
                        ui.label(f"受付時間：{hours}").classes("text-caption text-grey")
                    ui.label(f"Email：{email if email else '未入力'}").classes("text-body2")

                    ui.button(btn_primary).props(f"color={accent} text-color={accent_text} unelevated").classes("q-mt-sm")

        # Footer
        with ui.element("div").classes("w-full bg-grey-9 text-white"):
            with ui.column().classes("q-pa-md"):
                ui.label(company).classes("text-subtitle2")
                if addr:
                    ui.label(addr).classes("text-caption")
                if phone:
                    ui.label(f"TEL: {phone}").classes("text-caption")


def render_main(u: User) -> None:
    inject_global_styles()
    render_header(u)

    p = get_current_project()

    # プレビュー更新（スマホ/PC両方）
    preview_ref = {"mobile": (lambda: None), "desktop": (lambda: None)}

    def refresh_preview() -> None:
        for k in ("mobile", "desktop"):
            try:
                preview_ref[k]()
            except Exception:
                pass

    with ui.element("div").classes("cvhb-page"):
        with ui.element("div").classes("cvhb-container"):
            # ★ ここが左右表示の本体：Quasarの row は標準で wrap するので
            #   「入るなら左右」「入らないなら縦」が確実に動く
            with ui.row().classes("w-full q-col-gutter-md items-start"):
                # ---------------------------
                # Left: Builder（幅固定）
                # ---------------------------
                with ui.column().style("width: min(520px, 100%); flex: 0 0 auto;"):
                    if not p:
                        with ui.card().classes("q-pa-md rounded-borders").props("bordered"):
                            ui.label("案件が未選択です").classes("text-subtitle1 q-mb-sm")
                            ui.label("まず案件を作成/選択してください。").classes("text-body2 q-mb-md")
                            ui.button("案件一覧へ", on_click=lambda: navigate_to("/projects")).props("color=primary unelevated").classes("w-full")
                        return

                    # 1つめ：現在の案件
                    with ui.card().classes("q-pa-md rounded-borders").props("bordered"):
                        with ui.row().classes("items-center justify-between q-mb-sm"):
                            with ui.row().classes("items-center q-gutter-sm"):
                                ui.icon("folder").classes("text-grey-8")
                                ui.label("現在の案件").classes("text-subtitle1")
                            ui.button("案件一覧", on_click=lambda: navigate_to("/projects")).props("flat")

                        ui.label(f"現在の案件：{p.get('project_name','')}")
                        ui.label(f"ID：{p.get('project_id','')}").classes("text-caption text-grey")

                        ui.separator().classes("q-my-sm")

                        ui.label(f"案件開始日：{fmt_dt(p.get('created_at',''))}").classes("text-body2")
                        ui.label(f"最新更新日：{fmt_dt(p.get('updated_at',''))}").classes("text-body2")

                        updated_by = (p.get("updated_by") or "").strip() or u.username
                        ui.label(f"更新担当者：{updated_by}").classes("text-body2")

                        def do_save():
                            try:
                                save_project_to_sftp(p, u)
                                set_current_project(p)
                                ui.notify("保存しました（SFTP / project.json）", type="positive")
                            except Exception as e:
                                ui.notify(f"保存に失敗: {e}", type="negative")

                        ui.button("保存（PROJECT.JSON）", on_click=do_save).props("color=primary unelevated").classes("q-mt-sm w-full")

                    # 2つめ：作成ステップ（選択）
                    steps = [
                        ("s1", "1. 業種設定・ページカラー設定"),
                        ("s2", "2. 基本情報設定"),
                        ("s3", "3. ページ内容詳細設定（ブロックごと）"),
                        ("s4", "4. 承認・最終チェック"),
                        ("s5", "5. 公開（管理者権限のみ）"),
                    ]

                    with ui.card().classes("q-pa-md rounded-borders q-mt-md").props("bordered"):
                        with ui.row().classes("items-center q-gutter-sm q-mb-sm"):
                            ui.icon("format_list_bulleted").classes("text-grey-8")
                            ui.label("作成ステップ").classes("text-subtitle1")
                        ui.label("ここでステップを選ぶと、下の入力画面が切り替わります。").classes("text-caption text-grey q-mb-sm")

                        with ui.tabs().props("vertical dense").classes("w-full") as tabs:
                            for key, label in steps:
                                ui.tab(key, label=label)

                    # 3つめ：入力画面（ステップ内容）
                    with ui.card().classes("q-pa-md rounded-borders q-mt-md").props("bordered"):
                        with ui.tab_panels(tabs, value="s1").props("vertical").classes("w-full"):
                            # Step1
                            with ui.tab_panel("s1"):
                                ui.label("1. 業種設定・ページカラー設定").classes("text-subtitle1 q-mb-xs")
                                ui.label("最初にここを決めると、右の完成イメージが一気に整います。").classes("text-caption text-grey q-mb-md")

                                # --- 業種選択（大枠） ---
                                with ui.card().classes("q-pa-md rounded-borders").props("flat bordered"):
                                    ui.label("業種を選んでください").classes("text-subtitle2")
                                    ui.label("※ v1.0 は「会社・企業サイト」をまず商用ラインまで完成させます。").classes("text-caption text-grey q-mb-sm")

                                    @ui.refreshable
                                    def industry_selector() -> None:
                                        current = normalize_industry(p["data"]["step1"].get("industry", "会社・企業サイト"))
                                        p["data"]["step1"]["industry"] = current

                                        theme = get_theme(p["data"]["step1"].get("primary_color", "blue"))
                                        accent = theme["accent"]

                                        for it in INDUSTRY_PRESETS:
                                            selected = (it["value"] == current)
                                            with ui.card().classes("q-pa-sm rounded-borders cvhb-option-card q-mb-sm").props("flat bordered") as c:
                                                if selected:
                                                    c.classes("bg-grey-2")

                                                def _pick(v=it["value"]):
                                                    p["data"]["step1"]["industry"] = v
                                                    set_current_project(p)
                                                    refresh_preview()
                                                    industry_selector.refresh()

                                                c.on("click", lambda e, v=it["value"]: _pick(v))

                                                with ui.row().classes("items-start q-gutter-sm"):
                                                    ui.icon("check_circle" if selected else "radio_button_unchecked").classes(
                                                        f"text-{accent}"
                                                    ).style("margin-top: 2px;")
                                                    with ui.column().classes("col"):
                                                        ui.label(it["label"]).classes("text-body1")
                                                        ui.label(f"特徴：{it['feature']}").classes("text-caption text-grey")

                                    industry_selector()

                                ui.separator().classes("q-my-md")

                                # --- カラー選択（大枠） ---
                                with ui.card().classes("q-pa-md rounded-borders").props("flat bordered"):
                                    ui.label("ページカラー設定").classes("text-subtitle2")
                                    ui.label("ボタン・見出しなどの雰囲気を統一できます。").classes("text-body2")
                                    ui.label("※ページカラーのイメージを選択してください。").classes("text-caption text-grey q-mb-sm")

                                    @ui.refreshable
                                    def theme_selector() -> None:
                                        current_key = normalize_theme_key(p["data"]["step1"].get("primary_color", "blue"))
                                        p["data"]["step1"]["primary_color"] = current_key

                                        for t in THEME_PRESETS:
                                            selected = (t["key"] == current_key)
                                            with ui.card().classes("q-pa-sm rounded-borders cvhb-option-card q-mb-sm").props("flat bordered") as c:
                                                if selected:
                                                    c.classes("bg-grey-2")

                                                def _pick_theme(k=t["key"]):
                                                    p["data"]["step1"]["primary_color"] = k
                                                    set_current_project(p)
                                                    refresh_preview()
                                                    theme_selector.refresh()
                                                    industry_selector.refresh()  # チェック色も追従

                                                c.on("click", lambda e, k=t["key"]: _pick_theme(k))

                                                with ui.row().classes("items-center q-gutter-sm"):
                                                    ui.icon("check_circle" if selected else "radio_button_unchecked").classes(
                                                        "text-grey-7" if not selected else "text-primary"
                                                    )
                                                    ui.element("div").classes(f"cvhb-swatch bg-{t['swatch_bg']}")
                                                    ui.label(f"{t['label']}").classes("text-body1")
                                                    ui.label(f"（{t['impression']}）").classes("text-caption text-grey")

                                    theme_selector()

                            # Step2
                            with ui.tab_panel("s2"):
                                ui.label("2. 基本情報設定").classes("text-subtitle1 q-mb-xs")
                                ui.label("入力すると右のプレビューに反映されます。").classes("text-caption text-grey q-mb-md")

                                with ui.card().classes("q-pa-md rounded-borders").props("flat bordered"):
                                    ui.label("会社の基本情報設定").classes("text-subtitle2 q-mb-sm")

                                    def bind_input(key: str, label: str):
                                        val = p["data"]["step2"].get(key, "")

                                        def _on_change(e):
                                            p["data"]["step2"][key] = e.value
                                            set_current_project(p)
                                            refresh_preview()

                                        ui.input(label, value=val, on_change=_on_change).props("outlined").classes("w-full q-mb-sm")

                                    bind_input("company_name", "会社名")
                                    bind_input("catch_copy", "キャッチコピー")
                                    bind_input("phone", "電話番号")
                                    bind_input("email", "メール（任意）")
                                    bind_input("address", "住所（地図リンクは自動生成されます）")

                            # Step3
                            with ui.tab_panel("s3"):
                                ui.label("3. ページ内容詳細設定（ブロックごと）").classes("text-subtitle1 q-mb-xs")
                                ui.label("ブロックを切り替えて編集できます。迷わないように整理してあります。").classes("text-caption text-grey q-mb-md")

                                def update_block(block_key: str, field_key: str, value) -> None:
                                    p["data"]["blocks"].setdefault(block_key, {})[field_key] = value
                                    set_current_project(p)
                                    refresh_preview()

                                def update_points(index: int, value) -> None:
                                    ph = p["data"]["blocks"].setdefault("philosophy", {})
                                    pts = ph.setdefault("points", ["", "", ""])
                                    if not isinstance(pts, list):
                                        pts = ["", "", ""]
                                    while len(pts) < 3:
                                        pts.append("")
                                    pts[index] = value
                                    ph["points"] = pts[:3]
                                    set_current_project(p)
                                    refresh_preview()

                                def update_list_item(block_key: str, list_key: str, idx: int, field_key: str, value) -> None:
                                    b = p["data"]["blocks"].setdefault(block_key, {})
                                    items = b.setdefault(list_key, [])
                                    if not isinstance(items, list):
                                        items = []
                                        b[list_key] = items
                                    if 0 <= idx < len(items) and isinstance(items[idx], dict):
                                        items[idx][field_key] = value
                                    set_current_project(p)
                                    refresh_preview()

                                news_ref = {"refresh": (lambda: None)}
                                faq_ref = {"refresh": (lambda: None)}

                                def add_news_item() -> None:
                                    news = p["data"]["blocks"].setdefault("news", {})
                                    items = news.setdefault("items", [])
                                    if not isinstance(items, list):
                                        items = []
                                    items.append({
                                        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                                        "category": "お知らせ",
                                        "title": "",
                                        "body": "",
                                    })
                                    news["items"] = items
                                    set_current_project(p)
                                    refresh_preview()
                                    news_ref["refresh"]()

                                def delete_news_item(idx: int) -> None:
                                    news = p["data"]["blocks"].setdefault("news", {})
                                    items = news.setdefault("items", [])
                                    if isinstance(items, list) and 0 <= idx < len(items):
                                        del items[idx]
                                    set_current_project(p)
                                    refresh_preview()
                                    news_ref["refresh"]()

                                def add_faq_item() -> None:
                                    faq = p["data"]["blocks"].setdefault("faq", {})
                                    items = faq.setdefault("items", [])
                                    if not isinstance(items, list):
                                        items = []
                                    items.append({"q": "", "a": ""})
                                    faq["items"] = items
                                    set_current_project(p)
                                    refresh_preview()
                                    faq_ref["refresh"]()

                                def delete_faq_item(idx: int) -> None:
                                    faq = p["data"]["blocks"].setdefault("faq", {})
                                    items = faq.setdefault("items", [])
                                    if isinstance(items, list) and 0 <= idx < len(items):
                                        del items[idx]
                                    set_current_project(p)
                                    refresh_preview()
                                    faq_ref["refresh"]()

                                with ui.card().classes("q-pa-md rounded-borders").props("flat bordered"):
                                    ui.label("ブロック編集（会社テンプレ：6ブロック）").classes("text-subtitle2")
                                    ui.label("ヒーロー / 理念 / お知らせ / FAQ / アクセス / お問い合わせ").classes("text-caption text-grey q-mb-sm")

                                    with ui.tabs().props("dense").classes("w-full") as block_tabs:
                                        ui.tab("hero", label="ヒーロー")
                                        ui.tab("philosophy", label="理念/概要")
                                        ui.tab("news", label="お知らせ")
                                        ui.tab("faq", label="FAQ")
                                        ui.tab("access", label="アクセス")
                                        ui.tab("contact", label="お問い合わせ")

                                    with ui.tab_panels(block_tabs, value="hero").classes("w-full q-mt-md"):
                                        with ui.tab_panel("hero"):
                                            hero = p["data"]["blocks"].get("hero", {})
                                            with ui.card().classes("q-pa-md rounded-borders").props("flat bordered"):
                                                ui.label("ヒーロー（ページ最上部）").classes("text-subtitle2")
                                                ui.label("大きい写真 + キャッチコピーのエリアです").classes("text-caption text-grey q-mb-sm")

                                                current_img = hero.get("hero_image", "A: オフィス")

                                                def on_hero_image_change(e):
                                                    update_block("hero", "hero_image", e.value)

                                                ui.radio(HERO_IMAGE_OPTIONS, value=current_img, on_change=on_hero_image_change).props("dense")

                                                ui.input(
                                                    "画像URL（任意：貼るだけ）",
                                                    value=hero.get("hero_image_url", ""),
                                                    on_change=lambda e: update_block("hero", "hero_image_url", e.value),
                                                ).props("outlined").classes("w-full q-mt-sm")

                                                ui.input(
                                                    "キャッチの補足（任意）",
                                                    value=hero.get("sub_catch", ""),
                                                    on_change=lambda e: update_block("hero", "sub_catch", e.value),
                                                ).props("outlined").classes("w-full q-mt-sm")

                                                ui.input(
                                                    "メインボタン文字（例：お問い合わせ）",
                                                    value=hero.get("primary_button_text", ""),
                                                    on_change=lambda e: update_block("hero", "primary_button_text", e.value),
                                                ).props("outlined").classes("w-full q-mt-sm")

                                                ui.input(
                                                    "サブボタン文字（任意：例：見学・相談）",
                                                    value=hero.get("secondary_button_text", ""),
                                                    on_change=lambda e: update_block("hero", "secondary_button_text", e.value),
                                                ).props("outlined").classes("w-full q-mt-sm")

                                        with ui.tab_panel("philosophy"):
                                            ph = p["data"]["blocks"].get("philosophy", {})
                                            pts = ph.get("points") or ["", "", ""]
                                            if not isinstance(pts, list):
                                                pts = ["", "", ""]
                                            while len(pts) < 3:
                                                pts.append("")

                                            with ui.card().classes("q-pa-md rounded-borders").props("flat bordered"):
                                                ui.label("理念 / 概要").classes("text-subtitle2")
                                                ui.label("会社紹介・想いを書きます").classes("text-caption text-grey q-mb-sm")

                                                ui.input(
                                                    "見出し（例：私たちの想い）",
                                                    value=ph.get("title", ""),
                                                    on_change=lambda e: update_block("philosophy", "title", e.value),
                                                ).props("outlined").classes("w-full q-mb-sm")

                                                ui.input(
                                                    "本文（長文OK）",
                                                    value=ph.get("body", ""),
                                                    on_change=lambda e: update_block("philosophy", "body", e.value),
                                                ).props("outlined type=textarea autogrow").classes("w-full q-mb-sm")

                                                for i in range(3):
                                                    ui.input(
                                                        f"ポイント{i+1}（短く）",
                                                        value=str(pts[i] or ""),
                                                        on_change=lambda e, i=i: update_points(i, e.value),
                                                    ).props("outlined").classes("w-full q-mb-sm")

                                        with ui.tab_panel("news"):
                                            ui.label("お知らせ").classes("text-subtitle2 q-mb-sm")

                                            @ui.refreshable
                                            def news_editor() -> None:
                                                blocks2 = p["data"]["blocks"]
                                                items = (blocks2.get("news", {}) or {}).get("items") or []
                                                if not isinstance(items, list):
                                                    items = []

                                                if not items:
                                                    ui.label("お知らせがまだありません。下のボタンで追加できます。").classes("text-caption text-grey")

                                                for idx, it in enumerate(items):
                                                    if not isinstance(it, dict):
                                                        continue
                                                    with ui.card().classes("q-pa-md q-mb-sm rounded-borders").props("flat bordered"):
                                                        with ui.row().classes("items-center justify-between"):
                                                            ui.label(f"お知らせ {idx+1}").classes("text-body1")
                                                            ui.button("削除", on_click=lambda idx=idx: delete_news_item(idx)).props("color=negative flat")

                                                        ui.input(
                                                            "日付",
                                                            value=it.get("date", ""),
                                                            on_change=lambda e, idx=idx: update_list_item("news", "items", idx, "date", e.value),
                                                        ).props("outlined type=date").classes("w-full q-mb-sm")

                                                        ui.input(
                                                            "カテゴリ（例：お知らせ）",
                                                            value=it.get("category", ""),
                                                            on_change=lambda e, idx=idx: update_list_item("news", "items", idx, "category", e.value),
                                                        ).props("outlined").classes("w-full q-mb-sm")

                                                        ui.input(
                                                            "タイトル",
                                                            value=it.get("title", ""),
                                                            on_change=lambda e, idx=idx: update_list_item("news", "items", idx, "title", e.value),
                                                        ).props("outlined").classes("w-full q-mb-sm")

                                                        ui.input(
                                                            "本文（長文OK）",
                                                            value=it.get("body", ""),
                                                            on_change=lambda e, idx=idx: update_list_item("news", "items", idx, "body", e.value),
                                                        ).props("outlined type=textarea autogrow").classes("w-full")

                                                ui.button("＋ お知らせを追加", on_click=add_news_item).props("flat color=primary").classes("q-mt-sm")

                                            news_editor()
                                            news_ref["refresh"] = news_editor.refresh

                                        with ui.tab_panel("faq"):
                                            ui.label("FAQ（よくある質問）").classes("text-subtitle2 q-mb-sm")

                                            @ui.refreshable
                                            def faq_editor() -> None:
                                                blocks2 = p["data"]["blocks"]
                                                items = (blocks2.get("faq", {}) or {}).get("items") or []
                                                if not isinstance(items, list):
                                                    items = []

                                                if not items:
                                                    ui.label("FAQがまだありません。下のボタンで追加できます。").classes("text-caption text-grey")

                                                for idx, it in enumerate(items):
                                                    if not isinstance(it, dict):
                                                        continue
                                                    with ui.card().classes("q-pa-md q-mb-sm rounded-borders").props("flat bordered"):
                                                        with ui.row().classes("items-center justify-between"):
                                                            ui.label(f"FAQ {idx+1}").classes("text-body1")
                                                            ui.button("削除", on_click=lambda idx=idx: delete_faq_item(idx)).props("color=negative flat")

                                                        ui.input(
                                                            "質問",
                                                            value=it.get("q", ""),
                                                            on_change=lambda e, idx=idx: update_list_item("faq", "items", idx, "q", e.value),
                                                        ).props("outlined").classes("w-full q-mb-sm")

                                                        ui.input(
                                                            "回答（長文OK）",
                                                            value=it.get("a", ""),
                                                            on_change=lambda e, idx=idx: update_list_item("faq", "items", idx, "a", e.value),
                                                        ).props("outlined type=textarea autogrow").classes("w-full")

                                                ui.button("＋ FAQを追加", on_click=add_faq_item).props("flat color=primary").classes("q-mt-sm")

                                            faq_editor()
                                            faq_ref["refresh"] = faq_editor.refresh

                                        with ui.tab_panel("access"):
                                            ac = p["data"]["blocks"].get("access", {})
                                            with ui.card().classes("q-pa-md rounded-borders").props("flat bordered"):
                                                ui.label("アクセス").classes("text-subtitle2")
                                                ui.label("地図URLは空でもOK（住所から自動生成します）").classes("text-caption text-grey q-mb-sm")

                                                ui.input(
                                                    "地図URL（任意）",
                                                    value=ac.get("map_url", ""),
                                                    on_change=lambda e: update_block("access", "map_url", e.value),
                                                ).props("outlined").classes("w-full q-mb-sm")

                                                ui.input(
                                                    "補足（例：〇〇駅から徒歩5分 / 駐車場あり）",
                                                    value=ac.get("notes", ""),
                                                    on_change=lambda e: update_block("access", "notes", e.value),
                                                ).props("outlined type=textarea autogrow").classes("w-full")

                                        with ui.tab_panel("contact"):
                                            ct = p["data"]["blocks"].get("contact", {})
                                            with ui.card().classes("q-pa-md rounded-borders").props("flat bordered"):
                                                ui.label("お問い合わせ").classes("text-subtitle2")
                                                ui.label("まずは電話・メールを目立たせます（フォームはv0.8で強化）").classes("text-caption text-grey q-mb-sm")

                                                ui.input(
                                                    "受付時間（例：平日 9:00〜18:00）",
                                                    value=ct.get("hours", ""),
                                                    on_change=lambda e: update_block("contact", "hours", e.value),
                                                ).props("outlined").classes("w-full q-mb-sm")

                                                ui.input(
                                                    "一言メッセージ（任意）",
                                                    value=ct.get("message", ""),
                                                    on_change=lambda e: update_block("contact", "message", e.value),
                                                ).props("outlined type=textarea autogrow").classes("w-full")

                            # Step4
                            with ui.tab_panel("s4"):
                                ui.label("4. 承認・最終チェック").classes("text-subtitle1 q-mb-xs")
                                ui.label("v0.7.0で承認フロー（OK/差戻し）を実装します。").classes("text-body2")

                            # Step5
                            with ui.tab_panel("s5"):
                                ui.label("5. 公開（管理者権限のみ）").classes("text-subtitle1 q-mb-xs")
                                ui.label("v0.7.0で公開（アップロード）を実装します。").classes("text-body2")

                # ---------------------------
                # Right: Preview（スマホ/PC切替）
                # ---------------------------
                with ui.column().classes("cvhb-preview-sticky").style("flex: 1 1 360px; min-width: 0;"):
                    with ui.card().classes("q-pa-md rounded-borders").props("bordered"):
                        with ui.row().classes("items-center justify-between q-mb-sm"):
                            with ui.row().classes("items-center q-gutter-sm"):
                                ui.icon("visibility").classes("text-grey-8")
                                ui.label("プレビュー").classes("text-subtitle1")
                            ui.badge("スマホ / PC 切替").props("outline")

                        with ui.tabs().props("dense align=justify").classes("w-full") as preview_tabs:
                            ui.tab("mobile", icon="smartphone", label="スマホ")
                            ui.tab("desktop", icon="desktop_windows", label="PC")

                        with ui.tab_panels(preview_tabs, value="mobile").classes("w-full q-mt-md"):
                            with ui.tab_panel("mobile"):
                                with ui.column().classes("w-full items-center"):
                                    with ui.card().classes("q-pa-none rounded-borders shadow-1").style(
                                        "width: clamp(320px, 38vw, 420px);"
                                        "height: clamp(560px, 75vh, 740px);"
                                        "border: 1px solid #ddd;"
                                        "overflow: hidden;"
                                        "background: white;"
                                    ).props("flat"):
                                        with ui.element("div").style("height: 100%; overflow-y: auto;"):
                                            @ui.refreshable
                                            def preview_mobile():
                                                render_preview(p)

                                            preview_mobile()
                                            preview_ref["mobile"] = preview_mobile.refresh

                            with ui.tab_panel("desktop"):
                                with ui.column().classes("w-full items-center"):
                                    with ui.card().classes("q-pa-none rounded-borders shadow-1").style(
                                        "width: min(1100px, 70vw);"
                                        "height: clamp(560px, 78vh, 820px);"
                                        "border: 1px solid #ddd;"
                                        "overflow: hidden;"
                                        "background: white;"
                                    ).props("flat"):
                                        with ui.element("div").style("height: 100%; overflow-y: auto;"):
                                            @ui.refreshable
                                            def preview_desktop():
                                                render_preview(p)

                                            preview_desktop()
                                            preview_ref["desktop"] = preview_desktop.refresh


# =========================
# Pages
# =========================

init_db_schema()


@ui.page("/")
def index() -> None:
    inject_global_styles()
    ui.page_title("CV-HomeBuilder")

    root = ui.column().classes("w-full")

    @ui.refreshable
    def root_refresh() -> None:
        root.clear()
        with root:
            u = current_user()

            if APP_ENV != "stg" and count_users() == 0:
                render_header(None)
                render_first_admin_setup(root_refresh)
                return

            if not u:
                render_header(None)
                render_login(root_refresh)
                return

            render_main(u)

    root_refresh()


@ui.page("/projects")
def projects_page() -> None:
    inject_global_styles()
    ui.page_title("Projects - CV-HomeBuilder")

    u = current_user()
    if not u:
        ui.notify("ログインが必要です", type="warning")
        navigate_to("/")
        return

    render_header(u)

    with ui.element("div").classes("cvhb-page"):
        with ui.element("div").classes("cvhb-container"):
            ui.label(f"案件一覧（v{VERSION}）").classes("text-h6 q-mb-md")

            with ui.card().classes("q-pa-md rounded-borders").props("bordered"):
                name_input = ui.input("新規案件名（例：アルカーサ株式会社）").props("outlined").classes("q-mb-sm").style("max-width:520px;")

                @ui.refreshable
                def list_refresh() -> None:
                    try:
                        items = list_projects_from_sftp()
                    except Exception as e:
                        ui.notify(f"一覧取得に失敗: {e}", type="negative")
                        items = []

                    ui.separator().classes("q-my-sm")

                    if not items:
                        ui.label("案件がまだありません。上で新規作成してください。").classes("text-body2")
                        return

                    for it in items:
                        with ui.card().classes("q-pa-md q-mb-sm rounded-borders").props("flat bordered"):
                            ui.label(it.get("project_name", "")).classes("text-body1")
                            ui.label(f"ID: {it.get('project_id','')}").classes("text-caption text-grey")
                            ui.label(f"更新: {fmt_dt(it.get('updated_at',''))}").classes("text-caption text-grey")

                            def open_this(pid=it.get("project_id", "")):
                                try:
                                    p2 = load_project_from_sftp(pid, u)
                                    set_current_project(p2)
                                    ui.notify("案件を開きました", type="positive")
                                    navigate_to("/")
                                except Exception as e:
                                    ui.notify(f"開けませんでした: {e}", type="negative")

                            ui.button("開く", on_click=open_this).props("color=primary unelevated").classes("q-mt-sm")

                def create_new():
                    name = (name_input.value or "").strip()
                    if not name:
                        ui.notify("案件名を入力してください", type="warning")
                        return
                    try:
                        p2 = create_project(name, u)
                        save_project_to_sftp(p2, u)
                        set_current_project(p2)
                        ui.notify("新規案件を作成しました", type="positive")
                        name_input.value = ""
                        list_refresh.refresh()
                        navigate_to("/")
                    except Exception as e:
                        ui.notify(f"作成に失敗: {e}", type="negative")

                with ui.row().classes("q-gutter-sm q-mt-sm"):
                    ui.button("新規作成", on_click=create_new).props("color=primary unelevated")
                    ui.button("更新", on_click=list_refresh.refresh).props("flat")

                list_refresh()


@ui.page("/audit")
def audit_page() -> None:
    inject_global_styles()
    ui.page_title("Audit Logs - CV-HomeBuilder")

    u = current_user()
    if not u:
        ui.notify("ログインが必要です", type="warning")
        navigate_to("/")
        return

    if u.role not in {"admin", "subadmin"}:
        render_header(u)
        with ui.element("div").classes("cvhb-page"):
            with ui.element("div").classes("cvhb-container"):
                ui.label("権限がありません（管理者/副管理者のみ）").classes("text-negative q-mb-md")
                ui.button("戻る", on_click=lambda: navigate_to("/")).props("color=primary unelevated")
        return

    render_header(u)

    with ui.element("div").classes("cvhb-page"):
        with ui.element("div").classes("cvhb-container"):
            ui.label("操作ログ（最新200件）").classes("text-h6 q-mb-md")

            with ui.card().classes("q-pa-md rounded-borders").props("bordered"):
                limit_input = ui.number("表示件数", value=200, min=10, max=1000).props("outlined").classes("q-mb-sm")
                action_input = ui.input("actionで絞り込み（例: login_success）").props("outlined").classes("q-mb-sm")

                @ui.refreshable
                def table_refresh() -> None:
                    limit = int(limit_input.value or 200)
                    action = (action_input.value or "").strip()

                    if action:
                        rows = db_fetchall(
                            """
                            SELECT id, created_at, username, role, action, details
                            FROM audit_logs
                            WHERE action = %s
                            ORDER BY created_at DESC
                            LIMIT %s
                            """,
                            (action, limit),
                        )
                    else:
                        rows = db_fetchall(
                            """
                            SELECT id, created_at, username, role, action, details
                            FROM audit_logs
                            ORDER BY created_at DESC
                            LIMIT %s
                            """,
                            (limit,),
                        )

                    for r in rows:
                        if r.get("created_at"):
                            r["created_at"] = r["created_at"].strftime("%Y-%m-%d %H:%M:%S")

                    columns = [
                        {"name": "created_at", "label": "日時", "field": "created_at", "sortable": True},
                        {"name": "username", "label": "ユーザー", "field": "username", "sortable": True},
                        {"name": "role", "label": "権限", "field": "role", "sortable": True},
                        {"name": "action", "label": "操作", "field": "action", "sortable": True},
                        {"name": "details", "label": "詳細", "field": "details"},
                    ]

                    ui.table(columns=columns, rows=rows, row_key="id").classes("w-full")

                ui.button("更新", on_click=table_refresh.refresh).props("color=primary unelevated").classes("q-mb-md")
                table_refresh()


if __name__ in {"__main__", "__mp_main__"}:
    ui.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8080")),
        storage_secret=STORAGE_SECRET,
        title="CV-HomeBuilder",
    )
