from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import fnmatch
import secrets
import stat
import traceback
import asyncio
import mimetypes
import inspect
import zipfile
from io import BytesIO
import html
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, unquote, quote_plus

# =========================
# [HELP_MODE] オフラインでヘルプ作成するための安全スイッチ
#   - ローカルで CVHB_HELP_MODE=1 のとき有効
#   - Heroku(DYNO) では強制OFF（事故防止）
# =========================

def _env_flag(name: str) -> bool:
    v = os.getenv(name)
    if v is None:
        return False
    s = str(v).strip().lower()
    return s in {"1", "true", "yes", "on"}

HELP_MODE = _env_flag("CVHB_HELP_MODE")
if os.getenv("DYNO") and HELP_MODE:
    # 事故防止: Herokuにこの変数を入れてしまっても本番挙動が変わらないように無効化する
    print("[cvhb] HELP_MODE ignored on Heroku (DYNO detected)", flush=True)
    HELP_MODE = False

# heavy deps: 通常モードのみ必要（HELP_MODEでは未インストールでも動くようにする）
if not HELP_MODE:
    import paramiko
    import psycopg
    from psycopg.rows import dict_row
else:
    paramiko = None  # type: ignore
    psycopg = None  # type: ignore
    dict_row = None  # type: ignore

# Response: 画像/ZIPのダウンロード等で使う
# - HELP_MODE では fastapi 未インストールでも動くように、まず starlette を試す
# - どちらも無い場合は NiceGUI 自体が動かない可能性が高いが、エラーを分かりやすくする
try:
    from starlette.responses import Response  # type: ignore
except Exception:
    try:
        from fastapi import Response  # type: ignore
    except Exception:
        Response = None  # type: ignore

if Response is None:  # pragma: no cover
    class Response:  # type: ignore
        def __init__(self, *args, **kwargs):
            raise RuntimeError("Response クラスが見つかりません。まずは `pip install nicegui` を実行してください。")



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


def _maybe_resize_image_bytes(data: bytes, mime: str, *, max_w: int, max_h: int, force_png: bool = False) -> tuple[bytes, str]:
    """画像を target(max_w×max_h) に「比率を合わせてセンタークロップ + リサイズ」して返す。

    目的:
    - 画像の保存/表示の比率を 1280×720（16:9）に統一したい（ヒーロー/理念/業務内容など）
    - 元画像が縦長/横長でも、できるだけ残しつつ中心を基準にカットする（cover方式）
    - ファビコンは 32×32 の PNG（正方形）にしたい → force_png=True を使う

    仕様:
    - Pillow(PIL) が無い環境では元データを返す（安全優先）
    - 画像は EXIF の回転を補正してから処理する
    - 出力は基本: 透過あり -> PNG / 透過なし -> JPEG(quality=85)
      ただし force_png=True の場合は常に PNG を返す
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

        # --- センタークロップで target_ratio に寄せる（できるだけ残す） ---
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

        # --- target にリサイズ（小さければ拡大もする） ---
        try:
            im = im.resize((target_w, target_h), Image.LANCZOS)
        except Exception:
            try:
                im = im.resize((target_w, target_h))
            except Exception:
                pass

        from io import BytesIO  # local import（PILがあるときだけ到達）
        out = BytesIO()

        # force_png のときは常に PNG
        if force_png:
            out_mime = "image/png"
            try:
                im.save(out, format="PNG", optimize=True)
            except Exception:
                return data, mime
            out_bytes = out.getvalue()
            return (out_bytes, out_mime) if out_bytes else (data, mime)

        # 透過がある場合は PNG、それ以外は JPEG（軽量化）
        has_alpha = (
            im.mode in ("RGBA", "LA")
            or (im.mode == "P" and ("transparency" in getattr(im, "info", {})))
        )

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


def _upload_debug_summary(obj) -> str:
    """Heroku logs 向けの安全な要約（生bytes/base64は絶対に出さない）"""
    try:
        if obj is None:
            return "None"
        if isinstance(obj, (bytes, bytearray, memoryview)):
            return f"{type(obj).__name__}(len={len(obj)})"
        if isinstance(obj, str):
            s = obj.strip()
            if s.startswith("data:") and "base64," in s:
                return f"str(data_url,len={len(s)})"
            return f"str(len={len(s)})"
        if isinstance(obj, dict):
            keys = list(obj.keys())
            if len(keys) > 12:
                keys = keys[:12] + ["..."]
            return f"dict(keys={keys})"
        if isinstance(obj, (list, tuple)):
            return f"{type(obj).__name__}(len={len(obj)})"
        return type(obj).__name__
    except Exception:
        return "unknown"


def _extract_upload_event_payload(e) -> dict:
    """NiceGUI upload のイベント引数の揺れ（object / dict / list）を吸収して payload を返す。

    返り値の形:
      {"name": str, "type": str, "content": Any}

    ねらい:
    - NiceGUI側のバージョン差・イベント型の差をここで吸収する
    - 後段は「content から bytes を取れるか」だけに集中できる
    """
    # list/tuple: 先頭から「それっぽい」ものを拾う
    if isinstance(e, (list, tuple)):
        for it in e:
            p = _extract_upload_event_payload(it)
            if p.get("content") is not None or p.get("name") or p.get("type"):
                return p
        return {"name": "", "type": "", "content": None}

    # dict: キー名ゆれを吸収
    if isinstance(e, dict):
        name = str(
            e.get("name")
            or e.get("filename")
            or e.get("fileName")
            or e.get("file_name")
            or ""
        ).strip()
        mime = str(
            e.get("type")
            or e.get("mime")
            or e.get("mimetype")
            or e.get("content_type")
            or ""
        ).strip()

        content = e.get("content")
        if content is None:
            content = e.get("file")
        if content is None:
            content = e.get("data")
        if content is None:
            content = e.get("bytes")

        # 入れ子（files/args/payload/value）にも対応
        if content is None:
            for k in ("files", "args", "payload", "value"):
                if k in e:
                    nested = e.get(k)
                    p = _extract_upload_event_payload(nested)
                    # 上書きできる情報があれば反映
                    if not name:
                        name = p.get("name", "") or name
                    if not mime:
                        mime = p.get("type", "") or mime
                    if p.get("content") is not None:
                        content = p.get("content")
                        break

        return {"name": name, "type": mime, "content": content}

    # object: attribute 名ゆれを吸収
    try:
        name = str(getattr(e, "name", "") or getattr(e, "filename", "") or "").strip()
        mime = str(getattr(e, "type", "") or getattr(e, "mime", "") or "").strip()

        content = getattr(e, "content", None)
        if content is None:
            content = getattr(e, "file", None)
        if content is None:
            content = getattr(e, "data", None)
        if content is None:
            # some wrappers use .files/.args/.payload/.value
            for attr in ("files", "args", "payload", "value"):
                nested = getattr(e, attr, None)
                if nested is not None:
                    p = _extract_upload_event_payload(nested)
                    if not name:
                        name = p.get("name", "") or name
                    if not mime:
                        mime = p.get("type", "") or mime
                    if p.get("content") is not None:
                        content = p.get("content")
                        break

        # それでも取れない場合は「e 自体」を content として渡す（後段で深掘り）
        if content is None:
            content = e

        return {"name": name, "type": mime, "content": content}
    except Exception:
        return {"name": "", "type": "", "content": e}


async def _read_upload_bytes(content, *, _depth: int = 0, _seen: Optional[set[int]] = None) -> bytes:
    """Upload content から bytes を確実に取り出す（同期/非同期・dict/list の揺れを吸収）。

    重要:
    - NiceGUI/Starlette の UploadFile は read() が async のことがある（= await 必須）
    - 逆に file.read() は sync のこともある
    - ここで「両方」吸収して、必ず bytes を確保する
    """
    if content is None:
        return b""
    # 再帰ループ防止
    if _seen is None:
        _seen = set()
    try:
        obj_id = id(content)
        if obj_id in _seen:
            return b""
        _seen.add(obj_id)
    except Exception:
        pass

    if _depth > 8:
        return b""
    # bytes 直
    if isinstance(content, (bytes, bytearray, memoryview)):
        return bytes(content)

    # dict
    if isinstance(content, dict):
        # よくあるキーから優先して掘る
        for k in ("content", "data", "bytes", "file", "raw", "body", "buffer"):
            if k in content:
                b = await _read_upload_bytes(content.get(k), _depth=_depth + 1, _seen=_seen)
                if b:
                    return b

        # 入れ子（files/args/payload/value）
        for k in ("files", "args", "payload", "value"):
            if k in content:
                b = await _read_upload_bytes(content.get(k), _depth=_depth + 1, _seen=_seen)
                if b:
                    return b

        return b""
    # list/tuple: 先頭から読めるものを探す
    if isinstance(content, (list, tuple)):
        for it in content:
            b = await _read_upload_bytes(it, _depth=_depth + 1, _seen=_seen)
            if b:
                return b
        return b""
    # data URL（念のため）
    try:
        if isinstance(content, str) and content.startswith("data:") and "base64," in content:
            b64 = content.split("base64,", 1)[1]
            return base64.b64decode(b64)
    except Exception:
        pass

    # 1) Prefer underlying file object (UploadFile.file 等) (sync)
    try:
        fobj = getattr(content, "file", None)
    except Exception:
        fobj = None

    if fobj is not None and hasattr(fobj, "read"):
        try:
            # seek(0) できるなら戻す（同じイベントの再読みでも事故らない）
            if hasattr(fobj, "seek"):
                try:
                    fobj.seek(0)
                except Exception:
                    pass
            data = fobj.read()
            if inspect.isawaitable(data):
                data = await data
            if isinstance(data, str):
                data = data.encode("utf-8", errors="ignore")
            if isinstance(data, (bytes, bytearray, memoryview)) and len(data) > 0:
                return bytes(data)
        except Exception:
            pass

    # 2) Try seek/read on the content itself (sync/async)
    try:
        seek_fn = getattr(content, "seek", None)
        if callable(seek_fn):
            try:
                r = seek_fn(0)
                if inspect.isawaitable(r):
                    await r
            except Exception:
                pass
    except Exception:
        pass

    try:
        read_fn = getattr(content, "read", None)
        if callable(read_fn):
            data = read_fn()
            if inspect.isawaitable(data):
                data = await data
            if isinstance(data, str):
                data = data.encode("utf-8", errors="ignore")
            if isinstance(data, (bytes, bytearray, memoryview)) and len(data) > 0:
                return bytes(data)
    except Exception:
        pass

    # 3) Known wrappers: content.value / content.body / content.buffer など
    try:
        for attr in ("value", "body", "buffer", "raw", "data"):
            v = getattr(content, attr, None)
            if v is not None and v is not content:
                b = await _read_upload_bytes(v, _depth=_depth + 1, _seen=_seen)
                if b:
                    return b
    except Exception:
        pass

    # 4) Last resort
    try:
        b = bytes(content)
        return b if b else b""
    except Exception:
        return b""


async def _upload_event_to_data_url(
    e, *, max_w: int = 0, max_h: int = 0, force_png: bool = False
) -> tuple[str, str]:
    """Upload event -> data URL（v0.6.9995 と同じ流れに戻す）.

    - event の型ゆれ（object/dict/list）を吸収
    - content から bytes を確保（sync/async 両対応）
    - bytes を max_w×max_h に中心トリミング＋リサイズ（cover方式）
    - data URL 化して返す

    NOTE:
    - 成功通知は呼び出し側で行う（場所ごとに文言を変えたい）
    """
    payload = _extract_upload_event_payload(e)
    # UI表示は短い方が安心。保存用も短縮で統一。
    fname = _short_name(payload.get("name", "") or "uploaded")
    mime = (payload.get("type") or "").strip()
    content = payload.get("content")

    # まず payload.content から読む。ダメなら e 自体も読む（古いイベント形状対策）
    data = await _read_upload_bytes(content)
    if not data:
        data = await _read_upload_bytes(e)

    if not data:
        try:
            print(
                "[UPLOAD] empty bytes",
                json.dumps(
                    {
                        "event": _upload_debug_summary(e),
                        "payload": _upload_debug_summary(payload),
                        "content": _upload_debug_summary(content),
                        "name": fname,
                        "mime": mime,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
        except Exception:
            pass
        try:
            ui.notify("画像の読み込みに失敗しました（JPG/PNG をお試しください）", type="warning")
        except Exception:
            pass
        return "", fname

    if len(data) > MAX_UPLOAD_BYTES:
        try:
            ui.notify("画像が大きすぎます（10MB以下にしてください）", type="warning")
        except Exception:
            pass
        return "", fname

    mime = mime or _guess_mime(fname, default="image/png")

    # Resize/crop (cover)
    if max_w and max_h:
        try:
            # PIL が重い時でも UI 全体が固まらないようにスレッドへ退避
            data, mime = await asyncio.to_thread(
                _maybe_resize_image_bytes, data, mime, max_w=max_w, max_h=max_h, force_png=force_png
            )
        except Exception:
            traceback.print_exc()

    try:
        b64 = base64.b64encode(data).decode("ascii")
        # 成功ログ（バイナリは出さない）
        try:
            print(
                "[UPLOAD] ok",
                json.dumps(
                    {"name": fname, "mime": mime, "bytes": len(data), "resized": bool(max_w and max_h)},
                    ensure_ascii=False,
                ),
                flush=True,
            )
        except Exception:
            pass
        return f"data:{mime};base64,{b64}", fname
    except Exception:
        traceback.print_exc()
        try:
            ui.notify("画像の読み込みに失敗しました（JPG/PNG をお試しください）", type="warning")
        except Exception:
            pass
        return "", fname
# ---------------------------
# Preview image serving (data URL -> /pv_img/<hash>)
# 目的: data URL をHTML/WS payloadに毎回乗せない（案件読込・操作を軽くする）
# ---------------------------

_PV_IMG_CACHE: dict[str, tuple[str, bytes]] = {}
_PV_IMG_CACHE_MAX = 256  # safety cap

def pv_img_src(url: Optional[str]) -> str:
    """Preview用: data URL を短いURLに置き換える（巨大payload削減）"""
    if not url or not isinstance(url, str):
        return url or ''
    s = url.strip()
    if not s.startswith('data:') or 'base64,' not in s:
        return s
    key = hashlib.sha1(s.encode('utf-8')).hexdigest()[:16]
    if key not in _PV_IMG_CACHE:
        try:
            head, b64part = s.split('base64,', 1)
            mime = head[5:].split(';', 1)[0].strip() or 'application/octet-stream'
            data = base64.b64decode(b64part)
            if len(_PV_IMG_CACHE) >= _PV_IMG_CACHE_MAX:
                _PV_IMG_CACHE.clear()
            _PV_IMG_CACHE[key] = (mime, data)
        except Exception:
            return s
    return f'/pv_img/{key}'


try:
    _pv_route_added = getattr(app, "_cvhb_pv_img_route_added", False)
except Exception:
    _pv_route_added = False

if not _pv_route_added:

    @app.get('/pv_img/{key}')
    def _pv_img_endpoint(key: str):
        item = _PV_IMG_CACHE.get(key)
        if not item:
            return Response(status_code=404)
        mime, data = item
        return Response(
            content=data,
            media_type=mime,
            headers={'Cache-Control': 'public, max-age=31536000, immutable'},
        )

    try:
        app._cvhb_pv_img_route_added = True
    except Exception:
        pass



# =========================
# Export ZIP download cache (v0.7.0)
# =========================

_EXPORT_ZIP_CACHE: dict[str, dict] = {}
_EXPORT_ZIP_CACHE_MAX = 10

def _build_content_disposition_attachment(filename: str) -> str:
    """Content-Disposition を安全に作る（日本語ファイル名で500にならないようにする）.
    - header は基本 ASCII が安全。非ASCIIは filename* (RFC5987) で渡す。
    - filename はフォールバック（英数字化）を入れる。
    """
    name = str(filename or "export.zip")
    # header injection / encoding trouble guard
    try:
        name = name.replace("\r", "").replace("\n", "")
    except Exception:
        pass
    try:
        name = name.replace('"', "")
    except Exception:
        pass

    # length guard (very long names can cause trouble)
    try:
        root, ext = os.path.splitext(name)
        if len(name) > 180:
            keep = max(1, 180 - len(ext))
            name = root[:keep] + ext
    except Exception:
        pass

    # ASCII fallback (safe for HTTP headers)
    try:
        fallback = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    except Exception:
        fallback = "export"
    if not fallback:
        fallback = "export"
    # keep extension if original has it
    try:
        if name.lower().endswith(".zip") and not fallback.lower().endswith(".zip"):
            fallback = fallback + ".zip"
    except Exception:
        pass
    if not fallback.lower().endswith(".zip"):
        # safety: exported zip should end with .zip
        fallback = fallback + ".zip"

    # RFC5987 (UTF-8 percent-encoding)
    try:
        from urllib.parse import quote
        encoded = quote(name, safe="")
    except Exception:
        try:
            encoded = quote_plus(name)
        except Exception:
            encoded = ""

    if encoded:
        return f'attachment; filename="{fallback}"; filename*=UTF-8\'\'{encoded}'
    return f'attachment; filename="{fallback}"'


def _export_cache_put(user_id: int, filename: str, data: bytes) -> str:
    """生成したZIPを一時キャッシュし、ダウンロード用キーを返す。"""
    key = secrets.token_urlsafe(16)
    if len(_EXPORT_ZIP_CACHE) >= _EXPORT_ZIP_CACHE_MAX:
        # drop oldest
        try:
            oldest = sorted(_EXPORT_ZIP_CACHE.items(), key=lambda kv: kv[1].get("created_at", ""))[0][0]
            _EXPORT_ZIP_CACHE.pop(oldest, None)
        except Exception:
            _EXPORT_ZIP_CACHE.clear()
    _EXPORT_ZIP_CACHE[key] = {
        "user_id": int(user_id),
        "filename": str(filename),
        "data": data,
        "created_at": now_jst_iso(),
    }
    return key

try:
    _export_route_added = getattr(app, "_cvhb_export_route_added", False)
except Exception:
    _export_route_added = False

if not _export_route_added:

    @app.get("/export_zip/{key}")
    def _export_zip_endpoint(key: str):
        user = current_user()
        item = _EXPORT_ZIP_CACHE.get(key)
        if not user or not item:
            return Response(status_code=404)
        try:
            if int(item.get("user_id") or 0) != int(user.id):
                return Response(status_code=404)
        except Exception:
            return Response(status_code=404)

        filename = str(item.get("filename") or "export.zip")
        data = item.get("data") or b""
        # Response は bytes/str 前提なので、念のため bytes に寄せる
        try:
            if isinstance(data, memoryview):
                data = data.tobytes()
            elif isinstance(data, bytearray):
                data = bytes(data)
            elif isinstance(data, str):
                data = data.encode("utf-8", errors="ignore")
            elif not isinstance(data, (bytes,)):
                data = b""
        except Exception:
            data = b""


        # Content-Disposition はASCIIのみが安全（日本語ファイル名で500になる事故を防ぐ）
        headers = {
            "Content-Disposition": _build_content_disposition_attachment(filename),
            "Cache-Control": "no-store",
        }

        try:
            resp = Response(content=data, media_type="application/zip", headers=headers)
        except Exception as ex:
            # ここで落ちると「Internal Server Error」になるため、ログだけ残して 500 を返す
            try:
                print(f"[EXPORT] response build failed: {type(ex).__name__}: {ex}")
            except Exception:
                pass
            # 失敗した場合はキャッシュを残す（リトライできるように）
            return Response(status_code=500)

        # One-time download: delete after building response (safety)
        try:
            _EXPORT_ZIP_CACHE.pop(key, None)
        except Exception:
            pass
        return resp


    try:
        app._cvhb_export_route_added = True
    except Exception:
        pass

# =========================
# Preview Static-Site serving (export template)  (v0.8.11)
# 目的: プレビューとZIP書き出しの HTML/CSS を「同じ生成物」に揃えて、ズレを根絶する
# =========================

_PV_SITE_CACHE: dict[str, dict] = {}
_PV_SITE_CACHE_MAX = 8  # safety cap (per dyno)

def _pv_site_cache_upsert(user_id: int, key: str, files: dict[str, bytes]) -> str:
    # Preview用: 生成した静的ファイル一式をメモリに置く（keyはユーザーごとに安定させる）
    try:
        # cap: drop oldest if too many
        if len(_PV_SITE_CACHE) >= _PV_SITE_CACHE_MAX and key not in _PV_SITE_CACHE:
            try:
                oldest = sorted(_PV_SITE_CACHE.items(), key=lambda kv: kv[1].get("updated_at", ""))[0][0]
                _PV_SITE_CACHE.pop(oldest, None)
            except Exception:
                _PV_SITE_CACHE.clear()
        _PV_SITE_CACHE[key] = {
            "user_id": int(user_id),
            "files": files,
            "updated_at": now_jst_iso(),
        }
    except Exception:
        traceback.print_exc()
    return key

def _pv_site_guess_media_type(path: str) -> str:
    # 拡張子から Content-Type を推定（プレビュー用）
    try:
        mt, _ = mimetypes.guess_type(path)
        if mt:
            return mt
    except Exception:
        pass
    p = (path or "").lower()
    if p.endswith(".html"):
        return "text/html"
    if p.endswith(".css"):
        return "text/css"
    if p.endswith(".js"):
        return "application/javascript"
    if p.endswith(".png"):
        return "image/png"
    if p.endswith(".jpg") or p.endswith(".jpeg"):
        return "image/jpeg"
    if p.endswith(".svg"):
        return "image/svg+xml"
    if p.endswith(".webp"):
        return "image/webp"
    return "application/octet-stream"

try:
    _pv_site_route_added = getattr(app, "_cvhb_pv_site_route_added", False)
except Exception:
    _pv_site_route_added = False

if not _pv_site_route_added:

    @app.get("/pv_site/{key}/{full_path:path}")
    def _pv_site_get_endpoint(key: str, full_path: str):
        """Preview用の静的ファイル配信エンドポイント。

        ⚠ 重要（今回の不具合の根本原因）:
        /pv_site は iframe から通常の HTTP リクエストとして呼ばれます。
        そのため NiceGUI の UI セッション（app.storage.user）に依存する current_user() が
        取得できないケースがあり、404（中身なし）になってプレビューが「真っ白」に見えることがあります。

        ここでは「key が一致している＝そのプレビューを生成したブラウザ」とみなして返します。
        （key は十分に長いランダム値で、推測は現実的ではありません）
        """

        item = _PV_SITE_CACHE.get(key)
        if not item:
            # 404 でも「真っ白」にならないように、簡易HTMLを返す（デバッグ容易化）
            body = (
                "<!doctype html><html lang='ja'><head><meta charset='utf-8'>"
                "<meta name='viewport' content='width=device-width,initial-scale=1'>"
                "<title>Preview not found</title>"
                "<style>"
                "body{font-family:system-ui,-apple-system,'Segoe UI','Noto Sans JP',sans-serif;padding:24px;line-height:1.7;}"
                ".box{max-width:780px;margin:0 auto;padding:16px 18px;border:1px solid #e5e7eb;border-radius:14px;background:#fff;}"
                "h1{margin:0 0 8px;font-size:18px;}"
                "p{margin:0;color:#374151;}"
                "</style></head><body><div class='box'>"
                "<h1>プレビューが見つかりません</h1>"
                "<p>プレビュー用の一時データが見つかりません。画面を更新して、もう一度開き直してください。</p>"
                "</div></body></html>"
            ).encode("utf-8")
            return Response(
                content=body,
                status_code=404,
                media_type="text/html",
                headers={"Cache-Control": "no-store"},
            )

        path = (full_path or "").lstrip("/") or "index.html"
        files = item.get("files") or {}
        content = files.get(path)
        if content is None:
            body = (
                "<!doctype html><html lang='ja'><head><meta charset='utf-8'>"
                "<meta name='viewport' content='width=device-width,initial-scale=1'>"
                "<title>File not found</title>"
                "<style>"
                "body{font-family:system-ui,-apple-system,'Segoe UI','Noto Sans JP',sans-serif;padding:24px;line-height:1.7;}"
                ".box{max-width:780px;margin:0 auto;padding:16px 18px;border:1px solid #e5e7eb;border-radius:14px;background:#fff;}"
                "h1{margin:0 0 8px;font-size:18px;}"
                "p{margin:0;color:#374151;}"
                "code{background:#f3f4f6;padding:2px 6px;border-radius:6px;}"
                "</style></head><body><div class='box'>"
                "<h1>ファイルが見つかりません</h1>"
                "<p>要求されたファイル: <code>" + html.escape(path) + "</code></p>"
                "</div></body></html>"
            ).encode("utf-8")
            return Response(
                content=body,
                status_code=404,
                media_type="text/html",
                headers={"Cache-Control": "no-store"},
            )

        # media type
        mt = _pv_site_guess_media_type(path)

        # cache policy:
        # - html/css: no-store (編集が即反映されることを優先)
        # - hashed images: immutable (転送を軽くする)
        headers = {"Cache-Control": "no-store"}
        try:
            if path.startswith("assets/img/") and re.search(r"_[0-9a-f]{10}\\.", path):
                headers["Cache-Control"] = "public, max-age=31536000, immutable"
        except Exception:
            pass

        # Response は bytes/str 前提
        try:
            if isinstance(content, memoryview):
                content = content.tobytes()
            elif isinstance(content, bytearray):
                content = bytes(content)
        except Exception:
            pass

        return Response(content=content, media_type=mt, headers=headers)

    # プレビュー内で contact.php を POST した場合だけ、thanks.html を返して「送信導線」を確認できるようにする
    # ※メール送信などは行わない（あくまでプレビュー上の見た目確認用）
    @app.post("/pv_site/{key}/contact.php")
    def _pv_site_contact_post_endpoint(key: str):
        item = _PV_SITE_CACHE.get(key)
        if not item:
            return Response(status_code=404)

        # 302 redirect -> thanks.html (status=ok)
        try:
            return Response(
                status_code=302,
                headers={
                    "Location": f"/pv_site/{key}/thanks.html?status=ok",
                    "Cache-Control": "no-store",
                },
            )
        except Exception:
            # fallback: thanks.html をそのまま返す
            files = item.get("files") or {}
            content = files.get("thanks.html") or b""
            return Response(content=content, media_type="text/html", headers={"Cache-Control": "no-store"})



    try:
        app._cvhb_pv_site_route_added = True
    except Exception:
        pass


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
  display: flex;
  flex-direction: column;
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
  flex: 1 1 auto;
  min-height: 0;
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
  width: 100%;
  aspect-ratio: 16 / 9;
  height: auto;
  border-radius: 0;
  border: none;
  box-shadow: none;
  background: rgba(255,255,255,0.10);
}
.pv-layout-260218.pv-mode-mobile .pv-hero-slider-wide{
  height: auto;
}
.pv-layout-260218.pv-mode-pc .pv-hero-slider-wide{
  height: auto;
}
.pv-layout-260218.pv-dark .pv-hero-slider-wide{
  background: rgba(0,0,0,0.16);
}

/* ===== Hero slider dots (4 dots) ===== */
.pv-layout-260218 .pv-hero-stage{
  position: relative;
}

/* PC: dots are shown "below" the hero image, so we keep some space under the hero */
.pv-layout-260218.pv-mode-pc .pv-hero-wide{
  margin-bottom: 44px;
}

.pv-layout-260218 .pv-hero-dots{
  position: absolute;
  z-index: 8;
  display: flex;
  gap: 12px;
  align-items: center;
  justify-content: center;
  pointer-events: auto;
}

.pv-layout-260218.pv-mode-mobile .pv-hero-dots{
  right: 16px;
  top: 50%;
  transform: translateY(-50%);
  flex-direction: column;
}

.pv-layout-260218.pv-mode-pc .pv-hero-dots{
  left: 50%;
  bottom: -28px;
  transform: translateX(-50%);
  flex-direction: row;
}

.pv-layout-260218 .pv-hero-dot{
  width: 12px;
  height: 12px;
  border-radius: 999px;
  border: 2px solid rgba(255,255,255,0.72);
  background: rgba(255,255,255,0.30);
  cursor: pointer;
  padding: 0;
  margin: 0;
  outline: none;
  box-shadow: 0 10px 22px rgba(0,0,0,0.16);
  transition: transform 140ms ease, background 140ms ease, opacity 140ms ease;
}

.pv-layout-260218 .pv-hero-dot:hover{
  transform: scale(1.15);
}

.pv-layout-260218 .pv-hero-dot.is-active{
  background: var(--pv-primary);
  border-color: rgba(255,255,255,0.92);
  opacity: 1;
}

.pv-layout-260218.pv-dark .pv-hero-dot{
  border-color: rgba(255,255,255,0.62);
  background: rgba(0,0,0,0.24);
  box-shadow: 0 12px 26px rgba(0,0,0,0.26);
}

.pv-layout-260218.pv-dark .pv-hero-dot.is-active{
  background: rgba(255,255,255,0.92);
  border-color: rgba(255,255,255,0.96);
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
  justify-content: center;
  backdrop-filter: blur(12px);
  background: linear-gradient(180deg, rgba(255,255,255,0.86), rgba(255,255,255,0.72));
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
/* ===== Hero: キャッチ/サブキャッチ 文字サイズ（大/中/小） ===== */
.pv-layout-260218.pv-mode-pc .pv-hero-caption-title.pv-size-l{
  font-size: clamp(2.6rem, 3.6vw, 4.4rem);
}
.pv-layout-260218.pv-mode-pc .pv-hero-caption-title.pv-size-s{
  font-size: clamp(2.0rem, 2.8vw, 3.5rem);
}
.pv-layout-260218.pv-mode-pc .pv-hero-caption-sub.pv-size-l{
  font-size: clamp(1.35rem, 1.65vw, 1.75rem);
}
.pv-layout-260218.pv-mode-pc .pv-hero-caption-sub.pv-size-s{
  font-size: clamp(1.02rem, 1.25vw, 1.25rem);
}

.pv-layout-260218.pv-mode-mobile .pv-hero-caption-title.pv-size-l{ font-size: 2.05rem; }
.pv-layout-260218.pv-mode-mobile .pv-hero-caption-title.pv-size-s{ font-size: 1.55rem; }
.pv-layout-260218.pv-mode-mobile .pv-hero-caption-sub{ font-size: 1.02rem; }
.pv-layout-260218.pv-mode-mobile .pv-hero-caption-sub.pv-size-l{ font-size: 1.15rem; }
.pv-layout-260218.pv-mode-mobile .pv-hero-caption-sub.pv-size-s{ font-size: 0.95rem; }


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
  aspect-ratio: 16 / 9;
  height: auto;
  border-radius: 22px;
  object-fit: cover;
  border: 1px solid var(--pv-border);
  box-shadow: var(--pv-shadow);
}

.pv-layout-260218.pv-mode-mobile .pv-about-img{
  height: auto;
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
  aspect-ratio: 16 / 9;
  height: auto;
  border-radius: 18px;
  object-fit: cover;
  border: 1px solid rgba(0,0,0,0.06);
}

.pv-layout-260218.pv-dark .pv-services-img{
  border-color: rgba(255,255,255,0.12);
}

.pv-layout-260218.pv-mode-pc .pv-services-img{
  height: auto;
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
/* ====== Legal (Privacy Policy) ====== */
.pv-legal-title{
  font-weight: 900;
  font-size: 1.05rem;
}
.pv-legal-md{
  font-size: 0.98rem;
  line-height: 1.85;
}
.pv-legal-md h1,
.pv-legal-md h2,
.pv-legal-md h3{
  margin: 14px 0 8px;
  font-weight: 900;
}
.pv-legal-md h2{ font-size: 1.05rem; }
.pv-legal-md h3{ font-size: 1.0rem; }
.pv-legal-md ul{ padding-left: 1.2em; }
.pv-legal-md li{ margin: 6px 0; }

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

      let dots = slider.querySelectorAll('.pv-hero-dot');
      try{
        // dots may be outside the slider (PC: below image)
        if(!dots || dots.length === 0){
          const box = document.getElementById(sliderId + '-dots');
          if(box){ dots = box.querySelectorAll('.pv-hero-dot'); }
        }
      } catch(e){}
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

      const hoverTarget = (slider.parentElement && slider.parentElement.classList && slider.parentElement.classList.contains('pv-hero-stage')) ? slider.parentElement : slider;
      hoverTarget.onmouseenter = function(){ stop(); };
      hoverTarget.onmouseleave = function(){ start(); };

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


VERSION = read_text_file("VERSION", "0.8.12")
APP_ENV = (os.getenv("APP_ENV") or ("help" if HELP_MODE else "prod")).lower().strip()

# Preview: ZIP書き出しと同じHTML/CSSで描画する（ズレ防止）
# - 1: 有効（デフォルト） / 0: 従来プレビュー（デバッグ用）
PREVIEW_USE_EXPORT_TEMPLATE = str(os.getenv("CVHB_PREVIEW_UNIFY") or "1").strip().lower() not in {"0", "false", "no", "off"}

# NiceGUI のユーザーセッション（Cookie）に使う秘密鍵
# - 通常モード: 必須（Heroku Config Vars）
# - HELP_MODE  : ローカル用途なので未設定でも動く（ローカル起動用に毎回ランダム生成）
#
# ※ 固定文字列をソースに残さない（「秘密情報の値」をコードへ置かない方針）
STORAGE_SECRET = (os.getenv("STORAGE_SECRET") or "").strip()
if not STORAGE_SECRET:
    if HELP_MODE:
        STORAGE_SECRET = secrets.token_hex(16)
    else:
        raise RuntimeError("STORAGE_SECRET が未設定です。HerokuのConfig Varsに追加してください。")

# DB（Heroku Postgres）
DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
# 一部の環境で postgres:// が来る場合があるので正規化
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
if (not HELP_MODE) and (not DATABASE_URL):
    raise RuntimeError("DATABASE_URL が未設定です。Heroku Postgres を追加してください。")

# SFTP To Go（案件の project.json / ZIP を保存）
SFTP_BASE_DIR = (os.getenv("SFTP_BASE_DIR") or "/cvhb").rstrip("/")
SFTPTOGO_URL = (os.getenv("SFTPTOGO_URL") or "").strip()
if (not HELP_MODE) and (not SFTPTOGO_URL):
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
    if psycopg is None:
        raise RuntimeError("DBが利用できません（psycopg未インストール or HELP_MODE）")
    if not DATABASE_URL:
        raise RuntimeError("DBが利用できません（DATABASE_URL が空です）")
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
    # HELP_MODE: DBに触れない（完全オフラインでヘルプ作成するため）
    if HELP_MODE:
        return
    try:
        log_action(user, action, details)
    except Exception as e:
        # ここでURL等が出る可能性があるので、画面には出さない
        print(f"[audit_log] failed: {sanitize_error_text(e)}")


def stg_auto_admin_enabled() -> bool:
    """stgだけで使える「ログインなし admin」スイッチ（ヘルプ作成/スクショ用途）。

    ✅ 有効条件:
      - APP_ENV == "stg"
      - Heroku(DYNO) 上
      - 環境変数 CVHB_STG_AUTO_ADMIN が true
    """
    if HELP_MODE:
        return False
    # 事故防止: stg 以外は絶対に有効化しない
    if APP_ENV != "stg":
        return False
    # ローカルは HELP_MODE を使う想定。stg(=Heroku) だけで使えるように制限。
    if not os.getenv("DYNO"):
        return False
    return _env_flag("CVHB_STG_AUTO_ADMIN")


def stg_auto_admin_user_row() -> Optional[dict]:
    """自動ログインに使う admin ユーザー行を返す（無ければ作成/seedする）。"""
    if not stg_auto_admin_enabled():
        return None

    # 変更したい場合は env で上書き可能（例: admin_test）
    target = (os.getenv("CVHB_STG_AUTO_ADMIN_USER") or "").strip() or "admin_test"

    # 1) まずは target を探す
    try:
        row = get_user_by_username(target)
        if row and str(row.get("role")) == "admin":
            return row
    except Exception:
        pass

    # 2) stg のテストユーザーを作る（admin_test が作られる）
    try:
        ensure_stg_test_users()
    except Exception:
        pass

    # 3) 再チェック（target → admin_test）
    try:
        row = get_user_by_username(target)
        if row and str(row.get("role")) == "admin":
            return row
    except Exception:
        pass
    try:
        row = get_user_by_username("admin_test")
        if row and str(row.get("role")) == "admin":
            return row
    except Exception:
        pass

    # 4) 最終手段: stg_auto_admin を作る（パスワードはランダムでOK）
    try:
        create_user("stg_auto_admin", secrets.token_urlsafe(24), "admin")
        row = get_user_by_username("stg_auto_admin")
        if row:
            return row
    except Exception:
        pass

    return None


def stg_auto_login_admin() -> Optional[User]:
    """stgで、ログイン無しで admin をセットする（必要なときだけ）。"""
    if not stg_auto_admin_enabled():
        return None

    # すでにログイン状態ならそのまま返す
    try:
        uid = app.storage.user.get("user_id")
        username = app.storage.user.get("username")
        role = app.storage.user.get("role")
        if uid and username and role:
            return User(id=int(uid), username=str(username), role=str(role))
    except Exception:
        pass

    try:
        row = stg_auto_admin_user_row()
        if not row:
            return None
        set_logged_in(row)
        app.storage.user["stg_auto_admin"] = True
        cleanup_user_storage()

        u = User(id=int(row["id"]), username=str(row["username"]), role=str(row["role"]))
        safe_log_action(u, "stg_auto_admin_login", details=json.dumps({"mode": "stg_auto_admin"}))
        return u
    except Exception:
        return None


def current_user() -> Optional[User]:
    # HELP_MODE: ログインなしで admin 扱い（ヘルプ作成用）
    if HELP_MODE:
        try:
            app.storage.user["user_id"] = 0
            app.storage.user["username"] = "help_admin"
            app.storage.user["role"] = "admin"
        except Exception:
            pass
        return User(id=0, username="help_admin", role="admin")

    # STG_AUTO_ADMIN をOFFに戻したら、セッションを即クリアしてログイン画面に戻す（戻しやすさ最優先）
    try:
        if app.storage.user.get("stg_auto_admin") and not stg_auto_admin_enabled():
            app.storage.user.clear()
            return None
    except Exception:
        pass

    try:
        uid = app.storage.user.get("user_id")
        username = app.storage.user.get("username")
        role = app.storage.user.get("role")
        if uid and username and role:
            return User(id=int(uid), username=str(username), role=str(role))
    except Exception:
        pass

    # stgのみ: ログインなしで admin 扱い（環境変数で明示ONしたときだけ）
    try:
        u = stg_auto_login_admin()
        if u:
            return u
    except Exception:
        pass

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

# HELP_MODE用: ローカルだけで案件を保持（SFTP/DB無し）
HELP_PROJECT_STORE: dict[str, dict] = {}


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
    # HELP_MODE: ローカルでのヘルプ作成は「完全オフライン」を想定するためSFTPは使わない
    if HELP_MODE:
        raise RuntimeError("HELP_MODEではSFTP To Goを使いません（オフライン専用）")
    if paramiko is None:
        raise RuntimeError("paramiko が未インストールです（SFTPが使えません）")
    if not SFTPTOGO_URL:
        raise RuntimeError("SFTPTOGO_URL が未設定です")
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


def sftp_write_bytes(sftp: paramiko.SFTPClient, remote_path: str, data: bytes) -> None:
    """SFTPにバイナリを書き込む（ZIPなど）。"""
    remote_dir = "/".join(remote_path.split("/")[:-1])
    sftp_mkdirs(sftp, remote_dir)
    with sftp.open(remote_path, "wb") as f:
        f.write(data or b"")


def sftp_read_text(sftp: paramiko.SFTPClient, remote_path: str) -> str:
    with sftp.open(remote_path, "r") as f:
        return f.read()


def sftp_read_bytes(sftp: paramiko.SFTPClient, remote_path: str) -> bytes:
    """SFTPからバイナリを読み込む（ZIPなど）。"""
    with sftp.open(remote_path, "rb") as f:
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

def sftp_rmtree(sftp: paramiko.SFTPClient, remote_dir: str) -> None:
    """SFTP上のディレクトリを中身ごと削除する（危険なので用途限定）。"""
    remote_dir = (remote_dir or "").rstrip("/")
    if not remote_dir:
        raise ValueError("remote_dir is empty")
    # Safety: projectsディレクトリ配下だけ許可
    if not remote_dir.startswith(SFTP_PROJECTS_DIR.rstrip("/") + "/"):
        raise ValueError("unsafe delete path")
    try:
        for it in sftp.listdir_attr(remote_dir):
            p = f"{remote_dir}/{it.filename}"
            try:
                if stat.S_ISDIR(it.st_mode):
                    sftp_rmtree(sftp, p)
                else:
                    try:
                        sftp.remove(p)
                    except Exception:
                        pass
            except Exception:
                pass
        try:
            sftp.rmdir(remote_dir)
        except Exception:
            pass
    except Exception:
        # already deleted / not found
        return


def delete_project_from_sftp(project_id: str, user: Optional[User]) -> None:
    """案件ディレクトリごと削除する（管理者専用）。"""
    pid = (project_id or "").strip()
    if not pid:
        raise ValueError("project_id is empty")

    # Safety: 想定フォーマットだけ許可（pYYYY..._hex）
    if not re.fullmatch(r"p\d{14}_[0-9a-f]{6}", pid):
        raise ValueError("invalid project_id format")

    remote_dir = project_dir(pid)
    with sftp_client() as sftp:
        sftp_rmtree(sftp, remote_dir)

    # 案件一覧キャッシュを無効化（削除が即反映されるように）
    try:
        _projects_cache["ts"] = 0
    except Exception:
        pass

    if user:
        safe_log_action(user, "project_delete", details=json.dumps({"project_id": pid}, ensure_ascii=False))


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
        "features": "特徴：福祉向けの分岐（介護/障がい/児童 × 入所/通所）を選べます",
    },
    {
        "value": "個人事業",
        "label": "個人事業",
        "features": "特徴：店舗・個人向け。文章が少なめでも作れます（あとで自由に変更OK）",
    },
    {
        "value": "その他",
        "label": "その他",
        "features": "特徴：会社サイトと同じ構成で作れます（まず形を作って、文章は後で調整）",
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
                "catch_copy": corp_sample_catch,
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
        # v0.6.998: キャッチが空のときに「会社名」が表示され、
        # テンプレ切替でそのまま残ってしまうと「消えた/固定された」に見えるため、
        # 現在の会社名も「差し替えてよい値」に含めます。
        try:
            _cn = _txt(step2.get("company_name"))
            if _cn:
                sample_catch.add(_cn)
        except Exception:
            pass
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

    p["schema_version"] = "0.8.0"
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
    step2.setdefault("catch_size", "中")
    step2.setdefault("sub_catch_size", "中")
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
    # NOTE: UI の入力フォームが items 内の dict を参照しているため、ここで dict を作り直さず「同じ dict を整形して使う」
    norm_items = []
    for it in items:
        if not isinstance(it, dict):
            continue
        it["title"] = str(it.get("title") or "").strip()
        it["body"] = str(it.get("body") or "").strip()
        norm_items.append(it)
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
    # v0.8: お問い合わせフォーム方式（フォーム/PHP・外部フォームURL・メール対応）
    contact.setdefault("form_mode", "フォーム方式（おすすめ）")
    contact.setdefault("external_form_url", "")

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

        # --- v0.7 workflow / publish settings ---
    workflow = data.get("workflow")
    if not isinstance(workflow, dict):
        workflow = {}
        data["workflow"] = workflow

    approval = workflow.get("approval")
    if not isinstance(approval, dict):
        approval = {}
        workflow["approval"] = approval

    approval.setdefault("status", "draft")  # draft / requested / approved / rejected
    approval.setdefault("requested_at", "")
    approval.setdefault("requested_by", "")
    approval.setdefault("request_note", "")
    approval.setdefault("reviewed_at", "")
    approval.setdefault("reviewed_by", "")
    approval.setdefault("review_note", "")
    approval.setdefault("approved_at", "")
    approval.setdefault("approved_by", "")
    approval.setdefault("approved_note", "")

    workflow.setdefault("last_export_at", "")
    workflow.setdefault("last_export_by", "")
    workflow.setdefault("last_backup_zip_at", "")
    workflow.setdefault("last_backup_zip_by", "")
    workflow.setdefault("last_backup_zip_file", "")
    workflow.setdefault("last_publish_at", "")
    workflow.setdefault("last_publish_by", "")
    workflow.setdefault("last_publish_target", "")

    publish = data.get("publish")
    if not isinstance(publish, dict):
        publish = {}
        data["publish"] = publish

    publish.setdefault("sftp_host", "")
    # portは文字列で入っても壊れないようにintへ
    try:
        publish["sftp_port"] = int(publish.get("sftp_port", 22) or 22)
    except Exception:
        publish["sftp_port"] = 22
    publish.setdefault("sftp_user", "")
    publish.setdefault("sftp_dir", "")
    publish.setdefault("sftp_note", "")  # メモ（例: サーバー会社/案件番号など）

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
        "schema_version": "0.7.0",
        "project_id": pid,
        "project_name": name,
        "created_at": now_jst_iso(),
        "updated_at": now_jst_iso(),
        "created_by": created_by.username if created_by else "",
        "updated_by": created_by.username if created_by else "",
        "data": {
            "step1": {"industry": "会社サイト（企業）", "primary_color": "blue", "welfare_domain": "", "welfare_mode": "", "template_id": "corp_v1"},
            "step2": {"company_name": "", "favicon_url": "", "favicon_filename": "", "catch_copy": "", "catch_size": "中", "sub_catch_size": "中", "phone": "", "address": "", "email": ""},
            "blocks": {},
        },
    }
    p = normalize_project(p)

    if created_by:
        safe_log_action(created_by, "project_create", details=json.dumps({"project_id": pid, "name": name}, ensure_ascii=False))
    return p


def _help_seed_sample_projects(user: Optional[User]) -> None:
    """HELP_MODE用: サンプル案件を複数プリセットしておく（企業/福祉/飲食）。

    目的:
    - ローカル完全オフラインで「ヘルプ作成（スクショ撮影）」ができる
    - 1つだけだと説明が偏るので、代表3パターンを用意する
    - DB/SFTPには一切触らない（メモリ上の HELP_PROJECT_STORE のみ）
    """
    if HELP_PROJECT_STORE:
        return

    u = user or User(id=0, username="help_admin", role="admin")

    def _set(p: dict, *, industry: str, color: str, company: str, catch_copy: str, contact_msg: str, proposal_note: str, welfare_domain: str = "", welfare_mode: str = "") -> dict:
        try:
            data = p.get("data") if isinstance(p, dict) else {}
            if not isinstance(data, dict):
                data = {}
                p["data"] = data

            # step1
            step1 = data.get("step1")
            if not isinstance(step1, dict):
                step1 = {}
                data["step1"] = step1
            step1["industry"] = industry
            step1["primary_color"] = color
            if industry == "福祉事業所":
                step1["welfare_domain"] = welfare_domain or step1.get("welfare_domain") or ""
                step1["welfare_mode"] = welfare_mode or step1.get("welfare_mode") or ""

            # normalize -> template反映
            p2 = normalize_project(p)

            # step2
            data2 = p2.get("data") if isinstance(p2, dict) else {}
            if isinstance(data2, dict):
                step2 = data2.get("step2")
                if not isinstance(step2, dict):
                    step2 = {}
                    data2["step2"] = step2
                step2.setdefault("company_name", company)
                step2.setdefault("catch_copy", catch_copy)

                # blocks.contact
                blocks = data2.get("blocks")
                if not isinstance(blocks, dict):
                    blocks = {}
                    data2["blocks"] = blocks
                contact = blocks.setdefault("contact", {})
                if isinstance(contact, dict):
                    contact.setdefault("message", contact_msg)

                # workflow.approval.request_note（提案のサンプルとして使う）
                wf = data2.get("workflow")
                if not isinstance(wf, dict):
                    wf = {}
                    data2["workflow"] = wf
                approval = wf.get("approval")
                if not isinstance(approval, dict):
                    approval = {}
                    wf["approval"] = approval
                # 既存が空のときだけ入れる（やりすぎない）
                if not str(approval.get("request_note") or "").strip():
                    approval["request_note"] = proposal_note

            return normalize_project(p2)
        except Exception:
            return normalize_project(p)

    samples: list[dict] = []

    # 企業サイト
    p1 = create_project("サンプル：企業サイト", u)
    p1 = _set(
        p1,
        industry="会社サイト（企業）",
        color="blue",
        company="サンプル株式会社",
        catch_copy="やさしいホームページを、みんなで。",
        contact_msg="お気軽にご相談ください。",
        proposal_note="【提案メモの例】\n1) 目的：初めての人が3秒で何の会社か分かる\n2) 変更：キャッチコピーを短く / 実績をFAQへ\n3) 理由：スマホで読みやすくなる\n4) 確認：掲載して良い内容（写真・住所など）",
    )
    samples.append(p1)

    # 福祉事業所（障がい福祉・通所の例）
    p2 = create_project("サンプル：福祉事業所", u)
    p2 = _set(
        p2,
        industry="福祉事業所",
        color="green",
        company="サンプル福祉事業所",
        catch_copy="地域で支える、毎日のくらし。",
        contact_msg="見学・体験、お気軽にどうぞ。",
        proposal_note="【提案メモの例】\n- 見学の流れ（予約→見学→体験）をFAQに入れる\n- 送迎の有無（地域）を一行で入れる\n- 支援内容は“3つだけ”に整理すると伝わりやすい",
        welfare_domain="障がい福祉サービス",
        welfare_mode="通所系",
    )
    samples.append(p2)

    # 飲食店（個人事業の例）
    p3 = create_project("サンプル：飲食店", u)
    p3 = _set(
        p3,
        industry="個人事業",
        color="red",
        company="サンプル食堂",
        catch_copy="今日のごはん、ここで。",
        contact_msg="お問い合わせはこちらからお願いします。",
        proposal_note="【提案メモの例】\n- 営業時間と定休日を一番上に\n- メニュー写真は“3枚だけ”でもOK\n- アクセスに“最寄り駅/駐車場”を入れると迷いにくい",
    )
    samples.append(p3)

    for p in samples:
        try:
            p = normalize_project(p)
            HELP_PROJECT_STORE[p["project_id"]] = p
        except Exception:
            pass


def _help_ensure_sample_project(user: Optional[User]) -> dict:
    """HELP_MODE用のサンプル案件を返す（無ければ作る）。

    - 完全オフラインでヘルプ作成をしたいときに使う
    - SFTP/DB には一切触らない
    """
    if not HELP_PROJECT_STORE:
        _help_seed_sample_projects(user)

    # できれば「企業サンプル」を既定にする（説明が汎用的で使いやすい）
    for _p in HELP_PROJECT_STORE.values():
        if isinstance(_p, dict) and _p.get("project_name") == "サンプル：企業サイト":
            return normalize_project(_p)

    # fallback
    try:
        return normalize_project(next(iter(HELP_PROJECT_STORE.values())))
    except Exception:
        u = user or User(id=0, username="help_admin", role="admin")
        p = create_project("サンプル：企業サイト", u)
        p = normalize_project(p)
        HELP_PROJECT_STORE[p["project_id"]] = p
        return p


def save_project_to_sftp(p: dict, user: Optional[User]) -> None:
    p = normalize_project(p)
    p["updated_at"] = now_jst_iso()
    if user:
        p["updated_by"] = user.username

    # HELP_MODE: SFTPには保存せず、メモリ上の案件ストアに保存する
    if HELP_MODE:
        HELP_PROJECT_STORE[p["project_id"]] = p
        return

    remote = project_json_path(p["project_id"])
    body = json.dumps(p, ensure_ascii=False, indent=2)
    with sftp_client() as sftp:
        sftp_write_text(sftp, remote, body)

    if user:
        safe_log_action(user, "project_save", details=json.dumps({"project_id": p["project_id"]}, ensure_ascii=False))


def load_project_from_sftp(project_id: str, user: Optional[User]) -> dict:
    # HELP_MODE: SFTPは使わず、メモリ上の案件ストアから読む
    if HELP_MODE:
        if not HELP_PROJECT_STORE:
            _help_ensure_sample_project(user)
        p = HELP_PROJECT_STORE.get(project_id)
        if not isinstance(p, dict):
            p = _help_ensure_sample_project(user)
        return normalize_project(p)

    remote = project_json_path(project_id)
    with sftp_client() as sftp:
        body = sftp_read_text(sftp, remote)
    p = normalize_project(json.loads(body))
    if user:
        safe_log_action(user, "project_load", details=json.dumps({"project_id": project_id}, ensure_ascii=False))
    return p


def list_projects_from_sftp() -> list[dict]:
    """案件一覧は project.json が肥大化しやすい（data URL画像）ため、先頭だけ読んでメタ情報を抜く。

    目的:
    - /projects の表示を高速化（SFTP転送量・JSONデコード量を最小化）
    - WebSocket切断（Connection lost）を起こしにくくする
    """
    # HELP_MODE: SFTPは使わず、メモリ上の案件ストアから一覧を作る
    if HELP_MODE:
        if not HELP_PROJECT_STORE:
            _help_ensure_sample_project(None)
        projects: list[dict] = []
        for _p in HELP_PROJECT_STORE.values():
            if not isinstance(_p, dict):
                continue
            projects.append({
                "project_id": _p.get("project_id", ""),
                "project_name": _p.get("project_name", ""),
                "updated_at": _p.get("updated_at", ""),
                "created_at": _p.get("created_at", ""),
                "updated_by": _p.get("updated_by", ""),
            })
        projects.sort(key=lambda x: x.get("updated_at", ""), reverse=True)
        return projects

    HEAD_BYTES = 24 * 1024

    def _json_head_get_str(head: str, key: str) -> str:
        try:
            m = re.search(rf'"{re.escape(key)}"\s*:\s*"((?:\\.|[^"])*)"', head)
            if not m:
                return ""
            return json.loads('"' + m.group(1) + '"')
        except Exception:
            return ""

    projects: list[dict] = []
    with sftp_client() as sftp:
        dirs = sftp_list_dirs(sftp, SFTP_PROJECTS_DIR)
        for d in dirs:
            try:
                path = project_json_path(d)
                head = ""
                try:
                    # NOTE: 全文を読まない（巨大な data URL 画像を避ける）
                    with sftp.open(path, "rb") as f:
                        head = f.read(HEAD_BYTES).decode("utf-8", errors="ignore")
                except Exception:
                    head = ""

                project_id = _json_head_get_str(head, "project_id") or d
                project_name = _json_head_get_str(head, "project_name") or "(no name)"
                updated_at = _json_head_get_str(head, "updated_at")
                created_at = _json_head_get_str(head, "created_at")
                updated_by = _json_head_get_str(head, "updated_by")

                # 最低限の表示用に欠損を埋める（壊れたJSONでも一覧は出す）
                projects.append({
                    "project_id": project_id,
                    "project_name": project_name,
                    "updated_at": updated_at,
                    "created_at": created_at,
                    "updated_by": updated_by,
                })
            except Exception:
                projects.append({"project_id": d, "project_name": "(broken project.json)", "updated_at": "", "created_at": "", "updated_by": ""})

    projects.sort(key=lambda x: x.get("updated_at", ""), reverse=True)
    return projects


# =========================
# v0.7.0 Approval / Export / Publish helpers
# =========================

ADMIN_ONLY_ROLES = {"admin"}
APPROVER_ROLES = {"admin", "subadmin"}
EXPORT_ROLES = {"admin", "subadmin"}
PUBLISH_ROLES = {"admin"}


def is_admin(u: Optional[User]) -> bool:
    return bool(u and u.role in ADMIN_ONLY_ROLES)


def can_approve(u: Optional[User]) -> bool:
    return bool(u and u.role in APPROVER_ROLES)


def can_export(u: Optional[User]) -> bool:
    return bool(u and u.role in EXPORT_ROLES)


def can_publish(u: Optional[User]) -> bool:
    return bool(u and (u.role in PUBLISH_ROLES) and (not HELP_MODE))


def get_workflow(p: dict) -> dict:
    try:
        data = p.get("data") if isinstance(p, dict) else {}
        if not isinstance(data, dict):
            data = {}
        wf = data.get("workflow")
        if not isinstance(wf, dict):
            wf = {}
            data["workflow"] = wf
        return wf
    except Exception:
        return {}


def get_approval(p: dict) -> dict:
    wf = get_workflow(p)
    a = wf.get("approval")
    if not isinstance(a, dict):
        a = {}
        wf["approval"] = a
    # normalize keys (in case old project.json)
    a.setdefault("status", "draft")
    a.setdefault("requested_at", "")
    a.setdefault("requested_by", "")
    a.setdefault("request_note", "")
    a.setdefault("reviewed_at", "")
    a.setdefault("reviewed_by", "")
    a.setdefault("review_note", "")
    a.setdefault("approved_at", "")
    a.setdefault("approved_by", "")
    a.setdefault("approved_note", "")
    return a


def approval_status_label(status: str) -> str:
    s = (status or "").strip()
    if s == "approved":
        return "承認OK"
    if s == "requested":
        return "承認待ち"
    if s == "rejected":
        return "差戻し"
    return "編集中"


def is_approved(p: dict) -> bool:
    try:
        return get_approval(p).get("status") == "approved"
    except Exception:
        return False


def approval_request(p: dict, actor: User, note: str = "") -> None:
    a = get_approval(p)
    a["status"] = "requested"
    a["requested_at"] = now_jst_iso()
    a["requested_by"] = actor.username
    a["request_note"] = (note or "").strip()
    # reset review
    a["reviewed_at"] = ""
    a["reviewed_by"] = ""
    a["review_note"] = ""
    a["approved_at"] = ""
    a["approved_by"] = ""
    a["approved_note"] = ""


def approval_approve(p: dict, actor: User, note: str = "") -> None:
    a = get_approval(p)
    a["status"] = "approved"
    a["approved_at"] = now_jst_iso()
    a["approved_by"] = actor.username
    a["approved_note"] = (note or "").strip()
    # mark review info too
    a["reviewed_at"] = a.get("approved_at", "")
    a["reviewed_by"] = a.get("approved_by", "")
    a["review_note"] = a.get("approved_note", "")


def approval_reject(p: dict, actor: User, note: str = "") -> None:
    a = get_approval(p)
    a["status"] = "rejected"
    a["reviewed_at"] = now_jst_iso()
    a["reviewed_by"] = actor.username
    a["review_note"] = (note or "").strip()
    # keep approved fields cleared
    a["approved_at"] = ""
    a["approved_by"] = ""
    a["approved_note"] = ""


def compute_final_checks(p: dict) -> dict:
    """公開前チェック（必須/推奨）を返す。

    NOTE:
      - v0.7.1 時点の入力UIでは「業務内容」は blocks.philosophy.services.items に入る。
        （将来、blocks.service.items に分割された場合も考慮して両方を見る）
    """
    data = p.get("data") if isinstance(p, dict) else {}
    if not isinstance(data, dict):
        data = {}
    step2 = data.get("step2") if isinstance(data.get("step2"), dict) else {}
    blocks = data.get("blocks") if isinstance(data.get("blocks"), dict) else {}

    company_name = str(step2.get("company_name") or "").strip()
    phone = str(step2.get("phone") or "").strip()
    email = str(step2.get("email") or "").strip()
    address = str(step2.get("address") or "").strip()
    catch_copy = str(step2.get("catch_copy") or "").strip()
    # v0.8: お問い合わせ方式（フォーム/メール/外部フォームURL）
    contact_block = blocks.get("contact") if isinstance(blocks.get("contact"), dict) else {}
    contact_mode_raw = str(contact_block.get("form_mode") or "").strip().lower()
    if contact_mode_raw in {"external", "url"}:
        contact_mode = "external"
    elif contact_mode_raw in {"mail", "email"}:
        contact_mode = "mail"
    else:
        contact_mode = "php"
    external_form_url = str(contact_block.get("external_form_url") or "").strip()


    philosophy = blocks.get("philosophy") if isinstance(blocks.get("philosophy"), dict) else {}
    service_block = blocks.get("service") if isinstance(blocks.get("service"), dict) else {}
    faq = blocks.get("faq") if isinstance(blocks.get("faq"), dict) else {}
    news = blocks.get("news") if isinstance(blocks.get("news"), dict) else {}

    # 業務内容 items（優先: philosophy.services.items / fallback: service.items）
    svc_items: list[dict] = []
    try:
        ph_svc = philosophy.get("services") if isinstance(philosophy.get("services"), dict) else {}
        raw = ph_svc.get("items")
        if isinstance(raw, list):
            svc_items = [it for it in raw if isinstance(it, dict)]
        else:
            raw2 = service_block.get("items")
            if isinstance(raw2, list):
                svc_items = [it for it in raw2 if isinstance(it, dict)]
    except Exception:
        svc_items = []

    faq_items = faq.get("items")
    if not isinstance(faq_items, list):
        faq_items = []
    faq_items = [it for it in faq_items if isinstance(it, dict)]

    news_items = news.get("items")
    if not isinstance(news_items, list):
        news_items = []
    news_items = [it for it in news_items if isinstance(it, dict)]

    required = [
        {"key": "company_name", "label": "会社名（基本情報）", "ok": bool(company_name), "hint": "2. 基本情報設定で入力します"},
        {"key": "contact", "label": "お問い合わせ（メール / 外部フォームURL）", "ok": (bool(external_form_url) if contact_mode == "external" else bool(email)), "hint": "2. 基本情報設定（メール）または 3. お問い合わせブロック（外部フォームURL）で入力します"},
        {"key": "address", "label": "住所（アクセス用）", "ok": bool(address), "hint": "2. 基本情報設定で入力します"},
    ]

    recommended = [
        {"key": "catch_copy", "label": "キャッチコピー（ヒーロー）", "ok": bool(catch_copy), "hint": "2. 基本情報設定で入力します"},
        {"key": "philosophy", "label": "私たちの想い（文章）", "ok": bool(str(philosophy.get("body") or "").strip()), "hint": "3. ブロックで入力します"},
        {"key": "service", "label": "業務内容（最低1件）", "ok": any(str(it.get("title") or "").strip() and str(it.get("body") or "").strip() for it in svc_items), "hint": "3. ブロックで入力します"},
        {"key": "faq", "label": "FAQ（任意: 1件以上あると親切）", "ok": any(str(it.get("q") or "").strip() and str(it.get("a") or "").strip() for it in faq_items), "hint": "3. ブロックで入力します"},
        {"key": "news", "label": "お知らせ（任意: 1件以上あると更新感）", "ok": any(str(it.get("title") or "").strip() for it in news_items), "hint": "3. ブロックで入力します"},
    ]

    ok_required = all(bool(x.get("ok")) for x in required)
    return {
        "required": required,
        "recommended": recommended,
        "ok_required": ok_required,
    }

def _is_data_url(s: str) -> bool:
    try:
        return bool(s and isinstance(s, str) and s.startswith("data:") and "base64," in s)
    except Exception:
        return False


def _data_url_meta(s: str) -> tuple[str, bytes]:
    """dataURL -> (mime, bytes). invalidなら ('', b'')"""
    try:
        head, b64part = s.split("base64,", 1)
        mime = head[5:].split(";", 1)[0].strip() or "application/octet-stream"
        data = base64.b64decode(b64part.encode("utf-8"))
        return mime, data
    except Exception:
        return "", b""


def _mime_to_ext(mime: str) -> str:
    m = (mime or "").lower().strip()
    if m in {"image/jpeg", "image/jpg"}:
        return ".jpg"
    if m == "image/png":
        return ".png"
    if m == "image/webp":
        return ".webp"
    if m in {"image/svg+xml", "image/svg"}:
        return ".svg"
    return mimetypes.guess_extension(m) or ".bin"


def _safe_filename(name: str) -> str:
    s = (name or "").strip()
    s = re.sub(r"[\\/:*?\"<>|]+", "_", s)
    s = re.sub(r"\s+", "_", s)
    s = s.strip("._")
    return s or "file"


def collect_project_images(p: dict) -> list[dict]:
    """project.json 内の dataURL 画像を収集（管理者の確認用）。"""
    out: list[dict] = []
    try:
        data = p.get("data") if isinstance(p, dict) else {}
        if not isinstance(data, dict):
            return out
        step2 = data.get("step2") if isinstance(data.get("step2"), dict) else {}
        blocks = data.get("blocks") if isinstance(data.get("blocks"), dict) else {}

        def _add(label: str, data_url: str, filename: str = ""):
            if not _is_data_url(data_url):
                return
            mime, b = _data_url_meta(data_url)
            size_kb = int(round(len(b) / 1024)) if b else 0
            out.append({
                "label": label,
                "filename": filename or "",
                "mime": mime,
                "size_kb": size_kb,
                "data_url": data_url,
            })

        # favicon
        _add("favicon", str(step2.get("favicon_url") or ""), str(step2.get("favicon_filename") or ""))

        hero = blocks.get("hero") if isinstance(blocks.get("hero"), dict) else {}
        urls = hero.get("hero_image_urls")
        names = hero.get("hero_upload_names")
        if isinstance(urls, list):
            for i, url in enumerate(urls):
                nm = ""
                if isinstance(names, list) and i < len(names):
                    nm = str(names[i] or "")
                _add(f"hero[{i+1}]", str(url or ""), nm)

        philosophy = blocks.get("philosophy") if isinstance(blocks.get("philosophy"), dict) else {}
        _add("philosophy_image", str(philosophy.get("image_url") or ""), str(philosophy.get("image_upload_name") or ""))

        service = blocks.get("service") if isinstance(blocks.get("service"), dict) else {}
        _add("service_image", str(service.get("image_url") or ""), str(service.get("image_upload_name") or ""))

        # その他：念のため再帰的に拾う（将来の拡張用）
        def _walk(obj, path=""):
            try:
                if isinstance(obj, dict):
                    for k, v in obj.items():
                        np = f"{path}.{k}" if path else str(k)
                        if isinstance(v, str) and _is_data_url(v):
                            _add(np, v, "")
                        else:
                            _walk(v, np)
                elif isinstance(obj, list):
                    for idx, v in enumerate(obj):
                        _walk(v, f"{path}[{idx}]")
            except Exception:
                pass

        _walk(data, "data")

    except Exception:
        pass

    # 重複除去（同じdataURLが複数経路で拾われることがある）
    uniq = {}
    for it in out:
        key = hashlib.sha1((it.get("data_url") or "").encode("utf-8")).hexdigest()
        uniq[key] = it
    return list(uniq.values())


def remove_data_url_from_project(p: dict, target_data_url: str) -> int:
    """project.json 内から指定dataURL画像を削除する（空文字にする）。

    - 管理者の「画像一覧」からの掃除用
    - 画像が複数箇所で使われている可能性があるため、一致する dataURL は全部消す
    - 関連する filename（upload_name）も、分かる範囲で一緒に消す
    """
    p = normalize_project(p)
    target = str(target_data_url or "")
    if not _is_data_url(target):
        return 0

    cleared = 0
    data = p.get("data") if isinstance(p, dict) else {}
    if not isinstance(data, dict):
        return 0

    step2 = data.get("step2") if isinstance(data.get("step2"), dict) else {}
    blocks = data.get("blocks") if isinstance(data.get("blocks"), dict) else {}

    # favicon
    try:
        if str(step2.get("favicon_url") or "") == target:
            step2["favicon_url"] = ""
            step2["favicon_filename"] = ""
            cleared += 1
    except Exception:
        pass

    # hero
    try:
        hero = blocks.get("hero") if isinstance(blocks.get("hero"), dict) else {}
        urls = hero.get("hero_image_urls")
        names = hero.get("hero_upload_names")
        if isinstance(urls, list):
            for i in range(len(urls)):
                if str(urls[i] or "") == target:
                    urls[i] = ""
                    cleared += 1
                    if isinstance(names, list) and i < len(names):
                        names[i] = ""
    except Exception:
        pass

    # philosophy / services image
    try:
        philosophy = blocks.get("philosophy") if isinstance(blocks.get("philosophy"), dict) else {}
        if str(philosophy.get("image_url") or "") == target:
            philosophy["image_url"] = ""
            philosophy["image_upload_name"] = ""
            cleared += 1

        svc = philosophy.get("services") if isinstance(philosophy.get("services"), dict) else {}
        if isinstance(svc, dict):
            if str(svc.get("image_url") or "") == target:
                svc["image_url"] = ""
                svc["image_upload_name"] = ""
                cleared += 1
    except Exception:
        pass

    # service block image (将来の分離に備える)
    try:
        service = blocks.get("service") if isinstance(blocks.get("service"), dict) else {}
        if str(service.get("image_url") or "") == target:
            service["image_url"] = ""
            service["image_upload_name"] = ""
            cleared += 1
    except Exception:
        pass

    # 念のため：再帰的に一致する dataURL を全部消す
    def _walk(obj):
        nonlocal cleared
        try:
            if isinstance(obj, dict):
                for k in list(obj.keys()):
                    v = obj.get(k)
                    if isinstance(v, str) and v == target:
                        obj[k] = ""
                        cleared += 1
                    else:
                        _walk(v)
            elif isinstance(obj, list):
                for i in range(len(obj)):
                    v = obj[i]
                    if isinstance(v, str) and v == target:
                        obj[i] = ""
                        cleared += 1
                    else:
                        _walk(v)
        except Exception:
            return

    try:
        _walk(data)
    except Exception:
        pass

    return cleared

PRIMARY_COLOR_HEX = {
    "blue": "#1e5eff",
    "red": "#e53935",
    "green": "#2e7d32",
    "orange": "#ef6c00",
    "purple": "#6a1b9a",
    "pink": "#d81b60",
    "teal": "#00897b",
    "gray": "#546e7a",
}


def _simple_md_to_html(md: str) -> str:
    """このアプリの簡易Markdown（privacy向け）を最小変換。"""
    lines = (md or "").splitlines()
    html_parts: list[str] = []
    in_ul = False
    for raw in lines:
        line = raw.rstrip()
        if not line.strip():
            if in_ul:
                html_parts.append("</ul>")
                in_ul = False
            continue
        if line.startswith("## "):
            if in_ul:
                html_parts.append("</ul>")
                in_ul = False
            html_parts.append(f"<h2>{html.escape(line[3:].strip())}</h2>")
            continue
        if line.startswith("- "):
            if not in_ul:
                html_parts.append("<ul>")
                in_ul = True
            html_parts.append(f"<li>{html.escape(line[2:].strip())}</li>")
            continue
        if in_ul:
            html_parts.append("</ul>")
            in_ul = False
        html_parts.append(f"<p>{html.escape(line.strip())}</p>")
    if in_ul:
        html_parts.append("</ul>")
    return "\n".join(html_parts)


def build_privacy_markdown(p: dict) -> str:
    data = p.get("data") if isinstance(p, dict) else {}
    step2 = data.get("step2") if isinstance(data, dict) and isinstance(data.get("step2"), dict) else {}
    company_name = str(step2.get("company_name") or "当社").strip() or "当社"
    address = str(step2.get("address") or "").strip()
    phone = str(step2.get("phone") or "").strip()
    email = str(step2.get("email") or "").strip()

    contact = ""
    try:
        if address:
            contact += f"\n- 住所: {address}"

        if email:
            contact += f"\n- メール: {email}"
    except Exception:
        contact = ""
    if not contact:
        contact = "\n- 連絡先: このページのお問い合わせ欄をご確認ください。"

    return f"""当社（{company_name}）は、個人情報の重要性を認識し、個人情報保護法その他の関係法令・ガイドラインを遵守するとともに、以下のとおり個人情報を適切に取り扱います。

## 1. 取得する情報
当社は、以下の情報を取得することがあります。

- お問い合わせ等でお客様が入力・送信する情報（氏名、連絡先（電話番号/メールアドレス）、お問い合わせ内容 等）
- サイトの利用に伴い自動的に送信される情報（IPアドレス、ブラウザ情報、閲覧履歴、Cookie 等）

## 2. 利用目的
当社は、取得した個人情報を以下の目的で利用します。

- お問い合わせへの対応、連絡
- サービスの提供、運用、改善
- 不正利用の防止、セキュリティ確保

## 3. 第三者提供
当社は、法令で認められる場合を除き、本人の同意なく個人情報を第三者に提供しません。

## 4. 安全管理
当社は、個人情報の漏えい、滅失、毀損等を防止するため、必要かつ適切な安全管理措置を講じます。

## 5. 開示・訂正・削除
本人からの個人情報の開示、訂正、削除等の請求があった場合、本人確認のうえ、法令に従い適切に対応します。

## 6. お問い合わせ窓口
{company_name} へのお問い合わせは、以下までご連絡ください。
{contact}

## 7. 改定
本ポリシーは、必要に応じて内容を改定することがあります。"""


# =========================
# [BLK-07] Export: Contact form (v0.8)
# =========================

CONTACT_FORM_MODE_FORM = "フォーム方式（おすすめ）"
CONTACT_FORM_MODE_EXTERNAL = "外部フォームURL"
CONTACT_FORM_MODE_MAIL = "メール対応（メール作成フォーム）"


def _normalize_contact_form_mode(raw: str) -> str:
    """project.json の contact.form_mode から内部モード（php/mail/external）へ正規化する。"""
    try:
        s = str(raw or "").strip()
    except Exception:
        s = ""
    if not s:
        return "php"

    # 旧値/将来値にも耐える（安全側）
    if s in ("php", "form", "フォーム", "フォーム方式", CONTACT_FORM_MODE_FORM):
        return "php"
    if s in ("mail", "メール", "メール対応", CONTACT_FORM_MODE_MAIL):
        return "mail"
    if s in ("external", "外部", "外部フォーム", CONTACT_FORM_MODE_EXTERNAL):
        return "external"

    if s.startswith("外部"):
        return "external"
    if s.startswith("メール"):
        return "mail"

    return "php"


def _contact_message_hint(step1: dict) -> str:
    """業種別：フォームの「内容」欄の説明文（最小限の差し替え）。"""
    try:
        industry = str((step1 or {}).get("industry") or "").strip()
    except Exception:
        industry = ""

    if industry == "福祉事業所":
        return "（例）見学希望／利用相談／求人について など"
    if ("飲食" in industry) or ("店舗" in industry) or ("小売" in industry):
        return "（例）ご予約／営業時間の確認／商品について など"
    if ("病院" in industry) or ("クリニック" in industry):
        return "（例）受診のご相談／予約／診療時間の確認 など"

    return "（例）ご相談内容／お見積り／資料請求 など"


def _php_escape_single_quoted(s: str) -> str:
    """PHPのシングルクォート文字列用にエスケープする（安全・最小）。"""
    try:
        s = str(s)
    except Exception:
        s = ""
    return s.replace("\\", "\\\\").replace("'", "\\'")


def build_contact_config_php(*, company_name: str, to_email: str, phone: str) -> str:
    """config/config.php（送信先など）を生成する。"""
    site = _php_escape_single_quoted(company_name.strip() or "サイト")
    to = _php_escape_single_quoted(to_email.strip())
    ph = _php_escape_single_quoted(phone.strip())

    return f"""<?php
// CV-HomeBuilder generated config (v0.8)
// ※ ここは「送信先」をまとめた設定ファイルです。
// ※ 値は案件の「基本情報」に合わせて自動生成されています。

return [
  'site_name' => '{site}',
  'to_email' => '{to}',
  'subject_prefix' => 'お問い合わせ',
  'phone' => '{ph}',
];
"""


def build_contact_php(*, company_name: str, to_email: str) -> str:
    """contact.php（フォーム送信の受け口）を生成する。"""
    # contact.php 自体は config/config.php を参照して動作するが、
    # 「PHPが使えないサーバー」でも誤って開かれて困らないように、説明コメントも入れる。
    site = html.escape(company_name.strip() or "サイト")
    to = html.escape(to_email.strip())

    return f"""<?php
// CV-HomeBuilder generated contact handler (v0.8)
// ------------------------------------------------------------
// このファイルは PHP 対応サーバーでのみ動作します。
// PHP が使えない場合は、index.html 側で「メール対応」または「外部フォームURL」を使ってください。
// ------------------------------------------------------------

$config = @include __DIR__ . '/config/config.php';
if (!is_array($config)) {{
  $config = [];
}}

$to = isset($config['to_email']) ? (string)$config['to_email'] : '';
$site_name = isset($config['site_name']) ? (string)$config['site_name'] : '{site}';
$subject_prefix = isset($config['subject_prefix']) ? (string)$config['subject_prefix'] : 'お問い合わせ';

function _cvhb_clean_header_value($s) {{
  $s = trim((string)$s);
  $s = str_replace(["\r", "\n"], ' ', $s);
  return $s;
}}

function _cvhb_redirect($status, $reason = '') {{
  $q = 'status=' . urlencode($status);
  if ($reason !== '') {{
    $q .= '&reason=' . urlencode($reason);
  }}
  header('Location: thanks.html?' . $q);
  exit;
}}

if (!isset($_SERVER['REQUEST_METHOD']) || $_SERVER['REQUEST_METHOD'] !== 'POST') {{
  _cvhb_redirect('ng', 'bad_method');
}}

$honeypot = isset($_POST['website']) ? trim((string)$_POST['website']) : '';
if ($honeypot !== '') {{
  // 迷惑送信対策：ボットはここを埋めがちなので、成功扱いで静かに終了
  _cvhb_redirect('ok', 'spam');
}}

$name = isset($_POST['name']) ? trim((string)$_POST['name']) : '';
$email = isset($_POST['email']) ? trim((string)$_POST['email']) : '';
$tel = isset($_POST['tel']) ? trim((string)$_POST['tel']) : '';
$message = isset($_POST['message']) ? trim((string)$_POST['message']) : '';
$agree = isset($_POST['agree']) ? (string)$_POST['agree'] : '';

if ($to === '') {{
  _cvhb_redirect('ng', 'no_to');
}}
if ($email === '') {{
  _cvhb_redirect('ng', 'no_email');
}}
if (!filter_var($email, FILTER_VALIDATE_EMAIL)) {{
  _cvhb_redirect('ng', 'bad_email');
}}
if ($message === '') {{
  _cvhb_redirect('ng', 'no_message');
}}
if ($agree === '') {{
  _cvhb_redirect('ng', 'no_agree');
}}

$subject = $subject_prefix . '｜' . $site_name;
if ($name !== '') {{
  $subject .= '｜' . _cvhb_clean_header_value($name);
}}

$body = '';
$body .= "このメールはお問い合わせフォームから送信されました。\n\n";
$body .= "【お名前】" . $name . "\n";
$body .= "【メール】" . $email . "\n";
if ($tel !== '') {{
  $body .= "【電話】" . $tel . "\n";
}}
$body .= "\n【内容】\n" . $message . "\n";

// headers
$headers = [];
$headers[] = 'From: ' . _cvhb_clean_header_value($to);
$headers[] = 'Reply-To: ' . _cvhb_clean_header_value($email);
$headers[] = 'Content-Type: text/plain; charset=UTF-8';

$ok = false;
if (function_exists('mb_send_mail')) {{
  mb_language('uni');
  mb_internal_encoding('UTF-8');
  $ok = @mb_send_mail($to, $subject, $body, implode("\r\n", $headers));
}} else {{
  $ok = @mail($to, $subject, $body, implode("\r\n", $headers));
}}

if ($ok) {{
  _cvhb_redirect('ok', '');
}}
_cvhb_redirect('ng', 'send_fail');
"""


def build_thanks_html(*, company_name: str, phone: str, email: str) -> str:
    """thanks.html（送信結果ページ）を生成する。"""
    company = html.escape(company_name.strip() or "会社名")
    phone_esc = html.escape(phone.strip())
    email_esc = html.escape(email.strip())

    # 電話は表示しない（フォーム/メール導線に寄せる）
    mail_html = f'<a class="btn-outline" href="mailto:{email_esc}">メールする</a>' if email_esc else ''
    fallback_actions = mail_html.strip()

    if fallback_actions:
        fallback_actions = f'<div class="contact_actions">{fallback_actions}</div>'

    return f"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>送信結果 | {company}</title>
<link rel="stylesheet" href="assets/site.css">
</head>
<body>
<header class="header">
  <div class="container header_in">
    <div class="brand"><a href="index.html">{company}</a></div>
    <div class="nav"><a href="index.html#contact">お問い合わせ</a><a href="privacy.html">プライバシー</a></div>
  </div>
</header>

<main class="container section">
  <h1 class="h2">送信結果</h1>

  <div id="thanks_ok" class="card" style="display:none;">
    <h2 class="h2" style="margin-top:0;">送信できました ✅</h2>
    <p class="p-muted">お問い合わせありがとうございます。内容を確認し、順次ご連絡します。</p>
    <div class="p-muted" style="margin-top:10px;">※ 自動返信メールは送らない設定です。</div>
    <div style="margin-top:14px;"><a class="btn" href="index.html">トップへ戻る</a></div>
  </div>

  <div id="thanks_ng" class="card" style="display:none;">
    <h2 class="h2" style="margin-top:0;">送信できませんでした</h2>
    <p class="p-muted">原因：<span id="thanks_reason">不明</span></p>

    <div class="card" style="margin-top:12px;">
      <div style="font-weight:800;">代わりの連絡方法</div>
      <div class="p-muted" style="margin-top:6px;">お手数ですが、下記の方法でご連絡ください。</div>
      {fallback_actions}
    </div>

    <div style="margin-top:14px;display:flex;flex-wrap:wrap;gap:10px;">
      <a class="btn" href="index.html#contact">入力画面へ戻る</a>
      <a class="btn-outline" href="index.html">トップへ</a>
    </div>
  </div>

  <div id="thanks_unknown" class="card" style="display:none;">
    <p class="p-muted">このページは「送信結果」の表示用です。</p>
    <div style="margin-top:14px;"><a class="btn" href="index.html#contact">お問い合わせへ戻る</a></div>
  </div>
</main>

<footer class="footer">
  <div class="container">
    <div>© {company}</div>
    <div style="margin-top:6px;"><a href="privacy.html">プライバシーポリシー</a></div>
  </div>
</footer>

<script>
(function() {{
  var sp = new URLSearchParams(location.search);
  var status = (sp.get('status') || '').toLowerCase();
  var reason = (sp.get('reason') || '').toLowerCase();

  var ok = document.getElementById('thanks_ok');
  var ng = document.getElementById('thanks_ng');
  var un = document.getElementById('thanks_unknown');
  var re = document.getElementById('thanks_reason');

  var map = {{
    bad_method: '送信方法が正しくありません（戻ってもう一度お試しください）。',
    no_to: '受信先メールが未設定です（基本情報のメールを入力してください）。',
    no_email: 'メールが未入力です。',
    bad_email: 'メールの形式が正しくありません。',
    no_message: '内容が未入力です。',
    no_agree: 'プライバシーポリシーへの同意が必要です。',
    send_fail: 'サーバーで送信に失敗しました（時間をおいて再試行してください）。',
    spam: '送信完了',
  }};

  function show(el) {{
    if (!el) return;
    el.style.display = 'block';
  }}

  if (status === 'ok') {{
    show(ok);
    return;
  }}
  if (status === 'ng') {{
    if (re) {{
      re.textContent = map[reason] || '送信に失敗しました。';
    }}
    show(ng);
    return;
  }}
  show(un);
}})();
</script>
</body>
</html>
"""


def build_contact_form_files(*, company_name: str, to_email: str, phone: str) -> dict[str, bytes]:
    """書き出しZIPに含める contact.php / thanks.html / config/ を生成する。"""
    cfg = build_contact_config_php(company_name=company_name, to_email=to_email, phone=phone)
    php = build_contact_php(company_name=company_name, to_email=to_email)
    thanks = build_thanks_html(company_name=company_name, phone=phone, email=to_email)
    return {
        "config/config.php": cfg.encode("utf-8"),
        "contact.php": php.encode("utf-8"),
        "thanks.html": thanks.encode("utf-8"),
    }


def build_contact_section_html(
    *,
    step1: dict,
    company_name: str,
    phone: str,
    email: str,
    hours_html: str,
    message_html: str,
    contact_mode: str,
    external_form_url: str,
    contact_warn_html: str,
) -> tuple[str, str]:
    """index.html のお問い合わせ欄（section内）を生成し、必要なscriptタグも返す。"""
    hint = html.escape(_contact_message_hint(step1))
    safe_phone = html.escape(phone.strip())
    safe_email = html.escape(email.strip())
    safe_ext = html.escape(external_form_url.strip())

    # 共通：追加の連絡手段ボタンは出さない（電話も出さない）
    fallback_actions = ""

    # 共通：エラー表示（JSがここへ出す）
    err_box = '<div id="contact_error" class="error_box" style="display:none;"></div>'

    # --- mode: external ---
    if contact_mode == "external":
        inner = f"""<div class="card">
      {f'<p class="p-muted">{message_html}</p>' if message_html else ''}
      <div class="form_note">外部フォームを開きます（別タブ）。</div>

      <div class="form_note" style="margin-top:10px;font-weight:800;">送信前の確認（必須）</div>
      <label style="display:flex;gap:8px;align-items:flex-start;margin-top:8px;">
        <input id="external_agree" type="checkbox">
        <span><a href="privacy.html" target="_blank" rel="noopener">プライバシーポリシー</a>に同意する</span>
      </label>

      <div class="contact_actions">
        <button id="external_form_btn" class="btn" type="button" data-url="{safe_ext}">フォームを開く</button>
      </div>

      {err_box}
      {fallback_actions}
      {contact_warn_html}
    </div>"""

    # --- mode: mail ---
    elif contact_mode == "mail":
        # メールが未設定なら mailto が作れないので、JS側でも止める
        inner = f"""<div class="card">
      {f'<p class="p-muted">{message_html}</p>' if message_html else ''}
      <div class="form_note">「送信」を押すと、端末のメールアプリが開きます（PHP不要）。</div>

      <form id="mail_contact_form" class="contact_form" data-to="{safe_email}" data-subject="お問い合わせ" novalidate>
        <label>お名前（任意）</label>
        <input name="name" type="text" autocomplete="name">

        <label>メール（必須）</label>
        <input name="email" type="email" required autocomplete="email">

        <label>電話（任意）</label>
        <input name="tel" type="tel" autocomplete="tel">

        <label>内容（必須） <span class="p-muted" style="font-weight:600;">{hint}</span></label>
        <textarea name="message" required placeholder="{hint}"></textarea>

        <div class="hp">
          <label>（迷惑対策）この欄は入力しないでください</label>
          <input type="text" name="website" tabindex="-1" autocomplete="off">
        </div>

        <label style="display:flex;gap:8px;align-items:flex-start;margin-top:12px;">
          <input type="checkbox" name="agree" value="1" required>
          <span><a href="privacy.html" target="_blank" rel="noopener">プライバシーポリシー</a>に同意する（必須）</span>
        </label>

        {err_box}

        <div class="contact_actions">
          <button class="btn" type="submit">メールを作成する</button>
        </div>
      </form>

      {fallback_actions}
      {contact_warn_html}
    </div>"""

    # --- mode: php (default) ---
    else:
        submit_disabled = " disabled" if not safe_email else ""
        inner = f"""<div class="card">
      {f'<p class="p-muted">{message_html}</p>' if message_html else ''}
      <div class="form_note">フォームから送信します（PHP対応サーバー向け）。</div>

      <form id="php_contact_form" class="contact_form" action="contact.php" method="post" data-to="{safe_email}" novalidate>
        <label>お名前（任意）</label>
        <input name="name" type="text" autocomplete="name">

        <label>メール（必須）</label>
        <input name="email" type="email" required autocomplete="email">

        <label>電話（任意）</label>
        <input name="tel" type="tel" autocomplete="tel">

        <label>内容（必須） <span class="p-muted" style="font-weight:600;">{hint}</span></label>
        <textarea name="message" required placeholder="{hint}"></textarea>

        <div class="hp">
          <label>（迷惑対策）この欄は入力しないでください</label>
          <input type="text" name="website" tabindex="-1" autocomplete="off">
        </div>

        <label style="display:flex;gap:8px;align-items:flex-start;margin-top:12px;">
          <input type="checkbox" name="agree" value="1" required>
          <span><a href="privacy.html" target="_blank" rel="noopener">プライバシーポリシー</a>に同意する（必須）</span>
        </label>

        {err_box}

        <div class="contact_actions">
          <button class="btn" type="submit"{submit_disabled}>送信する</button>
          {fallback_actions}
        </div>
      </form>

      {f'<div class="p-muted" style="margin-top:10px;">受付: {hours_html}</div>' if hours_html else ''}
      {contact_warn_html}
    </div>"""

    # JS（フォームの必須チェック / 外部フォームの同意チェック / mailto生成）
    script_tag = """<script>
(function() {
  function showError(msg) {
    var box = document.getElementById('contact_error');
    if (box) {
      box.textContent = msg;
      box.style.display = 'block';
      try { box.scrollIntoView({block: 'nearest'}); } catch(e) {}
    } else {
      alert(msg);
    }
    return false;
  }

  // PHP form: validate before submit
  var pf = document.getElementById('php_contact_form');
  if (pf) {
    pf.addEventListener('submit', function(ev) {
      var to = (pf.getAttribute('data-to') || '').trim();
      var email = pf.querySelector('input[name="email"]');
      var msg = pf.querySelector('textarea[name="message"]');
      var agree = pf.querySelector('input[name="agree"]');
      if (!to) { ev.preventDefault(); showError('受信先メールが未設定です（基本情報のメールを入力してください）。'); return; }
      if (!email || !email.value.trim()) { ev.preventDefault(); showError('メールが未入力です。'); return; }
      if (!msg || !msg.value.trim()) { ev.preventDefault(); showError('内容が未入力です。'); return; }
      if (!agree || !agree.checked) { ev.preventDefault(); showError('プライバシーポリシーに同意してください。'); return; }
    });
  }

  // Mail form: build mailto
  var mf = document.getElementById('mail_contact_form');
  if (mf) {
    mf.addEventListener('submit', function(ev) {
      ev.preventDefault();
      var to = (mf.getAttribute('data-to') || '').trim();
      var name = (mf.querySelector('input[name="name"]') || {}).value || '';
      var from = (mf.querySelector('input[name="email"]') || {}).value || '';
      var tel = (mf.querySelector('input[name="tel"]') || {}).value || '';
      var msg = (mf.querySelector('textarea[name="message"]') || {}).value || '';
      var agree = (mf.querySelector('input[name="agree"]') || {}).checked;

      if (!to) { showError('受信先メールが未設定です（基本情報のメールを入力してください）。'); return; }
      if (!from.trim()) { showError('メールが未入力です。'); return; }
      if (!msg.trim()) { showError('内容が未入力です。'); return; }
      if (!agree) { showError('プライバシーポリシーに同意してください。'); return; }

      var subject = (mf.getAttribute('data-subject') || 'お問い合わせ');
      var body = '';
      body += '【お名前】' + name + '\\n';
      body += '【メール】' + from + '\\n';
      if (tel.trim()) { body += '【電話】' + tel + '\\n'; }
      body += '\\n【内容】\\n' + msg;

      // to の危険文字だけ除去（mailto破壊を防ぐ）
      var safeTo = to.replace(/[^0-9A-Za-z@._+-]/g, '');
      location.href = 'mailto:' + safeTo + '?subject=' + encodeURIComponent(subject) + '&body=' + encodeURIComponent(body);
    });
  }

  // External form: require agree to enable button
  var eb = document.getElementById('external_form_btn');
  var ec = document.getElementById('external_agree');
  if (eb && ec) {
    function update() { eb.disabled = !ec.checked; }
    ec.addEventListener('change', update);
    update();

    eb.addEventListener('click', function() {
      var url = (eb.getAttribute('data-url') || '').trim();
      if (!url) { showError('外部フォームURLが未入力です。'); return; }
      try {
        window.open(url, '_blank', 'noopener');
      } catch (e) {
        location.href = url;
      }
    });
  }
})();
</script>"""

    return inner, script_tag


def build_static_site_files(p: dict) -> dict[str, bytes]:
    """案件データから、公開用の静的ファイル一式を生成して返す。"""
    p = normalize_project(p)
    data = p.get("data") if isinstance(p, dict) else {}
    step1 = data.get("step1") if isinstance(data, dict) and isinstance(data.get("step1"), dict) else {}
    step2 = data.get("step2") if isinstance(data, dict) and isinstance(data.get("step2"), dict) else {}
    blocks = data.get("blocks") if isinstance(data, dict) and isinstance(data.get("blocks"), dict) else {}

    project_id = str(p.get("project_id") or "")
    company_name = str(step2.get("company_name") or "会社名").strip() or "会社名"
    catch_copy = str(step2.get("catch_copy") or "").strip()
    phone = str(step2.get("phone") or "").strip()
    address = str(step2.get("address") or "").strip()
    email = str(step2.get("email") or "").strip()

    primary_key = str(step1.get("primary_color") or "blue")
    primary_hex = PRIMARY_COLOR_HEX.get(primary_key, "#1e5eff")

    # --- assets (CSS / images) ---
    files: dict[str, bytes] = {}

    site_css = f""":root{{{_preview_glass_style(step1)}--site-input-bg:{'rgba(13,16,22,0.55)' if primary_key=='black' else 'rgba(255,255,255,0.72)'};--site-input-border:{'rgba(255,255,255,0.16)' if primary_key=='black' else 'rgba(15,23,42,0.12)'};--site-input-bg2:{'rgba(13,16,22,0.40)' if primary_key=='black' else 'rgba(255,255,255,0.62)'};}}
*{{box-sizing:border-box;}}
html{{scroll-behavior:smooth;}}
body{{
  margin:0;
  font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans JP", "Hiragino Kaku Gothic ProN", "Yu Gothic", "Meiryo", sans-serif;
  color: var(--pv-text);
  background-image: var(--pv-bg-img);
  background-size: cover;
  background-position: center;
  background-repeat: no-repeat;
  background-attachment: fixed;
  line-height: 1.7;
}}
a{{color: var(--pv-primary); text-decoration:none;}}
a:hover{{text-decoration:underline;}}

.container{{max-width: 1040px; margin: 0 auto; padding: 0 16px;}}

.header{{
  position: sticky;
  top: 0;
  z-index: 50;
  padding: 12px 0;
  backdrop-filter: blur(16px);
  background: var(--pv-card);
  border-bottom: 1px solid var(--pv-border);
}}
/* old/new class names both supported */
.header_in,
.header .inner{{
  display:flex;
  align-items:center;
  justify-content:space-between;
  gap: 12px;
  flex-wrap: wrap;
}}
.brand{{font-weight: 900; letter-spacing: .02em;}}
.brand a{{color: var(--pv-text); text-decoration:none;}}
.brand a:hover{{text-decoration:none;}}

.nav{{display:flex; gap: 8px; flex-wrap: wrap; font-size: 13px;}}
.nav a{{
  display:inline-flex;
  align-items:center;
  padding: 7px 10px;
  border-radius: 999px;
  background: var(--pv-chip-bg);
  border: 1px solid var(--pv-chip-border);
  color: var(--pv-text);
  font-weight: 800;
  letter-spacing: .01em;
  text-decoration:none;
}}
.nav a:hover{{text-decoration:none; filter: brightness(1.02);}}
.nav a:active{{transform: translateY(1px);}}

main.container{{padding: 18px 0 40px;}}

.hero{{margin-top: 10px;}}
.hero_card{{
  position: relative;
  border: 1px solid var(--pv-border);
  border-radius: 22px;
  overflow: hidden;
  background: var(--pv-card);
  box-shadow: var(--pv-shadow);
  backdrop-filter: blur(12px);
}}

.hero_slider{{position:relative;}}
.hero_slides{{position:relative;width:100%;height:320px;background:rgba(255,255,255,0.30);}}
.hero_slide{{position:absolute;inset:0;opacity:0;transition:opacity .35s ease;}}
.hero_slide.is-active{{opacity:1;}}
.hero_img{{width:100%;height:320px;object-fit:cover;display:block;background:rgba(255,255,255,0.22);}}

.hero_dots{{position:absolute;left:0;right:0;bottom:12px;display:flex;justify-content:center;gap:8px;}}
.hero_dot{{width:10px;height:10px;border-radius:50%;border:1px solid rgba(255,255,255,0.85);background:rgba(0,0,0,0.25);cursor:pointer;padding:0;}}
.hero_dot.is-active{{background:rgba(255,255,255,0.95);}}

.hero_text,
.hero_body{{
  /* Mobile default: below image (like preview mobile) */
  position: static;
  width: min(92%, 680px);
  margin: 14px auto 0;
  padding: 16px 18px;
  border-radius: 18px;
  text-align: center;
  backdrop-filter: blur(12px);
  background: rgba(255,255,255,0.55);
  border: 1px solid rgba(255,255,255,0.42);
  box-shadow: 0 22px 54px rgba(0,0,0,0.12);
}}
.hero_title{{font-size: 24px; margin: 0 0 6px; font-weight: 900; letter-spacing:.01em;}}
.hero_sub{{color: var(--pv-muted); margin: 0; font-weight: 700;}}

.section{{margin-top: 22px;}}
.h2,
.section_title{{font-size: 18px; font-weight: 900; margin: 0 0 10px; letter-spacing:.01em;}}
.p-muted{{color: var(--pv-muted);}}
.card{{
  border: 1px solid var(--pv-border);
  border-radius: 18px;
  background: var(--pv-card);
  padding: 14px;
  box-shadow: var(--pv-shadow);
  backdrop-filter: blur(12px);
}}

.grid2{{display:grid;grid-template-columns:1fr;gap:12px;}}
@media(min-width:860px){{
  .grid2{{grid-template-columns:1fr 1fr;}}
  .hero_slides{{height: 420px;}}
  .hero_img{{height: 420px;}}
  .hero_title{{font-size: 30px;}}
  /* Desktop: overlay caption (like preview pc) */
  .hero_text,
  .hero_body{{
    position: absolute;
    left: 50%;
    bottom: 26px;
    transform: translateX(-50%);
    width: fit-content;
    max-width: min(92%, 980px);
    margin: 0;
    padding: 18px 22px;
    border-radius: 18px;
  }}
}}

.badge{{
  display:inline-flex;
  align-items:center;
  padding: 2px 8px;
  border-radius: 999px;
  background: var(--pv-chip-bg);
  border: 1px solid var(--pv-chip-border);
  font-size: 12px;
  font-weight: 900;
  color: var(--pv-text);
}}

.news_list{{display:grid;gap:12px;}}
.news_item{{
  border: 1px solid var(--pv-border);
  border-radius: 16px;
  padding: 12px;
  background: var(--pv-card);
  box-shadow: var(--pv-shadow);
  backdrop-filter: blur(12px);
}}

.btn,
.btn-outline{{
  display:inline-flex;
  align-items:center;
  justify-content:center;
  gap: 8px;
  padding: 10px 14px;
  border-radius: 999px;
  font-weight: 900;
  letter-spacing:.01em;
  text-decoration:none;
  cursor:pointer;
  user-select:none;
}}
.btn{{
  background: var(--pv-primary);
  color: white;
  border: none;
}}
.btn:hover{{filter: brightness(1.03); text-decoration:none;}}
.btn-outline{{
  background: transparent;
  color: var(--pv-primary);
  border: 1px solid rgba(255,255,255,0.44);
}}
.btn-outline:hover{{text-decoration:none; filter: brightness(1.03);}}
.btn:disabled,
.btn.is-disabled{{opacity:0.6;pointer-events:none;}}

.footer{{border-top:1px solid var(--pv-border); padding: 18px 0; color: var(--pv-muted); font-size: 14px; background: rgba(255,255,255,0.22); backdrop-filter: blur(10px);}}

.faq details{{
  border: 1px solid var(--pv-border);
  border-radius: 16px;
  padding: 10px 12px;
  background: var(--pv-card);
  box-shadow: var(--pv-shadow);
  backdrop-filter: blur(12px);
}}
.faq details+details{{margin-top:10px;}}
.faq summary{{cursor:pointer;}}

.contact_form{{margin-top:12px;}}
.contact_form label{{display:block;font-weight:900;font-size:14px;margin:12px 0 6px;}}
.contact_form input,
.contact_form textarea{{
  width:100%;
  padding:10px 12px;
  border:1px solid var(--site-input-border);
  border-radius:14px;
  font-size:16px;
  font:inherit;
  color: var(--pv-text);
  background: var(--site-input-bg);
}}
.contact_form input:focus,
.contact_form textarea:focus{{
  outline: none;
  box-shadow: 0 0 0 4px var(--pv-primary-weak);
  border-color: rgba(255,255,255,0.40);
  background: var(--site-input-bg2);
}}
.contact_form textarea{{min-height:150px;resize:vertical;}}
.contact_actions{{display:flex;flex-wrap:wrap;gap:10px;margin-top:12px;}}
.form_note{{font-size:13px;color:var(--pv-muted);margin-top:8px;}}
.hp{{position:absolute;left:-9999px;top:-9999px;height:0;width:0;overflow:hidden;}}
.error_box{{border:1px solid rgba(239,68,68,0.65);background:rgba(239,68,68,0.10);padding:10px 12px;border-radius:14px;margin-top:12px;}}
"""
    files["assets/site.css"] = site_css.encode("utf-8")

    def _asset_from_data_url(data_url: str, key: str) -> str:
        """dataURL を assets/img/ 配下のファイルとして書き出し、相対パスを返す。

        重要:
        - ここで「アップロード時の表示用ファイル名（省略された名前）」を使うと、
          ZIP内のファイル名やHTML参照が壊れて見た目が崩れる原因になる。
        - そのため、key（用途/位置） + 内容ハッシュで、常に安全で再現性のある名前にする。
        """
        mime, b = _data_url_meta(data_url)
        ext = _mime_to_ext(mime)
        h = hashlib.sha1(b).hexdigest()[:10] if b else secrets.token_hex(4)

        base = _safe_filename(Path(str(key or "file")).stem)
        try:
            base = base[:40]
        except Exception:
            pass

        fname = f"{base}_{h}{ext}"
        rel = f"assets/img/{fname}"
        files[rel] = b
        return rel

    # favicon
    favicon_href = ""
    fav_url = str(step2.get("favicon_url") or "").strip()
    if _is_data_url(fav_url):
        try:
            favicon_href = _asset_from_data_url(fav_url, f"favicon_{project_id}")
        except Exception:
            favicon_href = ""
    else:
        favicon_href = fav_url

    # hero images
    hero = blocks.get("hero") if isinstance(blocks.get("hero"), dict) else {}
    hero_urls = hero.get("hero_image_urls")
    # NOTE: hero_upload_names は「表示用に省略された名前」が入り得るため、
    #       書き出しファイル名の材料には使わない（ZIP/HTMLが壊れる原因）。
    hero_imgs: list[str] = []
    if isinstance(hero_urls, list):
        for i, url in enumerate(hero_urls):
            u = str(url or "").strip()
            if not u:
                continue
            if _is_data_url(u):
                key = f"hero_{i+1}_{project_id or 'p'}"
                try:
                    hero_imgs.append(_asset_from_data_url(u, key))
                except Exception:
                    pass
            else:
                hero_imgs.append(u)

    hero_main = hero_imgs[0] if hero_imgs else ""

    # philosophy image
    philosophy = blocks.get("philosophy") if isinstance(blocks.get("philosophy"), dict) else {}
    ph_img = str(philosophy.get("image_url") or "").strip()
    ph_img_rel = ""
    if _is_data_url(ph_img):
        try:
            ph_img_rel = _asset_from_data_url(ph_img, f"philosophy_{project_id}")
        except Exception:
            ph_img_rel = ""
    else:
        ph_img_rel = ph_img

    # service image
    service = blocks.get("service") if isinstance(blocks.get("service"), dict) else {}
    svc_img = str(service.get("image_url") or "").strip()
    svc_img_rel = ""
    if _is_data_url(svc_img):
        try:
            svc_img_rel = _asset_from_data_url(svc_img, f"service_{project_id}")
        except Exception:
            svc_img_rel = ""
    else:
        svc_img_rel = svc_img

    # text blocks
    ph_title = str(philosophy.get("title") or "私たちの想い").strip()
    ph_body = html.escape(str(philosophy.get("body") or "").strip()).replace("\n", "<br>")
    svc_title = str(service.get("title") or "業務内容").strip()
    svc_lead = html.escape(str(service.get("lead") or "").strip()).replace("\n", "<br>")

    svc_items = service.get("items")
    if not isinstance(svc_items, list):
        svc_items = []
    svc_items = [it for it in svc_items if isinstance(it, dict)]

    news = blocks.get("news") if isinstance(blocks.get("news"), dict) else {}
    news_items = news.get("items")
    if not isinstance(news_items, list):
        news_items = []
    news_items = [it for it in news_items if isinstance(it, dict)]

    faq = blocks.get("faq") if isinstance(blocks.get("faq"), dict) else {}
    faq_items = faq.get("items")
    if not isinstance(faq_items, list):
        faq_items = []
    faq_items = [it for it in faq_items if isinstance(it, dict)]

    access = blocks.get("access") if isinstance(blocks.get("access"), dict) else {}
    embed_map = bool(access.get("embed_map", True))
    notes = html.escape(str(access.get("notes") or "").strip()).replace("\n", "<br>")

    contact = blocks.get("contact") if isinstance(blocks.get("contact"), dict) else {}
    hours = html.escape(str(contact.get("hours") or "").strip()).replace("\n", "<br>")
    message = html.escape(str(contact.get("message") or "").strip()).replace("\n", "<br>")
    button_text = str(contact.get("button_text") or "お問い合わせ").strip()

    # news pages
    def _news_slug(i: int, it: dict) -> str:
        d = str(it.get("date") or "").strip()
        d = re.sub(r"[^0-9]", "", d)[:8]
        return f"{d or 'news'}_{i+1}.html"

    news_list_items_html = ""
    news_detail_files: dict[str, str] = {}
    for i, it in enumerate(news_items):
        title = html.escape(str(it.get("title") or "").strip() or f"お知らせ{i+1}")
        body = html.escape(str(it.get("body") or "").strip()).replace("\n", "<br>")
        date = html.escape(str(it.get("date") or "").strip())
        cat = html.escape(str(it.get("category") or "").strip())
        slug = _news_slug(i, it)
        link = f"news/{slug}"
        news_list_items_html += f"""<div class="news_item"><div><span class="badge">{cat or "NEWS"}</span>{date}</div><div style="font-weight:800;margin-top:6px;"><a href="{link}">{title}</a></div></div>"""
        detail_html = f"""<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{title} | {html.escape(company_name)}</title><link rel="stylesheet" href="../assets/site.css"></head><body>
<header class="header"><div class="container inner"><div class="brand"><a href="../index.html">{html.escape(company_name)}</a></div><div class="nav"><a href="../news/index.html">お知らせ一覧</a><a href="../privacy.html">プライバシー</a></div></div></header>
<main class="container section"><h1 class="h2">{title}</h1><div class="p-muted">{date} {cat}</div><div class="card" style="margin-top:12px;">{body or "<p class='p-muted'>本文がありません</p>"}</div><div style="margin-top:14px;"><a class="btn-outline" href="../news/index.html">一覧へ戻る</a></div></main>
<footer class="footer"><div class="container">© {html.escape(company_name)}</div></footer></body></html>"""
        news_detail_files[f"news/{slug}"] = detail_html

    news_index_html = f"""<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>お知らせ | {html.escape(company_name)}</title><link rel="stylesheet" href="../assets/site.css"></head><body>
<header class="header"><div class="container inner"><div class="brand"><a href="../index.html">{html.escape(company_name)}</a></div><div class="nav"><a href="../index.html#contact">{html.escape(button_text)}</a><a href="../privacy.html">プライバシー</a></div></div></header>
<main class="container section"><h1 class="h2">お知らせ</h1><div class="news_list">{news_list_items_html or "<p class='p-muted'>お知らせはまだありません</p>"}</div></main>
<footer class="footer"><div class="container"><a href="../index.html">トップへ戻る</a></div></footer></body></html>"""
    files["news/index.html"] = news_index_html.encode("utf-8")
    for path, body in news_detail_files.items():
        files[path] = body.encode("utf-8")

    # map embed
    map_html = ""
    if address:
        q = quote_plus(address)
        if embed_map:
            map_html = f"""<iframe title="map" src="https://www.google.com/maps?q={q}&output=embed" style="width:100%;height:260px;border:0;border-radius:12px;"></iframe>"""
        else:
            map_html = f"""<a class="btn-outline" href="https://www.google.com/maps?q={q}" target="_blank" rel="noopener">地図を開く</a>"""
    else:
        map_html = '<p class="p-muted">住所が未入力のため地図を表示できません</p>'

    # service items html
    svc_items_html = ""
    for it in svc_items:
        t = html.escape(str(it.get("title") or "").strip())
        b = html.escape(str(it.get("body") or "").strip()).replace("\n", "<br>")
        if not (t or b):
            continue
        svc_items_html += f"""<div class="card"><div style="font-weight:800;">{t or "項目"}</div><div class="p-muted" style="margin-top:6px;">{b}</div></div>"""
    if not svc_items_html:
        svc_items_html = '<p class="p-muted">業務内容はまだありません</p>'

    # faq html
    faq_html = ""
    for it in faq_items:
        q = html.escape(str(it.get("q") or "").strip())
        a = html.escape(str(it.get("a") or "").strip()).replace("\n", "<br>")
        if not (q or a):
            continue
        faq_html += f"""<details><summary style="font-weight:800;">{q or "質問"}</summary><div style="margin-top:8px;" class="p-muted">{a}</div></details>"""
    if not faq_html:
        faq_html = '<p class="p-muted">FAQはまだありません</p>'

    hero_sub = html.escape(catch_copy) if catch_copy else html.escape("ここにキャッチコピーが入ります")

    # 書き出しHTMLもプレビューの見た目に寄せる：ヒーローは複数画像ならスライダー化
    hero_slider_script_tag = ""
    if len(hero_imgs) >= 2:
        slides = ""
        dots = ""
        for i, src in enumerate(hero_imgs):
            esc = html.escape(src)
            active = " is-active" if i == 0 else ""
            slides += f'<div class="hero_slide{active}" data-idx="{i}"><img class="hero_img" src="{esc}" alt="hero {i+1}"></div>'
            dots += f'<button type="button" class="hero_dot{active}" data-idx="{i}" aria-label="スライド{i+1}"></button>'
        hero_media_tag = f'<div class="hero_slider"><div class="hero_slides">{slides}</div><div class="hero_dots">{dots}</div></div>'
        hero_slider_script_tag = """<script>
(function(){
  var slider = document.querySelector('.hero_slider');
  if (!slider) return;
  var slides = Array.prototype.slice.call(slider.querySelectorAll('.hero_slide'));
  var dots = Array.prototype.slice.call(slider.querySelectorAll('.hero_dot'));
  if (!slides.length) return;
  var idx = 0;
  function show(i){
    idx = (i + slides.length) % slides.length;
    slides.forEach(function(s, j){ s.classList.toggle('is-active', j === idx); });
    dots.forEach(function(d, j){ d.classList.toggle('is-active', j === idx); });
  }
  dots.forEach(function(d){
    d.addEventListener('click', function(){
      var n = parseInt(d.getAttribute('data-idx') || '0', 10);
      if (!isNaN(n)) show(n);
    });
  });
  show(0);
  setInterval(function(){ show(idx + 1); }, 5000);
})();
</script>"""
    else:
        hero_media_tag = f'<img class="hero_img" src="{html.escape(hero_main)}" alt="hero">' if hero_main else '<div class="hero_img"></div>'

    # --- contact form mode (v0.8) ---
    contact_mode_raw = ""
    try:
        contact_mode_raw = str(contact.get("form_mode") or "").strip()
    except Exception:
        contact_mode_raw = ""
    contact_mode = _normalize_contact_form_mode(contact_mode_raw)

    external_form_url = ""
    try:
        external_form_url = str(contact.get("external_form_url") or "").strip()
    except Exception:
        external_form_url = ""

    # 連絡先/設定の未入力チェック（現場で迷わないための警告）
    contact_warn_html = ""
    if contact_mode in {"php", "mail"} and not email:
        contact_warn_html = '<p class="p-muted" style="margin-top:10px;">メールアドレスが未入力です（フォーム送信にはメールが必要です）</p>'
    elif contact_mode == "external" and not external_form_url:
        contact_warn_html = '<p class="p-muted" style="margin-top:10px;">外部フォームURLが未入力です（お問い合わせブロックで入力）</p>'

    contact_section_html, contact_script_tag = build_contact_section_html(
        step1=step1,
        company_name=company_name,
        phone=phone,
        email=email,
        hours_html=hours,
        message_html=message,
        contact_mode=contact_mode,
        external_form_url=external_form_url,
        contact_warn_html=contact_warn_html,
    )

    # index page

    index_html = f"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(company_name)}</title>
<link rel="stylesheet" href="assets/site.css">
{f'<link rel="icon" href="{html.escape(favicon_href)}">' if favicon_href else ''}
</head>
<body>
<header class="header">
  <div class="container inner">
    <div class="brand"><a href="#top">{html.escape(company_name)}</a></div>
    <nav class="nav">
      <a href="#about">私たちの想い</a>
      <a href="#service">業務内容</a>
      <a href="news/index.html">お知らせ</a>
      <a href="#faq">FAQ</a>
      <a href="#access">アクセス</a>
      <a href="#contact">{html.escape(button_text)}</a>
      <a href="privacy.html">プライバシー</a>
    </nav>
  </div>
</header>

<main id="top" class="container">
  <section class="hero">
    <div class="hero_card">
      {hero_media_tag}
      <div class="hero_text">
        <h1 class="hero_title">{html.escape(company_name)}</h1>
        <div class="hero_sub">{hero_sub}</div>
      </div>
    </div>
  </section>

  <section id="about" class="section">
    <h2 class="h2">{html.escape(ph_title)}</h2>
    <div class="grid2">
      <div class="card">{ph_body or '<p class="p-muted">内容はまだありません</p>'}</div>
      <div class="card">
        {f'<img src="{html.escape(ph_img_rel)}" alt="about" style="width:100%;height:260px;object-fit:cover;border-radius:12px;">' if ph_img_rel else '<p class="p-muted">画像は未設定です</p>'}
      </div>
    </div>
  </section>

  <section id="service" class="section">
    <h2 class="h2">{html.escape(svc_title)}</h2>
    {f'<p class="p-muted">{svc_lead}</p>' if svc_lead else ''}
    <div class="grid2">
      <div class="card">
        {f'<img src="{html.escape(svc_img_rel)}" alt="service" style="width:100%;height:260px;object-fit:cover;border-radius:12px;">' if svc_img_rel else '<p class="p-muted">画像は未設定です</p>'}
      </div>
      <div class="news_list">{svc_items_html}</div>
    </div>
  </section>

  <section id="news" class="section">
    <h2 class="h2">お知らせ</h2>
    <div class="news_list">{news_list_items_html or '<p class="p-muted">お知らせはまだありません</p>'}</div>
    <div style="margin-top:12px;"><a class="btn-outline" href="news/index.html">お知らせ一覧へ</a></div>
  </section>

  <section id="faq" class="section faq">
    <h2 class="h2">FAQ</h2>
    {faq_html}
  </section>

  <section id="access" class="section">
    <h2 class="h2">アクセス</h2>
    <div class="grid2">
      <div class="card">
        <div style="font-weight:800;">住所</div>
        <div class="p-muted" style="margin-top:6px;">{html.escape(address) if address else '（未入力）'}</div>
        {f'<div class="p-muted" style="margin-top:10px;">{notes}</div>' if notes else ''}
        <div style="margin-top:12px;">{map_html}</div>
      </div>
      <div class="card">
        <div style="font-weight:800;">連絡先</div>
        <div class="p-muted" style="margin-top:6px;">{html.escape(email) if email else '（未入力）'}</div>
        {f'<div class="p-muted" style="margin-top:10px;">受付: {hours}</div>' if hours else ''}
      </div>
    </div>
  </section>

  <section id="contact" class="section">
    <h2 class="h2">{html.escape(button_text)}</h2>
    {contact_section_html}
  </section>
</main>

<footer class="footer">
  <div class="container">
    <div>© {html.escape(company_name)}</div>
    <div style="margin-top:6px;"><a href="privacy.html">プライバシーポリシー</a></div>
  </div>
</footer>
</body>
</html>
"""

    # index: append scripts（hero slider + contact form）
    try:
        script_tags = "\n".join([x for x in [hero_slider_script_tag, contact_script_tag] if x])
        if script_tags:
            index_html = index_html.replace("</footer>\n</body>", f"</footer>\n{script_tags}\n</body>")
    except Exception:
        pass

    files["index.html"] = index_html.encode("utf-8")

    # privacy page
    privacy_md = build_privacy_markdown(p)
    privacy_html = f"""<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>プライバシーポリシー | {html.escape(company_name)}</title><link rel="stylesheet" href="assets/site.css"></head><body>
<header class="header"><div class="container inner"><div class="brand"><a href="index.html">{html.escape(company_name)}</a></div><div class="nav"><a href="news/index.html">お知らせ</a><a href="index.html#contact">{html.escape(button_text)}</a></div></div></header>
<main class="container section"><h1 class="h2">プライバシーポリシー</h1><div class="card">{_simple_md_to_html(privacy_md)}</div><div style="margin-top:14px;"><a class="btn-outline" href="index.html">トップへ戻る</a></div></main>
<footer class="footer"><div class="container">© {html.escape(company_name)}</div></footer></body></html>"""
    files["privacy.html"] = privacy_html.encode("utf-8")

    # contact form files (v0.8)
    try:
        files.update(build_contact_form_files(company_name=company_name, to_email=email, phone=phone))
    except Exception:
        pass

    return files


def validate_static_site_files(files: dict[str, bytes]) -> tuple[bool, list[str]]:
    """静的サイト生成結果の簡易バリデーション。

    目的:
    - ZIPを書き出せても「中身が壊れていて見た目が崩れる」事故を防ぐ
    - 15歳でも原因が追えるよう、失敗時は『どこがダメか』を短い文章で返す

    ※ 厳密なHTML検証ではなく、よくある致命傷だけを確実に弾く。
    """
    errors: list[str] = []

    try:
        if not isinstance(files, dict) or not files:
            return False, ["生成ファイルが空です（内部エラー）"]
    except Exception:
        return False, ["生成ファイルの形式が不正です（内部エラー）"]

    # 必須ファイル
    for req in ("index.html", "privacy.html", "assets/site.css"):
        if req not in files:
            errors.append(f"必須ファイルがありません: {req}")

    # パスの安全性（zip slip 対策 + 文字化け/事故対策）
    for path, content in list(files.items()):
        try:
            p = str(path or "")
            if not p:
                errors.append("空のパスが含まれています")
                continue
            if p.startswith("/") or p.startswith("\\") or ":" in p:
                errors.append(f"不正なパス: {p}")
                continue
            if "\\" in p:
                errors.append(f"バックスラッシュを含むパス: {p}")
                continue
            parts = [x for x in p.split("/") if x]
            if any(x == ".." for x in parts):
                errors.append(f"危険な相対パス: {p}")
                continue
            if not isinstance(content, (bytes, bytearray)):
                errors.append(f"内容がbytesではありません: {p}")
        except Exception:
            errors.append("ファイル一覧の検証中に例外が発生しました")

    # HTMLの致命傷チェック（index中心）
    try:
        idx = files.get("index.html", b"")
        idx_s = idx.decode("utf-8", errors="replace")

        # CSS参照
        if 'href="assets/site.css"' not in idx_s:
            errors.append("index.html が assets/site.css を参照していません")

        # ありがちな壊れ方: button type の引用符抜け（HTMLが全崩れ）
        if 'type="submit>' in idx_s or 'type="submit disabled>' in idx_s:
            errors.append("index.html の送信ボタン(type=submit)の引用符が欠けています")

        # ありがちな壊れ方: JSの \n が \n ではなく改行になっている（JS構文エラー）
        if re.search(r"body\s*\+=\s*'【お名前】'\s*\+\s*name\s*\+\s*\n\s*';", idx_s):
            errors.append("index.html のJS内で\\nが改行になっており、スクリプトが壊れています")

        # assets/ 参照の実在チェック（../ を剥がして照合）
        for m in re.findall(r"(?:src|href)=['\"]([^'\"]+)['\"]", idx_s):
            ref = (m or "").strip()
            if not ref:
                continue
            if ref.startswith("http://") or ref.startswith("https://") or ref.startswith("mailto:") or ref.startswith("#"):
                continue
            # normalize ./ ../
            r = ref
            while r.startswith("./"):
                r = r[2:]
            while r.startswith("../"):
                r = r[3:]
            if r.startswith("assets/") and r not in files:
                errors.append(f"index.html が存在しないファイルを参照しています: {ref}")
    except Exception:
        errors.append("index.html の検証中に例外が発生しました")

    # 追加: 全HTMLで assets/ 参照が存在するか（news/ 配下は ../assets/... になるため ../ を剥がす）
    try:
        for fpath, bb in files.items():
            fp = str(fpath or "")
            if not fp.endswith(".html"):
                continue
            s = (bb or b"").decode("utf-8", errors="replace")
            for m in re.findall(r"(?:src|href)=['\"]([^'\"]+)['\"]", s):
                ref = (m or "").strip()
                if not ref:
                    continue
                if ref.startswith("http://") or ref.startswith("https://") or ref.startswith("mailto:") or ref.startswith("#"):
                    continue

                r = ref
                while r.startswith("./"):
                    r = r[2:]
                while r.startswith("../"):
                    r = r[3:]

                if r.startswith("assets/") and r not in files:
                    errors.append(f"{fp} が存在しないファイルを参照しています: {ref}")
    except Exception:
        errors.append("HTML参照（assets/）の検証中に例外が発生しました")

    ok = len(errors) == 0
    return ok, errors


def build_site_zip_filename(p: dict, *, dt: Optional[datetime] = None) -> str:
    """書き出しZIPのファイル名を生成する。

    形式: YYMMDD_案件名_site.zip
    例: 260223_テスト4_site.zip

    ※ 同日に同名バックアップが既にある場合は、保存側で _2, _3 ... を付けて上書きを避ける。
    """
    try:
        p = normalize_project(p)
    except Exception:
        p = p if isinstance(p, dict) else {}

    if dt is None:
        dt = datetime.now(JST)

    date_prefix = dt.strftime("%y%m%d")

    try:
        name = str(p.get("project_name") or "").strip()
    except Exception:
        name = ""

    if not name or name == "(no name)":
        try:
            name = str(p.get("project_id") or "project").strip()
        except Exception:
            name = "project"

    safe_name = _safe_filename(name)
    # 長すぎると扱いにくいので、ほどほどに制限
    try:
        safe_name = safe_name[:40]
    except Exception:
        pass
    safe_name = safe_name or "project"

    return f"{date_prefix}_{safe_name}_site.zip"


def build_site_zip_bytes(p: dict) -> tuple[bytes, str]:
    """静的サイト一式をZIP化して返す。"""
    p = normalize_project(p)
    files = build_static_site_files(p)

    # 生成物の簡易検証（壊れたZIPを配らない）
    ok, errs = validate_static_site_files(files)
    if not ok:
        msg = " / ".join(errs[:5])
        if len(errs) > 5:
            msg += f" …ほか{len(errs)-5}件"
        raise RuntimeError(f"ZIP書き出しに失敗しました（生成ファイルが壊れています）: {msg}")

    mem = BytesIO()
    write_errors: list[str] = []
    with zipfile.ZipFile(mem, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for path, content in files.items():
            try:
                z.writestr(path, content)
            except Exception as e:
                # ここで握りつぶすと『見た目が崩れたZIP』になるので、集計して最後に止める
                write_errors.append(f"{path} ({type(e).__name__})")

    if write_errors:
        msg = ", ".join(write_errors[:5])
        if len(write_errors) > 5:
            msg += f" …ほか{len(write_errors)-5}件"
        raise RuntimeError(f"ZIP作成中にファイル書き込みで失敗しました: {msg}")

    filename = build_site_zip_filename(p)
    return mem.getvalue(), filename


def save_site_zip_backup_to_project(p: dict, actor: User, zip_bytes: bytes, filename: str) -> str:
    """書き出しZIPを案件内（SFTP To Go）へバックアップ保存する。

    - デフォルト名: YYMMDD_案件名_site.zip
    - 既に同名がある場合は _2, _3 ... を付けて上書き事故を避ける
    """
    p = normalize_project(p)
    pid = str(p.get("project_id") or "project")

    desired = str(filename or "").strip() or build_site_zip_filename(p)

    # ファイル名を安全にする（日本語OKだが、念のため危険文字だけ置換）
    base = _safe_filename(Path(desired).stem) or "backup"
    try:
        base = base[:80]
    except Exception:
        pass

    remote_dir = f"{project_dir(pid)}/backups"

    with sftp_client() as sftp:
        # 同名があるなら _2, _3... を付与（同日に複数保存しても上書きしない）
        cand = f"{base}.zip"
        for i in range(2, 100):
            try:
                sftp.stat(f"{remote_dir}/{cand}")
                cand = f"{base}_{i}.zip"
                continue
            except Exception:
                break

        remote_path = f"{remote_dir}/{cand}"
        sftp_write_bytes(sftp, remote_path, zip_bytes or b"")

    # ログ（SFTPのURL/パスワード等は出さない）
    try:
        safe_log_action(actor, "project_export_backup_zip", details=json.dumps({"project_id": pid, "file": cand}, ensure_ascii=False))
    except Exception:
        pass

    return remote_path




def parse_cleanup_exclude_list(raw) -> list[str]:
    """不要ファイル削除で残したいファイルの除外リストを正規化する。

    - 1行1つ（例: robots.txt）
    - ワイルドカード（* / ?）OK（例: *.xml）
    - 先頭/末尾の空白は削除
    - 先頭の / は無視（相対パス扱い）
    - # で始まる行はコメント扱いで無視
    """
    try:
        lines: list[str] = []
        if isinstance(raw, list):
            lines = [str(x) for x in raw]
        else:
            lines = str(raw or "").splitlines()

        out: list[str] = []
        for ln in lines:
            s = str(ln or "").strip()
            if not s:
                continue
            if s.startswith("#"):
                continue
            s = s.lstrip("/").strip()
            if not s:
                continue
            # 事故防止（明らかに危険な指定は無視）
            if ".." in s.replace("\\", "/").split("/"):
                continue
            out.append(s)

        # uniq (keep order)
        seen = set()
        uniq: list[str] = []
        for s in out:
            if s in seen:
                continue
            seen.add(s)
            uniq.append(s)
        return uniq
    except Exception:
        return []


def is_excluded_path(rel_path: str, patterns: list[str]) -> bool:
    """rel_path が除外パターンに一致するか（fnmatch）。"""
    try:
        rel = str(rel_path or "").lstrip("/")
        pats = patterns or []
        for p in pats:
            pp = str(p or "").strip()
            if not pp:
                continue
            try:
                if fnmatch.fnmatch(rel, pp):
                    return True
            except Exception:
                continue
        return False
    except Exception:
        return False


def list_project_backup_zips(project_id: str) -> list[dict]:
    """案件ディレクトリ内の backups/*.zip を一覧する（新しい順）。"""
    pid = (project_id or "").strip()
    if not pid:
        return []
    remote_dir = f"{project_dir(pid)}/backups"
    out: list[dict] = []
    with sftp_client() as sftp:
        try:
            items = sftp.listdir_attr(remote_dir)
        except Exception:
            return []
        for it in items:
            try:
                if stat.S_ISDIR(it.st_mode):
                    continue
                fn = str(it.filename or "")
                if not fn.lower().endswith(".zip"):
                    continue
                size_kb = int(round(int(getattr(it, "st_size", 0) or 0) / 1024))
                mtime_iso = ""
                try:
                    mt = int(getattr(it, "st_mtime", 0) or 0)
                    if mt > 0:
                        mtime_iso = datetime.fromtimestamp(mt, tz=JST).replace(microsecond=0).isoformat()
                except Exception:
                    mtime_iso = ""
                out.append({"filename": fn, "size_kb": size_kb, "mtime": mtime_iso})
            except Exception:
                continue
    # mtime（更新日時）降順で並べる（名前規則が変わっても安定）
    try:
        out.sort(key=lambda x: str(x.get("mtime") or ""), reverse=True)
    except Exception:
        pass
    return out


def read_project_backup_zip_bytes(project_id: str, filename: str) -> bytes:
    """案件内バックアップZIPを読み込む（安全チェックあり）。"""
    pid = (project_id or "").strip()
    fn = str(filename or "").strip()
    if not pid:
        raise ValueError("project_id is empty")
    if not fn or "/" in fn or "\\" in fn:
        raise ValueError("invalid filename")
    if not fn.lower().endswith(".zip"):
        raise ValueError("filename must be .zip")
    remote_path = f"{project_dir(pid)}/backups/{fn}"
    with sftp_client() as sftp:
        return sftp_read_bytes(sftp, remote_path)


def delete_project_backup_zip(project_id: str, filename: str, actor: Optional[User] = None) -> bool:
    """案件内バックアップZIPを削除する（安全チェックあり）。"""
    pid = (project_id or "").strip()
    fn = str(filename or "").strip()
    if not pid:
        raise ValueError("project_id is empty")
    if not fn or "/" in fn or "\\" in fn or ".." in fn:
        raise ValueError("invalid filename")
    if not fn.lower().endswith(".zip"):
        raise ValueError("filename must be .zip")

    remote_path = f"{project_dir(pid)}/backups/{fn}"
    deleted = False
    with sftp_client() as sftp:
        try:
            sftp.remove(remote_path)
            deleted = True
        except FileNotFoundError:
            deleted = False

    try:
        safe_log_action(
            actor,
            "project_backup_zip_delete",
            json.dumps({"project_id": pid, "filename": fn, "deleted": bool(deleted)}, ensure_ascii=False),
        )
    except Exception:
        pass

    return bool(deleted)


def zip_bytes_to_site_files(zip_bytes: bytes) -> dict[str, bytes]:
    """ZIPバイト列を {rel_path: bytes} に展開（安全なパスだけ）。"""
    out: dict[str, bytes] = {}
    if not zip_bytes:
        return out
    try:
        mem = BytesIO(zip_bytes)
        with zipfile.ZipFile(mem, "r") as z:
            for info in z.infolist():
                try:
                    if info.is_dir():
                        continue
                    name = str(info.filename or "").replace("\\", "/").lstrip("/")
                    if not name or name.endswith("/"):
                        continue
                    # 事故防止: 絶対パス/親ディレクトリは拒否
                    parts = [p for p in name.split("/") if p]
                    if any(p in {".", ".."} for p in parts):
                        continue
                    safe_name = "/".join(parts)
                    if not safe_name:
                        continue
                    out[safe_name] = z.read(info)
                except Exception:
                    continue
    except Exception:
        return {}
    return out


def inspect_site_zip_bytes(zip_bytes: bytes) -> dict:
    """復元前チェック用: ZIPの中身を軽く確認する（メタ情報のみ）。

    - ZIP内ファイル数
    - 主要ファイルの有無（index.html / assets/site.css / privacy.html / news/index.html）

    ※ バイナリ内容は見ない（ログにも出さない）
    """
    res = {
        "ok": False,
        "file_count": 0,
        "required": {},
        "missing": [],
        "error": "",
    }
    if not zip_bytes:
        res["error"] = "ZIPが空です"
        return res

    try:
        mem = BytesIO(zip_bytes)
        with zipfile.ZipFile(mem, "r") as z:
            names: list[str] = []
            for info in z.infolist():
                try:
                    if info.is_dir():
                        continue
                    name = str(info.filename or "").replace("\\", "/").lstrip("/")
                    if not name or name.endswith("/"):
                        continue
                    # 事故防止: 絶対パス/親ディレクトリは拒否
                    parts = [p for p in name.split("/") if p]
                    if any(p in {".", ".."} for p in parts):
                        continue
                    safe_name = "/".join(parts)
                    if safe_name:
                        names.append(safe_name)
                except Exception:
                    continue

        res["file_count"] = len(names)
        s = set(names)

        required = ["index.html", "assets/site.css", "privacy.html", "news/index.html"]
        req_map = {k: (k in s) for k in required}
        res["required"] = req_map
        res["missing"] = [k for k, v in req_map.items() if not v]

        # 最低限 index.html があって、空じゃなければOK扱い
        res["ok"] = bool(res["file_count"] > 0 and req_map.get("index.html"))
        return res

    except zipfile.BadZipFile:
        res["error"] = "ZIPが壊れています（読み込めません）"
        return res
    except Exception as e:
        res["error"] = sanitize_error_text(e)
        return res


def publish_site_files_via_sftp(
    *,
    host: str,
    port: int,
    user: str,
    password: str,
    remote_dir: str,
    files: dict[str, bytes],
    actor: Optional[User],
    project_id: str,
    action: str,
    delete_extra: bool = False,
    exclude_patterns: Optional[list[str]] = None,
) -> tuple[bool, str, dict]:
    """静的サイトのファイル群をSFTPへアップロードする（共通処理）。

    戻り値:
      (ok, message, detail)

    detail 例:
      {
        "total": 42,
        "success": 42,
        "failed": 0,
        "failed_files": [],
        "delete_extra": False,
        "deleted": 0,
        "delete_failed": 0,
        "cleanup_warn": "",
        "cleanup_skipped": False,
        "exclude": 0,
      }

    - files: {rel_path: bytes}
    - delete_extra=True のとき（危険）:
        公開ディレクトリ配下の「このビルダーが作りそうな拡張子」のファイルだけを対象に
        『今回の出力に含まれないもの』を削除する（事故防止の安全フィルタあり）
    - exclude_patterns: 不要ファイル削除で「消さない」リスト（1行1つ / ワイルドカード可）
    """
    host = str(host or "").strip()
    user = str(user or "").strip()
    remote_dir = str(remote_dir or "").strip()
    try:
        port = int(port or 22)
    except Exception:
        port = 22

    excludes = exclude_patterns or []

    detail: dict = {
        "total": 0,
        "success": 0,
        "failed": 0,
        "failed_files": [],
        "delete_extra": bool(delete_extra),
        "deleted": 0,
        "delete_failed": 0,
        "cleanup_warn": "",
        "cleanup_skipped": False,
        "exclude": len(excludes),
    }

    if not (host and user and remote_dir):
        return False, "SFTP情報（host/user/dir）が未入力です", detail
    if not password:
        return False, "SFTPパスワードが未入力です", detail

    rd = remote_dir.rstrip("/")
    if rd in ("", "/"):
        return False, "安全のため、公開ディレクトリが不正です（'/' は不可）", detail

    # files 正規化
    out_files: dict[str, bytes] = {}
    try:
        for k, v in (files or {}).items():
            rk = str(k or "").replace("\\", "/").lstrip("/")
            if not rk:
                continue
            # 事故防止: ../ を含むパスは拒否
            parts = [p for p in rk.split("/") if p]
            if any(p in {".", ".."} for p in parts):
                continue
            out_files["/".join(parts)] = v if isinstance(v, (bytes, bytearray)) else bytes(v or b"")
    except Exception:
        out_files = {}

    total = len(out_files)
    detail["total"] = total
    if total <= 0:
        return False, "アップロード対象ファイルがありません", detail

    keep_files = set()
    for k in out_files.keys():
        kk = str(k or "").lstrip("/")
        if kk:
            keep_files.add(kk)

    # NOTE: パスワードはログに出さない（host/user/dir もマスク）
    try:
        print(
            "[PUBLISH] start",
            json.dumps(
                {
                    "target": f"{_mask_text_keep_ends(host)}{_mask_remote_dir(rd)}",
                    "port": port,
                    "user": _mask_text_keep_ends(user, head=1, tail=0),
                    "files": total,
                    "delete_extra": bool(delete_extra),
                    "exclude": len(excludes),
                    "action": action,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    except Exception:
        pass

    transport = paramiko.Transport((host, port))
    deleted = 0
    delete_failed = 0
    cleanup_warn = ""
    cleanup_skipped = False

    success = 0
    failed_files: list[str] = []
    fail_logged = 0
    try:
        transport.connect(username=user, password=password)
        sftp = paramiko.SFTPClient.from_transport(transport)

        # 1) upload（失敗しても続行して、部分成功を検知する）
        for rel_path, content in out_files.items():
            rpath = rd.rstrip("/") + "/" + str(rel_path).lstrip("/")
            rdir = "/".join(rpath.split("/")[:-1])
            try:
                sftp_mkdirs(sftp, rdir)
            except Exception:
                pass

            try:
                with sftp.open(rpath, "wb") as f:
                    f.write(content)
                success += 1
            except Exception as e:
                failed_files.append(str(rel_path)[:160])
                # 失敗ログ（安全な範囲）: 多すぎるとDBが辛いので、最初の10件だけ残す
                if actor and fail_logged < 10:
                    try:
                        safe_log_action(
                            actor,
                            f"{action}_failed",
                            details=json.dumps(
                                {
                                    "project_id": project_id,
                                    "phase": "upload",
                                    "file": str(rel_path)[:160],
                                    "error": sanitize_error_text(e),
                                },
                                ensure_ascii=False,
                            ),
                        )
                        fail_logged += 1
                    except Exception:
                        pass
                continue

        failed = len(failed_files)
        detail["success"] = success
        detail["failed"] = failed
        detail["failed_files"] = failed_files[:200]  # UI用に上限

        # 2) optional cleanup (danger)
        if delete_extra:
            if failed > 0:
                cleanup_skipped = True
                cleanup_warn = "アップロードに失敗があったため、不要ファイル削除は行いませんでした"
            else:
                try:
                    existing = _remote_list_files_recursive(sftp, rd)
                    if len(existing) >= 8000:
                        cleanup_warn = "ファイル数が多すぎるため（8000件超）、不要ファイル削除は中止しました"
                    else:
                        extra_all = [p for p in existing if (p not in keep_files and _remote_is_delete_candidate(p))]
                        # 除外
                        extra = [p for p in extra_all if not is_excluded_path(p, excludes)]

                        for rel in extra:
                            full = rd.rstrip("/") + "/" + rel.lstrip("/")
                            try:
                                sftp.remove(full)
                                deleted += 1
                            except Exception:
                                delete_failed += 1

                        if delete_failed > 0:
                            cleanup_warn = f"不要ファイル削除で失敗がありました（成功{deleted} / 失敗{delete_failed}）"
                        # 除外が効いている旨（ログ用に軽く残す）
                        if excludes and len(extra_all) != len(extra) and not cleanup_warn:
                            cleanup_warn = f"除外リストにより {len(extra_all) - len(extra)}件を削除対象から外しました"
                except Exception as e:
                    cleanup_warn = f"不要ファイル削除に失敗しました: {sanitize_error_text(e)}"

        detail["deleted"] = deleted
        detail["delete_failed"] = delete_failed
        detail["cleanup_warn"] = cleanup_warn
        detail["cleanup_skipped"] = bool(cleanup_skipped)

        # publish log（host等は保存しない）
        try:
            if actor:
                safe_log_action(
                    actor,
                    action,
                    details=json.dumps(
                        {
                            "project_id": project_id,
                            "files": total,
                            "success": success,
                            "failed": failed,
                            "delete_extra": bool(delete_extra),
                            "deleted": deleted,
                            "delete_failed": delete_failed,
                            "cleanup_warn": cleanup_warn,
                            "cleanup_skipped": bool(cleanup_skipped),
                            "exclude": len(excludes),
                        },
                        ensure_ascii=False,
                    ),
                )
        except Exception:
            pass

        try:
            print(
                "[PUBLISH] done",
                json.dumps(
                    {
                        "files": total,
                        "success": success,
                        "failed": failed,
                        "delete_extra": bool(delete_extra),
                        "deleted": deleted,
                        "delete_failed": delete_failed,
                        "cleanup_warn": cleanup_warn,
                        "cleanup_skipped": bool(cleanup_skipped),
                        "exclude": len(excludes),
                        "action": action,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
        except Exception:
            pass

        # message
        if failed <= 0:
            if delete_extra:
                if cleanup_warn:
                    return True, f"（注意）アップロード完了（{total}ファイル）。不要ファイル削除: {deleted}件。{cleanup_warn}", detail
                return True, f"アップロード完了（{total}ファイル）。不要ファイル削除: {deleted}件", detail
            return True, f"アップロード完了（{total}ファイル）", detail

        # 部分成功 or 全失敗
        msg = f"（一部失敗）アップロード: 成功{success} / 失敗{failed} / 合計{total}ファイル"
        if delete_extra and cleanup_warn:
            msg += f"。{cleanup_warn}"
        elif delete_extra and cleanup_skipped and cleanup_warn:
            msg += f"。{cleanup_warn}"
        return False, msg, detail

    except Exception as e:
        try:
            print(f"[PUBLISH] failed: {sanitize_error_text(e)}", flush=True)
        except Exception:
            pass
        # 失敗ログ（安全な範囲）
        try:
            if actor:
                safe_log_action(actor, f"{action}_failed", details=json.dumps({"project_id": project_id, "phase": "connect", "error": sanitize_error_text(e)}, ensure_ascii=False))
        except Exception:
            pass
        return False, f"アップロードに失敗しました: {sanitize_error_text(e)}", detail
    finally:
        try:
            transport.close()
        except Exception:
            pass


def _mask_text_keep_ends(s: str, *, head: int = 2, tail: int = 2) -> str:
    """ログ/画面で、値をそのまま出さないためのマスク。"""
    v = str(s or "").strip()
    if not v:
        return ""
    if len(v) <= head + tail:
        return "*" * len(v)
    return v[:head] + "…" + v[-tail:]


def _mask_remote_dir(path: str) -> str:
    v = str(path or "").strip()
    if not v:
        return ""
    v = v.rstrip("/")
    parts = [p for p in v.split("/") if p]
    if not parts:
        return "/"
    # 最後の1要素だけ見せる（それ以外は省略）
    last = parts[-1]
    return "/…/" + last


def _remote_list_files_recursive(sftp: paramiko.SFTPClient, root_dir: str, *, max_files: int = 8000) -> list[str]:
    """SFTP上の root_dir 配下の「ファイル」を相対パスで列挙する（再帰）。"""
    root = (root_dir or "").rstrip("/")
    if not root or root == "/":
        return []
    out: list[str] = []

    def _walk(cur: str):
        # 安全のため上限
        if len(out) >= max_files:
            return
        try:
            items = sftp.listdir_attr(cur)
        except Exception:
            return
        for it in items:
            if len(out) >= max_files:
                return
            name = it.filename
            if not name:
                continue
            full = cur.rstrip("/") + "/" + name
            try:
                if stat.S_ISDIR(it.st_mode):
                    _walk(full)
                else:
                    rel = full[len(root) + 1 :] if full.startswith(root + "/") else full
                    out.append(rel)
            except Exception:
                continue

    _walk(root)
    # ばらけるので並べる
    try:
        out.sort()
    except Exception:
        pass
    return out


def _remote_is_delete_candidate(rel_path: str) -> bool:
    """『不要ファイル削除』の対象にしてよいか（安全フィルタ）。"""
    p = str(rel_path or "").lstrip("/")
    if not p:
        return False

    # 触らない（サーバー側で使われがち）
    if p == ".htaccess" or p.startswith("."):
        return False
    if p.startswith(".well-known/") or "/.well-known/" in p:
        return False
    if p.startswith("cgi-bin/") or "/cgi-bin/" in p:
        return False

    # このビルダーが作る可能性が高い拡張子だけ削除対象にする（事故防止）
    ext = (Path(p).suffix or "").lower()
    allow = {".html", ".css", ".js", ".png", ".jpg", ".jpeg", ".webp", ".svg", ".ico", ".json", ".txt"}
    return ext in allow


def compute_remote_extra_files_for_cleanup(
    *,
    host: str,
    port: int,
    user: str,
    password: str,
    remote_dir: str,
    keep_files: set[str],
    exclude_patterns: Optional[list[str]] = None,
) -> tuple[bool, str, list[str]]:
    """公開先SFTPの不要ファイル候補を事前に調べる（プレビュー用）。

    返り値: (ok, message, extra_files_rel)

    exclude_patterns:
      - 不要ファイル削除の「除外リスト」（1行1つ / ワイルドカード可）
      - ここに一致するものは削除候補から外す
    """
    host = str(host or "").strip()
    user = str(user or "").strip()
    remote_dir = str(remote_dir or "").strip()
    try:
        port = int(port or 22)
    except Exception:
        port = 22

    if not (host and user and remote_dir):
        return False, "SFTP情報（host/user/dir）が未入力です", []
    if not password:
        return False, "SFTPパスワードが未入力です", []

    rd = remote_dir.rstrip("/")
    if rd in ("", "/"):
        return False, "安全のため、公開ディレクトリが不正です（'/' は不可）", []

    excludes = exclude_patterns or []

    # keep_files を正規化
    keep = set()
    for k in (keep_files or set()):
        kk = str(k or "").lstrip("/")
        if kk:
            keep.add(kk)

    transport = paramiko.Transport((host, port))
    try:
        transport.connect(username=user, password=password)
        sftp = paramiko.SFTPClient.from_transport(transport)

        existing = _remote_list_files_recursive(sftp, rd)

        # 上限（事故防止）
        if len(existing) >= 8000:
            return False, "ファイル数が多すぎるため（8000件超）、安全のため中止しました", []

        extra_all = [p for p in existing if (p not in keep and _remote_is_delete_candidate(p))]
        extra = [p for p in extra_all if not is_excluded_path(p, excludes)]
        excluded = max(0, len(extra_all) - len(extra))

        if excludes:
            return True, f"削除候補: {len(extra)}件（除外{excluded}件 / 全{len(existing)}件中）", extra
        return True, f"削除候補: {len(extra)}件（全{len(existing)}件中）", extra
    except Exception as e:
        return False, f"確認に失敗しました: {sanitize_error_text(e)}", []
    finally:
        try:
            transport.close()
        except Exception:
            pass







def publish_site_via_sftp(
    p: dict,
    actor: User,
    password: str,
    *,
    delete_extra: bool = False,
    only_files: Optional[list[str]] = None,
) -> tuple[bool, str, dict]:
    """案件の静的サイトを、案件に保存されているSFTP情報へアップロードする。

    - only_files を指定した場合:
        そのファイルだけアップロードする（再試行向け）。
        安全のため delete_extra（不要ファイル削除）は自動でOFFにする。
    """
    p = normalize_project(p)
    data = p.get("data") if isinstance(p, dict) else {}
    publish = data.get("publish") if isinstance(data, dict) and isinstance(data.get("publish"), dict) else {}
    host = str(publish.get("sftp_host") or "").strip()
    user = str(publish.get("sftp_user") or "").strip()
    remote_dir = str(publish.get("sftp_dir") or "").strip()
    try:
        port = int(publish.get("sftp_port", 22) or 22)
    except Exception:
        port = 22

    exclude_patterns = parse_cleanup_exclude_list(publish.get("cleanup_exclude"))

    files = build_static_site_files(p)

    # 再試行などで「一部ファイルだけ」アップロードしたい場合
    if only_files:
        try:
            wanted = set(str(x or "").replace("\\", "/").lstrip("/") for x in only_files)
            files = {k: v for k, v in files.items() if str(k or "").lstrip("/") in wanted}
        except Exception:
            files = files
        # 一部アップロード時は不要ファイル削除をしない（事故防止）
        delete_extra = False

    pid = str(p.get("project_id") or "project")
    return publish_site_files_via_sftp(
        host=host,
        port=port,
        user=user,
        password=password,
        remote_dir=remote_dir,
        files=files,
        actor=actor,
        project_id=pid,
        action="project_publish",
        delete_extra=bool(delete_extra),
        exclude_patterns=exclude_patterns,
    )



def render_header(u: Optional[User]) -> None:
    with ui.element("div").classes("w-full bg-white shadow-1").style("position: sticky; top: 0; z-index: 1000;"):
        with ui.row().classes("w-full items-center justify-between q-pa-md").style("gap: 12px;"):
            with ui.row().classes("items-center q-gutter-sm"):
                ui.icon("home").classes("text-grey-8")
                ui.label(f"CV-HomeBuilder v{VERSION}").classes("text-h6")

            with ui.row().classes("items-center q-gutter-sm").style("flex-wrap: wrap; justify-content: flex-end;"):
                ui.badge(APP_ENV.upper()).props("outline")
                if stg_auto_admin_enabled():
                    ui.badge("STG自動admin（ログインなし）").props("outline")
                if HELP_MODE:
                    ui.badge("HELP_MODE（オフライン）").props("outline")
                else:
                    ui.badge(f"SFTP_BASE_DIR: {SFTP_BASE_DIR}").props("outline")

                pname = app.storage.user.get("current_project_name")
                if pname:
                    ui.badge(f"案件: {str(pname)[:18]}").props("outline")

                if u:
                    ui.badge(f"{u.username} ({u.role})").props("outline")
                    ui.button("案件", on_click=lambda: navigate_to("/projects")).props("flat")
                    if HELP_MODE:
                        ui.button("ヘルプ", on_click=lambda: navigate_to("/help")).props("flat")
                    if (not HELP_MODE) and (u.role in {"admin", "subadmin"}):
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

def render_preview_static_site(p: dict, mode: str = "pc", *, root_id: Optional[str] = None) -> None:
    """プレビューを「ZIP書き出しと同じHTML/CSS」で表示する（ズレ防止）"""
    user = current_user()
    if not user:
        ui.label("ログインが必要です").classes("text-negative q-pa-md")
        return

    # ZIP書き出しと同じ生成ロジックを使う（＝同じ見た目）
    try:
        files = build_static_site_files(p)
    except Exception as ex:
        traceback.print_exc()
        ui.label("プレビュー生成に失敗しました").classes("text-negative q-pa-md")
        ui.label(f"{type(ex).__name__}: {ex}").classes("cvhb-muted q-pa-md")
        return

    # userごとに安定したkeyを持つ（ブラウザキャッシュが効きやすい）
    key = None
    try:
        key = app.storage.user.get("pv_site_key")
    except Exception:
        key = None
    if not key or not isinstance(key, str) or len(key) < 8:
        key = secrets.token_urlsafe(12)
        try:
            app.storage.user["pv_site_key"] = key
        except Exception:
            pass

    _pv_site_cache_upsert(int(user.id), str(key), files)

    # iframeの強制リロード用（クエリを変える）
    ver = 0
    try:
        ver = int(app.storage.user.get("pv_site_ver") or 0) + 1
        if ver > 1000000:
            ver = 1
        app.storage.user["pv_site_ver"] = ver
    except Exception:
        ver = int(datetime.now(JST).timestamp())

    # legacyの fit-to-width との整合（modeに応じた固定幅）
    design_w = 720 if mode == "mobile" else 1920
    root_id = root_id or "pv-root"
    src = f"/pv_site/{key}/index.html?v={ver}"

    with ui.element("div").props(f'id="{root_id}"').style(
        f"width: {design_w}px; height: 2400px; overflow: hidden; background: transparent;"
    ):
        ui.html(
            f'<iframe title="preview" src="{html.escape(src, quote=True)}" '
            'style="width:100%; height:100%; border:0; display:block; background:transparent;" '
            'loading="eager"></iframe>'
        )



def render_preview(p: dict, mode: str = "pc", *, root_id: Optional[str] = None) -> None:
    """右側プレビュー（260218配置レイアウト）を描画する。

    p は「プロジェクト全体(dict)」または p["data"] 相当(dict) のどちらでも受け付ける。
    """
    if PREVIEW_USE_EXPORT_TEMPLATE:
        return render_preview_static_site(p, mode=mode, root_id=root_id)

    # -------- data extraction (project dict / data dict 両対応) --------
    if isinstance(p, dict) and isinstance(p.get("data"), dict):
        d = p.get("data") or {}
    elif isinstance(p, dict):
        d = p
    else:
        d = {}

    # NOTE: ここで deep-copy + normalize を回すと（特に data URL を含む案件で）重くなるため、
    #       プレビュー側では参照時に不足キーを補完して表示する。

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

    def _size_class(v: str) -> str:
        """大/中/小 の選択を CSS class に変換（プレビュー側で落ちないように安全に）"""
        v = str(v or "").strip()
        if v in ("大", "L", "large", "big"):
            return "pv-size-l"
        if v in ("小", "S", "small"):
            return "pv-size-s"
        return "pv-size-m"

    # -------- content --------
    company_name = _clean(step2.get("company_name"), "会社名")
    favicon_url = _clean(step2.get("favicon_url")) or DEFAULT_FAVICON_DATA_URL
    catch_copy = _clean(step2.get("catch_copy"))
    catch_size = _clean(step2.get("catch_size"), "中")
    sub_catch_size = _clean(step2.get("sub_catch_size"), "中")
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

    # Ensure exactly 4 slides so dots are always 4 (fallback with presets if needed)
    if len(hero_urls) < 4:
        pad_order = ["A", "B", "C", "D"]
        for k in pad_order:
            if len(hero_urls) >= 4:
                break
            hero_urls.append(_clean(HERO_IMAGE_PRESETS.get(k), HERO_IMAGE_DEFAULT))
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
    contact_mode = _normalize_contact_form_mode(str(contact.get("form_mode") or ""))
    contact_external_url = _clean(contact.get("external_form_url"))

    # -------- render --------
    dark_class = " pv-dark" if is_dark else ""

    with ui.element("div").classes(f"pv-shell pv-layout-260218 pv-mode-{mode}{dark_class}").props(f'id="{root_id}"').style(theme_style):
        # header + scroll container
        # ----- header -----
        with ui.element("header").classes("pv-topbar pv-topbar-260218"):
            with ui.row().classes("pv-topbar-inner items-center justify-between"):
                # brand (favicon + name)
                with ui.row().classes("items-center no-wrap pv-brand").on("click", lambda e: scroll_to("top")):
                    if favicon_url:
                        ui.image(pv_img_src(favicon_url)).classes("pv-favicon")
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

        with ui.element("div").classes("pv-scroll"):
            # ----- HERO (full width / no buttons) -----
            with ui.element("section").classes("pv-hero-wide").props('id="pv-top"'):
                slider_id = f"pv-hero-slider-{mode}"

                # slider + dots are grouped so we can place dots "below the image" on PC
                with ui.element("div").classes("pv-hero-stage"):
                    with ui.element("div").classes("pv-hero-slider pv-hero-slider-wide").props(f'id="{slider_id}"'):
                        with ui.element("div").classes("pv-hero-track"):
                            for url in hero_urls:
                                with ui.element("div").classes("pv-hero-slide"):
                                    ui.image(pv_img_src(url)).classes("pv-hero-img")

                    # dots (4 dots)
                    if len(hero_urls) > 1:
                        with ui.element("div").classes("pv-hero-dots").props(f'id="{slider_id}-dots"'):
                            for i in range(len(hero_urls)):
                                cls = "pv-hero-dot is-active" if i == 0 else "pv-hero-dot"
                                ui.element("button").classes(cls).props(f'type="button" aria-label="画像 {i+1}"')

                # caption (PC: overlay / Mobile: below)
                with ui.element("div").classes("pv-hero-caption"):
                    ui.label(_clean(catch_copy, company_name)).classes(f"pv-hero-caption-title {_size_class(catch_size)}")
                    if sub_catch:
                        ui.label(sub_catch).classes(f"pv-hero-caption-sub {_size_class(sub_catch_size)}")

                # init slider (auto)
                axis = "y" if mode == "mobile" else "x"
                ui.run_javascript(
                    f"setTimeout(function(){{try{{window.cvhbInitHeroSlider && window.cvhbInitHeroSlider('{slider_id}','{axis}',4500);}}catch(e){{}}}},0);"
                )

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
                            ui.image(pv_img_src(about_image_url)).classes("pv-about-img q-mb-sm")

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
                            ui.image(pv_img_src(svc_image_url)).classes("pv-services-img q-mb-sm")

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

                        if email:
                            with ui.row().classes("pv-access-meta items-center q-gutter-md q-mt-sm"):
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
                        ui.label(contact_btn or "お問い合わせ").classes("pv-section-title")
                        ui.label("CONTACT").classes("pv-section-en")

                    with ui.element("div").classes("pv-panel pv-panel-glass pv-contact-card"):
                        if contact_message:
                            ui.label(contact_message).classes("pv-bodytext")
                        if contact_hours:
                            ui.label(contact_hours).classes("pv-muted q-mt-sm")

                        # v0.8: お問い合わせ方式のプレビュー
                        if contact_mode == "external":
                            if contact_external_url:
                                ui.button("フォームを開く").props(
                                    f'no-caps unelevated color=primary type=a href="{contact_external_url}" target="_blank" rel="noopener"'
                                ).classes("pv-btn pv-btn-primary q-mt-sm")
                                ui.label("※ 外部フォームが別タブで開きます（送信前にプライバシーポリシー同意が必要です）").classes(
                                    "pv-muted q-mt-sm"
                                )
                            else:
                                ui.label("外部フォームURLが未入力です（左の入力で設定）").classes("pv-muted q-mt-sm")

                        elif contact_mode == "mail":
                            ui.label("メール対応（メール作成フォーム）").classes("pv-muted q-mt-sm")
                            ui.label("※ 送信ボタンでメールアプリが開きます（PHP不要）").classes("pv-muted")
                            if not email:
                                ui.label("⚠ 基本情報のメールが未入力なので、メール作成できません").classes("pv-muted q-mt-sm")
                            ui.button("メールを作成（プレビューでは無効）").props(
                                "no-caps unelevated color=primary disable"
                            ).classes("pv-btn pv-btn-primary q-mt-sm")

                        else:
                            ui.label("フォーム方式（おすすめ / PHP対応サーバー向け）").classes("pv-muted q-mt-sm")
                            ui.label("※ メールと内容は必須。送信前にプライバシーポリシー同意が必要です。").classes("pv-muted")
                            if not email:
                                ui.label("⚠ 基本情報のメールが未入力なので、フォーム送信できません").classes("pv-muted q-mt-sm")
                            ui.button("送信（プレビューでは無効）").props(
                                "no-caps unelevated color=primary disable"
                            ).classes("pv-btn pv-btn-primary q-mt-sm")

                        # 電話ボタンは出さない（フォーム/メール導線に統一）

            # LEGAL: プライバシーポリシー（プレビュー内モーダル / v0.6.994）
            privacy_contact = ""
            try:
                if address:
                    privacy_contact += f"\n- 住所: {address}"
                if email:
                    privacy_contact += f"\n- メール: {email}"
            except Exception:
                privacy_contact = ""
            if not privacy_contact:
                privacy_contact = "\n- 連絡先: このページのお問い合わせ欄をご確認ください。"

            privacy_md = f"""当社（{company_name}）は、個人情報の重要性を認識し、個人情報保護法その他の関係法令・ガイドラインを遵守するとともに、以下のとおり個人情報を適切に取り扱います。

## 1. 取得する情報
当社は、以下の情報を取得することがあります。

- お問い合わせ等でお客様が入力・送信する情報（氏名、連絡先（電話番号/メールアドレス）、お問い合わせ内容 等）
- サイトの利用に伴い自動的に送信される情報（IPアドレス、ブラウザ情報、閲覧履歴、Cookie 等）

## 2. 利用目的
当社は、取得した個人情報を以下の目的で利用します。

- お問い合わせへの回答・必要な連絡のため
- サービスの提供、運営、案内のため
- 品質向上・改善、利便性向上のため
- 不正行為の防止、セキュリティ確保のため
- 法令に基づく対応のため

## 3. 第三者提供
当社は、法令で認められる場合を除き、あらかじめ本人の同意を得ることなく個人情報を第三者に提供しません。

## 4. 委託
当社は、利用目的の達成に必要な範囲で、個人情報の取扱いを外部事業者に委託することがあります。その場合、適切な委託先を選定し、契約等により必要かつ適切な監督を行います。

## 5. 安全管理措置
当社は、個人情報の漏えい、滅失、毀損等を防止するため、必要かつ適切な安全管理措置を講じます。

## 6. Cookie等の利用
当社サイトでは、利便性向上や利用状況の分析等のために Cookie 等の技術を使用する場合があります。Cookie はブラウザ設定により無効化できますが、その場合は一部機能が利用できないことがあります。

## 7. 外部リンク
当社サイトから外部サイトへリンクする場合があります。リンク先における個人情報の取扱いについて、当社は責任を負いません。

## 8. 開示・訂正・利用停止等
本人から、保有個人データの開示、訂正、追加、削除、利用停止、消去、第三者提供の停止等の請求があった場合、法令に基づき、本人確認のうえ適切に対応します。

## 9. 改定
当社は、法令等の変更や必要に応じて本ポリシーの内容を改定することがあります。改定後の内容は当社サイト上で掲示します。

## 10. お問い合わせ窓口
{company_name}{privacy_contact}
"""

            with ui.dialog() as privacy_dialog:
                with ui.card().classes("q-pa-md").style("max-width: 900px; width: calc(100vw - 24px);"):
                    ui.label("プライバシーポリシー").classes("pv-legal-title q-mb-sm")
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

    # ---------------------------
    # [UI-STATE] 画面の「今どこを編集中か」を覚える
    # - たまに接続が切れてUIが再生成されると、ステップが初期値に戻ることがある
    # - 入力内容は残っているのに「作成ステップ1」に戻る、という現象の対策
    # ---------------------------
    UI_STEP_KEY = "cvhb_ui_step"
    UI_BLOCK_KEY = "cvhb_ui_block"
    UI_PV_MODE_KEY = "cvhb_ui_preview_mode"

    def _ui_get(key: str, default: str, allowed: list[str]) -> str:
        try:
            v = app.storage.user.get(key)
            if isinstance(v, str) and v in allowed:
                return v
        except Exception:
            pass
        try:
            app.storage.user[key] = default
        except Exception:
            pass
        return default

    def _ui_set(key: str, value: str, allowed: list[str]) -> None:
        try:
            if isinstance(value, str) and value in allowed:
                app.storage.user[key] = value
        except Exception:
            pass

    def _event_value(e) -> str:
        """NiceGUIのイベントから value を安全に取り出す（型ゆれ吸収）"""
        try:
            v = getattr(e, "value", None)
            if isinstance(v, str):
                return v
        except Exception:
            pass
        try:
            args = getattr(e, "args", None)
            if isinstance(args, dict):
                # Quasar系の update:model-value は args.value / args.modelValue になることがある
                v = args.get("value") or args.get("modelValue") or args.get("model_value")
                if isinstance(v, str):
                    return v
        except Exception:
            pass
        try:
            if isinstance(e, dict):
                v = e.get("value") or e.get("modelValue") or e.get("model_value")
                if isinstance(v, str):
                    return v
        except Exception:
            pass
        return ""

    preview_ref = {"refresh": (lambda: None)}

    editor_ref = {"refresh": (lambda: None)}

    approval_ref = {"refresh": (lambda: None)}
    publish_ref = {"refresh": (lambda: None)}

    # プレビューは更新が重くなりがちなので、入力中はデバウンスして負荷を下げる
    _preview_refresh_handle: Optional[asyncio.TimerHandle] = None
    _PREVIEW_DEBOUNCE_SEC = 0.25

    def refresh_preview(force: bool = False) -> None:
        """プレビュー更新（デバウンス対応）"""
        nonlocal _preview_refresh_handle

        def _do_refresh() -> None:
            nonlocal _preview_refresh_handle
            _preview_refresh_handle = None
            try:
                preview_ref["refresh"]()
            except Exception:
                pass

        if force:
            if _preview_refresh_handle is not None:
                try:
                    _preview_refresh_handle.cancel()
                except Exception:
                    pass
                _preview_refresh_handle = None
            _do_refresh()
            return

        try:
            loop = asyncio.get_running_loop()
        except Exception:
            _do_refresh()
            return

        if _preview_refresh_handle is not None:
            try:
                _preview_refresh_handle.cancel()
            except Exception:
                pass
        _preview_refresh_handle = loop.call_later(_PREVIEW_DEBOUNCE_SEC, _do_refresh)

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

                            # UIの「今のステップ」を覚える（接続が切れても戻らないように）
                            allowed_steps = ["s1", "s2", "s3", "s4"] + ([] if HELP_MODE else ["s5"])
                            step_initial = _ui_get(UI_STEP_KEY, "s1", allowed_steps)

                            with ui.tabs(value=step_initial).props("vertical dense").classes("w-full cvhb-step-tabs") as step_tabs:
                                ui.tab("s1", label="1. 業種設定・ページカラー設定")
                                ui.tab("s2", label="2. 基本情報設定")
                                ui.tab("s3", label="3. ページ内容詳細設定（ブロックごと）")
                                ui.tab("s4", label="4. 承認・最終チェック")
                                if not HELP_MODE:
                                    ui.tab("s5", label="5. 公開（管理者権限のみ）")

                            def _on_step_tab_change(e):
                                v = _event_value(e)
                                if v:
                                    _ui_set(UI_STEP_KEY, v, allowed_steps)

                            try:
                                step_tabs.on("update:model-value", _on_step_tab_change)
                            except Exception:
                                pass

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
                                    try:
                                        approval_ref["refresh"]()
                                    except Exception:
                                        pass
                                    try:
                                        publish_ref["refresh"]()
                                    except Exception:
                                        pass

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


                                with ui.tab_panels(step_tabs, value=step_initial).classes("w-full"):

                                    # -----------------
                                    # Step 1
                                    # -----------------
                                    with ui.tab_panel("s1"):
                                        ui.label("1. 業種設定・ページカラー設定").classes("text-h6 q-mb-sm")
                                        ui.label("最初にここを決めると、右の完成イメージが一気に整います。").classes("cvhb-muted q-mb-md")

                                        # Industry
                                        with ui.card().classes("q-pa-sm rounded-borders q-mb-sm w-full").props("flat bordered"):
                                            ui.label("業種を選んでください").classes("text-subtitle1")
                                            ui.label("※迷ったら「会社・企業サイト」がおすすめです（文章は後で自由に変えられます）。").classes("cvhb-muted q-mb-sm")
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
                                            ui.label("未設定ならデフォルトを使用します（推奨: 正方形PNG 32×32）").classes("cvhb-muted")

                                            async def _on_upload_favicon(e):
                                                try:
                                                    data_url, fname = await _upload_event_to_data_url(e, max_w=32, max_h=32, force_png=True)
                                                    if not data_url:
                                                        return
                                                    step2["favicon_url"] = data_url
                                                    step2["favicon_filename"] = _short_name(fname)
                                                    update_and_refresh()
                                                    favicon_editor.refresh()
                                                except Exception as ex:
                                                    print(f"[UPLOAD:favicon] unexpected error: {ex}", flush=True)

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
                                                    ui.button("反映して保存", icon="save", on_click=lambda: (refresh_preview(force=True), save_now())).props(
                                                        "color=primary unelevated dense no-caps"
                                                    )
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

                                                # UIの「今のブロック」を覚える（接続が切れても戻らないように）
                                                allowed_blocks = ["hero", "philosophy", "news", "faq", "access", "contact"]
                                                block_initial = _ui_get(UI_BLOCK_KEY, "hero", allowed_blocks)

                                                with ui.tabs(value=block_initial).props("dense").classes("w-full cvhb-block-tabs") as block_tabs:
                                                    ui.tab("hero", label="ヒーロー")
                                                    ui.tab("philosophy", label="理念/概要")
                                                    ui.tab("news", label="お知らせ")
                                                    ui.tab("faq", label="FAQ")
                                                    ui.tab("access", label="アクセス")
                                                    ui.tab("contact", label="お問い合わせ")

                                                def _on_block_tab_change(e):
                                                    v = _event_value(e)
                                                    if v:
                                                        _ui_set(UI_BLOCK_KEY, v, allowed_blocks)

                                                try:
                                                    block_tabs.on("update:model-value", _on_block_tab_change)
                                                except Exception:
                                                    pass

                                                with ui.tab_panels(block_tabs, value=block_initial).classes("w-full q-mt-md"):

                                                    with ui.tab_panel("hero"):
                                                        hero = blocks.setdefault("hero", {})
                                                        ui.label("ヒーロー（ページ最上部）").classes("text-subtitle1 q-mb-sm")

                                                        # キャッチは Step2 に保存しているが、ここ（ヒーロー）でも編集できるようにする
                                                        bind_step2_input(
                                                            "キャッチコピー",
                                                            "catch_copy",
                                                            hint="ヒーローの一番大きい文章です。スマホは画像の下、PCは画像に重ねて表示されます。",
                                                        )

                                                        # 文字サイズ（大/中/小）
                                                        def _on_catch_size(e):
                                                            step2["catch_size"] = e.value or "中"
                                                            update_and_refresh()
                                                        ui.label("キャッチ文字サイズ").classes("cvhb-muted")
                                                        ui.radio(["大", "中", "小"], value=step2.get("catch_size", "中"), on_change=_on_catch_size).props(
                                                            "inline dense"
                                                        ).classes("q-mb-sm")

                                                        bind_block_input("hero", "サブキャッチ（任意）", "sub_catch")

                                                        def _on_sub_catch_size(e):
                                                            step2["sub_catch_size"] = e.value or "中"
                                                            update_and_refresh()
                                                        ui.label("サブキャッチ文字サイズ").classes("cvhb-muted")
                                                        ui.radio(["大", "中", "小"], value=step2.get("sub_catch_size", "中"), on_change=_on_sub_catch_size).props(
                                                            "inline dense"
                                                        ).classes("q-mb-sm")
                                                        ui.label("※ ヒーロー内のボタン表示は現在は使いません（必要なら後で追加できます）。").classes("cvhb-muted q-mt-sm")

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
                                                            except Exception as ex:
                                                                print(f"[UPLOAD:hero_slide {i}] unexpected error: {ex}", flush=True)

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
                                                                            ui.button("反映して保存", icon="save", on_click=lambda: (refresh_preview(force=True), save_now())).props("color=primary unelevated dense no-caps")
                                                                        ui.label(f"ファイル: {nn[_i] or '未アップロード'}").classes("cvhb-muted")
                                                                    else:
                                                                        ui.label(f"選択中: {cc[_i]}").classes("cvhb-muted")

                                                        hero_slides_editor()

                                                        with ui.row().classes("items-center q-gutter-sm q-mt-sm"):
                                                            ui.button("画像を反映して保存", icon="save", on_click=lambda: (refresh_preview(force=True), save_now())).props("color=primary unelevated no-caps")
                                                            ui.label("※アップロード後は、このボタンで保存すると安心です。").classes("cvhb-muted")

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
                                                            except Exception as ex:
                                                                print(f"[UPLOAD:placeholder_image] unexpected error: {ex}", flush=True)

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
                                                                ui.button("反映して保存", icon="save", on_click=lambda: (refresh_preview(force=True), save_now())).props("color=primary unelevated dense no-caps")
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
                                                            except Exception as ex:
                                                                print(f"[UPLOAD:service_image] unexpected error: {ex}", flush=True)

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
                                                                ui.button("反映して保存", icon="save", on_click=lambda: (refresh_preview(force=True), save_now())).props("color=primary unelevated dense no-caps")
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

                                                                                                                # v0.8: フォーム方式（おすすめ）/外部フォームURL/メール対応
                                                                                                                _c = blocks.get("contact", {}) if isinstance(blocks.get("contact"), dict) else {}
                                                                                                                _mode = str(_c.get("form_mode") or CONTACT_FORM_MODE_FORM).strip() or CONTACT_FORM_MODE_FORM

                                                                                                                def _on_form_mode(e):
                                                                                                                    update_block("contact", "form_mode", e.value or CONTACT_FORM_MODE_FORM)
                                                                                                                    # 方式によって表示項目が変わるので、編集パネルも即リフレッシュ
                                                                                                                    try:
                                                                                                                        editor_ref["refresh"]()
                                                                                                                    except Exception:
                                                                                                                        pass

                                                                                                                ui.label("フォーム方式（お問い合わせ）").classes("text-body2")
                                                                                                                ui.radio(
                                                                                                                    [CONTACT_FORM_MODE_FORM, CONTACT_FORM_MODE_EXTERNAL, CONTACT_FORM_MODE_MAIL],
                                                                                                                    value=_mode,
                                                                                                                    on_change=_on_form_mode,
                                                                                                                ).props("inline")

                                                                                                                ui.label("※ PHPが使えないサーバーなら「メール対応」または「外部フォームURL」を選びます。").classes(
                                                                                                                    "text-caption text-grey q-mb-sm"
                                                                                                                )

                                                                                                                if _normalize_contact_form_mode(_mode) == "external":
                                                                                                                    _ext = str(_c.get("external_form_url") or "").strip()

                                                                                                                    def _on_ext(e):
                                                                                                                        update_block("contact", "external_form_url", e.value or "")

                                                                                                                    ui.input("外部フォームURL（必須）", value=_ext, on_change=_on_ext).props("outlined").classes(
                                                                                                                        "w-full q-mb-sm"
                                                                                                                    )
                                                                                                                    ui.label("例：Googleフォーム等のURL").classes("text-caption text-grey q-mb-sm")

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
                                        ui.label("4. 承認・最終チェック").classes("text-h6 q-mb-sm")
                                        ui.label("公開前に「必須チェック」と「承認」を行います。").classes("cvhb-muted q-mb-md")

                                        approval_ui_state = {"request_note": "", "review_note": ""}

                                        @ui.refreshable
                                        def approval_panel():
                                            checks = compute_final_checks(p)
                                            a = get_approval(p)
                                            status = str(a.get("status") or "draft")

                                            # 現在状態
                                            with ui.row().classes("items-center q-gutter-sm q-mb-sm"):
                                                ui.label(f"現在の状態: {approval_status_label(status)}").classes("text-subtitle2")
                                                try:
                                                    if status == "approved":
                                                        ui.badge("OK").props("color=positive")
                                                    elif status == "requested":
                                                        ui.badge("待ち").props("color=warning")
                                                    elif status == "rejected":
                                                        ui.badge("差戻し").props("color=negative")
                                                    else:
                                                        ui.badge("編集").props("outline")
                                                except Exception:
                                                    pass

                                            # 必須チェック
                                            with ui.card().classes("q-pa-sm rounded-borders q-mb-sm w-full").props("flat bordered"):
                                                ui.label("必須チェック").classes("text-subtitle1")
                                                ui.label("ここがOKにならないと、承認依頼ができません。").classes("cvhb-muted q-mb-sm")
                                                req = checks.get("required") or []
                                                ok_req = bool(checks.get("ok_required"))
                                                for it in req:
                                                    mark = "✅" if it.get("ok") else "⬜"
                                                    ui.label(f"{mark} {it.get('label')}").classes("text-body2")
                                                    if not it.get("ok") and it.get("hint"):
                                                        ui.label(f"  → {it.get('hint')}").classes("cvhb-muted q-ml-md")
                                                if ok_req:
                                                    ui.label("必須チェック：OK").classes("text-positive q-mt-sm")
                                                else:
                                                    ui.label("必須チェック：未完了（2/3/などを入力してください）").classes("text-negative q-mt-sm")

                                            # 推奨チェック
                                            with ui.card().classes("q-pa-sm rounded-borders q-mb-sm w-full").props("flat bordered"):
                                                ui.label("推奨チェック（任意）").classes("text-subtitle1")
                                                ui.label("必須ではないですが、あると完成度が上がります。").classes("cvhb-muted q-mb-sm")
                                                rec = checks.get("recommended") or []
                                                for it in rec:
                                                    mark = "✅" if it.get("ok") else "⬜"
                                                    ui.label(f"{mark} {it.get('label')}").classes("text-body2")
                                                    if not it.get("ok") and it.get("hint"):
                                                        ui.label(f"  → {it.get('hint')}").classes("cvhb-muted q-ml-md")

                                            # 承認アクション
                                            with ui.card().classes("q-pa-sm rounded-borders w-full").props("flat bordered"):
                                                ui.label("承認フロー").classes("text-subtitle1")
                                                ui.label("編集者：承認依頼 → 管理者：OK/差戻し").classes("cvhb-muted q-mb-sm")

                                                # 表示情報
                                                if a.get("requested_at"):
                                                    ui.label(f"承認依頼: {fmt_jst(a.get('requested_at'))} / {a.get('requested_by') or ''}").classes("cvhb-muted")
                                                if a.get("reviewed_at"):
                                                    ui.label(f"最終レビュー: {fmt_jst(a.get('reviewed_at'))} / {a.get('reviewed_by') or ''}").classes("cvhb-muted")
                                                if a.get("approved_at"):
                                                    ui.label(f"承認OK: {fmt_jst(a.get('approved_at'))} / {a.get('approved_by') or ''}").classes("cvhb-muted")

                                                # 依頼メモ
                                                if status in {"draft", "rejected"}:
                                                    def _on_req_note(e):
                                                        approval_ui_state["request_note"] = e.value or ""

                                                    ui.textarea("承認依頼メモ（任意）", value=approval_ui_state.get("request_note", ""), on_change=_on_req_note).props("outlined autogrow").classes("w-full q-mt-sm")

                                                    async def _request():
                                                        checks2 = compute_final_checks(p)
                                                        if not checks2.get("ok_required"):
                                                            ui.notify("必須チェックが未完了です（2/3などを入力してください）", type="warning")
                                                            return
                                                        try:
                                                            approval_request(p, u, approval_ui_state.get("request_note", ""))
                                                            await asyncio.to_thread(save_project_to_sftp, p, u)
                                                            set_current_project(p, u)
                                                            safe_log_action(u, "approval_request", details=json.dumps({"project_id": p.get("project_id")}, ensure_ascii=False))
                                                            ui.notify("承認依頼を出しました", type="positive")
                                                            approval_panel.refresh()
                                                            publish_panel.refresh()
                                                        except Exception as e:
                                                            ui.notify(f"承認依頼に失敗しました: {sanitize_error_text(e)}", type="negative")

                                                    ui.button("承認依頼する", on_click=_request).props("color=primary unelevated").classes("q-mt-sm")

                                                elif status == "requested":
                                                    if can_approve(u):
                                                        def _on_review_note(e):
                                                            approval_ui_state["review_note"] = e.value or ""

                                                        ui.textarea("レビュー/メモ（任意）", value=approval_ui_state.get("review_note", ""), on_change=_on_review_note).props("outlined autogrow").classes("w-full q-mt-sm")

                                                        async def _approve():
                                                            checks2 = compute_final_checks(p)
                                                            if not checks2.get("ok_required"):
                                                                ui.notify("必須チェックが未完了のため、承認OKにできません", type="warning")
                                                                return
                                                            try:
                                                                approval_approve(p, u, approval_ui_state.get("review_note", ""))
                                                                await asyncio.to_thread(save_project_to_sftp, p, u)
                                                                set_current_project(p, u)
                                                                safe_log_action(u, "approval_approve", details=json.dumps({"project_id": p.get("project_id")}, ensure_ascii=False))
                                                                ui.notify("承認OKにしました", type="positive")
                                                                approval_panel.refresh()
                                                                publish_panel.refresh()
                                                            except Exception as e:
                                                                ui.notify(f"承認に失敗しました: {sanitize_error_text(e)}", type="negative")

                                                        async def _reject():
                                                            try:
                                                                approval_reject(p, u, approval_ui_state.get("review_note", ""))
                                                                await asyncio.to_thread(save_project_to_sftp, p, u)
                                                                set_current_project(p, u)
                                                                safe_log_action(u, "approval_reject", details=json.dumps({"project_id": p.get("project_id")}, ensure_ascii=False))
                                                                ui.notify("差戻しにしました", type="warning")
                                                                approval_panel.refresh()
                                                                publish_panel.refresh()
                                                            except Exception as e:
                                                                ui.notify(f"差戻しに失敗しました: {sanitize_error_text(e)}", type="negative")

                                                        with ui.row().classes("q-gutter-sm q-mt-sm"):
                                                            ui.button("承認OK", on_click=_approve).props("color=positive unelevated")
                                                            ui.button("差戻し", on_click=_reject).props("color=negative outline")
                                                    else:
                                                        ui.label("承認待ちです（管理者が確認します）").classes("cvhb-muted q-mt-sm")
                                                        if a.get("request_note"):
                                                            ui.label(f"依頼メモ: {a.get('request_note')}").classes("cvhb-muted")

                                                elif status == "approved":
                                                    ui.label("承認OKです。書き出し/公開に進めます。").classes("text-positive q-mt-sm")
                                                    if can_approve(u):
                                                        def _on_review_note2(e):
                                                            approval_ui_state["review_note"] = e.value or ""

                                                        ui.textarea("メモ（任意）", value=approval_ui_state.get("review_note", ""), on_change=_on_review_note2).props("outlined autogrow").classes("w-full q-mt-sm")

                                                        async def _unapprove():
                                                            try:
                                                                approval_reject(p, u, approval_ui_state.get("review_note", ""))
                                                                await asyncio.to_thread(save_project_to_sftp, p, u)
                                                                set_current_project(p, u)
                                                                safe_log_action(u, "approval_unapprove", details=json.dumps({"project_id": p.get("project_id")}, ensure_ascii=False))
                                                                ui.notify("差戻しにしました（承認解除）", type="warning")
                                                                approval_panel.refresh()
                                                                publish_panel.refresh()
                                                            except Exception as e:
                                                                ui.notify(f"更新に失敗しました: {sanitize_error_text(e)}", type="negative")

                                                        ui.button("承認を解除して差戻しにする", on_click=_unapprove).props("color=negative outline").classes("q-mt-sm")
                                                else:
                                                    ui.label("状態を確認できません").classes("text-negative")

                                        approval_panel()
                                        approval_ref["refresh"] = approval_panel.refresh

                                    with ui.tab_panel("s5"):
                                        ui.label("5. 書き出し・公開（管理者権限）").classes("text-h6 q-mb-sm")
                                        ui.label("承認OKになったら、ZIPの書き出しや公開（アップロード）ができます。").classes("cvhb-muted q-mb-md")

                                        export_state = {"url": "", "filename": ""}
                                        publish_ui_state = {"password": ""}

                                        # 危険オプション（不要ファイル削除）用：段階確認ダイアログ
                                        publish_actions = {"run": None}
                                        cleanup_state = {"loading": False, "ok": False, "message": "", "extra": [], "target": ""}

                                        with ui.dialog() as cleanup_dialog, ui.card().classes("q-pa-md rounded-borders").props("bordered"):
                                            ui.label("危険：不要ファイル削除つきで公開").classes("text-subtitle1 q-mb-sm")
                                            ui.label("この操作は、公開ディレクトリ内の『古いファイル』を削除します。").classes("text-negative")
                                            ui.label("※ そのフォルダに手作業で置いたファイルがある場合、それも消える可能性があります。").classes("text-negative text-caption q-mt-xs")

                                            cleanup_target_label = ui.label("").classes("cvhb-muted q-mt-sm")

                                            @ui.refreshable
                                            def cleanup_body():
                                                if cleanup_state.get("loading"):
                                                    ui.label("削除候補を確認中...").classes("cvhb-muted")
                                                    ui.spinner(size="lg")
                                                    return

                                                msg = str(cleanup_state.get("message") or "")
                                                if msg:
                                                    ui.label(msg).classes("cvhb-muted q-mt-sm")

                                                if not cleanup_state.get("ok"):
                                                    if msg:
                                                        ui.label("※ 確認に失敗したため、このモードは実行できません。").classes("text-negative q-mt-sm")
                                                    return

                                                extra = cleanup_state.get("extra") or []
                                                if not extra:
                                                    ui.label("削除候補はありません（削除しなくてもOKです）。").classes("cvhb-muted q-mt-sm")
                                                    return

                                                ui.label("削除候補（先頭30件だけ表示）").classes("text-subtitle2 q-mt-sm")
                                                with ui.element("div").classes("q-mt-xs"):
                                                    for pth in extra[:30]:
                                                        ui.label(str(pth)).classes("cvhb-muted text-caption")
                                                    if len(extra) > 30:
                                                        ui.label(f"...他 {len(extra)-30}件").classes("cvhb-muted text-caption")

                                            cleanup_body()

                                            cleanup_confirm_cb = ui.checkbox("理解しました。削除して公開します（最終確認）").classes("q-mt-md")

                                            async def _cleanup_publish_go():
                                                if not cleanup_confirm_cb.value:
                                                    ui.notify("最終確認のチェックをONにしてください", type="warning")
                                                    return
                                                if not cleanup_state.get("ok"):
                                                    ui.notify("削除候補の確認に失敗したため中止しました", type="negative")
                                                    return
                                                fn = publish_actions.get("run")
                                                if not fn:
                                                    ui.notify("内部エラー：公開処理が未準備です", type="negative")
                                                    return
                                                cleanup_dialog.close()
                                                r = fn(True)
                                                if inspect.isawaitable(r):
                                                    await r

                                            with ui.row().classes("q-gutter-sm q-mt-md"):
                                                ui.button("やめる", on_click=cleanup_dialog.close).props("flat")
                                                ui.button("削除して公開する", on_click=_cleanup_publish_go).props("color=negative unelevated")

                                        # --- バックアップZIP一覧 / 復元（公開先へ） ---
                                        backup_state = {"loading": False, "error": "", "items": [], "project_id": "", "project_name": ""}
                                        restore_state = {"filename": "", "size_kb": 0, "mtime": "", "inspect_loading": False, "inspect_error": "", "inspect": {}}
                                        backup_delete_state = {"filename": ""}

                                        with ui.dialog() as backup_delete_dialog, ui.card().classes("q-pa-md rounded-borders").props("bordered"):
                                            ui.label("バックアップZIPを削除").classes("text-subtitle1 q-mb-sm")
                                            backup_delete_fn_label = ui.label("").classes("cvhb-muted")
                                            backup_delete_confirm_cb = ui.checkbox("削除する（最終確認）").classes("q-mt-md")
                                            ui.label("※ この操作は元に戻せません。").classes("text-negative text-caption q-mt-xs")

                                            async def _do_backup_delete():
                                                if not is_admin(u):
                                                    ui.notify("削除は管理者のみです", type="negative")
                                                    return
                                                if not backup_delete_confirm_cb.value:
                                                    ui.notify("最終確認のチェックをONにしてください", type="warning")
                                                    return
                                                pid = str(backup_state.get("project_id") or "").strip()
                                                fn = str(backup_delete_state.get("filename") or "").strip()
                                                if not pid or not fn:
                                                    ui.notify("内部エラー：対象ZIPが不正です", type="negative")
                                                    return
                                                try:
                                                    await asyncio.to_thread(delete_project_backup_zip, pid, fn, u)
                                                    ui.notify("削除しました", type="positive")
                                                except Exception as e:
                                                    ui.notify(f"削除に失敗しました: {sanitize_error_text(e)}", type="negative")
                                                finally:
                                                    try:
                                                        backup_delete_dialog.close()
                                                    except Exception:
                                                        pass
                                                    # 一覧を更新
                                                    try:
                                                        backup_state["loading"] = True
                                                        backup_state["error"] = ""
                                                        backup_body.refresh()
                                                    except Exception:
                                                        pass
                                                    try:
                                                        items = await asyncio.to_thread(list_project_backup_zips, pid)
                                                        backup_state["items"] = items
                                                    except Exception as e:
                                                        backup_state["error"] = sanitize_error_text(e)
                                                    finally:
                                                        backup_state["loading"] = False
                                                        try:
                                                            backup_body.refresh()
                                                        except Exception:
                                                            pass

                                            with ui.row().classes("q-gutter-sm q-mt-md"):
                                                ui.button("やめる", on_click=backup_delete_dialog.close).props("flat")
                                                ui.button("削除する", on_click=_do_backup_delete).props("color=negative unelevated")

                                        with ui.dialog() as backup_dialog, ui.card().classes("q-pa-md rounded-borders").props("bordered"):
                                            ui.label("案件内バックアップZIP").classes("text-subtitle1 q-mb-sm")
                                            backup_title = ui.label("").classes("cvhb-muted")

                                            @ui.refreshable
                                            def backup_body():
                                                if backup_state["loading"]:
                                                    ui.label("読み込み中...").classes("cvhb-muted")
                                                    ui.spinner(size="lg")
                                                    return
                                                if backup_state["error"]:
                                                    ui.label(backup_state["error"]).classes("text-negative")
                                                    return
                                                items = backup_state.get("items") or []
                                                if not items:
                                                    ui.label("バックアップZIPはまだありません。").classes("cvhb-muted")
                                                    ui.label("※ 先に『ZIPバックアップを案件内に保存』を実行してください。").classes("cvhb-muted text-caption")
                                                    return

                                                ui.label(f"{len(items)}件").classes("text-subtitle2 q-mb-sm")

                                                for it in items:
                                                    fn = str(it.get("filename") or "")
                                                    size_kb = int(it.get("size_kb") or 0)
                                                    mtime = str(it.get("mtime") or "")
                                                    with ui.row().classes("items-center q-gutter-sm q-mb-sm"):
                                                        with ui.column().classes("col"):
                                                            ui.label(fn).classes("text-body2")
                                                            ui.label(f"{size_kb}KB / {mtime or '-'}").classes("cvhb-muted")

                                                        ui.space()

                                                        async def _download_file(_fn=fn):
                                                            try:
                                                                pid = str(backup_state.get("project_id") or "").strip()
                                                                if not pid:
                                                                    ui.notify("内部エラー：project_id が不正です", type="negative")
                                                                    return
                                                                zip_bytes = await asyncio.to_thread(read_project_backup_zip_bytes, pid, _fn)
                                                                key = _export_cache_put(u.id, _fn, zip_bytes)
                                                                navigate_to(f"/export_zip/{key}")
                                                            except Exception as e:
                                                                ui.notify(f"ダウンロードに失敗しました: {sanitize_error_text(e)}", type="negative")

                                                        ui.button("ダウンロード", on_click=_download_file).props("dense outline no-caps")

                                                        def _open_delete(_fn=fn):
                                                            if not is_admin(u):
                                                                ui.notify("削除は管理者のみです", type="negative")
                                                                return
                                                            backup_delete_state["filename"] = _fn
                                                            backup_delete_fn_label.text = _fn
                                                            try:
                                                                backup_delete_confirm_cb.value = False
                                                            except Exception:
                                                                pass
                                                            backup_delete_dialog.open()

                                                        ui.button("削除", on_click=_open_delete).props("dense outline color=negative no-caps")

                                                        def _open_restore(_fn=fn, _size=size_kb, _mtime=mtime):
                                                            if not can_publish(u):
                                                                ui.notify("復元（公開）は管理者のみです", type="negative")
                                                                return
                                                            wf = get_workflow(p)
                                                            st = str(wf.get("status") or "draft")
                                                            if st != "approved":
                                                                ui.notify("復元（公開）は『承認済み』の案件のみ実行できます", type="warning")
                                                                return
                                                            restore_state["filename"] = _fn
                                                            restore_state["size_kb"] = int(_size or 0)
                                                            restore_state["mtime"] = str(_mtime or "")
                                                            restore_state["inspect_loading"] = True
                                                            restore_state["inspect_error"] = ""
                                                            restore_state["inspect"] = {}
                                                            try:
                                                                restore_confirm_cb.value = False
                                                                restore_cleanup_cb.value = False
                                                            except Exception:
                                                                pass
                                                            try:
                                                                restore_body.refresh()
                                                            except Exception:
                                                                pass
                                                            restore_dialog.open()

                                                            # ZIPの中身を軽く確認（復元事故を減らす）
                                                            async def _calc_inspect():
                                                                try:
                                                                    pid2 = str(backup_state.get("project_id") or "").strip()
                                                                    if not pid2:
                                                                        restore_state["inspect_error"] = "project_id がありません"
                                                                        return
                                                                    zip_bytes2 = await asyncio.to_thread(read_project_backup_zip_bytes, pid2, _fn)
                                                                    info2 = await asyncio.to_thread(inspect_site_zip_bytes, zip_bytes2)
                                                                    restore_state["inspect"] = info2
                                                                    if str(info2.get("error") or "").strip():
                                                                        restore_state["inspect_error"] = str(info2.get("error") or "")
                                                                except Exception as e:
                                                                    restore_state["inspect_error"] = sanitize_error_text(e)
                                                                finally:
                                                                    restore_state["inspect_loading"] = False
                                                                    try:
                                                                        restore_body.refresh()
                                                                    except Exception:
                                                                        pass

                                                            try:
                                                                asyncio.create_task(_calc_inspect())
                                                            except Exception:
                                                                restore_state["inspect_loading"] = False
                                                                try:
                                                                    restore_body.refresh()
                                                                except Exception:
                                                                    pass

                                                        ui.button("復元（公開先へ）", on_click=_open_restore).props("dense outline color=primary no-caps")

                                            backup_body()

                                            with ui.row().classes("q-gutter-sm q-mt-md"):
                                                ui.button("閉じる", on_click=backup_dialog.close).props("flat")

                                        with ui.dialog() as restore_dialog, ui.card().classes("q-pa-md rounded-borders").props("bordered"):
                                            ui.label("バックアップZIPから復元（公開先へ）").classes("text-subtitle1 q-mb-sm")
                                            ui.label("このZIPを展開して、公開ディレクトリへ上書きアップロードします。").classes("cvhb-muted q-mb-sm")

                                            @ui.refreshable
                                            def restore_body():
                                                fn = str(restore_state.get("filename") or "")
                                                size_kb = int(restore_state.get("size_kb") or 0)
                                                mtime = str(restore_state.get("mtime") or "")
                                                if not fn:
                                                    ui.label("対象ZIPが未選択です").classes("text-negative")
                                                    return
                                                ui.label(f"対象: {fn}").classes("text-body1")
                                                ui.label(f"{size_kb}KB / {mtime or '-'}").classes("cvhb-muted")

                                                publish = p.get("data", {}).get("publish", {}) if isinstance(p.get("data"), dict) else {}
                                                host = str(publish.get("sftp_host") or "").strip()
                                                user_ = str(publish.get("sftp_user") or "").strip()
                                                rd = str(publish.get("sftp_dir") or "").strip()
                                                if host and user_ and rd:
                                                    ui.label(f"公開先: {_mask_text_keep_ends(host)} / {_mask_text_keep_ends(user_, head=1, tail=0)} / {_mask_remote_dir(rd)}").classes("cvhb-muted")
                                                else:
                                                    ui.label("公開先のSFTP情報が未入力です（このままでは復元できません）").classes("text-negative")

                                                excl = parse_cleanup_exclude_list(publish.get("cleanup_exclude"))
                                                if excl:
                                                    ui.label(f"不要ファイル削除の除外: {len(excl)}件").classes("cvhb-muted text-caption")
                                                # ZIP構成チェック（復元前に軽く見せる）
                                                if bool(restore_state.get("inspect_loading")):
                                                    ui.label("ZIPの中身を確認中...").classes("cvhb-muted q-mt-sm")
                                                else:
                                                    err = str(restore_state.get("inspect_error") or "").strip()
                                                    if err:
                                                        ui.label(f"ZIP確認: {err}").classes("text-negative q-mt-sm")
                                                    else:
                                                        info = restore_state.get("inspect") or {}
                                                        fc = int(info.get("file_count") or 0)
                                                        ui.label(f"ZIP内ファイル数: {fc}").classes("cvhb-muted q-mt-sm")
                                                        req = info.get("required") or {}
                                                        if isinstance(req, dict) and req:
                                                            ui.label("主要ファイルチェック（OK/NG）").classes("cvhb-muted text-caption q-mt-xs")
                                                            for k, ok2 in req.items():
                                                                ui.label(f"{'OK' if ok2 else 'NG'}: {k}").classes("text-caption" if ok2 else "text-negative text-caption")
                                                        miss = info.get("missing") or []
                                                        if miss:
                                                            ui.label(f"不足: {', '.join([str(x) for x in miss[:6]])}").classes("text-negative text-caption")

                                            restore_body()

                                            restore_confirm_cb = ui.checkbox("理解しました。復元する（最終確認）").classes("q-mt-md")
                                            restore_cleanup_cb = ui.checkbox("危険：不要ファイルも削除して、より完全に戻す").classes("q-mt-sm")
                                            ui.label("※ 危険ONのときも『除外リスト』に一致するファイルは削除しません。").classes("cvhb-muted text-caption")

                                            async def _do_restore():
                                                if not can_publish(u):
                                                    ui.notify("権限がありません", type="negative")
                                                    return
                                                wf = get_workflow(p)
                                                st = str(wf.get("status") or "draft")
                                                if st != "approved":
                                                    ui.notify("承認済みの案件のみ復元できます", type="warning")
                                                    return
                                                if not restore_confirm_cb.value:
                                                    ui.notify("最終確認のチェックをONにしてください", type="warning")
                                                    return

                                                if bool(restore_state.get("inspect_loading")):
                                                    ui.notify("ZIPの確認中です。少し待ってから実行してください", type="warning")
                                                    return
                                                if str(restore_state.get("inspect_error") or "").strip():
                                                    ui.notify("ZIPの確認でエラーがあるため中止しました", type="negative")
                                                    return
                                                info = restore_state.get("inspect") or {}
                                                if not bool(info.get("ok")):
                                                    ui.notify("このZIPはサイトZIPとして不完全です（index.html などが不足）", type="negative")
                                                    return

                                                publish = p.get("data", {}).get("publish", {}) if isinstance(p.get("data"), dict) else {}
                                                host = str(publish.get("sftp_host") or "").strip()
                                                user_ = str(publish.get("sftp_user") or "").strip()
                                                rd = str(publish.get("sftp_dir") or "").strip()
                                                try:
                                                    port = int(publish.get("sftp_port", 22) or 22)
                                                except Exception:
                                                    port = 22

                                                pwd = str(publish_ui_state.get("password") or "")
                                                if not (host and user_ and rd):
                                                    ui.notify("公開先SFTP情報が未入力です", type="negative")
                                                    return
                                                if not pwd:
                                                    ui.notify("SFTPパスワードが未入力です", type="warning")
                                                    return

                                                pid = str(p.get("project_id") or "").strip()
                                                fn = str(restore_state.get("filename") or "").strip()
                                                delete_extra = bool(restore_cleanup_cb.value)
                                                excludes = parse_cleanup_exclude_list(publish.get("cleanup_exclude"))

                                                try:
                                                    export_state["busy"] = True
                                                    publish_panel.refresh()
                                                except Exception:
                                                    pass

                                                try:
                                                    def _work():
                                                        zip_bytes = read_project_backup_zip_bytes(pid, fn)
                                                        files = zip_bytes_to_site_files(zip_bytes)
                                                        return publish_site_files_via_sftp(
                                                            host=host,
                                                            port=port,
                                                            user=user_,
                                                            password=pwd,
                                                            remote_dir=rd,
                                                            files=files,
                                                            actor=u,
                                                            project_id=pid,
                                                            action="project_publish_restore_backup_zip",
                                                            delete_extra=delete_extra,
                                                            exclude_patterns=excludes,
                                                        )

                                                    ok, msg, detail = await asyncio.to_thread(_work)

                                                    # workflow更新（成功/失敗とも記録）
                                                    wf2 = get_workflow(p)
                                                    wf2["last_publish_try_at"] = now_jst_iso()
                                                    wf2["last_publish_try_by"] = u.username
                                                    wf2["last_publish_try_ok"] = bool(ok)
                                                    wf2["last_publish_try_message"] = msg
                                                    wf2["last_publish_try_mode"] = "restore_backup_zip"
                                                    wf2["last_publish_try_file"] = fn
                                                    try:
                                                        wf2["last_publish_try_total"] = int(detail.get("total") or 0)
                                                        wf2["last_publish_try_success"] = int(detail.get("success") or 0)
                                                        wf2["last_publish_try_failed"] = int(detail.get("failed") or 0)
                                                        wf2["last_publish_try_failed_files"] = list(detail.get("failed_files") or [])[:80]
                                                        wf2["last_publish_try_cleanup_warn"] = str(detail.get("cleanup_warn") or "")
                                                        wf2["last_publish_try_cleanup_skipped"] = bool(detail.get("cleanup_skipped"))
                                                    except Exception:
                                                        pass

                                                    if ok:
                                                        wf2["last_publish_at"] = now_jst_iso()
                                                        wf2["last_publish_by"] = u.username
                                                        wf2["last_publish_target"] = f"{publish.get('sftp_host', '')}:{publish.get('sftp_dir', '')}"
                                                        wf2["last_publish_mode"] = "restore_backup_zip"
                                                        wf2["last_restore_at"] = now_jst_iso()
                                                        wf2["last_restore_by"] = u.username
                                                        wf2["last_restore_file"] = fn
                                                    else:
                                                        wf2["last_publish_fail_at"] = now_jst_iso()

                                                    try:
                                                        await asyncio.to_thread(save_project_to_sftp, p, u)
                                                        set_current_project(p, u)
                                                    except Exception:
                                                        pass

                                                    ui.notify(msg, type="positive" if ok else "negative")
                                                except Exception as e:
                                                    ui.notify(f"復元に失敗しました: {sanitize_error_text(e)}", type="negative")
                                                finally:
                                                    try:
                                                        export_state["busy"] = False
                                                    except Exception:
                                                        pass
                                                    try:
                                                        restore_dialog.close()
                                                    except Exception:
                                                        pass
                                                    try:
                                                        publish_panel.refresh()
                                                    except Exception:
                                                        pass

                                            with ui.row().classes("q-gutter-sm q-mt-md"):
                                                ui.button("やめる", on_click=restore_dialog.close).props("flat")
                                                ui.button("復元して公開する", on_click=_do_restore).props("color=primary unelevated")

                                        async def open_backup_dialog():
                                            if not can_export(u):
                                                ui.notify("権限がありません", type="negative")
                                                return
                                            pid = str(p.get("project_id") or "").strip()
                                            pname = str(p.get("name") or pid)
                                            backup_state["project_id"] = pid
                                            backup_state["project_name"] = pname
                                            backup_title.text = f"{pname}（{pid}）"
                                            backup_state["loading"] = True
                                            backup_state["error"] = ""
                                            backup_state["items"] = []
                                            backup_dialog.open()
                                            backup_body.refresh()
                                            try:
                                                items = await asyncio.to_thread(list_project_backup_zips, pid)
                                                backup_state["items"] = items
                                            except Exception as e:
                                                backup_state["error"] = f"一覧取得に失敗しました: {sanitize_error_text(e)}"
                                            finally:
                                                backup_state["loading"] = False
                                                backup_body.refresh()

                                        @ui.refreshable
                                        def publish_panel():
                                            a = get_approval(p)
                                            status = str(a.get("status") or "draft")
                                            wf = get_workflow(p)

                                            # -----------------
                                            # 1) ZIP書き出し
                                            # -----------------
                                            with ui.card().classes("q-pa-sm rounded-borders q-mb-sm w-full").props("flat bordered"):
                                                ui.label("書き出し（ZIP）").classes("text-subtitle1")
                                                ui.label("公開前のバックアップとして ZIP を作れます。").classes("cvhb-muted")
                                                if status != "approved":
                                                    ui.label("※ 承認OKになっていないため、まだ書き出しできません。").classes("text-negative q-mt-sm")
                                                else:
                                                    ui.label("承認OK：書き出し可能").classes("text-positive q-mt-sm")

                                                async def _do_export():
                                                    if status != "approved":
                                                        ui.notify("承認OKになってから書き出ししてください", type="warning")
                                                        return
                                                    if not can_export(u):
                                                        ui.notify("書き出しは管理者（admin/subadmin）のみです", type="negative")
                                                        return
                                                    try:
                                                        ui.notify("ZIPを作成中...", type="info")
                                                        zip_bytes, filename = await asyncio.to_thread(build_site_zip_bytes, p)
                                                        key = _export_cache_put(u.id, filename, zip_bytes)
                                                        export_state["url"] = f"/export_zip/{key}"
                                                        export_state["filename"] = filename

                                                        # workflow更新（保存）
                                                        wf2 = get_workflow(p)
                                                        wf2["last_export_at"] = now_jst_iso()
                                                        wf2["last_export_by"] = u.username
                                                        await asyncio.to_thread(save_project_to_sftp, p, u)
                                                        set_current_project(p, u)

                                                        ui.notify("ZIPを作成しました（ダウンロードできます）", type="positive")
                                                        publish_panel.refresh()
                                                    except Exception as e:
                                                        ui.notify(f"書き出しに失敗しました: {sanitize_error_text(e)}", type="negative")

                                                async def _do_export_backup():
                                                    if status != "approved":
                                                        ui.notify("承認OKになってからバックアップ保存してください", type="warning")
                                                        return
                                                    if not can_export(u):
                                                        ui.notify("バックアップ保存は管理者（admin/subadmin）のみです", type="negative")
                                                        return
                                                    try:
                                                        ui.notify("ZIPを作成して、案件バックアップへ保存中...", type="info")
                                                        zip_bytes, filename = await asyncio.to_thread(build_site_zip_bytes, p)
                                                        remote_path = await asyncio.to_thread(save_site_zip_backup_to_project, p, u, zip_bytes, filename)

                                                        # workflow更新（保存）
                                                        wf2 = get_workflow(p)
                                                        wf2["last_backup_zip_at"] = now_jst_iso()
                                                        wf2["last_backup_zip_by"] = u.username
                                                        try:
                                                            wf2["last_backup_zip_file"] = Path(str(remote_path or "")).name
                                                        except Exception:
                                                            wf2["last_backup_zip_file"] = ""
                                                        await asyncio.to_thread(save_project_to_sftp, p, u)
                                                        set_current_project(p, u)

                                                        ui.notify("案件バックアップに保存しました", type="positive")
                                                        publish_panel.refresh()
                                                    except Exception as e:
                                                        ui.notify(f"バックアップ保存に失敗しました: {sanitize_error_text(e)}", type="negative")

                                                if can_export(u) and status == "approved":
                                                    with ui.row().classes("q-gutter-sm q-mt-sm"):
                                                        ui.button("ZIPを書き出す", on_click=_do_export).props("color=primary unelevated")
                                                        ui.button("ZIPを案件バックアップへ保存", on_click=_do_export_backup).props("color=primary outline")
                                                else:
                                                    ui.label("書き出し/バックアップ保存は管理者（admin/subadmin）のみ操作できます。").classes("cvhb-muted q-mt-sm")

                                                if can_export(u):
                                                    ui.button("案件バックアップ一覧 / 復元", on_click=open_backup_dialog).props("dense outline no-caps").classes("q-mt-sm")

                                                if export_state.get("url"):
                                                    ui.label(f"作成済み: {export_state.get('filename') or ''}").classes("cvhb-muted q-mt-sm")
                                                    ui.link("ダウンロードリンクを開く", export_state["url"]).classes("q-mt-xs")
                                                if wf.get("last_export_at"):
                                                    ui.label(f"最終書き出し: {fmt_jst(wf.get('last_export_at'))} / {wf.get('last_export_by') or ''}").classes("cvhb-muted q-mt-sm")
                                                if wf.get("last_backup_zip_at"):
                                                    label = f"最終バックアップ: {fmt_jst(wf.get('last_backup_zip_at'))} / {wf.get('last_backup_zip_by') or ''}"
                                                    if wf.get("last_backup_zip_file"):
                                                        label += f" / {wf.get('last_backup_zip_file')}"
                                                    ui.label(label).classes("cvhb-muted q-mt-xs")

                                            # -----------------
                                            # 2) 公開（SFTP）
                                            # -----------------
                                            with ui.card().classes("q-pa-sm rounded-borders w-full").props("flat bordered"):
                                                ui.label("公開（SFTPアップロード）").classes("text-subtitle1")
                                                ui.label("※ ここは管理者（admin）のみ。公開先サーバーの情報がある案件だけ使えます。").classes("cvhb-muted")

                                                data = p.get("data") if isinstance(p, dict) else {}
                                                publish = data.get("publish") if isinstance(data, dict) and isinstance(data.get("publish"), dict) else {}

                                                def _set_publish(key: str, value):
                                                    publish[key] = value

                                                ui.input("SFTPホスト", value=str(publish.get("sftp_host") or ""), on_change=lambda e: _set_publish("sftp_host", e.value or "")).props("outlined dense").classes("w-full q-mt-sm")
                                                ui.input("ポート（通常22）", value=str(publish.get("sftp_port") or 22), on_change=lambda e: _set_publish("sftp_port", e.value or "22")).props("outlined dense").classes("w-full q-mt-sm")
                                                ui.input("SFTPユーザー名", value=str(publish.get("sftp_user") or ""), on_change=lambda e: _set_publish("sftp_user", e.value or "")).props("outlined dense").classes("w-full q-mt-sm")
                                                ui.input("公開ディレクトリ（例: /public_html）", value=str(publish.get("sftp_dir") or ""), on_change=lambda e: _set_publish("sftp_dir", e.value or "")).props("outlined dense").classes("w-full q-mt-sm")
                                                ui.input("メモ（任意）", value=str(publish.get("sftp_note") or ""), on_change=lambda e: _set_publish("sftp_note", e.value or "")).props("outlined dense").classes("w-full q-mt-sm")

                                                def _on_pw(e):
                                                    publish_ui_state["password"] = e.value or ""

                                                ui.input("SFTPパスワード（保存されません）", value=publish_ui_state.get("password", ""), on_change=_on_pw).props("outlined dense type=password").classes("w-full q-mt-sm")
                                                pub_confirm = ui.checkbox("公開する（上書きアップロード）").classes("q-mt-sm")
                                                cleanup_confirm = ui.checkbox("危険：リモートの不要ファイルも削除する（通常OFF）").classes("q-mt-xs")
                                                ui.label("※ ONにすると、公開ディレクトリ内の古いファイルが消える可能性があります。").classes("text-negative text-caption q-mt-xs")
                                                ui.textarea("不要ファイル削除：除外リスト（任意 / 1行1つ / ワイルドカード可）", value=str(publish.get("cleanup_exclude") or ""), on_change=lambda e: _set_publish("cleanup_exclude", e.value or "")).props("outlined autogrow").classes("w-full q-mt-sm")
                                                ui.label("例: robots.txt / ads.txt / humans.txt / *.xml").classes("cvhb-muted text-caption q-mt-xs")

                                                async def _run_publish(delete_extra: bool):
                                                    # 念のため毎回チェック（画面がズレても事故らない）
                                                    if not can_publish(u):
                                                        ui.notify("公開は admin のみ実行できます", type="negative")
                                                        return
                                                    a2 = get_approval(p)
                                                    status2 = str(a2.get("status") or "draft")
                                                    if status2 != "approved":
                                                        ui.notify("承認OKになってから公開してください", type="warning")
                                                        return
                                                    if not pub_confirm.value:
                                                        ui.notify("『公開する』チェックをONにしてください", type="warning")
                                                        return
                                                    try:
                                                        ui.notify("公開（アップロード）中...", type="info")
                                                                                                                # 直近の公開試行を記録（成功/失敗どちらも）
                                                        excludes = parse_cleanup_exclude_list(publish.get("cleanup_exclude"))
                                                        ok, msg, detail = await asyncio.to_thread(
                                                            publish_site_via_sftp,
                                                            p,
                                                            u,
                                                            publish_ui_state["password"],
                                                            delete_extra=delete_extra,
                                                        )

                                                        wf2 = get_workflow(p)
                                                        wf2["last_publish_try_at"] = now_jst_iso()
                                                        wf2["last_publish_try_by"] = u.username
                                                        wf2["last_publish_try_ok"] = bool(ok)
                                                        wf2["last_publish_try_message"] = msg
                                                        wf2["last_publish_try_delete_extra"] = bool(delete_extra)
                                                        wf2["last_publish_try_exclude"] = len(excludes)

                                                        try:
                                                            wf2["last_publish_try_total"] = int(detail.get("total") or 0)
                                                            wf2["last_publish_try_success"] = int(detail.get("success") or 0)
                                                            wf2["last_publish_try_failed"] = int(detail.get("failed") or 0)
                                                            wf2["last_publish_try_failed_files"] = list(detail.get("failed_files") or [])[:80]
                                                            wf2["last_publish_try_cleanup_warn"] = str(detail.get("cleanup_warn") or "")
                                                            wf2["last_publish_try_cleanup_skipped"] = bool(detail.get("cleanup_skipped"))
                                                        except Exception:
                                                            pass

                                                        if ok:
                                                            wf2["last_publish_at"] = now_jst_iso()
                                                            wf2["last_publish_by"] = u.username
                                                            wf2["last_publish_target"] = f"{publish.get('sftp_host', '')}:{publish.get('sftp_dir', '')}"
                                                        else:
                                                            wf2["last_publish_fail_at"] = now_jst_iso()

                                                        # 成功/失敗どちらでも保存（次の再試行導線に使う）
                                                        try:
                                                            await asyncio.to_thread(save_project_to_sftp, p, u)
                                                            set_current_project(p, u)
                                                        except Exception:
                                                            pass

                                                        ui.notify(msg, type="positive" if ok else "negative")
                                                        publish_panel.refresh()
                                                    except Exception as e:
                                                        ui.notify(f"公開に失敗しました: {sanitize_error_text(e)}", type="negative")

                                                # ダイアログ側から呼べるように保持
                                                publish_actions["run"] = _run_publish

                                                async def _do_publish():
                                                    if not can_publish(u):
                                                        ui.notify("公開は admin のみ実行できます", type="negative")
                                                        return
                                                    if status != "approved":
                                                        ui.notify("承認OKになってから公開してください", type="warning")
                                                        return
                                                    if not pub_confirm.value:
                                                        ui.notify("『公開する』チェックをONにしてください", type="warning")
                                                        return

                                                    if not cleanup_confirm.value:
                                                        await _run_publish(False)
                                                        return

                                                    # ここから危険モード：段階確認ダイアログ
                                                    cleanup_state["loading"] = True
                                                    cleanup_state["ok"] = False
                                                    cleanup_state["message"] = ""
                                                    cleanup_state["extra"] = []
                                                    try:
                                                        cleanup_target_label.text = f"対象: {_mask_text_keep_ends(str(publish.get('sftp_host') or ''))}{_mask_remote_dir(str(publish.get('sftp_dir') or ''))}"
                                                    except Exception:
                                                        pass

                                                    # 最終確認チェックをリセット
                                                    try:
                                                        cleanup_confirm_cb.value = False
                                                    except Exception:
                                                        pass

                                                    cleanup_dialog.open()
                                                    cleanup_body.refresh()

                                                    try:
                                                        def _calc_preview():
                                                            files2 = build_static_site_files(p)
                                                            keep2 = set(str(k or '').lstrip('/') for k in files2.keys() if k)
                                                            return compute_remote_extra_files_for_cleanup(
                                                                host=str(publish.get("sftp_host") or ""),
                                                                port=int(publish.get("sftp_port", 22) or 22),
                                                                user=str(publish.get("sftp_user") or ""),
                                                                password=publish_ui_state.get("password", ""),
                                                                remote_dir=str(publish.get("sftp_dir") or ""),
                                                                keep_files=keep2,
                                                                exclude_patterns=parse_cleanup_exclude_list(publish.get("cleanup_exclude")),
                                                            )
                                                        ok2, msg2, extra2 = await asyncio.to_thread(_calc_preview)
                                                        cleanup_state["loading"] = False
                                                        cleanup_state["ok"] = ok2
                                                        cleanup_state["message"] = msg2
                                                        cleanup_state["extra"] = extra2
                                                        cleanup_body.refresh()
                                                    except Exception as e:
                                                        cleanup_state["loading"] = False
                                                        cleanup_state["ok"] = False
                                                        cleanup_state["message"] = f"確認に失敗しました: {sanitize_error_text(e)}"
                                                        cleanup_state["extra"] = []
                                                        cleanup_body.refresh()

                                                if can_publish(u):
                                                    async def _do_backup_and_publish():
                                                        # _do_publish と同じ安全チェック（事故防止）
                                                        if not can_publish(u):
                                                            ui.notify("公開は admin のみ実行できます", type="negative")
                                                            return
                                                        if status != "approved":
                                                            ui.notify("承認OKになってから公開してください", type="warning")
                                                            return
                                                        if not pub_confirm.value:
                                                            ui.notify("『公開する』チェックをONにしてください", type="warning")
                                                            return

                                                        try:
                                                            ui.notify("① ZIPバックアップを案件内に保存中...", type="info")
                                                            zip_bytes, filename = await asyncio.to_thread(build_site_zip_bytes, p)
                                                            remote_path = await asyncio.to_thread(save_site_zip_backup_to_project, p, u, zip_bytes, filename)

                                                            # workflow更新（保存）
                                                            wf2 = get_workflow(p)
                                                            wf2["last_backup_zip_at"] = now_jst_iso()
                                                            wf2["last_backup_zip_by"] = u.username
                                                            try:
                                                                wf2["last_backup_zip_file"] = Path(str(remote_path or "")).name
                                                            except Exception:
                                                                wf2["last_backup_zip_file"] = ""

                                                            await asyncio.to_thread(save_project_to_sftp, p, u)
                                                            set_current_project(p, u)

                                                            ui.notify("② 公開を開始します...", type="info")
                                                        except Exception as e:
                                                            ui.notify(f"バックアップ保存に失敗しました: {sanitize_error_text(e)}", type="negative")
                                                            return

                                                        # 公開（危険削除モードならダイアログへ）
                                                        await _do_publish()

                                                    with ui.row().classes("q-gutter-sm q-mt-sm"):
                                                        ui.button("バックアップして公開（推奨）", on_click=_do_backup_and_publish).props("color=positive unelevated")
                                                        ui.button("公開だけする", on_click=_do_publish).props("color=positive outline")
                                                    if wf.get("last_publish_at"):
                                                        ui.label(f"最終公開: {fmt_jst(wf.get('last_publish_at'))} / {wf.get('last_publish_by') or ''}").classes("cvhb-muted q-mt-sm")

                                                    if wf.get("last_publish_try_at"):
                                                        try_ok = wf.get("last_publish_try_ok")
                                                        try_msg = str(wf.get("last_publish_try_message") or "")
                                                        if len(try_msg) > 140:
                                                            try_msg = try_msg[:140] + "..."

                                                        # 件数表示（部分成功もわかるように）
                                                        try_total = int(wf.get("last_publish_try_total") or 0)
                                                        try_success = int(wf.get("last_publish_try_success") or 0)
                                                        try_failed = int(wf.get("last_publish_try_failed") or 0)

                                                        if try_ok:
                                                            status_label = "成功"
                                                        else:
                                                            status_label = "一部失敗" if (try_success > 0 and try_failed > 0) else "失敗"

                                                        ui.label(
                                                            f"直近の公開試行: {fmt_jst(wf.get('last_publish_try_at'))} / {status_label}（成功{try_success}/失敗{try_failed}/合計{try_total}） / {try_msg}"
                                                        ).classes("cvhb-muted text-caption q-mt-xs")

                                                        cw = str(wf.get("last_publish_try_cleanup_warn") or "").strip()
                                                        if cw:
                                                            ui.label(f"補足: {cw}").classes("cvhb-muted text-caption q-mt-xs")

                                                        if try_failed > 0:
                                                            failed_list = wf.get("last_publish_try_failed_files") or []
                                                            with ui.expansion("失敗したファイル一覧（先頭30件）").props("dense").classes("q-mt-xs"):
                                                                for fp in failed_list[:30]:
                                                                    ui.label(str(fp)).classes("cvhb-muted text-caption")

                                                        if try_ok is False:
                                                            ui.button("再試行する（全部）", on_click=_do_publish).props("dense outline color=primary no-caps").classes("q-mt-sm")
                                                else:
                                                    ui.label("公開は admin のみ実行できます。").classes("cvhb-muted q-mt-sm")

                                        publish_panel()
                                        publish_ref["refresh"] = publish_panel.refresh

                # -----------------
                # RIGHT (preview)
                # -----------------
                with ui.element("div").classes("cvhb-right-col"):
                    with ui.card().classes("q-pa-md rounded-borders").props("bordered"):
                        with ui.row().classes("items-center justify-between"):
                            ui.label("プレビュー").classes("cvhb-card-title")
                            ui.label("スマホ / PC 切替").classes("cvhb-muted")

                                                # プレビュー表示モード（mobile / pc）
                        preview_mode = {"value": _ui_get(UI_PV_MODE_KEY, "mobile", ["mobile", "pc"])}

                        @ui.refreshable
                        def pv_mode_selector():
                            cur = str(preview_mode.get("value") or "mobile")
                            if cur not in ("mobile", "pc"):
                                cur = "mobile"

                            def set_mode(m: str) -> None:
                                if m not in ("mobile", "pc"):
                                    m = "mobile"
                                preview_mode["value"] = m

                                _ui_set(UI_PV_MODE_KEY, m, ["mobile", "pc"])
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

@ui.page("/help")
def help_page():
    """HELP_MODE専用: ローカルでヘルプ（手順書）を作るためのページ。"""
    inject_global_styles()
    cleanup_user_storage()
    ui.page_title("HELP MODE | CV-HomeBuilder")

    if not HELP_MODE:
        navigate_to("/")
        return

    u = current_user() or User(id=0, username="help_admin", role="admin")
    render_header(u)

    # サンプル案件を確実に用意
    try:
        _help_seed_sample_projects(u)
    except Exception:
        pass

    with ui.element("div").classes("cvhb-container"):
        with ui.card().classes("q-pa-md rounded-borders").props("bordered"):
            ui.label("HELP MODE（ローカル専用）").classes("text-h5")
            ui.label("DB/SFTPに触らず、画面のスクショを撮ってヘルプを作るためのモードです。").classes("cvhb-muted q-mt-sm")

            ui.separator().classes("q-my-md")

            ui.label("まずやること（超ざっくり）").classes("text-subtitle1")
            ui.markdown(
                """- **案件** → サンプル案件を開く（企業/福祉/飲食）  
- 左で編集 → 右でプレビュー確認  
- 必要な画面をスクショして、手順書に貼る  
"""
            )

            ui.separator().classes("q-my-md")

            ui.label("サンプル案件を開く").classes("text-subtitle1")
            items = []
            try:
                items = list_projects_from_sftp()
            except Exception:
                items = []

            async def _open(pid: str) -> None:
                try:
                    p = await asyncio.to_thread(load_project_from_sftp, pid, u)
                    set_current_project(p, u)
                    ui.notify("サンプル案件を開きました", type="positive")
                    navigate_to("/")
                except Exception as e:
                    ui.notify(f"開けませんでした: {sanitize_error_text(e)}", type="negative")

            if not items:
                ui.label("サンプル案件が見つかりません").classes("text-negative")
            else:
                for it in items:
                    pid = str(it.get("project_id") or "")
                    name = str(it.get("project_name") or "")
                    async def _open_one(project_id=pid):
                        await _open(project_id)

                    with ui.row().classes("items-center justify-between w-full q-mb-xs"):
                        ui.label(name).classes("text-body1")
                        ui.button("開く", on_click=_open_one).props("color=primary unelevated")

            ui.separator().classes("q-my-md")

            ui.label("提案メモの型（コピペ用）").classes("text-subtitle1")
            ui.markdown(
                """**迷ったらこの順番で書く**（短くてOK）  
1) 目的：誰に、何が伝わればOK？  
2) 変更：どこを、どう変える？  
3) 理由：なぜ良くなる？（1行でOK）  
4) 確認：お客様に確認が必要なこと（写真・住所・掲載OKなど）  
"""
            )

            with ui.row().classes("q-gutter-sm q-mt-md"):
                ui.button("案件一覧へ", on_click=lambda: navigate_to("/projects")).props("flat")
                ui.button("ビルダーへ", on_click=lambda: navigate_to("/")).props("color=primary flat")


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

            async def create_new_project() -> None:
                name = (dialog_name.value or "").strip()
                if not name:
                    ui.notify("案件名を入力してください", type="warning")
                    return
                try:
                    ui.notify("案件を作成中...", type="info")
                    p = create_project(name, u)
                    await asyncio.to_thread(save_project_to_sftp, p, u)
                    set_current_project(p, u)
                    ui.notify("案件を作成しました", type="positive")
                    new_project_dialog.close()
                    navigate_to("/")
                except Exception as e:
                    ui.notify(f"作成に失敗しました: {sanitize_error_text(e)}", type="negative")

            with ui.row().classes("q-gutter-sm q-mt-md"):
                ui.button("キャンセル", on_click=new_project_dialog.close).props("flat")
                ui.button("作成", on_click=create_new_project).props("color=primary unelevated")

        # --- ヘッダー ---
        with ui.row().classes("items-start justify-between q-mb-md"):
            with ui.column():
                ui.label("案件一覧").classes("text-h5 cvhb-card-title")
                ui.label("案件を開いて編集します。新規作成もここからできます。").classes("cvhb-muted")
            with ui.row().classes("q-gutter-sm"):
                ui.button("新規作成", on_click=lambda: new_project_dialog.open()).props("color=primary unelevated")
                ui.button("ビルダーへ戻る", on_click=lambda: navigate_to("/")).props("flat")

        ui.separator().classes("q-my-md")

        # =========================
        # 管理者用：画像一覧 / 案件削除（/projects 上で実行）
        # =========================
        img_state = {"loading": False, "error": "", "items": [], "project_id": "", "project_name": ""}
        del_state = {"project_id": "", "project_name": ""}
        img_del_state = {"label": "", "filename": "", "mime": "", "size_kb": 0, "data_url": ""}

        # --- 画像一覧ダイアログ（管理者のみ） ---
        with ui.dialog() as images_dialog, ui.card().classes("q-pa-md rounded-borders").props("bordered"):
            ui.label("登録画像の一覧（管理者のみ）").classes("text-subtitle1 q-mb-sm")
            ui.label("案件に保存されている画像（アップロード画像）を確認できます。").classes("cvhb-muted q-mb-sm")
            img_title = ui.label("").classes("cvhb-muted q-mb-sm")

            @ui.refreshable
            def images_dialog_body():
                # NOTE: ここは /projects の管理者ダイアログ（画像一覧）
                if img_state["loading"]:
                    ui.label("読み込み中...").classes("cvhb-muted")
                    ui.spinner(size="lg")
                    return
                if img_state["error"]:
                    ui.label(img_state["error"]).classes("text-negative")
                    return

                items = img_state.get("items") or []
                total_kb = sum(int(it.get("size_kb") or 0) for it in items)
                ui.label(f"{len(items)}件 / 合計 約{total_kb}KB").classes("text-subtitle2 q-mt-sm")

                if not items:
                    ui.label("画像は登録されていません（または、すべてプリセット画像です）。").classes("cvhb-muted q-mt-sm")
                    return

                def _open_image_delete(item: dict) -> None:
                    if not is_admin(u):
                        ui.notify("画像削除は管理者のみです", type="negative")
                        return
                    try:
                        img_del_state["label"] = str(item.get("label") or "")
                        img_del_state["filename"] = str(item.get("filename") or "")
                        img_del_state["mime"] = str(item.get("mime") or "")
                        img_del_state["size_kb"] = int(item.get("size_kb") or 0)
                        img_del_state["data_url"] = str(item.get("data_url") or "")
                    except Exception:
                        img_del_state["label"] = ""
                        img_del_state["filename"] = ""
                        img_del_state["mime"] = ""
                        img_del_state["size_kb"] = 0
                        img_del_state["data_url"] = ""
                    # 最終確認チェックは毎回リセット
                    try:
                        img_del_confirm_cb.value = False
                    except Exception:
                        pass
                    try:
                        img_delete_body.refresh()
                    except Exception:
                        pass
                    try:
                        img_delete_dialog.open()
                    except Exception:
                        pass

                with ui.element("div").classes("q-mt-sm"):
                    for it in items:
                        label = str(it.get("label") or "")
                        fn = str(it.get("filename") or "")
                        mime = str(it.get("mime") or "")
                        size_kb = int(it.get("size_kb") or 0)
                        data_url = str(it.get("data_url") or "")

                        with ui.row().classes("items-center q-gutter-sm q-mb-sm"):
                            try:
                                ui.image(data_url).style(
                                    "width: 92px; height: 52px; object-fit: cover; border-radius: 10px; border: 1px solid rgba(0,0,0,0.15);"
                                )
                            except Exception:
                                ui.element("div").style(
                                    "width: 92px; height: 52px; border-radius: 10px; border: 1px solid rgba(0,0,0,0.15); background: #f3f4f6;"
                                )

                            with ui.column():
                                ui.label(label).classes("text-body2")
                                ui.label(f"{fn or '(filenameなし)'} / {mime} / {size_kb}KB").classes("cvhb-muted")

                            ui.space()
                            ui.button("削除", on_click=lambda it=it: _open_image_delete(it)).props(
                                "dense outline color=negative no-caps"
                            )

            images_dialog_body()

            with ui.row().classes("q-gutter-sm q-mt-md"):
                ui.button("閉じる", on_click=images_dialog.close).props("flat")

        # --- 画像削除ダイアログ（チェック必須） ---
        with ui.dialog() as img_delete_dialog, ui.card().classes("q-pa-md rounded-borders").props("bordered"):
            ui.label("画像を削除（管理者のみ）").classes("text-subtitle1 q-mb-sm")
            ui.label("この画像を『案件から削除』します（関連する場所から画像URLを消します）。").classes("cvhb-muted q-mb-sm")

            @ui.refreshable
            def img_delete_body():
                label = str(img_del_state.get("label") or "")
                fn = str(img_del_state.get("filename") or "")
                mime = str(img_del_state.get("mime") or "")
                size_kb = int(img_del_state.get("size_kb") or 0)
                data_url = str(img_del_state.get("data_url") or "")

                if not data_url:
                    ui.label("対象画像が未選択です。").classes("text-negative")
                    return

                with ui.row().classes("items-center q-gutter-sm"):
                    try:
                        ui.image(data_url).style(
                            "width: 220px; height: 124px; object-fit: cover; border-radius: 12px; border: 1px solid rgba(0,0,0,0.15);"
                        )
                    except Exception:
                        ui.element("div").style(
                            "width: 220px; height: 124px; border-radius: 12px; border: 1px solid rgba(0,0,0,0.15); background: #f3f4f6;"
                        )
                    with ui.column():
                        ui.label(label).classes("text-body1")
                        ui.label(f"{fn or '(filenameなし)'} / {mime} / {size_kb}KB").classes("cvhb-muted")

                ui.label("※ この操作は元に戻せません（必要なら画像を再アップロードしてください）。").classes("text-negative q-mt-sm")

            img_delete_body()

            img_del_confirm_cb = ui.checkbox("理解しました。削除する（最終確認）").classes("q-mt-md")

            async def _confirm_img_delete():
                if not is_admin(u):
                    ui.notify("権限がありません", type="negative")
                    return
                if not img_del_confirm_cb.value:
                    ui.notify("最終確認のチェックをONにしてください", type="warning")
                    return
                pid = str(img_state.get("project_id") or "").strip()
                pname = str(img_state.get("project_name") or "").strip()
                target = str(img_del_state.get("data_url") or "")
                if not pid or not target:
                    ui.notify("内部エラー：対象が不正です", type="negative")
                    return

                try:
                    # load -> remove -> save
                    def _work():
                        proj = load_project_from_sftp(pid, u)
                        cleared = remove_data_url_from_project(proj, target)
                        save_project_to_sftp(proj, u)
                        return cleared

                    cleared = await asyncio.to_thread(_work)
                    ui.notify(f"画像を削除しました（反映箇所: {cleared}）", type="positive")
                except Exception as e:
                    ui.notify(f"削除に失敗しました: {sanitize_error_text(e)}", type="negative")
                    return
                finally:
                    try:
                        img_delete_dialog.close()
                    except Exception:
                        pass

                # 画像一覧を再読み込み
                try:
                    if pid:
                        await open_images_dialog(pid, pname or pid)
                except Exception:
                    pass

            with ui.row().classes("q-gutter-sm q-mt-md"):
                ui.button("やめる", on_click=img_delete_dialog.close).props("flat")
                ui.button("削除（確定）", on_click=_confirm_img_delete).props("color=negative unelevated")

# --- 削除準備ダイアログ（チェック必須） ---
        with ui.dialog() as delete_prepare_dialog, ui.card().classes("q-pa-md rounded-borders").props("bordered"):
            ui.label("案件を削除（管理者のみ）").classes("text-subtitle1 q-mb-sm")
            ui.label("この操作は取り消せません。").classes("text-negative q-mb-sm")
            del_title = ui.label("").classes("cvhb-muted q-mb-sm")
            del_checkbox = ui.checkbox("削除する（最終確認へ進むためのチェック）")

            def _go_delete_final():
                if not del_checkbox.value:
                    ui.notify("チェックをONにしてください", type="warning")
                    return
                delete_final_dialog.open()

            with ui.row().classes("q-gutter-sm q-mt-md"):
                ui.button("キャンセル", on_click=delete_prepare_dialog.close).props("flat")
                ui.button("次へ（最終確認）", on_click=_go_delete_final).props("color=negative unelevated")

        # --- 最終確認ダイアログ（本当に削除するか） ---
        with ui.dialog() as delete_final_dialog, ui.card().classes("q-pa-md rounded-borders").props("bordered"):
            ui.label("最終確認").classes("text-subtitle1 q-mb-sm")
            ui.label("本当に削除しますか？（元に戻せません）").classes("text-negative q-mb-sm")
            del_final_title = ui.label("").classes("cvhb-muted q-mb-sm")

            async def _confirm_delete():
                if not is_admin(u):
                    ui.notify("権限がありません", type="negative")
                    delete_final_dialog.close()
                    delete_prepare_dialog.close()
                    return
                pid = str(del_state.get("project_id") or "")
                if not pid:
                    ui.notify("削除対象が未選択です", type="warning")
                    return
                try:
                    ui.notify("削除中...", type="info")
                    await asyncio.to_thread(delete_project_from_sftp, pid, u)
                    ui.notify("削除しました", type="positive")
                    delete_final_dialog.close()
                    delete_prepare_dialog.close()
                    list_refresh.refresh()
                except Exception as e:
                    ui.notify(f"削除に失敗しました: {sanitize_error_text(e)}", type="negative")

            with ui.row().classes("q-gutter-sm q-mt-md"):
                ui.button("やめる", on_click=delete_final_dialog.close).props("flat")
                ui.button("削除（確定）", on_click=_confirm_delete).props("color=negative unelevated")

        async def open_images_dialog(project_id: str, project_name: str) -> None:
            if not is_admin(u):
                ui.notify("権限がありません", type="negative")
                return
            img_state["project_id"] = project_id
            img_state["project_name"] = project_name
            img_state["loading"] = True
            img_state["error"] = ""
            img_state["items"] = []
            img_title.text = f"案件: {project_name}（ID: {project_id}）"
            images_dialog.open()
            images_dialog_body.refresh()

            try:
                p = await asyncio.to_thread(load_project_from_sftp, project_id, u)
                items = collect_project_images(p)
                try:
                    items.sort(key=lambda x: int(x.get("size_kb") or 0), reverse=True)
                except Exception:
                    pass
                img_state["items"] = items
            except Exception as e:
                img_state["error"] = f"読み込みに失敗しました: {sanitize_error_text(e)}"
            finally:
                img_state["loading"] = False
                images_dialog_body.refresh()

        def open_delete_prepare(project_id: str, project_name: str) -> None:
            if not is_admin(u):
                ui.notify("権限がありません", type="negative")
                return
            del_state["project_id"] = project_id
            del_state["project_name"] = project_name
            del_checkbox.value = False
            del_title.text = f"案件: {project_name}（ID: {project_id}）"
            del_final_title.text = f"案件: {project_name}（ID: {project_id}）"
            delete_prepare_dialog.open()

        # --- 案件を開く ---
        async def open_project(project_id: str) -> None:
            try:
                ui.notify("案件を読み込み中...", type="info")
                p = await asyncio.to_thread(load_project_from_sftp, project_id, u)
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

            for it in items:
                pid = it.get("project_id", "")
                pname = it.get("project_name", "")
                updated_at = fmt_jst(it.get("updated_at"))
                created_at = fmt_jst(it.get("created_at"))
                updated_by = it.get("updated_by", "")

                with ui.card().classes("q-pa-md rounded-borders q-mb-sm").props("bordered"):
                    ui.label(pname).classes("text-subtitle1")
                    ui.label(f"最終更新: {updated_at}").classes("cvhb-project-meta q-mt-xs")
                    ui.label(f"案件開始: {created_at}").classes("cvhb-project-meta")
                    if updated_by:
                        ui.label(f"更新担当者: {updated_by}").classes("cvhb-project-meta")
                    ui.label(f"ID: {pid}").classes("cvhb-project-meta q-mt-xs")

                    async def _open(project_id=pid):
                        await open_project(project_id)

                    with ui.row().classes("q-gutter-sm q-mt-md"):
                        ui.button("開く", on_click=_open).props("color=primary unelevated")
                        if is_admin(u):
                            ui.button("登録画像一覧", on_click=lambda pid=pid, pname=pname: open_images_dialog(pid, pname)).props("outline")
                            ui.button("削除", on_click=lambda pid=pid, pname=pname: open_delete_prepare(pid, pname)).props("color=negative outline")

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

    if HELP_MODE:
        render_header(u)
        with ui.element("div").classes("cvhb-container"):
            with ui.card().classes("q-pa-md rounded-borders").props("bordered"):
                ui.label("HELP_MODEでは操作ログは表示しません").classes("text-subtitle1")
                ui.label("オフラインでヘルプ作成をするため、DB（操作ログ）を使わないモードです。").classes("cvhb-muted q-mt-sm")
                ui.button("トップへ戻る", on_click=lambda: navigate_to("/")).props("flat").classes("q-mt-md")
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

                # HELP_MODE: ログイン不要 / DB不要 / SFTP不要（オフラインでヘルプ作成）
                if HELP_MODE:
                    try:
                        if not app.storage.user.get("current_project_id"):
                            p_demo = _help_ensure_sample_project(u)
                            set_current_project(p_demo, u)
                    except Exception:
                        pass
                    render_main(u if u else User(id=0, username="help_admin", role="admin"))
                    return

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

if __name__ in {"__main__", "__mp_main__"}:
    # 通常モードのみDBを初期化（HELP_MODEはオフライン専用のためDBに触れない）
    if not HELP_MODE:
        init_db_schema()

    ui.run(
        title=f"CV-HomeBuilder v{VERSION}",
        storage_secret=STORAGE_SECRET,
        reload=False,
        port=int(os.getenv("PORT", "8080")),
        show=False,
    )
