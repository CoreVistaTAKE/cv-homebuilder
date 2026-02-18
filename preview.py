# preview.py (CV-HomeBuilder) - プレビュー描画専用
# - ここは「完成サイトの見え方」だけに集中
# - builder側（入力UI）とは、project(dict)で連携する

from __future__ import annotations

import json
import re
import traceback
from typing import Any, Dict, List, Optional

from nicegui import ui

from state import *  # noqa: F403,F401  (テンプレ/共通関数をそのまま使う)


# =========================
# [BLK-PV-01] Defensive preflight (同種エラー再発防止)
# =========================
def _preview_preflight_error() -> Optional[str]:
    """プレビューのよくある崩れ方を“画面内で”検出して、例外より先に理由を返す。"""

    required = [
        "_safe_list",
        "sanitize_error_text",
        "COLOR_PRESETS",
        "HERO_IMAGE_PRESETS",
        "HERO_IMAGE_DEFAULT",
    ]
    for name in required:
        if name not in globals():
            return f"{name} is not defined"

    if not isinstance(globals().get("COLOR_PRESETS"), dict):
        return "COLOR_PRESETS must be dict"
    if not isinstance(globals().get("HERO_IMAGE_PRESETS"), dict):
        return "HERO_IMAGE_PRESETS must be dict"

    # _preview_glass_style の引数ズレ（dark等）を吸収できるか確認
    try:
        import inspect

        sig = inspect.signature(_preview_glass_style)
        params = sig.parameters
        if "dark" not in params:
            return "_preview_glass_style must accept dark parameter"
    except Exception:
        # signatureチェック自体が失敗しても致命ではない
        pass

    return None


# =========================
# [BLK-PV-02] Theme helpers
# =========================
def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    c = (hex_color or "").strip().lstrip("#")
    if len(c) == 3:
        c = "".join([ch * 2 for ch in c])
    if len(c) != 6:
        return (25, 118, 210)  # fallback blue-ish
    try:
        return (int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16))
    except Exception:
        return (25, 118, 210)


def _rgb_to_hex(rgb: tuple[int, int, int]) -> str:
    r, g, b = rgb
    r = max(0, min(255, int(r)))
    g = max(0, min(255, int(g)))
    b = max(0, min(255, int(b)))
    return f"#{r:02x}{g:02x}{b:02x}"


def _blend_hex(a: str, b: str, t: float) -> str:
    """aとbをtで線形補間（t=0→a, t=1→b）"""
    t = 0.0 if t is None else float(t)
    t = max(0.0, min(1.0, t))
    ar, ag, ab = _hex_to_rgb(a)
    br, bg, bb = _hex_to_rgb(b)
    rr = ar + (br - ar) * t
    rg = ag + (bg - ag) * t
    rb = ab + (bb - ab) * t
    return _rgb_to_hex((rr, rg, rb))


def _accent_from_step1(step1: Dict[str, Any]) -> str:
    color_key = (
        step1.get("primary_color")
        or step1.get("primaryColor")
        or step1.get("color")
        or "blue"
    )
    preset = COLOR_PRESETS.get(str(color_key), COLOR_PRESETS.get("blue", {}))
    return str(preset.get("primary") or "#1976d2")


def _preview_glass_style(step1_or_primary: Any, dark: bool | None = None, **_ignore) -> str:
    """旧実装との互換のため残す。dark等の追加引数が来ても落ちないようにする。"""

    step1 = step1_or_primary if isinstance(step1_or_primary, dict) else {}
    accent = _accent_from_step1(step1) if step1 else str(step1_or_primary or "#1976d2")
    accent_soft = _blend_hex(accent, "#ffffff", 0.86)
    accent_soft2 = _blend_hex(accent, "#ffffff", 0.92)

    # darkは“見え方”に影響するけど、今回はテーマ優先で軽く反映
    base_bg = "#0b1220" if dark else "#f6f9ff"
    text = "#eaf0ff" if dark else "#0f172a"

    return (
        f"--cvhb-accent:{accent};"
        f"--cvhb-accent-soft:{accent_soft};"
        f"--cvhb-accent-soft2:{accent_soft2};"
        f"--cvhb-bg:{base_bg};"
        f"--cvhb-text:{text};"
    )


# =========================
# [BLK-PV-03] Preview CSS (260218配置の意図に合わせて再調整)
# =========================
_PREVIEW_STYLE_DONE = False


def inject_preview_styles() -> None:
    global _PREVIEW_STYLE_DONE
    if _PREVIEW_STYLE_DONE:
        return
    _PREVIEW_STYLE_DONE = True

    ui.add_head_html(
        """
<style>
/* ===== Preview Site Base ===== */
.cvhb-site{
  font-family: system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial, "Hiragino Kaku Gothic ProN", "Noto Sans JP", sans-serif;
  color: var(--cvhb-text, #0f172a);
  background:
    radial-gradient(1200px 600px at 20% 10%, var(--cvhb-accent-soft, #e3f2fd) 0%, transparent 60%),
    radial-gradient(900px 520px at 90% 30%, var(--cvhb-accent-soft2, #eef6ff) 0%, transparent 55%),
    linear-gradient(180deg, #ffffff 0%, #f6f9ff 55%, #ffffff 100%);
  height: 100%;
  overflow: auto;
  scroll-behavior: smooth;
}
.cvhb-site *{ box-sizing: border-box; }

.cvhb-site--mobile{ font-size: 14px; }
.cvhb-site--pc{ font-size: 15px; }

.cvhb-container{
  width: min(100%, 1040px);
  margin: 0 auto;
  padding: 0 18px;
}
.cvhb-site--mobile .cvhb-container{ padding: 0 14px; }

/* ===== Header ===== */
.cvhb-header{
  position: sticky;
  top: 0;
  z-index: 10;
  backdrop-filter: blur(10px);
  background: rgba(255,255,255,.72);
  border-bottom: 1px solid rgba(15,23,42,.10);
}
.cvhb-header-inner{
  display: flex;
  align-items: center;
  justify-content: space-between;
  height: 58px;
}
.cvhb-brand{
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}
.cvhb-favicon{
  width: 26px;
  height: 26px;
  border-radius: 7px;
  object-fit: cover;
  background: rgba(255,255,255,.9);
  border: 1px solid rgba(15,23,42,.10);
}
.cvhb-company{
  font-weight: 800;
  letter-spacing: .02em;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 280px;
}
.cvhb-site--mobile .cvhb-company{ max-width: 190px; }

.cvhb-nav{
  display: flex;
  align-items: center;
  gap: 6px;
}
.cvhb-nav-link{
  padding: 8px 10px;
  border-radius: 10px;
  font-weight: 700;
  color: rgba(15,23,42,.72);
}
.cvhb-nav-link:hover{ background: rgba(15,23,42,.06); }

.cvhb-menu-btn{
  width: 38px;
  height: 38px;
  border-radius: 12px;
  background: rgba(15,23,42,.06);
}
.cvhb-menu-btn:hover{ background: rgba(15,23,42,.10); }

.cvhb-only-pc{ display: none; }
.cvhb-site--pc .cvhb-only-pc{ display: flex; }
.cvhb-only-mobile{ display: none; }
.cvhb-site--mobile .cvhb-only-mobile{ display: flex; }

/* ===== Section shared ===== */
.cvhb-section{ padding: 26px 0; }
.cvhb-section-tight{ padding: 18px 0; }
.cvhb-section-title{
  display:flex; align-items:center; gap:10px;
  font-weight: 900;
  letter-spacing: .02em;
  margin: 0 0 14px 0;
}
.cvhb-kicker{
  display:flex; align-items:center; gap:8px;
  font-size: 12px;
  font-weight: 900;
  letter-spacing: .12em;
  color: rgba(15,23,42,.55);
  margin-bottom: 6px;
  text-transform: uppercase;
}
.cvhb-badge{
  display:inline-flex; align-items:center; gap:6px;
  padding: 4px 10px;
  border-radius: 999px;
  background: color-mix(in srgb, var(--cvhb-accent, #1976d2) 10%, white);
  border: 1px solid color-mix(in srgb, var(--cvhb-accent, #1976d2) 18%, white);
  font-weight: 800;
  font-size: 12px;
}

/* ===== Panels (glass) ===== */
.cvhb-panel{
  background: rgba(255,255,255,.72);
  border: 1px solid rgba(15,23,42,.10);
  border-radius: 18px;
  box-shadow: 0 18px 45px rgba(15,23,42,.08);
}
.cvhb-panel-pad{ padding: 16px; }
.cvhb-site--pc .cvhb-panel-pad{ padding: 18px; }

/* ===== Hero ===== */
.cvhb-hero{
  padding: 18px 0 8px 0;
}
.cvhb-hero-grid{
  display: grid;
  grid-template-columns: 1.05fr .95fr;
  gap: 18px;
  align-items: center;
}
.cvhb-site--mobile .cvhb-hero-grid{
  grid-template-columns: 1fr;
  gap: 14px;
}
.cvhb-hero-copy h1{
  margin: 0;
  font-size: 28px;
  line-height: 1.2;
  font-weight: 1000;
  letter-spacing: .02em;
}
.cvhb-site--mobile .cvhb-hero-copy h1{
  font-size: 24px;
}
.cvhb-hero-sub{
  margin-top: 10px;
  color: rgba(15,23,42,.68);
  line-height: 1.7;
  font-weight: 600;
}
.cvhb-hero-cta{
  display:flex;
  gap: 10px;
  flex-wrap: wrap;
  margin-top: 14px;
}
.cvhb-btn-primary{
  background: var(--cvhb-accent, #1976d2);
  color: white;
  border-radius: 999px;
  padding: 10px 14px;
  font-weight: 900;
}
.cvhb-btn-ghost{
  background: rgba(255,255,255,.70);
  border: 1px solid rgba(15,23,42,.12);
  border-radius: 999px;
  padding: 10px 14px;
  font-weight: 900;
  color: rgba(15,23,42,.75);
}
.cvhb-hero-media{
  border-radius: 30px;
  overflow: hidden;
  border: 1px solid rgba(15,23,42,.10);
  box-shadow: 0 22px 55px rgba(15,23,42,.10);
  background: rgba(255,255,255,.55);
}
.cvhb-hero-carousel{
  position: relative;
  width: 100%;
  height: 360px;
}
.cvhb-site--mobile .cvhb-hero-carousel{ height: 260px; }
.cvhb-hero-track{
  display:flex;
  height: 100%;
  width: 100%;
  transition: transform .55s ease;
}
.cvhb-hero-slide{
  min-width: 100%;
  height: 100%;
  background-size: cover;
  background-position: center;
}

/* ===== News ===== */
.cvhb-news-list{ display:flex; flex-direction: column; gap: 10px; }
.cvhb-news-item{
  display:flex; flex-direction: column; gap: 6px;
  padding: 12px;
  border-radius: 14px;
  background: rgba(255,255,255,.65);
  border: 1px solid rgba(15,23,42,.10);
}
.cvhb-news-meta{
  display:flex; align-items:center; gap: 10px;
  font-size: 12px;
  color: rgba(15,23,42,.60);
  font-weight: 800;
}
.cvhb-news-title{
  font-weight: 900;
  color: rgba(15,23,42,.82);
  line-height: 1.45;
}

/* ===== About ===== */
.cvhb-about-grid{
  display:grid;
  grid-template-columns: .95fr 1.05fr;
  gap: 16px;
  align-items: center;
}
.cvhb-site--mobile .cvhb-about-grid{ grid-template-columns: 1fr; }
.cvhb-about-img{
  width: 100%;
  height: 320px;
  border-radius: 18px;
  object-fit: cover;
  border: 1px solid rgba(15,23,42,.10);
}
.cvhb-site--mobile .cvhb-about-img{ height: 220px; }
.cvhb-points{ display:flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
.cvhb-point{
  padding: 7px 10px;
  border-radius: 999px;
  background: rgba(255,255,255,.70);
  border: 1px solid rgba(15,23,42,.10);
  font-weight: 800;
  color: rgba(15,23,42,.72);
  display:inline-flex; align-items:center; gap: 6px;
}

/* ===== Access / Contact ===== */
.cvhb-grid-2{
  display:grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
}
.cvhb-site--mobile .cvhb-grid-2{ grid-template-columns: 1fr; }
.cvhb-muted{ color: rgba(15,23,42,.62); font-weight: 650; line-height: 1.7; }
.cvhb-cta-box{
  margin-top: 14px;
  display:flex;
  align-items:center;
  justify-content: space-between;
  gap: 12px;
  padding: 12px 14px;
  border-radius: 16px;
  background: color-mix(in srgb, var(--cvhb-accent, #1976d2) 9%, white);
  border: 1px solid color-mix(in srgb, var(--cvhb-accent, #1976d2) 14%, white);
}
.cvhb-cta-left{ display:flex; flex-direction: column; gap: 2px; }
.cvhb-cta-main{ font-weight: 1000; }
.cvhb-cta-sub{ font-size: 12px; color: rgba(15,23,42,.65); font-weight: 800; }

/* ===== Prefooter / Footer ===== */
.cvhb-prefooter{
  margin-top: 18px;
  padding: 18px 0 6px 0;
}
.cvhb-prefooter-inner{
  display:flex;
  align-items:center;
  justify-content: space-between;
  gap: 10px;
  padding: 12px 14px;
  border-radius: 16px;
  background: rgba(255,255,255,.70);
  border: 1px solid rgba(15,23,42,.10);
}
.cvhb-prefooter-inner .cvhb-muted{ font-size: 12px; }

.cvhb-footer{
  margin-top: 16px;
  padding: 18px 0 28px 0;
  background: rgba(15,23,42,.92);
  color: rgba(255,255,255,.86);
}
.cvhb-footer a{ color: rgba(255,255,255,.92); text-decoration: none; }
.cvhb-footer-links{ display:flex; gap: 10px; flex-wrap: wrap; }
.cvhb-footer-links span{
  padding: 6px 10px;
  border-radius: 999px;
  background: rgba(255,255,255,.08);
}
.cvhb-copyright{ margin-top: 12px; font-size: 12px; color: rgba(255,255,255,.70); font-weight: 700; }
</style>
"""
    )


# =========================
# [BLK-PV-04] Preview rendering
# =========================
def _project_payload(p: Any) -> Dict[str, Any]:
    """project(dict)の“入れ子/フラット”どちらでも受けられるようにする。"""
    if not isinstance(p, dict):
        return {}
    data = p.get("data")
    if isinstance(data, dict):
        return data
    # 旧構造互換（step1/step2/blocks が直下にある）
    if any(k in p for k in ("step1", "step2", "blocks")):
        return p
    return {}


def _parse_multi_urls(raw: str) -> List[str]:
    raw = (raw or "").strip()
    if not raw:
        return []
    parts = re.split(r"[\n,]+", raw)
    urls = []
    for s in parts:
        u = (s or "").strip()
        if not u:
            continue
        urls.append(u)
    return urls


def render_preview(p: Dict[str, Any], mode: str = "mobile") -> None:
    """完成サイトのプレビューを描画する（mode: mobile/pc）"""

    inject_preview_styles()

    # “よくある壊れ方”を先に検出
    try:
        preflight = _preview_preflight_error()
        if preflight:
            with ui.element("div").classes("cvhb-panel cvhb-panel-pad").style("margin:12px;"):
                ui.label("プレビュー初期化エラー").classes("text-negative text-h6")
                ui.label(preflight).classes("text-negative")
            return
    except Exception:
        pass

    try:
        data = _project_payload(p)
        step1: Dict[str, Any] = data.get("step1", {}) or {}
        step2: Dict[str, Any] = data.get("step2", {}) or {}
        blocks: Dict[str, Any] = data.get("blocks", {}) or {}

        # ---- Common fields ----
        company_name = str(step2.get("company_name") or "会社名").strip()
        catch_copy = str(step2.get("catch_copy") or "スタッフ・利用者の笑顔を守る企業").strip()
        phone = str(step2.get("phone") or "").strip()
        email = str(step2.get("email") or "").strip()
        address = str(step2.get("address") or "").strip()

        industry_label = str(step1.get("industry") or "").strip()
        welfare_domain = str(step1.get("welfare_domain") or "").strip()
        welfare_mode = str(step1.get("welfare_mode") or "").strip()
        if industry_label == "福祉事業所" and (welfare_domain or welfare_mode):
            industry_label = f"福祉事業所（{welfare_domain}/{welfare_mode}）"

        # favicon（未入力なら非表示）
        favicon_url = str(step1.get("favicon_url") or step2.get("favicon_url") or "").strip()

        # ---- Theme ----
        style_vars = _preview_glass_style(step1, dark=None)
        accent = _accent_from_step1(step1)

        scroll_id = f"cvhb-scroll-{mode}"

        def scroll_to(anchor_id: str) -> None:
            # sticky header offset
            ui.run_javascript(
                f"""(function(){{
  const sc = document.getElementById('{scroll_id}');
  const el = document.getElementById('{anchor_id}');
  if(!sc || !el) return;
  const y = el.offsetTop - 62;
  sc.scrollTo({{top: y, behavior: 'smooth'}});
}})();"""
            )

        def scroll_top() -> None:
            ui.run_javascript(
                f"""(function(){{
  const sc = document.getElementById('{scroll_id}');
  if(!sc) return;
  sc.scrollTo({{top: 0, behavior: 'smooth'}});
}})();"""
            )

        # ---- Hero images (max 4) ----
        hero = blocks.get("hero", {}) or {}
        hero_choice = str(hero.get("hero_image") or HERO_IMAGE_OPTIONS[0]).strip()
        hero_raw_url = str(hero.get("hero_image_url") or "").strip()

        hero_urls = _parse_multi_urls(hero_raw_url)
        if not hero_urls:
            hero_urls = [str(HERO_IMAGE_PRESETS.get(hero_choice) or HERO_IMAGE_DEFAULT)]
        hero_urls = hero_urls[:4]

        # hero text
        sub_catch = str(hero.get("sub_catch") or "地域に寄り添い、安心できるサービスを届けます").strip()
        primary_btn = str(hero.get("primary_button_text") or "お問い合わせ").strip()
        secondary_btn = str(hero.get("secondary_button_text") or "見学・相談").strip()

        # ---- philosophy ----
        ph = blocks.get("philosophy", {}) or {}
        ph_title = str(ph.get("title") or "私たちの想い").strip()
        ph_body = str(ph.get("body") or "ここに理念や会社/事業所の紹介文を書きます。（あとで自由に書き換えてきます）").strip()
        ph_points = _safe_list(ph.get("points"), [])  # noqa: F405

        # ---- news ----
        news = blocks.get("news", {}) or {}
        news_items = _safe_list(news.get("items"), [])  # noqa: F405

        # ---- faq ----
        faq = blocks.get("faq", {}) or {}
        faq_items = _safe_list(faq.get("items"), [])  # noqa: F405

        # ---- access ----
        access = blocks.get("access", {}) or {}
        access_note = str(access.get("note") or "（例）〇〇駅から徒歩5分 / 駐車場あり").strip()
        map_url = str(access.get("map_url") or "").strip() or google_maps_url(address)  # noqa: F405

        # ---- contact ----
        contact = blocks.get("contact", {}) or {}
        contact_msg = str(contact.get("message") or "まずはお気軽にご相談ください。").strip()
        hours = str(contact.get("hours") or "平日 9:00〜18:00").strip()
        contact_btn = str(contact.get("button_text") or primary_btn).strip()

        # ---- about image preset (テーマ色に馴染む方向) ----
        # 入力欄が無いので、将来追加しても壊れないように “存在すれば採用” にする
        about_image_url = str(ph.get("image_url") or "").strip()
        if not about_image_url:
            # 暫定（木・自然系）
            about_image_url = "https://images.unsplash.com/photo-1441974231531-c6227db76b6e?auto=format&fit=crop&w=1200&q=60"

        # ---- Menu sections ----
        sections = [
            ("section-news", "お知らせ"),
            ("section-about", "私たちについて"),
            ("section-faq", "よくある質問"),
            ("section-access", "アクセス"),
            ("section-contact", "お問い合わせ"),
        ]

        # ===== Render =====
        with ui.element("div").classes(f"cvhb-site cvhb-site--{mode}").props(f'id="{scroll_id}"').style(style_vars):
            # Header
            with ui.element("header").classes("cvhb-header"):
                with ui.element("div").classes("cvhb-container"):
                    with ui.element("div").classes("cvhb-header-inner"):
                        # Brand (TOPへ)
                        with ui.element("div").classes("cvhb-brand"):
                            if favicon_url:
                                ui.image(favicon_url).classes("cvhb-favicon")
                            ui.button(company_name, on_click=scroll_top).props("flat").classes("cvhb-company")

                        # PC nav
                        with ui.element("nav").classes("cvhb-nav cvhb-only-pc"):
                            for sid, label in sections:
                                ui.button(label, on_click=lambda _sid=sid: scroll_to(_sid)).props("flat").classes("cvhb-nav-link")

                        # Hamburger (dialog)
                        menu_dialog = ui.dialog()
                        ui.button(icon="menu", on_click=menu_dialog.open).props("flat round").classes("cvhb-menu-btn")
                        with menu_dialog:
                            with ui.card().classes("q-pa-md").style("min-width: 260px;"):
                                ui.label("メニュー").classes("text-h6")
                                for sid, label in sections:
                                    ui.button(
                                        label,
                                        on_click=lambda _sid=sid: (menu_dialog.close(), scroll_to(_sid)),
                                    ).props("flat").classes("full-width")

            # Hero
            with ui.element("section").classes("cvhb-hero"):
                with ui.element("div").classes("cvhb-container cvhb-hero-grid"):
                    # copy
                    with ui.element("div").classes("cvhb-hero-copy"):
                        ui.html(f"<div class='cvhb-kicker'><span class='cvhb-badge'>TOP</span> <span>{industry_label or 'ホーム'}</span></div>")
                        ui.html(f"<h1>{catch_copy}</h1>")
                        ui.label(sub_catch).classes("cvhb-hero-sub")
                        with ui.element("div").classes("cvhb-hero-cta"):
                            ui.button(primary_btn, on_click=lambda: scroll_to("section-contact")).classes("cvhb-btn-primary")
                            ui.button(secondary_btn, on_click=lambda: scroll_to("section-about")).classes("cvhb-btn-ghost")

                    # media (slider)
                    hero_track_id = f"cvhb-hero-track-{mode}"
                    with ui.element("div").classes("cvhb-hero-media"):
                        with ui.element("div").classes("cvhb-hero-carousel"):
                            with ui.element("div").classes("cvhb-hero-track").props(f'id="{hero_track_id}"') as _track:
                                for u in hero_urls:
                                    ui.element("div").classes("cvhb-hero-slide").style(f"background-image: url('{u}');")
                    # slide-left timer (safe)
                    ui.run_javascript(
                        f"""(function(){{
  const id = '{hero_track_id}';
  const urls = {json.dumps(hero_urls)};
  const track = document.getElementById(id);
  if(!track) return;

  window.__cvhbHeroTimers = window.__cvhbHeroTimers || {{}};
  if(window.__cvhbHeroTimers[id]) {{
    clearInterval(window.__cvhbHeroTimers[id]);
    window.__cvhbHeroTimers[id] = null;
  }}

  let idx = 0;
  const apply = () => {{
    track.style.transform = `translateX(-${{idx * 100}}%)`;
  }};
  apply();

  if(urls.length <= 1) return;
  window.__cvhbHeroTimers[id] = setInterval(() => {{
    idx = (idx + 1) % urls.length;
    apply();
  }}, 5200);
}})();"""
                    )

            # NEWS
            with ui.element("section").classes("cvhb-section cvhb-section-tight").props('id="section-news"'):
                with ui.element("div").classes("cvhb-container"):
                    ui.html("<div class='cvhb-kicker'><span class='cvhb-badge'>NEWS</span><span>お知らせ</span></div>")
                    with ui.element("div").classes("cvhb-panel cvhb-panel-pad"):
                        if news_items:
                            cap = 4 if mode == "pc" else 3
                            with ui.element("div").classes("cvhb-news-list"):
                                for it in news_items[:cap]:
                                    date = str(it.get("date") or "").strip()
                                    category = str(it.get("category") or "お知らせ").strip()
                                    title = str(it.get("title") or "お知らせ").strip()
                                    with ui.element("div").classes("cvhb-news-item"):
                                        with ui.element("div").classes("cvhb-news-meta"):
                                            if date:
                                                ui.label(date)
                                            ui.html(f"<span class='cvhb-badge'>{category}</span>")
                                        ui.label(title).classes("cvhb-news-title")
                        else:
                            ui.label("まだお知らせはありません。").classes("cvhb-muted")

            # About (philosophy)
            with ui.element("section").classes("cvhb-section").props('id="section-about"'):
                with ui.element("div").classes("cvhb-container"):
                    ui.html("<div class='cvhb-kicker'><span class='cvhb-badge'>ABOUT</span><span>私たちについて</span></div>")
                    with ui.element("div").classes("cvhb-about-grid"):
                        # text
                        with ui.element("div").classes("cvhb-panel cvhb-panel-pad"):
                            ui.html(f"<div class='cvhb-section-title'><span style='font-size:18px'>🌳</span><span>{ph_title}</span></div>")
                            ui.label(ph_body).classes("cvhb-muted")
                            if ph_points:
                                with ui.element("div").classes("cvhb-points"):
                                    for pt in ph_points[:6]:
                                        ui.html(f"<span class='cvhb-point'>✔ {str(pt)}</span>")
                        # image
                        ui.image(about_image_url).classes("cvhb-about-img")

            # FAQ + Access/Contact
            with ui.element("section").classes("cvhb-section").props('id="section-faq"'):
                with ui.element("div").classes("cvhb-container"):
                    ui.html("<div class='cvhb-kicker'><span class='cvhb-badge'>FAQ</span><span>よくある質問</span></div>")
                    with ui.element("div").classes("cvhb-panel cvhb-panel-pad"):
                        if faq_items:
                            cap = 5 if mode == "pc" else 4
                            for qa in faq_items[:cap]:
                                q = str(qa.get("q") or "質問").strip()
                                a = str(qa.get("a") or "回答").strip()
                                ui.html(f"<div style='font-weight:1000; margin-top:10px;'>Q. {q}</div>")
                                ui.html(f"<div class='cvhb-muted' style='margin-top:6px;'>A. {a}</div>")
                        else:
                            ui.label("まだFAQはありません。").classes("cvhb-muted")

            with ui.element("section").classes("cvhb-section").props('id="section-access"'):
                with ui.element("div").classes("cvhb-container cvhb-grid-2"):
                    # Access
                    with ui.element("div").classes("cvhb-panel cvhb-panel-pad"):
                        ui.html("<div class='cvhb-kicker'><span class='cvhb-badge'>ACCESS</span><span>アクセス</span></div>")
                        ui.label(address or "住所（未入力）").classes("cvhb-muted")
                        ui.label(access_note).classes("cvhb-muted")
                        with ui.element("div").classes("cvhb-cta-box"):
                            with ui.element("div").classes("cvhb-cta-left"):
                                ui.html("<div class='cvhb-cta-main'>Googleマップで見る</div>")
                                ui.html("<div class='cvhb-cta-sub'>外部リンクで開きます</div>")
                            ui.link("開く", map_url).classes("cvhb-btn-primary")
                    # Contact
                    with ui.element("div").classes("cvhb-panel cvhb-panel-pad").props('id="section-contact"'):
                        ui.html("<div class='cvhb-kicker'><span class='cvhb-badge'>CONTACT</span><span>お問い合わせ</span></div>")
                        ui.label(contact_msg).classes("cvhb-muted")
                        if phone:
                            ui.html(f"<div class='cvhb-muted'><b>TEL：</b>{phone}</div>")
                        if email:
                            ui.html(f"<div class='cvhb-muted'><b>Email：</b>{email}</div>")
                        ui.html(f"<div class='cvhb-muted'><b>受付時間：</b>{hours}</div>")
                        with ui.element("div").classes("cvhb-hero-cta"):
                            ui.button(contact_btn, on_click=lambda: scroll_to("section-contact")).classes("cvhb-btn-primary")

            # Prefooter company info
            with ui.element("section").classes("cvhb-prefooter"):
                with ui.element("div").classes("cvhb-container"):
                    with ui.element("div").classes("cvhb-prefooter-inner"):
                        with ui.element("div"):
                            ui.html(f"<div style='font-weight:1000'>{company_name}</div>")
                            ui.html(f"<div class='cvhb-muted'>{industry_label or ''}</div>")
                        ui.button("お問い合わせへ", on_click=lambda: scroll_to("section-contact")).classes("cvhb-btn-primary")

            # Footer
            with ui.element("footer").classes("cvhb-footer"):
                with ui.element("div").classes("cvhb-container"):
                    ui.html(f"<div style='font-weight:1000; font-size:14px;'>{company_name}</div>")
                    with ui.element("div").classes("cvhb-footer-links").style("margin-top:10px;"):
                        for sid, label in sections:
                            ui.html(f"<span>{label}</span>")
                    ui.html(f"<div class='cvhb-copyright'>© CoreVistaJP / CV-HomeBuilder</div>")

    except Exception as e:
        with ui.element("div").classes("cvhb-panel cvhb-panel-pad").style("margin:12px;"):
            ui.label("プレビューでエラーが発生しました").classes("text-negative text-h6")
            ui.label(sanitize_error_text(str(e))).classes("text-negative")  # noqa: F405
            ui.label("詳細:").classes("text-negative")
            ui.code(traceback.format_exc()).classes("text-negative")
