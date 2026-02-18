# state.py (CV-HomeBuilder) - UI非依存の共通ロジック

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
# [BLK-INDEX] 検索用ブロック一覧（20以下）
#   [BLK-01] Runtime / Imports / Utils
#   [BLK-02] Global CSS
#   [BLK-03] Config
#   [BLK-04] DB helpers
#   [BLK-05] Auth & Sessions
#   [BLK-06] Storage (SFTP)
#   [BLK-07] Presets & Templates（業種/福祉分岐/カラー）
#   [BLK-08] Projects（normalize/load/save）
#   [BLK-09] UI components
#   [BLK-10] Preview renderer（スマホ/PC）
#   [BLK-11] Builder main UI（左：入力 / 右：プレビュー）
#   [BLK-12] Pages / Routing
# =========================

# =========================

from nicegui import app


# [BLK-01] Global (Japan time)
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



def _safe_list(value) -> list:
    """value を list として安全に扱う（None/単体でも落とさない）。"""
    if value is None:
        return []
    if isinstance(value, list):
        return [v for v in value if v is not None]
    # dict / str / number など単体は list に包む
    return [value]

# [BLK-03] Config
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

# [BLK-01] Small utils
# =========================

def google_maps_url(address: str) -> str:
    address = (address or "").strip()
    if not address:
        return ""
    return f"https://www.google.com/maps/search/?api=1&query={quote_plus(address)}"


# =========================

# [BLK-04] DB helpers
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

# [BLK-05] Users / Auth
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

# [BLK-05] Session / Project state (avoid storing big dict in cookie)
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

# [BLK-06] SFTP (SFTP To Go)
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

# [BLK-07] Presets & Templates (v0.6.4)
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

# 福祉事業所：追加の分岐（v0.6.4）
WELFARE_DOMAIN_PRESETS = [
    {"value": "介護福祉サービス", "label": "介護福祉サービス", "hint": "入所系介護 / 通所系介護（デイサービス等）"},
    {"value": "障がい福祉サービス", "label": "障がい福祉サービス", "hint": "施設入所支援 / 日中活動系（通所）"},
    {"value": "児童福祉サービス", "label": "児童福祉サービス", "hint": "障害児通所支援 / 障害児入所支援"},
]
WELFARE_DOMAIN_OPTIONS = [x["value"] for x in WELFARE_DOMAIN_PRESETS]

WELFARE_MODE_PRESETS = [
    {"value": "入所系", "label": "入所系", "hint": "施設サービスなど"},
    {"value": "通所系", "label": "通所系", "hint": "デイサービスなど"},
]
WELFARE_MODE_OPTIONS = [x["value"] for x in WELFARE_MODE_PRESETS]


def resolve_template_id(step1: dict) -> str:
    """Step1設定からテンプレIDを決める（project.jsonに固定保存する用）。

    NOTE:
    - v0.6.4 時点では、編集UI/プレビューは「会社テンプレ」をベースに動きます。
    - ただし、template_id を先に保存しておくと、次の版でテンプレ拡張がスムーズです。
    """
    step1 = step1 or {}
    industry = step1.get("industry", "会社サイト（企業）")

    if industry == "福祉事業所":
        domain = step1.get("welfare_domain") or WELFARE_DOMAIN_PRESETS[0]["value"]
        mode = step1.get("welfare_mode") or WELFARE_MODE_PRESETS[0]["value"]
        # ここは「6ブロックの中身」を後で育てるためのID（まずは判別だけを確定）
        if domain == "介護福祉サービス":
            return "care_residential_v1" if mode == "入所系" else "care_day_v1"
        if domain == "障がい福祉サービス":
            return "disability_residential_v1" if mode == "入所系" else "disability_day_v1"
        if domain == "児童福祉サービス":
            return "child_residential_v1" if mode == "入所系" else "child_day_v1"
        return "welfare_v1"

    if industry == "個人事業":
        return "personal_v1"
    if industry == "その他":
        return "free6_v1"

    # 会社サイト（企業）を既定
    return "corp_v1"


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

# v0.6.7: Safe defaults (avoid preview errors)
HERO_IMAGE_DEFAULT = HERO_IMAGE_PRESET_URLS.get("A: オフィス") or next(iter(HERO_IMAGE_PRESET_URLS.values()), "")
# Alias for backward compatibility
HERO_IMAGE_PRESETS = HERO_IMAGE_PRESET_URLS


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

    # 福祉事業所だけ追加の分岐（入所/通所/児童など）
    if industry == "福祉事業所":
        domain = step1.get("welfare_domain") or WELFARE_DOMAIN_PRESETS[0]["value"]
        if domain not in WELFARE_DOMAIN_OPTIONS:
            domain = WELFARE_DOMAIN_PRESETS[0]["value"]
        step1["welfare_domain"] = domain

        mode = step1.get("welfare_mode") or WELFARE_MODE_PRESETS[0]["value"]
        if mode not in WELFARE_MODE_OPTIONS:
            mode = WELFARE_MODE_PRESETS[0]["value"]
        step1["welfare_mode"] = mode
    else:
        # 福祉以外では空にしておく（UI上の混乱を防ぐ）
        step1["welfare_domain"] = ""
        step1["welfare_mode"] = ""

    # template_id は project.json に固定保存する（後でテンプレ拡張しやすい）
    step1["template_id"] = resolve_template_id(step1)

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

    # ---- Template-specific starter defaults (welfare day-service, v0.6.8) ----
    template_id = resolve_template_id(step1)

    # Ensure keys exist for preview stability
    contact.setdefault("button_text", "お問い合わせ")

    if template_id in ("care_day_v1", "disability_day_v1", "child_day_v1"):
        # Welfare day-service: default theme closer to "warm & calm"
        if step1.get("primary_color", "blue") == "blue":
            step1["primary_color"] = "green"

        # Catch copy: override only when still at the corporate sample
        if step2.get("catch_copy", "").strip() in ("", "スタッフ・利用者の笑顔を守る企業"):
            step2["catch_copy"] = "“できる”が増える毎日へ。"

        # Hero: sub / CTA
        if hero.get("sub_catch", "").strip() in ("", "地域に寄り添い、安心できるサービスを届けます"):
            hero["sub_catch"] = "見学・体験・ご相談は随時受付中。まずは気軽にお問い合わせください。"
        if hero.get("primary_button_text", "").strip() in ("", "お問い合わせ"):
            hero["primary_button_text"] = "見学・体験を予約"
        if hero.get("secondary_button_text", "").strip() in ("", "見学・相談"):
            hero["secondary_button_text"] = "まずは相談"

        # Hero image: prefer people / activity feel (can be changed later)
        if hero.get("hero_image", "").strip() in ("", "A: オフィス"):
            hero["hero_image"] = "B: チーム"

        # Philosophy: title/body/points
        if philosophy.get("title", "").strip() in ("", "私たちの想い"):
            philosophy["title"] = "私たちの支援"
        if philosophy.get("body", "").strip().startswith("ここに"):
            philosophy["body"] = (
                "一人ひとりのペースに合わせて、安心して過ごせる居場所と、日中活動の機会を提供します。"
                "体験利用から丁寧にご案内し、目標に合わせたサポートを行います。"
            )
        if philosophy.get("points", []) == ["地域密着", "丁寧な対応", "安心の体制"]:
            philosophy["points"] = ["見学・体験OK", "個別支援", "少人数", "送迎相談可"]

        # FAQ: day-service oriented (override only when sample)
        if any(str(it.get("q", "")).startswith("サンプル") for it in faq_items):
            faq["items"] = [
                {"q": "見学・体験はできますか？", "a": "はい。ご希望日時を伺い、日程調整のうえご案内します。"},
                {"q": "利用料金はどのくらいですか？", "a": "サービス区分・所得により異なります。まずは状況をお聞かせください。"},
                {"q": "送迎はありますか？", "a": "エリアにより対応可能です。詳細はお問い合わせください。"},
                {"q": "1日の流れを教えてください。", "a": "来所→活動→休憩→活動→帰宅の流れです。見学時に詳しくご説明します。"},
            ]

        # Contact: CTA/Message
        if contact.get("message", "").strip() in ("", "まずはお気軽にご相談ください。"):
            contact["message"] = "見学・体験・ご相談はお気軽に。ご希望日時を添えてご連絡ください。"
        if contact.get("button_text", "").strip() in ("", "お問い合わせ"):
            contact["button_text"] = "見学・体験を申し込む"

        # Optional: keep a small guideline for photo direction (future UI usage)
        p.setdefault("guides", {})
        p["guides"].setdefault(
            "photo_direction_day_service",
            "活動の様子（手元だけでなく空気感）＋スタッフの寄り添いが伝わる写真。逆光で文字が読めなくならない構図。"
        )

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
            "step1": {"industry": "会社サイト（企業）", "primary_color": "blue", "welfare_domain": "", "welfare_mode": "", "template_id": "corp_v1"},
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
