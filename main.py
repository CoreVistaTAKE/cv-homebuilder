
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
# [BLK-01] Heroku runtime guard
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


def _preview_preflight_error() -> Optional[str]:
    """プレビュー描画前に、必要な定義が揃っているかチェックして事故を減らす。"""
    try:
        import inspect

        required = {
            "_preview_glass_style": "callable",
            "_safe_list": "callable",
            "HERO_IMAGE_PRESETS": "dict",
            "HERO_IMAGE_DEFAULT": "str",
            "HERO_IMAGE_OPTIONS": "list",
        }
        g = globals()
        for name, kind in required.items():
            if name not in g:
                return f"内部定義が見つかりません: {name}"
            v = g[name]
            if kind == "callable" and not callable(v):
                return f"内部定義が不正です: {name}（callableではありません）"
            if kind == "dict" and not isinstance(v, dict):
                return f"内部定義が不正です: {name}（dictではありません）"
            if kind == "str" and not isinstance(v, str):
                return f"内部定義が不正です: {name}（strではありません）"
            if kind == "list" and not isinstance(v, list):
                return f"内部定義が不正です: {name}（listではありません）"

        # 引数ズレ事故（unexpected keyword argument 等）を早期に検知する
        sig = inspect.signature(g["_preview_glass_style"])
        if "dark" not in sig.parameters:
            return "_preview_glass_style に dark 引数がありません"
        if not any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
            return "_preview_glass_style は **kwargs を受け取る必要があります（互換維持のため）"

        return None
    except Exception:
        # preflight 自体が原因で落ちないようにする
        return None

# =========================
# [BLK-02] Global UI styles (v0.6.4)
# =========================

def inject_global_styles() -> None:
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
    max-width: 2000px;
    margin-left: 0;
    margin-right: auto;
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
/* ====== 260218 Layout (Preview) ====== */
.pv-shell.pv-layout-260218{
  height: 100%;
  width: 100%;
  overflow: hidden;
  border-radius: inherit;
  background: var(--pv-bg-img);
  color: var(--pv-text);
  font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Noto Sans JP", "Hiragino Kaku Gothic ProN", "Yu Gothic", "Meiryo", sans-serif;
}

/* ===== Builder内プレビューの「見やすい幅」(要望) =====
   スマホ: 750px / PC: 1080px
   ※カード自体は 800 / 最大1200 にして「余白」を作る
*/
.pv-shell.pv-layout-260218.pv-mode-mobile{
  width: 750px;
  max-width: 100%;
  height: 100%;
  margin: 0 auto;
}

.pv-shell.pv-layout-260218.pv-mode-pc{
  width: 1080px;
  max-width: 100%;
  height: 100%;
  margin: 0 auto;
}


.pv-layout-260218 .pv-scroll{
  height: 100%;
  overflow-y: auto;
  overscroll-behavior: contain;
  scroll-behavior: smooth;
  background: var(--pv-bg-img);
}

.pv-layout-260218 .pv-topbar-260218{
  position: sticky;
  top: 0;
  z-index: 50;
  padding: 12px 14px;
  backdrop-filter: blur(16px);
  background: rgba(255,255,255,0.72);
  border-bottom: 1px solid rgba(255,255,255,0.35);
}

.pv-layout-260218.pv-dark .pv-topbar-260218{
  background: rgba(13,16,22,0.72);
  border-bottom: 1px solid rgba(255,255,255,0.10);
}

.pv-layout-260218 .pv-topbar-inner{
  width: 100%;
  max-width: none;
  margin: 0;
}

/* ヘッダー内：会社名は左、メニューは右に固定 */

.pv-layout-260218 .pv-brand{
  cursor: pointer;
  gap: 10px;
  max-width: none;
  flex: 1 1 auto;
  min-width: 0;
  justify-content: flex-start;
}

.pv-layout-260218 .pv-favicon{
  width: 28px;
  height: 28px;
  border-radius: 8px;
  object-fit: cover;
  border: 1px solid rgba(0,0,0,0.08);
  background: rgba(255,255,255,0.9);
}

.pv-layout-260218.pv-dark .pv-favicon{
  border-color: rgba(255,255,255,0.16);
  background: rgba(0,0,0,0.20);
}

.pv-layout-260218 .pv-brand-name{
  font-weight: 800;
  letter-spacing: 0.01em;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.pv-layout-260218 .pv-menu-btn{
  opacity: 0.92;
}

/* 画像のように「三本線＋MENU（下）」にする */
.pv-layout-260218 .pv-menu-btn.q-btn{
  color: var(--pv-text);
  padding: 6px 8px;
  min-width: 48px;
}

.pv-layout-260218 .pv-menu-btn .q-btn__content{
  flex-direction: column;
  line-height: 1;
}

.pv-layout-260218 .pv-menu-btn .q-icon{
  font-size: 24px;
  margin: 0;
}

.pv-layout-260218 .pv-menu-btn .q-btn__content .block{
  font-size: 10px;
  font-weight: 800;
  letter-spacing: 0.08em;
  margin-top: 2px;
}


.pv-layout-260218 .pv-nav-card{
  width: min(92vw, 360px);
  border-radius: 18px;
}

.pv-layout-260218 .pv-nav-item{
  justify-content: flex-start;
}

.pv-layout-260218 .pv-nav-item.q-btn{
  color: var(--pv-text) !important;
  font-weight: 800;
}

.pv-layout-260218 .pv-nav-item.q-btn:hover{
  background: rgba(255,255,255,0.22);
}

.pv-layout-260218.pv-dark .pv-nav-item.q-btn:hover{
  background: rgba(255,255,255,0.08);
}

.pv-layout-260218 .pv-menu-btn.q-btn{
  color: var(--pv-text) !important;
}

.pv-layout-260218 .pv-main{
  max-width: 1080px;
  margin: 0 auto;
  padding: 18px 18px 0;
}

.pv-layout-260218 .pv-section{
  margin: 18px 0 28px;
}

.pv-layout-260218 .pv-section-head{
  display: flex;
  align-items: baseline;
  gap: 10px;
  margin-bottom: 12px;
}

.pv-layout-260218 .pv-section-title{
  font-weight: 900;
  font-size: 1.08rem;
}

.pv-layout-260218 .pv-section-en{
  font-weight: 800;
  font-size: 0.78rem;
  letter-spacing: 0.14em;
  opacity: 0.55;
}

.pv-layout-260218 .pv-panel{
  position: relative;
  overflow: hidden;
  border-radius: 22px;
  border: 1px solid var(--pv-border);
  box-shadow: var(--pv-shadow);
}

.pv-layout-260218 .pv-panel::before{
  content: "";
  position: absolute;
  inset: -1px;
  border-radius: inherit;
  pointer-events: none;
  background:
    radial-gradient(520px 420px at 18% 18%, var(--pv-blob4), transparent 62%),
    radial-gradient(420px 340px at 92% 0%, rgba(255,255,255,0.22), transparent 60%);
  opacity: 0.55;
}

.pv-layout-260218.pv-dark .pv-panel::before{
  background:
    radial-gradient(520px 420px at 18% 18%, var(--pv-blob4), transparent 62%),
    radial-gradient(420px 340px at 92% 0%, rgba(255,255,255,0.10), transparent 60%);
  opacity: 0.45;
}

.pv-layout-260218 .pv-panel > *{
  position: relative;
}

.pv-layout-260218 .pv-panel-glass{
  background: var(--pv-card);
}

.pv-layout-260218 .pv-panel-flat{
  background: rgba(255,255,255,0.70);
  backdrop-filter: blur(14px);
  padding: 16px;
}

.pv-layout-260218.pv-dark .pv-panel-flat{
  background: rgba(15,18,25,0.48);
}

.pv-layout-260218 .pv-muted{
  color: var(--pv-muted);
}

.pv-layout-260218 .pv-h2{
  font-weight: 900;
  font-size: 1.05rem;
  margin-bottom: 6px;
}

.pv-layout-260218 .pv-bodytext{
  color: var(--pv-text);
  opacity: 0.86;
  line-height: 1.7;
}

.pv-layout-260218 .pv-bullets{
  margin: 10px 0 0;
  padding-left: 18px;
  color: var(--pv-text);
  opacity: 0.84;
  line-height: 1.7;
}

.pv-layout-260218 .pv-hero-grid{
  display: grid;
  grid-template-columns: 1fr;
  gap: 14px;
  align-items: start;
}

.pv-layout-260218.pv-mode-pc .pv-hero-grid{
  grid-template-columns: 1fr;
  gap: 18px;
  align-items: start;
}

.pv-layout-260218 .pv-hero-title{
  font-weight: 1000;
  font-size: clamp(1.35rem, 3.4vw, 2.25rem);
  line-height: 1.18;
  margin-bottom: 6px;
}

.pv-layout-260218 .pv-hero-sub{
  color: var(--pv-muted);
  line-height: 1.7;
  margin-bottom: 12px;
}

.pv-layout-260218 .pv-cta-row{
  gap: 10px;
  flex-wrap: wrap;
}

.pv-layout-260218 .pv-btn.q-btn{
  border-radius: 999px;
  font-weight: 800;
}

.pv-layout-260218 .pv-hero-slider{
  border-radius: 30px;
  overflow: hidden;
  border: 1px solid rgba(255,255,255,0.40);
  box-shadow: var(--pv-shadow);
  background: rgba(255,255,255,0.20);
}

.pv-layout-260218.pv-dark .pv-hero-slider{
  border-color: rgba(255,255,255,0.12);
  background: rgba(0,0,0,0.14);
}

.pv-layout-260218 .pv-hero-track{
  display: flex;
  transition: transform 700ms cubic-bezier(0.2, 0.8, 0.2, 1);
  will-change: transform;
}

.pv-layout-260218 .pv-hero-slide{
  flex: 0 0 100%;
}

.pv-layout-260218 .pv-hero-img{
  width: 100%;
  height: 240px;
  display: block;
  object-fit: cover;
}

.pv-layout-260218.pv-mode-pc .pv-hero-img{
  height: 420px;
}

.pv-layout-260218 .pv-news-list{
  margin-top: 6px;
}

.pv-layout-260218 .pv-news-item{
  display: grid;
  grid-template-columns: 110px 92px 1fr;
  gap: 10px;
  align-items: center;
  padding: 10px 0;
  border-bottom: 1px solid rgba(0,0,0,0.06);
}

.pv-layout-260218.pv-dark .pv-news-item{
  border-bottom-color: rgba(255,255,255,0.10);
}

.pv-layout-260218 .pv-news-date{
  font-weight: 700;
  opacity: 0.7;
}

.pv-layout-260218 .pv-news-cat{
  display: inline-flex;
  align-items: center;
  justify-content: center;
  height: 24px;
  padding: 0 10px;
  border-radius: 999px;
  background: var(--pv-primary-weak);
  color: var(--pv-primary);
  font-weight: 900;
  font-size: 0.72rem;
}

.pv-layout-260218.pv-dark .pv-news-cat{
  background: rgba(255,255,255,0.10);
  color: rgba(255,255,255,0.86);
}

.pv-layout-260218 .pv-news-title{
  font-weight: 700;
}

.pv-layout-260218 .pv-link-btn.q-btn{
  margin-top: 10px;
  font-weight: 900;
}

.pv-layout-260218 .pv-about-grid{
  display: grid;
  grid-template-columns: 1fr;
  gap: 14px;
  align-items: stretch;
}

.pv-layout-260218.pv-mode-pc .pv-about-grid{
  grid-template-columns: 1.12fr 0.88fr;
}

.pv-layout-260218 .pv-about-img{
  width: 100%;
  height: 320px;
  border-radius: 22px;
  object-fit: cover;
  border: 1px solid var(--pv-border);
  box-shadow: var(--pv-shadow);
}

.pv-layout-260218.pv-mode-mobile .pv-about-img{
  height: 240px;
}

.pv-layout-260218 .pv-surface-white{
  background: rgba(255,255,255,0.82);
  border: 1px solid rgba(255,255,255,0.42);
  border-radius: 22px;
  box-shadow: var(--pv-shadow);
  backdrop-filter: blur(16px);
  padding: 16px;
}

.pv-layout-260218.pv-dark .pv-surface-white{
  background: rgba(12,15,22,0.52);
  border-color: rgba(255,255,255,0.12);
}

.pv-layout-260218 .pv-services-grid{
  display: grid;
  grid-template-columns: 1fr;
  gap: 14px;
  align-items: start;
}

.pv-layout-260218.pv-mode-pc .pv-services-grid{
  grid-template-columns: 0.95fr 1.05fr;
  align-items: center;
}

.pv-layout-260218 .pv-services-img{
  width: 100%;
  height: 260px;
  border-radius: 18px;
  object-fit: cover;
  border: 1px solid rgba(0,0,0,0.06);
}

.pv-layout-260218.pv-dark .pv-services-img{
  border-color: rgba(255,255,255,0.12);
}

.pv-layout-260218.pv-mode-pc .pv-services-img{
  height: 320px;
}

.pv-layout-260218 .pv-service-item{
  padding: 10px 0;
  border-bottom: 1px solid rgba(0,0,0,0.06);
}

.pv-layout-260218.pv-dark .pv-service-item{
  border-bottom-color: rgba(255,255,255,0.10);
}

.pv-layout-260218 .pv-service-title{
  font-weight: 900;
  margin-bottom: 2px;
}

.pv-layout-260218 .pv-faq-item{
  padding: 12px 0;
  border-bottom: 1px solid rgba(0,0,0,0.06);
}

.pv-layout-260218.pv-dark .pv-faq-item{
  border-bottom-color: rgba(255,255,255,0.10);
}

.pv-layout-260218 .pv-faq-q{
  font-weight: 900;
}

.pv-layout-260218 .pv-faq-a{
  margin-top: 4px;
  color: var(--pv-muted);
  line-height: 1.7;
}

.pv-layout-260218 .pv-access-card{
  padding: 16px;
}

.pv-layout-260218 .pv-contact-card{
  padding: 16px;
}

.pv-layout-260218 .pv-contact-actions{
  gap: 10px;
  flex-wrap: wrap;
  margin-top: 12px;
}

.pv-layout-260218 .pv-companybar{
  margin: 22px 0 0;
  padding: 0 18px 18px;
}

.pv-layout-260218 .pv-companybar-inner{
  max-width: 1080px;
  margin: 0 auto;
  border-radius: 22px;
  padding: 14px 16px;
  background: rgba(255,255,255,0.58);
  border: 1px solid rgba(255,255,255,0.28);
  backdrop-filter: blur(14px);
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.pv-layout-260218.pv-dark .pv-companybar-inner{
  background: rgba(13,16,22,0.52);
  border-color: rgba(255,255,255,0.12);
}

.pv-layout-260218 .pv-companybar-name{
  font-weight: 1000;
}

.pv-layout-260218 .pv-companybar-meta{
  color: var(--pv-muted);
  font-size: 0.85rem;
  margin-top: 2px;
}

.pv-layout-260218 .pv-footer{
  margin-top: 8px;
  padding: 18px;
  background: rgba(10,12,18,0.92);
  border-top: 1px solid rgba(255,255,255,0.08);
  color: rgba(255,255,255,0.78);
}

.pv-layout-260218 .pv-footer-grid{
  max-width: 1080px;
  margin: 0 auto;
  display: grid;
  grid-template-columns: 1fr;
  gap: 12px;
}

.pv-layout-260218.pv-mode-pc .pv-footer-grid{
  grid-template-columns: 1.2fr 0.8fr;
}

.pv-layout-260218 .pv-footer-brand{
  font-weight: 1000;
  color: #fff;
  margin-bottom: 6px;
}

.pv-layout-260218 .pv-footer-cap{
  font-weight: 900;
  color: rgba(255,255,255,0.92);
  margin-top: 10px;
  margin-bottom: 6px;
}

.pv-layout-260218 .pv-footer-link.q-btn,
.pv-layout-260218 .pv-footer-link.q-btn .q-btn__content,
.pv-layout-260218 .pv-footer-link.q-btn .q-btn__content span{
  color: rgba(255,255,255,0.86) !important;
}
.pv-layout-260218 .pv-footer-link.q-btn{
  justify-content: flex-start;
  padding-left: 0;
}

.pv-layout-260218 .pv-footer-link.q-btn:hover{
  color: #fff !important;
}

.pv-layout-260218 .pv-footer-text{
  color: rgba(255,255,255,0.76);
  line-height: 1.7;
}

.pv-layout-260218 .pv-footer-copy{
  max-width: 1080px;
  margin: 12px auto 0;
  opacity: 0.62;
  font-size: 0.8rem;
}
/* ====== Preview tabs icon spacing ====== */
.cvhb-preview-tabs .q-tab__icon { margin-right: 6px; }
</style>
"""
    )

    ui.add_head_html(
        """
<script>
(function(){
  window.__cvhbHeroIntervals = window.__cvhbHeroIntervals || {};
  window.cvhbInitHeroSlider = window.cvhbInitHeroSlider || function(sliderId, intervalMs){
    try{
      const slider = document.getElementById(sliderId);
      if(!slider) return;
      const track = slider.querySelector('.pv-hero-track');
      if(!track) return;
      const slides = track.children ? track.children.length : 0;
      if(window.__cvhbHeroIntervals[sliderId]){
        clearInterval(window.__cvhbHeroIntervals[sliderId]);
        delete window.__cvhbHeroIntervals[sliderId];
      }
      if(slides <= 1){
        track.style.transform = 'translateX(0%)';
        return;
      }
      let idx = 0;
      window.__cvhbHeroIntervals[sliderId] = setInterval(function(){
        idx = (idx + 1) % slides;
        track.style.transform = 'translateX(-' + (idx * 100) + '%)';
      }, intervalMs || 4600);
    } catch(e){}
  };

  window.cvhbPreviewScrollTo = window.cvhbPreviewScrollTo || function(rootId, targetId){
    try{
      const root = document.getElementById(rootId);
      if(!root) return;
      const sc = root.querySelector('.pv-scroll');
      const el = root.querySelector('#' + targetId);
      if(!sc || !el) return;
      const scRect = sc.getBoundingClientRect();
      const elRect = el.getBoundingClientRect();
      const top = sc.scrollTop + (elRect.top - scRect.top) - 72;
      sc.scrollTo({top: top, behavior: 'smooth'});
    } catch(e){}
  };
})();
</script>
""",
    )
# =========================
# [BLK-03] Config
# =========================

def read_text_file(path: str, default: str = "") -> str:
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except Exception:
        return default


VERSION = read_text_file("VERSION", "0.6.81")
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
    "C: 街並み": "https://images.unsplash.com/photo-1449824913935-59a10b8d2000?auto=format&fit=crop&w=1200&q=80",

    # 福祉テンプレ向けの“雰囲気”プリセット（※ 302リダイレクトの Unsplash Source をやめ、直URLで安定化）
    "D: ひかり": "https://images.unsplash.com/photo-1519751138087-5bf79df62d5b?auto=format&fit=crop&w=1200&q=80",
    "E: 木": "https://images.unsplash.com/photo-1441974231531-c6227db76b6e?auto=format&fit=crop&w=1200&q=80",
    "F: 手": "https://images.unsplash.com/photo-1749065311606-fa115df115af?auto=format&fit=crop&w=1200&q=80",
    "G: 家": "https://images.unsplash.com/photo-1632927126546-e3e051a0ba6e?auto=format&fit=crop&w=1200&q=80",
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


def apply_template_starter_defaults(p: dict, template_id: str) -> None:
    """業種（テンプレ）を切り替えたときの「初期文言」を入れる。

    重要:
    - すでにユーザーが編集した内容は、基本的に上書きしない
    - ただし「サンプル文章」のままの場合は、テンプレに合わせて入れ替える
    """
    try:
        data = p.get("data") or {}
        p["data"] = data

        step2 = data.get("step2") or {}
        blocks = data.get("blocks") or {}
        data["step2"] = step2
        data["blocks"] = blocks

        hero = blocks.get("hero") or {}
        philosophy = blocks.get("philosophy") or {}
        news = blocks.get("news") or {}
        faq = blocks.get("faq") or {}
        access = blocks.get("access") or {}
        contact = blocks.get("contact") or {}

        blocks["hero"] = hero
        blocks["philosophy"] = philosophy
        blocks["news"] = news
        blocks["faq"] = faq
        blocks["access"] = access
        blocks["contact"] = contact

        services = philosophy.get("services") or {}
        if not isinstance(services, dict):
            services = {}
        philosophy["services"] = services

        def _txt(v) -> str:
            return str(v or "").strip()

        def set_text(obj: dict, key: str, new_value: str, *, replace_if: Optional[set[str]] = None, startswith: Optional[str] = None) -> None:
            cur = _txt(obj.get(key))
            if cur == "":
                obj[key] = new_value
                return
            if replace_if and cur in replace_if:
                obj[key] = new_value
                return
            if startswith and cur.startswith(startswith):
                obj[key] = new_value

        def set_list(obj: dict, key: str, new_list: list, *, replace_if_lists: Optional[list] = None) -> None:
            cur = obj.get(key)
            if not isinstance(cur, list) or len(cur) == 0:
                obj[key] = new_list
                return
            if replace_if_lists and cur in replace_if_lists:
                obj[key] = new_list
                return
            if all(_txt(x) == "" for x in cur):
                obj[key] = new_list

        def set_services_items(new_items: list, *, replace_if_items_lists: Optional[list] = None) -> None:
            cur = services.get("items")
            if not isinstance(cur, list) or len(cur) == 0:
                services["items"] = new_items
                return

            if replace_if_items_lists and cur in replace_if_items_lists:
                services["items"] = new_items
                return

            # 既存の「サンプル」っぽい形なら入れ替える
            for it in cur:
                if isinstance(it, dict) and (
                    _txt(it.get("title")).startswith("サービス") or _txt(it.get("title")).startswith("項目")
                ):
                    services["items"] = new_items
                    return

            if all(isinstance(it, dict) and _txt(it.get("title")) == "" and _txt(it.get("body")) == "" for it in cur):
                services["items"] = new_items

        def set_faq_items(new_items: list, *, replace_if_items_lists: Optional[list] = None) -> None:
            cur = faq.get("items")
            if not isinstance(cur, list) or len(cur) == 0:
                faq["items"] = new_items
                return

            if replace_if_items_lists and cur in replace_if_items_lists:
                faq["items"] = new_items
                return

            for it in cur:
                if isinstance(it, dict) and _txt(it.get("q")).startswith("サンプル"):
                    faq["items"] = new_items
                    return

            if all(isinstance(it, dict) and _txt(it.get("q")) == "" and _txt(it.get("a")) == "" for it in cur):
                faq["items"] = new_items

        # --- 会社テンプレの「サンプル」 ---
        corp_sample_catch = "スタッフ・利用者の笑顔を守る企業"
        corp_sample_sub = "地域に寄り添い、安心できるサービスを届けます"
        corp_sample_points = ["地域密着", "丁寧な対応", "安心の体制"]
        corp_sample_about_body = "ここに理念や会社の紹介文を書きます。\n（あとで自由に書き換えできます）"
        corp_sample_svc_title = "業務内容"
        corp_sample_svc_lead = "提供サービスの概要をここに記載します。"
        corp_sample_svc_items = [
            {"title": "サービス1", "body": "内容をここに記載します。"},
            {"title": "サービス2", "body": "内容をここに記載します。"},
            {"title": "サービス3", "body": "内容をここに記載します。"},
        ]
        corp_sample_faq_items = [
            {"q": "サンプル：見学はできますか？", "a": "はい。お電話またはメールでお気軽にご連絡ください。"},
            {"q": "サンプル：費用はどのくらいですか？", "a": "内容により異なります。まずはご要望をお聞かせください。"},
            {"q": "サンプル：対応エリアはどこまでですか？", "a": "地域により異なります。詳細はお問い合わせください。"},
        ]
        corp_sample_contact_message = "まずはお気軽にご相談ください。"

        # --- テンプレ別の初期文言（6ブロックは維持） ---
        presets: dict[str, dict] = {
            # 会社・企業サイト（基本）
            "corp_v1": {
                "catch_copy": "",
                "sub_catch": corp_sample_sub,
                "primary_cta": "お問い合わせ",
                "secondary_cta": "見学・相談",
                "hero_image": "A: オフィス",
                "about_title": "私たちの想い",
                "about_body": corp_sample_about_body,
                "points": corp_sample_points,
                "svc_title": corp_sample_svc_title,
                "svc_lead": corp_sample_svc_lead,
                "svc_items": corp_sample_svc_items,
                "faq_items": corp_sample_faq_items,
                "contact_message": corp_sample_contact_message,
            },

            # 介護福祉（入所系）
            "care_residential_v1": {
                "catch_copy": "安心して暮らせる、あたたかな住まい",
                "sub_catch": "見学・入居相談を受け付けています",
                "primary_cta": "入居相談",
                "secondary_cta": "見学・相談",
                "hero_image": "G: 家",
                "about_title": "施設紹介",
                "about_body": "お部屋や共用スペース、食事や日々の過ごし方など、施設の雰囲気が伝わるようにご紹介します。安心してご相談いただけるよう、できるだけ分かりやすくまとめました。",
                "points": ["清潔な居室", "日々の見守り", "医療連携"],
                "svc_title": "サービス内容",
                "svc_lead": "医療連携や介護体制など、安心して生活できるサポートを整えています。",
                "svc_items": [
                    {"title": "生活サポート", "body": "食事・入浴・服薬など、日常生活を丁寧に支えます。"},
                    {"title": "医療連携", "body": "協力医療機関と連携し、体調変化に備えます。"},
                    {"title": "夜間体制", "body": "夜間も見守りを行い、緊急時に対応します。"},
                ],
                "faq_items": [
                    {"q": "見学はできますか？", "a": "はい、可能です。日程を調整しますので、お電話またはお問い合わせフォームからご連絡ください。"},
                    {"q": "費用の目安を知りたいです。", "a": "状況により異なります。料金の目安と補足をご案内しますので、お気軽にお問い合わせください。"},
                    {"q": "入居までの流れを教えてください。", "a": "ご相談→見学→面談→ご契約→ご入居の順に進みます。詳細は個別にご案内します。"},
                ],
                "contact_message": "空室状況や費用の目安など、まずはお気軽にお問い合わせください。",
            },

            # 介護福祉（通所系）
            "care_day_v1": {
                "catch_copy": "“できる”が増える毎日へ。",
                "sub_catch": "体験利用・見学を受付中です",
                "primary_cta": "体験利用",
                "secondary_cta": "見学・相談",
                "hero_image": "E: 木",
                "about_title": "サービス内容",
                "about_body": "日中の活動やリハビリ、食事、送迎など、ご利用者さまの毎日が楽しくなるサービスを提供します。はじめての方にも分かりやすいように、ポイントをまとめました。",
                "points": ["送迎あり", "安心の見守り", "楽しい活動"],
                "svc_title": "1日の流れ",
                "svc_lead": "ご利用のイメージができるように、1日の流れを簡単にご紹介します。",
                "svc_items": [
                    {"title": "到着・健康チェック", "body": "体調を確認し、無理のない1日を始めます。"},
                    {"title": "レクリエーション", "body": "季節行事や交流の機会を通して、無理なく楽しむ時間をつくります。"},
                    {"title": "お帰り（送迎）", "body": "ご自宅まで安全にお送りします。"},
                ],
                "faq_items": [
                    {"q": "体験利用はできますか？", "a": "はい。日程をご相談のうえ、ご案内します。"},
                    {"q": "送迎はありますか？", "a": "地域により対応可能です。詳しくはお問い合わせください。"},
                    {"q": "持ち物は必要ですか？", "a": "必要な持ち物は体験前にご案内します。"},
                ],
                "contact_message": "体験利用のご希望やご不安な点など、お気軽にご相談ください。",
            },

            # 障がい福祉（入所系 / グループホーム系）
            "disability_residential_v1": {
                "catch_copy": "安心して暮らせる、あたたかな住まい",
                "sub_catch": "見学・入居相談を受け付けています",
                "primary_cta": "入居相談",
                "secondary_cta": "見学・相談",
                "hero_image": "G: 家",
                "about_title": "事業所の想い",
                "about_body": "私たちは、一人ひとりの生活リズムを大切にしながら、安心して暮らせる環境づくりを行っています。日々の支援の考え方や体制を、分かりやすくまとめました。",
                "points": ["個別支援", "夜間体制", "医療連携"],
                "svc_title": "生活サポート内容",
                "svc_lead": "食事や服薬、日常生活の支援など、生活を支える体制を整えています。",
                "svc_items": [
                    {"title": "日常生活支援", "body": "食事・服薬・清掃など、生活の基本を支えます。"},
                    {"title": "相談支援", "body": "困りごとや不安に寄り添い、必要な支援につなげます。"},
                    {"title": "連携体制", "body": "医療・福祉機関と連携し、安心できる暮らしを支えます。"},
                ],
                "faq_items": [
                    {"q": "見学はできますか？", "a": "はい。ご都合に合わせてご案内します。"},
                    {"q": "夜間の体制はどうなっていますか？", "a": "夜間も見守り体制を整えています。詳しくはご案内します。"},
                    {"q": "費用の目安を知りたいです。", "a": "状況により異なりますので、お気軽にお問い合わせください。"},
                ],
                "contact_message": "空室状況や費用の目安など、まずはお気軽にお問い合わせください。",
            },

            # 障がい福祉（通所系）
            "disability_day_v1": {
                "catch_copy": "“できる”が増える毎日へ。",
                "sub_catch": "見学・体験を受付中です",
                "primary_cta": "体験利用",
                "secondary_cta": "見学・相談",
                "hero_image": "F: 手",
                "about_title": "サービス概要",
                "about_body": "対象の方や提供内容など、サービスの概要を分かりやすくまとめています。まずはお気軽に見学・体験をご相談ください。",
                "points": ["日中活動", "個別支援", "安心の体制"],
                "svc_title": "特徴",
                "svc_lead": "私たちの支援の強みを、3つのポイントでご紹介します。",
                "svc_items": [
                    {"title": "活動の充実", "body": "創作や運動など、楽しみながら取り組める活動を用意しています。"},
                    {"title": "個別支援", "body": "一人ひとりに合わせた支援計画で、無理なく続けられます。"},
                    {"title": "連携", "body": "関係機関やご家族と連携し、安心できる体制を整えます。"},
                ],
                "faq_items": [
                    {"q": "体験利用はできますか？", "a": "はい。日程をご相談のうえ、ご案内します。"},
                    {"q": "対象年齢はありますか？", "a": "サービスにより異なります。まずはお問い合わせください。"},
                    {"q": "送迎はありますか？", "a": "地域により対応可能です。詳しくはお問い合わせください。"},
                ],
                "contact_message": "見学・体験のご希望など、お気軽にご相談ください。",
            },

            # 児童福祉（入所系）
            "child_residential_v1": {
                "catch_copy": "安心して過ごせる、あたたかな環境",
                "sub_catch": "見学・ご相談を受け付けています",
                "primary_cta": "相談する",
                "secondary_cta": "見学する",
                "hero_image": "D: ひかり",
                "about_title": "施設紹介",
                "about_body": "生活環境や支援内容を分かりやすくご紹介します。お子さまやご家族が安心できるよう、丁寧にご案内します。",
                "points": ["安心の体制", "個別支援", "連携"],
                "svc_title": "支援内容",
                "svc_lead": "生活・学習・医療連携など、支援の内容をまとめています。",
                "svc_items": [
                    {"title": "生活支援", "body": "日常生活のサポートを行い、安心して過ごせる環境を整えます。"},
                    {"title": "学習支援", "body": "成長に合わせた学習の機会を提供します。"},
                    {"title": "連携", "body": "医療・関係機関と連携し、必要な支援につなげます。"},
                ],
                "faq_items": [
                    {"q": "見学はできますか？", "a": "はい。日程を調整してご案内します。"},
                    {"q": "入所までの流れを教えてください。", "a": "ご相談→見学→面談→手続き→入所の順に進みます。"},
                    {"q": "費用の目安はありますか？", "a": "状況により異なります。詳しくはお問い合わせください。"},
                ],
                "contact_message": "ご不安な点や手続きのことなど、お気軽にご相談ください。",
            },

            # 児童福祉（通所系 / 児発・放デイ）
            "child_day_v1": {
                "catch_copy": "“できた”が増える、たのしい毎日。",
                "sub_catch": "見学・無料相談を受付中です",
                "primary_cta": "見学する",
                "secondary_cta": "無料相談",
                "hero_image": "D: ひかり",
                "about_title": "私たちの想い",
                "about_body": "お子さま一人ひとりのペースを大切にしながら、安心して通える環境づくりを行っています。保護者の方にも分かりやすいように、ポイントをまとめました。",
                "points": ["安心の療育", "丁寧な支援", "保護者支援"],
                "svc_title": "療育プログラム",
                "svc_lead": "目的や内容が伝わるように、プログラムのポイントをまとめています。",
                "svc_items": [
                    {"title": "生活スキル", "body": "日常生活で必要な力を、遊びの中で育てます。"},
                    {"title": "コミュニケーション", "body": "やりとりの楽しさを増やし、自信につなげます。"},
                    {"title": "運動・感覚", "body": "体を動かす活動で、無理なく成長を促します。"},
                ],
                "faq_items": [
                    {"q": "見学はできますか？", "a": "はい。日程をご相談のうえ、ご案内します。"},
                    {"q": "受給者証が必要ですか？", "a": "サービスにより必要です。手続きも含めてご案内します。"},
                    {"q": "料金の目安を知りたいです。", "a": "状況により異なります。まずはお気軽にご相談ください。"},
                ],
                "contact_message": "見学や無料相談など、お気軽にお問い合わせください。",
            },
        }

        # テンプレIDのゆらぎ（簡易な寄せ）
        if template_id in {"personal_v1", "free6_v1"}:
            template_id = "corp_v1"

        preset = presets.get(template_id)
        if not preset:
            # welfare_v1 は「Step1だけ福祉」を選んだ状態でも最低限の文言を出すための保険
            if template_id == "welfare_v1":
                preset = presets.get("care_day_v1")
            else:
                return

        def _gather(key: str) -> set[str]:
            s: set[str] = set()
            for v in presets.values():
                vv = _txt(v.get(key))
                if vv:
                    s.add(vv)
            return s

        # サンプル値集合（テンプレ切替時に入れ替えてよい値）
        sample_catch = _gather("catch_copy") | {corp_sample_catch}
        sample_sub = _gather("sub_catch") | {corp_sample_sub}
        sample_primary = _gather("primary_cta") | {"お問い合わせ", "体験利用", "入居相談", "見学する", "相談する"}
        sample_secondary = _gather("secondary_cta") | {"見学・相談", "無料相談", "見学する"}
        sample_about_title = _gather("about_title") | {"私たちの想い", "理念・概要"}
        sample_about_body = _gather("about_body") | {corp_sample_about_body}
        sample_points_lists = [v.get("points") for v in presets.values() if isinstance(v.get("points"), list)]
        sample_svc_title = _gather("svc_title") | {corp_sample_svc_title}
        sample_svc_lead = _gather("svc_lead") | {corp_sample_svc_lead}
        sample_svc_items_lists = [v.get("svc_items") for v in presets.values() if isinstance(v.get("svc_items"), list)]
        sample_faq_items_lists = [v.get("faq_items") for v in presets.values() if isinstance(v.get("faq_items"), list)]
        sample_contact_msg = _gather("contact_message") | {corp_sample_contact_message}

        # --- Step2 ---
        set_text(step2, "catch_copy", preset.get("catch_copy", ""), replace_if=sample_catch)

        # --- Hero ---
        set_text(hero, "sub_catch", preset.get("sub_catch", corp_sample_sub), replace_if=sample_sub)
        set_text(hero, "primary_button_text", preset.get("primary_cta", "お問い合わせ"), replace_if=sample_primary)
        set_text(hero, "secondary_button_text", preset.get("secondary_cta", "見学・相談"), replace_if=sample_secondary)

        # hero image preset は「未設定 or 既存プリセット」のときだけ差し替える
        # （ユーザーがURL入力している可能性があるため、完全な上書きはしない）
        if preset.get("hero_image"):
            cur_hero_img = _txt(hero.get("hero_image"))
            if cur_hero_img == "" or cur_hero_img in set(HERO_IMAGE_PRESET_URLS.keys()):
                hero["hero_image"] = preset.get("hero_image")

        # --- About / Philosophy ---
        set_text(philosophy, "title", preset.get("about_title", "私たちの想い"), replace_if=sample_about_title)
        set_text(
            philosophy,
            "body",
            preset.get("about_body", corp_sample_about_body),
            replace_if=sample_about_body,
            startswith="ここに",
        )
        set_list(philosophy, "points", preset.get("points", corp_sample_points), replace_if_lists=sample_points_lists)

        # --- Services (inside philosophy) ---
        set_text(services, "title", preset.get("svc_title", corp_sample_svc_title), replace_if=sample_svc_title)
        set_text(
            services,
            "lead",
            preset.get("svc_lead", corp_sample_svc_lead),
            replace_if=sample_svc_lead,
            startswith="提供サービスの概要",
        )
        set_services_items(preset.get("svc_items", corp_sample_svc_items), replace_if_items_lists=sample_svc_items_lists)

        # --- FAQ ---
        set_faq_items(preset.get("faq_items", corp_sample_faq_items), replace_if_items_lists=sample_faq_items_lists)

        # --- Contact ---
        set_text(contact, "message", preset.get("contact_message", corp_sample_contact_message), replace_if=sample_contact_msg, startswith="ここに")
        if _txt(contact.get("button_text")) == "":
            contact["button_text"] = "お問い合わせ"

    except Exception:
        # テンプレ反映でコケても、アプリ全体を落とさない
        traceback.print_exc()
        return


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
    step2.setdefault("favicon_url", "")
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
    # hero: 最大4枚までのスライド画像（任意）
    hero.setdefault("hero_image_urls", [])
    hero["hero_image_urls"] = _safe_list(hero.get("hero_image_urls"))
    # 旧: hero_image_url を 1枚目として扱う
    _h1 = str(hero.get("hero_image_url") or "").strip()
    if _h1:
        hero["hero_image_urls"] = [_h1] + [u for u in hero["hero_image_urls"] if str(u).strip() and str(u).strip() != _h1]
    # 空を除去 & 最大4枚
    hero["hero_image_urls"] = [str(u).strip() for u in hero["hero_image_urls"] if str(u).strip()][:4]

    philosophy = blocks.setdefault("philosophy", {})
    philosophy.setdefault("title", "私たちの想い")
    philosophy.setdefault("body", "ここに理念や会社の紹介文を書きます。\n（あとで自由に書き換えできます）")
    pts = philosophy.setdefault("points", ["地域密着", "丁寧な対応", "安心の体制"])
    if not isinstance(pts, list):
        pts = ["地域密着", "丁寧な対応", "安心の体制"]
    while len(pts) < 3:
        pts.append("")
    philosophy["points"] = pts[:3]
    # philosophy: 画像（任意）
    philosophy.setdefault("image_url", "")

    # services: 業務内容（philosophyブロック内に統合 / 6ブロック固定のまま）
    services = philosophy.setdefault(
        "services",
        {
            "title": "業務内容",
            "lead": "提供サービスの概要をここに記載します。",
            "image_url": "",
            "items": [
                {"title": "サービス1", "body": "内容をここに記載します。"},
                {"title": "サービス2", "body": "内容をここに記載します。"},
                {"title": "サービス3", "body": "内容をここに記載します。"},
            ],
        },
    )
    if not isinstance(services, dict):
        services = {}
        philosophy["services"] = services
    services.setdefault("title", "業務内容")
    services.setdefault("lead", "提供サービスの概要をここに記載します。")
    services.setdefault("image_url", "")
    items = services.get("items")
    if not isinstance(items, list):
        items = []
    # item 正規化（最大6 / 表示は基本3）
    norm_items = []
    for it in items:
        if isinstance(it, dict):
            t = str(it.get("title") or "").strip()
            b = str(it.get("body") or "").strip()
            norm_items.append({"title": t, "body": b})
    if not norm_items:
        norm_items = [
            {"title": "サービス1", "body": "内容をここに記載します。"},
            {"title": "サービス2", "body": "内容をここに記載します。"},
            {"title": "サービス3", "body": "内容をここに記載します。"},
        ]
    services["items"] = norm_items[:6]

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

    # ---- Template-specific starter defaults (safe) ----
    # 業種を切り替えたときに「文章が変わらない」問題を避けるため、
    # 初期文（空/サンプル）だけをテンプレに合わせて差し替える。
    template_id = resolve_template_id(step1)
    step1["template_id"] = template_id

    # Ensure keys exist for preview stability
    contact.setdefault("button_text", "お問い合わせ")

    applied = step1.get("_applied_template_id")
    if applied != template_id:
        apply_template_starter_defaults(p, template_id)
        step1["_applied_template_id"] = template_id

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
            "step2": {"company_name": "", "favicon_url": "", "catch_copy": "", "phone": "", "address": "", "email": ""},
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
# [BLK-10] Preview rendering
# =========================
# [BLK-10] Preview: Glassmorphism theme (SP/PC)

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
    # 「白」はアクセントが白だと見えないので、濃い色に寄せる（=白基調 + 濃いアクセント）
    if primary == "white":
        return "#0f172a"
    # 「黒」はダークモード扱いにする（ボタン等が沈まないように、見えるアクセントを採用）
    if primary == "black":
        return "#60a5fa"  # blue-400
    # それ以外は既存の COLOR_HEX を採用
    return COLOR_HEX.get(primary, "#1976d2")

def _preview_accent2_hex(primary: str, accent_hex: str) -> str:
    """プレビュー用のアクセント2（グラデーション用の2色目）"""
    presets = {
        "blue": "#7c3aed",    # violet-600
        "green": "#14b8a6",   # teal-500
        "red": "#fb7185",     # rose-400
        "orange": "#f59e0b",  # amber-500
        "purple": "#ec4899",  # pink-500
        "grey": "#64748b",    # slate-500
        "black": "#a78bfa",   # violet-400
        "white": "#60a5fa",   # blue-400
    }
    if primary in presets:
        return presets[primary]
    # fallback: accent を少しだけ明るくして2色目にする
    try:
        return _blend_hex(accent_hex, "#ffffff", 0.35)
    except Exception:
        return accent_hex



def _preview_glass_style(step1_or_primary=None, *, dark: Optional[bool] = None, **_ignore) -> str:
    """Return inline CSS variables for the preview glass theme.

    - カラー設定が確実に効くように、ここで必要なCSS変数をすべて揃える
    - 背景は「薄いグラデ + 複数の丸い光（ガラス）」で奥行きを作る
    """

    # ---- primary color ----
    primary = "blue"
    try:
        if isinstance(step1_or_primary, dict):
            primary = str(step1_or_primary.get("primary_color") or "blue")
        elif isinstance(step1_or_primary, str) and step1_or_primary:
            primary = str(step1_or_primary)
    except Exception:
        primary = "blue"

    accent = _preview_accent_hex(primary)
    accent2 = _preview_accent2_hex(primary, accent)

    # ---- dark mode decision ----
    if dark is None:
        dark = (primary == "black")
    is_dark = bool(dark)

    # ---- color math ----
    r1, g1, b1 = _hex_to_rgb(accent)
    r2, g2, b2 = _hex_to_rgb(accent2)

    if not is_dark:
        # 背景（かなり薄く、でも色は感じる）
        bg1 = _blend_hex(accent, "#ffffff", 0.92)
        bg2 = _blend_hex(accent2, "#ffffff", 0.94)

        text = "#0f172a"
        muted = "rgba(15, 23, 42, 0.72)"
        border = "rgba(255, 255, 255, 0.38)"
        line = "rgba(15, 23, 42, 0.10)"

        # カード（＝各ブロック枠）をもう少し透明に（体感で約+50%）
        card = "rgba(255, 255, 255, 0.34)"
        chip_bg = "rgba(255, 255, 255, 0.36)"
        chip_border = "rgba(255, 255, 255, 0.32)"
        shadow = "0 24px 70px rgba(15, 23, 42, 0.12)"

        blob3 = "rgba(255, 255, 255, 0.26)"
        blob4_hex = _blend_hex(accent2, "#ffffff", 0.55)
        r4, g4, b4 = _hex_to_rgb(blob4_hex)

        primary_weak = f"rgba({r1}, {g1}, {b1}, 0.14)"

        bg_img = (
            f"radial-gradient(980px 620px at 12% 10%, rgba({r1}, {g1}, {b1}, 0.18), transparent 60%),"
            f"radial-gradient(920px 560px at 88% 8%, rgba({r2}, {g2}, {b2}, 0.14), transparent 60%),"
            f"radial-gradient(820px 520px at 8% 92%, {blob3}, transparent 62%),"
            f"radial-gradient(760px 520px at 60% 55%, rgba({r4}, {g4}, {b4}, 0.18), transparent 62%),"
            f"linear-gradient(160deg, {bg1} 0%, {bg2} 45%, {bg1} 100%)"
        )

        blob4 = f"rgba({r4}, {g4}, {b4}, 0.22)"

    else:
        # ダーク：黒ベタではなく、ほんのり色を乗せる
        bg1 = _blend_hex("#0b1220", accent, 0.10)
        bg2 = _blend_hex("#060913", accent2, 0.10)

        text = "rgba(255, 255, 255, 0.92)"
        muted = "rgba(255, 255, 255, 0.72)"
        border = "rgba(255, 255, 255, 0.22)"
        line = "rgba(255, 255, 255, 0.16)"

        card = "rgba(15, 23, 42, 0.40)"
        chip_bg = "rgba(255, 255, 255, 0.10)"
        chip_border = "rgba(255, 255, 255, 0.16)"
        shadow = "0 24px 80px rgba(0, 0, 0, 0.42)"

        blob3 = "rgba(255, 255, 255, 0.10)"
        blob4_hex = _blend_hex(accent2, "#0b1220", 0.35)
        r4, g4, b4 = _hex_to_rgb(blob4_hex)

        primary_weak = f"rgba({r1}, {g1}, {b1}, 0.18)"

        bg_img = (
            f"radial-gradient(980px 620px at 12% 10%, rgba({r1}, {g1}, {b1}, 0.14), transparent 60%),"
            f"radial-gradient(920px 560px at 88% 8%, rgba({r2}, {g2}, {b2}, 0.12), transparent 60%),"
            f"radial-gradient(820px 520px at 8% 92%, {blob3}, transparent 62%),"
            f"radial-gradient(760px 520px at 60% 55%, rgba({r4}, {g4}, {b4}, 0.14), transparent 62%),"
            f"linear-gradient(160deg, {bg1} 0%, {bg2} 45%, {bg1} 100%)"
        )

        blob4 = f"rgba({r4}, {g4}, {b4}, 0.16)"

    # 文字が埋もれないよう、最低限のコントラストはここで確保
    #（白テーマでも primary が白になって消える事故を避ける）
    q_primary = accent
    q_secondary = accent2

    bg_img_str = bg_img

    return (
        f"--q-primary: {q_primary};"
        f"--q-secondary: {q_secondary};"
        f"--pv-accent: {accent};"
        f"--pv-accent-2: {accent2};"
        f"--pv-primary: {accent};"
        f"--pv-primary-weak: {primary_weak};"
        f"--pv-text: {text};"
        f"--pv-muted: {muted};"
        f"--pv-border: {border};"
        f"--pv-line: {line};"
        f"--pv-card: {card};"
        f"--pv-chip-bg: {chip_bg};"
        f"--pv-chip-border: {chip_border};"
        f"--pv-shadow: {shadow};"
        f"--pv-blob4: {blob4};"
        f"--pv-bg-img: {bg_img_str};"
    )
def render_preview(p: dict, mode: str = "pc") -> None:
    """右側プレビュー（260218配置レイアウト）を描画する。

    p は「プロジェクト全体(dict)」または p["data"] 相当(dict) のどちらでも受け付ける。
    """
    # -------- data extraction (project dict / data dict 両対応) --------
    if isinstance(p, dict) and isinstance(p.get("data"), dict):
        d = p.get("data") or {}
    elif isinstance(p, dict):
        d = p
    else:
        d = {}

    step1 = d.get("step1", {}) if isinstance(d.get("step1"), dict) else {}
    step2 = d.get("step2", {}) if isinstance(d.get("step2"), dict) else {}
    blocks = d.get("blocks", {}) if isinstance(d.get("blocks"), dict) else {}

    # -------- theme --------
    primary_key = str(step1.get("primary_color") or "blue")
    is_dark = primary_key in ("black", "navy")
    root_id = f"pv-root-{mode}"
    theme_style = _preview_glass_style(step1, dark=is_dark)

    # -------- helpers --------
    SECTION_IDS = {
        "top": "pv-top",
        "news": "pv-news",
        "about": "pv-about",
        "services": "pv-services",
        "faq": "pv-faq",
        "access": "pv-access",
        "contact": "pv-contact",
    }

    def scroll_to(section_id: str) -> None:
        sid = SECTION_IDS.get(section_id, section_id)
        ui.run_javascript(f"window.cvhbPreviewScrollTo && window.cvhbPreviewScrollTo('{root_id}','{sid}')")

    def _clean(s: str, fallback: str = "") -> str:
        s = str(s or "").strip()
        return s if s else fallback

    # -------- content --------
    company_name = _clean(step2.get("company_name"), "会社名")
    favicon_url = _clean(step2.get("favicon_url"))
    catch_copy = _clean(step2.get("catch_copy"))
    phone = _clean(step2.get("phone"))
    email = _clean(step2.get("email"))
    address = _clean(step2.get("address"))

    hero = blocks.get("hero", {}) if isinstance(blocks.get("hero"), dict) else {}
    hero_image_choice = _clean(hero.get("hero_image"), "A: オフィス")
    sub_catch = _clean(hero.get("sub_catch"))

    # hero slider images (max 4)
    hero_urls = _safe_list(hero.get("hero_image_urls"))
    _legacy_hero_url = _clean(hero.get("hero_image_url"))
    if _legacy_hero_url:
        hero_urls = [_legacy_hero_url] + [u for u in hero_urls if _clean(u) and _clean(u) != _legacy_hero_url]
    hero_urls = [_clean(u) for u in hero_urls if _clean(u)]
    if not hero_urls:
        hero_urls = [_clean(HERO_IMAGE_PRESETS.get(hero_image_choice), HERO_IMAGE_DEFAULT)]
    hero_urls = hero_urls[:4]

    # CTA texts (legacy fields)
    primary_cta = _clean(hero.get("primary_button_text"), "お問い合わせ")
    secondary_cta = _clean(hero.get("secondary_button_text"), "見学・相談")

    news = blocks.get("news", {}) if isinstance(blocks.get("news"), dict) else {}
    news_items = _safe_list(news.get("items"))  # list[dict]

    philosophy = blocks.get("philosophy", {}) if isinstance(blocks.get("philosophy"), dict) else {}
    about_title = _clean(philosophy.get("title"), "私たちについて")
    about_body = _clean(philosophy.get("body"))
    about_points = _safe_list(philosophy.get("points"))

    about_image_url = _clean(
        philosophy.get("image_url"),
        # default: wood/forest vibe
        "https://images.unsplash.com/photo-1441974231531-c6227db76b6e?auto=format&fit=crop&w=1200&q=60",
    )

    services = philosophy.get("services") if isinstance(philosophy.get("services"), dict) else {}
    svc_title = _clean(services.get("title"), "業務内容")
    svc_lead = _clean(services.get("lead"))
    svc_image_url = _clean(
        services.get("image_url"),
        "https://images.unsplash.com/photo-1524758631624-e2822e304c36?auto=format&fit=crop&w=1200&q=60",
    )
    svc_items = _safe_list(services.get("items"))

    faq = blocks.get("faq", {}) if isinstance(blocks.get("faq"), dict) else {}
    faq_items = _safe_list(faq.get("items"))

    access = blocks.get("access", {}) if isinstance(blocks.get("access"), dict) else {}
    access_notes = _clean(access.get("notes"))
    map_url = _clean(access.get("map_url"))
    if not map_url and address:
        map_url = f"https://www.google.com/maps/search/?api=1&query={quote_plus(address)}"

    contact = blocks.get("contact", {}) if isinstance(blocks.get("contact"), dict) else {}
    contact_message = _clean(contact.get("message"))
    contact_hours = _clean(contact.get("hours"))
    contact_btn = _clean(contact.get("button_text"), "お問い合わせ")

    # -------- render --------
    dark_class = " pv-dark" if is_dark else ""

    with ui.element("div").classes(f"pv-shell pv-layout-260218 pv-mode-{mode}{dark_class}").props(f"id={root_id}").style(theme_style):
        # scroll container (header sticky)
        with ui.element("div").classes("pv-scroll"):
            # ----- header -----
            with ui.element("header").classes("pv-topbar pv-topbar-260218"):
                with ui.row().classes("pv-topbar-inner items-center justify-between no-wrap"):
                    # brand (favicon + name)
                    with ui.row().classes("items-center no-wrap pv-brand").on("click", lambda e: scroll_to("top")):
                        if favicon_url:
                            ui.image(favicon_url).classes("pv-favicon")
                        ui.label(company_name).classes("pv-brand-name")

                    # hamburger menu
                    # hamburger menu（先にdialogを作ってからボタンで開く）
                    with ui.dialog() as nav_dialog:
                        with ui.card().classes("pv-nav-card"):
                            ui.label("メニュー").classes("text-subtitle1 q-mb-sm")
                            for label, sec in [
                                ("トップ", "top"),
                                ("お知らせ", "news"),
                                ("私たちについて", "about"),
                                ("業務内容", "services"),
                                ("よくある質問", "faq"),
                                ("アクセス", "access"),
                                ("お問い合わせ", "contact"),
                            ]:
                                ui.button(
                                    label,
                                    on_click=lambda s=sec: (nav_dialog.close(), scroll_to(s)),
                                ).props("flat no-caps").classes("pv-nav-item w-full")
                    ui.button("MENU", icon="menu", on_click=nav_dialog.open).props("flat dense no-caps").classes("pv-menu-btn")
                    # menu opened by on_click

            # ----- main -----
            with ui.element("main").classes("pv-main"):
                # HERO
                with ui.element("section").classes("pv-section pv-hero").props('id="pv-top"'):
                    with ui.element("div").classes("pv-hero-grid"):
                        with ui.element("div").classes("pv-hero-copy"):
                            ui.label(_clean(catch_copy, company_name)).classes("pv-hero-title")
                            if sub_catch:
                                ui.label(sub_catch).classes("pv-hero-sub")
                            with ui.row().classes("pv-cta-row"):
                                ui.button(primary_cta, on_click=lambda: scroll_to("contact")).props(
                                    "no-caps unelevated color=primary"
                                ).classes("pv-btn pv-btn-primary")
                                ui.button(secondary_cta, on_click=lambda: scroll_to("contact")).props(
                                    "no-caps outline color=primary"
                                ).classes("pv-btn pv-btn-secondary")

                        with ui.element("div").classes("pv-hero-media"):
                            slider_id = f"pv-hero-slider-{mode}"
                            with ui.element("div").classes("pv-hero-slider").props(f'id="{slider_id}"'):
                                with ui.element("div").classes("pv-hero-track"):
                                    for url in hero_urls:
                                        with ui.element("div").classes("pv-hero-slide"):
                                            ui.image(url).classes("pv-hero-img")
                            # init slider (auto)
                            ui.run_javascript(
                                f"window.cvhbInitHeroSlider && window.cvhbInitHeroSlider('{slider_id}')"
                            )

                # NEWS
                with ui.element("section").classes("pv-section pv-news").props('id="pv-news"'):
                    with ui.element("div").classes("pv-section-head"):
                        ui.label("お知らせ").classes("pv-section-title")
                        ui.label("NEWS").classes("pv-section-en")
                    with ui.element("div").classes("pv-panel pv-panel-flat"):
                        with ui.element("div").classes("pv-news-list"):
                            shown = 0
                            for it in news_items:
                                if not isinstance(it, dict):
                                    continue
                                date = _clean(it.get("date"))
                                cat = _clean(it.get("category"), "お知らせ")
                                title = _clean(it.get("title"), "タイトル未設定")
                                shown += 1
                                with ui.element("div").classes("pv-news-item"):
                                    ui.label(date or "----.--.--").classes("pv-news-date")
                                    ui.label(cat).classes("pv-news-cat")
                                    ui.label(title).classes("pv-news-title")
                                if shown >= 4:
                                    break
                            if shown == 0:
                                ui.label("まだお知らせはありません。").classes("pv-muted q-mt-sm")
                        ui.button("お知らせ一覧").props("no-caps flat").classes("pv-link-btn")

                # ABOUT
                with ui.element("section").classes("pv-section pv-about").props('id="pv-about"'):
                    with ui.element("div").classes("pv-section-head"):
                        ui.label("私たちについて").classes("pv-section-title")
                        ui.label("ABOUT").classes("pv-section-en")
                    with ui.element("div").classes("pv-about-grid"):
                        with ui.element("div").classes("pv-panel pv-panel-glass"):
                            ui.label(about_title).classes("pv-h2")
                            if about_body:
                                ui.label(about_body).classes("pv-bodytext")
                            pts = [str(x).strip() for x in about_points if str(x).strip()]
                            if pts:
                                with ui.element("ul").classes("pv-bullets"):
                                    for t in pts[:6]:
                                        with ui.element("li"):
                                            ui.label(t)
                        with ui.element("div").classes("pv-about-media"):
                            ui.image(about_image_url).classes("pv-about-img")

                # SERVICES (integrated in philosophy block)
                with ui.element("section").classes("pv-section pv-services").props('id="pv-services"'):
                    with ui.element("div").classes("pv-section-head"):
                        ui.label(svc_title or "業務内容").classes("pv-section-title")
                        ui.label("SERVICES").classes("pv-section-en")
                    with ui.element("div").classes("pv-services-wrap pv-surface-white"):
                        with ui.element("div").classes("pv-services-grid"):
                            with ui.element("div").classes("pv-services-media"):
                                ui.image(svc_image_url).classes("pv-services-img")
                            with ui.element("div").classes("pv-services-copy"):
                                if svc_lead:
                                    ui.label(svc_lead).classes("pv-bodytext")
                                # items
                                cleaned_items = []
                                for it in svc_items:
                                    if isinstance(it, dict):
                                        t = _clean(it.get("title"))
                                        b = _clean(it.get("body"))
                                        if t or b:
                                            cleaned_items.append((t, b))
                                if cleaned_items:
                                    for (t, b) in cleaned_items[:6]:
                                        with ui.element("div").classes("pv-service-item"):
                                            ui.label(t or "項目").classes("pv-service-title")
                                            if b:
                                                ui.label(b).classes("pv-muted")
                                else:
                                    ui.label("業務内容の項目を設定してください。").classes("pv-muted")

                # FAQ
                with ui.element("section").classes("pv-section pv-faq").props('id="pv-faq"'):
                    with ui.element("div").classes("pv-section-head"):
                        ui.label("よくある質問").classes("pv-section-title")
                        ui.label("FAQ").classes("pv-section-en")
                    with ui.element("div").classes("pv-panel pv-panel-glass"):
                        shown = 0
                        for it in faq_items:
                            if not isinstance(it, dict):
                                continue
                            q = _clean(it.get("q"))
                            a = _clean(it.get("a"))
                            if not q and not a:
                                continue
                            shown += 1
                            with ui.element("div").classes("pv-faq-item"):
                                ui.label(q or "質問").classes("pv-faq-q")
                                if a:
                                    ui.label(a).classes("pv-faq-a")
                            if shown >= 4:
                                break
                        if shown == 0:
                            ui.label("よくある質問は準備中です。").classes("pv-muted")

                # ACCESS
                with ui.element("section").classes("pv-section pv-access").props('id="pv-access"'):
                    with ui.element("div").classes("pv-section-head"):
                        ui.label("アクセス").classes("pv-section-title")
                        ui.label("ACCESS").classes("pv-section-en")
                    with ui.element("div").classes("pv-panel pv-panel-glass pv-access-card"):
                        if address:
                            ui.label(address).classes("pv-bodytext")
                        else:
                            ui.label("住所を入力してください（基本情報 > 住所）").classes("pv-muted")
                        if access_notes:
                            ui.label(access_notes).classes("pv-muted q-mt-sm")
                        if map_url:
                            ui.button("地図を開く").props(
                                f'no-caps unelevated color=primary type=a href="{map_url}" target="_blank"'
                            ).classes("pv-btn pv-map-btn")

                # CONTACT
                with ui.element("section").classes("pv-section pv-contact").props('id="pv-contact"'):
                    with ui.element("div").classes("pv-section-head"):
                        ui.label("お問い合わせ").classes("pv-section-title")
                        ui.label("CONTACT").classes("pv-section-en")
                    with ui.element("div").classes("pv-panel pv-panel-glass pv-contact-card"):
                        if contact_message:
                            ui.label(contact_message).classes("pv-bodytext")
                        if contact_hours:
                            ui.label(contact_hours).classes("pv-muted q-mt-sm")
                        with ui.row().classes("pv-contact-actions"):
                            if phone:
                                ui.button("電話する").props(
                                    f'no-caps outline color=primary type=a href="tel:{phone}"'
                                ).classes("pv-btn pv-btn-secondary")
                            if email:
                                ui.button("メール").props(
                                    f'no-caps outline color=primary type=a href="mailto:{email}"'
                                ).classes("pv-btn pv-btn-secondary")
                            ui.button(contact_btn, on_click=lambda: None).props("no-caps unelevated color=primary").classes(
                                "pv-btn pv-btn-primary"
                            )

                # PRE-FOOTER (company mini)
                with ui.element("section").classes("pv-companybar"):
                    with ui.element("div").classes("pv-companybar-inner"):
                        with ui.element("div").classes("pv-companybar-left"):
                            ui.label(company_name).classes("pv-companybar-name")
                            if address:
                                ui.label(address).classes("pv-companybar-meta")
                            if phone:
                                ui.label(f"TEL: {phone}").classes("pv-companybar-meta")
                        with ui.element("div").classes("pv-companybar-right"):
                            ui.button("お問い合わせへ", on_click=lambda: scroll_to("contact")).props("no-caps unelevated color=primary").classes("pv-btn pv-btn-primary")

                # FOOTER
                with ui.element("footer").classes("pv-footer"):
                    with ui.element("div").classes("pv-footer-grid"):
                        with ui.element("div"):
                            ui.label(company_name).classes("pv-footer-brand")
                            for label, sec in [
                                ("トップ", "top"),
                                ("お知らせ", "news"),
                                ("私たちについて", "about"),
                                ("業務内容", "services"),
                                ("よくある質問", "faq"),
                                ("アクセス", "access"),
                                ("お問い合わせ", "contact"),
                            ]:
                                ui.button(label, on_click=lambda s=sec: scroll_to(s)).props("flat no-caps").classes("pv-footer-link text-white")
                        with ui.element("div"):
                            ui.label("連絡先").classes("pv-footer-cap")
                            if address:
                                ui.label(address).classes("pv-footer-text")
                            if phone:
                                ui.label(f"TEL: {phone}").classes("pv-footer-text")
                            if email:
                                ui.label(f"MAIL: {email}").classes("pv-footer-text")
                    ui.label(f"© {datetime.now().year} {company_name}. All rights reserved.").classes("pv-footer-copy")
def render_main(u: User) -> None:
    inject_global_styles()
    cleanup_user_storage()

    render_header(u)

    p = get_current_project(u)

    preview_ref = {"refresh_mobile": (lambda: None), "refresh_pc": (lambda: None)}

    editor_ref = {"refresh": (lambda: None)}

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
                                    before_tpl = step1.get("_applied_template_id") or step1.get("template_id") or ""
                                    step1["template_id"] = resolve_template_id(step1)
                                    set_current_project(p, u)
                                    after_tpl = step1.get("_applied_template_id") or step1.get("template_id") or ""
                                    # 業種（テンプレ）を切り替えた瞬間だけ、Step3の入力欄も描き直す
                                    if after_tpl != before_tpl:
                                        try:
                                            editor_ref["refresh"]()
                                        except Exception:
                                            pass
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
                                            bind_step2_input("ファビコンURL（任意）", "favicon_url", hint="ブラウザタブのアイコン用（32x32推奨）")
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

                                        @ui.refreshable
                                        def block_editor_panel():
                                            with ui.card().classes("q-pa-sm rounded-borders w-full").props("flat bordered"):
                                                ui.label("ブロック編集（6ブロック）").classes("text-subtitle1")
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
                                                        # スライド画像URL（最大4枚 / 任意）
                                                        urls = _safe_list(hero.get("hero_image_urls"))
                                                        # 旧: hero_image_url は 1枚目として扱う
                                                        _legacy = str(hero.get("hero_image_url") or "").strip()
                                                        if _legacy:
                                                            urls = [_legacy] + [u for u in urls if str(u).strip() and str(u).strip() != _legacy]
                                                        while len(urls) < 4:
                                                            urls.append("")
                                                        hero["hero_image_urls"] = urls[:4]
                                                        ui.label("スライド画像URL（最大4枚 / 任意）").classes("cvhb-muted q-mt-sm")
                                                        for _i in range(4):
                                                            def _on_url_change(e, i=_i):
                                                                uu = _safe_list(hero.get("hero_image_urls"))
                                                                while len(uu) < 4:
                                                                    uu.append("")
                                                                uu[i] = str(e.value or "").strip()
                                                                hero["hero_image_urls"] = uu[:4]
                                                                # legacy: 1枚目を hero_image_url にも反映
                                                                hero["hero_image_url"] = hero["hero_image_urls"][0] if hero.get("hero_image_urls") else ""
                                                                update_and_refresh()
                                                            ui.input(f"画像URL { _i + 1 }", value=hero["hero_image_urls"][_i], on_change=_on_url_change).props("outlined dense").classes("w-full q-mb-sm")
                                                        bind_block_input("hero", "サブキャッチ（任意）", "sub_catch")
                                                        bind_block_input("hero", "ボタン1の文言", "primary_button_text")
                                                        bind_block_input("hero", "ボタン2の文言（任意）", "secondary_button_text")

                                                    with ui.tab_panel("philosophy"):
                                                        ui.label("理念 / 会社概要").classes("text-subtitle1 q-mb-sm")
                                                        bind_block_input("philosophy", "見出し", "title")
                                                        bind_block_input("philosophy", "本文", "body", textarea=True)
                                                        bind_block_input("philosophy", "画像URL（任意）", "image_url", hint="未入力ならデフォルト画像")

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

                                                        ui.separator().classes("q-my-md")
                                                        ui.label("業務内容（プレビューに表示）").classes("text-subtitle2 q-mb-sm")
                                                        svc = blocks.setdefault("philosophy", {}).setdefault("services", {})
                                                        if not isinstance(svc, dict):
                                                            svc = {}
                                                            blocks["philosophy"]["services"] = svc
                                                        svc.setdefault("title", "業務内容")
                                                        svc.setdefault("lead", "提供サービスの概要をここに記載します。")
                                                        svc.setdefault("image_url", "")
                                                        items = svc.setdefault("items", [])
                                                        if not isinstance(items, list):
                                                            items = []
                                                            svc["items"] = items
                                                        while len(items) < 3:
                                                            items.append({"title": "", "body": ""})
                                                        svc["items"] = items[:3]
                                                        ui.input("セクション見出し", value=svc.get("title", ""), on_change=lambda e: (svc.__setitem__("title", e.value or ""), update_and_refresh())).props("outlined").classes("w-full q-mb-sm")
                                                        ui.input("導入文", value=svc.get("lead", ""), on_change=lambda e: (svc.__setitem__("lead", e.value or ""), update_and_refresh())).props("outlined type=textarea autogrow").classes("w-full q-mb-sm")
                                                        ui.input("画像URL（任意 / 1枚）", value=svc.get("image_url", ""), on_change=lambda e: (svc.__setitem__("image_url", e.value or ""), update_and_refresh())).props("outlined").classes("w-full q-mb-sm")
                                                        ui.label("項目（3つまで）").classes("cvhb-muted q-mt-sm")
                                                        for i in range(3):
                                                            it = svc["items"][i]
                                                            if not isinstance(it, dict):
                                                                it = {"title": "", "body": ""}
                                                                svc["items"][i] = it
                                                            ui.input(
                                                                f"項目{i+1} タイトル",
                                                                value=it.get("title", ""),
                                                                on_change=lambda e, idx=i: (svc["items"][idx].__setitem__("title", e.value or ""), update_and_refresh()),
                                                            ).props("outlined").classes("w-full q-mb-sm")
                                                            ui.input(
                                                                f"項目{i+1} 本文",
                                                                value=it.get("body", ""),
                                                                on_change=lambda e, idx=i: (svc["items"][idx].__setitem__("body", e.value or ""), update_and_refresh()),
                                                            ).props("outlined type=textarea autogrow").classes("w-full q-mb-sm")
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

                                        editor_ref["refresh"] = block_editor_panel.refresh
                                        block_editor_panel()


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
                                    "width: min(100%, 800px); height: clamp(720px, 86vh, 980px); overflow: hidden; border-radius: 22px; margin: 0 auto;"
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
                                    "width: min(100%, 1200px); height: clamp(720px, 86vh, 980px); overflow: hidden; border-radius: 14px; margin: 0 auto;"
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