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
# Projects (v0.6.0)
# =========================

INDUSTRY_OPTIONS = [
    "会社サイト（企業）",
    "福祉事業所",
    "個人事業",
    "その他",
]

COLOR_OPTIONS = [
    "blue",
    "indigo",
    "teal",
    "green",
    "deep-orange",
    "purple",
]


# =========================
# Blocks / Template presets (v0.6.0)
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
    # ★ ここで必ず0.6.0形式に“寄せる”
    p["schema_version"] = "0.6.0"
    p.setdefault("project_id", new_project_id())
    p.setdefault("project_name", "(no name)")
    p.setdefault("created_at", now_iso())
    p.setdefault("updated_at", now_iso())

    data = p.setdefault("data", {})
    step1 = data.setdefault("step1", {})
    step2 = data.setdefault("step2", {})
    blocks = data.setdefault("blocks", {})

    step1.setdefault("industry", "会社サイト（企業）")
    step1.setdefault("primary_color", "blue")

    step2.setdefault("company_name", "")
    step2.setdefault("catch_copy", "")
    step2.setdefault("phone", "")
    step2.setdefault("address", "")
    step2.setdefault("email", "")

    # -------------------------
    # blocks（会社テンプレ v1.0：6ブロック）
    # -------------------------
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
        "schema_version": "0.6.0",
        "project_id": pid,
        "project_name": name,
        "created_at": now_iso(),
        "updated_at": now_iso(),
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
    p["updated_at"] = now_iso()
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
                })
            except Exception:
                projects.append({"project_id": d, "project_name": "(broken project.json)", "updated_at": "", "created_at": ""})

    projects.sort(key=lambda x: x.get("updated_at", ""), reverse=True)
    return projects


# =========================
# UI parts
# =========================

def render_header(u: Optional[User]) -> None:
    with ui.element("div").classes("w-full bg-white shadow-1"):
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
    with ui.element("div").classes("w-full bg-grey-1").style("min-height: calc(100vh - 0px);"):
        with ui.column().classes("w-full items-center q-pa-xl"):
            with ui.card().classes("q-pa-lg rounded-borders").style("width: 520px; max-width: 92vw;").props("bordered"):
                ui.label("ログイン").classes("text-h5 q-mb-md")

                if APP_ENV == "stg":
                    seeded, msg = ensure_stg_test_users()
                    with ui.card().classes("q-pa-md q-mb-md bg-grey-1 rounded-borders").props("flat bordered"):
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
    with ui.element("div").classes("w-full bg-grey-1").style("min-height: calc(100vh - 0px);"):
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


def render_preview(p: dict) -> None:
    """右側のスマホプレビュー（会社テンプレ 6ブロック）"""
    p = normalize_project(p)
    step1 = p["data"]["step1"]
    step2 = p["data"]["step2"]
    blocks = p["data"]["blocks"]

    industry = step1.get("industry", "会社サイト（企業）")
    primary = step1.get("primary_color", "blue")

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
            ui.icon(icon_name).classes(f"text-{primary}")
            ui.label(title).classes("text-subtitle1")

    with ui.column().classes("w-full"):
        # Top bar
        with ui.element("div").classes(f"w-full bg-{primary} text-white"):
            with ui.row().classes("items-center justify-between q-px-md").style("height: 48px;"):
                ui.label(company).classes("text-subtitle2")
                ui.icon("menu").classes("text-white")

        # Hero
        with ui.element("div").style(
            f"height: 230px; background-image: url('{hero_url}'); background-size: cover; background-position: center;"
        ).classes("w-full"):
            with ui.element("div").style("height:100%; background: rgba(0,0,0,0.45);"):
                with ui.column().classes("q-pa-md text-white").style("height:100%; justify-content:flex-end;"):
                    ui.label(catch).classes("text-h6").style("line-height: 1.2;")
                    if sub_catch:
                        label_pre(sub_catch, "text-body2")
                    ui.label(industry).classes("text-caption").style("opacity: 0.9;")
                    with ui.row().classes("q-gutter-sm q-mt-sm"):
                        ui.button(btn_primary).props(f"color={primary} unelevated")
                        if btn_secondary:
                            ui.button(btn_secondary).props(f"outline color={primary}")

        # Philosophy / About
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
                            ui.badge(t).props("outline").classes(f"text-{primary}")

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
                                ui.badge(cat).props("outline").classes(f"text-{primary}")
                            if date:
                                ui.label(date).classes("text-caption text-grey")
                            if body:
                                snippet = body.replace("\n", " ")
                                if len(snippet) > 70:
                                    snippet = snippet[:70] + "…"
                                ui.label(snippet).classes("text-caption")

                    ui.button("お知らせ一覧（仮）").props(f"flat color={primary}").classes("q-mt-xs")

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
                    ).props(f"color={primary} unelevated").classes("q-mt-sm")
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

                    ui.button(btn_primary).props(f"color={primary} unelevated").classes("q-mt-sm")

        # Footer
        with ui.element("div").classes("w-full bg-grey-9 text-white"):
            with ui.column().classes("q-pa-md"):
                ui.label(company).classes("text-subtitle2")
                if addr:
                    ui.label(addr).classes("text-caption")
                if phone:
                    ui.label(f"TEL: {phone}").classes("text-caption")


def render_main(u: User) -> None:
    render_header(u)

    p = get_current_project()
    preview_ref = {"refresh": (lambda: None)}

    def refresh_preview() -> None:
        try:
            preview_ref["refresh"]()
        except Exception:
            pass

    with ui.element("div").classes("w-full bg-grey-1").style("min-height: calc(100vh - 0px);"):
        with ui.row().classes("w-full q-pa-md q-col-gutter-md").style("max-width: 1400px; margin: 0 auto;"):
            # ---------------------------------
            # Left: builder
            # ---------------------------------
            with ui.card().classes("col-12 col-md-5 col-xl-4 q-pa-md rounded-borders").props("bordered"):
                with ui.row().classes("items-center q-gutter-sm q-mb-sm"):
                    ui.icon("tune").classes("text-grey-8")
                    ui.label(f"制作ステップ（Step1/2/3入力 + プレビュー反映）").classes("text-subtitle1")

                if not p:
                    ui.label("案件が未選択です。まず案件を作成/選択してください。").classes("text-body2 q-mb-md")
                    ui.button("案件一覧へ", on_click=lambda: navigate_to("/projects")).props("color=primary")
                    return

                with ui.card().classes("q-pa-md q-mb-md bg-white rounded-borders").props("flat bordered"):
                    ui.label(f"現在の案件：{p.get('project_name')}").classes("text-body1")
                    ui.label(f"ID：{p.get('project_id')}").classes("text-caption text-grey")
                    ui.label(f"更新：{p.get('updated_at','')}").classes("text-caption text-grey")

                    def do_save():
                        try:
                            save_project_to_sftp(p, u)
                            set_current_project(p)
                            ui.notify("保存しました（SFTP / project.json）", type="positive")
                        except Exception as e:
                            ui.notify(f"保存に失敗: {e}", type="negative")

                    ui.button("保存（PROJECT.JSON）", on_click=do_save).props("color=primary unelevated").classes("q-mt-sm w-full")

                steps = [
                    ("s1", "1. 業種・色"),
                    ("s2", "2. 基本情報"),
                    ("s3", "3. ページ内容（ブロック）"),
                    ("s4", "4. 承認・最終チェック"),
                    ("s5", "5. 公開"),
                ]

                with ui.tabs().props("vertical").classes("w-full") as tabs:
                    for key, label in steps:
                        ui.tab(key, label=label)

                with ui.tab_panels(tabs, value="s1").props("vertical").classes("w-full q-mt-md"):
                    # -----------------------
                    # Step1
                    # -----------------------
                    with ui.tab_panel("s1"):
                        ui.label("業種（テンプレ）").classes("text-subtitle2")
                        current_industry = p["data"]["step1"].get("industry", "会社サイト（企業）")

                        def on_industry_change(e):
                            p["data"]["step1"]["industry"] = e.value
                            set_current_project(p)
                            refresh_preview()

                        ui.radio(INDUSTRY_OPTIONS, value=current_industry, on_change=on_industry_change).props("dense")

                        ui.separator().classes("q-my-md")

                        ui.label("カラー（テーマ色）").classes("text-subtitle2")
                        ui.label("※右のプレビューのボタンや見出し色が変わります").classes("text-caption text-grey")

                        current_color = p["data"]["step1"].get("primary_color", "blue")

                        def on_color_change(e):
                            p["data"]["step1"]["primary_color"] = e.value
                            set_current_project(p)
                            refresh_preview()

                        ui.radio(COLOR_OPTIONS, value=current_color, on_change=on_color_change).props("dense")

                    # -----------------------
                    # Step2
                    # -----------------------
                    with ui.tab_panel("s2"):
                        ui.label("会社の基本情報").classes("text-subtitle2")
                        ui.label("入力すると右のプレビューに反映されます").classes("text-caption text-grey q-mb-sm")

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
                        bind_input("address", "住所（地図リンクは自動生成）")

                    # -----------------------
                    # Step3 (Material-ish: block tabs)
                    # -----------------------
                    with ui.tab_panel("s3"):
                        ui.label("ページ内容（会社テンプレ：6ブロック）").classes("text-subtitle2")
                        ui.label("ブロックごとに切り替えて編集できます").classes("text-caption text-grey q-mb-sm")

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

                        with ui.tabs().props("dense").classes("w-full") as block_tabs:
                            ui.tab("hero", label="ヒーロー")
                            ui.tab("philosophy", label="理念/概要")
                            ui.tab("news", label="お知らせ")
                            ui.tab("faq", label="FAQ")
                            ui.tab("access", label="アクセス")
                            ui.tab("contact", label="お問い合わせ")

                        with ui.tab_panels(block_tabs, value="hero").classes("w-full q-mt-md"):
                            # HERO
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

                            # PHILOSOPHY
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

                            # NEWS
                            with ui.tab_panel("news"):
                                ui.label("お知らせ").classes("text-subtitle2 q-mb-sm")

                                @ui.refreshable
                                def news_editor() -> None:
                                    blocks = p["data"]["blocks"]
                                    news_items = (blocks.get("news", {}) or {}).get("items") or []
                                    if not isinstance(news_items, list):
                                        news_items = []

                                    if not news_items:
                                        ui.label("お知らせがまだありません。下のボタンで追加できます。").classes("text-caption text-grey")

                                    for idx, it in enumerate(news_items):
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

                            # FAQ
                            with ui.tab_panel("faq"):
                                ui.label("FAQ（よくある質問）").classes("text-subtitle2 q-mb-sm")

                                @ui.refreshable
                                def faq_editor() -> None:
                                    blocks = p["data"]["blocks"]
                                    faq_items = (blocks.get("faq", {}) or {}).get("items") or []
                                    if not isinstance(faq_items, list):
                                        faq_items = []

                                    if not faq_items:
                                        ui.label("FAQがまだありません。下のボタンで追加できます。").classes("text-caption text-grey")

                                    for idx, it in enumerate(faq_items):
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

                            # ACCESS
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

                            # CONTACT
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
                        ui.label("v0.7.0で承認フローを実装").classes("text-body2")

                    with ui.tab_panel("s5"):
                        ui.label("v0.7.0で公開（アップロード）を実装").classes("text-body2")

            # ---------------------------------
            # Right: preview
            # ---------------------------------
            with ui.card().classes("col-12 col-md-7 col-xl-8 q-pa-md rounded-borders").props("bordered"):
                with ui.row().classes("items-center justify-between q-mb-sm"):
                    with ui.row().classes("items-center q-gutter-sm"):
                        ui.icon("smartphone").classes("text-grey-8")
                        ui.label("プレビュー（スマホ表示）").classes("text-subtitle1")
                    ui.badge("画面幅に合わせて自動レイアウト").props("outline")

                with ui.column().classes("w-full items-center"):
                    # ★ ここが「画面が広いと変」問題の中心：中央寄せ＆自動サイズ
                    with ui.card().classes("q-pa-none rounded-borders shadow-1").style(
                        "width: clamp(320px, 38vw, 420px);"
                        "height: clamp(560px, 75vh, 740px);"
                        "border: 1px solid #ddd;"
                        "overflow: hidden;"
                        "background: white;"
                    ).props("flat"):
                        with ui.element("div").style("height: 100%; overflow-y: auto;"):
                            @ui.refreshable
                            def preview_panel():
                                render_preview(p)

                            preview_panel()
                            preview_ref["refresh"] = preview_panel.refresh


# =========================
# Pages
# =========================

init_db_schema()


@ui.page("/")
def index() -> None:
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
    ui.page_title("Projects - CV-HomeBuilder")

    u = current_user()
    if not u:
        ui.notify("ログインが必要です", type="warning")
        navigate_to("/")
        return

    render_header(u)
    with ui.element("div").classes("w-full bg-grey-1").style("min-height: calc(100vh - 0px);"):
        with ui.column().classes("w-full q-pa-md").style("max-width: 1100px; margin: 0 auto;"):
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
                            ui.label(f"更新: {it.get('updated_at','')}").classes("text-caption text-grey")

                            def open_this(pid=it.get("project_id", "")):
                                try:
                                    p = load_project_from_sftp(pid, u)
                                    set_current_project(p)
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
    ui.page_title("Audit Logs - CV-HomeBuilder")

    u = current_user()
    if not u:
        ui.notify("ログインが必要です", type="warning")
        navigate_to("/")
        return

    if u.role not in {"admin", "subadmin"}:
        render_header(u)
        with ui.element("div").classes("w-full bg-grey-1").style("min-height: calc(100vh - 0px);"):
            with ui.column().classes("w-full q-pa-md").style("max-width: 1100px; margin: 0 auto;"):
                ui.label("権限がありません（管理者/副管理者のみ）").classes("text-negative q-mb-md")
                ui.button("戻る", on_click=lambda: navigate_to("/")).props("color=primary unelevated")
        return

    render_header(u)
    with ui.element("div").classes("w-full bg-grey-1").style("min-height: calc(100vh - 0px);"):
        with ui.column().classes("w-full q-pa-md").style("max-width: 1100px; margin: 0 auto;"):
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
