#!/usr/bin/env python3
"""gdoc_site.py — turn a big Google Doc into a fast, tiny static site.

The Google Doc stays the source of truth. This script pulls it and renders a
static site with one page per heading — nested the way the document outlines
them, so every heading has a titled URL of its own (`/de-reverb`) — plus a
sidebar table of contents (grouped by document tab), client-side search,
working internal links and "edit this section" links back to Google Docs.

Sources
-------
api      Google Docs API via OAuth (preserves document tabs). Default when
         auth.json exists. Uses documents.get with includeTabsContent=true.
         One-time setup: python gdoc_site.py --auth --client-json <file>.
         (Google no longer accepts API keys for the Docs API.)
export   Public HTML export (no auth; tabs are flattened into one continuous
         document). Default when auth.json is absent.
file     Read a saved documents.get JSON response (handy for testing).
         Use --file path.

Usage
-----
  python gdoc_site.py --auth --client-json client_secret_XXXX.json   # once
  python gdoc_site.py --doc DOC_ID [--source api] [--out site]      # tabs
  python gdoc_site.py --doc DOC_ID [--out site]                     # export
  python gdoc_site.py --doc DOC_ID --source file --file resp.json

Only the Python standard library is used.
"""

import argparse
import html as htmlmod
import json
import os
import re
import shutil
import sys
import time
import urllib.parse
import urllib.request
from html.parser import HTMLParser

# Google Search Console verification. Add a URL-prefix property for
# https://msst-bible.pages.dev/ and paste the "HTML tag" content value here
# (NOT the DNS TXT value — pages.dev DNS is owned by Cloudflare, so the DNS
# method can't be used). Empty = no tag emitted.
GSC_VERIFICATION = ""

DOCS_API = "https://docs.googleapis.com/v1/documents/{doc}?includeTabsContent=true"
EXPORT_URL = "https://docs.google.com/document/d/{doc}/export?format=html"
USER_AGENT = "Mozilla/5.0 (gdoc-site mirror generator)"
# Default site name when no --title override is given (used for <title>, brand,
# tabs, RSS/sitemap). Forks can override with gdoc_site.py --title "...".
SITE_TITLE = "MSST Bible"

# --- OAuth 2.0 (Google no longer accepts API keys for the Docs API) ---------
TOKEN_URL = "https://oauth2.googleapis.com/token"
OAUTH_SCOPE = "https://www.googleapis.com/auth/documents.readonly"
AUTH_URL = (
    "https://accounts.google.com/o/oauth2/v2/auth"
    "?client_id={client_id}&redirect_uri={redirect_uri}"
    "&response_type=code&scope={scope}"
    "&access_type=offline&prompt=consent"
    "&code_challenge={challenge}&code_challenge_method=S256")

# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def esc(s):
    """Escape text for HTML body content."""
    return htmlmod.escape(s or "", quote=False)


def attr(s):
    """Escape text for an HTML attribute value."""
    return htmlmod.escape(s or "", quote=True)


def text_of_runs(runs):
    parts = []
    for r in runs:
        if r.get("br"):
            parts.append(" ")
        elif r.get("img"):
            parts.append(" [image] ")
        else:
            parts.append(r.get("text", ""))
    return re.sub(r"\s+", " ", "".join(parts)).strip()


def is_decorative_heading(t):
    t = t.strip()
    if not t:
        return True
    if re.fullmatch(r"[\s\-–—_=~*·•.]+", t):
        return True
    return False


# Leading marker characters some doc headings begin with ("- ", "> ", ".",
# "- For…", etc.) in the source doc. They're not part of the real tab title,
# so strip them from the label shown in the navigation outline/sidebar.
_NAV_MARK = re.compile(r"^[\s>#*•·\-\u2013\u2014\.]+")


def clean_nav_title(t):
    t = (t or "").strip()
    return _NAV_MARK.sub("", t).strip()


# Sub-headings to hide from the navigation outline (the document-tabs index)
# even though they remain real headings in the page body (their anchors and
# content still render). The doc author flagged these as noise.
OUTLINE_EXCLUDE = {
    "h.rbip7cu8qym5", "h.zgs4nmt4n7oi", "h.7d0taha5j2l4", "h.7hyw89vmnqto",
    "h.snva8th7p3bn", "h.en3r3zxljk3w", "h.7ln6u4wu5csz", "h.g6guxr3z230p",
    "h.nujhjtjwtvpb", "h.941m71ob400", "h.k3vca4e9ena8-1", "h.tvbntqdvkn9n",
    "h.jx9um5zd7fnp", "h.hkry3b4x7kv0",
    # Second round of doc-author-flagged noise (news/model-list clutter):
    "h.mha9xrfqx84j", "h.wxjfq1gfq37b", "h.ych3p8fftzi0", "h.v4wq54do0m30",
    "h.85xth1o1xa0p", "h.9una7hhstsnk", "h.c25tza6ak9pi", "h.toae4851qt3d",
    "h.vktvthhthrvh", "h.euyv55qdbx07", "h.ynukuzsi11zf", "h.pjvi1qowv8cq",
}

# Headings whose original leading marker characters are meaningful and must
# stay visible in the outline (normally markers like "- ", "> " are stripped).
KEEP_RAW_HEADINGS = {"h.929g1wjjaxz7"}


def nav_title(t, hid=None):
    """Navigation label: keep raw markers only for the KEEP_RAW_HEADINGS ids."""
    if hid in KEEP_RAW_HEADINGS:
        return (t or "").strip()
    return clean_nav_title(t)


def slugify(t):
    t = t.lower()
    t = re.sub(r"[^\w\s-]", "", t, flags=re.UNICODE)
    t = re.sub(r"[\s_]+", "-", t).strip("-")
    return t or "section"


def unique_slug(base, used, max_len=60):
    base = base[:max_len].rstrip("-")
    slug = base
    i = 2
    while slug in used:
        suffix = f"-{i}"
        slug = base[: max_len - len(suffix)].rstrip("-") + suffix
        i += 1
    used.add(slug)
    return slug


# Names the generator writes itself. A heading that slugifies to one of these
# gets a "-2" suffix instead of clobbering that page (`index`, `contents`,
# `404`) or shadowing the asset folder (`assets`).
RESERVED_SLUGS = frozenset(("index", "contents", "404", "assets"))


# The doc author renames the news section in place every day (`edit. DD.MM.YY`),
# so a slug taken from that heading would move the page's URL every morning and
# churn its sitemap entry. Such a section keeps this stable slug instead: the
# date stays in the visible title, where readers expect it, and links minted to
# an older dated URL are forwarded to it by 404.html.
#
# Such a section is recognised by the slug its heading would have got: `edit.`,
# `Edit–`, `edit ` and so on all reduce to `edit-<digits>`.
NEWS_SLUG = "news"
DATED_SLUG_RE = re.compile(r"^edit-\d+$")


# ---------------------------------------------------------------------------
# link resolution
# ---------------------------------------------------------------------------

def unwrap_google_url(href):
    """Google's HTML export wraps external links in a /url?q= redirect."""
    u = urllib.parse.urlsplit(href)
    if u.netloc in ("www.google.com", "google.com") and u.path in ("/url", "/url/"):
        q = urllib.parse.parse_qs(u.query)
        if "q" in q:
            return q["q"][0]
    return href


# Two hyperlinks sitting on one line in the source doc sometimes get merged
# into one URL, glued with the marker string "%0A(src)%20" (an encoded
# newline + "(src)" + space, e.g. a Drive-folder link fusing with a Discord
# link from "Download (ref)"). Everything from the marker on belongs to the
# second link, so drop it and keep only the first URL.
_MERGED_LINK_RE = re.compile(r"(?:%0A|\n)\(src\)(?:%20| )", re.IGNORECASE)


def clean_merged_url(url):
    """Split links fused by the '%0A(src)%20' marker: keep the first URL."""
    if not url:
        return url
    cleaned = _MERGED_LINK_RE.split(url, maxsplit=1)[0]
    return cleaned.rstrip("\\") if cleaned != url else url


def raw_link_from_href(href):
    """Turn an href from the export HTML into a raw link descriptor."""
    if not href:
        return None
    href = href.strip()
    if href.startswith("#"):
        return ("heading", href[1:], None)
    if href.startswith("?"):
        q, _, frag = href.partition("#")
        params = urllib.parse.parse_qs(q.lstrip("?"))
        tab = (params.get("tab") or [None])[0]
        if frag.startswith("heading="):
            return ("heading", frag.split("=", 1)[1], tab)
        if tab:
            return ("tab", tab)
        return None
    return ("url", clean_merged_url(unwrap_google_url(href)))


def parse_api_link(link):
    """Turn a Link object from the Docs API into a raw link descriptor."""
    if "heading" in link:
        h = link["heading"]
        return ("heading", h.get("id"), h.get("tabId"))
    if "headingId" in link:
        return ("heading", link["headingId"], None)
    if "bookmark" in link:
        return ("bookmark", link["bookmark"].get("tabId"))
    if "bookmarkId" in link:
        return ("bookmark", None)
    if "tabId" in link:
        return ("tab", link["tabId"])
    if link.get("url"):
        return ("url", clean_merged_url(link["url"]))
    return None


def build_subs(section):
    """Headings that live *inside* a page: everything except the page's own
    title heading.

    Every real heading is its own page now, so the only headings left in a
    page body are the ones the doc author hid from the outline
    (OUTLINE_EXCLUDE) and spacer/separator headings. They are listed here so
    their anchors stay known and so the search index can offer them as
    in-page sub-heading hints. A heading whose content is body-length prose is
    demoted to a paragraph during normalization and is intentionally not
    listed — matching Google's outline, which lists the document's actual
    section titles but not the prose typed into heading styles."""
    subs = []
    for b in section["blocks"]:
        if b["type"] != "heading":
            continue
        if b.get("heading_id") and b["heading_id"] == section.get("heading_id"):
            continue  # the page's own title heading (front matter may precede it)
        subs.append({"level": b["level"], "id": b.get("heading_id"),
                     "title": text_of_runs(b["runs"])})
    return subs


class Site:
    """Holds the parsed document and knows how to resolve links."""

    def __init__(self, doc_id, title, tabs, source):
        self.doc_id = doc_id
        self.title = title
        self.source = source
        self.tabs = [t for t in tabs if t.get("blocks")]
        self.sections = []
        self.heading_page = {}      # heading id -> (slug, fragment or None)
        self.heading_tab_page = {}  # (tab id, heading id) -> (slug, fragment)
        self.tab_first = {}         # tab id -> slug of first section
        self.generated = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
        self._split_and_slug()

    # -- structure ----------------------------------------------------------

    @staticmethod
    def _owns_page(b):
        """True when this heading becomes a page of its own.

        Every real heading does — that is what gives each outline entry a
        titled URL. Two kinds of heading stay inline in their parent page and
        keep only an anchor: spacer/separator headings, and the ones the doc
        author flagged as outline noise (OUTLINE_EXCLUDE).
        """
        if b["type"] != "heading":
            return False
        if b.get("heading_id") in OUTLINE_EXCLUDE:
            return False
        return not is_decorative_heading(text_of_runs(b["runs"]))

    def _split_and_slug(self):
        """Cut the document into one page per heading.

        Levels nest: a page's parent is the nearest preceding heading with a
        smaller level, so the site mirrors the doc's outline and every heading
        gets a titled URL of its own. The blocks between a heading and the next
        heading belong to that heading's page, so text written above a deeper
        heading stays with its nearest ancestor — which keeps the document's
        opening (its front matter and the news section's intro) on the front
        page instead of stranding it on a sub-page.
        """
        # names of pages/files the site itself writes: a heading that slugifies
        # to one of these must not take the name over (`index`, `contents` and
        # `404` would be overwritten, `assets` would shadow the asset folder)
        used = set(RESERVED_SLUGS)
        for tab in self.tabs:
            stack = []      # [(level, section)] — the open ancestor pages
            preamble = []   # blocks before this tab's first heading
            cur = None
            for b in tab["blocks"]:
                if not self._owns_page(b):
                    if cur is None:
                        preamble.append(b)
                    else:
                        cur["blocks"].append(b)
                    continue
                level = b["level"]
                title = text_of_runs(b["runs"])
                while stack and stack[-1][0] >= level:
                    stack.pop()
                parent = stack[-1][1] if stack else None
                # a dated news heading (`edit. 23.09.26`) keeps a stable slug
                # rather than one that changes with the date in its title
                slug_base = slugify(title)
                dated_news = bool(DATED_SLUG_RE.match(slug_base))
                if dated_news:
                    slug_base = NEWS_SLUG
                sec = {
                    "tab": tab["id"], "tab_title": tab["title"],
                    "title": title, "heading_id": b.get("heading_id"),
                    "level": level, "blocks": [b], "subs": [],
                    "parent": parent["slug"] if parent else None,
                    "depth": max(level - 2, 0),
                    "slug": unique_slug(slug_base, used),
                    "dated_news": dated_news,
                }
                self.sections.append(sec)
                stack.append((level, sec))
                if tab["id"] not in self.tab_first:
                    self.tab_first[tab["id"]] = sec["slug"]
                cur = sec
            if preamble:
                # A document that opens with a Title/Subtitle line (its front
                # matter) must not grow a near-empty front page titled after
                # the site: the preamble joins the tab's first real page.
                first = next((s for s in self.sections
                              if s["tab"] == tab["id"]), None)
                if first is None:  # a tab holding no headings at all
                    first = {
                        "tab": tab["id"], "tab_title": tab["title"],
                        "title": tab["title"], "heading_id": None,
                        "level": 0, "blocks": [], "subs": [], "parent": None,
                        "depth": 0,
                        "slug": unique_slug(slugify(tab["title"]), used),
                    }
                    self.sections.append(first)
                    self.tab_first.setdefault(tab["id"], first["slug"])
                first["blocks"] = preamble + first["blocks"]

        # heading id -> (page slug, fragment), for internal links. The fragment
        # is None for the heading that titles its own page, so links to it use
        # the page's titled URL; headings that stay inline in a page body keep
        # their anchor. Insertion order (document order) decides collisions.
        for s in self.sections:
            s["subs"] = build_subs(s)
            hid = s.get("heading_id")
            if hid:
                self.heading_page.setdefault(hid, (s["slug"], None))
                self.heading_tab_page.setdefault((s["tab"], hid),
                                                (s["slug"], None))
            for b in s["blocks"]:
                anchor = b.get("heading_id")
                if anchor:
                    self.heading_page.setdefault(anchor, (s["slug"], anchor))
                    self.heading_tab_page.setdefault((s["tab"], anchor),
                                                     (s["slug"], anchor))

    # -- link resolution ----------------------------------------------------

    def heading_href(self, hid, tab=None):
        """Link to a heading: its own titled page, or the page that holds it
        plus its anchor when the heading stays inline in a page body."""
        hit = None
        if tab:
            hit = self.heading_tab_page.get((tab, hid))
        if not hit:
            hit = self.heading_page.get(hid)
        if not hit:
            return None
        slug, frag = hit
        return toc_href(home_section_slug(self), slug, frag)

    def tab_url(self, tab):
        slug = self.tab_first.get(tab)
        if slug:
            # the tab holding the front section is served from the site root
            return toc_href(home_section_slug(self), slug)
        return "/"

    def pretty_own_page(self, u):
        """Rewrite a hand-written link to one of the mirror's own pages into
        the extension-less URL Cloudflare serves (`…/index.html` -> `/`).

        The doc's author pastes the mirror's addresses into the doc, so they
        arrive with `.html`, which the host 308-redirects — dropping the
        suffix here saves every reader that hop. Links to other sites, and
        paths we don't generate, are left exactly as written.
        """
        if u.netloc:
            base = urllib.parse.urlsplit(getattr(self, "base_url", "") or "")
            if not base.netloc or u.netloc.lower() != base.netloc.lower():
                return None  # somebody else's site: not ours to rewrite
        if not u.path.endswith(".html"):
            return None
        slug = u.path.rsplit("/", 1)[-1][:-len(".html")]
        if slug == "index":
            path = "/"
        elif slug == "contents" or any(s["slug"] == slug
                                       for s in self.sections):
            path = "/" + slug
        elif DATED_SLUG_RE.match(slug):
            # a link the doc still carries to an older daily news URL: the
            # section keeps the stable slug now, so point it there
            news = news_section_slug(self)
            if news is None:
                return None
            home = home_section_slug(self)
            path = "/" if news == home else "/" + news
        else:
            return None
        if u.query:
            path += "?" + u.query
        if u.fragment:
            path += "#" + u.fragment
        return path

    def resolve_url(self, url):
        url = clean_merged_url(url)
        u = urllib.parse.urlsplit(url)
        if u.scheme not in ("http", "https"):
            return self.pretty_own_page(u) or url
        if u.netloc in ("docs.google.com", "docs.googleusercontent.com") \
                and u.path.startswith("/document/d/"):
            parts = u.path.split("/")
            if len(parts) >= 4 and parts[3] == self.doc_id:
                q = urllib.parse.parse_qs(u.query)
                tab = (q.get("tab") or [None])[0]
                frag = u.fragment
                if frag.startswith("heading="):
                    return (self.heading_href(frag.split("=", 1)[1], tab)
                            or "/")
                if tab:
                    return self.tab_url(tab)
                return "/"
        return self.pretty_own_page(u) or url

    def resolve_run(self, raw):
        if raw is None:
            return None
        kind = raw[0]
        if kind == "heading":
            return self.heading_href(raw[1], raw[2]) or "/"
        if kind == "bookmark":
            return self.tab_url(raw[1]) if raw[1] else "/"
        if kind == "tab":
            return self.tab_url(raw[1])
        if kind == "url":
            return self.resolve_url(raw[1])
        return None

    def _norm_title(self, t):
        return re.sub(r"\s+", " ", t or "").strip().lower()

    def _build_section_title_index(self):
        # heading title -> (slug, fragment) lazily, keyed on normalized text
        if getattr(self, "_title_index", None) is not None:
            return self._title_index
        idx = {}
        for s in self.sections:
            # register the page itself under its title first, so a title that
            # is both a page and an inline heading resolves to the page
            idx.setdefault(self._norm_title(s["title"]), (s["slug"], None))
            # inline headings and demoted prose headings keep their anchors
            for b in s["blocks"]:
                if b["type"] not in ("heading", "para"):
                    continue
                if b.get("heading_id"):
                    idx.setdefault(self._norm_title(
                        text_of_runs(b.get("runs", []))),
                        (s["slug"], b["heading_id"]))
        self._title_index = idx
        return idx

    def resolve_stale_heading(self, hid, text):
        """A heading link points to an id that no longer exists (the doc's
        manually-maintained TOC is full of such stale links). Fall back to
        the registered heading whose title matches the link's text, so the
        entry still lands on the right section instead of the document top.
        """
        q = self._norm_title(text)
        if not q:
            return None
        idx = self._build_section_title_index()
        # exact title match first, then a generous prefix match
        hit = idx.get(q)
        if hit is None and len(q) >= 15:
            for k, v in idx.items():
                if k[:len(q)] == q or q[:len(k)] == k:
                    hit = v
                    break
        if not hit:
            return None
        slug, frag = hit
        return toc_href(home_section_slug(self), slug, frag)

    def edit_url(self, section):
        tab = section["tab"]
        hid = section.get("heading_id")
        base = f"https://docs.google.com/document/d/{self.doc_id}/edit?tab={tab}"
        if hid:
            base += f"#heading={hid}"
        return base

    # -- rendering ----------------------------------------------------------

    def render_runs(self, runs):
        out = []
        for r in runs:
            if r.get("br"):
                out.append("<br>")
                continue
            if r.get("img"):
                out.append(f'<img src="{attr(r["img"])}" alt="" loading="lazy">')
                continue
            t = esc(r.get("text", ""))
            if not t:
                continue
            if r.get("code"):
                t = f"<code>{t}</code>"
            else:
                if r.get("bold"):
                    t = f"<strong>{t}</strong>"
                if r.get("italic"):
                    t = f"<em>{t}</em>"
                if r.get("underline"):
                    t = f"<u>{t}</u>"
                if r.get("strike"):
                    t = f"<s>{t}</s>"
                if r.get("sub"):
                    t = f"<sub>{t}</sub>"
                if r.get("sup"):
                    t = f"<sup>{t}</sup>"
            style = []
            if r.get("color") and not color_is_dark(r["color"]):
                style.append(f"color:{r['color']}")
            if r.get("bg"):
                style.append(f"background-color:{r['bg']}")
            if style:
                t = f'<span style="{"; ".join(style)}">{t}</span>'
            link = r.get("link")
            href = self.resolve_run(link)
            if link and link[0] == "heading" and not self.heading_href(link[1], link[2]):
                # stale manual-TOC links target an id that no longer exists;
                # rescue them by matching the link's text to a live heading
                alt = self.resolve_stale_heading(link[1], r.get("text", ""))
                if alt:
                    href = alt
            if href:
                t = f'<a href="{attr(href)}">{t}</a>'
            out.append(t)
        return "".join(out)

    def render_blocks(self, blocks):
        out = []
        for b in blocks:
            t = b["type"]
            if t == "para":
                if not b.get("runs"):
                    # deliberate blank line (an empty paragraph in the doc);
                    # it keeps the anchor of the empty heading it came from
                    hid = f' id="{attr(b["heading_id"])}"' if b.get("heading_id") else ""
                    out.append(f'<p class="gap"{hid}>&nbsp;</p>')
                    continue
                # demoted prose headings keep their anchor id
                hid = f' id="{attr(b["heading_id"])}"' if b.get("heading_id") else ""
                out.append(f"<p{hid}>{self.render_runs(b['runs'])}</p>")
            elif t == "heading":
                lvl = min(max(b["level"], 1), 6)
                hid = f' id="{attr(b["heading_id"])}"' if b.get("heading_id") else ""
                cls = ' class="subtitle"' if b.get("subtitle") else ""
                out.append(f"<h{lvl}{hid}{cls}>{self.render_runs(b['runs'])}</h{lvl}>")
            elif t == "list":
                out.append(self.render_list(b["items"]))
            elif t == "table":
                out.append(self.render_table(b))
            elif t == "toc":
                items = []
                for pb in b.get("blocks", []):
                    if pb["type"] == "para":
                        items.append(f"<li>{self.render_runs(pb['runs'])}</li>")
                out.append('<details class="toc"><summary>Table of contents</summary>'
                           f'<ul>{"".join(items)}</ul></details>')
            elif t == "html":
                out.append(b["html"])
        return "\n".join(out)

    def render_list(self, items):
        parts = []
        stack = []  # [level, lid, tag, css, [[li_inner_parts], ...]]
        def close_list():
            level, lid, tag, css, lis = stack.pop()
            st = f' style="list-style-type:{css}"' if css else ""
            inner = f"<{tag}{st}>" + "".join(
                "<li>" + "".join(p) + "</li>" for p in lis) + f"</{tag}>"
            if stack:
                stack[-1][4][-1].append(inner)
            else:
                parts.append(inner)
        for it in items:
            level, lid = it["level"], it["list_id"]
            tag, css = it["kind"] or ("ul", "")
            while stack and (stack[-1][0] > level
                             or (stack[-1][0] == level and stack[-1][1] != lid)):
                close_list()
            if not stack or stack[-1][0] != level:
                stack.append([level, lid, tag, css, []])
            stack[-1][4].append([self.render_runs(it["runs"])])
        while stack:
            close_list()
        return "".join(parts)

    def render_table(self, b):
        out = ["<table>"]
        for row in b["rows"]:
            out.append("<tr>")
            for cell in row:
                out.append(f"<td>{self.render_blocks(cell)}</td>")
            out.append("</tr>")
        out.append("</table>")
        return "".join(out)


# ---------------------------------------------------------------------------
# source: Google Docs API
# ---------------------------------------------------------------------------

H_LEVELS = {
    "TITLE": 1, "SUBTITLE": 1,
    "HEADING_1": 2, "HEADING_2": 3, "HEADING_3": 4,
    "HEADING_4": 5, "HEADING_5": 6, "HEADING_6": 6,
}

MONO_FONTS = ("courier", "consolas", "menlo", "monaco", "andale",
              "roboto mono", "monospace", "mono")


def is_mono(fam):
    fam = (fam or "").lower()
    return any(m in fam for m in MONO_FONTS)


def rgb_to_css(color_obj):
    try:
        c = color_obj["color"]["rgbColor"]
        return "#%02x%02x%02x" % tuple(
            round(min(1.0, max(0.0, v)) * 255)
            for v in (c["red"], c["green"], c["blue"]))
    except Exception:
        return None


def run_style(style, raw_link):
    r = {"text": ""}
    if raw_link:
        r["link"] = raw_link
    if style.get("bold"):
        r["bold"] = True
    if style.get("italic"):
        r["italic"] = True
    if style.get("underline"):
        r["underline"] = True
    if style.get("strikethrough"):
        r["strike"] = True
    bo = style.get("baselineOffset")
    if bo == "SUPERSCRIPT":
        r["sup"] = True
    elif bo == "SUBSCRIPT":
        r["sub"] = True
    wf = style.get("weightedFontFamily") or {}
    if is_mono(wf.get("fontFamily")):
        r["code"] = True
    fg = rgb_to_css(style.get("foregroundColor")) if style.get("foregroundColor") else None
    if fg:
        r["color"] = fg
    bg = rgb_to_css(style.get("backgroundColor")) if style.get("backgroundColor") else None
    if bg:
        r["bg"] = bg
    return r


def runs_from_text_run(tr):
    style = tr.get("textStyle") or {}
    content = (tr.get("content") or "").replace("\uE907", "")
    if not content:
        return []
    raw_link = parse_api_link(style["link"]) if style.get("link") else None
    base = run_style(style, raw_link)
    out = []
    for i, chunk in enumerate(content.split("\n")):
        if i:
            out.append({"br": True})
        if chunk:
            out.append({**base, "text": chunk})
    return out


def list_kind(ctx, list_id, nesting):
    lst = (ctx.get("lists") or {}).get(list_id) or {}
    levels = (lst.get("listProperties") or {}).get("nestingLevels") or []
    glyph = ""
    if nesting < len(levels):
        glyph = (levels[nesting] or {}).get("glyphType", "")
    if glyph in ("DECIMAL", "DECIMAL_ZERO", "DECIMAL_ZERO_PADDED",
                 "UPPER_ALPHA", "LOWER_ALPHA", "UPPER_ROMAN", "LOWER_ROMAN"):
        css = {"UPPER_ALPHA": "upper-alpha", "LOWER_ALPHA": "lower-alpha",
               "UPPER_ROMAN": "upper-roman", "LOWER_ROMAN": "lower-roman"}.get(glyph, "decimal")
        return ("ol", css)
    return ("ul", "")


def parse_paragraph(p, ctx):
    pstyle = p.get("paragraphStyle") or {}
    named = pstyle.get("namedStyleType", "NORMAL_TEXT")
    level = H_LEVELS.get(named, 0)
    runs = []
    hr = False
    for e in p.get("elements", []):
        if "textRun" in e:
            runs.extend(runs_from_text_run(e["textRun"]))
        elif "inlineObjectElement" in e:
            oid = e["inlineObjectElement"].get("inlineObjectId")
            img = get_image_uri(ctx, oid)
            if img:
                runs.append({"img": img})
        elif "horizontalRule" in e:
            hr = True
        elif "footnoteReference" in e:
            runs.append({"text": "[" + str(e["footnoteReference"].get("footnoteNumber", "")) + "]"})
        elif "person" in e:
            pp = e["person"].get("personProperties") or {}
            email = pp.get("email", "")
            r = {"text": pp.get("name") or email}
            if email:
                r["link"] = ("url", "mailto:" + email)
            runs.append(r)
        elif "richLink" in e:
            rp = e["richLink"].get("richLinkProperties") or {}
            r = {"text": rp.get("title") or "link"}
            if rp.get("uri"):
                r["link"] = ("url", rp["uri"])
            runs.append(r)
        elif "dateElement" in e:
            dp = e["dateElement"].get("dateElementProperties") or {}
            runs.append({"text": dp.get("displayText", "")})
        # pageBreak / columnBreak / autoText / equation: ignored
    if hr:
        return {"type": "html", "html": "<hr>"}
    # drop the paragraph-ending newline (it arrives either as a text run
    # ending in \n or as a trailing <br> run after splitting on newlines)
    while runs and (runs[-1].get("br") or (runs[-1].get("text", "") or "").endswith("\n")):
        if runs[-1].get("br"):
            runs.pop()
        else:
            last = runs[-1]["text"]
            if last == "\n":
                runs.pop()
            else:
                runs[-1] = {**runs[-1], "text": last.rstrip("\n")}
                break
    if not runs:
        # An empty paragraph is the doc author's deliberate blank line
        # (a second Enter in Google Docs) — keep it so the site mirrors the
        # doc's layout: one Enter = tight next line, two Enters = blank line.
        # That includes empty paragraphs that carry a heading style (Enter
        # pressed while a heading style was active, e.g. at the top of the
        # next page/block); only empty list items add no visible line. Empty
        # Title/Subtitle paragraphs are dropped so they cannot break the
        # document front-matter detection.
        if p.get("bullet") or named in ("TITLE", "SUBTITLE"):
            return None
        return {"type": "para", "runs": [], "blank": True,
                "heading_id": pstyle.get("headingId") if level else None}
    if p.get("bullet"):
        b = p["bullet"]
        return {
            "type": "listitem",
            "list_id": b.get("listId", ""),
            "level": b.get("nestingLevel", 0),
            "kind": list_kind(ctx, b.get("listId", ""), b.get("nestingLevel", 0)),
            "runs": runs,
        }
    if level:
        h = {
            "type": "heading",
            "level": level,
            "heading_id": pstyle.get("headingId"),
            "subtitle": named == "SUBTITLE",
            "runs": runs,
        }
        if named in ("TITLE", "SUBTITLE"):
            # front matter of the document, not a section of its own
            h["doc_meta"] = True
        return h
    p_block = {"type": "para", "runs": runs}
    if named in ("TITLE", "SUBTITLE"):
        p_block["doc_meta"] = True
    return p_block


# A heading paragraph whose first line is this long is body prose that was
# typed into a heading style (Google Docs keeps it bold as part of the
# heading). Real titles are short; long first lines are demoted to plain
# paragraphs. First lines starting with "___" are this doc's decorative
# title convention and are treated as intentional headings.
HEADING_PROSE_LEN = 80


def normalize_heading(b):
    """Normalize a heading block from either source.

    1. A heading whose first line is body-length prose (e.g. a whole
       sentence typed into Heading 5 with shift+enter) is demoted to a plain
       paragraph. Its anchor id is kept so internal links keep working.
    2. A real heading with extra lines after it (shift+enter inside the
       heading) is split: the first line stays the heading, the rest becomes
       a body paragraph (it would otherwise render bold as part of the
       heading).
    """
    runs = b.get("runs", [])
    first = []
    for r in runs:
        if r.get("br"):
            break
        first.append(r.get("text", ""))
    fl = "".join(first).strip()
    if len(fl) >= HEADING_PROSE_LEN and not fl.startswith("___"):
        # Keep the original heading level so the navigation outline (which
        # mirrors Google's "Document tabs") can still include this entry.
        return [{"type": "para", "heading_id": b.get("heading_id"),
                 "level": b.get("level"), "runs": runs}]
    # A heading that begins with a line break has no title line to preserve —
    # don't split it into an empty heading + body. Just drop the leading
    # line break(s) and keep the whole thing as a heading.
    if not fl:
        idx = 0
        while idx < len(runs) and runs[idx].get("br"):
            idx += 1
        if idx and idx < len(runs):
            return [dict(b, runs=runs[idx:])]
    for i, r in enumerate(runs):
        if r.get("br"):
            body = [x for x in runs[i + 1:] if not x.get("br")]
            if not body:
                break  # nothing but line breaks after: heading stands alone
            return [dict(b, runs=runs[:i]),
                    {"type": "para", "runs": body}]
    return [b]


def get_image_uri(ctx, oid):
    io = (ctx.get("inline_objects") or {}).get(oid) or {}
    emb = (io.get("inlineObjectProperties") or {}).get("embeddedObject") or {}
    return (emb.get("imageProperties") or {}).get("contentUri")


def walk_elements(content, ctx):
    blocks = []
    pending = []

    def flush_list():
        if pending:
            blocks.append({"type": "list", "items": list(pending)})
            pending.clear()

    for el in content or []:
        if "paragraph" in el:
            b = parse_paragraph(el["paragraph"], ctx)
            if b is None:
                continue
            if b["type"] == "listitem":
                pending.append(b)
            else:
                flush_list()
                blocks.extend(normalize_heading(b))
        elif "table" in el:
            flush_list()
            blocks.append({"type": "table", "rows": parse_table(el["table"], ctx)})
        elif "tableOfContents" in el:
            flush_list()
            toc = walk_elements(el["tableOfContents"].get("content", []), ctx)
            blocks.append({"type": "toc", "blocks": toc})
        # sectionBreak etc.: ignored
    flush_list()
    return blocks


def parse_table(t, ctx):
    rows = []
    for row in t.get("tableRows") or []:
        cells = []
        for cell in row.get("tableCells") or []:
            cells.append(walk_elements(cell.get("content", []), ctx))
        rows.append(cells)
    return rows


def flatten_tab(tab):
    props = tab.get("tabProperties") or {}
    dtab = tab.get("documentTab") or {}
    body = dtab.get("body") or {}
    ctx = {
        "inline_objects": dtab.get("inlineObjects") or {},
        "lists": dtab.get("lists") or {},
    }
    yield {
        "id": props.get("tabId", "t.0"),
        "title": props.get("title", "Tab"),
        "blocks": walk_elements(body.get("content", []), ctx),
    }
    for c in tab.get("childTabs") or []:
        yield from flatten_tab(c)


def parse_document(data):
    doc_id = data.get("documentId", "")
    title = data.get("title") or "Google Doc"
    tabs = []
    if data.get("tabs"):
        for tb in data["tabs"]:
            tabs.extend(flatten_tab(tb))
    else:
        body = data.get("body") or {}
        ctx = {"inline_objects": data.get("inlineObjects") or {},
               "lists": data.get("lists") or {}}
        tabs.append({"id": "t.0", "title": title,
                     "blocks": walk_elements(body.get("content", []), ctx)})
    return title, tabs


# ---------------------------------------------------------------------------
# source: public HTML export
# ---------------------------------------------------------------------------

def parse_css(css_text):
    """Parse a <style> block into {classname: {prop: value}}."""
    rules = {}
    i, n = 0, len(css_text)
    while i < n:
        start = i
        while i < n and css_text[i] != "{":
            i += 1
        if i >= n:
            break
        sel = css_text[start:i].strip()
        i += 1
        depth, dstart = 1, i
        while i < n and depth:
            if css_text[i] == "{":
                depth += 1
            elif css_text[i] == "}":
                depth -= 1
            i += 1
        decl = css_text[dstart:i - 1]
        m = re.fullmatch(r"\.([A-Za-z0-9_-]+)", sel)
        if not m:
            continue
        cls = m.group(1)
        props = rules.setdefault(cls, {})
        # Tolerate a missing trailing semicolon on a rule's final declaration
        # (Google's export drops it, e.g. "text-decoration:line-through}").
        for pm in re.finditer(r"([a-zA-Z-]+)\s*:\s*([^;}]+)", decl):
            props[pm.group(1).strip().lower()] = pm.group(2).strip()
    return rules


def normalize_color(c):
    c = c.strip()
    m = re.match(r"#([0-9a-fA-F]{3})$", c)
    if m:
        h = m.group(1)
        return "#" + "".join(ch * 2 for ch in h)
    m = re.match(r"rgb\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)", c)
    if m:
        return "#%02x%02x%02x" % tuple(int(g) for g in m.groups())
    return c


def color_is_dark(color):
    """True for dark, near-neutral colors (black / dark grays) that would be
    unreadable on the dark theme. Such colors are dropped so the text inherits
    the themed foreground. Vivid dark colors (blue links, red warnings) are
    kept — they stay distinguishable on dark and keep their meaning in light."""
    m = re.match(r"#([0-9a-fA-F]{6})$", color or "")
    if not m:
        return False
    h = m.group(1)
    r, g, b = (int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    mx, mn = max(r, g, b), min(r, g, b)
    return mx < 0.5 and (mx - mn) < 0.35


def class_style(cls, css_map):
    props = css_map.get(cls, {})
    d = {}
    fw = props.get("font-weight")
    if fw and (fw == "bold" or (fw.isdigit() and int(fw) >= 700)):
        d["bold"] = True
    if props.get("font-style") == "italic":
        d["italic"] = True
    td = props.get("text-decoration", "")
    if "underline" in td:
        d["underline"] = True
    if "line-through" in td:
        d["strike"] = True
    col = props.get("color")
    if col:
        d["color"] = normalize_color(col)
    bg = props.get("background-color")
    if bg and bg != "transparent":
        d["bg"] = normalize_color(bg)
    if is_mono(props.get("font-family")):
        d["code"] = True
    return d


def inline_is_blank(runs):
    """True when runs carry no visible content (no text, no image)."""
    for r in runs:
        if r.get("img"):
            return False
        if (r.get("text") or "").replace("\u00a0", " ").strip():
            return False
    return True


class ExportParser(HTMLParser):
    """Parse the export HTML into the same block model as the API source."""

    def __init__(self, css_map):
        super().__init__(convert_charrefs=True)
        self.css = css_map
        self.blocks = []
        self.pending = []
        self.inline = []
        self.style_stack = []
        self.style = {}
        self.link_stack = []
        self.heading = None
        self.para = False
        self.para_meta = False    # <p class="title"/"subtitle"> front matter
        self.li = None            # (tag, level, lid)
        self.list_stack = []
        self.list_count = 0
        self.table = None
        self.table_row = None
        self.table_cell = None
        self.skip_depth = 0       # inside <style>/<script>

    # -- plumbing -----------------------------------------------------------

    def push_style(self, delta):
        self.style_stack.append((delta, self.style))
        self.style = dict(self.style)
        self.style.update(delta)

    def pop_style(self):
        if self.style_stack:
            _, prev = self.style_stack.pop()
            self.style = prev

    def flush_pending_list(self):
        if self.pending:
            self.blocks.append({"type": "list", "items": self.pending})
            self.pending = []

    def flush_inline(self):
        if not self.inline:
            return
        runs = self.inline
        self.inline = []
        if self.heading is not None:
            lvl, hid = self.heading
            b = {"type": "heading", "level": lvl,
                 "heading_id": hid, "runs": runs}
            self.blocks.extend(normalize_heading(b))
        elif self.table is not None:
            self.table_cell.append({"type": "para", "runs": runs})
        elif self.li is not None:
            self.pending.append({"type": "listitem", "list_id": self.li[2],
                                 "level": self.li[1], "kind": (self.li[0], ""),
                                 "runs": runs})
        else:
            self.flush_pending_list()
            b = {"type": "para", "runs": runs}
            if self.para_meta:
                # the document's Title/Subtitle line: front matter, not a
                # section of its own (see Site._split_and_slug)
                b["doc_meta"] = True
            self.blocks.append(b)

    # -- HTMLParser callbacks ----------------------------------------------

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("style", "script"):
            self.skip_depth += 1
            return
        if self.skip_depth:
            return
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self.flush_inline()
            self.heading = (int(tag[1]), a.get("id"))
        elif tag == "p":
            self.flush_inline()
            classes = (a.get("class") or "").split()
            self.para_meta = "title" in classes or "subtitle" in classes
            self.para = True
        elif tag == "span":
            delta = {}
            for cls in (a.get("class") or "").split():
                delta.update(class_style(cls, self.css))
            self.push_style(delta)
        elif tag == "a":
            self.link_stack.append(raw_link_from_href(a.get("href")))
        elif tag == "b" or tag == "strong":
            self.push_style({"bold": True})
        elif tag == "i" or tag == "em":
            self.push_style({"italic": True})
        elif tag == "u":
            self.push_style({"underline": True})
        elif tag in ("s", "strike", "del"):
            self.push_style({"strike": True})
        elif tag in ("code", "tt", "kbd"):
            self.push_style({"code": True})
        elif tag == "sub":
            self.push_style({"sub": True})
        elif tag == "sup":
            self.push_style({"sup": True})
        elif tag == "br":
            self.inline.append({"br": True})
        elif tag == "img":
            self.inline.append({"img": a.get("src", "")})
        elif tag in ("ul", "ol"):
            self.flush_inline()
            self.list_stack.append(tag)
            self.list_count += 1
        elif tag == "li":
            self.flush_inline()
            level = max(len(self.list_stack) - 1, 0)
            self.li = (self.list_stack[-1] if self.list_stack else "ul",
                       level, f"l{self.list_count}")
        elif tag == "table":
            self.flush_inline()
            self.flush_pending_list()
            self.table = []
            self.table_row = None
            self.table_cell = None
        elif tag == "tr":
            self.flush_inline()
            self.table_row = []
        elif tag in ("td", "th"):
            self.flush_inline()
            self.table_cell = []
        elif tag == "hr":
            self.flush_inline()
            self.blocks.append({"type": "html", "html": "<hr>"})

    def handle_endtag(self, tag):
        if tag in ("style", "script"):
            self.skip_depth = max(self.skip_depth - 1, 0)
            return
        if self.skip_depth:
            return
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            if self.heading is not None and inline_is_blank(self.inline):
                # A heading with no text at all is the doc author's deliberate
                # blank line: pressing Enter while a heading style is active
                # (e.g. at the top of the next page/block) leaves an empty
                # heading behind. Keep it, so the site mirrors the doc's
                # spacing instead of dropping the intended free line.
                self.inline = []
                b = {"type": "para", "runs": [],
                     "heading_id": self.heading[1]}
                if self.table is not None:
                    self.table_cell.append(b)
                else:
                    self.flush_pending_list()
                    self.blocks.append(b)
            self.flush_inline()
            self.heading = None
        elif tag == "p":
            if self.para and not self.inline and self.heading is None \
                    and self.li is None:
                # empty paragraph = the doc author's deliberate blank line
                b = {"type": "para", "runs": []}
                if self.table is not None:
                    self.table_cell.append(b)
                else:
                    self.flush_pending_list()
                    self.blocks.append(b)
            self.flush_inline()
            self.para = False
            self.para_meta = False
        elif tag == "span":
            self.pop_style()
        elif tag == "a":
            if self.link_stack:
                self.link_stack.pop()
        elif tag in ("b", "strong", "i", "em", "u", "s", "strike", "del",
                     "code", "tt", "kbd", "sub", "sup"):
            self.pop_style()
        elif tag == "li":
            self.flush_inline()
            self.li = None
        elif tag in ("ul", "ol"):
            self.flush_inline()
            if self.list_stack:
                self.list_stack.pop()
        elif tag == "td" or tag == "th":
            self.flush_inline()
            if self.table_row is not None and self.table_cell is not None:
                self.table_row.append(self.table_cell)
            self.table_cell = None
        elif tag == "tr":
            self.flush_inline()
            if self.table is not None and self.table_row is not None:
                self.table.append(self.table_row)
            self.table_row = None
        elif tag == "table":
            self.flush_inline()
            if self.table is not None:
                self.blocks.append({"type": "table", "rows": self.table})
            self.table = None

    def handle_data(self, data):
        if self.skip_depth or not data:
            return
        r = {"text": data}
        r.update(self.style)
        if self.link_stack:
            r["link"] = self.link_stack[-1]
        self.inline.append(r)


def is_toc_para(b):
    """A paragraph that is exactly one internal heading link (a TOC entry)."""
    runs = b.get("runs", [])
    if len(runs) != 1:
        return False
    r = runs[0]
    lnk = r.get("link")
    return bool(lnk and lnk[0] == "heading" and (r.get("text", "") or "").strip())


def collapse_toc(blocks):
    """Google's HTML export renders an in-document table of contents as plain
    single-link paragraphs. Group runs of them into one collapsible block so
    a 2,500-entry TOC doesn't blow up a page."""
    out = []
    i, n = 0, len(blocks)
    while i < n:
        if blocks[i]["type"] == "para" and is_toc_para(blocks[i]):
            j = i
            while j < n and blocks[j]["type"] == "para" and is_toc_para(blocks[j]):
                j += 1
            if j - i >= 5:
                out.append({"type": "toc", "blocks": blocks[i:j]})
                i = j
                continue
        out.append(blocks[i])
        i += 1
    return out


def parse_export(html_text, title=SITE_TITLE, doc_id=""):
    css_map = {}
    for m in re.finditer(r"<style[^>]*>(.*?)</style>", html_text, re.S):
        css_map.update(parse_css(m.group(1)))
    parser = ExportParser(css_map)
    parser.feed(html_text)
    parser.close()
    blocks = collapse_toc(parser.blocks)
    return title, [{"id": "t.0", "title": title, "blocks": blocks}]


# ---------------------------------------------------------------------------
# fetch
# ---------------------------------------------------------------------------

RETRYABLE_STATUS = (429, 500, 502, 503, 504)


def http_get(url, timeout=180, headers=None, retries=4):
    """Fetch a URL, retrying transient failures with exponential backoff.

    Returns (text, final_url). HTTP 429/5xx responses and network errors
    are retried up to `retries` times; other HTTP errors (401/403/404 …)
    are re-raised immediately, since retrying cannot help them.
    """
    h = {"User-Agent": USER_AGENT}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    delay = 3.0
    attempt = 0
    while True:
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                text = resp.read().decode("utf-8", "replace")
                return text, resp.geturl()
        except urllib.error.HTTPError as e:
            retryable = e.code in RETRYABLE_STATUS
            exc = e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            retryable = True
            exc = e
        if not retryable or attempt >= retries:
            raise exc
        attempt += 1
        sys.stderr.write(f"  fetch failed, retrying in {delay:.0f}s "
                         f"(attempt {attempt}/{retries}) — {url}\n")
        time.sleep(delay)
        delay = min(delay * 2, 30) + 0.5


LOGIN_HOSTS = ("accounts.google.com", "accounts.google.com.")


def fetch_export(doc):
    """Fetch the public HTML export of a Google Doc.

    Raises RuntimeError if Google redirected us to a sign-in page instead of
    the document (i.e. the doc is not shared "Anyone with the link").
    """
    text, final_url = http_get(EXPORT_URL.format(doc=doc))
    host = urllib.parse.urlsplit(final_url).netloc.lower()
    if host in LOGIN_HOSTS or "ServiceLogin" in final_url:
        raise RuntimeError(
            "Google returned a sign-in page — make sure the document is "
            "shared as 'Anyone with the link' (see README.md)")
    if len(text) < 50_000:
        sys.stderr.write(f"  warning: export is suspiciously small "
                         f"({len(text)} bytes); Google may have served an "
                         f"error page instead of the document\n")
    return text


def token_post(params):
    data = urllib.parse.urlencode(params).encode("utf-8")
    req = urllib.request.Request(
        TOKEN_URL, data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def load_auth(path):
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_auth(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def pkce_pair():
    import base64
    import hashlib
    import secrets
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def get_access_token(auth):
    tok = token_post({
        "client_id": auth["client_id"],
        "client_secret": auth["client_secret"],
        "refresh_token": auth["refresh_token"],
        "grant_type": "refresh_token",
    })
    if "access_token" not in tok:
        sys.stderr.write("Auth failed: " + json.dumps(tok)[:300] + "\n")
        sys.exit(1)
    return tok["access_token"]


def fetch_api(doc, auth):
    url = DOCS_API.format(doc=doc)
    token = get_access_token(auth)
    try:
        text, _ = http_get(url, timeout=120,
                           headers={"Authorization": "Bearer " + token})
        return json.loads(text)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        if e.code in (401, 403):
            sys.stderr.write("Google rejected the saved authorization. Re-run:\n"
                             f"  python gdoc_site.py --auth --client-json <client_secret_*.json>\n")
            sys.exit(1)
        sys.stderr.write(f"Docs API error {e.code} (a very large document may "
                         f"be larger than the API can serve): {body[:200]}\n")
        return None
    except Exception as e:
        sys.stderr.write(f"Docs API fetch failed: {e}\n")
        return None


def run_auth_flow(client_json_path, port=8912):
    """One-time OAuth: open a browser, let the user click Allow, save tokens."""
    import webbrowser
    from http.server import BaseHTTPRequestHandler, HTTPServer

    with open(client_json_path, encoding="utf-8") as f:
        cfg = json.load(f)
    info = cfg.get("installed") or cfg.get("web") or {}
    client_id = info.get("client_id") or cfg.get("client_id")
    client_secret = info.get("client_secret") or cfg.get("client_secret")
    if not client_id or not client_secret:
        sys.exit(f"Could not find client_id/client_secret in {client_json_path}")

    redirect_uri = f"http://127.0.0.1:{port}"
    result = {"code": None, "error": None}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if "code" in q:
                result["code"] = q["code"][0]
                body = (b"<html><body style='font-family:sans-serif;margin:2em'>"
                        b"<h2>Authorized - you can close this tab.</h2></body></html>")
            elif "error" in q:
                result["error"] = q["error"][0]
                body = (f"<html><body><h2>Authorization failed: "
                        f"{q['error'][0]}</h2></body></html>").encode()
            else:
                self.send_response(400)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    verifier, challenge = pkce_pair()
    auth_url = AUTH_URL.format(
        client_id=urllib.parse.quote(client_id, safe=""),
        redirect_uri=urllib.parse.quote(redirect_uri, safe=""),
        scope=urllib.parse.quote(OAUTH_SCOPE, safe=""),
        challenge=challenge)
    server = HTTPServer(("127.0.0.1", port), Handler)
    print("Opening your browser…")
    print("If nothing opens, visit this URL and click Allow:\n  " + auth_url)
    webbrowser.open(auth_url)
    deadline = time.time() + 300
    while (result["code"] is None and result["error"] is None
           and time.time() < deadline):
        server.handle_request()
    server.server_close()
    if result["error"]:
        sys.exit(f"Authorization failed: {result['error']}")
    if not result["code"]:
        sys.exit("Timed out after 5 minutes waiting for authorization.")
    tok = token_post({
        "code": result["code"],
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
        "code_verifier": verifier,
    })
    if "refresh_token" not in tok:
        sys.exit("No refresh token returned: " + json.dumps(tok)[:300])
    return {"client_id": client_id, "client_secret": client_secret,
            "refresh_token": tok["refresh_token"]}


# ---------------------------------------------------------------------------
# page templates
# ---------------------------------------------------------------------------

STYLE_CSS = """\
:root {
  /* dark is the default theme */
  --bg: #0d1117; --fg: #e6edf3; --muted: #8b949e;
  --sidebar-bg: #161b22; --border: #30363d;
  --accent: #4493f8; --accent-soft: #1f3a5f;
  --code-bg: #1c2128; --table-border: #30363d;
  /* neutral backdrop + the "sheet of paper" the document sits on */
  --page-bg: #0b0e14;
  --paper-bg: #1c2331;
  /* visible scrollbars for the document-tabs sidebar (dark theme) */
  --scroll-thumb: #3f444c; --scroll-thumb-hover: #4b5158;
  --scroll-track: #10151b;
}
:root[data-theme="light"] {
  --bg: #ffffff; --fg: #1f2328; --muted: #656d76;
  --sidebar-bg: #f6f8fa; --border: #d8dee4;
  --accent: #0969da; --accent-soft: #ddf4ff;
  --code-bg: #eff1f3; --table-border: #d0d7de;
  --scroll-thumb: #c1c9d1; --scroll-thumb-hover: #b1b9c2;
  --scroll-track: transparent;
  --page-bg: #f8f9fa;
  --paper-bg: #ffffff;
}
* { box-sizing: border-box; }
html { scroll-behavior: smooth; }
body {
  margin: 0; background: var(--page-bg); color: var(--fg);
  font: 16px/1.65 Arial, Helvetica, sans-serif;
}
a { color: var(--accent); text-decoration: none; font-weight: 600; }
a:hover { text-decoration: underline; }

.topbar {
  position: sticky; top: 0; z-index: 30;
  display: flex; align-items: center; gap: 10px;
  padding: 8px 16px; background: var(--sidebar-bg);
  border-bottom: 1px solid var(--border);
}
.topbar .brand { display: flex; align-items: center; gap: 8px;
  font-weight: 600; white-space: nowrap; overflow: hidden;
  text-overflow: ellipsis; color: var(--fg); }
.brand-logo { width: 22px; height: 22px; flex: 0 0 auto;
  border-radius: 5px; object-fit: contain; }
#nav-toggle { display: none; background: none; border: 1px solid var(--border);
  border-radius: 6px; color: var(--fg); font-size: 18px; padding: 2px 10px; cursor: pointer; }
#search { flex: 1; max-width: 420px; margin-left: auto;
  padding: 7px 12px; border: 1px solid var(--border); border-radius: 8px;
  background: var(--bg); color: var(--fg); font-size: 14px; }
#theme-toggle { background: none; border: 1px solid var(--border);
  border-radius: 6px; color: var(--fg); font-size: 16px; line-height: 1;
  padding: 3px 9px; cursor: pointer; flex: 0 0 auto; }

.layout { display: flex; align-items: stretch; }
.sidebar {
  width: 380px; flex: 0 0 380px; border-right: 1px solid var(--border);
  background: var(--sidebar-bg); max-height: calc(100vh - 49px);
  position: sticky; top: 49px; overflow-y: auto; padding: 12px 10px 40px;
  scrollbar-width: thin;
  scrollbar-color: var(--scroll-thumb) var(--scroll-track);
}
.sidebar::-webkit-scrollbar { width: 8px; }
.sidebar::-webkit-scrollbar-track { background: var(--scroll-track); }
.sidebar::-webkit-scrollbar-thumb {
  background: var(--scroll-thumb); border-radius: 6px;
  border: 1px solid var(--sidebar-bg); }
.sidebar::-webkit-scrollbar-thumb:hover { background: var(--scroll-thumb-hover); }

/* main document scrollbar (the window scroll), same theme styling */
html, body { scrollbar-width: auto;
  scrollbar-color: var(--scroll-thumb) var(--scroll-track); }
html::-webkit-scrollbar, body::-webkit-scrollbar { width: 13px; }
html::-webkit-scrollbar-track, body::-webkit-scrollbar-track {
  background: var(--scroll-track); }
html::-webkit-scrollbar-thumb, body::-webkit-scrollbar-thumb {
  background: var(--scroll-thumb); border-radius: 8px;
  border: 2px solid var(--bg); }
html::-webkit-scrollbar-thumb:hover, body::-webkit-scrollbar-thumb:hover {
  background: var(--scroll-thumb-hover); }
.sidebar-brand { display: block; font-weight: 700; margin: 4px 8px 10px; color: var(--fg); }
.tab-chips { display: flex; flex-wrap: wrap; gap: 6px; margin: 0 4px 12px; }
.chip { font-size: 13px; padding: 3px 10px; border: 1px solid var(--border);
  border-radius: 999px; background: var(--bg); color: var(--fg); }
.toc { list-style: none; margin: 0; padding: 0; }
.toc ul { list-style: none; margin: 0; padding-left: 14px; }
.toc-group { margin: 10px 0; }
.toc-tab { font-size: 12px; font-weight: 700; text-transform: uppercase;
  letter-spacing: .04em; color: var(--muted); display: block; margin: 0 6px 4px; }
.toc-section { display: block; padding: 6px 6px; border-radius: 6px;
  font-size: 14px; color: var(--fg); }
.toc-section:hover { background: var(--accent-soft); text-decoration: none; }
.toc-section.current { background: var(--accent-soft); font-weight: 600; }
.toc-subs a { display: block; padding: 5px 6px; font-size: 13px; color: var(--muted); }
.toc-subs a:hover { color: var(--fg); text-decoration: none; }
.toc-sub.current { color: var(--fg); font-weight: 600; }
.toc a.nav-active { color: var(--fg); font-weight: 700;
  background: var(--accent-soft); border-radius: 6px; }
.toc a, .toc .toc-tab { display: block; white-space: nowrap; overflow: hidden;
  text-overflow: ellipsis; max-width: 100%; }
.toc-item { margin: 3px 0; }
.toc-subs { display: none; }
.toc-subs.open { display: block; }
.toc-subs .toc-item { margin: 1px 0; }
.outline-hint { font-size: 13px; color: var(--muted); margin: 6px 0 14px; }
.toc-full { padding: 2px 4px; }
.toc-full > .toc-item { border-bottom: 1px solid var(--border); }
.toc-preview { position: fixed; z-index: 60; pointer-events: none;
  max-width: min(340px, calc(100vw - 32px)); padding: 8px 12px; border-radius: 8px;
  background: var(--bg); color: var(--fg); border: 1px solid var(--border);
  box-shadow: 0 4px 16px rgba(0, 0, 0, .25); font-size: 13px; line-height: 1.45;
  opacity: 0; transform: translateY(3px);
  transition: opacity .1s ease, transform .1s ease; }
.toc-preview.show { opacity: 1; transform: translateY(0); }
.sidebar-foot { margin: 18px 6px 0; font-size: 12px; color: var(--muted); }

.content {
  flex: 1; min-width: 0; padding: 36px 40px 72px;
  background: var(--paper-bg); color: var(--fg);
  border: 1px solid var(--border); border-radius: 6px;
  box-shadow: 0 1px 3px rgba(0, 0, 0, .12), 0 1px 2px rgba(0, 0, 0, .08);
}
@media (min-width: 1000px) {
  .content { max-width: 860px; margin: 24px auto 48px; }
}

.crumbs { font-size: 14px; color: var(--muted); margin-bottom: 8px; }
h1 { font-size: 26px; line-height: 1.3; margin: 4px 0 10px; font-weight: 400; }
.meta { color: var(--muted); font-size: 14px; margin: 0 0 18px; }
.notice { background: var(--accent-soft); border-radius: 8px; padding: 10px 14px;
  font-size: 14px; margin: 0 0 26px; }
#search-backdrop { position: fixed; inset: 0; z-index: 40; display: none;
  background: rgba(0, 0, 0, .4); }
#search-results { position: fixed; z-index: 50; display: none;
  background: var(--bg); color: var(--fg);
  border: 1px solid var(--border); border-radius: 10px;
  box-shadow: 0 10px 34px rgba(0, 0, 0, .35);
  padding: 10px 12px; max-height: min(70vh, 620px); overflow-y: auto; }
.sr-count { font-size: 13px; color: var(--muted); margin: 0 0 8px; }
#search-results .sr { display: block; padding: 8px 12px; border: 1px solid var(--border);
  border-radius: 8px; margin-bottom: 8px; background: var(--bg); }
#search-results .sr .sr-hits { font-size: 12px; color: var(--muted); margin-left: 8px; }
#search-results .sr .t { font-weight: 600; color: var(--fg); }
#search-results .sr .tab { font-size: 12px; color: var(--muted); margin-left: 8px; }
#search-results .sr .sn { font-size: 13px; color: var(--muted); display: block; margin-top: 2px; }
#search-results .sr mark, .doc mark {
  background: #ffe58f; color: #392f00;
  border-radius: 3px; padding: 0 1px; }

.doc { font-size: 15px; line-height: 1.6; overflow-wrap: break-word; }
/* anchor jumps land just below the sticky topbar instead of hidden under it */
.doc [id] { scroll-margin-top: 68px; }
.doc h2 { font-size: 20px; margin: 1.6em 0 .6em; padding-top: .3em;
  border-bottom: 1px solid var(--border); font-weight: 600; }
.doc h3 { font-size: 17px; margin: 1.4em 0 .5em; font-weight: 600; }
.doc h4, .doc h5, .doc h6 { font-size: 16px; margin: 1.2em 0 .3em; }
/* Prose sentence the doc author typed as a heading: keep it in the outline
   (and keep its anchor id) but drop the default bold heading weight so it
   reads as body text. */
.doc #h\.bguqx29wxh6h { font-weight: 400; }
.doc h1.subtitle, .doc h2.subtitle { border-bottom: none; font-weight: 500;
  color: var(--muted); font-size: 18px; }
/* Paragraphs sit tight like in the Google Doc: a single Enter just moves to
   the next line; the doc author's deliberate empty paragraphs render as
   <p class="gap"> below — one visible blank line between blocks. */
.doc p { margin: 0; line-height: 1.6; }
.doc ul, .doc ol { padding-left: 1.6em; margin: .6em 0; }
.doc li { margin: .25em 0; }
.doc code { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: .88em; background: var(--code-bg); padding: .1em .35em; border-radius: 4px; }
.doc a { color: var(--accent) !important; }
.doc a span { color: inherit !important; }
.doc a code { color: inherit; }
.doc table { border-collapse: collapse; margin: 1em 0; max-width: 100%;
  display: block; overflow-x: auto; }
.doc td, .doc th { border: 1px solid var(--table-border); padding: 6px 10px;
  vertical-align: top; }
.doc img { max-width: 100%; height: auto; }
.doc hr { border: none; border-top: 1px solid var(--border); margin: 1.4em 0; }
.doc details.toc { border: 1px solid var(--border); border-radius: 8px;
  margin: 1em 0; }
.doc details.toc summary { cursor: pointer; padding: 8px 14px; font-weight: 600;
  user-select: none; }
.doc details.toc ul { list-style: none; margin: 0; padding: 0 18px 12px;
  columns: 2; column-gap: 2em; }
.doc details.toc li { margin: .2em 0; font-size: 14px; }
@media (max-width: 700px) { .doc details.toc ul { columns: 1; } }

.section-list { display: grid; gap: 10px; margin-top: 6px; }
.section-card { display: flex; align-items: baseline; gap: 8px;
  border: 1px solid var(--border); border-radius: 10px; padding: 10px 14px; color: var(--fg); }
.section-card:hover { border-color: var(--accent); text-decoration: none; }
.section-card .t { flex: 1 1 auto; min-width: 0; font-weight: 600;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.section-card .n { flex: 0 0 auto; font-size: 12px; color: var(--muted); white-space: nowrap; }
.card-children { padding: 2px 14px 10px; font-size: 13px; color: var(--muted); }
.card-children a { display: block; white-space: nowrap; overflow: hidden;
  text-overflow: ellipsis; padding: 1px 0; }

.section-list { margin-top: 30px; padding-top: 16px; border-top: 1px solid var(--border); }
.section-list-h { display: block; font-size: 12px; font-weight: 700;
  text-transform: uppercase; letter-spacing: .04em; color: var(--muted);
  margin-bottom: 6px; }
.section-list ul { list-style: none; margin: 0; padding: 0; columns: 2; }
.section-list li { margin: 3px 0; break-inside: avoid; }
@media (max-width: 700px) { .section-list ul { columns: 1; } }
.empty-note { color: var(--muted); font-style: italic; }
.index-link { margin-top: 32px; padding-top: 16px; border-top: 1px solid var(--border); }
.index-link a { color: var(--accent); font-size: 14px; }
.pager { display: flex; justify-content: space-between; gap: 10px;
  margin-top: 48px; padding-top: 16px; border-top: 1px solid var(--border); }
.pager a { border: 1px solid var(--border); border-radius: 8px; padding: 8px 14px;
  color: var(--fg); }
.pager a:hover { border-color: var(--accent); text-decoration: none; }
.pager .next { margin-left: auto; }

footer { margin-top: 48px; font-size: 13px; color: var(--muted); }

@media (max-width: 900px) {
  .brand-logo { width: 26px; height: 26px; }
  .brand-text { display: none; }
  #nav-toggle { display: block; }
  .sidebar { position: fixed; top: 49px; bottom: 0; left: 0; z-index: 20;
    transform: translateX(-100%); transition: transform .18s ease; max-height: none;
    width: min(380px, 86vw); flex-basis: auto; }
  .sidebar.open { transform: translateX(0); }
  .content { padding: 20px 18px 60px; max-width: none; margin: 0;
    border-radius: 0; box-shadow: none; border-left: none; border-right: none; }
}
@media print {
  .topbar, .sidebar, .pager, .crumbs { display: none !important; }
  .content { max-width: none; padding: 0; margin: 0;
    background: none; border: none; box-shadow: none; }
}
"""

APP_JS = """\
(function () {
  // in-page highlight: landing here with ?q=terms (from a search result
  // click) marks every occurrence of the terms in the document text
  function findWord(lower, tk, from) {
    var i = lower.indexOf(tk, from || 0);
    while (i !== -1) {
      var b = i === 0 || !/[a-z0-9]/.test(lower.charAt(i - 1));
      var e = i + tk.length >= lower.length ||
        !/[a-z0-9]/.test(lower.charAt(i + tk.length));
      if (b && e) return i;
      i = lower.indexOf(tk, i + 1);
    }
    return -1;
  }
  var m = location.search.match(/[?&]q=([^&]+)/);
  if (m) {
    var terms = decodeURIComponent(m[1].replace(/\+/g, " ")).toLowerCase()
      .split(/\s+/).filter(Boolean);
    var root = document.querySelector(".content");
    if (terms.length && root) {
      var nodes = [];
      var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null);
      while (walker.nextNode()) nodes.push(walker.currentNode);
      for (var i = 0; i < nodes.length; i++) {
        var node = nodes[i];
        var txt = node.nodeValue;
        if (!txt) continue;
        var lower = txt.toLowerCase();
        var hit = false;
        for (var tk = 0; tk < terms.length; tk++) {
          if (findWord(lower, terms[tk], 0) !== -1) { hit = true; break; }
        }
        if (!hit) continue;
        var frag = document.createDocumentFragment();
        var pos = 0, len = txt.length;
        while (pos < len) {
          var best = -1, bl = 0;
          for (tk = 0; tk < terms.length; tk++) {
            var k = findWord(lower, terms[tk], pos);
            if (k >= 0 && (best < 0 || k < best)) { best = k; bl = terms[tk].length; }
          }
          if (best < 0) { frag.appendChild(document.createTextNode(txt.slice(pos))); break; }
          if (best > pos) frag.appendChild(document.createTextNode(txt.slice(pos, best)));
          var mk = document.createElement("mark");
          mk.textContent = txt.slice(best, best + bl);
          frag.appendChild(mk);
          pos = best + bl;
        }
        node.parentNode.replaceChild(frag, node);
      }
    }
  }
})();

(function () {
  "use strict";
  var data = null;
  var results = [];

  function qs(sel) { return document.querySelector(sel); }

  // whole-word (case-insensitive) matching: "bas" must not match "based"
  function findWord(lower, tk, from) {
    var i = lower.indexOf(tk, from || 0);
    while (i !== -1) {
      var b = i === 0 || !/[a-z0-9]/.test(lower.charAt(i - 1));
      var e = i + tk.length >= lower.length ||
        !/[a-z0-9]/.test(lower.charAt(i + tk.length));
      if (b && e) return i;
      i = lower.indexOf(tk, i + 1);
    }
    return -1;
  }

  // --- theme toggle (persisted in localStorage; default dark) -------------
  var themeBtn = qs("#theme-toggle");
  if (themeBtn) {
    function applyTheme(t) {
      document.documentElement.setAttribute("data-theme", t);
      themeBtn.textContent = t === "light" ? "☀" : "☾";
      try { localStorage.setItem("doc-theme", t); } catch (e) {}
    }
    themeBtn.addEventListener("click", function () {
      var cur = document.documentElement.getAttribute("data-theme");
      applyTheme(cur === "light" ? "dark" : "light");
    });
    applyTheme(document.documentElement.getAttribute("data-theme") || "dark");
  }



  // --- truncated-title preview -------------------------------------------
  var pv = document.createElement("div");
  pv.className = "toc-preview";
  document.body.appendChild(pv);
  var isTrunc = function (el) { return el.scrollWidth > el.clientWidth + 1; };
  function showPreview(a) {
    pv.textContent = a.textContent.replace(/\s+/g, " ").trim();
    pv.classList.add("show");
    var r = a.getBoundingClientRect();
    var pw = pv.offsetWidth, ph = pv.offsetHeight;
    var left = Math.max(8, r.right + 10);
    if (left + pw > window.innerWidth - 8) left = Math.max(8, r.left - pw - 10);
    pv.style.left = left + "px";
    pv.style.top = Math.max(8, Math.min(r.top, window.innerHeight - ph - 8)) + "px";
  }
  function hidePreview() { pv.classList.remove("show"); }
  document.querySelectorAll(".toc a").forEach(function (a) {
    a.addEventListener("mouseenter", function () { if (isTrunc(a)) showPreview(a); });
    a.addEventListener("mouseleave", hidePreview);
    a.addEventListener("focus", function () { if (isTrunc(a)) showPreview(a); });
    a.addEventListener("blur", hidePreview);
  });
  var sbEl = qs(".sidebar");
  if (sbEl) sbEl.addEventListener("scroll", hidePreview);
  window.addEventListener("resize", hidePreview);

  // sidebar toggle (mobile)
  var toggle = qs("#nav-toggle");
  if (toggle) {
    toggle.addEventListener("click", function () {
      var sb = qs(".sidebar");
      if (sb) sb.classList.toggle("open");
    });
  }

  // --- search -------------------------------------------------------------
  var input = qs("#search");
  if (input) {
    var backdrop = document.createElement("div");
    backdrop.id = "search-backdrop";
    document.body.appendChild(backdrop);
    var box = document.createElement("div");
    box.id = "search-results";
    document.body.appendChild(box);

    fetch("data.json").then(function (r) { return r.json(); }).then(function (d) {
      data = d;
      d.sections.forEach(function (s) {
        var hay = (s.tabTitle + " " + s.title + " " +
          (s.subs || []).map(function (x) { return x.title; }).join(" ") + " "
          + (s.full || s.text || "")).toLowerCase();
        s._hay = hay;
      });
    }).catch(function () {});

    function escHtml(t) {
      var d = document.createElement("div"); d.textContent = t; return d.innerHTML;
    }
    function mark(txt, tokens) {
      var low = txt.toLowerCase();
      var runs = [];
      tokens.forEach(function (tk) {
        if (!tk) return;
        var i = findWord(low, tk, 0);
        while (i !== -1) {
          runs.push([i, i + tk.length]);
          i = findWord(low, tk, i + tk.length);
        }
      });
      if (!runs.length) return escHtml(txt);
      runs.sort(function (x, y) { return x[0] - y[0]; });
      var merged = [runs[0].slice()];
      for (var j = 1; j < runs.length; j++) {
        var m = merged[merged.length - 1];
        if (runs[j][0] <= m[1]) m[1] = Math.max(m[1], runs[j][1]);
        else merged.push(runs[j].slice());
      }
      var out = "", p = 0;
      merged.forEach(function (r) {
        out += escHtml(txt.slice(p, r[0]));
        out += "<mark>" + escHtml(txt.slice(r[0], r[1])) + "</mark>";
        p = r[1];
      });
      return out + escHtml(txt.slice(p));
    }
    // a short snippet of fullText built around the first place a token occurs
    function context(full) {
      var lower = (full || "").toLowerCase();
      var idx = -1;
      (tokensOrEmpty()).forEach(function (tk) {
        var k = findWord(lower, tk, 0);
        if (k >= 0 && (idx < 0 || k < idx)) idx = k;
      });
      if (idx < 0) return mark((full || "").slice(0, 160), tokensOrEmpty());
      var a = Math.max(0, idx - 70);
      var b = Math.min(full.length, idx + 110);
      return (a > 0 ? "…" : "") + mark(full.slice(a, b), tokensOrEmpty())
        + (b < full.length ? "…" : "");
    }
    function tokensOrEmpty() {
      return input.value.trim().toLowerCase().split(/\s+/).filter(Boolean);
    }
    var tokens = [];
    // total occurrences of every token in a section's full text
    function countTokens(full, tks) {
      var lo = (full || "").toLowerCase(), total = 0;
      tks.forEach(function (tk) {
        if (!tk) return;
        var i = findWord(lo, tk, 0), tl = tk.length;
        while (i !== -1) { total++; i = findWord(lo, tk, i + tl); }
      });
      return total;
    }
    // id of the sub-heading under which the first token match falls
    function anchorAt(full, s, tks) {
      var lo = (full || "").toLowerCase(), idx = -1;
      tks.forEach(function (tk) {
        if (!tk) return;
        var k = findWord(lo, tk, 0);
        if (k >= 0 && (idx < 0 || k < idx)) idx = k;
      });
      if (idx < 0) return null;
      var best = null;
      (s.anchors || []).forEach(function (a) { if (a.o <= idx) best = a.id; });
      return best;
    }

    function render(list, totalHits) {
      box.innerHTML = "";
      var banner = document.createElement("div");
      banner.className = "sr-count";
      banner.textContent = list.length
        ? totalHits + " hit" + (totalHits === 1 ? "" : "s") + " in "
          + list.length + " section" + (list.length === 1 ? "" : "s")
        : "No matches";
      box.appendChild(banner);
      list.forEach(function (s) {
        var a = document.createElement("a");
        a.className = "sr";
        var aid = anchorAt(s.full || "", s, tokens);
        var qs = "?q=" + encodeURIComponent(input.value.trim());
        // the front section is served from the site root, which is its
        // canonical URL; every other page uses its own slug
        a.href = (s.slug === (data && data.home) ? "/" : s.slug)
          + qs + (aid ? "#" + aid : "");
        var t = document.createElement("span");
        t.className = "t";
        t.innerHTML = mark(s.title, tokens);
        var tab = document.createElement("span");
        tab.className = "tab";
        tab.textContent = s.tabTitle;
        var hits = countTokens(s.full || "", tokens);
        var cnt = document.createElement("span");
        cnt.className = "sr-hits";
        cnt.textContent = hits + (hits === 1 ? " hit" : " hits");
        var sn = document.createElement("span");
        sn.className = "sn";
        sn.innerHTML = context(s.full || "");
        a.appendChild(t); a.appendChild(tab); a.appendChild(cnt); a.appendChild(sn);
        box.appendChild(a);
      });
    }

    // --- results shown in a modal anchored under the search field --------
    function positionPanel() {
      var r = input.getBoundingClientRect();
      var w = Math.max(r.width, 360);
      w = Math.min(w, window.innerWidth - 24);
      var left = Math.max(12, Math.min(r.left, window.innerWidth - w - 12));
      box.style.left = left + "px";
      box.style.top = (r.bottom + 8) + "px";
      box.style.width = w + "px";
    }
    function showPanel() {
      positionPanel();
      box.style.display = "block";
      backdrop.style.display = "block";
    }
    function hidePanel() {
      box.style.display = "none";
      backdrop.style.display = "none";
    }

    function search() {
      var val = input.value.trim().toLowerCase();
      if (!val || !data) { box.innerHTML = ""; hidePanel(); return; }
      tokens = val.split(/\s+/);
      var out = [];
      data.sections.forEach(function (s) {
        if (tokens.every(function (tk) { return findWord(s._hay, tk, 0) !== -1; })) {
          var title = s.title.toLowerCase();
          var score = 0;
          if (findWord(title, val, 0) === 0) score -= 200;
          else if (title.indexOf(val) !== -1) score -= 100;
          score += findWord(s._hay, tokens[0], 0);
          // a page carrying more hits is more relevant than one that only
          // mentions the term once, which matters now that every heading is
          // its own (small) page
          score -= countTokens(s.full || "", tokens);
          out.push({ s: s, score: score });
        }
      });
      out.sort(function (a, b) { return a.score - b.score; });
      var top = out.slice(0, 25).map(function (o) { return o.s; });
      var total = 0;
      top.forEach(function (s) { total += countTokens(s.full || "", tokens); });
      render(top, total);
      showPanel();
    }

    input.addEventListener("input", search);
    input.addEventListener("focus", function () {
      if (input.value.trim() && data) showPanel();
    });
    backdrop.addEventListener("click", hidePanel);
    document.addEventListener("click", function (e) {
      if (box.style.display !== "none"
          && !box.contains(e.target) && e.target !== input
          && e.target !== backdrop) hidePanel();
    });
    window.addEventListener("resize", function () {
      if (box.style.display !== "none") positionPanel();
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "/" && document.activeElement !== input) {
        e.preventDefault(); input.focus();
      }
      if (e.key === "Escape") { input.value = ""; search(); input.blur(); hidePanel(); }
    });
  }
})();

(function () {
  // --- index navigation: highlight the chosen entry in the left index and
  // keep it visible (scroll the index panel if the entry sits outside its
  // current viewport) whenever the page lands on a #anchor deep link.
  function qs(sel) { return document.querySelector(sel); }
  function syncNav() {
    var sb = qs(".sidebar");
    if (!sb) return;
    var old = sb.querySelector("a.nav-active");
    if (old) old.classList.remove("nav-active");
    var hash = location.hash;
    var target = null;
    if (hash && hash.length > 1) {
      var hid = hash.slice(1);
      sb.querySelectorAll("a[href]").forEach(function (a) {
        if (target) return;
        var href = a.getAttribute("href") || "";
        var i = href.indexOf("#");
        if (i >= 0 && href.slice(i + 1) === hid) target = a;
      });
    }
    // no matching #anchor (a plain section link, or a heading the outline
    // hides): fall back to the entry for this page, so the index still
    // scrolls to the chosen position instead of jumping back to the top.
    if (!target) target = sb.querySelector("a.current");
    if (!target) return;
    target.classList.add("nav-active");
    // reveal the highlighted row inside the index's own scroll area
    var pad = 14;
    var sTop = sb.getBoundingClientRect().top;
    var sBot = sb.getBoundingClientRect().bottom;
    var tTop = target.getBoundingClientRect().top;
    var tBot = target.getBoundingClientRect().bottom;
    if (tTop < sTop + pad) {
      sb.scrollTop += tTop - sTop - pad;
    } else if (tBot > sBot - pad) {
      sb.scrollTop += tBot - sBot + pad;
    }
  }
  window.addEventListener("hashchange", syncNav);
  syncNav();
})();

(function () {
  // --- forwarding for pre-split heading links ------------------------------
  // Headings used to render inside their section's page, so an old link such
  // as example.com/#h.abc123 points at the document root. Every heading is a
  // page of its own now; when the anchor is not on the page we landed on, look
  // it up in the generated map and forward, keeping the anchor.
  var hash = location.hash;
  if (!hash || hash.length < 2 || hash.indexOf("#h.") !== 0) return;
  var id = hash.slice(1);
  if (document.getElementById(id)) return;
  fetch("anchors.json").then(function (r) {
    return r.ok ? r.json() : null;
  }).then(function (map) {
    var hit = map && map[id];
    if (!hit) return;
    var here = document.body.getAttribute("data-page") || "";
    if (hit.slug === here) return;  // already on the page that owns it
    location.replace(hit.href);
  }).catch(function () {});
})();
"""


def news_section_slug(site):
    """Slug of the dated news section (`edit. DD.MM.YY`), if the doc has one.

    It is the page the author retitles every morning, so it is the one whose
    slug must not follow its heading (see NEWS_SLUG)."""
    for sec in site.sections:
        if sec.get("dated_news"):
            return sec["slug"]
    return None


def render_404_page(site):
    """A real 404 page. Without one, Cloudflare Pages serves the homepage
    (HTTP 200) for any unknown path, so a link shared to the daily-renamed
    'edit. DD.MM.YY' news section silently shows the wrong snapshot. This
    page forwards such dated links to the news section's page, which keeps a
    stable URL, so the forward target no longer changes daily. The #heading
    anchor is kept on the way through (heading ids survive the rename)."""
    news = news_section_slug(site)
    if news:
        # the news section is the site's front page, so it forwards to the
        # root; for a document where it sits elsewhere, to its own page
        to = ("dir" if news == home_section_slug(site)
              else "dir + '%s'" % news)   # `dir` already ends in a slash
        redirect_js = (
            "(function(){\n"
            "  var seg = location.pathname.replace(/.*\\//, '').replace(/\\.html$/, '');\n"
            "  if (/^edit-/.test(seg)) {\n"
            "    var dir = location.pathname.replace(/\\/[^/]*$/, '/');\n"
            "    var target = %(to)s + location.search + location.hash;\n"
            "    var msg = document.getElementById('msg');\n"
            "    if (msg) msg.textContent =\n"
            "      'This daily news section was renamed \u2014 redirecting to the current one\u2026';\n"
            "    setTimeout(function(){ location.replace(target); }, 300);\n"
            "  }\n"
            "})();"
        ) % {"to": to}
    else:
        redirect_js = ""
    return """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>Page not found · MSST Bible</title>
<link rel="icon" href="favicon.ico">
<style>
  :root { color-scheme: dark; }
  body { margin: 0; min-height: 100vh; display: flex; align-items: center;
         justify-content: center; font: 15px/1.6 Arial, Helvetica, sans-serif;
         background: #0d1117; color: #e6edf3; text-align: center; }
  .card { max-width: 460px; padding: 32px 36px; }
  h1 { font-size: 52px; margin: 0 0 6px; color: #8b949e; }
  p  { margin: 8px 0; }
  a  { color: #4493f8; }
  img { vertical-align: -4px; margin-right: 6px; }
</style>
</head>
<body>
<div class="card">
  <h1>404</h1>
  <p id="msg">This page doesn't exist &mdash; it may have been renamed in the document.</p>
  <p><img src="favicon.png" alt="" width="20" height="20"><a href="contents">Open the index</a></p>
</div>
%(script)s</body>
</html>
""" % {"script": (f"<script>\n{redirect_js}\n</script>\n"
                                     if redirect_js else "")}


def canonical_url(site, slug):
    """The one address that should be indexed for a page.

    The news section is also what the site serves from its root, so its own
    page (`/news`) declares `/` as canonical instead of competing with it,
    and neither the sitemap nor the feed advertises the second URL.
    """
    base = (getattr(site, "base_url", "") or "").rstrip("/")
    if not base or not slug:
        return ""
    if slug == home_section_slug(site):
        return base + "/"
    return base + "/" + slug


def page_template(site, title, sidebar, body, active_slug, canon_slug=None):
    meta = f"{len(site.sections)} sections · generated {site.generated}"
    canon = canonical_url(site, active_slug if canon_slug is None else canon_slug)
    canon_link = f'<link rel="canonical" href="{attr(canon)}">\n' if canon else ""
    feed_link = ('<link rel="alternate" type="application/rss+xml" title="RSS feed" '
                 'href="feed.xml">\n') if getattr(site, "base_url", "") else ""
    gsc_meta = (f'<meta name="google-site-verification" '
                f'content="{GSC_VERIFICATION}">\n') if GSC_VERIFICATION else ""
    apple_icon = ('<link rel="apple-touch-icon" href="favicon.png">\n'
                  if getattr(site, "apple_icon", False) else "")
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
{canon_link}{feed_link}{gsc_meta}<link rel="icon" href="favicon.ico">
{apple_icon}<link rel="stylesheet" href="assets/style.css">
<script>(function(){{var t;try{{t=localStorage.getItem("doc-theme");}}catch(e){{}}document.documentElement.setAttribute("data-theme",t||"dark");}})();</script>
</head>
<body data-page="{attr(active_slug or '')}">
<header class="topbar">
<button id="nav-toggle" aria-label="Toggle navigation">☰</button>
<a class="brand" href="/"><img class="brand-logo"
  src="favicon.png" alt="site logo" width="22" height="22"><span
  class="brand-text">{esc(site.title)}</span></a>
<input id="search" type="search" placeholder="Search this document…" autocomplete="off">
<button id="theme-toggle" aria-label="Toggle color theme" title="Toggle light/dark theme">☾</button>
</header>
<div class="layout">
<nav class="sidebar">{sidebar}</nav>
<main class="content">{body}
<footer>Mirror of <a href="https://docs.google.com/document/d/{attr(site.doc_id)}/edit">the Google Doc</a> — {esc(meta)}</footer>
</main>
</div>
<script src="assets/app.js"></script>
</body>
</html>
"""


def home_section_slug(site):
    """Slug of the section the site serves as its front page (index.html).

    `render_index` renders that section's body into index.html, so outline
    links to it can use the site root (/#h.…) rather than a page of its own:
    the site's front page is the root, and the news section at the top of the
    document is its front page.
    """
    return site.sections[0]["slug"] if site.sections else None


def strip_own_heading(sec):
    """The section's blocks without its own title heading (that heading is
    already shown as the page title), skipping any leading document front
    matter (a Title/Subtitle line) that precedes it."""
    blocks = list(sec["blocks"])
    i = 0
    while i < len(blocks) and blocks[i].get("doc_meta"):
        i += 1
    if (i < len(blocks) and blocks[i]["type"] == "heading"
            and blocks[i].get("heading_id") == sec.get("heading_id")):
        del blocks[i]
    return blocks


def toc_href(home, slug, hid=None):
    """Href for a page (or one of its headings) in the outline.

    Extension-less, which is the URL Cloudflare Pages actually serves: it
    308-redirects `/page.html` to `/page`, so linking the `.html` form would
    cost every click a redirect hop.
    """
    if slug and slug == home:
        return f"/#{hid}" if hid else "/"
    return f"{slug}#{hid}" if hid else slug


def _children_map(site):
    """parent slug (None for the roots) -> child pages, document order."""
    children = {}
    for s in site.sections:
        children.setdefault(s.get("parent"), []).append(s)
    return children


def _outline_node(site, s, home, children, active_slug, depth):
    """One nested, fully-expanded outline entry (like Google's "Document
    tabs"): the page itself plus its child pages, recursively."""
    cls = ' current' if s["slug"] == active_slug else ""
    root = ' toc-section' if depth == 0 else ' toc-sub'
    link = (f'<a class="{root.strip()}{cls}" href="{toc_href(home, s["slug"])}">'
            f'{esc(nav_title(s["title"], s.get("heading_id")))}</a>')
    kids = children.get(s["slug"], [])
    if not kids:
        return f'<li>{link}</li>'
    inner = "".join(_outline_node(site, k, home, children, active_slug, depth + 1)
                    for k in kids)
    return (f'<li class="toc-item">{link}'
            f'<ul class="toc-subs open">{inner}</ul></li>')


def sidebar_html(site, active_slug=None):
    home = home_section_slug(site)
    children = _children_map(site)
    p = ['<div class="sidebar-inner">']
    if len(site.tabs) > 1:
        p.append('<div class="tab-chips">')
        for t in site.tabs:
            first = site.tab_first.get(t["id"])
            href = toc_href(home, first) if first else site.tab_url(t["id"])
            p.append(f'<a class="chip" href="{href}">{esc(t["title"])}</a>')
        p.append('</div>')
    p.append('<ul class="toc">')
    for t in site.tabs:
        roots = [s for s in children.get(None, []) if s["tab"] == t["id"]]
        if not roots:
            continue
        # The first tab corresponds to the front/index page, so label its TOC
        # group INDEX instead of repeating the site title.
        label = "INDEX" if t is site.tabs[0] else t["title"]
        p.append(f'<li class="toc-group"><span class="toc-tab">{esc(label)}</span><ul>')
        for s in roots:
            p.append(_outline_node(site, s, home, children, active_slug, 0))
        p.append('</ul></li>')
    p.append('</ul>')
    p.append(f'<div class="sidebar-foot"><a href="/">Index</a> · '
             f'<a href="https://docs.google.com/document/d/{attr(site.doc_id)}/edit">Google Doc ↗</a></div>')
    p.append('</div>')
    return "".join(p)


def snippet(section, limit=400):
    parts = []
    for b in section["blocks"]:
        if b["type"] in ("para", "heading"):
            t = text_of_runs(b["runs"])
            if t:
                parts.append(t)
        elif b["type"] == "list":
            for it in b["items"]:
                t = text_of_runs(it["runs"])
                if t:
                    parts.append(t)
        elif b["type"] == "html":
            parts.append(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", b["html"])).strip())
    return " ".join(parts)[:limit]


def section_full_text(section):
    """Full plain text of a section, plus the character offset where each
    sub-heading starts. Search uses this to deep-link a hit to the heading
    it falls under instead of just the top of the section page."""
    segs = []
    heads = []  # (index into segs, heading id, title)

    def add(t, hid=None, htitle=None):
        t = (t or "").strip()
        if not t:
            return
        segs.append(t)
        if hid:
            heads.append((len(segs) - 1, hid, htitle))

    def walk(blocks):
        for b in blocks:
            t = b["type"]
            if t == "para":
                add(text_of_runs(b.get("runs", [])))
            elif t == "heading":
                ht = text_of_runs(b.get("runs", []))
                add(ht, b.get("heading_id"), ht)
            elif t == "list":
                for it in b.get("items", []):
                    add(text_of_runs(it.get("runs", [])))
            elif t == "table":
                for row in b.get("rows", []):
                    for cell in row:
                        walk(cell)
            elif t == "toc":
                walk(b.get("blocks", []))
            elif t == "html":
                add(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", b["html"])).strip())

    walk(section["blocks"])
    text = " ".join(segs)
    anchors = [
        {"o": sum(len(segs[j]) for j in range(idx)) + idx,
         "id": aid, "t": htitle}
        for idx, aid, htitle in heads
    ]
    return text, anchors


# ---------------------------------------------------------------------------
# site writing
# ---------------------------------------------------------------------------

def page_head(site, i, sec):
    """Breadcrumb chain, page heading (with its anchor id) and meta line."""
    home = home_section_slug(site)
    by_slug = {s["slug"]: s for s in site.sections}
    chain, slug = [], sec.get("parent")
    while slug:
        p = by_slug.get(slug)
        if p is None:
            break
        chain.append(p)
        slug = p.get("parent")
    crumbs = [f'<a href="/">{esc(site.title)}</a>']
    if sec["tab_title"] and sec["tab_title"] != site.title:
        crumbs.append(f'<span>{esc(sec["tab_title"])}</span>')
    for p in reversed(chain):
        crumbs.append(f'<a href="{toc_href(home, p["slug"])}">'
                      f'{esc(nav_title(p["title"], p.get("heading_id")))}</a>')
    out = ['<nav class="crumbs">' + " › ".join(crumbs) + '</nav>']
    hid = sec.get("heading_id")
    idattr = f' id="{attr(hid)}"' if hid else ""
    out.append(f'<h1{idattr}>{esc(sec["title"])}</h1>')
    out.append(f'<p class="meta">Page {i + 1} of {len(site.sections)} · '
               f'<a href="{attr(site.edit_url(sec))}" target="_blank" '
               f'rel="noopener">Open the original GDoc ↗</a></p>')
    return out


def page_pager(site, i, home):
    """Prev/next links walking the document in reading order."""
    total = len(site.sections)
    pager = ['<nav class="pager">']
    if i > 0:
        prev = site.sections[i - 1]
        pager.append(f'<a class="prev" href="{toc_href(home, prev["slug"])}">'
                     f'← {esc(nav_title(prev["title"], prev.get("heading_id")))}</a>')
    if i + 1 < total:
        nxt = site.sections[i + 1]
        pager.append(f'<a class="next" href="{toc_href(home, nxt["slug"])}">'
                     f'{esc(nav_title(nxt["title"], nxt.get("heading_id")))} →</a>')
    pager.append('</nav>')
    return "".join(pager)


def section_list_html(site, sec, home):
    """Links to the pages directly under this one.

    Empty when the page has no child pages, so a page whose heading introduces
    nothing shows no generated list at all."""
    kids = [s for s in site.sections if s.get("parent") == sec["slug"]]
    if not kids:
        return []
    out = ['<nav class="section-list"><span class="section-list-h">'
           'In this section</span><ul>']
    for k in kids:
        out.append(f'<li><a href="{toc_href(home, k["slug"])}">'
                   f'{esc(nav_title(k["title"], k.get("heading_id")))}</a></li>')
    out.append('</ul></nav>')
    return out


def page_body_html(site, sec):
    """The page's own text. A heading can legitimately have none of its own
    (the doc has a few such titles); say so instead of showing a blank page."""
    blocks = strip_own_heading(sec)
    if not blocks:
        return ['<p class="empty-note">This heading has no text of its own in '
                'the document.</p>']
    return ['<div class="doc">', site.render_blocks(blocks), '</div>']


def render_index(site):
    """The front page: the document's opening section (its front matter and
    its own text) plus links to the sections directly under it."""
    if not site.sections:
        return page_template(site, site.title, sidebar_html(site), '<p>No content.</p>', None)
    home = home_section_slug(site)
    first = site.sections[0]
    body = page_head(site, 0, first)
    body.extend(page_body_html(site, first))
    body.extend(section_list_html(site, first, home))
    body.append('<div class="index-link"><a href="contents">Full table of contents →</a></div>')
    return page_template(site, site.title, sidebar_html(site, first["slug"]),
                         chr(10).join(body), first["slug"])


def render_contents(site):
    """The index page: every page of the document, nested in reading order,
    mirroring the Google Doc's "Document tabs" panel."""
    home = home_section_slug(site)
    children = _children_map(site)
    body = ['<h1>Contents</h1>']
    body.append('<p class="outline-hint">Every heading in the document, in reading '
                'order — each one is a page of its own.</p>')
    body.append('<ul class="toc toc-full">')
    multi = len(site.tabs) > 1
    for t in site.tabs:
        roots = [s for s in children.get(None, []) if s["tab"] == t["id"]]
        if not roots:
            continue
        if multi:
            body.append(f'<li class="toc-group"><span class="toc-tab">'
                        f'{esc(t["title"])}</span><ul class="toc-subs open">')
        for s in roots:
            body.append(_outline_node(site, s, home, children, None, 0))
        if multi:
            body.append('</ul></li>')
    body.append('</ul>')
    return page_template(site, site.title, sidebar_html(site), "\n".join(body),
                         None, canon_slug="contents")


def render_section_page(site, i, sec):
    home = home_section_slug(site)
    body = page_head(site, i, sec)
    # the page title is already shown as <h1>; skip the page's own title
    # heading block so it isn't repeated inside the body
    body.extend(page_body_html(site, sec))
    body.extend(section_list_html(site, sec, home))
    body.append(page_pager(site, i, home))
    return page_template(site, f"{sec['title']} — {site.title}",
                         sidebar_html(site, sec["slug"]), "\n".join(body), sec["slug"])


def anchors_json(site):
    """heading id -> the page (and anchor) that owns it.

    Headings used to live inside their section's page, so a shared
    `example.com/#h.xyz` link pointed at the document root. Now that every
    heading is a page, such a link can land on a page where the anchor no
    longer exists; the client fetches this map and forwards it.
    """
    home = home_section_slug(site)
    out = {}
    for hid, (slug, _frag) in site.heading_page.items():
        out[hid] = {"slug": slug, "href": toc_href(home, slug, hid)}
    return json.dumps(out, ensure_ascii=False)


def data_json(site):
    return json.dumps({
        "title": site.title,
        "doc": site.doc_id,
        "generated": site.generated,
        "source": site.source,
        # the section served from the site root: search links to it use `/`
        # rather than its own slug, which is only an alias
        "home": home_section_slug(site) or "",
        "tabs": [{"id": t["id"], "title": t["title"]} for t in site.tabs],
        "sections": [
            {
                "slug": s["slug"],
                "title": s["title"],
                "tab": s["tab"],
                "tabTitle": s["tab_title"],
                "headingId": s.get("heading_id"),
                "parent": s.get("parent"),
                "depth": s.get("depth", 0),
                "subs": s.get("subs", []),
                "text": s.get("text", ""),
                # full plain text + sub-heading offsets, used as the search index
                "full": s.get("full", ""),
                "anchors": s.get("anchors", []),
            }
            for s in site.sections
        ],
    }, ensure_ascii=False)


def _rfc2822():
    import email.utils
    return email.utils.formatdate(time.time(), usegmt=True)


def _esc_xml(s):
    from xml.sax.saxutils import escape
    return escape(s)


def write_seo_files(site, out, base_url):
    """Emit sitemap.xml and an RSS 2.0 feed (feed.xml). Requires base_url."""
    if not base_url:
        return False
    base_url = base_url.rstrip("/")
    stamp = _rfc2822()

    # Extension-less, matching the URLs Cloudflare Pages serves (it redirects
    # the .html form), so the sitemap lists canonical URLs — one per page.
    # The front section's own page is left out: it is the same content as the
    # root, which is listed first.
    home = home_section_slug(site)
    urls = [""] + [s["slug"] for s in site.sections if s["slug"] != home]
    locs = "\n".join(
        "  <url><loc>%s</loc></url>" % _esc_xml(base_url + "/" + u)
        for u in urls)
    sitemap = ('<?xml version="1.0" encoding="UTF-8"?>\n'
               '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
               + locs + "\n</urlset>\n")
    with open(os.path.join(out, "sitemap.xml"), "w", encoding="utf-8") as f:
        f.write(sitemap)

    # the feed carries the top-level sections only: one entry per page would
    # bury subscribers under the document's whole heading tree
    feed_sections = [s for s in site.sections if not s.get("parent")]
    items = []
    for s in feed_sections or site.sections:
        loc = base_url + ("/" if s["slug"] == home else "/" + s["slug"])
        desc = _esc_xml((s.get("text") or "")[:300])
        # The news page keeps one stable URL, but its heading is retitled every
        # day, so its link still lands somewhere permanent. Its guid has to move
        # with the date all the same: readers treat an unchanged guid as the
        # same item, and the daily update would stop reaching them.
        if s.get("dated_news"):
            guid = '    <guid isPermaLink="false">%s</guid>\n' % _esc_xml(
                f"{s['slug']}:{slugify(s['title'])}")
        else:
            guid = '    <guid isPermaLink="true">%s</guid>\n' % _esc_xml(loc)
        items.append(
            "  <item>\n"
            "    <title>%s</title>\n" % _esc_xml(s["title"])
            + "    <link>%s</link>\n" % _esc_xml(loc)
            + guid
            + "    <description>%s</description>\n" % desc
            + "    <pubDate>%s</pubDate>\n" % stamp
            + "  </item>")
    feed = ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">\n'
            "<channel>\n"
            "  <title>%s</title>\n" % _esc_xml(site.title)
            + "  <link>%s/</link>\n" % _esc_xml(base_url)
            + "  <description>Fast, readable mirror of a Google Doc — %s. </description>\n"
                % _esc_xml(site.title)
            + "  <language>en</language>\n"
            + "  <generator>doc-site (gdoc_site.py)</generator>\n"
            + "  <lastBuildDate>%s</lastBuildDate>\n" % stamp
            + "\n".join(items) + "\n"
            + "</channel>\n"
            + "</rss>\n")
    with open(os.path.join(out, "feed.xml"), "w", encoding="utf-8") as f:
        f.write(feed)
    return True


def write_site(site, out):
    os.makedirs(os.path.join(out, "assets"), exist_ok=True)
    # Self-host the favicon (if present next to the script) instead of
    # hotlinking it, so the mirror is self-contained.
    here = os.path.dirname(os.path.abspath(__file__))
    for fn in ("favicon.ico", "favicon.png"):
        src = os.path.join(here, fn)
        if os.path.exists(src):
            shutil.copy(src, os.path.join(out, fn))
    # Copy the Google Search Console HTML-file verification token (if present),
    # so it stays on the site root across every rebuild/deploy.
    gsc_file = os.path.join(here, "google590856c13a26a9ec.html")
    if os.path.exists(gsc_file):
        shutil.copy(gsc_file, os.path.join(out, "google590856c13a26a9ec.html"))
    # Always write a real robots.txt (otherwise Cloudflare serves the HTML
    # fallback for /robots.txt and Google's crawler finds no Sitemap directive).
    base_url = getattr(site, "base_url", "")
    robots = "User-agent: *\nAllow: /\n"
    if base_url:
        robots += ("Sitemap: %s/sitemap.xml\n" % base_url.rstrip("/"))
    with open(os.path.join(out, "robots.txt"), "w", encoding="utf-8") as f:
        f.write(robots)
    # Only link the high-res PNG icon when it's actually available.
    site.apple_icon = os.path.exists(os.path.join(here, "favicon.png"))
    with open(os.path.join(out, "assets", "style.css"), "w", encoding="utf-8") as f:
        f.write(STYLE_CSS)
    with open(os.path.join(out, "assets", "app.js"), "w", encoding="utf-8") as f:
        f.write(APP_JS)
    with open(os.path.join(out, "index.html"), "w", encoding="utf-8") as f:
        f.write(render_index(site))
    with open(os.path.join(out, "contents.html"), "w", encoding="utf-8") as f:
        f.write(render_contents(site))
    with open(os.path.join(out, "404.html"), "w", encoding="utf-8") as f:
        f.write(render_404_page(site))
    for i, sec in enumerate(site.sections):
        with open(os.path.join(out, f"{sec['slug']}.html"), "w", encoding="utf-8") as f:
            f.write(render_section_page(site, i, sec))
    with open(os.path.join(out, "data.json"), "w", encoding="utf-8") as f:
        f.write(data_json(site))
    # heading id -> the page that now owns that heading, so a link minted
    # before every heading became its own page (a bare /#h.… fragment) can
    # still be forwarded to the right page by the client
    with open(os.path.join(out, "anchors.json"), "w", encoding="utf-8") as f:
        f.write(anchors_json(site))
    if write_seo_files(site, out, getattr(site, "base_url", "")):
        print("wrote:      sitemap.xml, feed.xml")
    else:
        # remove any stale sitemap/feed from an earlier build with --base-url
        for stale in ("sitemap.xml", "feed.xml"):
            p = os.path.join(out, stale)
            if os.path.exists(p):
                os.remove(p)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--doc", help="Google Docs document ID (from the URL)")
    ap.add_argument("--source", choices=["api", "export", "file"],
                    help="default: api if auth.json exists, else export")
    ap.add_argument("--file", help="path to a saved documents.get JSON (for --source file)")
    ap.add_argument("--title", help="override the document title (export mode needs this)")
    ap.add_argument("--out", default="site", help="output directory (default: site)")
    ap.add_argument("--auth", action="store_true",
                    help="one-time OAuth authorization (saves --auth-file)")
    ap.add_argument("--client-json",
                    help="path to the client_secret_*.json downloaded from Google Cloud (for --auth)")
    ap.add_argument("--auth-file", default="auth.json",
                    help="where to store the OAuth token (default: auth.json)")
    ap.add_argument("--port", type=int, default=8912,
                    help="loopback port used by --auth (default: 8912)")
    ap.add_argument("--base-url",
                    help="public URL of the deployed site (e.g. https://example.com/) — "
                         "required to emit sitemap.xml and feed.xml")
    args = ap.parse_args(argv)

    if args.auth:
        if not args.client_json:
            ap.error("--auth needs --client-json PATH "
                     "(the client_secret_*.json from Google Cloud — see README.md)")
        if not os.path.exists(args.client_json):
            ap.error(f"--client-json file not found: {args.client_json}")
        auth = run_auth_flow(args.client_json, args.port)
        save_auth(args.auth_file, auth)
        print(f"Saved credentials to {args.auth_file}. You can now build with:")
        print(f"  python gdoc_site.py --doc <DOC_ID> --source api")
        return 0

    if not args.doc and args.source != "file":
        ap.error("--doc is required (except with --source file)")
    if args.source == "file" and not args.file:
        ap.error("--source file requires --file PATH")

    # Default to the fast, reliable export path. Tabs (via the API) are opt-in
    # with --source api; the API cannot serve very large documents and falls
    # back to export automatically when it times out.
    source = args.source or "export"

    if source == "api":
        auth = load_auth(args.auth_file)
        if not auth:
            ap.error("--source api needs authorization first:\n"
                     "  python gdoc_site.py --auth --client-json <client_secret_*.json>\n"
                     "(see README.md — Google no longer accepts API keys for the Docs API)")
        data = fetch_api(args.doc, auth)
        if data is not None:
            title, tabs = parse_document(data)
        else:
            sys.stderr.write("Falling back to the (flattened) HTML export.\n")
            html_text = fetch_export(args.doc)
            title, tabs = parse_export(html_text, title=args.title or SITE_TITLE,
                                       doc_id=args.doc)
            source = "export"
    elif source == "file":
        with open(args.file, encoding="utf-8") as f:
            data = json.load(f)
        title, tabs = parse_document(data)
        args.doc = args.doc or data.get("documentId", "")
    else:
        html_text = fetch_export(args.doc)
        title, tabs = parse_export(html_text, title=args.title or SITE_TITLE,
                                   doc_id=args.doc)

    if args.title:
        title = args.title

    site = Site(args.doc, title, tabs, source)
    site.base_url = args.base_url or ""
    for s in site.sections:
        s["text"] = snippet(s)
        s["full"], s["anchors"] = section_full_text(s)

    t0 = time.time()
    write_site(site, args.out)
    if not site.base_url:
        print("(pass --base-url https://your.domain/ to also emit sitemap.xml + feed.xml)")

    sizes = []
    for root, _, files in os.walk(args.out):
        for fn in files:
            sizes.append(os.path.getsize(os.path.join(root, fn)))
    total = sum(sizes)
    biggest = max(sizes) if sizes else 0
    print(f"title:      {site.title}")
    print(f"source:     {source}")
    print(f"tabs:       {len(site.tabs)}")
    print(f"sections:   {len(site.sections)}  ->  {args.out}/")
    print(f"pages:      {len(site.sections) + 1}")
    print(f"size:       {total / 1024:.0f} KiB total, largest page {biggest / 1024:.0f} KiB")
    print(f"time:       {time.time() - t0:.1f}s")
    print(f"\nPreview:  python serve.py --dir {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
