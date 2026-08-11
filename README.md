# Álvaro Cabana — sitio personal + blog

Sitio estático armado con [Quarto](https://quarto.org): página de presentación y blog profesional.

## Estructura

```text
├── _quarto.yml          # Configuración del sitio
├── index.qmd            # Inicio
├── publi.qmd            # Publicaciones (incluye data/publications.qmd)
├── proyectos.qmd        # Proyectos
├── prensa.qmd           # Prensa (incluye data/prensa.qmd)
├── datos.qmd            # Datos
├── charlas.qmd          # Charlas
├── blog.qmd             # Listado del blog
├── about.qmd            # Sobre mí
├── styles.scss          # Tema (paleta + tipografía)
├── custom.css           # Ajustes finos
├── assets/              # Favicon y recursos
├── data/                # Publicaciones ORCID + candidatos de prensa
├── scripts/sync_orcid.py
├── scripts/search_prensa.py
├── posts/               # Entradas del blog
└── docs/                # Salida para GitHub Pages (tras render)
```

## Requisitos

- [Quarto](https://quarto.org/docs/get-started/) ≥ 1.4

## Vista previa local

```bash
quarto preview
```

## Compilar para publicar

```bash
quarto render
```

El HTML queda en `docs/` (incluida en el repo para GitHub Pages).

En GitHub: **Settings → Pages → Deploy from a branch** → branch `main` → carpeta `/docs`.

### Dominio propio

Cuando tengas el dominio (p. ej. `alvarocabana.uy`):

1. Creá un archivo `docs/CNAME` con una línea: `alvarocabana.uy`  
   (o usá la opción Custom domain en GitHub Pages; a veces regenera el CNAME solo).
2. Actualizá `site-url` en `_quarto.yml`.

## Actualizar publicaciones (ORCID)

```bash
python3 scripts/sync_orcid.py
quarto render publi.qmd
```

El script lee el ORCID en `scripts/orcid.yml`, descarga las obras públicas y escribe `data/publications.json` + `data/publications.qmd`. En corridas siguientes informa qué títulos son nuevos o se quitaron.

## Actualizar prensa (candidatos + votos)

```bash
python3 -m venv .venv
.venv/bin/pip install ddgs pyyaml requests beautifulsoup4
.venv/bin/python scripts/search_prensa.py search   # Google News + web
.venv/bin/python scripts/search_prensa.py list     # ver ordenados por fecha
.venv/bin/python scripts/search_prensa.py vote     # sí/no en consola
.venv/bin/python scripts/search_prensa.py render   # escribe data/prensa.qmd
quarto render prensa.qmd
```

Los candidatos viven en `data/prensa_candidates.json` (y una vista en `.md`). Solo los `vote: yes` salen en la página. Consultas y filtros: `scripts/prensa.yml`.

## Nueva entrada del blog

```bash
mkdir -p posts/mi-nueva-nota
```

Creá `posts/mi-nueva-nota/index.qmd`:

```yaml
---
title: "Título"
description: "Resumen corto."
author: "Álvaro Cabana"
date: "2026-07-20"
categories: [tema]
---

Texto de la nota…
```

Volvé a renderizar o usá `quarto preview` para ver el cambio.

## Personalización rápida

| Qué | Dónde |
| --- | --- |
| Nombre, nav, footer | `_quarto.yml` |
| Colores y tipografía | `styles.scss` |
| Texto de inicio / bio | `index.qmd`, `about.qmd` |
| Enlaces redes | `_quarto.yml` (navbar right) |

## Nota sobre el diseño

Paleta **Atlántico**: fondo niebla, tinta `#15202b`, acento teal `#0e6e6e`.  
Tipografías: **Fraunces** (títulos) + **Source Sans 3** (cuerpo).
