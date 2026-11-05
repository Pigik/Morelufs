"""Юридические документы: web/legal/*.md → HTML-страницы /privacy, /offer, /consent, /requisites.

Исходник — Markdown (его правят вручную, без программиста). Поддерживается только то, что есть
в документах: заголовки #/##/###, абзацы, списки «- », таблицы, **жирный**, [ссылки](url) и голые https-ссылки.
Страницы собираются один раз при старте: после правки .md нужен перезапуск (деплой).
"""

import re
from dataclasses import dataclass
from html import escape
from pathlib import Path

# slug (он же адрес страницы) → название в навигации
PAGES = {
    "offer": "Публичная оферта",
    "privacy": "Политика конфиденциальности",
    "consent": "Согласие на обработку ПДн",
    "requisites": "Реквизиты",
}

EDITION = re.compile(r"^Редакция от\s+(.+?)\s*$", re.MULTILINE)
INLINE = re.compile(r"\*\*(.+?)\*\*|\[([^\]]+)\]\(([^)\s]+)\)|(https?://[^\s<)]+[^\s<).,;:])")


@dataclass(frozen=True, slots=True)
class LegalPage:
    slug: str
    title: str
    edition: str | None  # «Редакция от …» из документа; пишется в заказ как версия принятых условий
    html: str


def inline(text: str) -> str:
    """Экранирует текст и превращает **жирный**, [ссылку](url) и голые https-ссылки в HTML."""
    out, pos = [], 0
    for m in INLINE.finditer(text):
        out.append(escape(text[pos:m.start()], quote=False))
        bold, label, href, bare = m.groups()
        if bold is not None:
            out.append(f"<b>{inline(bold)}</b>")
        else:
            href = href or bare
            # только свои адреса и https: javascript: и прочее в документ не пропускаем
            if not href.startswith(("https://", "/", "mailto:")):
                out.append(escape(m.group(0), quote=False))
            else:
                out.append(f'<a href="{escape(href)}">{escape(label or bare, quote=False)}</a>')
        pos = m.end()
    out.append(escape(text[pos:], quote=False))
    return "".join(out)


def table_row(line: str, cell: str) -> str:
    cells = [c.strip() for c in line.strip().strip("|").split("|")]
    return "<tr>" + "".join(f"<{cell}>{inline(c)}</{cell}>" for c in cells) + "</tr>"


def to_html(md: str) -> str:
    """Markdown документа → HTML. Блоки разделяются пустыми строками или сменой типа строки."""
    html: list[str] = []
    para: list[str] = []
    items: list[str] = []
    rows: list[str] = []

    def flush() -> None:
        if para:
            html.append(f"<p>{'<br>'.join(inline(p) for p in para)}</p>")
            para.clear()
        if items:
            html.append("<ul>" + "".join(f"<li>{inline(i)}</li>" for i in items) + "</ul>")
            items.clear()
        if rows:
            head, *body = [r for r in rows if not re.fullmatch(r"\|?[\s:|-]+\|?", r)]
            html.append('<div class="table"><table><thead>' + table_row(head, "th") + "</thead><tbody>"
                        + "".join(table_row(r, "td") for r in body) + "</tbody></table></div>")
            rows.clear()

    for raw in md.splitlines():
        line = raw.rstrip()
        heading = re.match(r"^(#{1,3})\s+(.*)$", line)
        if not line.strip():
            flush()
        elif heading:
            flush()
            level = len(heading.group(1))
            html.append(f"<h{level}>{inline(heading.group(2))}</h{level}>")
        elif line.startswith("|"):
            if para or items:
                flush()
            rows.append(line)
        elif line.startswith("- "):
            if para or rows:
                flush()
            items.append(line[2:])
        else:
            if items or rows:
                flush()
            para.append(line.strip())
    flush()
    return "\n".join(html)


def page_html(slug: str, title: str, body: str) -> str:
    current = ' aria-current="page"'
    nav = "".join(f'<a href="/{s}"{current if s == slug else ""}>{escape(t)}</a>' for s, t in PAGES.items())
    docs = "[" + ", ".join(f'"/{s}"' for s in PAGES) + "]"
    return TEMPLATE.format(title=escape(title), body=body, nav=nav, docs=docs)


def load(legal_dir: Path) -> dict[str, LegalPage]:
    """Читает и собирает все документы. Отсутствующий файл — нет страницы (404), сайт работает."""
    pages = {}
    for slug, nav_title in PAGES.items():
        path = legal_dir / f"{slug}.md"
        if not path.is_file():
            continue
        md = path.read_text(encoding="utf-8")
        first = next((ln for ln in md.splitlines() if ln.startswith("# ")), "")
        title = first[2:].strip() or nav_title
        edition = EDITION.search(md)
        pages[slug] = LegalPage(slug, title, edition and edition.group(1), page_html(slug, title, to_html(md)))
    return pages


def accepted_version(pages: dict[str, LegalPage]) -> str:
    """Какие редакции документов покупатель принял, ставя галочки при заказе."""
    def ed(slug: str) -> str:
        page = pages.get(slug)
        return (page and page.edition) or "?"
    return f"согласие {ed('consent')}; политика {ed('privacy')}; оферта {ed('offer')}"


# Стиль — как у сайта: чёрный фон, Source Code Pro. Отдельно от style.css, чтобы страница была лёгкой.
TEMPLATE = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title} — MORELUFS</title>
<link rel="icon" href="/favicon.ico" sizes="any">
<link rel="icon" type="image/png" sizes="96x96" href="/assets/img/favicon-96.png">
<link rel="icon" type="image/png" sizes="144x144" href="/assets/img/favicon-144.png">
<link rel="apple-touch-icon" href="/assets/img/apple-touch-icon.png">
<meta name="theme-color" content="#000000">
<style>
@font-face {{ font-family: 'Source Code Pro'; src: url('/assets/fonts/source-code-pro-400.woff2') format('woff2');
  font-weight: 400; font-display: swap; }}
@font-face {{ font-family: 'Source Code Pro'; src: url('/assets/fonts/source-code-pro-600.woff2') format('woff2');
  font-weight: 600 700; font-display: swap; }}
:root {{ --bg: #000; --text: #fff; --soft: #cfcfcf; --hint: #8a8a8a; --line: rgba(255,255,255,.15); }}
* {{ box-sizing: border-box; }}
html {{ -webkit-text-size-adjust: 100%; }}
body {{ margin: 0; background: var(--bg); color: var(--soft);
  font: 14px/1.65 'Source Code Pro', ui-monospace, Menlo, Consolas, monospace; -webkit-font-smoothing: antialiased; }}
/* шапка — как на сайте (style.css: .topbar, .logo, .topbar-close): «M» слева, крестик справа */
.topbar {{ position: sticky; top: 0; z-index: 1; display: flex; justify-content: space-between; align-items: center;
  padding: calc(18px + env(safe-area-inset-top)) 16px 12px; background: var(--bg); }}
.logo, .topbar-close {{ display: flex; align-items: center; height: 36px; text-decoration: none;
  transition: opacity .15s ease; -webkit-tap-highlight-color: transparent; }}
.logo img {{ display: block; height: 28px; width: auto; }}
.topbar-close {{ justify-content: center; width: 36px; margin-right: -8px; color: var(--text); font-size: 22px;
  line-height: 1; }}
.logo:active, .topbar-close:active {{ opacity: .6; }}
:focus-visible {{ outline: 1px solid var(--text); outline-offset: 3px; }}
main {{ max-width: 760px; margin: 0 auto; padding: 32px 16px 48px; overflow-wrap: anywhere; }}
h1 {{ color: var(--text); font-size: 22px; line-height: 1.3; margin: 0 0 16px; }}
h2 {{ color: var(--text); font-size: 16px; margin: 36px 0 12px; }}
h3 {{ color: var(--text); font-size: 14px; margin: 24px 0 8px; }}
p, ul {{ margin: 0 0 12px; }}
ul {{ padding-left: 20px; }}
li {{ margin-bottom: 4px; }}
b {{ color: var(--text); }}
a {{ color: var(--text); text-underline-offset: 3px; }}
.table {{ overflow-x: auto; margin: 0 0 16px; }}
table {{ border-collapse: collapse; min-width: 560px; font-size: 13px; }}
th, td {{ border: 1px solid var(--line); padding: 8px 10px; text-align: left; vertical-align: top; }}
th {{ color: var(--text); font-weight: 600; }}
nav {{ display: flex; flex-wrap: wrap; justify-content: space-between; gap: 12px 24px; max-width: 760px; margin: 0 auto;
  padding: 24px 16px 16px; border-top: 1px solid var(--line); }}
nav a {{ color: var(--hint); font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: .04em;
  text-decoration: none; }}
nav a[aria-current] {{ color: var(--text); }}
/* кредит разработчика — под документами, отдельной строкой, не в навигации по документам */
.credit {{ max-width: 760px; margin: 0 auto; padding: 0 16px calc(24px + env(safe-area-inset-bottom));
  text-align: center; }}
.credit a {{ color: var(--hint); font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: .04em;
  text-decoration: none; }}
@media (hover: hover) {{
  nav a:hover, .credit a:hover {{ color: var(--text); }}
  .logo:hover, .topbar-close:hover {{ opacity: .6; }}
}}
@media (min-width: 900px) {{
  .topbar {{ padding: 36px 40px 16px; }}
  .logo, .topbar-close {{ height: 40px; }}
  .topbar-close {{ width: 40px; }}
  .logo img {{ height: 36px; }}
  h1 {{ font-size: 26px; }}
}}
</style>
</head>
<body>
<header class="topbar">
<a class="logo" href="/" aria-label="MORELUFS — на главную">
<img src="/assets/img/m.webp" alt="" width="326" height="254"></a>
<a class="topbar-close" href="/" aria-label="Закрыть">&#10005;</a>
</header>
<main>
{body}
</main>
<nav aria-label="Документы">{nav}</nav>
<footer class="credit"><a href="https://t.me/pigik_fun" target="_blank" rel="noopener">Разработчик сайта</a></footer>
<script>
// Крестик — «назад»: сайт сам вернётся на тот экран и в то место, откуда открыли документ (app.js, RETURN_KEY).
// Открыли не с сайта (поиск, внешний браузер из мини-аппа) — некуда «назад», крестик ведёт на главную.
(() => {{
  const docs = new Set({docs});
  // сайт перед переходом сюда кладёт точку возврата; без неё — пришли ли с сайта (а не из другого документа)
  const fromSite = () => {{
    try {{
      if (sessionStorage.getItem('mls_return')) return true;
      const ref = new URL(document.referrer);
      return ref.origin === location.origin && !docs.has(ref.pathname);
    }} catch {{ return false; }}
  }};
  document.querySelector('.topbar-close').addEventListener('click', (e) => {{
    if (history.length > 1 && fromSite()) {{ e.preventDefault(); history.back(); }}
  }});
  // Переход между документами заменяет запись в истории:
  // крестик по-прежнему возвращает на сайт, а не в прошлый документ
  document.querySelectorAll('main a, nav a').forEach((a) => a.addEventListener('click', (e) => {{
    const url = new URL(a.href, location.href);
    if (url.origin !== location.origin || !docs.has(url.pathname) ||
        e.button || e.metaKey || e.ctrlKey || e.shiftKey) return;
    e.preventDefault();
    location.replace(url.href);
  }}));
}})();
</script>
</body>
</html>
"""
