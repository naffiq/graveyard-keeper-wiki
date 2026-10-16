#!/usr/bin/env python3
"""Download the Graveyard Keeper Fandom wiki into a static offline site.

Usage:  python3 scrape.py [--out site] [--workers 6] [--refresh]

Pulls every article and category page through the MediaWiki API, rewrites
links to local files, downloads every referenced image, and writes an
index.html with title search. API responses are cached in <out>/_cache so
re-runs only fetch what is missing (use --refresh to refetch pages).
"""
import argparse
import hashlib
import html
import json
import os
import re
import shutil
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import recipes as recipe_parser

BASE = "https://graveyardkeeper.fandom.com"
API = BASE + "/api.php"
UA = "Mozilla/5.0 (offline-wiki-archiver; personal use)"
NAMESPACES = {0: "", 14: "Category:"}
SPRITE_SHEET = ("https://static.wikia.nocookie.net/graveyardkeeper_gamepedia_en/images/"
                "6/6c/ItemSprite.png/revision/latest")


_cooldown = {}  # host -> monotonic time until which all threads hold off
_cooldown_lock = threading.Lock()


def http_get(url, retries=8):
    """GET with retries. A 429/503 pauses every thread hitting that host
    (honouring Retry-After), since Fandom rate-limits per client, not per request."""
    host = urllib.parse.urlparse(url).netloc
    for attempt in range(retries):
        with _cooldown_lock:
            pause = _cooldown.get(host, 0) - time.monotonic()
        if pause > 0:
            time.sleep(pause)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read()
        except Exception as e:
            if attempt == retries - 1:
                raise
            wait = min(120, 5 * 2 ** attempt)
            if isinstance(e, urllib.error.HTTPError) and e.code in (429, 503):
                retry_after = e.headers.get("Retry-After", "")
                if retry_after.isdigit():
                    wait = max(wait, int(retry_after))
                with _cooldown_lock:
                    _cooldown[host] = max(_cooldown.get(host, 0), time.monotonic() + wait)
            print(f"  retry {url[:90]} in {wait}s ({e})", file=sys.stderr)
            time.sleep(wait)


def api(**params):
    params.update(format="json", formatversion="2")
    return json.loads(http_get(API + "?" + urllib.parse.urlencode(params)))


def list_titles(ns, redirects):
    """Yield page titles in a namespace (non-redirects), or redirect pairs."""
    params = dict(action="query", list="allpages", apnamespace=ns, aplimit="max",
                  apfilterredir="redirects" if redirects else "nonredirects")
    while True:
        data = api(**params)
        for p in data["query"]["allpages"]:
            yield p["title"]
        if "continue" not in data:
            break
        params.update(data["continue"])


def resolve_redirects(titles):
    mapping = {}
    for i in range(0, len(titles), 50):
        data = api(action="query", titles="|".join(titles[i:i + 50]), redirects=1)
        for r in data["query"].get("redirects", []):
            mapping[r["from"]] = (r["to"], r.get("tofragment"))
    return mapping


_files = {}       # title -> filename
_files_used = {}  # lowercased filename -> title


def register_files(titles):
    """Assign filenames up front, in sorted order, so they are stable across runs.

    Wiki titles are case-sensitive ("Red Paint" and "Red paint" are separate
    pages) but macOS/Windows filesystems are not, so a later title whose name
    differs only by case gets a numeric suffix instead of overwriting the first.
    """
    for t in sorted(titles):
        page_file(t)


def page_file(title):
    """Local filename for a wiki title (flat dir, filesystem-safe, case-unique)."""
    if title in _files:
        return _files[title]
    base = _raw_file(title)
    name, n = base, 1
    while name.lower() in _files_used:
        n += 1
        name = f"{base}~{n}"
    _files_used[name.lower()] = title
    _files[title] = name + ".html"
    return _files[title]


def _raw_file(title):
    name = title.replace(" ", "_").replace(":", "_", 1) if title.startswith("Category:") \
        else title.replace(" ", "_")
    name = re.sub(r'[/\\?%*|"<>:]', lambda m: "%%%02X" % ord(m.group()), name)
    if len(name.encode()) > 200:
        name = name[:120] + "_" + hashlib.md5(title.encode()).hexdigest()[:8]
    return name


def write_text(path, text):
    """Write a file, recreating it so its name keeps the exact letter case
    (macOS keeps the old spelling when overwriting "A.html" with "a.html")."""
    if os.path.exists(path):
        os.remove(path)
    with open(path, "w") as f:
        f.write(text)


def page_href(title, frag=""):
    """URL-quoted href for a page (filenames may contain literal %XX)."""
    return urllib.parse.quote(page_file(title)) + frag


def norm_title(t):
    t = urllib.parse.unquote(t).replace("_", " ").strip()
    return t[:1].upper() + t[1:] if t else t


class Archiver:
    def __init__(self, out, workers, refresh):
        self.out = out
        self.workers = workers
        self.refresh = refresh
        self.cache = os.path.join(out, "_cache")
        self.wiki_dir = os.path.join(out, "wiki")
        self.img_dir = os.path.join(out, "images")
        for d in (self.cache, self.wiki_dir, self.img_dir):
            os.makedirs(d, exist_ok=True)
        self.pages = {}       # title -> parse result
        self.redirects = {}   # title -> (target, fragment)
        self.images = {}      # remote url -> local relative path (from wiki/)
        self.full_images = {} # image path key -> best local path
        self.failed = []
        self.recipes = []
        self.products = set()
        self.page_products = {}  # page title -> craftable things described on it

    # ---------- fetching ----------
    # ---------- incremental updates ----------
    def invalidate_changed(self, state_path):
        """Drop cached copies of pages edited on the wiki since the last run.

        Uses list=recentchanges (kept by MediaWiki for ~90 days). Edits to
        templates/modules change how every page renders, so they invalidate
        the whole parse cache; wikitext of untouched pages stays valid.
        Returns the run start time to record as the next starting point.
        """
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        try:
            with open(state_path) as f:
                since = json.load(f)["updated"]
        except (OSError, ValueError, KeyError):
            print("no previous state: full cache is used as-is")
            return now
        changed, templates = set(), False
        params = dict(action="query", list="recentchanges", rcend=since, rclimit="max",
                      rcprop="title|timestamp", rcnamespace="0|10|14|828", rctype="edit|new|log")
        while True:
            data = api(**params)
            for rc in data["query"]["recentchanges"]:
                if rc["ns"] in (10, 828):
                    templates = True
                else:
                    changed.add(rc["title"])
            if "continue" not in data:
                break
            params.update(data["continue"])
        print(f"since {since}: {len(changed)} pages changed, templates changed: {templates}")
        wt_path = os.path.join(self.cache, "wikitext.json")
        if os.path.exists(wt_path) and changed:
            with open(wt_path) as f:
                wikitext = json.load(f)
            for t in changed:
                wikitext.pop(t, None)
            with open(wt_path, "w") as f:
                json.dump(wikitext, f)
        for name in os.listdir(self.cache):
            if name in ("meta.json", "wikitext.json"):
                continue
            title_hashes = {hashlib.md5(t.encode()).hexdigest() + ".json" for t in changed}
            if templates or name in title_hashes:
                os.remove(os.path.join(self.cache, name))
        return now

    def export_content(self, content_dir, updated):
        """Versionable snapshot: one wikitext file per page + parsed recipes."""
        pages_dir = os.path.join(content_dir, "pages")
        os.makedirs(pages_dir, exist_ok=True)
        keep = set()
        for title in sorted(self.pages):
            if title not in self.wikitext:
                continue
            name = page_file(title)[:-len(".html")] + ".wiki"
            keep.add(name.lower())
            text = self.wikitext[title]
            path = os.path.join(pages_dir, name)
            try:
                with open(path) as f:
                    if f.read() == text:
                        continue
            except OSError:
                pass
            write_text(path, text)
        for name in os.listdir(pages_dir):
            if name.lower() not in keep:
                os.remove(os.path.join(pages_dir, name))
        with open(os.path.join(content_dir, "recipes.json"), "w") as f:
            f.write("[\n" + ",\n".join(json.dumps(r, ensure_ascii=False) for r in self.recipes) + "\n]\n")
        with open(os.path.join(content_dir, "redirects.json"), "w") as f:
            json.dump(dict(sorted(self.redirects.items())), f, ensure_ascii=False, indent=0)
        with open(os.path.join(content_dir, "state.json"), "w") as f:
            json.dump({"updated": updated, "pages": len(self.pages), "recipes": len(self.recipes)}, f, indent=1)

    def collect_titles(self):
        titles, redirect_titles = [], []
        for ns in NAMESPACES:
            titles += list(list_titles(ns, False))
            redirect_titles += list(list_titles(ns, True))
        print(f"{len(titles)} pages, {len(redirect_titles)} redirects")
        self.redirects = resolve_redirects(redirect_titles)
        with open(os.path.join(self.cache, "meta.json"), "w") as f:
            json.dump({"titles": titles, "redirects": self.redirects}, f)
        return titles

    def fetch_page(self, title):
        path = os.path.join(self.cache, hashlib.md5(title.encode()).hexdigest() + ".json")
        if os.path.exists(path) and not self.refresh:
            with open(path) as f:
                return title, json.load(f)
        try:
            data = api(action="parse", page=title, prop="text|displaytitle|categories",
                       disableeditsection=1, redirects=1)
        except Exception as e:  # rate limit outlasted the retries; try next run
            print(f"  ! {title}: {e}", file=sys.stderr)
            self.failed.append(title)
            return title, None
        if "error" in data:
            print(f"  ! {title}: {data['error'].get('info')}", file=sys.stderr)
            return title, None
        result = data["parse"]
        with open(path, "w") as f:
            json.dump(result, f)
        return title, result

    def fetch_all(self, titles):
        done = 0
        with ThreadPoolExecutor(self.workers) as ex:
            for title, result in ex.map(self.fetch_page, titles):
                done += 1
                if result:
                    self.pages[title] = result
                if done % 50 == 0 or done == len(titles):
                    print(f"  pages {done}/{len(titles)}")
        if self.failed:
            # Stop before building: a snapshot missing pages would record them
            # as deleted. Everything fetched so far stays in the cache.
            sys.exit(f"{len(self.failed)} pages could not be fetched (rate limited?); "
                     "cached progress is kept, run again to continue")

    def drop_case_duplicates(self):
        """Drop junk twins of pages whose titles differ only by letter case.

        The wiki has a few, e.g. "Red paint" (marked for deletion as a duplicate
        of "Red Paint") or an empty "Category:Alchemy Products" next to the used
        "Category:Alchemy products". The junk twin becomes a redirect.
        """
        used_cats = {norm_title(c["category"]) for p in self.pages.values()
                     for c in p.get("categories", [])}

        def junk(title):
            cats = {norm_title(c["category"]).lower() for c in self.pages[title].get("categories", [])}
            if any("candidates for" in c and "deletion" in c for c in cats):
                return True
            return title.startswith("Category:") and title[len("Category:"):] not in used_cats

        groups = {}
        for t in self.pages:
            groups.setdefault(t.lower(), []).append(t)
        for twins in groups.values():
            if len(twins) < 2:
                continue
            keep = [t for t in twins if not junk(t)]
            if len(keep) != 1:
                continue
            for t in twins:
                if t != keep[0]:
                    print(f"  dropping duplicate {t!r} -> {keep[0]!r}")
                    del self.pages[t]
                    self.wikitext.pop(t, None)
                    self.redirects[t] = (keep[0], None)

    def fetch_wikitext(self, titles):
        """Raw wikitext of every page (recipe tables are parsed from it)."""
        path = os.path.join(self.cache, "wikitext.json")
        if os.path.exists(path) and not self.refresh:
            with open(path) as f:
                self.wikitext = json.load(f)
        else:
            self.wikitext = {}
        missing = [t for t in titles if t not in self.wikitext]
        for i in range(0, len(missing), 50):
            data = api(action="query", prop="revisions", rvprop="content", rvslots="main",
                       titles="|".join(missing[i:i + 50]))
            for p in data["query"]["pages"]:
                if p.get("revisions"):
                    self.wikitext[p["title"]] = p["revisions"][0]["slots"]["main"]["content"]
            print(f"  wikitext {min(i + 50, len(missing))}/{len(missing)}")
            with open(path, "w") as f:  # save per batch so a failed run keeps progress
                json.dump(self.wikitext, f)
        with open(path, "w") as f:
            json.dump(self.wikitext, f)

    # ---------- images ----------
    @staticmethod
    def image_key(url):
        m = re.search(r"/images/([0-9a-f]/[0-9a-f]{2}/[^/?]+)", url)
        return m.group(1) if m else None

    def local_image(self, url):
        url = html.unescape(url)
        if url in self.images:
            return self.images[url]
        parsed = urllib.parse.urlparse(url)
        key = self.image_key(url)
        base = urllib.parse.unquote(key.rsplit("/", 1)[-1]) if key else os.path.basename(parsed.path)
        stem, ext = os.path.splitext(base)
        size = re.search(r"scale-to-width-down/(\d+)", parsed.path)
        if size:
            stem += f"-{size.group(1)}px"
        h = hashlib.md5(url.encode()).hexdigest()[:6]
        safe = re.sub(r"[^\w.\-]+", "_", stem)[:80] + "-" + h + (ext or ".img")
        # Fandom serves .webp/.png etc. as-is; keep extension from the file name.
        rel = "../images/" + safe
        self.images[url] = rel
        if key and not size:
            self.full_images[key] = rel
        elif key:
            self.full_images.setdefault(key, rel)
        return rel

    def download_image(self, item):
        url, rel = item
        dest = os.path.join(self.wiki_dir, rel)
        if os.path.exists(dest):
            return True
        try:
            data = http_get(url)
        except Exception as e:
            print(f"  ! image {url}: {e}", file=sys.stderr)
            return False
        with open(dest + ".part", "wb") as f:
            f.write(data)
        os.replace(dest + ".part", dest)
        return True

    def download_images(self):
        items = list(self.images.items())
        print(f"downloading {len(items)} images")
        done = 0
        with ThreadPoolExecutor(self.workers) as ex:
            for _ in ex.map(self.download_image, items):
                done += 1
                if done % 200 == 0 or done == len(items):
                    print(f"  images {done}/{len(items)}")

    # ---------- rewriting ----------
    def page_for(self, name):
        """Page describing an item name: exact title, redirect, then a
        case-insensitive match (recipes say "Cream of Mushroom Soup", the
        page is "Cream of mushroom soup")."""
        page = self.redirects.get(name, (name, None))[0]
        if page in self.pages:
            return page
        if not hasattr(self, "_lower_pages"):
            self._lower_pages = {t.lower(): t for t in self.pages}
        return self._lower_pages.get(name.lower())

    def link_target(self, href):
        """Map a wiki href to a local file, or return None if not local."""
        m = re.match(r"^(?:https?://graveyardkeeper\.fandom\.com)?/wiki/([^?#]+)(#.*)?$", href)
        if not m:
            return None
        title = norm_title(m.group(1))
        frag = m.group(2) or ""
        if title in self.redirects:
            title, tofrag = self.redirects[title]
            if tofrag and not frag:
                frag = "#" + tofrag.replace(" ", "_")
        if title in self.pages:
            return page_href(title, frag)
        return None

    def rewrite(self, text):
        # Lazy-loaded images: promote data-src to src.
        def img(m):
            tag = m.group(0)
            ds = re.search(r'\sdata-src="([^"]+)"', tag)
            if ds:
                tag = re.sub(r'\ssrc="[^"]*"', "", tag)
                tag = tag.replace(ds.group(0), f' src="{ds.group(1)}"')
            tag = re.sub(r'\s(?:srcset|data-srcset)="[^"]*"', "", tag)
            tag = re.sub(r'\ssrc="(https?://static\.wikia\.nocookie\.net/[^"]+)"',
                         lambda s: f' src="{self.local_image(s.group(1))}"', tag)
            return tag.replace("lazyload", "")
        text = re.sub(r"<img\b[^>]*>", img, text)

        def href(m):
            url = html.unescape(m.group(1))
            if url.startswith("#"):
                return m.group(0)
            if "static.wikia.nocookie.net" in url:
                key = self.image_key(url)
                local = self.full_images.get(key) if key else None
                return f'href="{local}"' if local else m.group(0)
            if "redlink=1" in url or "auth.fandom.com" in url:
                return 'data-missing="1"'
            local = self.link_target(url)
            if local:
                return f'href="{html.escape(local, quote=True)}"'
            if url.startswith("/"):
                return f'href="{html.escape(BASE + url, quote=True)}" class="external"'
            return m.group(0)
        return re.sub(r'href="([^"]*)"', href, text)

    # ---------- output ----------
    @staticmethod
    def head(root, title):
        return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>{title}</title>
<link rel="stylesheet" href="{root}style.css">
<link rel="manifest" href="{root}manifest.webmanifest">
<meta name="theme-color" content="#2a211d">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<link rel="apple-touch-icon" href="{root}icons/apple-touch-icon.png">
<link rel="icon" type="image/png" href="{root}icons/icon-192.png">
<script>window.GK_SITE = "gk1";</script>
<script data-goatcounter="https://naffiq.goatcounter.com/count" async src="https://gc.zgo.at/count.js"></script>
</head><body>"""

    @staticmethod
    def footer():
        return ('<footer class="site-footer">Developed by <a href="https://naffiq.com">naffiq</a> · '
                f'content from the <a href="{BASE}">Graveyard Keeper Fandom wiki</a>, '
                '<a href="https://creativecommons.org/licenses/by-sa/3.0/">CC BY-SA 3.0</a></footer>')

    @staticmethod
    def header(root, extra):
        return f"""<header><a href="{root}index.html" class="home"><img src="{root}icons/logo.png" alt="" width="30" height="32">Graveyard Keeper Wiki</a>
<div class="search"><input id="search" type="search" autocomplete="off"
  placeholder="Search the wiki…  ( / )"><div id="search-results" hidden></div></div>
<a href="{root}cart.html" class="cart-link" title="Crafting cart">🛒<span class="cart-count" hidden></span></a>
{extra}</header>
<script src="{root}search.js" defer></script>
<script src="{root}app.js" defer></script>"""

    def render(self, title, body, cats, source):
        cat_links = " · ".join(
            f'<a href="{html.escape(page_href("Category:" + c), quote=True)}">{html.escape(c)}</a>'
            for c in cats)
        products = self.page_products.get(source, [])
        add_btn = "".join(
            f'<button class="cart-add" data-item="{html.escape(p, quote=True)}">+ '
            f'{"Add to cart" if len(products) == 1 else html.escape(p)}</button>' for p in products)
        if add_btn:
            add_btn = f'<div class="cart-adds">{add_btn}</div>'

        orig = f'<a href="{BASE}/wiki/{urllib.parse.quote(source.replace(" ", "_"))}" class="orig">original ↗</a>'
        return f"""{self.head("../", html.escape(re.sub(r"<[^>]+>", "", html.unescape(title))) + " — Graveyard Keeper Wiki")}
{self.header("../", orig)}
<main><div class="title-row"><h1>{title}</h1>{add_btn}</div>
{body}
{f'<footer class="cats">Categories: {cat_links}</footer>' if cat_links else ''}
</main>
{self.footer()}
</body></html>"""

    def canon_cat(self, raw):
        """Category name, folding case variants into the one that has a page
        (pages are tagged with both "Witch Hill workstations" and "Witch hill workstations")."""
        name = norm_title(raw)
        if "Category:" + name in self.pages:
            return name
        if not hasattr(self, "_cat_pages"):
            self._cat_pages = {t.lower(): t[len("Category:"):] for t in self.pages
                               if t.startswith("Category:")}
        canon = self._cat_pages.get(("Category:" + name).lower(), name)
        if canon != name:
            self.redirects.setdefault("Category:" + name, ("Category:" + canon, None))
        return canon

    def write_pages(self):
        members = {}
        for title, p in self.pages.items():
            for c in p.get("categories", []):
                if c.get("hidden"):
                    continue
                cat = self.canon_cat(c["category"])
                if title not in members.get(cat, []):
                    members.setdefault(cat, []).append(title)
        register_files(list(self.pages) + ["Category:" + c for c in members])

        # Rewrite all pages first so the image map is complete before download.
        for title, p in self.pages.items():
            cats = list(dict.fromkeys(self.canon_cat(c["category"]) for c in p.get("categories", [])
                                      if not c.get("hidden")))
            cats = [c for c in cats if ("Category:" + c) in self.pages or c in members]
            body = self.rewrite(p["text"])
            if title.startswith("Category:"):
                body += self.category_listing(members.get(title[len("Category:"):], []))
            display = p.get("displaytitle") or html.escape(title)
            write_text(os.path.join(self.wiki_dir, page_file(title)),
                       self.render(display, body, cats, title))

        # Compare lowercased: on a case-insensitive filesystem removing a stale
        # "Ice_Axe.html" would delete the live "Ice_axe.html".
        written = {page_file(t).lower() for t in self.pages} | \
                  {page_file("Category:" + c).lower() for c in members}
        for name in os.listdir(self.wiki_dir):
            if name.endswith(".html") and name.lower() not in written:
                os.remove(os.path.join(self.wiki_dir, name))

        # Categories that have members but no page of their own on the wiki.
        for cat, titles in members.items():
            full = "Category:" + cat
            if full in self.pages:
                continue
            self.pages[full] = {"text": "", "categories": []}
            write_text(os.path.join(self.wiki_dir, page_file(full)),
                       self.render(html.escape(full), self.category_listing(titles), [], full))

    def category_listing(self, titles):
        items = "".join(
            f'<li><a href="{html.escape(page_href(t), quote=True)}">{html.escape(t)}</a></li>'
            for t in sorted(titles, key=str.lower))
        return f'<h2>Pages in this category ({len(titles)})</h2><ul class="catlist">{items}</ul>'

    @staticmethod
    def plain_text(text):
        """Searchable text of a parsed page (navboxes at the bottom are noise)."""
        cut = text.find('class="navbox"')
        if cut > 0:
            text = text[:text.rfind("<", 0, cut)]
        text = re.sub(r"<(style|script)\b.*?</\1>", " ", text, flags=re.S)
        text = re.sub(r"<[^>]+>", " ", text)
        return re.sub(r"\s+", " ", html.unescape(text)).strip()

    def write_search_index(self):
        def js(var, data):
            return f"window.{var} = " + json.dumps(data, ensure_ascii=False).replace("</", "<\\/") + ";\n"
        titles = [[t, page_href(t)] for t in sorted(self.pages, key=str.lower)]
        titles += [[src, page_href(dst) + ("#" + frag.replace(" ", "_") if frag else "")]
                   for src, (dst, frag) in self.redirects.items() if dst in self.pages]
        texts = [[t, page_href(t), self.plain_text(p["text"])]
                 for t, p in sorted(self.pages.items()) if p["text"]]
        with open(os.path.join(self.out, "search-titles.js"), "w") as f:
            f.write(js("GK_TITLES", titles))
        with open(os.path.join(self.out, "search-text.js"), "w") as f:
            f.write(js("GK_TEXT", texts))

    def write_recipe_data(self):
        """data.js: recipes plus icon/page lookups for the crafting cart."""
        pages = list(self.pages.values())
        sprites = recipe_parser.sprite_positions(pages)
        images = recipe_parser.image_icons(pages)
        names = sorted(self.products | {m for r in self.recipes for m, _ in r["materials"]})
        items = {}
        for n in names:
            entry = {}
            page = self.page_for(n)
            if page:
                entry["h"] = page_href(page)
            if n in sprites:
                entry["s"] = sprites[n]
            elif n in images:
                entry["i"] = self.local_image(images[n])[len("../"):]
            items[n] = entry
        rows = [[r["product"], r["qty"], [[m, q] for m, q in r["materials"]], r["stations"],
                 (1 if r["perk"] else 0) | (2 if r["building"] else 0)]
                + ([[[n, q] for n, q in r["also"]]] if r.get("also") else [])
                for r in self.recipes]
        data = json.dumps({"items": items, "recipes": rows}, ensure_ascii=False, separators=(",", ":"))
        with open(os.path.join(self.out, "data.js"), "w") as f:
            f.write("window.GK_DATA = " + data.replace("</", "<\\/") + ";\n")

    def write_cart_page(self):
        with open(os.path.join(self.out, "cart.html"), "w") as f:
            f.write(f"""{self.head("", "Crafting cart — Graveyard Keeper Wiki")}
{self.header("", "")}
<main class="cart-page">
<div class="title-row"><h1>Crafting cart</h1><button id="cart-clear" class="chip" hidden>Clear cart</button></div>
<div class="picker"><input id="cart-pick" type="search" autocomplete="off"
  placeholder="Add an item or building…"><div id="cart-suggest" hidden></div></div>
<ul id="cart-items"></ul>
<p id="cart-empty" class="note">The cart is empty. Add things with the search box above or with
the <b>+ Add to cart</b> button on any item or building page.</p>
<div id="cart-result" hidden>
<section><h2>Raw materials</h2><ul id="raw-list" class="raw-list"></ul></section>
<section><h2>Crafting steps <small class="note">grouped by product · numbered in crafting order</small></h2><ol id="steps-list" class="steps"></ol>
<p><button id="reset-choices" class="chip">Reset recipe choices</button></p>
<p class="note">Recipes are parsed from the wiki's tables; a few may be incomplete or need a perk.</p></section>
</div>
</main>
{self.footer()}
<script src="data.js" defer></script>
<script src="cart.js" defer></script>
</body></html>""")

    def write_pwa_files(self):
        manifest = {
            "name": "Graveyard Keeper Wiki", "short_name": "GK Wiki",
            "description": "Offline Graveyard Keeper wiki with a crafting planner",
            "start_url": "./index.html", "scope": "./", "display": "standalone",
            "background_color": "#1a1816", "theme_color": "#2a211d",
            "icons": [{"src": f"icons/icon-{n}.png", "sizes": f"{n}x{n}", "type": "image/png",
                       "purpose": "any maskable"} for n in (192, 512)],
            "shortcuts": [{"name": "Crafting cart", "url": "./cart.html"}],
        }
        with open(os.path.join(self.out, "manifest.webmanifest"), "w") as f:
            json.dump(manifest, f, indent=1)
        files = []
        for dirpath, dirnames, filenames in os.walk(self.out):
            dirnames[:] = [d for d in dirnames if d != "_cache"]
            for name in filenames:
                rel = os.path.relpath(os.path.join(dirpath, name), self.out)
                if rel in ("sw.js", "offline-files.json") or name.endswith(".part"):
                    continue
                files.append(urllib.parse.quote(rel.replace(os.sep, "/")))
        with open(os.path.join(self.out, "offline-files.json"), "w") as f:
            json.dump({"built": time.strftime("%Y-%m-%d %H:%M"), "files": sorted(files)}, f)

    def write_index(self):
        def listing(titles):
            return "".join(
                f'<li><a href="wiki/{html.escape(page_href(t), quote=True)}">'
                f'{html.escape(t.removeprefix("Category:"))}</a></li>'
                for t in sorted(titles, key=str.lower))
        # Wiki maintenance categories are noise for a reader.
        meta = re.compile(r"template|candidates|deletion|images?\b|icons?\b|stubs|hidden|cleanup|citation|"
                          r"documentation|infobox|logos|screenshots|legal|media|sprite|gifs|requested|"
                          r"needing|lacking|missing|wiki\b|main page", re.I)
        cats = [t for t in self.pages if t.startswith("Category:") and not meta.search(t)]
        articles = [t for t in self.pages if not t.startswith("Category:")]
        main = page_href("Graveyard Keeper")
        stamp = f'<span class="orig">offline copy · {time.strftime("%Y-%m-%d")}</span>'
        with open(os.path.join(self.out, "index.html"), "w") as f:
            f.write(f"""{self.head("", "Graveyard Keeper Wiki")}
{self.header("", stamp)}
<main>
<div class="home-links">
<a href="wiki/{main}" class="tile">📖 <b>Main page</b><span>{len(articles)} articles · {len(cats)} categories</span></a>
<a href="cart.html" class="tile">🛒 <b>Crafting cart</b><span>Plan builds, get the raw-material list</span></a>
</div>
<section class="offline-panel">
<div><b>Offline access</b> <span class="offline-status">Checking…</span></div>
<progress hidden></progress>
<button type="button">Download everything for offline</button>
</section>
<h2>Categories</h2><ul class="catlist">{listing(cats)}</ul>
<h2>All articles</h2><ul class="catlist">{listing(articles)}</ul>
<p class="note">Every page links to its original on Fandom.
Unofficial offline copy, not affiliated with Fandom or the game's developers.</p>
</main>
{self.footer()}
</body></html>""")

    def run(self, content_dir=None):
        updated = None
        if content_dir:
            updated = self.invalidate_changed(os.path.join(content_dir, "state.json"))
        titles = self.collect_titles()
        self.fetch_all(titles)
        self.fetch_wikitext(titles)
        self.drop_case_duplicates()
        self.recipes = recipe_parser.build(self.wikitext)
        self.products = {r["product"] for r in self.recipes}
        print(f"{len(self.recipes)} recipes for {len(self.products)} items")
        for p in sorted(self.products):
            page = self.page_for(p)
            if page:
                self.page_products.setdefault(page, []).append(p)
        self.write_pages()
        # Item icons are CSS sprites cut from one sheet (see MediaWiki:Common.css).
        self.images[SPRITE_SHEET] = "../images/ItemSprite.png"
        self.write_recipe_data()
        self.download_images()
        self.write_search_index()
        self.write_index()
        here = os.path.dirname(os.path.abspath(__file__))
        self.write_cart_page()
        for name in ("style.css", "search.js", "app.js", "cart.js", "sw.js"):
            shutil.copy(os.path.join(here, name), os.path.join(self.out, name))
        shutil.copytree(os.path.join(here, "icons"), os.path.join(self.out, "icons"), dirs_exist_ok=True)
        self.write_pwa_files()
        if content_dir:
            self.export_content(content_dir, updated)
        print(f"done → {os.path.join(self.out, 'index.html')}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="site")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--content", metavar="DIR",
                    help="incremental mode: refetch pages changed since DIR/state.json "
                         "and write a versionable wikitext snapshot to DIR")
    a = ap.parse_args()
    Archiver(a.out, a.workers, a.refresh).run(a.content)
