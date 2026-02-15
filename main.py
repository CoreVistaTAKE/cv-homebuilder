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
from zoneinfo import ZoneInfo

import paramiko
import psycopg
from psycopg.rows import dict_row
from nicegui import app, ui


# =========================================================
# Global UI styles (v0.6.2)
# - CSSは static/cvhb.css に分離（見た目調整が楽）
# - レイアウトはCSS Gridで「広いと左右 / 狭いと縦」を安定化
# =========================================================

_STYLES_INJECTED = False
DEFAULT_CSS_PATH = Path("static/cvhb.css")


def _load_global_css() -> str:
    """static/cvhb.css があればそれを使い、なければ最低限のCSSを使う"""
    if DEFAULT_CSS_PATH.exists():
        try:
            return DEFAULT_CSS_PATH.read_text(encoding="utf-8")
        except Exception:
            pass

    # fallback（最小限）
    return """
/* fallback: static/cvhb.css が無い場合の最低限 */
.cvhb-page{background:#f5f5f5;min-height:calc(100vh - 64px);}
.cvhb-container{max-width:1400px;margin:0 auto;padding:16px;}
.cvhb-split{display:grid;grid-template-columns:520px minmax(0,1fr);gap:16px;align-items:start;}
.cvhb-left,.cvhb-right{min-width:0;}
@media (max-width: 900px){.cvhb-split{grid-template-columns:1fr;}}
@media (min-width: 901px){.cvhb-preview-sticky{position:sticky;top:88px;}}
"""


def inject_global_styles() -> None:
    global _STYLES_INJECTED
    if _STYLES_INJECTED:
        return

    css = _load_global_css()
    ui.add_head_html(f"<style>{css}</style>")
    _STYLES_INJECTED = True


# =========================
# Timezone (JST)
# =========================

JST = ZoneInfo("Asia/Tokyo")


def now_iso() -> str:
    """このプロジェクトの時刻はすべて日本時間（JST）"""
    return datetime.now(JST).isoformat(timespec="seconds")


def new_project_id() -> str:
    ts = datetime.now(JST).strftime("%Y%m%d%H%M%S")
    rnd = secrets.token_hex(3)
    return f"p{ts}_{rnd}"


def _parse_iso(iso_str: str) -> Optional[datetime]:
    s = (iso_str or "").strip()
    if not s:
        return None
    # 末尾Z（UTC）対策
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    try:
        return dt.astimezone(JST)
    except Exception:
        return dt


def fmt_jst(iso_str: str, fmt: str = "%Y-%m-%d %H:%M") -> str:
    dt = _parse_iso(iso_str)
    return dt.strftime(fmt) if dt else (iso_str or "")


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
# Projects (v0.6.2)
# =========================

INDUSTRY_PRESETS = [
    {
        "value": "会社サイト（企業）",
        "label": "会社・企業サイト",
        "feature": "特徴：会社テンプレ（6ブロック）を使います。まずここを商用ラインまで仕上げます。",
        "note": "",
    },
    {
        "value": "福祉事業所",
        "label": "福祉事業所",
        "feature": "特徴：利用者・ご家族向けの導線が中心（※テンプレは準備中）",
        "note": "準備中",
    },
    {
        "value": "個人事業",
        "label": "個人事業",
        "feature": "特徴：サービス紹介＋お問い合わせに最短で誘導（※テンプレは準備中）",
        "note": "準備中",
    },
    {
        "value": "その他",
        "label": "その他",
        "feature": "特徴：要件に合わせてカスタム（※テンプレは準備中）",
        "note": "準備中",
    },
]

COLOR_THEMES = [
    {"key": "white", "label": "白", "hex": "#ffffff", "impression": "清潔感"},
    {"key": "black", "label": "黒", "hex": "#111111", "impression": "高級感"},
    {"key": "red", "label": "赤", "hex": "#e53935", "impression": "情熱"},
    {"key": "blue", "label": "青", "hex": "#1e88e5", "impression": "信頼感"},
    {"key": "yellow", "label": "黄", "hex": "#fdd835", "impression": "明るさ"},
    {"key": "green", "label": "緑", "hex": "#43a047", "impression": "安心感"},
    {"key": "purple", "label": "紫", "hex": "#8e24aa", "impression": "上品"},
    {"key": "grey", "label": "灰", "hex": "#757575", "impression": "落ち着き"},
    {"key": "orange", "label": "オレンジ", "hex": "#fb8c00", "impression": "親しみ"},
]
COLOR_THEME_DEFAULT = "blue"


def theme_by_key(key: str) -> dict:
    k = (key or "").strip()
    for t in COLOR_THEMES:
        if t["key"] == k:
            return t
    for t in COLOR_THEMES:
        if t["key"] == COLOR_THEME_DEFAULT:
            return t
    return COLOR_THEMES[0]


def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    s = (hex_color or "").strip().lstrip("#")
    if len(s) == 3:
        s = "".join([c * 2 for c in s])
    if len(s) != 6:
        return (0, 0, 0)
    try:
        r = int(s[0:2], 16)
        g = int(s[2:4], 16)
        b = int(s[4:6], 16)
        return (r, g, b)
    except Exception:
        return (0, 0, 0)


def _rgb_to_hex(rgb: tuple[int, int, int]) -> str:
    r, g, b = rgb
    r = max(0, min(255, int(r)))
    g = max(0, min(255, int(g)))
    b = max(0, min(255, int(b)))
    return "#{:02x}{:02x}{:02x}".format(r, g, b)


def mix(hex_a: str, hex_b: str, ratio: float) -> str:
    """ratio: 0.0 -> a, 1.0 -> b"""
    ratio = max(0.0, min(1.0, float(ratio)))
    ar, ag, ab = _hex_to_rgb(hex_a)
    br, bg, bb = _hex_to_rgb(hex_b)
    rr = ar + (br - ar) * ratio
    rg = ag + (bg - ag) * ratio
    rb = ab + (bb - ab) * ratio
    return _rgb_to_hex((rr, rg, rb))


def contrast_text_color(bg_hex: str) -> str:
    """背景色に対して読みやすい文字色（白 or ほぼ黒）を返す"""
    r, g, b = _hex_to_rgb(bg_hex)
    brightness = (0.299 * r + 0.587 * g + 0.114 * b)
    return "#111111" if brightness > 170 else "#ffffff"


# =========================
# Blocks / Template presets (v0.6.2)
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
    # schema_version は project.json の管理用（アプリの VERSION と一致させなくてもOK）
    p["schema_version"] = "0.6.2"
    p.setdefault("project_id", new_project_id())
    p.setdefault("project_name", "(no name)")
    p.setdefault("created_at", now_iso())
    p.setdefault("updated_at", now_iso())
    p.setdefault("created_by", "")
    p.setdefault("updated_by", "")

    data = p.setdefault("data", {})
    step1 = data.setdefault("step1", {})
    step2 = data.setdefault("step2", {})
    blocks = data.setdefault("blocks", {})

    step1.setdefault("industry", "会社サイト（企業）")
    step1.setdefault("primary_color", COLOR_THEME_DEFAULT)

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
        "schema_version": "0.6.2",
        "project_id": pid,
        "project_name": name,
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "created_by": created_by.username if created_by else "",
        "updated_by": created_by.username if created_by else "",
        "data": {
            "step1": {"industry": "会社サイト（企業）", "primary_color": COLOR_THEME_DEFAULT},
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
                        u = current_user()
                        if u:
                            safe_log_action(u, "first_admin_created")
                        ui.notify("管理者を作成しました。ログインしました。", type="positive")
                        root_refresh()
                    else:
                        ui.notify("作成に失敗しました（同名ユーザーがいる可能性）", type="negative")

                ui.button("管理者を作成", on_click=create_admin).props("color=primary unelevated").classes("q-mt-md w-full")


def render_preview(p: dict, mode: str = "sp") -> None:
    """右側プレビュー（スマホ / PCでレイアウトを変える）"""
    p = normalize_project(p)
    step1 = p["data"]["step1"]
    step2 = p["data"]["step2"]
    blocks = p["data"]["blocks"]

    industry = step1.get("industry", "会社サイト（企業）")
    theme = theme_by_key(step1.get("primary_color", COLOR_THEME_DEFAULT))
    primary_hex = theme["hex"]
    on_primary = contrast_text_color(primary_hex)

    # 柔らかい背景（テーマ色を少し混ぜる）
    page_bg = mix(primary_hex, "#f5f5f5", 0.85)
    surface = "#ffffff"
    surface_alt = mix(primary_hex, "#ffffff", 0.94)

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

    hero_url = hero_image_url or HERO_IMAGE_PRESET_URLS.get(hero_image, HERO_IMAGE_PRESET_URLS["A: オフィス"])

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
            ui.icon(icon_name).style(f"color:{primary_hex};")
            ui.label(title).classes("text-subtitle1")

    def primary_button(text: str) -> None:
        ui.button(text).props("unelevated").style(
            f"background:{primary_hex}; color:{on_primary}; border-radius:10px;"
        )

    def outline_button(text: str) -> None:
        ui.button(text).props("outline").style(
            f"color:{primary_hex}; border-color:{primary_hex}; border-radius:10px;"
        )

    # ====== SMARTPHONE ======
    if mode == "sp":
        with ui.element("div").style(f"background:{page_bg}; min-height:100%;"):
            # top bar
            with ui.element("div").style(
                f"background:{primary_hex}; color:{on_primary}; height:48px;"
            ).classes("w-full"):
                with ui.row().classes("items-center justify-between q-px-md").style("height:48px;"):
                    ui.label(company).classes("text-subtitle2")
                    ui.icon("menu").style(f"color:{on_primary};")

            # hero
            with ui.element("div").style(
                f"height: 230px; background-image: url('{hero_url}'); background-size: cover; background-position: center;"
            ).classes("w-full"):
                with ui.element("div").style("height:100%; background: rgba(0,0,0,0.45);"):
                    with ui.column().classes("q-pa-md").style("height:100%; justify-content:flex-end; color:white;"):
                        ui.label(catch).classes("text-h6").style("line-height: 1.2;")
                        if sub_catch:
                            label_pre(sub_catch, "text-body2")
                        ui.label(industry).classes("text-caption").style("opacity: 0.9;")
                        with ui.row().classes("q-gutter-sm q-mt-sm"):
                            primary_button(btn_primary)
                            if btn_secondary:
                                outline_button(btn_secondary)

            # philosophy
            with ui.element("div").style(f"background:{surface};").classes("w-full"):
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
                                ui.badge(t).props("outline").style(f"color:{primary_hex}; border-color:{primary_hex};")

            # news
            with ui.element("div").style(f"background:{surface_alt};").classes("w-full"):
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
                                    ui.badge(cat).props("outline").style(f"color:{primary_hex}; border-color:{primary_hex};")
                                if date:
                                    ui.label(date).classes("text-caption text-grey")
                                if body:
                                    snippet = body.replace("\n", " ")
                                    if len(snippet) > 70:
                                        snippet = snippet[:70] + "…"
                                    ui.label(snippet).classes("text-caption")

                        ui.button("お知らせ一覧（仮）").props("flat").style(f"color:{primary_hex};").classes("q-mt-xs")

            # faq
            with ui.element("div").style(f"background:{surface};").classes("w-full"):
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

            # access
            with ui.element("div").style(f"background:{surface_alt};").classes("w-full"):
                with ui.column().classes("q-pa-md"):
                    section_title("place", "アクセス")
                    ui.label(f"住所：{addr if addr else '未入力'}").classes("text-body2")
                    if access_notes:
                        label_pre(access_notes, "text-caption text-grey")
                    if map_url:
                        ui.button(
                            "地図を開く",
                            on_click=lambda u=map_url: ui.run_javascript(f"window.open('{u}','_blank')")
                        ).props("unelevated").style(f"background:{primary_hex}; color:{on_primary};").classes("q-mt-sm")
                    else:
                        ui.label("住所を入力すると地図ボタンが出ます").classes("text-caption text-grey")

            # contact
            with ui.element("div").style(f"background:{surface};").classes("w-full"):
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

                        primary_button(btn_primary).classes("q-mt-sm")

            # footer
            with ui.element("div").style(
                f"background:{mix(primary_hex, '#000000', 0.72)}; color:white;"
            ).classes("w-full"):
                with ui.column().classes("q-pa-md"):
                    ui.label(company).classes("text-subtitle2")
                    if addr:
                        ui.label(addr).classes("text-caption")
                    if phone:
                        ui.label(f"TEL: {phone}").classes("text-caption")
        return

    # ====== PC ======
    # PCっぽく「余白」「2カラム」「カード」で見せる
    with ui.element("div").style(f"background:{page_bg}; min-height:100%;"):
        # App bar
        with ui.element("div").style(
            f"background:{primary_hex}; color:{on_primary}; height:64px; border-bottom: 1px solid rgba(0,0,0,0.08);"
        ).classes("w-full"):
            with ui.row().classes("items-center justify-between q-px-lg").style("height:64px;"):
                ui.label(company).classes("text-h6")
                with ui.row().classes("items-center q-gutter-md"):
                    for label in ["理念", "お知らせ", "FAQ", "アクセス", "お問い合わせ"]:
                        ui.label(label).classes("text-body2").style("opacity:0.95;")

        # Hero
        with ui.element("div").style(
            f"height: 360px; background-image: url('{hero_url}'); background-size: cover; background-position: center;"
        ).classes("w-full"):
            with ui.element("div").style(
                "height:100%; background: linear-gradient(90deg, rgba(0,0,0,0.62), rgba(0,0,0,0.15));"
            ):
                with ui.element("div").style("max-width: 980px; margin: 0 auto; height:100%;"):
                    with ui.column().classes("q-pa-xl").style("height:100%; justify-content:center; color:white;"):
                        ui.label(catch).classes("text-h4").style("line-height:1.15;")
                        if sub_catch:
                            label_pre(sub_catch, "text-body1")
                        ui.label(industry).classes("text-caption").style("opacity:0.9;")
                        with ui.row().classes("q-gutter-sm q-mt-md"):
                            primary_button(btn_primary)
                            if btn_secondary:
                                outline_button(btn_secondary)

        # Main container
        with ui.element("div").style("max-width: 980px; margin: 0 auto; padding: 18px 18px 28px;"):
            # Philosophy (2 columns)
            with ui.card().classes("q-pa-lg rounded-borders q-mb-md").props("flat bordered"):
                section_title("favorite", ph_title)
                with ui.row().classes("q-col-gutter-lg items-start"):
                    with ui.column().classes("col-12 col-md-8"):
                        if ph_body:
                            label_pre(ph_body, "text-body1")
                        else:
                            ui.label("（本文 未入力）").classes("text-caption text-grey")
                    with ui.column().classes("col-12 col-md-4"):
                        ui.label("ポイント").classes("text-subtitle2 q-mb-sm")
                        pts = [str(x).strip() for x in ph_points][:3]
                        pts = [x for x in pts if x]
                        if not pts:
                            ui.label("（未入力）").classes("text-caption text-grey")
                        else:
                            for t in pts:
                                with ui.element("div").style(
                                    f"border:1px solid {mix(primary_hex, '#ffffff', 0.35)}; border-radius:10px; padding:10px 12px; margin-bottom:10px; background:{mix(primary_hex,'#ffffff',0.92)};"
                                ):
                                    ui.label(t).classes("text-body2")

            # News + FAQ (2 columns)
            with ui.row().classes("q-col-gutter-md items-start"):
                # News
                with ui.column().classes("col-12 col-md-6"):
                    with ui.card().classes("q-pa-lg rounded-borders q-mb-md").props("flat bordered"):
                        section_title("campaign", "お知らせ")
                        if not news_items:
                            ui.label("まだお知らせはありません").classes("text-caption text-grey")
                        else:
                            for it in news_items[:3]:
                                date = (it.get("date") or "").strip()
                                cat = (it.get("category") or "").strip() or "お知らせ"
                                title = (it.get("title") or "").strip() or "（タイトル 未入力）"
                                body = (it.get("body") or "").strip()

                                with ui.element("div").style("padding: 10px 0;"):
                                    with ui.row().classes("items-start justify-between q-gutter-sm"):
                                        ui.label(title).classes("text-body1")
                                        ui.badge(cat).props("outline").style(f"color:{primary_hex}; border-color:{primary_hex};")
                                    if date:
                                        ui.label(date).classes("text-caption text-grey")
                                    if body:
                                        snippet = body.replace("\n", " ")
                                        if len(snippet) > 80:
                                            snippet = snippet[:80] + "…"
                                        ui.label(snippet).classes("text-caption")
                                    ui.separator().classes("q-mt-sm")
                        ui.button("お知らせ一覧（仮）").props("flat").style(f"color:{primary_hex};").classes("q-mt-sm")

                # FAQ
                with ui.column().classes("col-12 col-md-6"):
                    with ui.card().classes("q-pa-lg rounded-borders q-mb-md").props("flat bordered"):
                        section_title("help", "よくある質問")
                        if not faq_items:
                            ui.label("まだFAQはありません").classes("text-caption text-grey")
                        else:
                            for it in faq_items[:4]:
                                q = (it.get("q") or "").strip() or "（質問 未入力）"
                                a = (it.get("a") or "").strip() or "（回答 未入力）"
                                with ui.element("div").style("padding: 10px 0;"):
                                    ui.label(q).classes("text-body1")
                                    label_pre(a, "text-body2")
                                    ui.separator().classes("q-mt-sm")

            # Access + Contact (2 columns)
            with ui.row().classes("q-col-gutter-md items-start"):
                with ui.column().classes("col-12 col-md-6"):
                    with ui.card().classes("q-pa-lg rounded-borders q-mb-md").props("flat bordered"):
                        section_title("place", "アクセス")
                        ui.label(f"住所：{addr if addr else '未入力'}").classes("text-body1")
                        if access_notes:
                            label_pre(access_notes, "text-caption text-grey")
                        if map_url:
                            ui.button(
                                "地図を開く",
                                on_click=lambda u=map_url: ui.run_javascript(f"window.open('{u}','_blank')")
                            ).props("unelevated").style(
                                f"background:{primary_hex}; color:{on_primary}; border-radius:10px;"
                            ).classes("q-mt-md")
                        else:
                            ui.label("住所を入力すると地図ボタンが出ます").classes("text-caption text-grey")

                with ui.column().classes("col-12 col-md-6"):
                    with ui.card().classes("q-pa-lg rounded-borders q-mb-md").props("flat bordered"):
                        section_title("call", "お問い合わせ")
                        if message:
                            label_pre(message, "text-body2")
                            ui.separator().classes("q-my-md")

                        ui.label(f"TEL：{phone if phone else '未入力'}").classes("text-body1")
                        if hours:
                            ui.label(f"受付時間：{hours}").classes("text-caption text-grey")
                        ui.label(f"Email：{email if email else '未入力'}").classes("text-body2")
                        primary_button(btn_primary).classes("q-mt-md")

        # footer
        with ui.element("div").style(
            f"background:{mix(primary_hex, '#000000', 0.72)}; color:white; padding: 18px 0;"
        ).classes("w-full"):
            with ui.element("div").style("max-width: 980px; margin: 0 auto; padding: 0 18px;"):
                ui.label(company).classes("text-subtitle1")
                if addr:
                    ui.label(addr).classes("text-caption")
                if phone:
                    ui.label(f"TEL: {phone}").classes("text-caption")


def render_main(u: User) -> None:
    inject_global_styles()
    render_header(u)

    p = get_current_project()
    preview_ref = {"sp": (lambda: None), "pc": (lambda: None)}

    def refresh_preview() -> None:
        for k in ["sp", "pc"]:
            try:
                preview_ref[k]()
            except Exception:
                pass

    with ui.element("div").classes("cvhb-page"):
        with ui.element("div").classes("cvhb-container"):
            with ui.element("div").classes("cvhb-split"):
                # ---------------------------
                # Left: Builder (fixed width)
                # ---------------------------
                with ui.element("div").classes("cvhb-left"):
                    with ui.column().classes("w-full q-gutter-md"):
                        # 1) Current project card
                        with ui.card().classes("q-pa-md rounded-borders").props("bordered"):
                            with ui.row().classes("items-center justify-between"):
                                with ui.row().classes("items-center q-gutter-sm"):
                                    ui.icon("folder").classes("text-grey-8")
                                    ui.label("現在の案件").classes("text-subtitle1")
                                ui.button("案件一覧", on_click=lambda: navigate_to("/projects")).props("flat")

                            if not p:
                                ui.label("案件が未選択です。まず案件を作成/選択してください。").classes("text-body2 q-mt-sm")
                            else:
                                ui.label(f"現在の案件：{p.get('project_name','')}").classes("text-body1 q-mt-sm")
                                ui.label(f"ID：{p.get('project_id','')}").classes("text-caption text-grey")

                                ui.separator().classes("q-my-sm")

                                ui.label(f"案件開始日：{fmt_jst(p.get('created_at',''))}").classes("text-body2")
                                ui.label(f"最新更新日：{fmt_jst(p.get('updated_at',''))}").classes("text-body2")
                                updated_by = (p.get("updated_by") or "").strip() or u.username
                                ui.label(f"更新担当者：{updated_by}").classes("text-body2")

                                def do_save() -> None:
                                    try:
                                        save_project_to_sftp(p, u)
                                        set_current_project(p)
                                        ui.notify("保存しました（SFTP / project.json）", type="positive")
                                    except Exception as e:
                                        ui.notify(f"保存に失敗: {e}", type="negative")

                                ui.button("保存（PROJECT.JSON）", on_click=do_save).props(
                                    "color=primary unelevated"
                                ).classes("q-mt-md w-full")

                        if not p:
                            # 案件未選択なら、左のカードはここまで
                            with ui.card().classes("q-pa-md rounded-borders").props("bordered"):
                                ui.label("次にやること").classes("text-subtitle2")
                                ui.label("「案件一覧」から案件を開くか、新規作成してください。").classes("text-body2")
                            # 右のプレビューは表示しない
                        else:
                            # 2) Step list card
                            steps = [
                                ("s1", "1. 業種設定・ページカラー設定"),
                                ("s2", "2. 基本情報設定"),
                                ("s3", "3. ページ内容詳細設定（ブロックごと）"),
                                ("s4", "4. 承認・最終チェック"),
                                ("s5", "5. 公開（管理者権限のみ）"),
                            ]

                            with ui.card().classes("q-pa-md rounded-borders").props("bordered"):
                                with ui.row().classes("items-center q-gutter-sm q-mb-sm"):
                                    ui.icon("list_alt").classes("text-grey-8")
                                    ui.label("作成ステップ").classes("text-subtitle1")

                                ui.label("ここでステップを選ぶと、下の入力画面が切り替わります。").classes(
                                    "text-caption text-grey q-mb-sm"
                                )

                                with ui.tabs().props("vertical dense").classes("w-full") as tabs:
                                    for key, label in steps:
                                        ui.tab(key, label=label)

                            # 3) Step input card
                            with ui.card().classes("q-pa-md rounded-borders").props("bordered"):
                                with ui.tab_panels(tabs, value="s1").props("vertical").classes("w-full"):
                                    # -----------------
                                    # Step1
                                    # -----------------
                                    with ui.tab_panel("s1"):
                                        ui.label("1. 業種設定・ページカラー設定").classes("text-h6 q-mb-xs")
                                        ui.label("最初にここを決めると、右の完成イメージが一気に整います。").classes(
                                            "text-caption text-grey q-mb-md"
                                        )

                                        # Industry selector
                                        with ui.card().classes("q-pa-md rounded-borders q-mb-md").props("flat bordered"):
                                            ui.label("業種を選んでください").classes("text-subtitle2 q-mb-xs")
                                            ui.label("※ v1.0 は「会社・企業サイト」をまず商用ラインにします。").classes(
                                                "text-caption text-grey q-mb-sm"
                                            )

                                            def set_industry(value: str) -> None:
                                                p["data"]["step1"]["industry"] = value
                                                set_current_project(p)
                                                industry_cards.refresh()
                                                refresh_preview()

                                            @ui.refreshable
                                            def industry_cards() -> None:
                                                selected = p["data"]["step1"].get("industry", "会社サイト（企業）")
                                                for opt in INDUSTRY_PRESETS:
                                                    is_sel = (selected == opt["value"])
                                                    cls = "cvhb-option-card cursor-pointer q-mb-sm"
                                                    if is_sel:
                                                        cls += " cvhb-option-selected"
                                                    with ui.card().classes(cls).props("flat bordered").on(
                                                        "click", lambda e=None, v=opt["value"]: set_industry(v)
                                                    ):
                                                        with ui.row().classes("items-start q-gutter-sm"):
                                                            ui.icon("check_circle" if is_sel else "radio_button_unchecked").style(
                                                                f"color:{'#1976d2' if is_sel else '#9e9e9e'};"
                                                            )
                                                            with ui.column().classes("col"):
                                                                with ui.row().classes("items-center q-gutter-sm"):
                                                                    ui.label(opt["label"]).classes("text-body1")
                                                                    if opt.get("note"):
                                                                        ui.badge(opt["note"]).props("outline").classes("text-grey-7")
                                                                ui.label(opt["feature"]).classes("text-caption text-grey")

                                            industry_cards()

                                        # Color theme selector
                                        with ui.card().classes("q-pa-md rounded-borders").props("flat bordered"):
                                            ui.label("ページカラー設定").classes("text-subtitle2 q-mb-xs")
                                            ui.label(
                                                "ページ内のボタン・見出し・枠の雰囲気を統一できます。"
                                            ).classes("text-caption text-grey")
                                            ui.label(
                                                "※ページカラーのイメージを選択してください。"
                                            ).classes("text-caption text-grey q-mb-sm")

                                            def set_color(key: str) -> None:
                                                p["data"]["step1"]["primary_color"] = key
                                                set_current_project(p)
                                                color_cards.refresh()
                                                refresh_preview()

                                            @ui.refreshable
                                            def color_cards() -> None:
                                                selected = p["data"]["step1"].get("primary_color", COLOR_THEME_DEFAULT)
                                                for opt in COLOR_THEMES:
                                                    is_sel = (selected == opt["key"])
                                                    cls = "cvhb-option-card cursor-pointer q-mb-sm"
                                                    if is_sel:
                                                        cls += " cvhb-option-selected"
                                                    swatch = opt["hex"]
                                                    with ui.card().classes(cls).props("flat bordered").on(
                                                        "click", lambda e=None, k=opt["key"]: set_color(k)
                                                    ):
                                                        with ui.row().classes("items-center q-gutter-sm"):
                                                            ui.icon("check_circle" if is_sel else "radio_button_unchecked").style(
                                                                f"color:{'#1976d2' if is_sel else '#9e9e9e'};"
                                                            )
                                                            ui.label(f"〇 {opt['label']}").classes("text-body1")
                                                            ui.element("div").classes("cvhb-color-swatch").style(
                                                                f"background:{swatch};"
                                                            )
                                                        ui.label(f"※ {opt['label']}：{opt['impression']}").classes(
                                                            "text-caption text-grey q-mt-xs"
                                                        )

                                            color_cards()

                                    # -----------------
                                    # Step2
                                    # -----------------
                                    with ui.tab_panel("s2"):
                                        ui.label("2. 基本情報設定").classes("text-h6 q-mb-xs")
                                        ui.label("入力すると右のプレビューに反映されます。").classes(
                                            "text-caption text-grey q-mb-md"
                                        )

                                        with ui.card().classes("q-pa-md rounded-borders").props("flat bordered"):
                                            ui.label("会社の基本情報設定").classes("text-subtitle2 q-mb-sm")

                                            def bind_input(key: str, label: str):
                                                val = p["data"]["step2"].get(key, "")

                                                def _on_change(e):
                                                    p["data"]["step2"][key] = e.value
                                                    set_current_project(p)
                                                    refresh_preview()

                                                ui.input(label, value=val, on_change=_on_change).props("outlined").classes(
                                                    "w-full q-mb-sm"
                                                )

                                            bind_input("company_name", "会社名")
                                            bind_input("catch_copy", "キャッチコピー")
                                            bind_input("phone", "電話番号")
                                            bind_input("email", "メール（任意）")
                                            bind_input("address", "住所（地図リンクは自動作成されます）")

                                    # -----------------
                                    # Step3 (blocks)
                                    # -----------------
                                    with ui.tab_panel("s3"):
                                        ui.label("3. ページ内容詳細設定（ブロックごと）").classes("text-h6 q-mb-xs")
                                        ui.label("ブロックを切り替えて編集できます。迷わないように整理してあります。").classes(
                                            "text-caption text-grey q-mb-md"
                                        )

                                        with ui.card().classes("q-pa-md rounded-borders").props("flat bordered"):
                                            ui.label("ブロック編集（会社テンプレ：6ブロック）").classes("text-subtitle2")
                                            ui.label("ヒーロー / 理念 / お知らせ / FAQ / アクセス / お問い合わせ").classes(
                                                "text-caption text-grey q-mb-sm"
                                            )

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
                                                    "date": datetime.now(JST).strftime("%Y-%m-%d"),
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
                                                        blocks = p["data"]["blocks"]
                                                        items = (blocks.get("news", {}) or {}).get("items") or []
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
                                                        blocks = p["data"]["blocks"]
                                                        items = (blocks.get("faq", {}) or {}).get("items") or []
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

                                    with ui.tab_panel("s4"):
                                        ui.label("4. 承認・最終チェック").classes("text-h6 q-mb-xs")
                                        ui.label("v0.7.0で承認フロー（OK/差戻し）を実装します。").classes("text-body2")

                                    with ui.tab_panel("s5"):
                                        ui.label("5. 公開（管理者権限のみ）").classes("text-h6 q-mb-xs")
                                        ui.label("v0.7.0で公開（アップロード）を実装します。").classes("text-body2")

                # ---------------------------
                # Right: Preview
                # ---------------------------
                if p:
                    with ui.element("div").classes("cvhb-right"):
                        with ui.element("div").classes("cvhb-preview-sticky"):
                            with ui.card().classes("q-pa-md rounded-borders").props("bordered"):
                                with ui.row().classes("items-center justify-between q-mb-sm"):
                                    with ui.row().classes("items-center q-gutter-sm"):
                                        ui.icon("visibility").classes("text-grey-8")
                                        ui.label("プレビュー").classes("text-subtitle1")
                                    ui.badge("スマホ / PC 切替").props("outline")

                                with ui.tabs().props("dense").classes("w-full") as preview_tabs:
                                    ui.tab("sp", label="スマホ")
                                    ui.tab("pc", label="PC")

                                with ui.tab_panels(preview_tabs, value="sp").classes("w-full q-mt-sm"):
                                    with ui.tab_panel("sp"):
                                        with ui.card().classes("q-pa-none rounded-borders shadow-1").style(
                                            "width: min(100%, 420px);"
                                            "height: clamp(560px, 75vh, 740px);"
                                            "border: 1px solid #ddd;"
                                            "overflow: hidden;"
                                            "background: white;"
                                        ).props("flat"):
                                            with ui.element("div").style("height: 100%; overflow-y: auto;"):
                                                @ui.refreshable
                                                def preview_sp():
                                                    render_preview(p, mode="sp")

                                                preview_sp()
                                                preview_ref["sp"] = preview_sp.refresh

                                    with ui.tab_panel("pc"):
                                        with ui.card().classes("q-pa-none rounded-borders shadow-1").style(
                                            "width: min(100%, 980px);"
                                            "height: clamp(560px, 75vh, 820px);"
                                            "border: 1px solid #ddd;"
                                            "overflow: hidden;"
                                            "background: white;"
                                        ).props("flat"):
                                            with ui.element("div").style("height: 100%; overflow-y: auto;"):
                                                @ui.refreshable
                                                def preview_pc():
                                                    render_preview(p, mode="pc")

                                                preview_pc()
                                                preview_ref["pc"] = preview_pc.refresh


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
                            ui.label(f"更新: {fmt_jst(it.get('updated_at',''))}（{it.get('updated_by','') or '---'}）").classes("text-caption text-grey")

                            def open_this(pid=it.get("project_id", "")):
                                try:
                                    p = load_project_from_sftp(pid, u)
                                    set_current_project(p)
                                    ui.notify("案件を開きました", type="positive")
                                    navigate_to("/")
                                except Exception as e:
                                    ui.notify(f"開けませんでした: {e}", type="negative")

                            ui.button("開く", on_click=open_this).props("color=primary unelevated").classes("q-mt-sm")

                def create_new() -> None:
                    name = (name_input.value or "").strip()
                    if not name:
                        ui.notify("案件名を入力してください", type="warning")
                        return
                    try:
                        p = create_project(name, u)
                        save_project_to_sftp(p, u)
                        set_current_project(p)
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
                            try:
                                r["created_at"] = r["created_at"].astimezone(JST).strftime("%Y-%m-%d %H:%M:%S")
                            except Exception:
                                r["created_at"] = str(r["created_at"])

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
