#!/usr/bin/env python3
"""Busca apariciones en prensa y arma una lista de candidatos para votar.

Uso:
  # (recomendado) entorno local del repo
  python3 -m venv .venv && .venv/bin/pip install ddgs requests beautifulsoup4

  .venv/bin/python scripts/search_prensa.py search   # buscar y fusionar candidatos
  .venv/bin/python scripts/search_prensa.py list     # ver candidatos (orden fecha)
  .venv/bin/python scripts/search_prensa.py vote     # votar sí/no en consola
  .venv/bin/python scripts/search_prensa.py add --url URL --title "Título" [--medio M] [--date YYYY-MM-DD] [--yes]
  .venv/bin/python scripts/search_prensa.py render   # volcar los "sí" a data/prensa.qmd

Archivos:
  data/prensa_candidates.json  — estado (incluye votos); se fusiona en cada search
  data/prensa_candidates.md    — vista legible para revisar/votar a mano
  data/prensa.qmd              — ítems con vote=yes (incluye en prensa.qmd)
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

try:
    import yaml
except ImportError:
    yaml = None

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = Path(__file__).with_name("prensa.yml")
DATA_DIR = ROOT / "data"
JSON_PATH = DATA_DIR / "prensa_candidates.json"
MD_PATH = DATA_DIR / "prensa_candidates.md"
QMD_PATH = DATA_DIR / "prensa.qmd"

UA = "alvarocabana.uy-prensa-search/1.0 (mailto:acabana@psico.edu.uy)"
HEADERS = {"User-Agent": UA, "Accept": "application/rss+xml, application/xml, text/xml, */*"}

# Día/mes de dos dígitos primero (si no, /2021/02/26 captura día=2).
DATE_IN_URL = re.compile(
    r"(?:/(?P<y>20\d{2})/(?P<m>1[0-2]|0[1-9]|[1-9])(?:/(?P<d>3[01]|[12]\d|0[1-9]|[1-9]))?)"
    r"|(?:(?P<y2>20\d{2})(?P<m2>1[0-2]|0[1-9])(?P<d2>3[01]|[12]\d|0[1-9]))"
)

# "Feb 26, 2021" / "26 de febrero de 2021" en snippets de buscadores
MONTHS_ES = {
    "enero": 1,
    "febrero": 2,
    "marzo": 3,
    "abril": 4,
    "mayo": 5,
    "junio": 6,
    "julio": 7,
    "agosto": 8,
    "septiembre": 9,
    "setiembre": 9,
    "octubre": 10,
    "noviembre": 11,
    "diciembre": 12,
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}
DATE_IN_TEXT = re.compile(
    r"\b(?P<mon>[A-Za-záéíóú]+)\.?\s+(?P<d>\d{1,2}),?\s+(?P<y>20\d{2})\b"
    r"|\b(?P<d2>\d{1,2})\s+de\s+(?P<mon2>[A-Za-záéíóú]+)\s+de\s+(?P<y2>20\d{2})\b"
    r"|\b(?P<d3>\d{1,2})/(?P<m3>\d{1,2})/(?P<y3>20\d{2})\b"
)


def load_config() -> dict[str, Any]:
    text = CONFIG_PATH.read_text(encoding="utf-8")
    if yaml is None:
        sys.exit("Necesito PyYAML (o el venv del proyecto) para leer prensa.yml")
    cfg = yaml.safe_load(text) or {}
    if not cfg.get("google_news_queries") and not cfg.get("web_queries"):
        sys.exit(f"Config vacía en {CONFIG_PATH}")
    return cfg


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def cid_for(url: str) -> str:
    return hashlib.sha1(url.strip().encode("utf-8")).hexdigest()[:12]


def norm_text(s: str) -> str:
    s = html.unescape(s or "")
    s = s.replace("\xa0", " ")
    return re.sub(r"\s+", " ", s).strip()


def fold(s: str) -> str:
    """Minúsculas y sin tildes para matching blando."""
    table = str.maketrans("áéíóúüñÁÉÍÓÚÜÑ", "aeiouunAEIOUUN")
    return (s or "").translate(table).lower()


def domain_of(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return ""
    return host[4:] if host.startswith("www.") else host


def media_label(cfg: dict, url: str, fallback: str | None = None) -> str:
    labels: dict = cfg.get("media_labels") or {}
    host = domain_of(url)
    if host in labels:
        return labels[host]
    for key, label in labels.items():
        if host.endswith(key):
            return label
    if fallback:
        return fallback
    return host or "Desconocido"


def strip_title_suffix(title: str, medio: str | None) -> str:
    t = norm_text(title)
    # Google News suele anexar " - Medio"
    if " - " in t:
        left, right = t.rsplit(" - ", 1)
        if medio and fold(right) == fold(medio):
            return left
        if len(right) < 40 and not right.endswith("?"):
            return left
    return t


def _ymd(y: str | int, mo: str | int, d: str | int = 1) -> str | None:
    try:
        yi, mi, di = int(y), int(mo), int(d)
        if not (2000 <= yi <= 2100 and 1 <= mi <= 12 and 1 <= di <= 31):
            return None
        return f"{yi:04d}-{mi:02d}-{di:02d}"
    except Exception:
        return None


def date_from_url(url: str) -> str | None:
    m = DATE_IN_URL.search(url)
    if not m:
        return None
    y = m.group("y") or m.group("y2")
    mo = m.group("m") or m.group("m2")
    d = m.group("d") or m.group("d2") or "1"
    return _ymd(y, mo, d)


def date_from_text(text: str) -> str | None:
    if not text:
        return None
    m = DATE_IN_TEXT.search(text)
    if not m:
        return None
    if m.group("y"):
        mon = MONTHS_ES.get(fold(m.group("mon")))
        if not mon:
            return None
        return _ymd(m.group("y"), mon, m.group("d"))
    if m.group("y2"):
        mon = MONTHS_ES.get(fold(m.group("mon2")))
        if not mon:
            return None
        return _ymd(m.group("y2"), mon, m.group("d2"))
    return _ymd(m.group("y3"), m.group("m3"), m.group("d3"))


def best_date(*candidates: str | None) -> str | None:
    """Prefiere fechas con día real (no default -01)."""
    valid = [c for c in candidates if c]
    if not valid:
        return None
    scored = [(0 if d.endswith("-01") else 1, d) for d in valid]
    scored.sort(reverse=True)
    return scored[0][1]


def cleanup_store(cfg: dict, store: dict[str, Any]) -> int:
    """Quita o auto-rechaza candidatos que ya no pasarían el filtro."""
    keep: list[dict[str, Any]] = []
    dropped = 0
    for c in store.get("candidates") or []:
        sc, reasons = score_item(
            cfg,
            title=c.get("title") or "",
            snippet=c.get("snippet") or "",
            url=c.get("url") or "",
            medio=c.get("medio"),
        )
        # recalcular fecha por si el parser mejoró
        c["date"] = best_date(
            c.get("date"),
            date_from_url(c.get("url") or ""),
            date_from_text(c.get("snippet") or ""),
            date_from_text(c.get("title") or ""),
        )
        # refrescar etiqueta de medio si el config mejoró
        c["medio"] = media_label(cfg, c.get("url") or "", c.get("medio"))
        c["score"] = sc
        c["score_reasons"] = reasons
        if sc < 0 and c.get("vote") is None:
            dropped += 1
            continue
        keep.append(c)
    store["candidates"] = keep
    return dropped


def parse_rss_date(pub: str | None) -> str | None:
    if not pub:
        return None
    try:
        return parsedate_to_datetime(pub).date().isoformat()
    except Exception:
        return None


def name_hits(text: str, names: list[str]) -> bool:
    f = fold(text)
    for n in names:
        if fold(n) in f:
            return True
    # apellido + contexto mínimo
    if "cabana" in f and ("alvaro" in f or "álvaro" in fold(text)):
        return True
    return False


def score_item(
    cfg: dict,
    *,
    title: str,
    snippet: str,
    url: str,
    medio: str | None,
) -> tuple[int, list[str]]:
    names = cfg.get("person_names") or []
    keywords = [fold(k) for k in (cfg.get("keywords") or [])]
    blocks = [fold(b) for b in (cfg.get("block_terms") or [])]
    block_domains = [d.lower() for d in (cfg.get("block_domains") or [])]

    blob = fold(f"{title} {snippet} {medio or ''} {url}")
    reasons: list[str] = []
    score = 0

    host = domain_of(url)
    for bd in block_domains:
        if bd in host:
            return -100, [f"dominio bloqueado:{bd}"]

    for b in blocks:
        if b and b in blob:
            return -100, [f"término bloqueado:{b}"]

    if name_hits(f"{title} {snippet}", names):
        score += 5
        reasons.append("nombre")
        if name_hits(title, names):
            score += 2
            reasons.append("nombre-en-título")
    else:
        # sin nombre visible: solo si hay keywords fuertes + Cabana
        if "cabana" not in blob:
            return -50, ["sin-nombre"]
        score -= 2
        reasons.append("solo-apellido")

    for kw in keywords:
        if kw and kw in blob:
            score += 1
            reasons.append(f"kw:{kw}")

    # Preferir medios .uy
    if host.endswith(".uy") or ".uy/" in url:
        score += 1
        reasons.append("uy")

    return score, reasons


def http_get(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def search_google_news(cfg: dict) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for q in cfg.get("google_news_queries") or []:
        params = urllib.parse.urlencode(
            {"q": q, "hl": "es-419", "gl": "UY", "ceid": "UY:es"}
        )
        url = f"https://news.google.com/rss/search?{params}"
        try:
            raw = http_get(url)
        except (urllib.error.URLError, TimeoutError) as exc:
            print(f"  [google-news] error en query: {exc}", file=sys.stderr)
            continue
        try:
            root = ET.fromstring(raw)
        except ET.ParseError as exc:
            print(f"  [google-news] XML inválido: {exc}", file=sys.stderr)
            continue

        n = 0
        for it in root.findall(".//item"):
            title = norm_text(it.findtext("title") or "")
            link = (it.findtext("link") or "").strip()
            if not link or not title:
                continue
            src_el = it.find("source")
            medio_rss = norm_text(src_el.text) if src_el is not None and src_el.text else None
            date = parse_rss_date(it.findtext("pubDate"))
            snippet = norm_text(it.findtext("description") or "")
            # description de GNews a veces trae HTML
            snippet = re.sub(r"<[^>]+>", " ", snippet)
            snippet = norm_text(snippet)
            medio = media_label(cfg, link, medio_rss)
            title_clean = strip_title_suffix(title, medio_rss or medio)
            sc, reasons = score_item(
                cfg, title=title_clean, snippet=snippet, url=link, medio=medio
            )
            if sc < 3:
                continue
            out.append(
                {
                    "url": link,
                    "title": title_clean,
                    "medio": medio,
                    "date": best_date(date, date_from_url(link), date_from_text(snippet)),
                    "snippet": snippet[:400],
                    "source": "google-news",
                    "query": q,
                    "score": sc,
                    "score_reasons": reasons,
                }
            )
            n += 1
        print(f"  [google-news] {n} utiles ← {q[:70]}{'…' if len(q) > 70 else ''}")
        time.sleep(0.4)
    return out


def search_ddgs(cfg: dict) -> list[dict[str, Any]]:
    try:
        from ddgs import DDGS
    except ImportError:
        print("  [web] ddgs no instalado — salteo búsquedas web (pip install ddgs)")
        return []

    out: list[dict[str, Any]] = []
    queries = cfg.get("web_queries") or []
    with DDGS() as ddgs:
        for q in queries:
            try:
                results = list(
                    ddgs.text(q, max_results=12, region="uy-es", backend="auto")
                )
            except Exception as exc:  # noqa: BLE001 — backends fallan a menudo
                print(f"  [web] fallo ({q[:50]}…): {exc}", file=sys.stderr)
                continue
            n = 0
            for r in results:
                title = norm_text(r.get("title") or "")
                link = (r.get("href") or r.get("url") or "").strip()
                snippet = norm_text(r.get("body") or r.get("description") or "")
                if not link or not title:
                    continue
                medio = media_label(cfg, link)
                sc, reasons = score_item(
                    cfg, title=title, snippet=snippet, url=link, medio=medio
                )
                if sc < 4:
                    continue
                out.append(
                    {
                        "url": link,
                        "title": strip_title_suffix(title, medio),
                        "medio": medio,
                        "date": best_date(date_from_url(link), date_from_text(snippet), date_from_text(title)),
                        "snippet": snippet[:400],
                        "source": "web",
                        "query": q,
                        "score": sc,
                        "score_reasons": reasons,
                    }
                )
                n += 1
            print(f"  [web] {n} utiles ← {q[:70]}{'…' if len(q) > 70 else ''}")
            time.sleep(0.6)
    return out


def load_store() -> dict[str, Any]:
    if not JSON_PATH.exists():
        return {"updated_at": None, "candidates": []}
    return json.loads(JSON_PATH.read_text(encoding="utf-8"))


def save_store(store: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    store["updated_at"] = now_iso()
    store["candidates"] = sort_candidates(store.get("candidates") or [])
    JSON_PATH.write_text(
        json.dumps(store, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def sort_key(c: dict[str, Any]) -> tuple:
    d = c.get("date") or ""
    # sin fecha al final; dentro del mismo día, mayor score primero
    return (0 if d else 1, d and f"9999-{d}" or "", -(c.get("score") or 0), c.get("title") or "")


def sort_candidates(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # fecha descendente, sin fecha al final
    def key(c: dict[str, Any]) -> tuple:
        d = c.get("date")
        has = 1 if d else 0
        return (has, d or "", c.get("score") or 0, c.get("title") or "")

    return sorted(items, key=key, reverse=True)


def merge_candidates(store: dict[str, Any], found: list[dict[str, Any]]) -> tuple[int, int]:
    by_id = {c["id"]: c for c in store.get("candidates") or [] if c.get("id")}
    # también indexar por URL normalizada
    by_url = {c.get("url"): c for c in by_id.values()}

    new = 0
    updated = 0
    for item in found:
        url = item["url"]
        existing = by_url.get(url) or by_id.get(cid_for(url))
        if existing:
            # no pisar voto; mejorar metadata si hace falta
            changed = False
            for field in ("title", "medio", "snippet", "date", "score", "score_reasons", "source", "query"):
                if item.get(field) and (
                    not existing.get(field)
                    or (field == "score" and (item.get("score") or 0) > (existing.get("score") or 0))
                    or (field == "date" and not existing.get("date") and item.get("date"))
                ):
                    if existing.get(field) != item.get(field):
                        existing[field] = item[field]
                        changed = True
            if changed:
                existing["updated_at"] = now_iso()
                updated += 1
            continue

        cand = {
            "id": cid_for(url),
            "url": url,
            "title": item.get("title"),
            "medio": item.get("medio"),
            "date": item.get("date"),
            "snippet": item.get("snippet"),
            "source": item.get("source"),
            "query": item.get("query"),
            "score": item.get("score"),
            "score_reasons": item.get("score_reasons"),
            "vote": None,  # null | "yes" | "no"
            "voted_at": None,
            "found_at": now_iso(),
            "updated_at": now_iso(),
            "notes": "",
        }
        by_id[cand["id"]] = cand
        by_url[url] = cand
        new += 1

    store["candidates"] = list(by_id.values())
    return new, updated


def write_markdown(store: dict[str, Any]) -> None:
    cands = sort_candidates(store.get("candidates") or [])
    pending = [c for c in cands if c.get("vote") is None]
    yes = [c for c in cands if c.get("vote") == "yes"]
    no = [c for c in cands if c.get("vote") == "no"]

    lines = [
        "# Candidatos de prensa",
        "",
        f"_Actualizado: {store.get('updated_at') or '—'} · total {len(cands)} "
        f"(pendientes {len(pending)}, sí {len(yes)}, no {len(no)})_",
        "",
        "Editá `vote` en `data/prensa_candidates.json` (`yes` / `no` / `null`),",
        "o corré `python scripts/search_prensa.py vote`.",
        "Orden: **fecha descendente** (sin fecha al final).",
        "",
    ]

    def section(title: str, items: list[dict[str, Any]]) -> None:
        lines.append(f"## {title}")
        lines.append("")
        if not items:
            lines.append("_Ninguno._")
            lines.append("")
            return
        for c in items:
            d = c.get("date") or "s/f"
            vote = c.get("vote")
            mark = {"yes": "[x]", "no": "[ ]", None: "[?]"}.get(vote, "[?]")
            lines.append(f"### {mark} {d} · {c.get('medio') or '?'}")
            lines.append("")
            lines.append(f"**{c.get('title') or '(sin título)'}**")
            lines.append("")
            lines.append(f"- id: `{c.get('id')}`")
            lines.append(f"- url: {c.get('url')}")
            if c.get("snippet"):
                lines.append(f"- extracto: {c['snippet'][:280]}")
            lines.append(f"- score: {c.get('score')} ({', '.join(c.get('score_reasons') or [])})")
            lines.append(f"- vote: `{vote}`")
            lines.append("")

    section("Pendientes", pending)
    section("Aceptados (vote: yes)", yes)
    section("Descartados (vote: no)", no)

    MD_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def cmd_search(args: argparse.Namespace) -> int:
    cfg = load_config()
    print("Buscando candidatos…")
    found: list[dict[str, Any]] = []
    found.extend(search_google_news(cfg))
    if not args.no_web:
        found.extend(search_ddgs(cfg))

    store = load_store()
    new, updated = merge_candidates(store, found)
    dropped = cleanup_store(cfg, store)
    save_store(store)
    write_markdown(store)

    cands = store["candidates"]
    pending = sum(1 for c in cands if c.get("vote") is None)
    print(
        f"Listo: +{new} nuevos, {updated} actualizados, {dropped} filtrados, "
        f"{len(cands)} en total ({pending} pendientes de voto)."
    )
    print(f"  → {JSON_PATH.relative_to(ROOT)}")
    print(f"  → {MD_PATH.relative_to(ROOT)}")
    return 0


def fmt_row(c: dict[str, Any]) -> str:
    d = c.get("date") or "s/f      "
    medio = (c.get("medio") or "?")[:28]
    title = (c.get("title") or "")[:70]
    vote = c.get("vote")
    mark = {None: "?", "yes": "Y", "no": "N"}.get(vote, "?")
    return f"[{mark}] {d}  {medio:<28}  {title}"


def cmd_list(args: argparse.Namespace) -> int:
    store = load_store()
    cands = sort_candidates(store.get("candidates") or [])
    if args.pending:
        cands = [c for c in cands if c.get("vote") is None]
    elif args.yes:
        cands = [c for c in cands if c.get("vote") == "yes"]
    elif args.no:
        cands = [c for c in cands if c.get("vote") == "no"]

    if not cands:
        print("No hay candidatos (corrés `search` primero).")
        return 0

    print(f"{'voto':4} {'fecha':10}  {'medio':28}  título")
    print("-" * 90)
    for c in cands:
        print(fmt_row(c))
        if args.urls:
            print(f"     {c.get('url')}")
    print("-" * 90)
    print(f"{len(cands)} ítems · fuente: {JSON_PATH.relative_to(ROOT)}")
    return 0


def cmd_vote(args: argparse.Namespace) -> int:
    store = load_store()
    cands = sort_candidates(store.get("candidates") or [])
    pending = [c for c in cands if c.get("vote") is None]
    if args.revote:
        pending = cands

    if not pending:
        print("Nada pendiente de voto. Usá --revote para revisarlos todos.")
        return 0

    print(
        "Votación: [y] sí  [n] no  [s] saltar  [q] guardar y salir\n"
        "Orden: fecha descendente.\n"
    )
    changed = 0
    for i, c in enumerate(pending, 1):
        print("=" * 72)
        print(f"({i}/{len(pending)})  {c.get('date') or 's/f'}  ·  {c.get('medio')}")
        print(c.get("title") or "(sin título)")
        print(c.get("url"))
        if c.get("snippet"):
            print(f"— {c['snippet'][:300]}")
        if c.get("vote") is not None:
            print(f"(voto actual: {c['vote']})")
        while True:
            try:
                ans = input("Voto [y/n/s/q]? ").strip().lower()
            except EOFError:
                ans = "q"
            if ans in {"y", "yes", "si", "sí"}:
                c["vote"] = "yes"
                c["voted_at"] = now_iso()
                changed += 1
                break
            if ans in {"n", "no"}:
                c["vote"] = "no"
                c["voted_at"] = now_iso()
                changed += 1
                break
            if ans in {"s", "skip", "saltar", ""}:
                break
            if ans in {"q", "quit", "salir"}:
                save_store(store)
                write_markdown(store)
                print(f"Guardado ({changed} cambios).")
                return 0
            print("  respuestas: y / n / s / q")

    save_store(store)
    write_markdown(store)
    print(f"Listo ({changed} cambios). Luego: python scripts/search_prensa.py render")
    return 0


def set_votes_bulk(ids: list[str], vote: str) -> int:
    store = load_store()
    by_id = {c["id"]: c for c in store.get("candidates") or []}
    n = 0
    for i in ids:
        c = by_id.get(i)
        if not c:
            print(f"  id desconocido: {i}", file=sys.stderr)
            continue
        c["vote"] = vote
        c["voted_at"] = now_iso()
        n += 1
    save_store(store)
    write_markdown(store)
    return n


def cmd_accept(args: argparse.Namespace) -> int:
    n = set_votes_bulk(args.ids, "yes")
    print(f"Aceptados: {n}")
    return 0


def cmd_reject(args: argparse.Namespace) -> int:
    n = set_votes_bulk(args.ids, "no")
    print(f"Rechazados: {n}")
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    """Sugerir una nota a mano (queda pendiente o aceptada con --yes)."""
    cfg = load_config()
    url = (args.url or "").strip()
    title = norm_text(args.title or "")
    if not url or not title:
        print("Hacen falta --url y --title.", file=sys.stderr)
        return 2

    medio = norm_text(args.medio) if args.medio else media_label(cfg, url)
    if not args.medio:
        medio = media_label(cfg, url, medio)
    date = args.date or best_date(date_from_url(url), date_from_text(title))
    if args.date and not re.fullmatch(r"20\d{2}-\d{2}-\d{2}", args.date):
        print("Usá --date en formato YYYY-MM-DD.", file=sys.stderr)
        return 2
    snippet = norm_text(args.snippet or "")

    item = {
        "url": url,
        "title": strip_title_suffix(title, medio),
        "medio": medio,
        "date": date,
        "snippet": snippet[:400],
        "source": "manual",
        "query": "manual",
        "score": 99,
        "score_reasons": ["manual"],
    }
    store = load_store()
    existing = next(
        (c for c in store.get("candidates") or [] if c.get("url") == url),
        None,
    )
    if existing and not args.force:
        print(
            f"Ya existe id={existing['id']} vote={existing.get('vote')!r}.\n"
            f"  {existing.get('title')}\n"
            f"Usá --force para actualizar metadata, o accept/reject con ese id."
        )
        return 1

    new, updated = merge_candidates(store, [item])
    # merge no pisa score alto a la fuerza en updates parciales; reubicar
    cand = next(c for c in store["candidates"] if c.get("url") == url)
    cand["source"] = "manual"
    cand["query"] = "manual"
    cand["title"] = item["title"]
    cand["medio"] = medio
    if date:
        cand["date"] = date
    if snippet:
        cand["snippet"] = snippet[:400]
    cand["score"] = 99
    cand["score_reasons"] = ["manual"]
    cand["updated_at"] = now_iso()
    if args.yes:
        cand["vote"] = "yes"
        cand["voted_at"] = now_iso()
    elif args.no:
        cand["vote"] = "no"
        cand["voted_at"] = now_iso()
    # si era nuevo y no hay voto, queda null

    save_store(store)
    write_markdown(store)
    estado = cand.get("vote")
    print(
        f"{'Actualizado' if existing else 'Agregado'} id={cand['id']}  "
        f"vote={estado!r}  {cand.get('date') or 's/f'} · {cand.get('medio')}"
    )
    print(f"  {cand.get('title')}")
    print(f"  {cand.get('url')}")
    if estado == "yes":
        print("Tip: corré `render` para volcarlo a la página.")
    elif estado is None:
        print("Tip: `accept <id>` o `vote` cuando quieras decidir.")
    return 0


def cmd_render(_args: argparse.Namespace) -> int:
    store = load_store()
    yes = [c for c in sort_candidates(store.get("candidates") or []) if c.get("vote") == "yes"]

    lines = [
        "<!-- Generado por scripts/search_prensa.py render — no editar a mano -->",
        "",
    ]
    if not yes:
        lines += [
            "_Todavía no hay apariciones aceptadas._",
            "",
            "Corré la búsqueda, votá candidatos y volvé a renderizar:",
            "",
            "```bash",
            "python scripts/search_prensa.py search",
            "python scripts/search_prensa.py vote",
            "python scripts/search_prensa.py render",
            "```",
            "",
        ]
    else:
        current_year: str | None = None
        for c in yes:
            d = c.get("date")
            year = (d or "")[:4] if d else "Sin fecha"
            if year != current_year:
                if current_year is not None:
                    lines.append("")
                lines.append(f"## {year}")
                lines.append("")
                current_year = year
            medio = c.get("medio") or "Medio"
            title = c.get("title") or c.get("url")
            url = c.get("url")
            date_bit = f"{d} · " if d else ""
            # Sin líneas en blanco entre ítems: si no, pandoc hace lista "loose"
            # y mete <p> dentro de cada <li> (cambia tipografía/márgenes).
            lines.append(f"- **{date_bit}{medio}** — [{title}]({url})")
        lines.append("")

    QMD_PATH.write_text("\n".join(lines), encoding="utf-8")
    print(f"Escrito {QMD_PATH.relative_to(ROOT)} ({len(yes)} ítems aceptados)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Candidatos de prensa para Álvaro Cabana")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("search", help="Buscar y fusionar candidatos")
    s.add_argument("--no-web", action="store_true", help="Solo Google News RSS")
    s.set_defaults(func=cmd_search)

    s = sub.add_parser("list", help="Listar candidatos ordenados por fecha")
    s.add_argument("--pending", action="store_true")
    s.add_argument("--yes", action="store_true")
    s.add_argument("--no", action="store_true")
    s.add_argument("--urls", action="store_true")
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("vote", help="Votar candidatos en consola")
    s.add_argument("--revote", action="store_true", help="Incluir ya votados")
    s.set_defaults(func=cmd_vote)

    s = sub.add_parser("accept", help="Aceptar por id")
    s.add_argument("ids", nargs="+")
    s.set_defaults(func=cmd_accept)

    s = sub.add_parser("reject", help="Rechazar por id")
    s.add_argument("ids", nargs="+")
    s.set_defaults(func=cmd_reject)

    s = sub.add_parser("add", help="Sugerir / registrar una nota a mano")
    s.add_argument("--url", required=True, help="URL de la nota o aparición")
    s.add_argument("--title", required=True, help="Título de la nota")
    s.add_argument("--medio", help="Nombre del medio (si no, se infiere del dominio)")
    s.add_argument("--date", help="Fecha YYYY-MM-DD")
    s.add_argument("--snippet", help="Extracto opcional")
    s.add_argument("--yes", action="store_true", help="Aceptarla de una")
    s.add_argument("--no", action="store_true", help="Descartarla de una")
    s.add_argument("--force", action="store_true", help="Actualizar si la URL ya existe")
    s.set_defaults(func=cmd_add)

    s = sub.add_parser("render", help="Generar data/prensa.qmd desde votos yes")
    s.set_defaults(func=cmd_render)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
