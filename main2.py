"""
CV-HomeBuilder (Help Mode) - main2.py

目的:
- ヘルプ（操作手順書）を作るとき、毎回ログイン入力をしなくても
  すぐにビルダー画面へ入れるようにする「入口ファイル」です。

動き:
- main.py をそのまま読み込み（UI/機能は main.py と同じ）
- ログイン状態が無いときだけ、管理者ユーザーとして自動ログインします

安全策（重要）:
- Heroku 上(DYNOがある)ではデフォルトで自動ログインをOFFにします
  （うっかり公開してしまう事故を避けるため）
- どうしても Heroku の stg で使う場合だけ、Config Vars で
  CVHB_HELP_MODE=1 を入れてください（作業が終わったら必ずOFFに戻す）

注意:
- このファイルは「ヘルプ作成用」です。本番(prod)で使わないでください。
"""

from __future__ import annotations

import os
import secrets
import json
from typing import Optional

import main as base


# =========================
# Help mode config
# =========================

HELP_USERNAME = (os.getenv("CVHB_HELP_USERNAME") or "admin_help").strip()

# Heroku では DYNO が入ります。ローカルは空。
IS_HEROKU = bool(os.getenv("DYNO"))

# デフォルト: ローカルは ON / Heroku は OFF
HELP_MODE = not IS_HEROKU

# 手動でON/OFFしたい場合
if (os.getenv("CVHB_HELP_MODE") or "").strip() == "1":
    HELP_MODE = True
if (os.getenv("CVHB_HELP_MODE") or "").strip() == "0":
    HELP_MODE = False

# さらに安全策: Herokuの prod では絶対にONにしない
try:
    if IS_HEROKU and getattr(base, "APP_ENV", "prod") == "prod":
        HELP_MODE = False
except Exception:
    pass


# =========================
# Auto login patch
# =========================

_original_current_user = base.current_user


def _find_any_admin_row() -> Optional[dict]:
    """DB上に存在する最初の admin を探す（いればそれを使う）。"""
    try:
        row = base.db_fetchone(
            "SELECT * FROM users WHERE role = %s AND is_active = TRUE ORDER BY id ASC LIMIT 1",
            ("admin",),
        )
        return row
    except Exception:
        return None


def _ensure_help_admin_row() -> Optional[dict]:
    """
    使える admin ユーザー行を返す。
    - 既に admin がいるならそれを使う（DBを増やさない）
    - いないなら help 用 admin を作成する
    """
    # 1) 既存の admin がいればそれを使う
    row = _find_any_admin_row()
    if row:
        return row

    # 2) いなければ help admin を作る（パスワードはランダムでOK）
    try:
        pwd = secrets.token_urlsafe(24)
        base.create_user(HELP_USERNAME, pwd, "admin")
        row = base.get_user_by_username(HELP_USERNAME)
        if row and (row.get("role") == "admin"):
            return row
    except Exception:
        pass

    # 3) 最後の保険：名前衝突などがあっても作れるように suffix 付きで作る
    try:
        uname = f"{HELP_USERNAME}_{secrets.token_hex(3)}"
        pwd = secrets.token_urlsafe(24)
        base.create_user(uname, pwd, "admin")
        row = base.get_user_by_username(uname)
        if row and (row.get("role") == "admin"):
            return row
    except Exception:
        pass

    return None


def _help_current_user() -> Optional[base.User]:
    """main.py の current_user を置き換える版（ログインが無ければ自動ログイン）。"""
    u = _original_current_user()
    if u or not HELP_MODE:
        return u

    try:
        row = _ensure_help_admin_row()
        if not row:
            return None

        base.set_logged_in(row)
        base.cleanup_user_storage()
        u2 = _original_current_user()

        # 操作ログ（失敗しても落とさない）
        try:
            if u2:
                base.safe_log_action(
                    u2,
                    "help_auto_login",
                    details=json.dumps({"entry": "main2", "mode": "autologin"}, ensure_ascii=False),
                )
        except Exception:
            pass

        return u2
    except Exception as e:
        try:
            print("[help] auto login failed:", base.sanitize_error_text(e))
        except Exception:
            print("[help] auto login failed")
        return None


# パッチ適用（main.py の画面は current_user() を呼ぶので、ここを差し替えるだけで効く）
base.current_user = _help_current_user


# =========================
# Boot (same as main.py)
# =========================

if __name__ in {"__main__", "__mp_main__"}:
    base.ui.run(
        title=f"CV-HomeBuilder HELP v{base.VERSION}",
        storage_secret=base.STORAGE_SECRET,
        reload=False,
        port=int(os.getenv("PORT", "8080")),
        show=False,
    )