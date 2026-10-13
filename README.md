# Graveyard Keeper Wiki — offline PWA

Offline copy of <https://graveyardkeeper.fandom.com> as a static site and installable PWA,
with full-text search and a crafting cart that breaks builds down to raw materials.

## Build

```sh
python3 scrape.py            # → site/  (API responses cached in site/_cache, re-runs are incremental)
python3 scrape.py --refresh  # re-fetch every page
```

No dependencies beyond the Python 3 standard library.

## Run

The PWA (service worker, install, offline download) needs http(s):

```sh
python3 -m http.server 8765 --directory site   # http://localhost:8765
```

To install on a phone/tablet, host `site/` on any HTTPS static host (GitHub Pages,
Cloudflare Pages, Netlify…), open it, "Add to Home Screen", then press
**Download everything for offline** on the home page (~70 MB).
Opening `site/index.html` as a file also works, just without offline install.

## Files

| file | what |
|---|---|
| `scrape.py` | fetches pages/wikitext/images via the MediaWiki API, writes `site/` |
| `recipes.py` | parses recipe tables (`{{Item|…}}` rows, `==Cost==` sections) from wikitext |
| `search.js` | header search: titles + lazily loaded full-text index |
| `app.js` | cart storage, “Add to cart” buttons, service worker registration |
| `cart.js` | cart page: expands recipes to raw materials and ordered crafting steps |
| `sw.js` | service worker: app shell, cache-as-you-browse, full offline download |
| `style.css`, `icons/` | styling (light/dark, phone/tablet), app icons |

Recipes are parsed heuristically from wiki tables; when an item has several recipes the
cart lets you pick one, or mark an intermediate item as “I have it / buy it”.

## License

Wiki text and images are from the [Graveyard Keeper Fandom wiki](https://graveyardkeeper.fandom.com)
under [CC BY-SA 3.0](https://creativecommons.org/licenses/by-sa/3.0/); each page links back to its
original. Game assets belong to Lazy Bear Games / tinyBuild. This is an unofficial fan copy.

## Deploy (GitHub Pages)

`main` holds the source; the built `site/` is published from the `gh-pages` branch:

```sh
python3 scrape.py && ./deploy.sh
```

## Automatic updates & versioning

`.github/workflows/update-wiki.yml` runs weekly, Mondays 04:00 UTC (and on demand from the Actions tab):

1. `python3 scrape.py --content content` — asks the wiki's `recentchanges` API what was edited
   since `content/state.json`, refetches only those pages (template edits re-render everything),
   downloads new images; API responses and images persist in the Actions cache.
2. Commits `content/` — one `.wiki` file per page plus `recipes.json` — so `git log content/`
   is the wiki's history and `git diff` shows exactly what changed between snapshots.
3. On `main`, redeploys GitHub Pages if anything changed.
