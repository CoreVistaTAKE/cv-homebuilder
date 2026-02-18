# main.py (CV-HomeBuilder) - 起動入口のみ（ui.runはここだけ）
# 0.6.81 : builder / preview / state に分割（以後の修正を安全・短縮するため）

import os

# [BLK-MN-01] Heroku runtime guard (Gunicorn / concurrency)
# Herokuの基本設定だと同時接続数で落ちやすいので、明示的に1に寄せる
if os.getenv("DYNO") and not os.getenv("WEB_CONCURRENCY"):
    os.environ["WEB_CONCURRENCY"] = "1"

from nicegui import ui  # noqa: E402

import builder  # noqa: E402  (import時に @ui.page が登録される)
from state import APP_TITLE, init_db_schema  # noqa: E402


# =========================
# [BLK-MN-02] Boot
# =========================
if __name__ == "__main__":
    init_db_schema()
    ui.run(
        title=APP_TITLE,
        reload=False,
        port=int(os.getenv("PORT", "8080")),
        show=False,
        dark=False,
    )
