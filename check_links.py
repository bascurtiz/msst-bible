#!/usr/bin/env python3
"""check_links.py — fail a build that would ship broken links or markup.

The site is regenerated from a Google Doc that nobody edits through this repo,
so a renamed heading, a deleted section or a retitled page silently breaks the
mirror's cross-references and `#heading` anchors. This walks a generated site
and verifies that

  * every internal `href`/`src` resolves to a file in the output,
  * every `#fragment` has a matching `id` on the page it points at,
  * the slugs in `data.json` (client-side search), the forwards in
    `anchors.json` (links minted before every heading became its own page) and
    the `<loc>`s in `sitemap.xml` point at real pages,
  * no page repeats an `id` (a duplicate silently redirects every `#anchor`
    to the first copy) and no page leaves a tag unclosed (a stray `</div>`
    can hide the rest of a page from the browser).

Exit code 0 when everything checks out, 1 with a report otherwise, so wiring
it into the deploy makes a broken anchor fail the build instead of shipping.

Usage:
  python check_links.py --dir _site
"""

from __future__ import annotations

import argparse
import collections
import html
import json
import os
import re
import sys
import urllib.parse
from html.parser import HTMLParser

LOC_RE = re.compile(r"<loc>(.*?)</loc>", re.I | re.S)
REMOTE_PREFIXES = ("http:", "https:", "mailto:", "tel:", "javascript:", "data:")

# elements that never have an end tag
VOID = frozenset(("area", "base", "br", "col", "embed", "hr", "img", "input",
                  "link", "meta", "param", "source", "track", "wbr"))

# elements whose *end* tag the HTML spec lets a document omit; a page may
# close them implicitly (a new <li>, an enclosing </ul>, end of document)
OPTIONAL_END = frozenset(("html", "head", "body", "p", "li", "dt", "dd",
                          "option", "optgroup", "thead", "tbody", "tfoot",
                          "tr", "td", "th", "colgroup", "rt", "rp"))

LINK_ATTRS = {"a": "href", "area": "href", "link": "href",
              "img": "src", "script": "src", "iframe": "src", "source": "src"}

# block-level elements: starting one closes an open <p> implicitly
P_CLOSERS = frozenset(("address", "article", "aside", "blockquote",
                       "details", "div", "dl", "fieldset", "figcaption",
                       "figure", "footer", "form", "h1", "h2", "h3",
                       "h4", "h5", "h6", "header", "hr", "main", "menu",
                       "nav", "ol", "p", "pre", "section", "table",
                       "ul"))


class PageScan(HTMLParser):
    """One pass over a page: its ids, its links and its tag balance.

    Uses the real parser (not regexes) so attribute values containing `>`,
    comments and inline script/style text can't be mistaken for markup.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.ids = collections.Counter()
        self.links = []
        self.issues = []
        self.stack = []

    # -- markup ------------------------------------------------------------

    def _record(self, tag, attrs):
        a = dict(attrs)
        if a.get("id"):
            self.ids[a["id"]] += 1
        want = LINK_ATTRS.get(tag)
        if want and a.get(want):
            self.links.append(a[want])

    def handle_starttag(self, tag, attrs):
        self._record(tag, attrs)
        if tag in VOID:
            return
        if tag in P_CLOSERS:
            while self.stack and self.stack[-1] == "p":
                self.stack.pop()  # implied </p>
        elif tag in OPTIONAL_END and self.stack and self.stack[-1] == tag:
            self.stack.pop()  # a new <li>/<td>/… closes the previous one
        self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        self._record(tag, attrs)  # <br/>, <img/>, … open and close at once

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        if tag not in self.stack:
            self.issues.append(f"stray </{tag}> with nothing open")
            return
        while self.stack[-1] != tag:
            inner = self.stack.pop()
            if inner not in OPTIONAL_END:
                self.issues.append(f"<{inner}> left unclosed before </{tag}>")
        self.stack.pop()

    # -- results -----------------------------------------------------------

    def finish(self):
        left = [t for t in self.stack if t not in OPTIONAL_END]
        if left:
            self.issues.append("never closed: " + ", ".join(
                f"<{t}>" for t in reversed(left)))
        return self


def classify(href):
    """(path, fragment, kind) — kind is 'link', 'external' or 'empty'."""
    href = html.unescape(href or "").strip()
    if not href:
        return "", "", "empty"
    low = href.lower()
    if low.startswith("//") or any(low.startswith(p) for p in REMOTE_PREFIXES):
        return "", "", "external"
    u = urllib.parse.urlsplit(href)
    if u.scheme:  # any other scheme (ftp:, sftp:, …) is not ours to check
        return "", "", "external"
    return u.path, u.fragment, "link"


def resolve(path, page, files):
    """Site-relative output path a href points at, or None when it is missing.

    `page` is the site-relative path of the file doing the linking, so relative
    hrefs resolve the way a browser resolves them. Extension-less links are
    allowed because the host serves `/de-reverb` from `de-reverb.html`.
    """
    if not path:
        return page  # href="#anchor" or href="?query": the page itself
    if path.startswith("/"):
        rel = path.lstrip("/")
    else:
        rel = os.path.join(os.path.dirname(page), path)
    rel = os.path.normpath(rel).replace(os.sep, "/")
    if rel in ("", "."):  # "/" and "./": this page's directory index
        rel = os.path.join(os.path.dirname(page), "index.html").lstrip("/")
    if rel in files:
        return rel
    if rel.rstrip("/") + "/index.html" in files:
        return rel.rstrip("/") + "/index.html"
    if not path.endswith("/") and rel + ".html" in files:
        return rel + ".html"
    return None


def scan_pages(files, pages, problems):
    """Parse every page once: collect its ids and links, check its markup."""
    ids, links = {}, {}
    for page in sorted(pages):
        with open(files[page], encoding="utf-8", errors="replace") as f:
            scan = PageScan()
            scan.feed(f.read())
            scan.close()
            scan.finish()
        ids[page] = set(scan.ids)
        links[page] = scan.links
        for hid in sorted(k for k, n in scan.ids.items() if n > 1):
            problems["duplicate id"].append(
                f"{page}: id=\"{hid}\" appears {scan.ids[hid]} times")
        for issue in scan.issues:
            problems["unclosed tag"].append(f"{page}: {issue}")
    return ids, links


def check_targets(files, links, ids, problems):
    """Every link must resolve, and its #fragment must exist on the target."""
    checked = 0
    for page in sorted(links):
        for href in links[page]:
            path, frag, kind = classify(href)
            if kind == "external":
                continue
            if kind == "empty":
                problems["empty link"].append(page)
                continue
            checked += 1
            target = resolve(path, page, files)
            if target is None:
                problems["missing file"].append(f"{page} -> {href}")
            elif frag and frag not in ids.get(target, set()):
                problems["missing anchor"].append(f"{page} -> {href}")
    return checked


def check_json(root, files, ids, problems):
    """data.json (search) and anchors.json (old-link forwards) point at pages."""
    checked = 0
    dj = os.path.join(root, "data.json")
    if os.path.exists(dj):
        with open(dj, encoding="utf-8") as f:
            data = json.load(f)
        for sec in data.get("sections", []):
            checked += 1
            slug = sec.get("slug") or ""
            if slug + ".html" not in files:
                problems["missing file"].append(f"data.json -> {slug}.html")
    aj = os.path.join(root, "anchors.json")
    if os.path.exists(aj):
        with open(aj, encoding="utf-8") as f:
            anchors = json.load(f)
        for hid, hit in anchors.items():
            checked += 1
            href = (hit or {}).get("href") or ""
            path, frag, _kind = classify(href)
            target = resolve(path, "index.html", files)
            if target is None:
                problems["missing file"].append(f"anchors.json[{hid}] -> {href}")
            elif frag and frag not in ids.get(target, set()):
                problems["missing anchor"].append(f"anchors.json[{hid}] -> {href}")
    return checked


def check_sitemap(root, files, problems):
    """Every sitemap URL must be a page we actually wrote."""
    path = os.path.join(root, "sitemap.xml")
    if not os.path.exists(path):
        return 0
    with open(path, encoding="utf-8") as f:
        locs = LOC_RE.findall(f.read())
    checked = 0
    for loc in locs:
        url = urllib.parse.urlsplit(html.unescape(loc.strip()))
        if not url.path:
            continue
        checked += 1
        if resolve(url.path, "index.html", files) is None:
            problems["missing file"].append(f"sitemap.xml -> {loc.strip()}")
    return checked


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Check that every internal link, anchor, id and tag in a "
                    "generated site holds together.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", default="_site",
                    help="generated site directory (default: _site)")
    ap.add_argument("--limit", type=int, default=15,
                    help="examples to print per problem kind (default: 15)")
    args = ap.parse_args(argv)

    root = args.dir
    if not os.path.isdir(root):
        sys.stderr.write(f"check_links: not a directory: {root}\n")
        return 2

    files = {}
    for dirpath, _dirs, names in os.walk(root):
        for name in names:
            full = os.path.join(dirpath, name)
            files[os.path.relpath(full, root).replace(os.sep, "/")] = full
    pages = [p for p in files if p.endswith((".html", ".htm"))]
    if not pages:
        sys.stderr.write(f"check_links: no HTML pages in {root}\n")
        return 2

    problems = {"missing file": [], "missing anchor": [], "empty link": [],
                "duplicate id": [], "unclosed tag": []}
    ids, links = scan_pages(files, pages, problems)
    checked = check_targets(files, links, ids, problems)
    checked += check_json(root, files, ids, problems)
    checked += check_sitemap(root, files, problems)

    total = sum(len(v) for v in problems.values())
    if total:
        print(f"link check FAILED: {total} problem(s) across {len(pages)} "
              f"pages in {root}")
        for kind, items in problems.items():
            if not items:
                continue
            print(f"\n{len(items)} {kind}:")
            for line in items[:args.limit]:
                print("  " + line)
            if len(items) > args.limit:
                print(f"  ... and {len(items) - args.limit} more")
        return 1

    print(f"link check OK: {checked} internal links/anchors across "
          f"{len(pages)} pages - ids unique, markup balanced  [{root}]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
