# builder.py (CV-HomeBuilder) - ビルダーUI / ページ定義

import os
from nicegui import ui, app


from state import *  # noqa: F403,F401 (このプロジェクトでは“定義漏れ”を防ぐために許容)

from preview import render_preview, inject_preview_styles


# [BLK-BD-01] 画面遷移・ログアウト（state.pyから移管）

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


def logout() -> None:
    u = current_user()
    if u:
        safe_log_action(u, "logout")
        clear_current_project(u)
    app.storage.user.clear()
    navigate_to("/")


# [BLK-02] Global UI styles (v0.6.4)
# =========================

def inject_global_styles() -> None:
    # preview側のCSSもここで一括注入（重複注入はpreview側でガード）
    try:
        inject_preview_styles()
    except Exception:
        pass
    """全ページ共通の見た目（左右分割/カード/選択UI）を安定させるCSS。
    - flex-wrap だと「ちょっと足りない」時に右が下へ落ちて空白ができやすい
    - grid + minmax で「入るなら左右、無理なら縦」に安定させる
    """
    ui.add_head_html(
        """
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Cdefs%3E%3ClinearGradient id='g' x1='0' y1='0' x2='1' y2='1'%3E%3Cstop offset='0' stop-color='%2360a5fa'/%3E%3Cstop offset='1' stop-color='%23a78bfa'/%3E%3C/linearGradient%3E%3C/defs%3E%3Crect x='8' y='8' width='48' height='48' rx='14' fill='url(%23g)'/%3E%3Cpath d='M20 36c8 8 16 8 24 0' stroke='rgba(255,255,255,.85)' stroke-width='6' fill='none' stroke-linecap='round'/%3E%3C/svg%3E">
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
        .cvhb-preview-glass {
          height: 100%;
          width: 100%;
          display: flex;
          flex-direction: column;
          color: var(--pv-text);
          background-image: var(--pv-bg-img);
          background-size: cover;
          background-position: center;
          background-repeat: no-repeat;
          border: 1px solid var(--pv-border);
          border-radius: 18px;
          overflow: hidden;
        }

        .pv-topbar {
          display: flex;
          align-items: center;
          justify-content: space-between;
          gap: 12px;
          padding: 10px 12px;
          background: var(--pv-card);
          backdrop-filter: blur(12px);
          border-bottom: 1px solid var(--pv-line);
        }

        .pv-brand {
          font-weight: 900;
          letter-spacing: .02em;
          font-size: 15px;
          white-space: nowrap;
          overflow: hidden;
          text-overflow: ellipsis;
          max-width: 48%;
        }

        .pv-nav {
          display: flex;
          align-items: center;
          gap: 6px;
          overflow-x: auto;
          white-space: nowrap;
          scrollbar-width: none;
          max-width: 100%;
        }
        .pv-nav::-webkit-scrollbar { height: 0; }

        .pv-navbtn {
          font-weight: 900;
          color: var(--pv-accent);
        }
        .pv-navbtn.q-btn--flat { padding: 4px 8px; border-radius: 999px; }
        .pv-navbtn.q-btn--flat:hover { background: rgba(255,255,255,.22); }

        .pv-topcta {
          font-weight: 900;
          border-radius: 999px;
          background: linear-gradient(135deg, var(--pv-accent), var(--pv-accent-2));
          color: white;
          padding: 6px 12px;
          box-shadow: 0 12px 28px rgba(0,0,0,.18);
        }

        .pv-scroll {
          flex: 1;
          overflow: auto;
          padding: 14px 0 16px;
        }

        .pv-container {
          width: min(1040px, calc(100% - 24px));
          margin: 0 auto;
        }

        .pv-band { padding: 18px 0; }
        .pv-band + .pv-band { border-top: 1px solid var(--pv-line); }

        .pv-grid-2 {
          display: grid;
          grid-template-columns: 1fr 1fr;
          gap: 14px;
          align-items: start;
        }
        @media (max-width: 920px) { .pv-grid-2 { grid-template-columns: 1fr; } }

        .pv-panel {
          background: var(--pv-card);
          border: 1px solid var(--pv-border);
          border-radius: 22px;
          box-shadow: var(--pv-shadow);
          backdrop-filter: blur(14px);
          padding: 16px;
          animation: pvIn .45s ease both;
        }
        @keyframes pvIn { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: translateY(0); } }
        @media (prefers-reduced-motion: reduce) { .pv-panel { animation: none; } }

        .pv-kicker {
          display: flex;
          align-items: center;
          gap: 8px;
          font-weight: 900;
          letter-spacing: .02em;
          color: var(--pv-muted);
          margin-bottom: 10px;
        }
        .pv-kicker .q-icon { color: var(--pv-accent); }

        .pv-title {
          font-weight: 900;
          letter-spacing: .01em;
          font-size: 26px;
          line-height: 1.2;
          margin: 0 0 6px;
        }
        .pv-h2 {
          font-weight: 900;
          letter-spacing: .01em;
          font-size: 18px;
          margin: 0 0 6px;
        }
        .pv-sub {
          color: var(--pv-muted);
          font-size: 14px;
          line-height: 1.55;
          margin: 0 0 12px;
        }

        .pv-chip {
          display: inline-flex;
          align-items: center;
          gap: 6px;
          border-radius: 999px;
          padding: 5px 10px;
          background: var(--pv-chip-bg);
          border: 1px solid var(--pv-chip-border);
          font-size: 12px;
          font-weight: 900;
          color: var(--pv-muted);
          margin: 4px 6px 0 0;
        }
        .pv-chip .q-icon { color: var(--pv-accent); }

        /* Hero split layout */
        .pv-hero-grid {
          display: grid;
          grid-template-columns: 1.05fr .95fr;
          gap: 14px;
          align-items: stretch;
        }
        @media (max-width: 920px) { .pv-hero-grid { grid-template-columns: 1fr; } }

        .pv-hero-media { position: relative; }
        .pv-hero-image {
          position: relative;
          height: 360px;
          border-radius: 26px;
          overflow: hidden;
          border: 1px solid rgba(255,255,255,.45);
          box-shadow: 0 22px 60px rgba(15,23,42,.18);
          background-size: cover;
          background-position: center;
        }
        .pv-hero-image::after {
          content: "";
          position: absolute;
          inset: 0;
          background: linear-gradient(135deg, rgba(0,0,0,.55), rgba(0,0,0,.10));
        }
        .pv-hero-image-inner {
          position: relative;
          z-index: 1;
          height: 100%;
          display: flex;
          align-items: flex-end;
          padding: 14px;
        }

        .pv-hero-cta-row {
          display: flex;
          gap: 10px;
          flex-wrap: wrap;
          margin-top: 6px;
        }
        .pv-cta {
          border-radius: 999px;
          font-weight: 900;
        }
        .pv-cta-primary {
          background: linear-gradient(135deg, var(--pv-accent), var(--pv-accent-2));
          color: white;
          padding: 10px 14px;
          box-shadow: 0 14px 32px rgba(0,0,0,.22);
        }
        .pv-cta-secondary {
          background: rgba(255,255,255,.18);
          color: white;
          border: 1px solid rgba(255,255,255,.35);
          padding: 10px 14px;
        }

        .pv-hero-float {
          position: absolute;
          top: 14px;
          right: 14px;
          width: min(340px, 78%);
          z-index: 2;
        }
        @media (max-width: 520px) {
          .pv-hero-image { height: 250px; }
          .pv-hero-float { position: static; width: 100%; margin-top: 12px; }
          .pv-brand { max-width: 42%; }
        }

        /* News / FAQ lists */
        .pv-news-item, .pv-faq-item {
          padding: 10px 0;
          border-bottom: 1px solid var(--pv-line);
        }
        .pv-news-item:last-child, .pv-faq-item:last-child { border-bottom: none; }

        .pv-news-meta {
          font-size: 12px;
          color: var(--pv-muted);
          display: flex;
          gap: 10px;
          align-items: center;
        }
        .pv-badge {
          display: inline-flex;
          align-items: center;
          border-radius: 999px;
          padding: 2px 8px;
          background: var(--pv-chip-bg);
          border: 1px solid var(--pv-chip-border);
          color: var(--pv-accent);
          font-weight: 900;
          font-size: 11px;
        }
        .pv-q { font-weight: 900; }
        .pv-a { color: var(--pv-muted); }

        /* Actions */
        .pv-action {
          border-radius: 12px;
          font-weight: 900;
          padding: 10px 12px;
          background: var(--pv-chip-bg);
          border: 1px solid var(--pv-chip-border);
        }
        .pv-action-primary {
          background: linear-gradient(135deg, var(--pv-accent), var(--pv-accent-2));
          color: white;
          border: none;
          box-shadow: 0 14px 32px rgba(0,0,0,.22);
        }

        .pv-prefooter {
          padding: 12px 12px;
          color: var(--pv-muted);
          font-size: 12px;
          display: flex;
          justify-content: space-between;
          align-items: center;
          gap: 8px;
        }
        .pv-prefooter a { color: var(--pv-muted); text-decoration: none; }
        .pv-prefooter a:hover { text-decoration: underline; }
/* ====== Preview tabs icon spacing ====== */
.cvhb-preview-tabs .q-tab__icon { margin-right: 6px; }
</style>
"""
    )
# =========================

# [BLK-09] UI parts
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
                                    # Step1の分岐（テンプレID）を常に同期
                                    step1["template_id"] = resolve_template_id(step1)
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

                                                    # 福祉事業所だけ追加分岐（初期値を入れる）
                                                    if value == "福祉事業所":
                                                        step1["welfare_domain"] = step1.get("welfare_domain") or WELFARE_DOMAIN_PRESETS[0]["value"]
                                                        step1["welfare_mode"] = step1.get("welfare_mode") or WELFARE_MODE_PRESETS[0]["value"]
                                                    else:
                                                        # 福祉以外は空にする
                                                        step1["welfare_domain"] = ""
                                                        step1["welfare_mode"] = ""

                                                    step1["template_id"] = resolve_template_id(step1)
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

                                                # 福祉事業所の追加分岐（入所/通所/児童など）
                                                if current_industry == "福祉事業所":
                                                    ui.separator().classes("q-my-sm")
                                                    ui.label("福祉事業所のタイプ").classes("text-subtitle2")
                                                    ui.label("「介護/障がい/児童」と「入所/通所」を選びます。").classes("cvhb-muted")

                                                    # 初期値（業種選択直後でも必ず表示されるように）
                                                    current_domain = step1.get("welfare_domain") or WELFARE_DOMAIN_PRESETS[0]["value"]
                                                    current_mode = step1.get("welfare_mode") or WELFARE_MODE_PRESETS[0]["value"]

                                                    def set_domain(v: str) -> None:
                                                        step1["welfare_domain"] = v
                                                        step1["template_id"] = resolve_template_id(step1)
                                                        update_and_refresh()
                                                        industry_selector.refresh()

                                                    def set_mode(v: str) -> None:
                                                        step1["welfare_mode"] = v
                                                        step1["template_id"] = resolve_template_id(step1)
                                                        update_and_refresh()
                                                        industry_selector.refresh()

                                                    ui.label("サービス種別").classes("text-body2 q-mt-sm")
                                                    with ui.column().classes("q-gutter-xs"):
                                                        for x in WELFARE_DOMAIN_PRESETS:
                                                            selected = x["value"] == current_domain
                                                            cls = "cvhb-choice q-pa-sm rounded-borders w-full"
                                                            if selected:
                                                                cls += " is-selected"
                                                            c = ui.card().classes(cls).props("flat bordered")
                                                            with c:
                                                                with ui.row().classes("items-start justify-between"):
                                                                    with ui.column().classes("q-gutter-xs"):
                                                                        ui.label(x["label"]).classes("text-body1")
                                                                        ui.label(x["hint"]).classes("cvhb-muted")
                                                                    if selected:
                                                                        ui.icon("check_circle").classes("text-primary")
                                                            c.on("click", lambda e, v=x["value"]: set_domain(v))

                                                    ui.label("提供形態").classes("text-body2 q-mt-sm")
                                                    with ui.column().classes("q-gutter-xs"):
                                                        for x in WELFARE_MODE_PRESETS:
                                                            selected = x["value"] == current_mode
                                                            cls = "cvhb-choice q-pa-sm rounded-borders w-full"
                                                            if selected:
                                                                cls += " is-selected"
                                                            c = ui.card().classes(cls).props("flat bordered")
                                                            with c:
                                                                with ui.row().classes("items-start justify-between"):
                                                                    with ui.column().classes("q-gutter-xs"):
                                                                        ui.label(x["label"]).classes("text-body1")
                                                                        ui.label(x["hint"]).classes("cvhb-muted")
                                                                    if selected:
                                                                        ui.icon("check_circle").classes("text-primary")
                                                            c.on("click", lambda e, v=x["value"]: set_mode(v))

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
                                    "width: clamp(390px, 46vw, 520px); height: clamp(820px, 90vh, 1120px); overflow: hidden; border-radius: 22px; margin: 0 auto;"
                                ).props("flat bordered"):
                                    with ui.element("div").style("height: 100%; overflow: hidden;"):
                                        @ui.refreshable
                                        def preview_mobile_panel():
                                            if not p:
                                                ui.label("案件を選ぶとプレビューが出ます").classes("cvhb-muted q-pa-md")
                                                return
                                            try:
                                                render_preview(p, mode="mobile")
                                            except Exception as e:
                                                ui.label("プレビューでエラーが発生しました").classes("text-negative")
                                                ui.label(sanitize_error_text(e)).classes("cvhb-muted")
                                                traceback.print_exc()

                                        preview_ref["refresh_mobile"] = preview_mobile_panel.refresh
                                        preview_mobile_panel()

                            with ui.tab_panel("pc"):
                                with ui.card().style(
                                    "width: min(100%, 1240px); height: clamp(820px, 90vh, 1120px); overflow: hidden; border-radius: 14px; margin: 0 auto;"
                                ).props("flat bordered"):
                                    with ui.element("div").style("height: 100%; overflow: hidden; background: transparent;"):
                                        @ui.refreshable
                                        def preview_pc_panel():
                                            if not p:
                                                ui.label("案件を選ぶとプレビューが出ます").classes("cvhb-muted q-pa-md")
                                                return
                                            try:
                                                render_preview(p, mode="pc")
                                            except Exception as e:
                                                ui.label("プレビューでエラーが発生しました").classes("text-negative")
                                                ui.label(sanitize_error_text(e)).classes("cvhb-muted")
                                                traceback.print_exc()

                                        preview_ref["refresh_pc"] = preview_pc_panel.refresh
                                        preview_pc_panel()


# =========================

# [BLK-12] Pages
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
