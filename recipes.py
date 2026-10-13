"""Extract crafting recipes from the wiki's wikitext.

Recipes on this wiki live in plain wikitables, e.g. on an item page:

    !colspan="3"|{{Item|Furnace}}          <- station header row
    |-
    |{{Item|Iron ingot|1}}                 <- product cell
    |{{Item|Iron ore|1}}                   <- materials cell
    |2:30<br>{{Fuel|-|25}}

and on structure pages a ==Cost== table whose row has an icon cell followed
by the materials needed to build the page's subject. Product rows prefixed
with "*" need a perk.
"""
import re

ITEM_RE = re.compile(r"\{\{\s*[Ii]tem\s*\|([^{}|]+?)(?:\|\s*([\d.,]+)\s*)?(?:\|[^{}]*)?\}\}")
LINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|([^\]]*))?\]\]")
UPGRADE_RE = re.compile(r"\[\[([^\]|#]+)\|\s*Upgrade to [^\]]*\]\]", re.I)
TOOL_RE = re.compile(r"pickaxe|\baxe\b|shovel|fishing rod|\bhoe\b", re.I)
COST_SECTIONS = re.compile(r"^(cost|costs|construction|building|build|price|requirements?|materials?|crafting cost)$", re.I)


def norm(name):
    name = re.sub(r"\s+", " ", name.replace("_", " ")).strip()
    return name[:1].upper() + name[1:]


def items_in(cell):
    out = []
    for m in ITEM_RE.finditer(cell):
        qty = m.group(2)
        try:
            qty = float(qty.replace(",", ".")) if qty else 1.0
        except ValueError:
            qty = 1.0
        out.append((norm(re.sub(r"^\s*1\s*=", "", m.group(1))), qty))
    return out


def split_tables(text):
    """Yield (section_heading, table_text) for every top-level wikitable."""
    heading, depth, start = "", 0, None
    lines = text.split("\n")
    buf = []
    for line in lines:
        s = line.strip()
        if depth == 0:
            h = re.match(r"^(=+)\s*(.*?)\s*\1\s*$", s)
            if h:
                heading = h.group(2)
        if s.startswith("{|"):
            depth += 1
            if depth == 1:
                buf = []
        if depth:
            buf.append(line)
        if s.startswith("|}") and depth:
            depth -= 1
            if depth == 0:
                yield heading, "\n".join(buf)


def parse_rows(table):
    """Return rows as lists of (is_header, cell_text); nested tables are kept in cells."""
    rows, row, cell, depth = [], [], None, 0

    def flush_cell():
        nonlocal cell
        if cell is not None:
            row.append(cell)
        cell = None

    for line in table.split("\n")[1:]:
        s = line.strip()
        if depth:
            cell = (cell[0], cell[1] + "\n" + line)
            if s.startswith("{|"):
                depth += 1
            elif s.startswith("|}"):
                depth -= 1
            continue
        if s.startswith("{|"):
            depth += 1
            cell = (cell[0], cell[1] + "\n" + line) if cell else (False, line)
            continue
        if s.startswith("|}"):
            break
        if s.startswith("|-"):
            flush_cell()
            if row:
                rows.append(row)
            row = []
            continue
        if s.startswith("|+"):
            continue
        if s.startswith("!") or s.startswith("|"):
            flush_cell()
            header = s.startswith("!")
            parts = re.split(r"!!|\|\|", s[1:]) if header else s[1:].split("||")
            for i, part in enumerate(parts):
                # strip cell attributes: 'style="..."|content' (but not template pipes)
                m = re.match(r'^\s*((?:[\w-]+\s*=\s*(?:"[^"]*"|\'[^\']*\'|[^\s|]+)\s*)+)\|(?!\|)(.*)$', part)
                content = m.group(2) if m else part
                if i < len(parts) - 1:
                    row.append((header, content))
                else:
                    cell = (header, content)
            continue
        if cell is not None:
            cell = (cell[0], cell[1] + "\n" + line)
    flush_cell()
    if row:
        rows.append(row)
    return rows


def station_of(cell):
    items = items_in(cell)
    if items:
        return " / ".join(n for n, _ in items)
    links = [norm(m.group(1)) for m in LINK_RE.finditer(cell)
             if not m.group(1).lower().startswith(("file:", "image:", "category:"))]
    return " / ".join(links)


def extract(title, text, in_place=None):
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = re.sub(r"\{\{\s*(?:lc:|ucfirst:)?\s*\{\{\s*PAGENAME\s*\}\}\s*\}\}", title, text, flags=re.I)
    text = re.sub(r"\{\{\s*PAGENAME\s*\}\}", title, text, flags=re.I)
    recipes = []
    is_item_page = re.search(r"\{\{\s*Item Infobox", text, re.I) is not None
    is_station_page = re.search(r"\{\{\s*Workstation-Infobox", text, re.I) is not None
    for heading, table in split_tables(text):
        rows = parse_rows(table)
        station = ""
        cost_table = bool(COST_SECTIONS.match(heading.strip()))
        cost_rows = 0
        for row in rows:
            data = [c for h, c in row if not h]
            if all(h for h, _ in row):
                # A lone header cell naming a workstation sets the station for following rows.
                if len(row) == 1:
                    st = station_of(row[0][1])
                    if st:
                        station = st
                continue
            if len(data) < 2:
                continue
            first, second = data[0], data[1]
            products = items_in(first)
            materials = items_in(second)
            perk = first.lstrip().startswith("*")
            if products and materials and len(products) == 1:
                name, qty = products[0]
                if any(m == name for m, _ in materials):
                    # e.g. embalming: Corpse + injection -> Corpse. The item is
                    # modified, not made, so it should stay a raw material.
                    if in_place is not None:
                        in_place.add(name)
                    continue
                recipes.append(dict(product=name, qty=qty, materials=materials,
                                    station=station or (title if is_station_page else ""),
                                    perk=perk, source=title))
                continue
            up = UPGRADE_RE.search(first)
            if up and materials:
                # Upgrading consumes the current structure.
                recipes.append(dict(product=norm(up.group(1)), qty=1.0,
                                    materials=[(norm(title), 1.0)] + materials,
                                    station=title, perk=False, source=title, building=True))
                continue
            if (cost_table and not is_item_page and not products and materials and cost_rows == 0
                    and all(m != norm(title) for m, _ in materials)):
                # Structure cost: icon cell + materials for the page's own subject.
                cost_rows += 1
                recipes.append(dict(product=norm(title), qty=1.0, materials=materials,
                                    station="Build", perk=False, source=title, building=True))
    return recipes


def build(wikitext):
    """Merge recipes found on all pages.

    The same recipe is usually listed on the product page, the workstation page
    and some guides; those are merged into one entry with the union of stations.
    Recipes from the product's own page come first, in page order (that is the
    wiki's tier order, e.g. Wooden anvil, Anvil, Anvil II).
    """
    merged, in_place = {}, set()
    pages = sorted(wikitext.items(), key=lambda kv: kv[0])
    extracted = [(title, extract(title, text, in_place)) for title, text in pages
                 if not title.startswith("Category:")]
    for title, found in extracted:
        for i, r in enumerate(found):
            if r["product"] in in_place and not r.get("building"):
                continue
            # "*" marks both perk-only recipes and perk bonuses, so a recipe also
            # listed without "*" elsewhere is treated as not needing the perk.
            mats = list(dict.fromkeys(r["materials"]))  # drop repeated cells
            if not r["station"] and any(TOOL_RE.search(m) for m, _ in mats):
                continue  # gathering with a tool, not a recipe
            r["materials"] = mats
            key = (r["product"], r["qty"], tuple(sorted(r["materials"])))
            stations = [s for s in r["station"].split(" / ") if s]
            rank = (0 if r["source"] == r["product"] else 1, i)
            if key in merged:
                m = merged[key]
                for s in stations:
                    if s not in m["stations"]:
                        m["stations"].append(s)
                m["building"] = m["building"] or r.get("building", False)
                m["perk"] = m["perk"] and r["perk"]
                m["rank"] = min(m["rank"], rank)
                continue
            merged[key] = dict(product=r["product"], qty=r["qty"], materials=r["materials"],
                               stations=stations, perk=r["perk"],
                               building=r.get("building", False), rank=rank)
    # Default recipe = first one: no perk, known station, product's own page order.
    recipes = sorted(merged.values(),
                     key=lambda r: (r["product"], r["perk"], not r["stations"], r["rank"]))
    for r in recipes:
        del r["rank"]
    return recipes


SPRITE_RE = re.compile(r'<a href="/wiki/[^"]*" title="([^"]+)"[^>]*>\s*<span class="sprite item-sprite" '
                       r'style="background-position:\s*(-?\d+)px\s+(-?\d+)px')


IMG_ICON_RE = re.compile(r'<a href="/wiki/[^"]*" title="([^"]+)"[^>]*>\s*<img [^>]*?'
                         r'(?:data-src|src)="(https://static\.wikia\.nocookie\.net/[^"]+)"[^>]*'
                         r'width="(\d+)"')


def image_icons(parsed_pages):
    """Item name -> small image URL, for items drawn with a file instead of a sprite."""
    icons = {}
    for p in parsed_pages:
        for name, url, width in IMG_ICON_RE.findall(p.get("text", "")):
            if int(width) <= 48:
                icons.setdefault(norm(name), url)
    return icons


def sprite_positions(parsed_pages):
    """Item name -> (x, y) offset in ItemSprite.png, read from rendered pages."""
    icons = {}
    for p in parsed_pages:
        for name, x, y in SPRITE_RE.findall(p.get("text", "")):
            icons.setdefault(norm(name), (int(x), int(y)))
    return icons
