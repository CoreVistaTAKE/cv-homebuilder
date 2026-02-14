import base64
import hashlib
import os
import secrets
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

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
    # 外部ライブラリなしで安全に（PBKDF2）
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


def logout() -> None:
    u = current_user()
    if u:
        log_action(u, "logout")
    app.storage.user.clear()
    ui.open("/")


def ensure_stg_test_users() -> tuple[bool, str]:
    """returns: (seeded, message)"""
    if APP_ENV != "stg":
        return (False, "not stg")
    pwd = os.getenv("STG_TEST_PASSWORD")
    if not pwd:
        return (False, "STG_TEST_PASSWORD が未設定です（stgのみ必要）")
    # create users (idempotent)
    create_user("admin_test", pwd, "admin")
    create_user("subadmin_test", pwd, "subadmin")
    for i in range(1, 6):
        create_user(f"user{i:02d}", pwd, "user")
    return (True, "stg test users seeded")


# =========================
# UI
# =========================

def render_header(u: Optional[User]) -> None:
    with ui.row().classes("w-full items-center justify-between q-pa-md"):
        ui.label(f"CV-HomeBuilder  v{VERSION}").classes("text-h6")
        with ui.row().classes("items-center q-gutter-sm"):
            ui.badge(APP_ENV.upper()).props("outline")
            ui.badge(f"SFTP_BASE_DIR: {SFTP_BASE_DIR}").props("outline")
            if u:
                ui.badge(f"{u.username} ({u.role})").props("outline")
                ui.button("ログアウト", on_click=logout).props("color=negative flat")


def render_login(root_refresh) -> None:
    ui.label("ログイン").classes("text-h5 q-mb-md")

    # stg用メッセージ
    if APP_ENV == "stg":
        seeded, msg = ensure_stg_test_users()
        if not seeded:
            ui.card().classes("q-pa-md q-mb-md").style("max-width: 520px;").props("flat bordered")
            ui.label("stg（検証環境）です").classes("text-subtitle1")
            ui.label("テストアカウントを自動作成するには、Heroku Config Vars に STG_TEST_PASSWORD を追加してください。")
            ui.label(f"理由: {msg}").classes("text-caption text-grey")

        else:
            ui.card().classes("q-pa-md q-mb-md").style("max-width: 520px;").props("flat bordered")
            ui.label("stg（検証環境）テストアカウント").classes("text-subtitle1")
            ui.label("ユーザー名：admin_test / subadmin_test / user01〜user05")
            ui.label("パスワード：STG_TEST_PASSWORD（Herokuに入れた値）").classes("text-caption text-grey")

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
            # 失敗ログ（ユーザー名だけ記録）
            log_action(None, "login_failed", details=f'{{"username":"{un}"}}')
            ui.notify("ユーザー名またはパスワードが違います", type="negative")
            return

        set_logged_in(row)
        u = current_user()
        if u:
            log_action(u, "login_success")
        ui.notify("ログインしました", type="positive")
        root_refresh()

    ui.button("ログイン", on_click=do_login).props("color=primary").classes("q-mt-md")


def render_first_admin_setup(root_refresh) -> None:
    ui.label("初期設定：管理者アカウント作成（本番用）").classes("text-h5 q-mb-md")
    ui.label("※この画面は、ユーザーが1人もいない時だけ表示されます。").classes("text-caption text-grey")

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
                log_action(u, "first_admin_created")
            ui.notify("管理者を作成しました。ログインしました。", type="positive")
            root_refresh()
        else:
            ui.notify("作成に失敗しました（同名ユーザーがいる可能性）", type="negative")

    ui.button("管理者を作成", on_click=create_admin).props("color=primary").classes("q-mt-md")


def render_main(u: User) -> None:
    render_header(u)

    # メイン 2カラム（左：入力、右：プレビュー）
    with ui.row().classes("w-full q-pa-md q-gutter-md").style("height: calc(100vh - 90px);"):
        # 左
        with ui.card().classes("q-pa-md").style("width: 520px; max-width: 520px;").props("flat bordered"):
            ui.label("制作ステップ（v0.2.0は枠だけ）").classes("text-subtitle1 q-mb-sm")

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
                with ui.tab_panel("s1"):
                    ui.label("ここに業種選択・色選択を置きます（v0.4.0で実装予定）").classes("text-body2")
                with ui.tab_panel("s2"):
                    ui.label("ここに会社名・電話・住所などの入力を置きます（v0.4.0で実装予定）").classes("text-body2")
                with ui.tab_panel("s3"):
                    ui.label("ここにブロック入力（ヒーロー/理念/FAQ…）を置きます（v0.5.0で実装予定）").classes("text-body2")
                with ui.tab_panel("s4"):
                    ui.label("ここに管理者承認・最終チェックを置きます（v0.7.0で実装予定）").classes("text-body2")
                with ui.tab_panel("s5"):
                    ui.label("ここに公開（アップロード）を置きます（v0.7.0で実装予定）").classes("text-body2")

        # 右（プレビュー）
        with ui.card().classes("q-pa-md").style("flex: 1; min-width: 360px;").props("flat bordered"):
            ui.label("プレビュー（スマホ表示：仮）").classes("text-subtitle1 q-mb-sm")

            # スマホ枠っぽいカード
            with ui.card().classes("q-pa-md").style(
                "width: 360px; height: 640px; border-radius: 24px; border: 1px solid #ddd;"
            ).props("flat"):
                ui.label("ここに完成イメージが表示されます").classes("text-body2")
                ui.label("v0.2.0では枠だけ（ダミー）").classes("text-caption text-grey")

            ui.separator().classes("q-my-md")
            ui.label("今後：スマホ/PC切替、リアルタイム反映").classes("text-caption text-grey")


# =========================
# Page
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
            # 本番でユーザーがまだ0人なら、最初の管理者作成画面
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


if __name__ in {"__main__", "__mp_main__"}:
    ui.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8080")),
        storage_secret=STORAGE_SECRET,
        title="CV-HomeBuilder",
    )