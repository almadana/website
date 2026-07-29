#!/usr/bin/env python3
"""Sincroniza publicaciones desde ORCID hacia data/ para el sitio Quarto.

Uso:
  python3 scripts/sync_orcid.py

Escribe:
  data/publications.json   — caché estructurada
  data/publications.qmd    — listado para incluir en publi.qmd
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

try:
    import yaml
except ImportError:  # stdlib fallback: solo leemos orcid_id a mano
    yaml = None

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = Path(__file__).with_name("orcid.yml")
DATA_DIR = ROOT / "data"
JSON_PATH = DATA_DIR / "publications.json"
QMD_PATH = DATA_DIR / "publications.qmd"

API = "https://pub.orcid.org/v3.0"
HEADERS = {
    "Accept": "application/json",
    "User-Agent": "alvarocabana.uy-orcid-sync/1.0 (mailto:acabana@psico.edu.uy)",
}

TYPE_LABELS = {
    "journal-article": "Artículo",
    "preprint": "Preprint",
    "conference-paper": "Congreso",
    "book-chapter": "Capítulo",
    "book": "Libro",
    "dissertation-thesis": "Tesis",
    "data-set": "Datos",
    "software": "Software",
    "other": "Otro",
}


def load_orcid_id() -> str:
    text = CONFIG_PATH.read_text(encoding="utf-8")
    if yaml is not None:
        cfg = yaml.safe_load(text) or {}
        orcid = cfg.get("orcid_id")
    else:
        orcid = None
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("orcid_id:"):
                orcid = line.split(":", 1)[1].strip().strip("\"'")
                break
    if not orcid:
        sys.exit(f"No encontré orcid_id en {CONFIG_PATH}")
    return orcid


def api_get(path: str) -> dict:
    req = urllib.request.Request(f"{API}{path}", headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        sys.exit(f"ORCID HTTP {exc.code} en {path}: {exc.reason}")
    except urllib.error.URLError as exc:
        sys.exit(f"No pude conectar con ORCID: {exc.reason}")


def pick_summary(group: dict) -> dict:
    summaries = group.get("work-summary") or []
    if not summaries:
        raise ValueError("grupo sin work-summary")

    def score(s: dict) -> tuple:
        has_doi = 0
        for eid in (s.get("external-ids") or {}).get("external-id") or []:
            if eid.get("external-id-type") == "doi" and eid.get("external-id-relationship") == "self":
                has_doi = 1
        year = ((s.get("publication-date") or {}).get("year") or {}).get("value") or ""
        return (has_doi, 1 if s.get("journal-title") else 0, year, str(s.get("put-code")))

    return max(summaries, key=score)


def extract_doi(node: dict) -> str | None:
    for eid in (node.get("external-ids") or {}).get("external-id") or []:
        if eid.get("external-id-type") == "doi" and eid.get("external-id-relationship") in (None, "self"):
            return (eid.get("external-id-normalized") or {}).get("value") or eid.get("external-id-value")
    return None


def extract_url(node: dict, doi: str | None) -> str | None:
    url = (node.get("url") or {}).get("value")
    if url:
        return url
    for eid in (node.get("external-ids") or {}).get("external-id") or []:
        if eid.get("external-id-type") == "doi":
            u = (eid.get("external-id-url") or {}).get("value")
            if u:
                return u
    if doi:
        return f"https://doi.org/{doi}"
    return None


def extract_authors(work: dict) -> list[str]:
    authors: list[str] = []
    for c in (work.get("contributors") or {}).get("contributor") or []:
        role = ((c.get("contributor-attributes") or {}).get("contributor-role") or "author").lower()
        if role not in ("author", "co-investigator"):
            continue
        name = (c.get("credit-name") or {}).get("value")
        if name:
            authors.append(name.strip())
    return authors


def summarize_to_record(summary: dict, work: dict | None) -> dict:
    node = work or summary
    title = ((node.get("title") or {}).get("title") or {}).get("value") or "Sin título"
    year = ((node.get("publication-date") or {}).get("year") or {}).get("value")
    month = ((node.get("publication-date") or {}).get("month") or {}).get("value")
    journal = (node.get("journal-title") or {}).get("value")
    wtype = node.get("type") or summary.get("type") or "other"
    doi = extract_doi(node) or extract_doi(summary)
    url = extract_url(node, doi) or extract_url(summary, doi)
    authors = extract_authors(work) if work else []

    return {
        "put_code": summary.get("put-code"),
        "title": title,
        "year": int(year) if year and str(year).isdigit() else None,
        "month": int(month) if month and str(month).isdigit() else None,
        "type": wtype,
        "type_label": TYPE_LABELS.get(wtype, wtype.replace("-", " ").title()),
        "venue": journal,
        "authors": authors,
        "doi": doi,
        "url": url,
    }


def fetch_publications(orcid_id: str) -> list[dict]:
    payload = api_get(f"/{orcid_id}/works")
    groups = payload.get("group") or []
    records: list[dict] = []

    for i, group in enumerate(groups, start=1):
        summary = pick_summary(group)
        put_code = summary.get("put-code")
        work = None
        if put_code is not None:
            work = api_get(f"/{orcid_id}/work/{put_code}")
            # cortesía con la API pública
            time.sleep(0.15)
        records.append(summarize_to_record(summary, work))
        print(f"  [{i}/{len(groups)}] {records[-1]['title'][:70]}")

    before = len(records)
    records = dedupe_records(records)
    if before != len(records):
        print(f"  Deduplicados por título: {before} → {len(records)}")

    records.sort(key=lambda r: (r.get("year") or 0, r.get("month") or 0, r.get("title") or ""), reverse=True)
    return records


TYPE_RANK = {
    "journal-article": 0,
    "book-chapter": 1,
    "book": 2,
    "conference-paper": 3,
    "dissertation-thesis": 4,
    "preprint": 5,
    "data-set": 6,
    "software": 7,
    "other": 8,
}


def norm_title(title: str) -> str:
    return " ".join((title or "").lower().replace("‐", "-").replace("–", "-").split())


def dedupe_records(records: list[dict]) -> list[dict]:
    """Si ORCID lista preprint y paper con el mismo título, nos quedamos con uno."""
    best: dict[str, dict] = {}
    for rec in records:
        key = norm_title(rec.get("title") or "")
        if not key:
            continue
        prev = best.get(key)
        if prev is None:
            best[key] = rec
            continue
        prev_score = (
            -TYPE_RANK.get(prev.get("type") or "other", 9),
            1 if prev.get("doi") else 0,
            len(prev.get("authors") or []),
            prev.get("year") or 0,
        )
        new_score = (
            -TYPE_RANK.get(rec.get("type") or "other", 9),
            1 if rec.get("doi") else 0,
            len(rec.get("authors") or []),
            rec.get("year") or 0,
        )
        if new_score > prev_score:
            best[key] = rec
    return list(best.values())


def identity(rec: dict) -> str:
    if rec.get("doi"):
        return f"doi:{rec['doi'].lower()}"
    if rec.get("put_code") is not None:
        return f"put:{rec['put_code']}"
    return f"title:{(rec.get('title') or '').lower()}"


def diff_records(old: list[dict], new: list[dict]) -> tuple[list[dict], list[dict]]:
    old_ids = {identity(r) for r in old}
    new_ids = {identity(r) for r in new}
    added = [r for r in new if identity(r) not in old_ids]
    removed = [r for r in old if identity(r) not in new_ids]
    return added, removed


def md_escape(text: str) -> str:
    return text.replace("[", "\\[").replace("]", "\\]")


def format_authors(authors: list[str]) -> str:
    if not authors:
        return ""
    if len(authors) == 1:
        return authors[0]
    if len(authors) == 2:
        return f"{authors[0]} y {authors[1]}"
    return f"{', '.join(authors[:-1])} y {authors[-1]}"


def render_qmd(records: list[dict], orcid_id: str) -> str:
    lines = [
        "<!-- Generado por scripts/sync_orcid.py — no editar a mano -->",
        "",
        f"Fuente: [ORCID {orcid_id}](https://orcid.org/{orcid_id}).",
        "",
    ]

    by_year: dict[str, list[dict]] = {}
    for rec in records:
        key = str(rec["year"]) if rec.get("year") else "Sin fecha"
        by_year.setdefault(key, []).append(rec)

    year_keys = sorted(
        by_year.keys(),
        key=lambda y: (-1 if y == "Sin fecha" else -int(y)),
    )

    for year in year_keys:
        lines.append(f"## {year}")
        lines.append("")
        for rec in by_year[year]:
            title = md_escape(rec["title"])
            if rec.get("url"):
                title_md = f"[**{title}**]({rec['url']})"
            else:
                title_md = f"**{title}**"

            bits = [title_md]
            authors = format_authors(rec.get("authors") or [])
            if authors:
                bits.append(md_escape(authors))
            if rec.get("venue"):
                bits.append(f"*{md_escape(rec['venue'])}*")
            bits.append(f"`{rec.get('type_label') or rec.get('type')}`")

            lines.append(f"- {' · '.join(bits)}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def main() -> None:
    orcid_id = load_orcid_id()
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    old: list[dict] = []
    if JSON_PATH.exists():
        old = json.loads(JSON_PATH.read_text(encoding="utf-8")).get("publications") or []

    print(f"Consultando ORCID {orcid_id} …")
    records = fetch_publications(orcid_id)
    added, removed = diff_records(old, records)

    payload = {
        "orcid_id": orcid_id,
        "synced_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "count": len(records),
        "publications": records,
    }
    JSON_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    QMD_PATH.write_text(render_qmd(records, orcid_id), encoding="utf-8")

    print()
    print(f"Listo: {len(records)} publicaciones → {JSON_PATH.relative_to(ROOT)}")
    print(f"Listado Markdown → {QMD_PATH.relative_to(ROOT)}")
    if not old:
        print("Primera sincronización (sin base previa para comparar).")
    else:
        print(f"Nuevas: {len(added)} · Quitadas: {len(removed)}")
        for rec in added:
            print(f"  + {rec.get('year') or '????'} — {rec['title']}")
        for rec in removed:
            print(f"  - {rec.get('year') or '????'} — {rec['title']}")


if __name__ == "__main__":
    main()
