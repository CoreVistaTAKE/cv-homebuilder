
import base64
import hashlib
import json
import os
import re
import secrets
import stat
import traceback
import asyncio
import mimetypes
import inspect
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



def _short_name(name: str, keep: int = 5) -> str:
    """Shorten filename for UI: keep first N chars and add ellipsis."""
    try:
        s = str(name or "").strip()
    except Exception:
        s = ""
    if not s:
        return ""
    if len(s) <= keep:
        return s
    return s[:keep] + "…"


def _guess_mime(filename: str, default: str = "image/png") -> str:
    """Best-effort MIME guess from filename."""
    try:
        mt, _ = mimetypes.guess_type(filename or "")
        return mt or default
    except Exception:
        return default

# =========================
# Image handling (v0.6.994)
# =========================

# 画像の推奨サイズ（アップロード時の目安）
IMAGE_MAX_W = 1280
IMAGE_MAX_H = 720
IMAGE_RECOMMENDED_TEXT = "推奨画像サイズ：1280×720（16:9）※自動で16:9にカットして保存"

# 事故防止：極端に大きいファイルは弾く（Heroku/ブラウザの負荷対策）
MAX_UPLOAD_BYTES = 10_000_000  # 10MB


def _maybe_resize_image_bytes(data: bytes, mime: str, *, max_w: int, max_h: int) -> tuple[bytes, str]:
    """画像を target(max_w×max_h) に「16:9でセンタークロップ + リサイズ」して返す（v0.6.996）。

    目的:
    - 画像の保存/表示の比率を 1280×720（16:9）に統一したい
    - 元画像が縦長/横長でも、できるだけ残しつつ中心を基準にカットする

    仕様:
    - Pillow(PIL) が無い環境では元データを返す（安全優先）
    - 画像は EXIF の回転を補正してから処理する
    - 出力は: 透過あり -> PNG / 透過なし -> JPEG(quality=85)
    """
    try:
        if not data:
            return data, mime
        if max_w <= 0 or max_h <= 0:
            return data, mime
        if not str(mime or "").startswith("image/"):
            return data, mime

        # Pillow が入っている場合だけ加工する（依存が無い環境でも落ちない）
        try:
            from PIL import Image, ImageOps  # type: ignore
            from io import BytesIO
        except Exception:
            return data, mime

        im = Image.open(BytesIO(data))
        try:
            im.load()
        except Exception:
            pass

        # EXIF の回転を補正（スマホ写真が横倒しになる事故を防ぐ）
        try:
            im = ImageOps.exif_transpose(im)
        except Exception:
            pass

        w, h = getattr(im, "size", (0, 0))
        if not w or not h:
            return data, mime

        target_w = int(max_w)
        target_h = int(max_h)
        if target_w <= 0 or target_h <= 0:
            return data, mime

        target_ratio = target_w / float(target_h)
        src_ratio = w / float(h)

        # --- センタークロップで 16:9 に寄せる（できるだけ残す） ---
        # 縦横どちらが大きいかをベースにして、はみ出る分だけをカット
        try:
            if src_ratio > target_ratio:
                # 横長 → 左右をカット
                new_w = max(1, int(round(h * target_ratio)))
                left = int(round((w - new_w) / 2.0))
                im = im.crop((left, 0, left + new_w, h))
            elif src_ratio < target_ratio:
                # 縦長 → 上下をカット
                new_h = max(1, int(round(w / target_ratio)))
                top = int(round((h - new_h) / 2.0))
                im = im.crop((0, top, w, top + new_h))
        except Exception:
            # crop に失敗しても元のまま続行（落ちない方が大事）
            pass

        # --- 1280×720 にリサイズ（小さければ拡大もする） ---
        try:
            im = im.resize((target_w, target_h), Image.LANCZOS)
        except Exception:
            try:
                im = im.resize((target_w, target_h))
            except Exception:
                pass

        # 透過がある場合は PNG、それ以外は JPEG（軽量化）
        has_alpha = (
            im.mode in ("RGBA", "LA")
            or (im.mode == "P" and ("transparency" in getattr(im, "info", {})))
        )

        from io import BytesIO  # local import（PILがあるときだけ到達）
        out = BytesIO()
        if has_alpha:
            out_mime = "image/png"
            try:
                im.save(out, format="PNG", optimize=True)
            except Exception:
                return data, mime
        else:
            out_mime = "image/jpeg"
            if im.mode != "RGB":
                try:
                    im = im.convert("RGB")
                except Exception:
                    pass
            try:
                im.save(out, format="JPEG", quality=85, optimize=True, progressive=True)
            except Exception:
                return data, mime

        out_bytes = out.getvalue()
        return (out_bytes, out_mime) if out_bytes else (data, mime)
    except Exception:
        return data, mime



async def _read_upload_bytes(content) -> bytes:
    """Read bytes from NiceGUI upload content safely (supports sync/async).

    v0.6.997:
    - 画像の「読み込み位置」が末尾になって 0バイトになることがあるため、
      先に seek(0) で先頭に戻してから読み込みます。
    - content / content.file どちらでも読めるようにフォールバックします。
    """
    if content is None:
        return b""

    # 1) try rewind (UploadFile-like)
    try:
        seek_fn = getattr(content, "seek", None)
    except Exception:
        seek_fn = None
    try:
        if callable(seek_fn):
            r = seek_fn(0)
            if inspect.isawaitable(r):
                await r
    except Exception:
        pass

    # 2) try rewind underlying file too
    try:
        fobj = getattr(content, "file", None)
    except Exception:
        fobj = None
    try:
        if fobj is not None and hasattr(fobj, "seek"):
            fobj.seek(0)
    except Exception:
        pass

    # 3) read bytes
    try:
        read_fn = getattr(content, "read", None)
    except Exception:
        read_fn = None

    try:
        data = None

        if callable(read_fn):
            data = read_fn()
            if inspect.isawaitable(data):
                data = await data
        elif fobj is not None and hasattr(fobj, "read"):
            data = fobj.read()
        else:
            data = content

        if data is None:
            return b""
        if isinstance(data, (bytes, bytearray, memoryview)):
            return bytes(data)

        # last resort: try bytes()
        try:
            return bytes(data)
        except Exception:
            return b""
    except Exception:
        return b""



async def _upload_event_to_data_url(e, *, max_w: int = 0, max_h: int = 0) -> tuple[str, str]:
    """Convert a NiceGUI upload event into (data_url, filename).

    v0.6.996:
    - 画像は「1280×720（16:9）」に自動でセンターカットして保存（Pillowがある環境のみ）
    - 極端に大きいファイルは弾く（事故防止）
    """
    try:
        fname = str(getattr(e, "name", "") or "")
    except Exception:
        fname = ""
    try:
        mime = str(getattr(e, "type", "") or "")
    except Exception:
        mime = ""
    content = getattr(e, "content", None)
    data = await _read_upload_bytes(content)
    if not data:
        try:
            ui.notify("画像の読み込みに失敗しました（JPG/PNG をお試しください）", type="warning")
        except Exception:
            pass
        return "", fname

    # safety: too big
    try:
        if len(data) > MAX_UPLOAD_BYTES:
            try:
                ui.notify("画像ファイルが大きすぎます。1280×720に縮小してから再アップロードしてください。", type="warning")
            except Exception:
                pass
            return "", fname
    except Exception:
        pass

    if not mime:
        mime = _guess_mime(fname, "image/png")

    # optional resize/compress
    try:
        if max_w and max_h:
            data, mime = _maybe_resize_image_bytes(data, mime, max_w=max_w, max_h=max_h)
    except Exception:
        pass

    try:
        b64 = base64.b64encode(data).decode("ascii")
    except Exception:
        b64 = ""
    if not b64:
        return "", fname
    return f"data:{mime};base64,{b64}", fname

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
    width: 100%;
    max-width: none;
    margin: 0;
    padding: 16px;
  }

  /* ====== Split layout (PC builder) ====== */
  .cvhb-split {
    display: grid;
    grid-template-columns: 520px minmax(0, 1fr);
    gap: 16px;
    align-items: start;
  }
  .cvhb-left-col,
  .cvhb-right-col {
    width: 100%;
  }

  /* 左側フォームはカード幅いっぱいを使う（左寄せで細く見えるのを防ぐ） */
  .cvhb-left-col .q-field,
  .cvhb-left-col .q-input,
  .cvhb-left-col .q-textarea {
    width: 100%;
  }

  /* v0.6.992: 左入力欄の見た目（横幅・余白）を微調整 */
  .cvhb-left-col .q-card { width: 100%; }
  .cvhb-left-col .q-card.q-pa-md { padding: 14px !important; }
  .cvhb-left-col .q-field--outlined .q-field__control { border-radius: 12px; }
  .cvhb-left-col .q-field__bottom { padding-left: 0; }



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
          padding: 12px 6px;
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

/* ===== Builder内プレビューの基準幅（重要） =====
   - スマホ: 720px
   - PC: 1920px（プレビューは縮小表示／最低1280px）
   ※ 実際の幅は JS の fit 関数が style.width で制御します。
      ここで max-width:100% を付けると「PCもスマホも同じ」に見える原因になるので付けません。
*/
.pv-shell.pv-layout-260218.pv-mode-mobile{
  width: 720px;      /* JS未適用時のフォールバック */
  max-width: none;
  height: 100%;
  margin: 0;
}

.pv-shell.pv-layout-260218.pv-mode-pc{
  width: 1920px;     /* JS未適用時のフォールバック */
  max-width: none;
  height: 100%;
  margin: 0;
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
  background: linear-gradient(180deg, rgba(255,255,255,0.64), rgba(255,255,255,0.50));
  border-bottom: 1px solid rgba(255,255,255,0.28);
}

.pv-layout-260218.pv-dark .pv-topbar-260218{
  background: linear-gradient(180deg, rgba(13,16,22,0.74), rgba(13,16,22,0.56));
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

/* PCヘッダー：デスクトップナビ（PCモードだけ表示） */
.pv-layout-260218 .pv-desktop-nav{
  display: none;
  gap: 6px;
  align-items: center;
  flex: 0 0 auto;
}
.pv-layout-260218.pv-mode-pc .pv-desktop-nav{
  display: flex;
}
.pv-layout-260218 .pv-desktop-nav .q-btn{
  font-weight: 800;
  border-radius: 999px;
  padding: 6px 10px;
  color: var(--pv-text) !important;
}
.pv-layout-260218 .pv-desktop-nav .q-btn:hover{
  background: rgba(255,255,255,0.22);
}
.pv-layout-260218.pv-dark .pv-desktop-nav .q-btn:hover{
  background: rgba(255,255,255,0.08);
}

.pv-layout-260218 .pv-main{
  max-width: 1280px;
  margin: 0 auto;
  padding: 20px 18px 0;
  font-size: 18px; /* v0.6.992: 本文を大きく（ヘッダー/フッターは除外） */
}

.pv-layout-260218 .pv-section{
  margin: 22px 0 34px;
}

.pv-layout-260218 .pv-section-head{
  display: flex;
  align-items: baseline;
  gap: 10px;
  margin-bottom: 12px;
}

.pv-layout-260218 .pv-section-title{
  font-weight: 900;
  font-size: 1.24rem; /* v0.6.992 */
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
  background: linear-gradient(180deg, rgba(255,255,255,0.46), rgba(255,255,255,0.30));
  border: 1px solid rgba(255,255,255,0.26);
  border-radius: 18px;
  backdrop-filter: blur(14px);
  padding: 16px;
}

.pv-layout-260218.pv-dark .pv-panel-flat{
  background: linear-gradient(180deg, rgba(15,18,25,0.62), rgba(15,18,25,0.40));
  border-color: rgba(255,255,255,0.12);
}

.pv-layout-260218 .pv-muted{
  color: var(--pv-muted);
}

.pv-layout-260218 .pv-h2{
  font-weight: 900;
  font-size: 1.15rem; /* v0.6.992 */
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


/* ====== About: 要点カード（v0.6.992） ====== */
.pv-layout-260218 .pv-points{
  margin-top: 12px;
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
}
.pv-layout-260218 .pv-point-card{
  flex: 1 1 180px;
  min-width: 160px;
  padding: 12px 12px;
  border-radius: 16px;
  border: 1px solid rgba(255,255,255,0.22);
  box-shadow: 0 26px 70px rgba(15, 23, 42, 0.18);
  background: linear-gradient(180deg, rgba(255,255,255,0.44), rgba(255,255,255,0.28));
  box-shadow: 0 14px 34px rgba(15, 23, 42, 0.10);
  backdrop-filter: blur(14px);
  border-left: 6px solid var(--pv-primary);
}
.pv-layout-260218.pv-dark .pv-point-card{
  background: linear-gradient(180deg, rgba(15,18,25,0.58), rgba(15,18,25,0.38));
  border-color: rgba(255,255,255,0.12);
  box-shadow: 0 18px 44px rgba(0,0,0,0.42);
}
.pv-layout-260218 .pv-point-text{
  font-weight: 900;
  line-height: 1.55;
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
  font-size: clamp(1.45rem, 3.8vw, 2.55rem);
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
.pv-layout-260218.pv-mode-mobile .pv-hero-slider{
  height: 240px;
}

.pv-layout-260218.pv-mode-pc .pv-hero-slider{
  height: 420px;
}


.pv-layout-260218.pv-dark .pv-hero-slider{
  border-color: rgba(255,255,255,0.12);
  background: rgba(0,0,0,0.14);
}

/* ===== Hero: フル幅 & 大きく（nagomi-support.com のTOPみたいに） ===== */
.pv-layout-260218 .pv-hero-wide{
  position: relative;
  margin: 0;
}
.pv-layout-260218 .pv-hero-slider-wide{
  border-radius: 0;
  border: none;
  box-shadow: none;
  background: rgba(255,255,255,0.10);
}
.pv-layout-260218.pv-mode-mobile .pv-hero-slider-wide{
  height: 380px;
}
.pv-layout-260218.pv-mode-pc .pv-hero-slider-wide{
  height: 680px;
}
.pv-layout-260218.pv-dark .pv-hero-slider-wide{
  background: rgba(0,0,0,0.16);
}

.pv-layout-260218 .pv-hero-caption{
  position: absolute;
  left: 50%;
  bottom: 26px;
  transform: translateX(-50%);
  display: inline-block;
  width: fit-content;
  max-width: min(92%, 980px);
  padding: 18px 22px;
  border-radius: 18px;
  text-align: center;
  backdrop-filter: blur(18px);
  background: rgba(255,255,255,0.55);
  border: 1px solid rgba(255,255,255,0.42);
  box-shadow: 0 22px 54px rgba(0,0,0,0.12);
}

/* SP: キャッチは画像に重ねず下へ（画像の下で目立たせる） */
.pv-layout-260218.pv-mode-mobile .pv-hero-caption{
  position: static;
  left: auto;
  bottom: auto;
  transform: none;
  width: min(92%, 680px);
  margin: 14px auto 0;
  padding: 16px 18px;
  border-radius: 16px;
  text-align: center;
  display: flex;
  flex-direction: column;
  align-items: center;
  backdrop-filter: blur(12px);
  background: linear-gradient(180deg, rgba(255,255,255,0.90), rgba(255,255,255,0.78));
  border: 1px solid rgba(0,0,0,0.06);
  border-top: 5px solid var(--pv-primary);
  box-shadow: 0 20px 52px rgba(0,0,0,0.14);
}
.pv-layout-260218.pv-dark.pv-mode-mobile .pv-hero-caption{
  background: rgba(0,0,0,0.55);
  border-color: rgba(255,255,255,0.14);
}

.pv-layout-260218.pv-dark .pv-hero-caption{
  background: rgba(0,0,0,0.45);
  border-color: rgba(255,255,255,0.14);
  box-shadow: 0 18px 44px rgba(0,0,0,0.22);
}

/* PC: キャッチを「ガラスっぽく」して目立たせる（v0.6.996） */
.pv-layout-260218.pv-mode-pc .pv-hero-caption{
  bottom: 42px;
  display: inline-block;
  width: fit-content;
  max-width: min(92%, 1120px);
  padding: 26px 32px;
  border-radius: 26px;
  text-align: center;
  backdrop-filter: blur(22px);
  background: linear-gradient(180deg, rgba(255,255,255,0.32), rgba(255,255,255,0.14));
  border: 1px solid rgba(255,255,255,0.56);
  box-shadow: 0 40px 120px rgba(0,0,0,0.16);
}
.pv-layout-260218.pv-dark.pv-mode-pc .pv-hero-caption{
  background: linear-gradient(180deg, rgba(0,0,0,0.46), rgba(0,0,0,0.28));
  border-color: rgba(255,255,255,0.18);
  box-shadow: 0 24px 64px rgba(0,0,0,0.26);
}
.pv-layout-260218.pv-mode-pc .pv-hero-caption-title{
  font-size: clamp(2.25rem, 3.2vw, 3.9rem);
  letter-spacing: 0.02em;
  text-shadow: 0 12px 30px rgba(0,0,0,0.18);
}
.pv-layout-260218.pv-mode-pc .pv-hero-caption-sub{
  font-size: clamp(1.18rem, 1.45vw, 1.45rem);
  text-shadow: 0 10px 24px rgba(0,0,0,0.16);
}

.pv-layout-260218 .pv-hero-caption-title{
  font-weight: 1000;
  font-size: clamp(1.4rem, 2.8vw, 2.8rem);
  line-height: 1.15;
}
.pv-layout-260218.pv-mode-mobile .pv-hero-caption-title{
  font-size: 1.75rem;
}

.pv-layout-260218.pv-mode-mobile .pv-hero-caption-title,
.pv-layout-260218.pv-mode-mobile .pv-hero-caption-sub{
  width: 100%;
  text-align: center;
}
.pv-layout-260218 .pv-hero-caption-sub{
  margin-top: 8px;
  line-height: 1.7;
  color: var(--pv-muted);
}

.pv-layout-260218 .pv-hero-track{
  display: flex;
  height: 100%;
  transition: transform 500ms ease;
}

.pv-layout-260218 .pv-hero-slide{
  flex: 0 0 100%;
  height: 100%;
  position: relative;
}

.pv-layout-260218 .pv-hero-img{
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}


.pv-layout-260218 .pv-news-list{
  margin-top: 6px;
}

.pv-layout-260218 .pv-news-item{
  display: grid;
  grid-template-columns: 110px 92px 1fr 24px;
  gap: 10px;
  align-items: center;
  padding: 10px 0;
  border-bottom: 1px solid var(--pv-line);
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
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.pv-layout-260218 .pv-news-empty{
  opacity: 0;
}

.pv-layout-260218 .pv-news-arrow{
  justify-self: end;
  opacity: 0.45;
}

.pv-layout-260218.pv-dark .pv-news-arrow{
  opacity: 0.55;
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
  background: linear-gradient(180deg, rgba(255,255,255,0.52), rgba(255,255,255,0.34));
  border: 1px solid rgba(255,255,255,0.28);
  border-radius: 22px;
  box-shadow: var(--pv-shadow);
  backdrop-filter: blur(16px);
  padding: 16px;
}

.pv-layout-260218.pv-dark .pv-surface-white{
  background: linear-gradient(180deg, rgba(12,15,22,0.64), rgba(12,15,22,0.44));
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
  border-bottom: 1px solid var(--pv-line);
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
  border-bottom: 1px solid var(--pv-line);
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

.pv-layout-260218 .pv-access-company{
  font-weight: 900;
  font-size: 1.05rem;
  margin-bottom: 6px;
}
.pv-layout-260218 .pv-access-meta{
  flex-wrap: wrap;
}
.pv-layout-260218 .pv-access-icon{
  font-size: 18px;
  opacity: 0.92;
}


/* ====== Access: 地図枠（画像風）+ GoogleMap iframe（任意）+ 地図を開くリンク（v0.6.995） ====== */
.pv-layout-260218 .pv-mapframe{
  margin-top: 12px;
  border-radius: 20px;
  overflow: hidden;
  position: relative;
  height: 260px;
  border: 1px solid rgba(255,255,255,0.22);
  box-shadow: 0 26px 70px rgba(15,23,42,0.14);
  background:
    radial-gradient(420px 260px at 12% 18%, rgba(255,255,255,0.36), transparent 70%),
    radial-gradient(520px 320px at 88% 92%, rgba(255,255,255,0.22), transparent 72%),
    linear-gradient(135deg, var(--pv-primary-weak), rgba(255,255,255,0.14)),
    repeating-linear-gradient(0deg, rgba(255,255,255,0.16) 0, rgba(255,255,255,0.16) 2px, transparent 2px, transparent 26px),
    repeating-linear-gradient(90deg, rgba(255,255,255,0.14) 0, rgba(255,255,255,0.14) 2px, transparent 2px, transparent 30px),
    repeating-linear-gradient(45deg, rgba(15,23,42,0.05) 0, rgba(15,23,42,0.05) 2px, transparent 2px, transparent 78px),
    repeating-linear-gradient(-45deg, rgba(15,23,42,0.04) 0, rgba(15,23,42,0.04) 2px, transparent 2px, transparent 92px);
}
.pv-layout-260218.pv-mode-mobile .pv-mapframe{
  height: 230px;
}
.pv-layout-260218.pv-mode-pc .pv-mapframe{
  height: 310px;
}
.pv-layout-260218.pv-dark .pv-mapframe{
  border-color: rgba(255,255,255,0.12);
  box-shadow: 0 26px 70px rgba(0,0,0,0.28);
  background:
    radial-gradient(420px 260px at 12% 18%, rgba(255,255,255,0.10), transparent 70%),
    radial-gradient(520px 320px at 88% 92%, rgba(255,255,255,0.06), transparent 72%),
    linear-gradient(135deg, rgba(255,255,255,0.06), rgba(0,0,0,0.22)),
    repeating-linear-gradient(0deg, rgba(255,255,255,0.08) 0, rgba(255,255,255,0.08) 2px, transparent 2px, transparent 26px),
    repeating-linear-gradient(90deg, rgba(255,255,255,0.08) 0, rgba(255,255,255,0.08) 2px, transparent 2px, transparent 30px),
    repeating-linear-gradient(45deg, rgba(255,255,255,0.05) 0, rgba(255,255,255,0.05) 2px, transparent 2px, transparent 78px),
    repeating-linear-gradient(-45deg, rgba(255,255,255,0.04) 0, rgba(255,255,255,0.04) 2px, transparent 2px, transparent 92px);
}

.pv-layout-260218 .pv-mapframe-link{
  display: block;
  text-decoration: none;
  color: inherit;
}

/* live map (iframe) */
.pv-layout-260218 .pv-mapframe-live{
  background: rgba(255,255,255,0.06);
}
.pv-layout-260218.pv-dark .pv-mapframe-live{
  background: rgba(0,0,0,0.20);
}
.pv-layout-260218 .pv-map-iframe{
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  border: 0;
}
.pv-layout-260218 .pv-mapframe-ui{
  position: absolute;
  inset: 0;
  pointer-events: none;
}
.pv-layout-260218 .pv-mapframe-ui .pv-mapframe-open{
  pointer-events: auto;
}

.pv-layout-260218 .pv-mapframe-badge{
  position: absolute;
  left: 12px;
  top: 12px;
  padding: 6px 10px;
  border-radius: 999px;
  font-weight: 900;
  font-size: 0.72rem;
  letter-spacing: 0.06em;
  background: rgba(255,255,255,0.66);
  border: 1px solid rgba(255,255,255,0.28);
  color: var(--pv-text);
  backdrop-filter: blur(10px);
  pointer-events: none;
}
.pv-layout-260218.pv-dark .pv-mapframe-badge{
  background: rgba(0,0,0,0.32);
  border-color: rgba(255,255,255,0.12);
  color: rgba(255,255,255,0.90);
}

.pv-layout-260218 .pv-mapframe-pin{
  position: absolute;
  left: 50%;
  top: 46%;
  transform: translate(-50%, -70%);
  font-size: 44px;
  color: var(--pv-primary);
  filter: drop-shadow(0 14px 24px rgba(0,0,0,0.22));
}
.pv-layout-260218 .pv-mapframe-bottom{
  position: absolute;
  left: 12px;
  right: 12px;
  bottom: 12px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 10px 12px;
  border-radius: 16px;
  background: rgba(255,255,255,0.18);
  border: 1px solid rgba(255,255,255,0.20);
  backdrop-filter: blur(10px);
}
.pv-layout-260218.pv-dark .pv-mapframe-bottom{
  background: rgba(0,0,0,0.22);
  border-color: rgba(255,255,255,0.12);
}

.pv-layout-260218 .pv-mapframe-label{
  font-weight: 900;
  color: var(--pv-text);
  text-shadow: 0 2px 12px rgba(0,0,0,0.28);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.pv-layout-260218 .pv-mapframe-open{
  flex: 0 0 auto;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 8px 12px;
  border-radius: 999px;
  text-decoration: none;
  background: linear-gradient(135deg, var(--pv-accent), var(--pv-accent-2));
  color: #fff;
  font-weight: 900;
  box-shadow: 0 14px 28px rgba(0,0,0,0.22);
}

.pv-layout-260218 .pv-map-openlink{
  display: inline-flex;
  align-items: center;
  gap: 6px;
  margin-top: 10px;
  font-weight: 900;
  text-decoration: none;
  color: var(--pv-primary);
}
.pv-layout-260218 .pv-map-openlink:hover{
  text-decoration: underline;
}
.pv-layout-260218 .pv-map-openlink .q-icon{
  font-size: 18px;
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
  max-width: 1280px;
  margin: 0 auto;
  border-radius: 22px;
  padding: 14px 16px;
  font-size: 18px; /* v0.6.992: 下部バーも読みやすく */
  background: linear-gradient(180deg, rgba(255,255,255,0.40), rgba(255,255,255,0.26));
  border: 1px solid rgba(255,255,255,0.22);
  backdrop-filter: blur(14px);
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.pv-layout-260218.pv-dark .pv-companybar-inner{
  background: linear-gradient(180deg, rgba(13,16,22,0.62), rgba(13,16,22,0.44));
  border-color: rgba(255,255,255,0.12);
}

.pv-layout-260218 .pv-companybar-name{
  font-weight: 1000;
}

.pv-layout-260218 .pv-companybar-meta{
  color: var(--pv-muted);
  font-size: 0.95rem; /* v0.6.992 */
  margin-top: 2px;
}

.pv-layout-260218 .pv-mapshot{
  padding: 0 18px 18px;
}

.pv-layout-260218 .pv-mapshot-inner{
  max-width: 1280px;
  margin: 0 auto;
}

.pv-layout-260218 .pv-mapshot-card{
  padding: 14px;
}

.pv-layout-260218 .pv-mapshot-head{
  margin-bottom: 10px;
}

.pv-layout-260218 .pv-mapshot-label{
  font-weight: 900;
  font-size: 0.78rem;
  letter-spacing: 0.14em;
  opacity: 0.65;
}

.pv-layout-260218 .pv-mapshot-img-link{
  display: block;
  text-decoration: none;
}

.pv-layout-260218 .pv-mapshot-img{
  position: relative;
  height: 220px;
  border-radius: 22px;
  overflow: hidden;
  border: 1px solid rgba(255,255,255,0.22);
  box-shadow: var(--pv-shadow);
  background:
    radial-gradient(520px 220px at 18% 22%, rgba(255,255,255,0.44), transparent 62%),
    radial-gradient(420px 240px at 88% 18%, rgba(255,255,255,0.28), transparent 62%),
    linear-gradient(160deg, rgba(255,255,255,0.30), rgba(255,255,255,0.14)),
    repeating-linear-gradient(0deg, rgba(15,23,42,0.06), rgba(15,23,42,0.06) 1px, transparent 1px, transparent 14px),
    repeating-linear-gradient(90deg, rgba(15,23,42,0.04), rgba(15,23,42,0.04) 1px, transparent 1px, transparent 18px);
}

.pv-layout-260218.pv-mode-pc .pv-mapshot-img{
  height: 280px;
}

.pv-layout-260218.pv-dark .pv-mapshot-img{
  border-color: rgba(255,255,255,0.12);
  background:
    radial-gradient(520px 220px at 18% 22%, rgba(255,255,255,0.14), transparent 62%),
    radial-gradient(420px 240px at 88% 18%, rgba(255,255,255,0.08), transparent 62%),
    linear-gradient(160deg, rgba(15,18,25,0.66), rgba(15,18,25,0.42)),
    repeating-linear-gradient(0deg, rgba(255,255,255,0.08), rgba(255,255,255,0.08) 1px, transparent 1px, transparent 14px),
    repeating-linear-gradient(90deg, rgba(255,255,255,0.06), rgba(255,255,255,0.06) 1px, transparent 1px, transparent 18px);
}

.pv-layout-260218 .pv-mapshot-pin{
  position: absolute;
  left: 50%;
  top: 50%;
  transform: translate(-50%, -62%);
  font-size: 46px;
  color: var(--pv-primary) !important;
  filter: drop-shadow(0 10px 24px rgba(0,0,0,0.12));
  opacity: 0.90;
}

.pv-layout-260218.pv-dark .pv-mapshot-pin{
  filter: drop-shadow(0 10px 24px rgba(0,0,0,0.32));
}

.pv-layout-260218 .pv-mapshot-open{
  position: absolute;
  left: 50%;
  top: 50%;
  transform: translate(-50%, 42%);
  padding: 6px 12px;
  border-radius: 999px;
  background: rgba(255,255,255,0.34);
  border: 1px solid rgba(255,255,255,0.22);
  color: var(--pv-text);
  font-weight: 900;
  font-size: 0.82rem;
  backdrop-filter: blur(10px);
}

.pv-layout-260218.pv-dark .pv-mapshot-open{
  background: rgba(15,18,25,0.50);
  border-color: rgba(255,255,255,0.12);
  color: rgba(255,255,255,0.86);
}

.pv-layout-260218 .pv-mapshot-address{
  margin-top: 10px;
  color: var(--pv-text);
  opacity: 0.80;
  font-weight: 700;
  line-height: 1.5;
}

.pv-layout-260218 .pv-footer{
  margin-top: 8px;
  padding: 18px;
  background: rgba(10,12,18,0.92);
  border-top: 1px solid rgba(255,255,255,0.08);
  color: rgba(255,255,255,0.78);
}

.pv-layout-260218 .pv-footer-grid{
  max-width: 1280px;
  margin: 0 auto;
  display: grid;
  grid-template-columns: 1fr;
  gap: 12px;
}

.pv-layout-260218.pv-mode-pc .pv-footer-grid{
  grid-template-columns: 1fr;
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
  max-width: 1280px;
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
  window.cvhbInitHeroSlider = function(sliderId, axis, intervalMs){
    try{
      // backward compatible: (sliderId, intervalMs)
      if(typeof axis === 'number' && (intervalMs === undefined || intervalMs === null)){
        intervalMs = axis;
        axis = 'x';
      }
      const slider = document.getElementById(sliderId);
      if(!slider) return;

      // v0.6.994: interval 多重化を抑止（PCが重くなる主因になりがち）
      try{
        const old = window.__cvhbHeroIntervals[sliderId];
        if(old){ window.clearInterval(old); }
        window.__cvhbHeroIntervals[sliderId] = null;
      } catch(e){}

      const track = slider.querySelector('.pv-hero-track');
      const slides = slider.querySelectorAll('.pv-hero-slide');
      if(!track || !slides || slides.length <= 1) return;

      const dots = slider.querySelectorAll('.pv-hero-dot');
      let idx = 0;
      const useAxis = (String(axis || 'x').toLowerCase() === 'y') ? 'y' : 'x';

      // stack direction (PC=横, スマホ=縦)
      try{
        track.style.flexDirection = (useAxis === 'y') ? 'column' : 'row';
      } catch(e){}

      const apply = () => {
        if(useAxis === 'y'){
          track.style.transform = 'translateY(-' + (idx * 100) + '%)';
        } else {
          track.style.transform = 'translateX(-' + (idx * 100) + '%)';
        }
        try{
          dots.forEach((d,i)=>{ if(i===idx) d.classList.add('is-active'); else d.classList.remove('is-active'); });
        } catch(e){}
      };

      // v0.6.994: addEventListener を使わず「上書き」で重複を防ぐ
      try{
        dots.forEach((d,i)=>{ d.onclick = function(){ idx=i; apply(); }; });
      } catch(e){}

      const ms = (intervalMs && intervalMs > 0) ? intervalMs : 4500;

      const stop = () => {
        try{
          const t = window.__cvhbHeroIntervals[sliderId];
          if(t){ window.clearInterval(t); }
          window.__cvhbHeroIntervals[sliderId] = null;
        } catch(e){}
      };

      const start = () => {
        stop();
        try{
          const t = window.setInterval(()=>{ idx=(idx+1)%slides.length; apply(); }, ms);
          window.__cvhbHeroIntervals[sliderId] = t;
        } catch(e){}
      };

      slider.onmouseenter = function(){ stop(); };
      slider.onmouseleave = function(){ start(); };

      start();
      apply();
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

  // Fit-to-width scaler for preview frames (e.g. 720px / 1920px)
// - Previewカード内で「横が全部見える」ように自動で縮小する
// - タブ切替 / 再描画の瞬間に width が 0 になることがあるため、リトライして安定化する
window.__cvhbFit = window.__cvhbFit || { regs: {}, observers: {}, timers: {}, gen: {} };

  // Debug logger (DevTools で必要なときだけONにできる)
  window.__cvhbDebug = window.__cvhbDebug || { enabled: false, logs: [] };
  window.cvhbDebugEnable = window.cvhbDebugEnable || function(flag){
    try{
      window.__cvhbDebug.enabled = !!flag;
      if(window.__cvhbDebug.enabled){
        try{ console.info('[cvhb] debug enabled'); }catch(e){}
      }
    }catch(e){}
  };
  window.cvhbDebugLog = window.cvhbDebugLog || function(event, data){
    try{
      if(!window.__cvhbDebug || !window.__cvhbDebug.enabled) return;
      const rec = { ts: new Date().toISOString(), event: String(event||''), data: data || null };
      window.__cvhbDebug.logs.push(rec);
      if(window.__cvhbDebug.logs.length > 600) window.__cvhbDebug.logs.shift();
      try{ console.log('[cvhb]', rec.event, rec.data); }catch(e){}
    }catch(e){}
  };
  window.cvhbDebugDump = window.cvhbDebugDump || function(){
    try{
      const logs = (window.__cvhbDebug && window.__cvhbDebug.logs) ? window.__cvhbDebug.logs : [];
      return JSON.stringify(logs, null, 2);
    }catch(e){
      return '[]';
    }
  };
  // 現象再現時に DOM状態も一緒に採取できる（任意）
  window.cvhbDebugSnapshot = window.cvhbDebugSnapshot || function(){
    try{
      const outer = document.getElementById('pv-fit');
      const inner = document.getElementById('pv-root');
      const cs = outer ? window.getComputedStyle(outer) : null;
      const snap = {
        has_outer: !!outer,
        has_inner: !!inner,
        outer_clientWidth: outer ? outer.clientWidth : null,
        outer_offsetWidth: outer ? outer.offsetWidth : null,
        outer_rectWidth: outer ? outer.getBoundingClientRect().width : null,
        outer_clientHeight: outer ? outer.clientHeight : null,
        outer_offsetHeight: outer ? outer.offsetHeight : null,
        outer_rectHeight: outer ? outer.getBoundingClientRect().height : null,
        outer_display: cs ? cs.display : null,
        outer_visibility: cs ? cs.visibility : null,
        outer_position: cs ? cs.position : null,
      };
      try{ window.cvhbDebugLog && window.cvhbDebugLog('snapshot', snap); }catch(e){}
      return snap;
    }catch(e){
      return null;
    }
  };


window.cvhbFitRegister = window.cvhbFitRegister || function(key, outerId, innerId, designWidth, minWidth, maxWidth, minScale, maxScale){
  try{
    const safeNum = function(v, fb){
      v = Number(v);
      if(!isFinite(v)) return fb || 0;
      return v;
    };

    const dwReq = Math.max(1, safeNum(designWidth, 1));
    const minW = Math.max(0, safeNum(minWidth, 0));
    const maxW = Math.max(0, safeNum(maxWidth, 0));

    const minS = safeNum(minScale, 0);
    const maxS = safeNum(maxScale, 0);
    const hasScaleLimits = (minS > 0) || (maxS > 0);

    // register 世代管理（古いタイマーが新しいDOMに触って事故るのを防ぐ）
    try{
      window.__cvhbFit.gen = window.__cvhbFit.gen || {};
      window.__cvhbFit.gen[key] = (window.__cvhbFit.gen[key] || 0) + 1;
    }catch(e){}
    const myGen = (window.__cvhbFit.gen && window.__cvhbFit.gen[key]) ? window.__cvhbFit.gen[key] : 0;

    // 古いタイマーは無効化
    try{
      if(window.__cvhbFit.timers && window.__cvhbFit.timers[key]){
        clearTimeout(window.__cvhbFit.timers[key]);
        delete window.__cvhbFit.timers[key];
      }
    }catch(e){}

    let tries = 0;
    const MAX_TRIES = 60;
    const DELAY_MS = 80;

    const apply = function(){
      try{
        // stale guard
        try{
          if(window.__cvhbFit.gen && window.__cvhbFit.gen[key] !== myGen) return;
        }catch(e){}

        const outer = document.getElementById(outerId);
        const inner = document.getElementById(innerId);
        if(!outer || !inner){
          try{ window.cvhbDebugLog && window.cvhbDebugLog('fit_missing', {key:key, outerId:outerId, innerId:innerId}); }catch(e){}
          return;
        }

        // outer が content-driven で 0px になる事故を防止
        try{
          outer.style.width = '100%';
          outer.style.display = 'block';
        }catch(e){}

        const rect = outer.getBoundingClientRect();
        const ow = Math.max(
          safeNum(rect.width, 0),
          safeNum(outer.clientWidth, 0),
          safeNum(outer.offsetWidth, 0)
        );
        const oh = Math.max(
          safeNum(rect.height, 0),
          safeNum(outer.clientHeight, 0),
          safeNum(outer.offsetHeight, 0)
        );

        // not ready / hidden (0px になりがち) -> 少し待って再計測
        if(ow <= 0 || oh <= 0){
          try{ window.cvhbDebugLog && window.cvhbDebugLog('fit_wait', {key:key, ow:ow, oh:oh, tries:tries}); }catch(e){}
          if(tries < MAX_TRIES){
            tries++;
            try{ clearTimeout(window.__cvhbFit.timers[key]); }catch(e){}
            window.__cvhbFit.timers[key] = setTimeout(apply, DELAY_MS);
          }else{
            // fallback: とにかく見える状態に戻す（scale は諦める）
            try{
              inner.style.position = 'relative';
              inner.style.top = '0px';
              inner.style.left = '0px';
              inner.style.width = '100%';
              inner.style.height = '100%';
              inner.style.maxWidth = 'none';
              inner.style.transformOrigin = 'top left';
              inner.style.transform = 'none';
              inner.style.visibility = 'visible';
              inner.style.opacity = '1';
            }catch(e){}
            try{ window.cvhbDebugLog && window.cvhbDebugLog('fit_fallback', {key:key, ow:ow, oh:oh, dw_req:dwReq, minW:minW||0, maxW:maxW||0, minS:minS||0, maxS:maxS||0}); }catch(e){}
          }
          return;
        }

        // inner width:
        // - scale指定がある場合: 「設計幅(dwReq)」固定で縮小（PC=1920 / SP=720）
        // - scale指定がない場合: 互換モード（旧: minW/maxWで dwUsed を可変にする）
        let dwUsed = dwReq;
        if(!hasScaleLimits){
          try{
            if(maxW && maxW > 0) dwUsed = Math.min(dwUsed, maxW);
            if(minW && minW > 0){
              if(ow < minW) dwUsed = minW;
              dwUsed = Math.max(dwUsed, minW);
            }
          }catch(e){}
        }

        inner.style.position = 'absolute';
        inner.style.top = '0px';
        inner.style.width = dwUsed + 'px';
        inner.style.maxWidth = 'none';
        inner.style.visibility = 'visible';
        inner.style.opacity = '1';
        inner.style.transformOrigin = 'top left';

        const rawScale = ow / dwUsed;
        let scale = rawScale;

        if(hasScaleLimits){
          const lo = (minS > 0) ? minS : 0.01;
          let hi = (maxS > 0) ? maxS : 1;
          hi = Math.min(1, hi);
          scale = Math.max(lo, Math.min(hi, rawScale));
        }else{
          scale = Math.max(0.01, Math.min(1, rawScale));
        }

        // 重要: 縦も「枠いっぱい」に見えるように、inner の高さを scale で補正する
        // outer 高さ = oh
        // inner 高さ = oh / scale  にすると、縮小後の見た目がちょうど oh になる
        const innerH = Math.max(1, oh / Math.max(0.01, scale));
        inner.style.height = innerH + 'px';

        inner.style.transform = 'scale(' + scale + ')';

        const visualW = dwUsed * scale;
        const left = Math.max(0, (ow - visualW) / 2);
        inner.style.left = left + 'px';

        // 横が足りない場合は横スクロール（PC: 960px未満で発生する想定）
        try{
          outer.style.overflowY = 'hidden';
          outer.style.overflowX = (visualW > ow + 1) ? 'auto' : 'hidden';
        }catch(e){}

        try{ window.cvhbDebugLog && window.cvhbDebugLog('fit_applied', {key:key, ow:ow, oh:oh, dw_req:dwReq, dw_used:dwUsed, minW:minW||0, maxW:maxW||0, minS:minS||0, maxS:maxS||0, scale:scale, left:left}); }catch(e){}
      }catch(e){}
    };

    window.__cvhbFit.regs[key] = apply;

    // ResizeObserver (一番安定)
    const ensureObserver = function(){
      try{
        if(!window.ResizeObserver) return;
        const outer = document.getElementById(outerId);
        if(!outer) return;

        if(window.__cvhbFit.observers[key]){
          window.__cvhbFit.observers[key].disconnect();
          delete window.__cvhbFit.observers[key];
        }
        const obs = new ResizeObserver(function(){ try{ apply(); }catch(e){} });
        obs.observe(outer);
        window.__cvhbFit.observers[key] = obs;
      }catch(e){}
    };
    try{ ensureObserver(); }catch(e){}
    setTimeout(function(){ try{ ensureObserver(); }catch(e){} }, 120);

    // fallback: window resize
    if(!window.__cvhbFitInit){
      window.__cvhbFitInit = true;
      window.addEventListener('resize', function(){
        try{
          const regs = (window.__cvhbFit && window.__cvhbFit.regs) ? window.__cvhbFit.regs : {};
          for(const k in regs){
            try{ regs[k](); }catch(e){}
          }
        }catch(e){}
      });
    }

    // first runs (layout settle)
    apply();
    try{ requestAnimationFrame(apply); }catch(e){}
    setTimeout(apply, 60);
    setTimeout(apply, 240);
    setTimeout(apply, 600);
  }catch(e){}
};

  window.cvhbFitApply = window.cvhbFitApply || function(key){
    try{
      const regs = (window.__cvhbFit && window.__cvhbFit.regs) ? window.__cvhbFit.regs : null;
      if(regs && regs[key]) regs[key]();
    }catch(e){}
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


VERSION = read_text_file("VERSION", "0.6.997")
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
    "A: オフィス": "https://images.unsplash.com/photo-1486406146926-c627a92ad1ab?auto=format&fit=crop&w=1280&h=720&q=80",
    "B: チーム": "https://images.unsplash.com/photo-1521737604893-d14cc237f11d?auto=format&fit=crop&w=1280&h=720&q=80",
    "C: 街並み": "https://images.unsplash.com/photo-1449824913935-59a10b8d2000?auto=format&fit=crop&w=1280&h=720&q=80",

    # 福祉テンプレ向けの“雰囲気”プリセット（※ 302リダイレクトの Unsplash Source をやめ、直URLで安定化）
    "D: ひかり": "https://images.unsplash.com/photo-1519751138087-5bf79df62d5b?auto=format&fit=crop&w=1280&h=720&q=80",
    "E: 木": "https://images.unsplash.com/photo-1441974231531-c6227db76b6e?auto=format&fit=crop&w=1280&h=720&q=80",
    "F: 手": "https://images.unsplash.com/photo-1749065311606-fa115df115af?auto=format&fit=crop&w=1280&h=720&q=80",
    "G: 家": "https://images.unsplash.com/photo-1632927126546-e3e051a0ba6e?auto=format&fit=crop&w=1280&h=720&q=80",
}
HERO_IMAGE_OPTIONS = list(HERO_IMAGE_PRESET_URLS.keys())

# v0.6.7: Safe defaults (avoid preview errors)
HERO_IMAGE_DEFAULT = HERO_IMAGE_PRESET_URLS.get("A: オフィス") or next(iter(HERO_IMAGE_PRESET_URLS.values()), "")
# Alias for backward compatibility
HERO_IMAGE_PRESETS = HERO_IMAGE_PRESET_URLS

# Default favicon (data URL). Used when user doesn't upload one.
DEFAULT_FAVICON_SVG = """<svg xmlns='http://www.w3.org/2000/svg' width='64' height='64' viewBox='0 0 64 64'>
  <defs>
    <linearGradient id='g' x1='0' y1='0' x2='1' y2='1'>
      <stop offset='0' stop-color='#6aa8ff'/>
      <stop offset='1' stop-color='#8bf2d2'/>
    </linearGradient>
  </defs>
  <rect x='4' y='4' width='56' height='56' rx='14' fill='url(#g)'/>
  <text x='32' y='40' text-anchor='middle' font-family='Arial, sans-serif' font-size='22' font-weight='700' fill='rgba(0,0,0,0.70)'>CV</text>
</svg>"""
DEFAULT_FAVICON_DATA_URL = "data:image/svg+xml;base64," + base64.b64encode(DEFAULT_FAVICON_SVG.encode("utf-8")).decode("ascii")



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
            "personal_v1": {
                "catch_copy": "あなたの想いを、丁寧に届けます",
                "sub_catch": "まずは無料相談から。お気軽にご連絡ください。",
                "primary_cta": "お問い合わせ",
                "secondary_cta": "相談する",
                "hero_image": "F: 手",
                "about_title": "自己紹介",
                "about_body": "ここに自己紹介や活動内容を書きます。\n（あとで自由に書き換えできます）",
                "about_points": ["丁寧な対応", "柔軟な提案", "分かりやすい説明"],
                "svc_title": "メニュー",
                "svc_lead": "できることを分かりやすくまとめました。",
                "svc_image": "F: 手",
                "svc_items": [
                    {"title": "メニュー1", "body": "内容をここに記載します。"},
                    {"title": "メニュー2", "body": "内容をここに記載します。"},
                    {"title": "メニュー3", "body": "内容をここに記載します。"},
                ],
                "faq_items": [
                    {"q": "相談だけでも大丈夫ですか？", "a": "はい。まずは状況を伺い、最適な進め方をご提案します。"},
                    {"q": "対応エリアはどこですか？", "a": "オンライン／対面どちらも対応可能です。詳しくはお問い合わせください。"},
                    {"q": "料金の目安を教えてください。", "a": "内容により異なります。ご要望を伺い、お見積りをご案内します。"},
                ],
                "contact_message": "まずはお気軽にご相談ください。",
            },
            "free6_v1": {
                "catch_copy": "あなたのサイトを、ここから作れます",
                "sub_catch": "自由に編集して、あなたの内容に合わせましょう。",
                "primary_cta": "お問い合わせ",
                "secondary_cta": "相談する",
                "hero_image": "G: 家",
                "about_title": "自由枠（ここにタイトル）",
                "about_body": "このエリアは自由に使えます。\n（例：サービス紹介／実績／料金／施設紹介 など）",
                "about_points": ["ポイント1（自由）", "ポイント2（自由）", "ポイント3（自由）"],
                "svc_title": "自由枠（追加・削除できます）",
                "svc_lead": "FAQのように、項目を追加・削除して使えます。",
                "svc_image": "F: 手",
                "svc_items": [
                    {"title": "項目1", "body": "内容をここに記載します。"},
                    {"title": "項目2", "body": "内容をここに記載します。"},
                    {"title": "項目3", "body": "内容をここに記載します。"},
                ],
                "faq_items": [
                    {"q": "ここは自由に編集できますか？", "a": "はい。文章や項目を自由に書き換えできます。"},
                    {"q": "項目は追加できますか？", "a": "「＋追加」から増やせます（最大6件）。"},
                    {"q": "公開前に確認できますか？", "a": "右側プレビューでいつでも確認できます。"},
                ],
                "contact_message": "ご相談はお気軽にどうぞ。",
            },
        }

        # personal_v1 / free6_v1 は専用プリセットを使う（corpへ寄せない）

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

    # --- schema guard: 古い/壊れた project.json でも落ちないように型を強制 ---
    data = p.get("data")
    if not isinstance(data, dict):
        data = {}
        p["data"] = data

    step1 = data.get("step1")
    if not isinstance(step1, dict):
        step1 = {}
        data["step1"] = step1

    step2 = data.get("step2")
    if not isinstance(step2, dict):
        step2 = {}
        data["step2"] = step2

    blocks = data.get("blocks")
    if not isinstance(blocks, dict):
        blocks = {}
        data["blocks"] = blocks

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
    step2.setdefault("favicon_filename", "")
    step2.setdefault("catch_copy", "")
    step2.setdefault("phone", "")
    step2.setdefault("address", "")
    step2.setdefault("email", "")

    # blocks
    hero = blocks.setdefault("hero", {})
    # 4枚固定（プリセット or アップロード）
    DEFAULT_CHOICES = ["A: オフィス", "B: チーム", "C: 街並み", "D: ひかり"]
    hero.setdefault("hero_image_url", "")
    hero.setdefault("hero_image_urls", [])
    hero.setdefault("hero_slide_choices", [])
    hero.setdefault("hero_upload_names", [])

    hero_urls_raw = hero.get("hero_image_urls", [])
    if not isinstance(hero_urls_raw, list):
        hero_urls_raw = []

    # legacy single url -> slide[0]
    legacy_one = str(hero.get("hero_image_url") or "").strip()
    if legacy_one and not hero_urls_raw:
        hero_urls_raw = [legacy_one]

    hero_urls = []
    for u in hero_urls_raw[:4]:
        if isinstance(u, str):
            hero_urls.append(u.strip())
        else:
            hero_urls.append("")
    while len(hero_urls) < 4:
        hero_urls.append("")

    # choices
    choices = hero.get("hero_slide_choices", [])
    if not isinstance(choices, list):
        choices = []
    rev = {v: k for k, v in HERO_IMAGE_PRESET_URLS.items()}
    norm_choices: list[str] = []
    for i in range(4):
        ch = ""
        if i < len(choices) and isinstance(choices[i], str):
            ch = choices[i].strip()
        if ch in HERO_IMAGE_PRESET_URLS or ch == "オリジナル":
            norm_choices.append(ch)
            continue
        # infer from existing url
        u = hero_urls[i].strip()
        if u and u in rev:
            norm_choices.append(rev[u])
        elif u:
            norm_choices.append("オリジナル")
        else:
            norm_choices.append(DEFAULT_CHOICES[i])

    # upload names (UI only)
    upload_names = hero.get("hero_upload_names", [])
    if not isinstance(upload_names, list):
        upload_names = []
    while len(upload_names) < 4:
        upload_names.append("")
    upload_names = [str(n)[:120] for n in upload_names[:4]]

    # resolve urls (always length 4)
    resolved: list[str] = []
    for i in range(4):
        ch = norm_choices[i]
        if ch == "オリジナル":
            u = hero_urls[i].strip()
            if u:
                resolved.append(u)
            else:
                resolved.append(HERO_IMAGE_PRESET_URLS.get(DEFAULT_CHOICES[i], HERO_IMAGE_DEFAULT))
        else:
            resolved.append(HERO_IMAGE_PRESET_URLS.get(ch, HERO_IMAGE_PRESET_URLS.get(DEFAULT_CHOICES[i], HERO_IMAGE_DEFAULT)))

    hero["hero_slide_choices"] = norm_choices
    hero["hero_image_urls"] = resolved
    hero["hero_upload_names"] = upload_names
    hero["hero_image_url"] = resolved[0] if resolved else ""
    hero.setdefault("hero_image", norm_choices[0] if norm_choices else DEFAULT_CHOICES[0])  # legacy

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
    philosophy.setdefault("image_upload_name", "")

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
    services.setdefault("image_upload_name", "")
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
    access.setdefault("embed_map", True)  # v0.6.995: GoogleMap iframe（任意 / 重い場合あり）
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
        border = "rgba(255, 255, 255, 0.30)"
        line = "rgba(15, 23, 42, 0.10)"

        # カード（＝各ブロック枠）をもう少し透明に（体感で約+50%）
        card = "linear-gradient(180deg, rgba(255, 255, 255, 0.28), rgba(255, 255, 255, 0.18))"
        chip_bg = "rgba(255, 255, 255, 0.26)"
        chip_border = "rgba(255, 255, 255, 0.24)"
        shadow = "0 20px 60px rgba(15, 23, 42, 0.10)"

        blob3 = "rgba(255, 255, 255, 0.22)"
        blob4_hex = _blend_hex(accent2, "#ffffff", 0.55)
        r4, g4, b4 = _hex_to_rgb(blob4_hex)

        primary_weak = f"rgba({r1}, {g1}, {b1}, 0.14)"

        bg_img = (
    f"radial-gradient(1000px 720px at 12% 10%, rgba({r1}, {g1}, {b1}, 0.16), transparent 62%),"
    f"radial-gradient(920px 680px at 90% 12%, rgba({r2}, {g2}, {b2}, 0.12), transparent 62%),"
    f"radial-gradient(760px 520px at 58% 52%, rgba({r4}, {g4}, {b4}, 0.16), transparent 64%),"
    f"radial-gradient(860px 560px at 12% 92%, rgba(255, 255, 255, 0.22), transparent 64%),"
    f"radial-gradient(520px 520px at 84% 20%, rgba(255, 255, 255, 0.28), rgba(255, 255, 255, 0.00) 72%),"
    f"radial-gradient(420px 420px at 18% 72%, rgba(255, 255, 255, 0.18), rgba(255, 255, 255, 0.00) 70%),"
    f"radial-gradient(360px 360px at 86% 78%, rgba(255, 255, 255, 0.00) 52%, rgba(255, 255, 255, 0.32) 56%, rgba(255, 255, 255, 0.00) 68%),"
    f"radial-gradient(280px 280px at 22% 38%, rgba({r2}, {g2}, {b2}, 0.10) 0%, rgba({r2}, {g2}, {b2}, 0.10) 36%, transparent 37%),"
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

        card = "linear-gradient(180deg, rgba(15, 23, 42, 0.48), rgba(15, 23, 42, 0.32))"
        chip_bg = "rgba(255, 255, 255, 0.09)"
        chip_border = "rgba(255, 255, 255, 0.16)"
        shadow = "0 22px 80px rgba(0, 0, 0, 0.42)"

        blob3 = "rgba(255, 255, 255, 0.09)"
        blob4_hex = _blend_hex(accent2, "#0b1220", 0.35)
        r4, g4, b4 = _hex_to_rgb(blob4_hex)

        primary_weak = f"rgba({r1}, {g1}, {b1}, 0.18)"

        bg_img = (
    f"radial-gradient(1000px 720px at 12% 10%, rgba({r1}, {g1}, {b1}, 0.12), transparent 62%),"
    f"radial-gradient(920px 680px at 90% 12%, rgba({r2}, {g2}, {b2}, 0.10), transparent 62%),"
    f"radial-gradient(760px 520px at 58% 52%, rgba({r4}, {g4}, {b4}, 0.12), transparent 64%),"
    f"radial-gradient(860px 560px at 12% 92%, rgba(255, 255, 255, 0.08), transparent 66%),"
    f"radial-gradient(520px 520px at 84% 20%, rgba(255, 255, 255, 0.10), rgba(255, 255, 255, 0.00) 72%),"
    f"radial-gradient(420px 420px at 18% 72%, rgba(255, 255, 255, 0.08), rgba(255, 255, 255, 0.00) 70%),"
    f"radial-gradient(360px 360px at 86% 78%, rgba(255, 255, 255, 0.00) 52%, rgba(255, 255, 255, 0.12) 56%, rgba(255, 255, 255, 0.00) 68%),"
    f"radial-gradient(280px 280px at 22% 38%, rgba({r2}, {g2}, {b2}, 0.08) 0%, rgba({r2}, {g2}, {b2}, 0.08) 36%, transparent 37%),"
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
def render_preview(p: dict, mode: str = "pc", *, root_id: Optional[str] = None) -> None:
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

    # mode / root id（プレビュー統合のため、root_id を外から差し替え可能にする）
    mode = str(mode or "mobile").strip() or "mobile"
    if mode not in ("mobile", "pc"):
        mode = "mobile"
    root_id = str(root_id or f"pv-root-{mode}").strip() or f"pv-root-{mode}"

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
    favicon_url = _clean(step2.get("favicon_url")) or DEFAULT_FAVICON_DATA_URL
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

    news = blocks.get("news", {}) if isinstance(blocks.get("news"), dict) else {}
    news_items = _safe_list(news.get("items"))  # list[dict]

    philosophy = blocks.get("philosophy", {}) if isinstance(blocks.get("philosophy"), dict) else {}
    about_title = _clean(philosophy.get("title"), "私たちについて")
    about_body = _clean(philosophy.get("body"))
    about_points = _safe_list(philosophy.get("points"))

    about_image_url = _clean(
        philosophy.get("image_url"),
        # default: wood/forest vibe
        "https://images.unsplash.com/photo-1441974231531-c6227db76b6e?auto=format&fit=crop&w=1280&h=720&q=60",
    )

    services = philosophy.get("services") if isinstance(philosophy.get("services"), dict) else {}
    svc_title = _clean(services.get("title"), "業務内容")
    svc_lead = _clean(services.get("lead"))
    svc_image_url = _clean(
        services.get("image_url"),
        "https://images.unsplash.com/photo-1524758631624-e2822e304c36?auto=format&fit=crop&w=1280&h=720&q=60",
    )
    svc_items = _safe_list(services.get("items"))

    faq = blocks.get("faq", {}) if isinstance(blocks.get("faq"), dict) else {}
    faq_items = _safe_list(faq.get("items"))

    access = blocks.get("access", {}) if isinstance(blocks.get("access"), dict) else {}
    access_notes = _clean(access.get("notes"))
    map_url = _clean(access.get("map_url"))
    if not map_url and address:
        map_url = f"https://www.google.com/maps/search/?api=1&query={quote_plus(address)}"

    # v0.6.995: GoogleMap iframe（任意 / 重い場合あり）
    try:
        map_embed = bool(access.get("embed_map", True))
    except Exception:
        map_embed = True

    contact = blocks.get("contact", {}) if isinstance(blocks.get("contact"), dict) else {}
    contact_message = _clean(contact.get("message"))
    contact_hours = _clean(contact.get("hours"))
    contact_btn = _clean(contact.get("button_text"), "お問い合わせ")

    # -------- render --------
    dark_class = " pv-dark" if is_dark else ""

    with ui.element("div").classes(f"pv-shell pv-layout-260218 pv-mode-{mode}{dark_class}").props(f'id="{root_id}"').style(theme_style):
        # scroll container (header sticky)
        with ui.element("div").classes("pv-scroll"):
            # ----- header -----
            with ui.element("header").classes("pv-topbar pv-topbar-260218"):
                with ui.row().classes("pv-topbar-inner items-center justify-between"):
                    # brand (favicon + name)
                    with ui.row().classes("items-center no-wrap pv-brand").on("click", lambda e: scroll_to("top")):
                        if favicon_url:
                            ui.image(favicon_url).classes("pv-favicon")
                        ui.label(company_name).classes("pv-brand-name")

                    if mode == "pc":
                        # desktop nav (PC only)
                        with ui.row().classes("pv-desktop-nav items-center no-wrap"):
                            for label, sec in [
                                ("私たちについて", "about"),
                                ("業務内容", "services"),
                                ("お知らせ", "news"),
                                ("FAQ", "faq"),
                                ("アクセス", "access"),
                            ]:
                                ui.button(label, on_click=lambda s=sec: scroll_to(s)).props("flat no-caps").classes("pv-desktop-nav-btn")
                            ui.button("お問い合わせ", on_click=lambda: scroll_to("contact")).props(
                                "no-caps outline color=primary"
                            ).classes("pv-desktop-nav-btn pv-nav-contact")
                    else:
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

            # ----- HERO (full width / no buttons) -----
            with ui.element("section").classes("pv-hero-wide").props('id="pv-top"'):
                slider_id = f"pv-hero-slider-{mode}"
                with ui.element("div").classes("pv-hero-slider pv-hero-slider-wide").props(f'id="{slider_id}"'):
                    with ui.element("div").classes("pv-hero-track"):
                        for url in hero_urls:
                            with ui.element("div").classes("pv-hero-slide"):
                                ui.image(url).classes("pv-hero-img")

                # init slider (auto)
                axis = "y" if mode == "mobile" else "x"
                ui.run_javascript(f"window.cvhbInitHeroSlider && window.cvhbInitHeroSlider('{slider_id}','{axis}',4500)")

                # caption overlay
                with ui.element("div").classes("pv-hero-caption"):
                    ui.label(_clean(catch_copy, company_name)).classes("pv-hero-caption-title")
                    if sub_catch:
                        ui.label(sub_catch).classes("pv-hero-caption-sub")

            # ----- main -----
            with ui.element("main").classes("pv-main"):
                # NEWS
                with ui.element("section").classes("pv-section").props('id="pv-news"'):
                    with ui.element("div").classes("pv-section-head"):
                        ui.label("お知らせ").classes("pv-section-title")
                        ui.label("NEWS").classes("pv-section-en")
                    with ui.element("div").classes("pv-panel pv-panel-glass"):
                        if not news_items:
                            with ui.row().classes("items-center justify-between"):
                                ui.label("まだお知らせがありません").classes("pv-muted")
                        else:
                            # show latest (mobile:3 / pc:4)
                            shown = news_items[:3] if mode == "mobile" else news_items[:4]
                            with ui.element("div").classes("pv-news-list"):
                                for it in shown:
                                    # it は dict を想定するが、古いデータで str が混ざっても落とさない
                                    if isinstance(it, dict):
                                        date = _clean(it.get("date"))
                                        cat = _clean(it.get("category"))
                                        title = _clean(it.get("title"), "お知らせ")
                                    else:
                                        date = ""
                                        cat = ""
                                        title = _clean(it, "お知らせ")

                                    with ui.element("div").classes("pv-news-item"):
                                        d_el = ui.label(date or "").classes("pv-news-date")
                                        if not date:
                                            d_el.classes("pv-news-empty")
                                        c_el = ui.label(cat or "").classes("pv-news-cat")
                                        if not cat:
                                            c_el.classes("pv-news-empty")
                                        ui.label(title).classes("pv-news-title")
                                        ui.icon("chevron_right").classes("pv-news-arrow")
                        with ui.row().classes("justify-end"):
                            ui.button("お知らせ一覧", on_click=lambda: None).props("flat no-caps color=primary").classes("pv-link-btn")

                # ABOUT
                with ui.element("section").classes("pv-section").props('id="pv-about"'):
                    with ui.element("div").classes("pv-section-head"):
                        ui.label(about_title).classes("pv-section-title")
                        ui.label("ABOUT").classes("pv-section-en")

                    # 並び：見出し → 画像 → 要点 → 本文
                    with ui.element("div").classes("pv-panel pv-panel-glass"):
                        if about_image_url:
                            ui.image(about_image_url).classes("pv-about-img q-mb-sm")

                        # points (cards)
                        if about_points:
                            with ui.element("div").classes("pv-points"):
                                for pt in about_points:
                                    pt = _clean(pt)
                                    if not pt:
                                        continue
                                    with ui.element("div").classes("pv-point-card"):
                                        ui.label(pt).classes("pv-point-text")

                        if about_body:
                            ui.label(about_body).classes("pv-bodytext q-mt-sm")
                        else:
                            ui.label("ここに文章が入ります。").classes("pv-muted q-mt-sm")

                # SERVICES
                with ui.element("section").classes("pv-section").props('id="pv-services"'):
                    with ui.element("div").classes("pv-section-head"):
                        ui.label(svc_title).classes("pv-section-title")
                        ui.label("SERVICE").classes("pv-section-en")

                    # 並び：業務内容タイトル → 画像 → リード文 → 項目
                    with ui.element("div").classes("pv-panel pv-panel-glass"):
                        if svc_image_url:
                            ui.image(svc_image_url).classes("pv-services-img q-mb-sm")

                        if svc_lead:
                            ui.label(svc_lead).classes("pv-bodytext")

                        if svc_items:
                            with ui.element("div").classes("pv-service-list q-mt-sm"):
                                for item in svc_items:
                                    # item は dict を想定するが、古いデータで str が混ざっても落とさない
                                    if isinstance(item, dict):
                                        t = _clean(item.get("title"))
                                        b = _clean(item.get("body"))
                                    else:
                                        t = _clean(item)
                                        b = ""
                                    if not t and not b:
                                        continue
                                    with ui.element("div").classes("pv-service-item"):
                                        if t:
                                            ui.label(t).classes("pv-service-title")
                                        if b:
                                            ui.label(b).classes("pv-muted")
                        else:
                            ui.label("業務内容を入力すると、ここに表示されます。").classes("pv-muted")

                # FAQ
                with ui.element("section").classes("pv-section").props('id="pv-faq"'):
                    with ui.element("div").classes("pv-section-head"):
                        ui.label("よくある質問").classes("pv-section-title")
                        ui.label("FAQ").classes("pv-section-en")

                    with ui.element("div").classes("pv-panel pv-panel-glass"):
                        if not faq_items:
                            ui.label("まだFAQがありません。").classes("pv-muted")
                        else:
                            with ui.element("div").classes("pv-faq-list"):
                                for it in faq_items:
                                    # it は dict を想定するが、古いデータで str が混ざっても落とさない
                                    if isinstance(it, dict):
                                        q = _clean(it.get("q"))
                                        a = _clean(it.get("a"))
                                    else:
                                        q = _clean(it)
                                        a = ""
                                    if not q and not a:
                                        continue
                                    with ui.element("div").classes("pv-faq-item"):
                                        if q:
                                            ui.label(f"Q. {q}").classes("pv-faq-q")
                                        if a:
                                            ui.label(a).classes("pv-faq-a")

                # ACCESS
                with ui.element("section").classes("pv-section").props('id="pv-access"'):
                    with ui.element("div").classes("pv-section-head"):
                        ui.label("アクセス").classes("pv-section-title")
                        ui.label("ACCESS").classes("pv-section-en")

                    with ui.element("div").classes("pv-panel pv-panel-glass pv-access-card"):
                        # 会社情報（アクセスに統合）
                        ui.label(company_name).classes("pv-access-company")
                        if address:
                            ui.label(address).classes("pv-bodytext")
                        else:
                            ui.label("住所を入力すると、ここに表示されます。").classes("pv-muted")

                        if phone or email:
                            with ui.row().classes("pv-access-meta items-center q-gutter-md q-mt-sm"):
                                if phone:
                                    with ui.row().classes("items-center q-gutter-xs"):
                                        ui.icon("call").classes("pv-access-icon")
                                        ui.label(phone).classes("pv-muted")
                                if email:
                                    with ui.row().classes("items-center q-gutter-xs"):
                                        ui.icon("mail").classes("pv-access-icon")
                                        ui.label(email).classes("pv-muted")

                        if access_notes:
                            ui.label(access_notes).classes("pv-muted q-mt-sm")

                        # 地図：住所がある時は必ず表示（iframe は任意）
                        if address:
                            _murl = map_url or f"https://www.google.com/maps/search/?api=1&query={quote_plus(address)}"
                            iframe_src = f"https://www.google.com/maps?q={quote_plus(address)}&output=embed"

                            if map_embed:
                                with ui.element("div").classes("pv-mapframe pv-mapframe-live"):
                                    ui.element("iframe").classes("pv-map-iframe").props(
                                        f'src="{iframe_src}" loading="lazy" referrerpolicy="no-referrer-when-downgrade"'
                                    )
                                    with ui.element("div").classes("pv-mapframe-ui"):
                                        ui.label("MAP").classes("pv-mapframe-badge")
                                        with ui.element("div").classes("pv-mapframe-bottom"):
                                            ui.label(address).classes("pv-mapframe-label")
                                            with ui.element("a").props(
                                                f'href="{_murl}" target="_blank" rel="noopener"'
                                            ).classes("pv-mapframe-open"):
                                                ui.label("地図を開く")
                            else:
                                with ui.element("a").props(
                                    f'href="{_murl}" target="_blank" rel="noopener"'
                                ).classes("pv-mapframe-link"):
                                    with ui.element("div").classes("pv-mapframe"):
                                        ui.label("MAP").classes("pv-mapframe-badge")
                                        ui.icon("place").classes("pv-mapframe-pin")
                                        with ui.element("div").classes("pv-mapframe-bottom"):
                                            ui.label(address).classes("pv-mapframe-label")
                                            ui.label("地図を開く").classes("pv-mapframe-open")

                            # どちらの場合も「地図を開く」を保証
                            with ui.element("a").props(
                                f'href="{_murl}" target="_blank" rel="noopener"'
                            ).classes("pv-map-openlink"):
                                ui.icon("open_in_new")
                                ui.label("地図を開く（Googleマップ）")


                # CONTACT
                with ui.element("section").classes("pv-section").props('id="pv-contact"'):
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

            # LEGAL: プライバシーポリシー（プレビュー内モーダル / v0.6.994）
            privacy_contact = ""
            try:
                if address:
                    privacy_contact += f"\n- 住所: {address}"
                if phone:
                    privacy_contact += f"\n- 電話: {phone}"
                if email:
                    privacy_contact += f"\n- メール: {email}"
            except Exception:
                privacy_contact = ""
            if not privacy_contact:
                privacy_contact = "\n- 連絡先: このページのお問い合わせ欄をご確認ください。"

            privacy_md = f"""※ これは公開用のたたき台（テンプレート）です。公開前に必ず内容を確認し、必要に応じて専門家へご相談ください。

## 1. 取得する情報
{company_name}（以下「当社」）は、お問い合わせ等を通じて、氏名、連絡先（電話番号/メールアドレス）、お問い合わせ内容などの情報を取得することがあります。

## 2. 利用目的
当社は取得した個人情報を、以下の目的の範囲で利用します。

- お問い合わせへの回答・必要な連絡のため
- サービス提供・ご案内のため
- 品質向上・改善のため（必要な範囲）

## 3. 第三者提供
当社は、法令に基づく場合を除き、ご本人の同意なく個人情報を第三者に提供しません。

## 4. 委託
当社は、利用目的の達成に必要な範囲で、個人情報の取り扱いを外部事業者に委託することがあります。その場合、適切な委託先を選定し、必要かつ適切な監督を行います。

## 5. Cookie等の利用
当社サイトでは、利便性向上やアクセス解析等のために Cookie 等の技術を使用する場合があります。ブラウザ設定により Cookie を無効にすることができますが、その場合は一部機能が利用できないことがあります。

## 6. 安全管理
当社は、個人情報の漏えい、滅失、毀損等を防止するため、合理的な安全管理措置を講じます。

## 7. 開示・訂正・利用停止等
ご本人から、個人情報の開示、訂正、追加、削除、利用停止等のご請求があった場合、所定の手続きにより対応します。

## 8. 外部リンク
当社サイトから外部サイトへリンクする場合があります。リンク先における個人情報の取り扱いについて、当社は責任を負いません。

## 9. 改定
当社は、必要に応じて本ポリシーの内容を改定することがあります。

## 10. お問い合わせ窓口
{company_name}{privacy_contact}
"""

            with ui.dialog() as privacy_dialog:
                with ui.card().classes("q-pa-md").style("max-width: 900px; width: calc(100vw - 24px);"):
                    ui.label("プライバシーポリシー").classes("text-h6 q-mb-sm")
                    ui.markdown(privacy_md).classes("pv-legal-md")
                    ui.button("閉じる", on_click=privacy_dialog.close).props("outline no-caps").classes("q-mt-md")

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
                        ui.button("プライバシーポリシー", on_click=privacy_dialog.open).props("flat no-caps").classes("pv-footer-link text-white")
                ui.label(f"© {datetime.now().year} {company_name}. All rights reserved.").classes("pv-footer-copy")

def render_main(u: User) -> None:
    inject_global_styles()
    cleanup_user_storage()

    render_header(u)

    p = get_current_project(u)

    preview_ref = {"refresh": (lambda: None)}

    editor_ref = {"refresh": (lambda: None)}

    def refresh_preview() -> None:
        # プレビューは1つに統合（表示モードだけ切替）
        try:
            preview_ref["refresh"]()
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

                                def bind_dict_input(target: dict, label: str, field: str, *, textarea: bool = False, hint: str = "") -> None:
                                    """Bind ui.input directly to a dict field (used for nested blocks like philosophy/services)."""
                                    if not isinstance(target, dict):
                                        return
                                    val = target.get(field, "")

                                    def _on_change(e):
                                        try:
                                            target[field] = e.value or ""
                                        except Exception:
                                            pass
                                        update_and_refresh()

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
                                            ui.label("※作成途中の業種変更は、3.ページ内容詳細設定（ブロックごと）がリセットされます。").classes("text-negative text-caption q-mb-sm")

                                            @ui.refreshable
                                            def industry_selector():
                                                current_industry = step1.get("industry", "会社サイト（企業）")

                                                def set_industry(value: str) -> None:
                                                    # 業種変更でテンプレが変わる場合は、Step3（ブロック編集）をリセットする
                                                    prev_tpl = step1.get("_applied_template_id") or step1.get("template_id") or resolve_template_id(step1) or ""

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
                                                    next_tpl = step1.get("template_id") or ""

                                                    if next_tpl and next_tpl != prev_tpl:
                                                        try:
                                                            blocks.clear()
                                                        except Exception:
                                                            pass
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
                                                        prev_tpl = step1.get("_applied_template_id") or step1.get("template_id") or resolve_template_id(step1) or ""
                                                        step1["welfare_domain"] = v
                                                        step1["template_id"] = resolve_template_id(step1)
                                                        next_tpl = step1.get("template_id") or ""
                                                        if next_tpl and next_tpl != prev_tpl:
                                                            try:
                                                                blocks.clear()
                                                            except Exception:
                                                                pass
                                                        update_and_refresh()
                                                        industry_selector.refresh()

                                                    def set_mode(v: str) -> None:
                                                        prev_tpl = step1.get("_applied_template_id") or step1.get("template_id") or resolve_template_id(step1) or ""
                                                        step1["welfare_mode"] = v
                                                        step1["template_id"] = resolve_template_id(step1)
                                                        next_tpl = step1.get("template_id") or ""
                                                        if next_tpl and next_tpl != prev_tpl:
                                                            try:
                                                                blocks.clear()
                                                            except Exception:
                                                                pass
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
                                            # ファビコン（アップロード仕様）
                                            ui.label("ファビコン（任意）").classes("text-body1 q-mt-sm")
                                            ui.label("未設定ならデフォルトを使用します（32×32推奨）").classes("cvhb-muted")

                                            async def _on_upload_favicon(e):
                                                try:
                                                    data_url, fname = await _upload_event_to_data_url(e)
                                                    if not data_url:
                                                        return
                                                    step2["favicon_url"] = data_url
                                                    step2["favicon_filename"] = _short_name(fname)
                                                    update_and_refresh()
                                                    favicon_editor.refresh()
                                                except Exception:
                                                    pass

                                            def _clear_favicon():
                                                try:
                                                    step2["favicon_url"] = ""
                                                    step2["favicon_filename"] = ""
                                                except Exception:
                                                    pass
                                                update_and_refresh()
                                                favicon_editor.refresh()

                                            @ui.refreshable
                                            def favicon_editor():
                                                cur = str(step2.get("favicon_url") or "").strip()
                                                name = str(step2.get("favicon_filename") or "").strip()
                                                show_url = cur or DEFAULT_FAVICON_DATA_URL
                                                with ui.row().classes("items-center q-gutter-sm"):
                                                    ui.image(show_url).style("width:32px;height:32px;border-radius:6px;")
                                                    ui.upload(on_upload=_on_upload_favicon, auto_upload=True).props("accept=image/*")
                                                    ui.button("クリア", on_click=_clear_favicon).props("outline dense")
                                                ui.label(f"現在: {'デフォルト' if not cur else ('オリジナル(' + (name or 'アップロード') + ')')}").classes("cvhb-muted")

                                            favicon_editor()
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
                                                        hero = blocks.setdefault("hero", {})
                                                        ui.label("ヒーロー（ページ最上部）").classes("text-subtitle1 q-mb-sm")

                                                        # ヒーロー画像（4枚固定：プリセット or オリジナルアップロード）
                                                        DEFAULT_CHOICES = ["A: オフィス", "B: チーム", "C: 街並み", "D: ひかり"]
                                                        hero.setdefault("hero_slide_choices", DEFAULT_CHOICES.copy())
                                                        hero.setdefault("hero_upload_names", ["", "", "", ""])
                                                        hero.setdefault("hero_image_urls", hero.get("hero_image_urls") or [])

                                                        def _normalize_hero_slides():
                                                            cc = _safe_list(hero.get("hero_slide_choices"))
                                                            uu = _safe_list(hero.get("hero_image_urls"))
                                                            nn = _safe_list(hero.get("hero_upload_names"))
                                                            # ensure length 4
                                                            while len(cc) < 4:
                                                                cc.append(DEFAULT_CHOICES[len(cc)])
                                                            cc = cc[:4]
                                                            while len(uu) < 4:
                                                                idx = len(uu)
                                                                choice = str(cc[idx] or "").strip()
                                                                if choice and choice != "オリジナル":
                                                                    uu.append(HERO_IMAGE_PRESET_URLS.get(choice, HERO_IMAGE_PRESET_URLS.get(DEFAULT_CHOICES[idx], HERO_IMAGE_DEFAULT)))
                                                                else:
                                                                    uu.append(HERO_IMAGE_PRESET_URLS.get(DEFAULT_CHOICES[idx], HERO_IMAGE_DEFAULT))
                                                            uu = uu[:4]
                                                            while len(nn) < 4:
                                                                nn.append("")
                                                            nn = nn[:4]
                                                            hero["hero_slide_choices"] = cc
                                                            hero["hero_image_urls"] = uu
                                                            hero["hero_upload_names"] = nn
                                                            hero["hero_image_url"] = uu[0] if uu else ""
                                                            hero["hero_image"] = cc[0] if cc else DEFAULT_CHOICES[0]

                                                        _normalize_hero_slides()

                                                        def _set_slide_choice(i: int, val: str):
                                                            _normalize_hero_slides()
                                                            cc = hero["hero_slide_choices"]
                                                            uu = hero["hero_image_urls"]
                                                            nn = hero["hero_upload_names"]
                                                            cc[i] = val
                                                            if val != "オリジナル":
                                                                uu[i] = HERO_IMAGE_PRESET_URLS.get(val, HERO_IMAGE_PRESET_URLS.get(DEFAULT_CHOICES[i], HERO_IMAGE_DEFAULT))
                                                                nn[i] = ""
                                                            else:
                                                                if not str(uu[i] or "").strip():
                                                                    uu[i] = HERO_IMAGE_PRESET_URLS.get(DEFAULT_CHOICES[i], HERO_IMAGE_DEFAULT)
                                                            hero["hero_slide_choices"] = cc
                                                            hero["hero_image_urls"] = uu
                                                            hero["hero_upload_names"] = nn
                                                            hero["hero_image_url"] = uu[0] if uu else ""
                                                            hero["hero_image"] = cc[0] if cc else DEFAULT_CHOICES[0]
                                                            update_and_refresh()
                                                            hero_slides_editor.refresh()

                                                        async def _on_upload_slide(e, i: int):
                                                            try:
                                                                data_url, fname = await _upload_event_to_data_url(e, max_w=IMAGE_MAX_W, max_h=IMAGE_MAX_H)
                                                                if not data_url:
                                                                    return
                                                                _normalize_hero_slides()
                                                                hero["hero_slide_choices"][i] = "オリジナル"
                                                                hero["hero_image_urls"][i] = data_url
                                                                hero["hero_upload_names"][i] = _short_name(fname)
                                                                hero["hero_image_url"] = hero["hero_image_urls"][0] if hero.get("hero_image_urls") else ""
                                                                hero["hero_image"] = hero["hero_slide_choices"][0] if hero.get("hero_slide_choices") else DEFAULT_CHOICES[0]
                                                                update_and_refresh()
                                                                hero_slides_editor.refresh()
                                                            except Exception:
                                                                pass

                                                        def _clear_slide_upload(i: int):
                                                            try:
                                                                _normalize_hero_slides()
                                                                hero["hero_slide_choices"][i] = DEFAULT_CHOICES[i]
                                                                hero["hero_image_urls"][i] = HERO_IMAGE_PRESET_URLS.get(DEFAULT_CHOICES[i], HERO_IMAGE_DEFAULT)
                                                                hero["hero_upload_names"][i] = ""
                                                                hero["hero_image_url"] = hero["hero_image_urls"][0] if hero.get("hero_image_urls") else ""
                                                                hero["hero_image"] = hero["hero_slide_choices"][0] if hero.get("hero_slide_choices") else DEFAULT_CHOICES[0]
                                                            except Exception:
                                                                pass
                                                            update_and_refresh()
                                                            hero_slides_editor.refresh()

                                                        @ui.refreshable
                                                        def hero_slides_editor():
                                                            _normalize_hero_slides()
                                                            cc = hero["hero_slide_choices"]
                                                            uu = hero["hero_image_urls"]
                                                            nn = hero["hero_upload_names"]
                                                            ui.label("ヒーロー画像（4枚固定）").classes("text-body1")
                                                            ui.label("スマホ：縦スライド／PC：横スライド").classes("cvhb-muted")
                                                            ui.label(f"{IMAGE_RECOMMENDED_TEXT}").classes("cvhb-muted")
                                                            for _i in range(4):
                                                                with ui.card().classes("q-pa-sm q-mb-sm").props("flat bordered"):
                                                                    ui.label(f"画像{_i+1}").classes("text-subtitle2")
                                                                    def _on_choice(e, i=_i):
                                                                        _set_slide_choice(i, e.value)
                                                                    ui.radio(HERO_IMAGE_OPTIONS + ["オリジナル"], value=cc[_i], on_change=_on_choice).props("inline")
                                                                    if cc[_i] == "オリジナル":
                                                                        async def _upload_handler(e, i=_i):
                                                                            await _on_upload_slide(e, i)
                                                                        with ui.row().classes("items-center q-gutter-sm"):
                                                                            # 現在反映されている画像（サムネ）
                                                                            try:
                                                                                ui.image(uu[_i]).style("width:120px;height:68px;object-fit:cover;border-radius:10px;border:1px solid rgba(0,0,0,0.08);")
                                                                            except Exception:
                                                                                pass
                                                                            ui.upload(on_upload=_upload_handler, auto_upload=True).props("accept=image/*")
                                                                            ui.button("クリア", on_click=lambda i=_i: _clear_slide_upload(i)).props("outline dense")
                                                                            ui.button("反映して保存", icon="save", on_click=lambda: (refresh_preview(), save_now())).props("color=primary unelevated dense no-caps")
                                                                        ui.label(f"ファイル: {nn[_i] or '未アップロード'}").classes("cvhb-muted")
                                                                    else:
                                                                        ui.label(f"選択中: {cc[_i]}").classes("cvhb-muted")

                                                        hero_slides_editor()

                                                        with ui.row().classes("items-center q-gutter-sm q-mt-sm"):
                                                            ui.button("画像を反映して保存", icon="save", on_click=lambda: (refresh_preview(), save_now())).props("color=primary unelevated no-caps")
                                                            ui.label("※アップロード後は、このボタンで保存すると安心です。").classes("cvhb-muted")

                                                        # キャッチは Step2 に保存しているが、ここ（ヒーロー）でも編集できるようにする
                                                        bind_step2_input(
                                                            "キャッチコピー",
                                                            "catch_copy",
                                                            hint="ヒーローの一番大きい文章です。スマホは画像の下、PCは画像に重ねて表示されます。",
                                                        )
                                                        bind_block_input("hero", "サブキャッチ（任意）", "sub_catch")
                                                        ui.label("※ ヒーロー内のボタン表示は v0.6.98 で廃止しました（後で必要になったら復活できます）。").classes("cvhb-muted q-mt-sm")

                                                    with ui.tab_panel("philosophy"):
                                                        # 理念/概要（必須）
                                                        ph = blocks.setdefault("philosophy", {})
                                                        ph.setdefault("title", "")
                                                        ph.setdefault("body", "")
                                                        ph.setdefault("image_url", "")
                                                        ph.setdefault("image_upload_name", "")
                                                        bind_dict_input(ph, "見出し（必須）", "title", hint="例：私たちについて")
                                                        
                                                        # 画像（アップロード仕様）
                                                        ui.label("画像（任意）").classes("text-body1 q-mt-sm")
                                                        ui.label("未設定ならデフォルト（E: 木）を使用").classes("cvhb-muted")
                                                        ui.label(IMAGE_RECOMMENDED_TEXT).classes("cvhb-muted")

                                                        async def _on_upload_ph_image(e):
                                                            try:
                                                                data_url, fname = await _upload_event_to_data_url(e, max_w=IMAGE_MAX_W, max_h=IMAGE_MAX_H)
                                                                if not data_url:
                                                                    return
                                                                ph["image_url"] = data_url
                                                                ph["image_upload_name"] = _short_name(fname)
                                                                update_and_refresh()
                                                                ph_image_editor.refresh()
                                                            except Exception:
                                                                pass

                                                        def _clear_ph_image():
                                                            try:
                                                                ph["image_url"] = ""
                                                                ph["image_upload_name"] = ""
                                                            except Exception:
                                                                pass
                                                            update_and_refresh()
                                                            ph_image_editor.refresh()

                                                        @ui.refreshable
                                                        def ph_image_editor():
                                                            cur = str(ph.get("image_url") or "").strip()
                                                            name = str(ph.get("image_upload_name") or "").strip()
                                                            show_url = cur or "https://images.unsplash.com/photo-1441974231531-c6227db76b6e?auto=format&fit=crop&w=1280&h=720&q=60"
                                                            with ui.row().classes("items-center q-gutter-sm"):
                                                                # 現在反映されている画像（サムネ）
                                                                try:
                                                                    ui.image(show_url).style("width:120px;height:68px;object-fit:cover;border-radius:10px;border:1px solid rgba(0,0,0,0.08);")
                                                                except Exception:
                                                                    pass
                                                                ui.upload(on_upload=_on_upload_ph_image, auto_upload=True).props("accept=image/*")
                                                                ui.button("クリア", on_click=_clear_ph_image).props("outline dense")
                                                                ui.button("反映して保存", icon="save", on_click=lambda: (refresh_preview(), save_now())).props("color=primary unelevated dense no-caps")
                                                            ui.label(f"現在: {'デフォルト(E: 木)' if not cur else ('オリジナル(' + (name or 'アップロード') + ')')}").classes("cvhb-muted")

                                                        ph_image_editor()

                                                        # 要点（任意 / 3つまで）
                                                        ui.label("要点（任意 / 3つまで）").classes("text-body1 q-mt-sm")
                                                        points = _safe_list(ph.get("points"))
                                                        if not points:
                                                            points = ["", "", ""]
                                                        while len(points) < 3:
                                                            points.append("")
                                                        points = points[:3]
                                                        ph["points"] = points

                                                        def _set_point(i: int, v: str):
                                                            ps = _safe_list(ph.get("points"))
                                                            while len(ps) < 3:
                                                                ps.append("")
                                                            ps[i] = v
                                                            ph["points"] = ps[:3]
                                                            update_and_refresh()

                                                        for i in range(3):
                                                            ui.input(f"要点{i+1}", value=points[i], on_change=lambda e, i=i: _set_point(i, e.value)).props("dense")

                                                        bind_dict_input(ph, "本文（必須）", "body", textarea=True, hint="例：私たちは、〜")



                                                        ui.separator().classes("q-mt-md q-mb-sm")

                                                        # 業務内容（追加・削除可）
                                                        ui.label("業務内容（追加・削除できます / 最大6）").classes("text-body1")
                                                        svc = ph.setdefault("services", {})
                                                        svc.setdefault("title", "")
                                                        svc.setdefault("lead", "")
                                                        svc.setdefault("image_url", "")
                                                        svc.setdefault("image_upload_name", "")
                                                        bind_dict_input(svc, "業務内容：タイトル（任意）", "title", hint="例：業務内容")
                                                        
                                                        ui.label("業務内容：画像（任意）").classes("text-body2 q-mt-sm")
                                                        ui.label("未設定ならデフォルト（F: 手）を使用").classes("cvhb-muted")
                                                        ui.label(IMAGE_RECOMMENDED_TEXT).classes("cvhb-muted")

                                                        async def _on_upload_svc_image(e):
                                                            try:
                                                                data_url, fname = await _upload_event_to_data_url(e, max_w=IMAGE_MAX_W, max_h=IMAGE_MAX_H)
                                                                if not data_url:
                                                                    return
                                                                svc["image_url"] = data_url
                                                                svc["image_upload_name"] = _short_name(fname)
                                                                update_and_refresh()
                                                                svc_image_editor.refresh()
                                                            except Exception:
                                                                pass

                                                        def _clear_svc_image():
                                                            try:
                                                                svc["image_url"] = ""
                                                                svc["image_upload_name"] = ""
                                                            except Exception:
                                                                pass
                                                            update_and_refresh()
                                                            svc_image_editor.refresh()

                                                        @ui.refreshable
                                                        def svc_image_editor():
                                                            cur = str(svc.get("image_url") or "").strip()
                                                            name = str(svc.get("image_upload_name") or "").strip()
                                                            show_url = cur or "https://images.unsplash.com/photo-1524758631624-e2822e304c36?auto=format&fit=crop&w=1280&h=720&q=60"
                                                            with ui.row().classes("items-center q-gutter-sm"):
                                                                # 現在反映されている画像（サムネ）
                                                                try:
                                                                    ui.image(show_url).style("width:120px;height:68px;object-fit:cover;border-radius:10px;border:1px solid rgba(0,0,0,0.08);")
                                                                except Exception:
                                                                    pass
                                                                ui.upload(on_upload=_on_upload_svc_image, auto_upload=True).props("accept=image/*")
                                                                ui.button("クリア", on_click=_clear_svc_image).props("outline dense")
                                                                ui.button("反映して保存", icon="save", on_click=lambda: (refresh_preview(), save_now())).props("color=primary unelevated dense no-caps")
                                                            ui.label(f"現在: {'デフォルト(F: 手)' if not cur else ('オリジナル(' + (name or 'アップロード') + ')')}").classes("cvhb-muted")

                                                        svc_image_editor()


                                                        bind_dict_input(svc, "業務内容：リード文（任意）", "lead", textarea=True, hint="例：提供サービスの概要")

                                                        @ui.refreshable
                                                        def svc_items_editor():
                                                            items = svc.get("items", [])
                                                            if not isinstance(items, list):
                                                                items = []
                                                            items = [it for it in items if isinstance(it, dict)]
                                                            svc["items"] = items

                                                            def _add_item():
                                                                items2 = svc.get("items", [])
                                                                if not isinstance(items2, list):
                                                                    items2 = []
                                                                if len(items2) >= 6:
                                                                    return
                                                                items2.append({"title": "", "body": ""})
                                                                svc["items"] = items2
                                                                update_and_refresh()
                                                                svc_items_editor.refresh()

                                                            with ui.row().classes("items-center q-gutter-sm q-mt-sm"):
                                                                ui.button("＋ 追加", on_click=_add_item).props("outline dense")
                                                                ui.label("※最大6件").classes("cvhb-muted")

                                                            for idx, item in enumerate(items):
                                                                with ui.card().classes("q-pa-sm q-mb-sm").props("flat bordered"):
                                                                    with ui.row().classes("items-center justify-between"):
                                                                        ui.label(f"項目{idx+1}").classes("text-subtitle2")
                                                                        def _del(i=idx):
                                                                            items2 = svc.get("items", [])
                                                                            if not isinstance(items2, list):
                                                                                return
                                                                            if 0 <= i < len(items2):
                                                                                items2.pop(i)
                                                                                svc["items"] = items2
                                                                                update_and_refresh()
                                                                                svc_items_editor.refresh()
                                                                        ui.button("削除", on_click=_del).props("outline dense color=negative")
                                                                    ui.input("タイトル", value=item.get("title", ""), on_change=lambda e, it=item: (it.__setitem__("title", e.value), update_and_refresh())).props("dense")
                                                                    ui.textarea("本文", value=item.get("body", ""), on_change=lambda e, it=item: (it.__setitem__("body", e.value), update_and_refresh())).props("dense")

                                                        svc_items_editor()
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
                                                        ui.label("※ 住所は「2. 基本情報設定」の住所を使います").classes("cvhb-muted q-mb-xs")

                                                        acc = blocks.setdefault("access", {})
                                                        acc.setdefault("embed_map", True)

                                                        def _on_map_embed(e):
                                                            update_block("access", "embed_map", bool(e.value))

                                                        ui.switch("Googleマップを表示（任意 / 重くなる場合があります）", value=bool(acc.get("embed_map", True)), on_change=_on_map_embed).props("dense")
                                                        ui.label("※ OFF にすると軽い『地図風デザイン＋地図を開くリンク』になります。").classes("cvhb-muted q-mb-sm")

                                                        bind_block_input("access", "補足（任意）", "notes", textarea=True)

                                                    with ui.tab_panel("contact"):
                                                        ui.label("お問い合わせ").classes("text-subtitle1 q-mb-sm")
                                                        bind_block_input("contact", "受付時間（任意）", "hours")
                                                        bind_block_input("contact", "メッセージ（任意）", "message", textarea=True)

                                        editor_ref["refresh"] = block_editor_panel.refresh
                                        try:
                                            block_editor_panel()
                                        except Exception as e:
                                            # Step3 が壊れても、全体（特にプレビュー）を落とさない
                                            ui.label("ブロック編集の描画でエラーが発生しました").classes("text-negative")
                                            ui.label("プレビュー表示は継続します。").classes("cvhb-muted")
                                            ui.label(sanitize_error_text(e)).classes("cvhb-muted")
                                            traceback.print_exc()


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

                                                # プレビュー表示モード（mobile / pc）
                        preview_mode = {"value": "mobile"}

                        @ui.refreshable
                        def pv_mode_selector():
                            cur = str(preview_mode.get("value") or "mobile")
                            if cur not in ("mobile", "pc"):
                                cur = "mobile"

                            def set_mode(m: str) -> None:
                                if m not in ("mobile", "pc"):
                                    m = "mobile"
                                preview_mode["value"] = m
                                try:
                                    pv_mode_selector.refresh()
                                except Exception:
                                    pass
                                try:
                                    preview_ref["refresh"]()
                                except Exception:
                                    pass

                            with ui.row().classes("items-center q-gutter-sm q-mt-sm"):
                                props_mobile = "no-caps unelevated color=primary" if cur == "mobile" else "no-caps outline color=primary"
                                props_pc = "no-caps unelevated color=primary" if cur == "pc" else "no-caps outline color=primary"
                                ui.button("スマホ", icon="smartphone", on_click=lambda: set_mode("mobile")).props(props_mobile)
                                ui.button("PC", icon="desktop_windows", on_click=lambda: set_mode("pc")).props(props_pc)

                        pv_mode_selector()

                        @ui.refreshable
                        def preview_panel():
                            mode = str(preview_mode.get("value") or "mobile")
                            if mode not in ("mobile", "pc"):
                                mode = "mobile"

                            # デザイン上の横幅（SP=720 / PC=1920）
                            # ただし PC は、プレビュー枠が狭いと 1920 が小さくなりすぎるので
                            # 「最低 1280（最大 1920）」の範囲で縮小する（横が全部見えるのは維持）
                            design_w = 720 if mode == "mobile" else 1920
                            # 表示(縮小)ルール
                            # - スマホ: 720px をそのまま（大きくしすぎない / 中央揃え）
                            # - PC: 1920pxで作り、表示は 1440px(0.75)〜960px(0.50) の範囲に収める
                            min_scale = 0.01 if mode == "mobile" else 0.50
                            max_scale = 1.00 if mode == "mobile" else 0.75
                            radius = 22 if mode == "mobile" else 14

                            with ui.card().style(
                                f"width: 100%; height: 2400px; overflow: hidden; border-radius: {radius}px; margin: 0;"
                            ).props("flat bordered"):
                                with ui.element("div").props('id="pv-fit"').style(
                                    "height: 100%; width: 100%; display: block; overflow-x: hidden; overflow-y: hidden; position: relative; background: transparent;"
                                ):
                                    if not p:
                                        ui.label("案件を選ぶとプレビューが出ます").classes("cvhb-muted q-pa-md")
                                        return
                                    try:
                                        pre = _preview_preflight_error()
                                        if pre:
                                            ui.label("プレビューの初期化に失敗しました").classes("text-negative q-pa-md")
                                            ui.label(pre).classes("cvhb-muted q-pa-md")
                                            return

                                        # 右プレビュー本体（root_id を固定して Fit-to-width を安定化）
                                        render_preview(p, mode=mode, root_id="pv-root")

                                        # fit-to-width (design: 720px / 1920px)
                                        try:
                                            ui.run_javascript(
                                                f"window.cvhbFitRegister && window.cvhbFitRegister('pv', 'pv-fit', 'pv-root', {design_w}, 0, 0, {min_scale}, {max_scale});"
                                            )
                                        except Exception:
                                            pass
                                        try:
                                            ui.run_javascript(
                                                "setTimeout(function(){ window.cvhbFitApply && window.cvhbFitApply('pv'); }, 80);"
                                            )
                                        except Exception:
                                            pass
                                        # optional debug marker (DevTools で有効化したときだけ記録)
                                        try:
                                            ui.run_javascript(
                                                f"window.cvhbDebugLog && window.cvhbDebugLog('preview_render', {{mode: '{mode}', designW: {design_w}, minScale: {min_scale}, maxScale: {max_scale}}});"
                                            )
                                        except Exception:
                                            pass
                                    except Exception as e:
                                        ui.label("プレビューでエラーが発生しました").classes("text-negative")
                                        ui.label(sanitize_error_text(e)).classes("cvhb-muted")
                                        traceback.print_exc()

                        preview_ref["refresh"] = preview_panel.refresh
                        preview_panel()



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

                # 途中まで描画されたUIが残ると混乱するので、いったんクリアしてから復旧UIを出す
                root.clear()

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

                    # --- fallback: 画面描画で落ちても「プレビューだけ」は表示する ---
                    try:
                        u_fallback = current_user()
                        p_fallback = get_current_project(u_fallback) if u_fallback else None
                    except Exception as e2:
                        u_fallback = None
                        p_fallback = None
                        ui.separator().classes("q-my-lg")
                        ui.label("プレビュー（復旧モード）").classes("text-subtitle2")
                        ui.label("案件の取得に失敗しました").classes("text-negative")
                        ui.label(sanitize_error_text(e2)).classes("text-caption")
                        traceback.print_exc()

                    if p_fallback:
                        ui.separator().classes("q-my-lg")
                        ui.label("プレビュー（復旧モード）").classes("text-subtitle2")
                        try:
                            mode = app.storage.user.get("preview_mode", "mobile")
                            design_w = 720 if mode == "mobile" else 1920
                            fit_min_w = 720 if mode == "mobile" else 1280
                            fit_max_w = 720 if mode == "mobile" else 1920

                            with ui.card().classes("w-full").props("bordered"):
                                with ui.element("div").classes("w-full").style(
                                    f"max-width:{fit_max_w}px; min-width:{fit_min_w}px; width:100%;"
                                    "margin:0 auto; overflow:hidden; padding:12px;"
                                ):
                                    with ui.element("div").props('id="pv-fit"').style(
                                        f"max-width:{fit_max_w}px; min-width:{fit_min_w}px; width:100%;"
                                        "margin:0 auto; overflow:hidden;"
                                        "border-radius:18px;"
                                        "border:1px solid rgba(0,0,0,0.10);"
                                        "background:rgba(255,255,255,0.35);"
                                    ):
                                        try:
                                            render_preview(p_fallback, mode=mode, root_id="pv-root")
                                        except Exception as e3:
                                            ui.label("プレビュー描画でエラーが発生しました").classes("text-negative")
                                            ui.label(sanitize_error_text(e3)).classes("text-caption")
                                            traceback.print_exc()

                            ui.run_javascript(
                                f"""
try {{
  window.cvhbFitRegister && window.cvhbFitRegister('pv','pv-fit','pv-root',{design_w},{fit_min_w},{fit_max_w});
  window.cvhbFitApply && window.cvhbFitApply('pv');
}} catch (e) {{ console.warn('[cvhb] fit error', e); }}
"""
                            )
                        except Exception as e4:
                            ui.label("プレビュー（復旧モード）でエラーが発生しました").classes("text-negative")
                            ui.label(sanitize_error_text(e4)).classes("text-caption")
                            traceback.print_exc()

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
