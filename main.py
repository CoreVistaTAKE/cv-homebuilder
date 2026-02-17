
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
/* プレビューは「商用に耐える見た目」を目標に、ガラス+余白+軽い動きを基本にします。
   ※ builder側の見た目は変えません（.cvhb-preview-glass の中だけに効くCSSです） */

.cvhb-preview-glass {
  background-image: var(--pv-bg-img, linear-gradient(135deg, var(--pv-bg1, #f8fafc), var(--pv-bg2, #eef2ff)));
          background-size: cover;
          background-position: center;
  color: var(--pv-text, #0b1220);
  font-family: ui-sans-serif, system-ui, -apple-system, "Segoe UI", "Noto Sans JP", "Hiragino Kaku Gothic ProN", Meiryo, sans-serif;
  letter-spacing: 0.01em;
  scroll-behavior: smooth;
}

.cvhb-preview-glass a { color: inherit; text-decoration: none; }

.cvhb-preview-glass .pv-topbar {
  position: sticky;
  top: 0;
  z-index: 20;
  background: var(--pv-topbar-bg, rgba(255,255,255,.58));
  border-bottom: 1px solid var(--pv-topbar-border, rgba(255,255,255,.70));
  backdrop-filter: blur(14px);
}

.cvhb-preview-glass .pv-topbar-inner {
  padding: 12px 14px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}

.cvhb-preview-glass .pv-topbar-title { font-weight: 800; }

.cvhb-preview-glass .pv-section { padding: 16px 16px 0 16px; }

.cvhb-preview-glass .pv-card {
  background: var(--pv-card-bg, rgba(255,255,255,.72));
  border: 1px solid var(--pv-card-border, rgba(255,255,255,.64));
  border-radius: 16px;
  box-shadow: var(--pv-card-shadow, 0 16px 40px rgba(15,23,42,.12));
  overflow: hidden;
}

.cvhb-preview-glass .pv-card.pv-card-pad { padding: 14px; }

.cvhb-preview-glass .pv-muted { color: var(--pv-muted, rgba(15,23,42,.62)); }

.cvhb-preview-glass .pv-pill {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 6px 10px;
  border-radius: 999px;
  border: 1px solid var(--pv-pill-border, rgba(15,23,42,.10));
  background: var(--pv-pill-bg, rgba(255,255,255,.55));
  font-size: 12px;
}

.cvhb-preview-glass .pv-hero {
  position: relative;
  border-radius: 18px;
  overflow: hidden;
  min-height: 240px;
  box-shadow: 0 18px 44px rgba(0,0,0,.18);
}

.cvhb-preview-glass .pv-hero-bg {
  position: absolute;
  inset: 0;
  background-size: cover;
  background-position: center;
  transform: scale(1.02);
}

.cvhb-preview-glass .pv-hero-overlay {
  position: absolute;
  inset: 0;
  background: var(--pv-hero-overlay, linear-gradient(180deg, rgba(0,0,0,.24), rgba(0,0,0,.62)));
}

.cvhb-preview-glass .pv-hero-inner {
  position: relative;
  padding: 18px;
  min-height: 240px;
  display: flex;
  flex-direction: column;
  justify-content: flex-end;
  gap: 10px;
  color: #ffffff;
}

.cvhb-preview-glass .pv-hero-title {
  font-size: 24px;
  font-weight: 900;
  line-height: 1.2;
  text-shadow: 0 6px 20px rgba(0,0,0,.35);
}

.cvhb-preview-glass .pv-hero-sub {
  font-size: 13px;
  line-height: 1.45;
  opacity: .95;
  text-shadow: 0 6px 20px rgba(0,0,0,.35);
}

.cvhb-preview-glass .pv-btn-primary {
  background: var(--pv-accent, #1976d2) !important;
  color: var(--pv-accent-contrast, #ffffff) !important;
  border-radius: 999px !important;
  padding: 8px 14px !important;
  font-weight: 800 !important;
}

.cvhb-preview-glass .pv-btn-secondary {
  background: var(--pv-secondary-bg, rgba(255,255,255,.34)) !important;
  color: #ffffff !important;
  border: 1px solid var(--pv-secondary-border, rgba(255,255,255,.78)) !important;
  border-radius: 999px !important;
  padding: 8px 14px !important;
  font-weight: 800 !important;
}

.cvhb-preview-glass .pv-linkbtn {
  font-weight: 700;
  color: var(--pv-text, #0b1220);
  opacity: .92;
}

.cvhb-preview-glass .pv-divider { border-top: 1px solid var(--pv-line, rgba(15,23,42,.10)); margin: 12px 0; }
.cvhb-preview-glass .pv-kv { display: grid; grid-template-columns: 1fr; gap: 8px; }
.cvhb-preview-glass .pv-kv .pv-k { font-size: 12px; color: var(--pv-muted, rgba(15,23,42,.62)); }
.cvhb-preview-glass .pv-kv .pv-v { font-weight: 700; }

.cvhb-preview-glass .pv-section-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-weight: 900;
  margin: 0 0 10px 0;
}

.cvhb-preview-glass .pv-footer {
  margin-top: 18px;
  padding: 16px;
  color: var(--pv-muted, rgba(15,23,42,.62));
}

.cvhb-preview-glass .pv-footer-inner {
  border-top: 1px solid var(--pv-line, rgba(15,23,42,.10));
  padding-top: 12px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  flex-wrap: wrap;
}



        /* ====== Preview: layout rhythm & hero polish (v0.6.6) ====== */
        .cvhb-preview-glass .pv-scroll {
          flex: 1;
          overflow: auto;
          padding: 0;
        }

        .cvhb-preview-glass .pv-section {
          padding: 18px 16px;
        }

        .cvhb-preview-glass .pv-section.pv-section-tight {
          padding-top: 12px;
          padding-bottom: 12px;
        }

        .cvhb-preview-glass .pv-section.pv-section-alt {
          background: var(--pv-band, rgba(255,255,255,.22));
          border-top: 1px solid var(--pv-line, rgba(15,23,42,.10));
          border-bottom: 1px solid var(--pv-line, rgba(15,23,42,.10));
        }

        .cvhb-preview-glass .pv-hero-bg {
          will-change: transform;
          transition: transform 1400ms ease;
        }

        .cvhb-preview-glass .pv-hero:hover .pv-hero-bg {
          transform: scale(1.06);
        }

        .cvhb-preview-glass .pv-hero.pv-hero-lg {
          min-height: 420px;
          border-radius: 24px;
        }

        .cvhb-preview-glass .pv-hero-inner.pv-hero-inner-lg {
          padding: 34px;
        }

        .cvhb-preview-glass .pv-hero-grid {
          display: grid;
          gap: 22px;
          grid-template-columns: 1.15fr 0.85fr;
          align-items: end;
        }

        @media (max-width: 900px) {
          .cvhb-preview-glass .pv-hero-grid { grid-template-columns: 1fr; }
          .cvhb-preview-glass .pv-hero.pv-hero-lg { min-height: 320px; }
          .cvhb-preview-glass .pv-hero-inner.pv-hero-inner-lg { padding: 22px; }
        }

        .cvhb-preview-glass .pv-prefooter {
          border-radius: 18px;
          padding: 14px 16px;
          background: var(--pv-card, rgba(255,255,255,.55));
          border: 1px solid var(--pv-border, rgba(255,255,255,.45));
          backdrop-filter: blur(16px);
          box-shadow: 0 18px 40px rgba(2,6,23,.10);
        }

        .cvhb-preview-glass .pv-prefooter .pv-prefooter-title {
          font-weight: 800;
          letter-spacing: .2px;
        }

        .cvhb-preview-glass .pv-prefooter .pv-prefooter-meta {
          font-size: 12px;
          color: var(--pv-muted, rgba(15,23,42,.62));
        }

        .cvhb-preview-glass .pv-btn-primary,
        .cvhb-preview-glass .pv-btn-secondary {
          transition: transform 160ms ease, box-shadow 160ms ease, filter 160ms ease;
        }

        .cvhb-preview-glass .pv-btn-primary:hover,
        .cvhb-preview-glass .pv-btn-secondary:hover {
          transform: translateY(-1px);
          filter: brightness(1.02);
        }

        
        
        .cvhb-preview-glass .pv-newsitem,
        .cvhb-preview-glass .pv-faqitem {
          padding: 12px 0;
          border-top: 1px solid var(--pv-line, rgba(15,23,42,.10));
        }

        .cvhb-preview-glass .pv-newsitem:first-child,
        .cvhb-preview-glass .pv-faqitem:first-child {
          border-top: none;
          padding-top: 0;
        }

.cvhb-preview-glass .pv-navbtn {
          border-radius: 999px;
          font-weight: 800;
          letter-spacing: .2px;
          color: var(--pv-text);
        }

        .cvhb-preview-glass .pv-navbtn:hover {
          background: var(--pv-band, rgba(255,255,255,.22));
        }

@media (min-width: 900px) {
          .cvhb-preview-glass { background-attachment: fixed; }
        }

/* ====== Preview: micro interactions (軽い動き) ====== */
@keyframes pv_fade_up {
  from { opacity: 0; transform: translateY(10px); }
  to { opacity: 1; transform: translateY(0); }
}

.cvhb-preview-glass .pv-animate { animation: pv_fade_up 650ms cubic-bezier(.2,.8,.2,1) both; }
.cvhb-preview-glass .pv-delay-1 { animation-delay: 80ms; }
.cvhb-preview-glass .pv-delay-2 { animation-delay: 140ms; }
.cvhb-preview-glass .pv-delay-3 { animation-delay: 220ms; }

.cvhb-preview-glass .pv-card { transition: transform .18s ease, box-shadow .18s ease; }
.cvhb-preview-glass .pv-card:hover { transform: translateY(-2px); }

.cvhb-preview-glass .pv-btn-primary,
.cvhb-preview-glass .pv-btn-secondary { transition: transform .12s ease, filter .12s ease; }

.cvhb-preview-glass .pv-btn-primary:hover,
.cvhb-preview-glass .pv-btn-secondary:hover { transform: translateY(-1px); filter: brightness(1.04); }

/* ====== Preview: PC layout tweaks ====== */
.cvhb-preview-glass.cvhb-preview-pc .cvhb-pc-header {
  position: sticky;
  top: 0;
  z-index: 30;
  background: var(--pv-topbar-bg, rgba(255,255,255,.58));
  border-bottom: 1px solid var(--pv-topbar-border, rgba(255,255,255,.70));
  backdrop-filter: blur(14px);
}

.cvhb-preview-glass.cvhb-preview-pc .cvhb-pc-container { max-width: 1200px; padding: 0 28px; }
.cvhb-preview-glass.cvhb-preview-pc .cvhb-pc-hero { background: transparent; }
.cvhb-preview-glass.cvhb-preview-pc .cvhb-pc-hero-overlay {
  background: var(--pv-hero-overlay, linear-gradient(180deg, rgba(0,0,0,.24), rgba(0,0,0,.62)));
}

/* ====== Preview tabs icon spacing ====== */
.cvhb-preview-tabs .q-tab__icon { margin-right: 6px; }
</style>
"""
    )
# =========================
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


def _preview_glass_style(step1: dict) -> str:
    """Return inline CSS variables for the preview glass theme."""

    primary_color = (step1.get("primary_color") or "blue").strip() or "blue"
    theme = "dark" if primary_color in ("black", "grey") else "light"
    accent = _preview_accent_hex(primary_color)

    def _hex_to_rgb(h: str) -> tuple[int, int, int]:
        h = (h or "").lstrip("#")
        if len(h) != 6:
            return (0, 0, 0)
        try:
            return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
        except Exception:
            return (0, 0, 0)

    r, g, b = _hex_to_rgb(accent)
    blob1_a = 0.18 if theme == "light" else 0.16
    blob2_a = 0.10 if theme == "light" else 0.10
    blob3_a = 0.08 if theme == "light" else 0.09

    base = {
        "--pv-accent": accent,
        "--pv-bg1": "#f8fafc",
        "--pv-bg2": "#eef2ff",
        "--pv-text": "#0f172a",
        "--pv-muted": "rgba(15,23,42,.62)",
        "--pv-card": "rgba(255,255,255,.55)",
        "--pv-border": "rgba(255,255,255,.45)",
        "--pv-line": "rgba(15,23,42,.10)",
        "--pv-chip-bg": "rgba(255,255,255,.65)",
        "--pv-chip-border": "rgba(255,255,255,.55)",
        "--pv-blob1": f"rgba({r},{g},{b},{blob1_a})",
        "--pv-blob2": f"rgba({r},{g},{b},{blob2_a})",
        "--pv-blob3": f"rgba({r},{g},{b},{blob3_a})",
        "--pv-band": "rgba(255,255,255,.22)",
    }

    if theme == "dark":
        base.update(
            {
                "--pv-bg1": "#0b1220",
                "--pv-bg2": "#0f172a",
                "--pv-text": "#e2e8f0",
                "--pv-muted": "rgba(226,232,240,.72)",
                "--pv-card": "rgba(15,23,42,.62)",
                "--pv-border": "rgba(148,163,184,.18)",
                "--pv-line": "rgba(148,163,184,.14)",
                "--pv-chip-bg": "rgba(15,23,42,.52)",
                "--pv-chip-border": "rgba(148,163,184,.18)",
                "--pv-band": "rgba(15,23,42,.35)",
            }
        )

    # 背景“画像”＝レイヤーグラデ（外部画像に依存せず、ガラス表現が映える）
    base["--pv-bg-img"] = (
        "radial-gradient(900px 700px at 12% 10%, var(--pv-blob1), transparent 60%),"
        "radial-gradient(1000px 760px at 92% 28%, var(--pv-blob2), transparent 55%),"
        "radial-gradient(900px 760px at 30% 95%, var(--pv-blob3), transparent 60%),"
        "linear-gradient(135deg, var(--pv-bg1), var(--pv-bg2))"
    )

    # Quasar primary (NiceGUI button color etc.)
    base["--q-primary"] = accent

    return ";".join([f"{k}:{v}" for k, v in base.items()]) + ";"

def render_preview(p: dict, mode: str = "sp") -> None:
    """Render preview (SP/PC). Design target: commercial-grade glassmorphism base."""

    step1 = p.get("step1", {}) if isinstance(p, dict) else {}
    blocks = p.get("blocks", {}) if isinstance(p, dict) else {}

    company = (step1.get("company_name") or "").strip() or "会社名"
    catch = (step1.get("catch_copy") or "").strip() or "スタッフ・利用者の笑顔を守る企業"
    phone = (step1.get("phone") or "").strip()
    email = (step1.get("email") or "").strip()
    addr = (step1.get("address") or "").strip()
    industry = (step1.get("industry") or "").strip()

    hero = blocks.get("hero", {}) if isinstance(blocks, dict) else {}
    hero_choice = (hero.get("hero_image") or "A: オフィス").strip() or "A: オフィス"
    hero_img_url = (hero.get("hero_image_url") or "").strip() or HERO_IMAGE_PRESETS.get(hero_choice, HERO_IMAGE_DEFAULT)

    # sub catch: hero block > step1
    subcatch = (hero.get("sub_catch") or step1.get("sub_catch") or "").strip()

    primary_btn = (hero.get("primary_button_text") or "").strip() or "お問い合わせ"
    secondary_btn = (hero.get("secondary_button_text") or "").strip() or "見学・相談"

    philosophy = blocks.get("philosophy", {}) if isinstance(blocks, dict) else {}
    ph_text = (philosophy.get("text") or "").strip() or "ここに理念や会社の紹介文を書きます。（あとで自由に書き換えできます）"
    ph_features = philosophy.get("features") or ["地域密着", "丁寧な対応", "安心の体制"]
    if not isinstance(ph_features, list):
        ph_features = ["地域密着", "丁寧な対応", "安心の体制"]

    news = blocks.get("news", {}) if isinstance(blocks, dict) else {}
    news_items = news.get("items") or []
    if not isinstance(news_items, list):
        news_items = []

    faq = blocks.get("faq", {}) if isinstance(blocks, dict) else {}
    faq_items = faq.get("items") or []
    if not isinstance(faq_items, list):
        faq_items = []

    access = blocks.get("access", {}) if isinstance(blocks, dict) else {}
    map_url = (access.get("map_url") or "").strip() or "https://maps.google.com/"
    access_note = (access.get("note") or "").strip() or "（例）○○駅から徒歩5分 / 駐車場あり"

    contact = blocks.get("contact", {}) if isinstance(blocks, dict) else {}
    hours = (contact.get("hours") or "").strip() or "平日 9:00〜18:00"
    cta_text = (contact.get("cta_text") or "").strip() or "まずはお気軽にご相談ください。"

    style = _preview_glass_style(step1)

    def _chip(text_: str) -> None:
        ui.label(text_).classes("pv-chip")

    def _news_card(items: list, max_items: int = 3) -> None:
        shown = 0
        for it in items:
            if shown >= max_items:
                break
            if not isinstance(it, dict):
                continue
            title = (it.get("title") or "").strip()
            body = (it.get("body") or "").strip()
            date = (it.get("date") or "").strip()
            category = (it.get("category") or "").strip()
            if not title and not body:
                continue
            shown += 1

            with ui.element("div").classes("pv-newsitem"):
                with ui.row().classes("items-center justify-between"):
                    ui.label(title or "お知らせ").classes("text-body1 text-weight-bold")
                    if date:
                        ui.label(date).classes("pv-muted").style("font-size: 12px;")
                if category:
                    ui.label(category).classes("pv-pill q-mt-xs")
                if body:
                    ui.label(body).classes("pv-muted q-mt-sm").style("white-space: pre-wrap;")

        if shown == 0:
            ui.label("まだお知らせはありません。").classes("pv-muted")

    def _faq_card(items: list, max_items: int = 3) -> None:
        shown = 0
        for it in items:
            if shown >= max_items:
                break
            if not isinstance(it, dict):
                continue
            q = (it.get("q") or "").strip()
            a = (it.get("a") or "").strip()
            if not q and not a:
                continue
            shown += 1
            with ui.element("div").classes("pv-faqitem"):
                ui.label(q or "質問").classes("text-body1 text-weight-bold")
                if a:
                    ui.label(a).classes("pv-muted q-mt-sm").style("white-space: pre-wrap;")

        if shown == 0:
            ui.label("まだFAQはありません。").classes("pv-muted")

    # =========================
    # SP (Smartphone)
    # =========================
    if mode == "sp":
        with ui.element("div").classes("w-full cvhb-preview cvhb-preview-sp cvhb-preview-glass").style(
            style + "height: 100%; display:flex; flex-direction: column;"
        ):
            # topbar
            with ui.element("div").classes("pv-topbar"):
                with ui.row().classes("items-center justify-between pv-topbar-inner"):
                    ui.label(company).classes("pv-topbar-title")
                    ui.icon("menu").classes("pv-topbar-menu")

            # scroll body
            with ui.element("div").classes("pv-scroll"):
                # hero
                with ui.element("div").classes("pv-section pv-section-tight pv-animate pv-delay-1"):
                    with ui.element("div").classes("pv-hero"):
                        ui.element("div").classes("pv-hero-bg").style(f"background-image: url('{hero_img_url}');")
                        ui.element("div").classes("pv-hero-overlay")
                        with ui.element("div").classes("pv-hero-inner"):
                            if industry:
                                ui.label(industry).classes("pv-pill")
                            ui.label(catch).classes("pv-hero-title")
                            if subcatch:
                                ui.label(subcatch).classes("pv-hero-sub")
                            with ui.row().classes("q-gutter-sm q-mt-md items-center"):
                                ui.button(primary_btn).props("unelevated").classes("pv-btn-primary")
                                ui.button(secondary_btn).props("outline").classes("pv-btn-secondary")

                    # hero subcard (軽い説明 + 特徴チップ)
                    with ui.element("div").classes("pv-card pv-card-pad pv-animate pv-delay-2 q-mt-md"):
                        ui.label(company).classes("pv-section-title")
                        ui.label(ph_text).classes("pv-muted q-mt-sm").style("white-space: pre-wrap;")
                        with ui.row().classes("q-gutter-xs q-mt-md"):
                            for f in ph_features[:3]:
                                _chip(str(f))

                # philosophy
                with ui.element("div").classes("pv-section pv-section-alt pv-animate pv-delay-3"):
                    with ui.element("div").classes("pv-card pv-card-pad"):
                        ui.label("私たちの想い").classes("pv-section-title")
                        ui.label(ph_text).classes("pv-muted q-mt-sm").style("white-space: pre-wrap;")
                        with ui.row().classes("q-gutter-xs q-mt-md"):
                            for f in ph_features[:6]:
                                _chip(str(f))

                # news
                with ui.element("div").classes("pv-section pv-animate pv-delay-4"):
                    with ui.element("div").classes("pv-card pv-card-pad"):
                        ui.label("お知らせ").classes("pv-section-title")
                        _news_card(news_items, max_items=3)

                # faq
                with ui.element("div").classes("pv-section pv-section-alt pv-animate pv-delay-5"):
                    with ui.element("div").classes("pv-card pv-card-pad"):
                        ui.label("よくある質問").classes("pv-section-title")
                        _faq_card(faq_items, max_items=3)

                # access
                with ui.element("div").classes("pv-section pv-animate pv-delay-5"):
                    with ui.element("div").classes("pv-card pv-card-pad"):
                        ui.label("アクセス").classes("pv-section-title")
                        if addr:
                            ui.label(f"住所：{addr}").classes("pv-muted q-mt-sm").style("white-space: pre-wrap;")
                        ui.label(access_note).classes("pv-muted q-mt-sm").style("white-space: pre-wrap;")
                        ui.button("地図を開く").props("unelevated").classes("pv-btn-primary q-mt-md w-full").on(
                            "click", lambda: None
                        )

                # contact
                with ui.element("div").classes("pv-section pv-section-alt pv-animate pv-delay-5"):
                    with ui.element("div").classes("pv-card pv-card-pad"):
                        ui.label("お問い合わせ").classes("pv-section-title")
                        ui.label(cta_text).classes("pv-muted q-mt-sm").style("white-space: pre-wrap;")
                        if phone:
                            ui.label(f"TEL：{phone}").classes("text-body2 q-mt-md")
                        ui.label(hours).classes("pv-muted q-mt-xs")
                        if email:
                            ui.label(f"Email：{email}").classes("pv-muted q-mt-xs")
                        ui.button(primary_btn).props("unelevated").classes("pv-btn-primary q-mt-md w-full").on(
                            "click", lambda: None
                        )

                # pre-footer company mini
                with ui.element("div").classes("pv-section pv-animate pv-delay-5"):
                    with ui.element("div").classes("pv-prefooter"):
                        with ui.column().classes("q-gutter-xs"):
                            ui.label(company).classes("pv-prefooter-title")
                            if industry:
                                ui.label(industry).classes("pv-prefooter-meta")
                            if addr:
                                ui.label(addr).classes("pv-prefooter-meta").style("white-space: pre-wrap;")
                        with ui.row().classes("items-center q-gutter-sm"):
                            if phone:
                                ui.label(f"TEL：{phone}").classes("pv-prefooter-meta")
                            if email:
                                ui.label(email).classes("pv-prefooter-meta")

                # footer
                with ui.element("div").classes("pv-footer"):
                    ui.label("© CoreVistaJP / CV-HomeBuilder").classes("pv-muted")

        return

    # =========================
    # PC
    # =========================
    with ui.element("div").classes("w-full cvhb-preview cvhb-preview-pc cvhb-preview-glass").style(style):
        # header
        with ui.element("header").classes("w-full cvhb-pc-header"):
            with ui.element("div").classes("cvhb-pc-container"):
                with ui.row().classes("items-center justify-between q-py-sm"):
                    ui.label(company).classes("text-h6 text-weight-bold")
                    with ui.row().classes("items-center q-gutter-sm"):
                        for nav in ["想い", "お知らせ", "FAQ", "アクセス"]:
                            ui.button(nav).props("flat dense").classes("pv-navbtn")
                        ui.button("お問い合わせ").props("unelevated").classes("pv-btn-primary").style("height: 36px;")

        # hero
        with ui.element("div").classes("pv-section pv-section-tight pv-animate pv-delay-1"):
            with ui.element("div").classes("cvhb-pc-container"):
                with ui.element("div").classes("pv-hero pv-hero-lg"):
                    ui.element("div").classes("pv-hero-bg").style(f"background-image: url('{hero_img_url}');")
                    ui.element("div").classes("pv-hero-overlay")
                    with ui.element("div").classes("pv-hero-inner pv-hero-inner-lg"):
                        with ui.element("div").classes("pv-hero-grid"):
                            # left: message + CTA
                            with ui.column().classes("q-gutter-sm"):
                                if industry:
                                    ui.label(industry).classes("pv-pill")
                                ui.label(catch).classes("pv-hero-title")
                                if subcatch:
                                    ui.label(subcatch).classes("pv-hero-sub")
                                with ui.row().classes("q-gutter-sm q-mt-sm items-center"):
                                    ui.button(primary_btn).props("unelevated").classes("pv-btn-primary")
                                    ui.button(secondary_btn).props("outline").classes("pv-btn-secondary")
                                if phone:
                                    ui.label(f"TEL：{phone}（{hours}）").classes("pv-muted q-mt-sm")
                            # right: glass summary
                            with ui.element("div").classes("pv-card pv-card-pad"):
                                ui.label("概要").classes("pv-section-title")
                                ui.label(ph_text).classes("pv-muted q-mt-sm").style("white-space: pre-wrap;")
                                with ui.row().classes("q-gutter-xs q-mt-md"):
                                    for f in ph_features[:5]:
                                        _chip(str(f))

        # section group: philosophy + (news + faq)
        with ui.element("div").classes("pv-section pv-section-alt pv-animate pv-delay-2"):
            with ui.element("div").classes("cvhb-pc-container"):
                with ui.element("div").classes("cvhb-pc-grid2"):
                    # left: philosophy
                    with ui.element("div").classes("pv-card pv-card-pad"):
                        ui.label("私たちの想い").classes("pv-section-title")
                        ui.label(ph_text).classes("pv-muted q-mt-sm").style("white-space: pre-wrap;")
                        with ui.row().classes("q-gutter-xs q-mt-md"):
                            for f in ph_features[:8]:
                                _chip(str(f))
                    # right: stacked cards
                    with ui.column().classes("q-gutter-md"):
                        with ui.element("div").classes("pv-card pv-card-pad"):
                            ui.label("お知らせ").classes("pv-section-title")
                            _news_card(news_items, max_items=4)
                        with ui.element("div").classes("pv-card pv-card-pad"):
                            ui.label("よくある質問").classes("pv-section-title")
                            _faq_card(faq_items, max_items=3)

        # section group: access + contact
        with ui.element("div").classes("pv-section pv-animate pv-delay-3"):
            with ui.element("div").classes("cvhb-pc-container"):
                with ui.element("div").classes("cvhb-pc-grid2"):
                    with ui.element("div").classes("pv-card pv-card-pad"):
                        ui.label("アクセス").classes("pv-section-title")
                        if addr:
                            ui.label(f"住所：{addr}").classes("pv-muted q-mt-sm").style("white-space: pre-wrap;")
                        ui.label(access_note).classes("pv-muted q-mt-sm").style("white-space: pre-wrap;")
                        ui.button("地図を開く").props("unelevated").classes("pv-btn-primary q-mt-md").on(
                            "click", lambda: None
                        )
                    with ui.element("div").classes("pv-card pv-card-pad"):
                        ui.label("お問い合わせ").classes("pv-section-title")
                        ui.label(cta_text).classes("pv-muted q-mt-sm").style("white-space: pre-wrap;")
                        if phone:
                            ui.label(f"TEL：{phone}").classes("text-body2 q-mt-md")
                        ui.label(hours).classes("pv-muted q-mt-xs")
                        if email:
                            ui.label(f"Email：{email}").classes("pv-muted q-mt-xs")
                        ui.button(primary_btn).props("unelevated").classes("pv-btn-primary q-mt-md").on(
                            "click", lambda: None
                        )

        # pre-footer (company small)
        with ui.element("div").classes("pv-section pv-section-alt pv-animate pv-delay-4"):
            with ui.element("div").classes("cvhb-pc-container"):
                with ui.element("div").classes("pv-prefooter"):
                    with ui.row().classes("items-start justify-between"):
                        with ui.column().classes("q-gutter-xs"):
                            ui.label(company).classes("pv-prefooter-title")
                            if industry:
                                ui.label(industry).classes("pv-prefooter-meta")
                            if addr:
                                ui.label(addr).classes("pv-prefooter-meta").style("white-space: pre-wrap;")
                        with ui.column().classes("items-end q-gutter-xs"):
                            if phone:
                                ui.label(f"TEL：{phone}").classes("pv-prefooter-meta")
                            if email:
                                ui.label(email).classes("pv-prefooter-meta")

        # footer
        with ui.element("div").classes("pv-footer"):
            ui.label("© CoreVistaJP / CV-HomeBuilder").classes("pv-muted")


# [BLK-11] Builder (Main)
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
                                    "width: clamp(320px, 38vw, 420px); height: clamp(560px, 75vh, 740px); overflow: hidden; border-radius: 18px; margin: 0 auto;"
                                ).props("flat bordered"):
                                    with ui.element("div").style("height: 100%; overflow: auto;"):
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
                                    "width: min(100%, 1024px); height: clamp(560px, 75vh, 740px); overflow: hidden; border-radius: 14px; margin: 0 auto;"
                                ).props("flat bordered"):
                                    with ui.element("div").style("height: 100%; overflow: auto; background: transparent;"):
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