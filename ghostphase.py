#!/usr/bin/env python3
"""
GhostPhase — Universal File Extraction Engine

A completely novel approach to web scraping that works on nearly ANY website,
even behind authentication, paywalls, and anti-bot protections.

METHODOLOGY:
  Instead of scraping HTML like every other tool, GhostPhase uses 7 independent
  "phases" (extraction strategies) that attack the problem from fundamentally
  different angles. Each phase can independently discover and download files
  that the others miss.

PHASES:
  1. GHOST CACHE — Finds cached/mirrored copies via Google Cache, Coral CDN,
     archive.today, Wayback, and 12+ cache services. If the original is locked,
     the cache probably isn't.

  2. CDN RECON — Reverse-engineers CDN URLs. Most sites serve files from CDNs
     (Cloudflare, AWS S3, Azure Blob, GCS, Akamai, Fastly). CDN URLs are
     often directly accessible even when the website requires auth.

  3. API EXCAVATION — Discovers hidden API endpoints by analyzing JS bundles,
     network patterns, and common API structures. APIs often return direct
     file URLs without auth checks.

  4. SITEMAP SIEGE — Exhaustive crawl of robots.txt, sitemap.xml, sitemap_index.xml,
     and all their variants. Sitemaps are public and list every URL including
     files the UI doesn't link to.

  5. REFERRER BYPASS — Many "protected" downloads just check the Referer header.
     Spoofs legitimate referers to bypass download restrictions.

  6. EMBED EXTRACTION — Finds files embedded in pages via iframes, objects,
     embeds, video/audio players, PDF viewers, and JS media players. These
     often point to unlocked CDN URLs.

  7. DIRECTORY STORM — Brute-forces common directory structures, backup paths,
     admin panels, upload dirs, and version-numbered files.

Requires: pip install requests beautifulsoup4 rich
Optional: pip install cloudscraper (for Cloudflare bypass)
"""

import os
import re
import sys
import json
import time
import random
import hashlib
import argparse
import threading
from urllib.parse import urljoin, urlparse, unquote, quote, urlencode, parse_qs
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn
from rich import box

try:
    from bs4 import BeautifulSoup
    HAS_BS4 = True
except ImportError:
    HAS_BS4 = False

try:
    import cloudscraper
    HAS_CLOUDSCRAPER = True
except ImportError:
    HAS_CLOUDSCRAPER = False

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

console = Console()

DEFAULT_DEST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads", "ghostphase")


# ──────────────────────────────────────────────────────────────────────
# FILE TYPE EXTENSIONS
# ──────────────────────────────────────────────────────────────────────

VALUABLE_EXTENSIONS = {
    "audio": {"mp3", "flac", "wav", "ogg", "aac", "m4a", "wma", "opus", "aiff", "mid", "midi",
              "mod", "xm", "s3m", "it", "ra", "ram", "ape", "alac"},
    "video": {"mp4", "avi", "mkv", "wmv", "flv", "mov", "webm", "m4v", "mpg", "mpeg",
              "rm", "rmvb", "3gp", "ogv", "ts", "m3u8"},
    "image": {"png", "jpg", "jpeg", "gif", "bmp", "tiff", "psd", "svg", "webp", "raw",
              "cr2", "nef", "arw", "dng", "heic", "avif"},
    "document": {"pdf", "doc", "docx", "xls", "xlsx", "pptx", "ppt", "epub", "rtf",
                 "djvu", "chm", "odt", "ods", "odp", "tex", "mobi", "azw3"},
    "archive": {"zip", "rar", "7z", "tar", "gz", "bz2", "tgz", "xz", "lzh", "cab",
                "iso", "dmg", "img"},
    "software": {"exe", "msi", "apk", "deb", "rpm", "bin", "app", "appimage"},
    "flash": {"swf", "fla", "dcr", "dir"},
    "data": {"json", "xml", "csv", "sql", "db", "sqlite", "dat", "nfo", "txt", "log",
             "bak", "dump", "tar"},
    "crypto": {"wallet", "key", "pem", "p12", "pfx", "keystore", "seed", "priv", "gpg"},
    "credentials": {"env", "htpasswd", "htaccess", "npmrc", "netrc", "pgpass",
                    "credentials", "conf", "cfg", "ini"},
    "code": {"py", "js", "ts", "rb", "go", "rs", "java", "cpp", "c", "h", "php",
             "sh", "bat", "ps1"},
}

ALL_EXTENSIONS = set()
for exts in VALUABLE_EXTENSIONS.values():
    ALL_EXTENSIONS.update(exts)


# ──────────────────────────────────────────────────────────────────────
# DATA CLASSES
# ──────────────────────────────────────────────────────────────────────

@dataclass
class FileTarget:
    url: str
    extension: str = ""
    category: str = "other"
    source_phase: str = ""
    source_url: str = ""
    confidence: float = 0.5  # 0.0-1.0 how likely this is a real file
    content_length: int = 0
    downloaded: bool = False
    download_size: int = 0

    @property
    def filename(self):
        path = urlparse(self.url).path
        name = unquote(path.split("/")[-1].split("?")[0])
        if not name or name == "/":
            name = f"file_{hashlib.md5(self.url.encode()).hexdigest()[:8]}"
        if "." not in name and self.extension:
            name = f"{name}.{self.extension}"
        return re.sub(r'[<>:"/\\|?*]', "_", name).strip(". ")[:200]


# ──────────────────────────────────────────────────────────────────────
# SESSION MANAGER — Stealth HTTP client
# ──────────────────────────────────────────────────────────────────────

class StealthSession:
    """Multi-session HTTP client with anti-detection features."""

    USER_AGENTS = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:128.0) Gecko/20100101 Firefox/128.0",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    ]

    def __init__(self, proxy=None):
        self._sessions = []
        self._idx = 0
        self._lock = threading.Lock()

        retry = Retry(total=3, backoff_factor=1.0, status_forcelist=[500, 502, 503])
        adapter = HTTPAdapter(max_retries=retry, pool_connections=15, pool_maxsize=30)

        for ua in random.sample(self.USER_AGENTS, min(4, len(self.USER_AGENTS))):
            s = requests.Session()
            s.headers.update({
                "User-Agent": ua,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip, deflate, br",
                "DNT": "1",
                "Connection": "keep-alive",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Upgrade-Insecure-Requests": "1",
            })
            s.mount("https://", adapter)
            s.mount("http://", adapter)
            if proxy:
                s.proxies = {"http": proxy, "https": proxy}
            self._sessions.append(s)

        # Cloudscraper session for Cloudflare-protected sites
        self._cf_session = None
        if HAS_CLOUDSCRAPER:
            self._cf_session = cloudscraper.create_scraper(
                browser={"browser": "chrome", "platform": "windows", "desktop": True}
            )

    def get(self, url, **kwargs):
        with self._lock:
            session = self._sessions[self._idx % len(self._sessions)]
            self._idx += 1
        kwargs.setdefault("timeout", 20)
        kwargs.setdefault("allow_redirects", True)
        time.sleep(random.uniform(0.2, 0.8))
        return session.get(url, **kwargs)

    def get_with_referer(self, url, referer, **kwargs):
        """GET with a spoofed Referer header."""
        with self._lock:
            session = self._sessions[self._idx % len(self._sessions)]
            self._idx += 1
        headers = kwargs.pop("headers", {})
        headers["Referer"] = referer
        headers["Sec-Fetch-Site"] = "same-origin"
        kwargs.setdefault("timeout", 20)
        time.sleep(random.uniform(0.2, 0.5))
        return session.get(url, headers=headers, **kwargs)

    def get_cloudflare(self, url, **kwargs):
        """Use cloudscraper for Cloudflare-protected sites."""
        if self._cf_session:
            kwargs.setdefault("timeout", 30)
            return self._cf_session.get(url, **kwargs)
        return self.get(url, **kwargs)

    def head(self, url, **kwargs):
        with self._lock:
            session = self._sessions[self._idx % len(self._sessions)]
        kwargs.setdefault("timeout", 10)
        return session.head(url, **kwargs)


# ──────────────────────────────────────────────────────────────────────
# PHASE 1: GHOST CACHE — Find cached copies everywhere
# ──────────────────────────────────────────────────────────────────────

class GhostCache:
    """Find cached/mirrored/archived copies of any URL across 12+ services."""

    CACHE_SOURCES = [
        # Wayback Machine
        {"name": "wayback", "pattern": "https://web.archive.org/web/2024/{url}", "style": "wayback"},
        {"name": "wayback_raw", "pattern": "https://web.archive.org/web/2024id_/{url}", "style": "direct"},
        # Google Cache
        {"name": "google_cache", "pattern": "https://webcache.googleusercontent.com/search?q=cache:{url}", "style": "html"},
        # Google AMP Cache
        {"name": "google_amp", "pattern": "https://{domain}.cdn.ampproject.org/c/s/{domain}{path}", "style": "amp"},
        # archive.today
        {"name": "archive_today", "pattern": "https://archive.ph/newest/{url}", "style": "redirect"},
        # Coral CDN (old but sometimes cached)
        {"name": "coral_cdn", "pattern": "http://{domain}.nyud.net{path}", "style": "direct"},
        # Arquivo.pt
        {"name": "arquivo", "pattern": "https://arquivo.pt/wayback/20240101000000id_/{url}", "style": "direct"},
        # UK Web Archive
        {"name": "ukwebarchive", "pattern": "https://www.webarchive.org.uk/wayback/archive/20240101000000id_/{url}", "style": "direct"},
        # Yandex Cache
        {"name": "yandex_cache", "pattern": "https://yandexwebcache.net/yandbtm?fmode=inject&url={url}", "style": "html"},
        # Internet Archive's Save Page Now (check if already saved)
        {"name": "ia_cdx", "pattern": None, "style": "cdx"},
    ]

    def __init__(self, session):
        self.session = session

    def find_cached_files(self, target_url, target_ext=""):
        """Try to find a working cached copy of a file URL."""
        results = []
        parsed = urlparse(target_url)
        domain = parsed.netloc
        path = parsed.path

        for source in self.CACHE_SOURCES:
            try:
                if source["style"] == "cdx":
                    # CDX API lookup for best timestamp
                    cdx_url = f"https://web.archive.org/cdx/search/cdx?url={quote(target_url)}&output=json&limit=5&fl=timestamp,statuscode,length&filter=statuscode:200"
                    resp = self.session.get(cdx_url, timeout=10)
                    if resp.status_code == 200:
                        data = resp.json()
                        if len(data) > 1:
                            for row in data[1:]:
                                ts = row[0]
                                cache_url = f"https://web.archive.org/web/{ts}id_/{target_url}"
                                results.append({
                                    "url": cache_url,
                                    "source": "ia_cdx",
                                    "confidence": 0.9,
                                    "length": int(row[2] or 0),
                                })
                    continue

                if source["pattern"] is None:
                    continue

                cache_url = source["pattern"].format(
                    url=target_url,
                    domain=domain,
                    path=path,
                )

                # Quick HEAD check
                try:
                    resp = self.session.head(cache_url, timeout=8)
                    if resp.status_code in (200, 301, 302):
                        cl = int(resp.headers.get("content-length", 0) or 0)
                        ct = resp.headers.get("content-type", "").lower()

                        # Skip if it's HTML and we wanted a binary file
                        if "text/html" in ct and target_ext not in ("html", "htm", "php"):
                            if source["style"] not in ("html", "redirect"):
                                continue

                        results.append({
                            "url": cache_url,
                            "source": source["name"],
                            "confidence": 0.7 if source["style"] == "direct" else 0.5,
                            "length": cl,
                        })
                except Exception:
                    continue

            except Exception:
                continue

        # Sort by confidence then size
        results.sort(key=lambda x: (-x["confidence"], -x["length"]))
        return results


# ──────────────────────────────────────────────────────────────────────
# PHASE 2: CDN RECON — Reverse-engineer CDN URLs
# ──────────────────────────────────────────────────────────────────────

class CDNRecon:
    """Discover files by analyzing CDN patterns. CDN URLs are often directly
    accessible even when the main website requires authentication."""

    # Known CDN URL patterns
    CDN_PATTERNS = [
        # AWS S3
        (r'https?://([a-z0-9.-]+)\.s3[.-]([a-z0-9-]+)?\.amazonaws\.com/(.+)', "aws_s3"),
        (r'https?://s3[.-]([a-z0-9-]+)?\.amazonaws\.com/([a-z0-9.-]+)/(.+)', "aws_s3_path"),
        # Azure Blob
        (r'https?://([a-z0-9]+)\.blob\.core\.windows\.net/([^/]+)/(.+)', "azure_blob"),
        # Google Cloud Storage
        (r'https?://storage\.googleapis\.com/([^/]+)/(.+)', "gcs"),
        (r'https?://([a-z0-9.-]+)\.storage\.googleapis\.com/(.+)', "gcs_cname"),
        # Cloudflare R2
        (r'https?://([a-z0-9]+)\.r2\.cloudflarestorage\.com/(.+)', "cloudflare_r2"),
        # DigitalOcean Spaces
        (r'https?://([a-z0-9.-]+)\.digitaloceanspaces\.com/(.+)', "do_spaces"),
        (r'https?://([a-z0-9.-]+)\.cdn\.digitaloceanspaces\.com/(.+)', "do_spaces_cdn"),
        # Cloudinary
        (r'https?://res\.cloudinary\.com/([^/]+)/(.+)', "cloudinary"),
        # imgix
        (r'https?://([a-z0-9.-]+)\.imgix\.net/(.+)', "imgix"),
        # Akamai
        (r'https?://([a-z0-9.-]+)\.akamaized\.net/(.+)', "akamai"),
        (r'https?://([a-z0-9.-]+)\.akamaihd\.net/(.+)', "akamai_hd"),
        # Fastly
        (r'https?://([a-z0-9.-]+)\.fastly\.net/(.+)', "fastly"),
        # CloudFront
        (r'https?://([a-z0-9]+)\.cloudfront\.net/(.+)', "cloudfront"),
        # BunnyCDN
        (r'https?://([a-z0-9.-]+)\.b-cdn\.net/(.+)', "bunnycdn"),
        # Generic CDN subdomains
        (r'https?://cdn[0-9]*\.([a-z0-9.-]+)/(.+)', "generic_cdn"),
        (r'https?://static[0-9]*\.([a-z0-9.-]+)/(.+)', "generic_static"),
        (r'https?://media[0-9]*\.([a-z0-9.-]+)/(.+)', "generic_media"),
        (r'https?://assets[0-9]*\.([a-z0-9.-]+)/(.+)', "generic_assets"),
        (r'https?://files[0-9]*\.([a-z0-9.-]+)/(.+)', "generic_files"),
        (r'https?://dl[0-9]*\.([a-z0-9.-]+)/(.+)', "generic_dl"),
        (r'https?://download[0-9]*\.([a-z0-9.-]+)/(.+)', "generic_download"),
        (r'https?://uploads?\.([a-z0-9.-]+)/(.+)', "generic_upload"),
        (r'https?://content[0-9]*\.([a-z0-9.-]+)/(.+)', "generic_content"),
    ]

    def __init__(self, session):
        self.session = session
        self._compiled = [(re.compile(p, re.IGNORECASE), name) for p, name in self.CDN_PATTERNS]

    def extract_cdn_urls(self, html, base_url=""):
        """Extract CDN-hosted file URLs from HTML/JS content."""
        found = []
        seen = set()

        for pattern, cdn_name in self._compiled:
            for match in pattern.finditer(html):
                url = match.group(0)
                if url in seen:
                    continue
                seen.add(url)

                ext = self._get_ext(url)
                if ext in ALL_EXTENSIONS:
                    cat = "other"
                    for c, exts in VALUABLE_EXTENSIONS.items():
                        if ext in exts:
                            cat = c
                            break
                    found.append(FileTarget(
                        url=url,
                        extension=ext,
                        category=cat,
                        source_phase="cdn_recon",
                        source_url=base_url,
                        confidence=0.85,  # CDN URLs are usually directly accessible
                    ))

        # Also look for signed URL patterns (AWS, GCS, Azure)
        # These are time-limited but often valid for hours
        signed_patterns = [
            r'(https?://[^\s"\']+[?&](?:X-Amz-Signature|Signature|sig|token)=[^\s"\']+)',
            r'(https?://[^\s"\']+[?&](?:sv=|se=|sp=|spr=)[^\s"\']+)',  # Azure SAS
        ]
        for pat in signed_patterns:
            for match in re.finditer(pat, html, re.IGNORECASE):
                url = match.group(1).rstrip("'\")")
                if url in seen:
                    continue
                seen.add(url)
                ext = self._get_ext(url)
                if ext in ALL_EXTENSIONS:
                    cat = "other"
                    for c, exts in VALUABLE_EXTENSIONS.items():
                        if ext in exts:
                            cat = c
                            break
                    found.append(FileTarget(
                        url=url,
                        extension=ext,
                        category=cat,
                        source_phase="cdn_signed_url",
                        source_url=base_url,
                        confidence=0.95,  # Signed URLs are almost always valid
                    ))

        return found

    def _get_ext(self, url):
        path = urlparse(url).path.lower().split("?")[0]
        if "." in path:
            ext = path.rsplit(".", 1)[-1]
            if len(ext) <= 6:
                return ext
        return ""


# ──────────────────────────────────────────────────────────────────────
# PHASE 3: API EXCAVATION — Discover hidden API endpoints
# ──────────────────────────────────────────────────────────────────────

class APIExcavator:
    """Discover hidden API endpoints from JS bundles and common patterns.
    APIs often return direct file URLs without authentication checks."""

    COMMON_API_PATHS = [
        "/api/v1/files", "/api/v2/files", "/api/files",
        "/api/v1/media", "/api/v2/media", "/api/media",
        "/api/v1/downloads", "/api/downloads",
        "/api/v1/assets", "/api/assets",
        "/api/v1/uploads", "/api/uploads",
        "/api/v1/documents", "/api/documents",
        "/api/v1/attachments", "/api/attachments",
        "/api/v1/content", "/api/content",
        "/api/v1/storage", "/api/storage",
        "/api/graphql",  # GraphQL endpoint
        "/_api/files", "/_api/media",
        "/wp-json/wp/v2/media",  # WordPress
        "/jsonapi/node", "/jsonapi/media",  # Drupal
        "/rest/api/content",  # Confluence
        "/api/v4/projects",  # GitLab
    ]

    # Patterns to find API endpoints in JS code
    JS_API_PATTERNS = [
        r'''["'](/api/v[0-9]+/[a-zA-Z/]+)["']''',
        r'''["'](/_api/[a-zA-Z/]+)["']''',
        r'''fetch\s*\(\s*["'`]([^"'`]+)["'`]''',
        r'''axios\.[a-z]+\s*\(\s*["'`]([^"'`]+)["'`]''',
        r'''\.get\s*\(\s*["'`]([^"'`]+)["'`]''',
        r'''\.post\s*\(\s*["'`]([^"'`]+)["'`]''',
        r'''baseURL\s*[:=]\s*["'`]([^"'`]+)["'`]''',
        r'''apiUrl\s*[:=]\s*["'`]([^"'`]+)["'`]''',
        r'''endpoint\s*[:=]\s*["'`]([^"'`]+)["'`]''',
        r'''API_URL\s*[:=]\s*["'`]([^"'`]+)["'`]''',
        r'''UPLOAD_URL\s*[:=]\s*["'`]([^"'`]+)["'`]''',
    ]

    def __init__(self, session):
        self.session = session

    def discover_endpoints(self, base_url, html=""):
        """Find API endpoints from the page and its JS bundles."""
        endpoints = set()
        parsed = urlparse(base_url)
        origin = f"{parsed.scheme}://{parsed.netloc}"

        # Check common API paths
        for path in self.COMMON_API_PATHS:
            endpoints.add(f"{origin}{path}")

        # Extract API URLs from HTML/JS
        for pattern in self.JS_API_PATTERNS:
            for match in re.finditer(pattern, html):
                ep = match.group(1)
                if ep.startswith("/"):
                    endpoints.add(f"{origin}{ep}")
                elif ep.startswith("http"):
                    endpoints.add(ep)

        # Find JS bundle URLs and analyze them
        if HAS_BS4 and html:
            soup = BeautifulSoup(html, "html.parser")
            for script in soup.find_all("script", src=True):
                js_url = urljoin(base_url, script["src"])
                # Only analyze same-origin JS files
                if parsed.netloc in js_url:
                    try:
                        resp = self.session.get(js_url, timeout=15)
                        if resp.status_code == 200 and len(resp.text) > 100:
                            for pattern in self.JS_API_PATTERNS:
                                for match in re.finditer(pattern, resp.text):
                                    ep = match.group(1)
                                    if ep.startswith("/"):
                                        endpoints.add(f"{origin}{ep}")
                                    elif ep.startswith("http"):
                                        endpoints.add(ep)
                    except Exception:
                        continue

        return list(endpoints)

    def probe_api_for_files(self, endpoint):
        """Probe an API endpoint for file URLs. Returns list of FileTargets."""
        targets = []
        try:
            resp = self.session.get(endpoint, timeout=12)
            if resp.status_code != 200:
                return targets

            ct = resp.headers.get("content-type", "").lower()
            if "json" not in ct and "xml" not in ct:
                return targets

            text = resp.text
            # Extract any URLs from the API response
            url_pattern = re.compile(
                r'https?://[^\s"\'<>]+\.(?:' + "|".join(ALL_EXTENSIONS) + r')(?:\?[^\s"\'<>]*)?',
                re.IGNORECASE
            )
            for match in url_pattern.finditer(text):
                url = match.group(0).rstrip('",}])')
                ext = url.rsplit(".", 1)[-1].lower().split("?")[0]
                if ext in ALL_EXTENSIONS:
                    cat = "other"
                    for c, exts in VALUABLE_EXTENSIONS.items():
                        if ext in exts:
                            cat = c
                            break
                    targets.append(FileTarget(
                        url=url,
                        extension=ext,
                        category=cat,
                        source_phase="api_excavation",
                        source_url=endpoint,
                        confidence=0.8,
                    ))
        except Exception:
            pass
        return targets


# ──────────────────────────────────────────────────────────────────────
# PHASE 4: SITEMAP SIEGE — Exhaustive sitemap crawling
# ──────────────────────────────────────────────────────────────────────

class SitemapSiege:
    """Exhaustive crawl of robots.txt, sitemaps, and all their variants.
    Sitemaps are public and often list URLs the UI doesn't link to."""

    SITEMAP_PATHS = [
        "/robots.txt",
        "/sitemap.xml",
        "/sitemap_index.xml",
        "/sitemap-index.xml",
        "/sitemapindex.xml",
        "/sitemap1.xml",
        "/sitemap2.xml",
        "/sitemap-0.xml",
        "/sitemap-1.xml",
        "/post-sitemap.xml",
        "/page-sitemap.xml",
        "/category-sitemap.xml",
        "/media-sitemap.xml",  # WordPress media sitemap
        "/wp-sitemap.xml",
        "/wp-sitemap-posts-post-1.xml",
        "/wp-sitemap-posts-page-1.xml",
        "/wp-sitemap-posts-attachment-1.xml",
        "/news-sitemap.xml",
        "/video-sitemap.xml",
        "/image-sitemap.xml",
        "/feed/", "/rss.xml", "/atom.xml", "/feed.xml",
        "/.well-known/sitemap.xml",
    ]

    def __init__(self, session):
        self.session = session

    def siege(self, base_url):
        """Attack all possible sitemap locations. Returns list of file URLs."""
        parsed = urlparse(base_url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        targets = []
        sitemap_urls = set()

        # First, check robots.txt for Sitemap directives
        try:
            resp = self.session.get(f"{origin}/robots.txt", timeout=10)
            if resp.status_code == 200:
                for line in resp.text.split("\n"):
                    line = line.strip()
                    if line.lower().startswith("sitemap:"):
                        sm_url = line.split(":", 1)[1].strip()
                        sitemap_urls.add(sm_url)
                    # Also extract Disallow paths — these are often interesting!
                    if line.lower().startswith("disallow:"):
                        path = line.split(":", 1)[1].strip()
                        if path and path != "/":
                            # Disallowed paths often hide good stuff
                            targets.append(FileTarget(
                                url=f"{origin}{path}",
                                extension="",
                                category="potential_hidden",
                                source_phase="robots_disallow",
                                source_url=f"{origin}/robots.txt",
                                confidence=0.3,
                            ))
        except Exception:
            pass

        # Try all known sitemap paths
        for path in self.SITEMAP_PATHS:
            sitemap_urls.add(f"{origin}{path}")

        # Process each sitemap (iterate over copy to allow adding new ones)
        processed = set()
        while sitemap_urls - processed:
            sm_url = (sitemap_urls - processed).pop()
            processed.add(sm_url)
            try:
                resp = self.session.get(sm_url, timeout=12)
                if resp.status_code != 200:
                    continue

                # Parse sitemap XML for <loc> tags
                locs = re.findall(r'<loc>\s*(.*?)\s*</loc>', resp.text, re.IGNORECASE | re.DOTALL)
                for loc in locs:
                    loc = loc.strip()
                    # Check if it's a nested sitemap
                    if loc.endswith(".xml") or "sitemap" in loc.lower():
                        sitemap_urls.add(loc)
                        continue

                    ext = self._get_ext(loc)
                    if ext in ALL_EXTENSIONS:
                        cat = "other"
                        for c, exts in VALUABLE_EXTENSIONS.items():
                            if ext in exts:
                                cat = c
                                break
                        targets.append(FileTarget(
                            url=loc,
                            extension=ext,
                            category=cat,
                            source_phase="sitemap_siege",
                            source_url=sm_url,
                            confidence=0.75,
                        ))

                # Also extract URLs from RSS/Atom feeds
                if "<rss" in resp.text.lower() or "<feed" in resp.text.lower():
                    links = re.findall(r'<(?:link|enclosure)[^>]*(?:href|url)=["\']([^"\']+)["\']', resp.text, re.IGNORECASE)
                    for link in links:
                        ext = self._get_ext(link)
                        if ext in ALL_EXTENSIONS:
                            cat = "other"
                            for c, exts in VALUABLE_EXTENSIONS.items():
                                if ext in exts:
                                    cat = c
                                    break
                            targets.append(FileTarget(
                                url=link if link.startswith("http") else urljoin(origin, link),
                                extension=ext,
                                category=cat,
                                source_phase="rss_feed",
                                source_url=sm_url,
                                confidence=0.8,
                            ))

            except Exception:
                continue

        return targets

    def _get_ext(self, url):
        path = urlparse(url).path.lower().split("?")[0]
        if "." in path:
            ext = path.rsplit(".", 1)[-1]
            if len(ext) <= 6:
                return ext
        return ""


# ──────────────────────────────────────────────────────────────────────
# PHASE 5: REFERRER BYPASS — Spoof referers to unlock downloads
# ──────────────────────────────────────────────────────────────────────

class ReferrerBypass:
    """Many 'protected' downloads only check the Referer header.
    This phase tries legitimate referers to bypass download restrictions."""

    def __init__(self, session):
        self.session = session

    def try_download_with_referers(self, file_url, dest_path, origin_domain=""):
        """Try downloading with various spoofed referers. Returns (success, size, source)."""
        referers = [
            f"https://{origin_domain}/",
            f"https://{origin_domain}/downloads/",
            f"https://{origin_domain}/files/",
            f"http://{origin_domain}/",
            "https://www.google.com/",
            "https://www.google.com/search?q=site:" + origin_domain,
            "https://duckduckgo.com/",
            "https://www.bing.com/search?q=site:" + origin_domain,
            "https://t.co/redirect",  # Twitter referrer
            "https://www.reddit.com/",
            "https://news.ycombinator.com/",
            "",  # Empty referer
        ]

        ext = file_url.rsplit(".", 1)[-1].lower().split("?")[0] if "." in file_url else ""

        for referer in referers:
            try:
                resp = self.session.get_with_referer(file_url, referer, stream=True, timeout=30)

                if resp.status_code == 403:
                    continue
                if resp.status_code != 200:
                    continue

                ct = resp.headers.get("content-type", "").lower()
                if "text/html" in ct and ext not in ("html", "htm", "php"):
                    continue

                total = 0
                os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
                with open(dest_path, "wb") as f:
                    for chunk in resp.iter_content(65536):
                        f.write(chunk)
                        total += len(chunk)

                if total < 100:
                    try:
                        os.remove(dest_path)
                    except OSError:
                        pass
                    continue

                # Quick HTML check
                with open(dest_path, "rb") as f:
                    header = f.read(50)
                if ext not in ("html", "htm", "php") and (b"<!DOCTYPE" in header or b"<html" in header):
                    os.remove(dest_path)
                    continue

                return True, total, f"referer_bypass ({referer[:30]})"

            except Exception:
                continue

        return False, 0, "all referers failed"


# ──────────────────────────────────────────────────────────────────────
# PHASE 6: EMBED EXTRACTION — Find files in embeds/players
# ──────────────────────────────────────────────────────────────────────

class EmbedExtractor:
    """Extract file URLs from embedded media players, iframes, objects,
    and JS-based media players. These often point to unlocked CDN URLs."""

    # Patterns for common JS media players
    PLAYER_PATTERNS = [
        # JW Player
        r'jwplayer\s*\([^)]*\)\s*\.setup\s*\(\s*\{[^}]*file\s*:\s*["\']([^"\']+)["\']',
        r'jwplayer\s*\([^)]*\)\s*\.setup\s*\(\s*\{[^}]*sources\s*:\s*\[([^\]]+)\]',
        # Video.js
        r'videojs\s*\([^)]*\)\s*\.src\s*\(\s*["\']([^"\']+)["\']',
        r'data-setup\s*=\s*["\'][^"\']*sources\s*:\s*\[([^\]]+)\]["\']',
        # Plyr
        r'new\s+Plyr\s*\([^)]*\)\s*;\s*[^;]*source\s*[:=]\s*["\']([^"\']+)["\']',
        # HTML5 audio/video src
        r'<(?:audio|video)[^>]*src\s*=\s*["\']([^"\']+)["\']',
        r'<source[^>]*src\s*=\s*["\']([^"\']+)["\']',
        r'<source[^>]*srcset\s*=\s*["\']([^"\']+)["\']',
        # poster images
        r'poster\s*=\s*["\']([^"\']+)["\']',
        # Object/embed (Flash players etc.)
        r'<(?:object|embed)[^>]*(?:data|src)\s*=\s*["\']([^"\']+)["\']',
        r'<param[^>]*name\s*=\s*["\'](?:movie|src|flashvars|file)["\'][^>]*value\s*=\s*["\']([^"\']+)["\']',
        # iframe embeds
        r'<iframe[^>]*src\s*=\s*["\']([^"\']+)["\']',
        # Background images in CSS
        r'background(?:-image)?\s*:\s*[^;]*url\s*\(\s*["\']?([^"\')\s]+)["\']?\s*\)',
        # OpenGraph meta tags
        r'<meta[^>]*property\s*=\s*["\']og:(?:image|video|audio)["\'][^>]*content\s*=\s*["\']([^"\']+)["\']',
        r'<meta[^>]*content\s*=\s*["\']([^"\']+)["\'][^>]*property\s*=\s*["\']og:(?:image|video|audio)["\']',
        # Twitter cards
        r'<meta[^>]*name\s*=\s*["\']twitter:(?:image|player:stream)["\'][^>]*content\s*=\s*["\']([^"\']+)["\']',
        # JSON-LD
        r'"contentUrl"\s*:\s*"([^"]+)"',
        r'"embedUrl"\s*:\s*"([^"]+)"',
        r'"downloadUrl"\s*:\s*"([^"]+)"',
        # data attributes
        r'data-(?:src|file|url|audio|video|download|media|image|poster)\s*=\s*["\']([^"\']+)["\']',
    ]

    def __init__(self, session):
        self.session = session

    def extract(self, html, base_url=""):
        """Extract all embedded file URLs from HTML content."""
        targets = []
        seen = set()

        for pattern in self.PLAYER_PATTERNS:
            for match in re.finditer(pattern, html, re.IGNORECASE | re.DOTALL):
                raw = match.group(1)
                # Handle JSON arrays of sources
                if "[" in pattern or "sources" in pattern:
                    # Extract URLs from array format
                    for url_match in re.finditer(r'["\']?(https?://[^\s"\']+)["\']?', raw):
                        self._add_target(url_match.group(1), base_url, targets, seen, "embed_player")
                    for url_match in re.finditer(r'["\']?(/[^\s"\']+)["\']?', raw):
                        full = urljoin(base_url, url_match.group(1))
                        self._add_target(full, base_url, targets, seen, "embed_player")
                else:
                    url = raw.strip()
                    if url.startswith("/"):
                        url = urljoin(base_url, url)
                    elif not url.startswith("http"):
                        url = urljoin(base_url, url)
                    self._add_target(url, base_url, targets, seen, "embed_extract")

        return targets

    def _add_target(self, url, base_url, targets, seen, phase):
        url = url.split("#")[0]  # Remove fragment
        if url in seen or not url.startswith("http"):
            return
        seen.add(url)

        ext = self._get_ext(url)
        if ext in ALL_EXTENSIONS:
            cat = "other"
            for c, exts in VALUABLE_EXTENSIONS.items():
                if ext in exts:
                    cat = c
                    break
            targets.append(FileTarget(
                url=url,
                extension=ext,
                category=cat,
                source_phase=phase,
                source_url=base_url,
                confidence=0.75,
            ))

    def _get_ext(self, url):
        path = urlparse(url).path.lower().split("?")[0]
        if "." in path:
            ext = path.rsplit(".", 1)[-1]
            if len(ext) <= 6:
                return ext
        return ""


# ──────────────────────────────────────────────────────────────────────
# PHASE 7: DIRECTORY STORM — Brute-force common paths
# ──────────────────────────────────────────────────────────────────────

class DirectoryStorm:
    """Brute-force common directory structures, backup paths, and
    version-numbered files."""

    COMMON_DIRS = [
        "/files/", "/downloads/", "/download/", "/media/", "/assets/",
        "/uploads/", "/upload/", "/images/", "/img/", "/pics/", "/photos/",
        "/audio/", "/music/", "/mp3/", "/mp3s/", "/sounds/",
        "/video/", "/videos/", "/movies/", "/clips/",
        "/documents/", "/docs/", "/pdf/", "/pdfs/",
        "/archive/", "/archives/", "/backup/", "/backups/", "/bak/",
        "/old/", "/legacy/", "/deprecated/", "/previous/", "/v1/", "/v2/",
        "/public/", "/pub/", "/static/", "/content/",
        "/data/", "/db/", "/database/", "/dump/", "/dumps/",
        "/temp/", "/tmp/", "/test/", "/testing/", "/dev/",
        "/admin/", "/private/", "/internal/", "/staging/",
        "/dist/", "/build/", "/release/", "/releases/",
        "/gallery/", "/portfolio/", "/works/",
        "/wp-content/uploads/", "/sites/default/files/",
        "/user/", "/users/", "/~admin/", "/~root/",
        "/.git/", "/.svn/", "/.env", "/.htaccess",
        "/.well-known/", "/cgi-bin/",
        "/incoming/", "/outgoing/", "/mirror/",
        # Sensitive files
        "/robots.txt", "/sitemap.xml", "/crossdomain.xml",
        "/phpinfo.php", "/info.php", "/test.php",
        "/.DS_Store", "/Thumbs.db",
        "/wp-config.php.bak", "/config.php.bak",
        "/web.config", "/server-status", "/server-info",
        "/.git/HEAD", "/.git/config",
        "/.svn/entries", "/.svn/wc.db",
    ]

    def __init__(self, session):
        self.session = session

    def storm(self, base_url, max_checks=100):
        """Check common directory paths for listings or files."""
        parsed = urlparse(base_url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        targets = []
        checked = 0

        for dir_path in self.COMMON_DIRS:
            if checked >= max_checks:
                break

            url = f"{origin}{dir_path}"
            try:
                resp = self.session.get(url, timeout=8)
                checked += 1

                if resp.status_code != 200:
                    continue

                ct = resp.headers.get("content-type", "").lower()

                # Check if it's a directory listing
                if "text/html" in ct:
                    text = resp.text
                    is_dirlist = ("Index of" in text or "Parent Directory" in text or
                                 "Directory listing" in text or "[DIR]" in text or
                                 '?C=N;O=D' in text)

                    if is_dirlist:
                        # Parse directory listing for files
                        links = re.findall(r'href=["\']([^"\']+)["\']', text, re.IGNORECASE)
                        for link in links:
                            if link.startswith("?") or link == "../":
                                continue
                            full = urljoin(url, link)
                            ext = self._get_ext(full)
                            if ext in ALL_EXTENSIONS:
                                cat = "other"
                                for c, exts in VALUABLE_EXTENSIONS.items():
                                    if ext in exts:
                                        cat = c
                                        break
                                targets.append(FileTarget(
                                    url=full,
                                    extension=ext,
                                    category=cat,
                                    source_phase="directory_storm",
                                    source_url=url,
                                    confidence=0.9,
                                ))
                    else:
                        # Not a directory listing — check for interesting content
                        ext = self._get_ext(url)
                        if ext in ALL_EXTENSIONS:
                            targets.append(FileTarget(
                                url=url,
                                extension=ext,
                                category="data",
                                source_phase="directory_storm",
                                source_url=base_url,
                                confidence=0.6,
                            ))

                # Non-HTML responses are probably files
                elif "text/html" not in ct:
                    ext = self._get_ext(url) or ct.split("/")[-1][:6]
                    if ext:
                        targets.append(FileTarget(
                            url=url,
                            extension=ext,
                            category="data",
                            source_phase="directory_storm",
                            source_url=base_url,
                            confidence=0.85,
                        ))

            except Exception:
                continue

        return targets

    def _get_ext(self, url):
        path = urlparse(url).path.lower().rstrip("/").split("?")[0]
        if "." in path.split("/")[-1]:
            ext = path.rsplit(".", 1)[-1]
            if len(ext) <= 6:
                return ext
        return ""


# ──────────────────────────────────────────────────────────────────────
# GHOSTPHASE ENGINE — Orchestrator
# ──────────────────────────────────────────────────────────────────────

class GhostPhase:
    """Main orchestrator that runs all 7 phases in parallel."""

    def __init__(self, target_url, dest_dir=None, max_workers=4):
        self.target_url = target_url.rstrip("/")
        self.parsed = urlparse(self.target_url)
        self.domain = self.parsed.netloc
        self.dest_dir = dest_dir or os.path.join(DEFAULT_DEST, self.domain.replace(":", "_"))
        self.max_workers = max_workers

        self.session = StealthSession()
        self.targets = []
        self._seen_urls = set()
        self._lock = threading.Lock()

        # Initialize all phases
        self.ghost_cache = GhostCache(self.session)
        self.cdn_recon = CDNRecon(self.session)
        self.api_excavator = APIExcavator(self.session)
        self.sitemap_siege = SitemapSiege(self.session)
        self.referrer_bypass = ReferrerBypass(self.session)
        self.embed_extractor = EmbedExtractor(self.session)
        self.directory_storm = DirectoryStorm(self.session)

    def _add_targets(self, new_targets):
        with self._lock:
            for t in new_targets:
                if t.url not in self._seen_urls:
                    self._seen_urls.add(t.url)
                    self.targets.append(t)

    def run(self):
        """Execute all phases and return discovered files."""
        console.print(Panel(
            f"[bold cyan]GHOSTPHASE[/bold cyan] — Universal File Extraction\n"
            f"Target: [bold]{self.target_url}[/bold]\n"
            f"Domain: [cyan]{self.domain}[/cyan]\n"
            f"Output: [dim]{self.dest_dir}[/dim]",
            border_style="cyan",
        ))

        # First, fetch the target page
        console.print("\n[bold]Fetching target page...[/bold]")
        html = ""
        try:
            resp = self.session.get(self.target_url, timeout=20)
            if resp.status_code == 200:
                html = resp.text
                console.print(f"  [green]Got {len(html):,} bytes of HTML[/green]")
            elif resp.status_code == 403:
                console.print("  [yellow]403 Forbidden — trying Cloudflare bypass...[/yellow]")
                resp = self.session.get_cloudflare(self.target_url, timeout=30)
                if resp.status_code == 200:
                    html = resp.text
                    console.print(f"  [green]Cloudflare bypassed! {len(html):,} bytes[/green]")
            else:
                console.print(f"  [yellow]HTTP {resp.status_code} — proceeding with alternative phases[/yellow]")
        except Exception as e:
            console.print(f"  [red]Failed: {e} — proceeding with alternative phases[/red]")

        # Run all 7 phases
        console.print("\n[bold]===== RUNNING 7 EXTRACTION PHASES =====[/bold]\n")

        phases = [
            ("Phase 1: GHOST CACHE", self._phase_ghost_cache),
            ("Phase 2: CDN RECON", lambda: self._phase_cdn_recon(html)),
            ("Phase 3: API EXCAVATION", lambda: self._phase_api_excavation(html)),
            ("Phase 4: SITEMAP SIEGE", self._phase_sitemap_siege),
            ("Phase 5: REFERRER PROBE", lambda: []),  # Applied during download, not discovery
            ("Phase 6: EMBED EXTRACTION", lambda: self._phase_embed_extraction(html)),
            ("Phase 7: DIRECTORY STORM", self._phase_directory_storm),
        ]

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = {}
            for name, func in phases:
                console.print(f"  [dim]Launching {name}...[/dim]")
                futures[executor.submit(func)] = name

            for future in as_completed(futures):
                name = futures[future]
                try:
                    results = future.result()
                    if results:
                        self._add_targets(results)
                        console.print(f"  [green]{name}: found {len(results)} targets[/green]")
                    else:
                        console.print(f"  [dim]{name}: 0 targets[/dim]")
                except Exception as e:
                    console.print(f"  [red]{name}: error — {e}[/red]")

        # Also extract directly from HTML if we have it
        if html:
            html_targets = self._extract_from_html(html)
            self._add_targets(html_targets)
            if html_targets:
                console.print(f"  [green]HTML direct extraction: {len(html_targets)} targets[/green]")

        # Dedupe and sort by confidence
        self.targets.sort(key=lambda t: (-t.confidence, t.category))

        # Display results
        self._display_results()

        return self.targets

    def _phase_ghost_cache(self):
        """Phase 1: Find cached copies of the target URL and any file-like paths."""
        results = self.ghost_cache.find_cached_files(self.target_url)
        targets = []
        for r in results:
            ext = r["url"].rsplit(".", 1)[-1].lower().split("?")[0] if "." in r["url"] else ""
            targets.append(FileTarget(
                url=r["url"],
                extension=ext,
                category="cache",
                source_phase="ghost_cache",
                source_url=self.target_url,
                confidence=r["confidence"],
                content_length=r.get("length", 0),
            ))
        return targets

    def _phase_cdn_recon(self, html):
        """Phase 2: Extract CDN URLs from HTML/JS."""
        if not html:
            return []
        return self.cdn_recon.extract_cdn_urls(html, self.target_url)

    def _phase_api_excavation(self, html):
        """Phase 3: Discover and probe API endpoints."""
        endpoints = self.api_excavator.discover_endpoints(self.target_url, html)
        all_targets = []

        # Probe endpoints in parallel
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = {executor.submit(self.api_excavator.probe_api_for_files, ep): ep for ep in endpoints[:20]}
            for future in as_completed(futures):
                try:
                    targets = future.result()
                    all_targets.extend(targets)
                except Exception:
                    pass

        return all_targets

    def _phase_sitemap_siege(self):
        """Phase 4: Exhaust all sitemap sources."""
        return self.sitemap_siege.siege(self.target_url)

    def _phase_embed_extraction(self, html):
        """Phase 6: Extract embedded media URLs."""
        if not html:
            return []
        return self.embed_extractor.extract(html, self.target_url)

    def _phase_directory_storm(self):
        """Phase 7: Brute-force common directories."""
        return self.directory_storm.storm(self.target_url)

    def _extract_from_html(self, html):
        """Direct HTML link extraction (supplement to phases)."""
        targets = []
        seen = set()

        # All href/src attributes
        for attr in ("href", "src", "data-src", "data-url", "data-file", "data-download",
                     "data-media", "poster", "content", "action"):
            pattern = re.compile(rf'{attr}\s*=\s*["\']([^"\']+)["\']', re.IGNORECASE)
            for match in pattern.finditer(html):
                url = match.group(1).strip()
                if url.startswith("//"):
                    url = f"https:{url}"
                elif url.startswith("/"):
                    url = urljoin(self.target_url, url)
                elif not url.startswith("http"):
                    url = urljoin(self.target_url, url)

                if url in seen:
                    continue
                seen.add(url)

                ext = self._get_ext(url)
                if ext in ALL_EXTENSIONS:
                    cat = "other"
                    for c, exts in VALUABLE_EXTENSIONS.items():
                        if ext in exts:
                            cat = c
                            break
                    targets.append(FileTarget(
                        url=url,
                        extension=ext,
                        category=cat,
                        source_phase="html_direct",
                        source_url=self.target_url,
                        confidence=0.7,
                    ))

        return targets

    def _get_ext(self, url):
        path = urlparse(url).path.lower().split("?")[0]
        if "." in path.split("/")[-1]:
            ext = path.rsplit(".", 1)[-1]
            if len(ext) <= 6:
                return ext
        return ""

    def _display_results(self):
        """Show discovered targets in a nice table."""
        if not self.targets:
            console.print("\n[red]No file targets discovered.[/red]")
            return

        table = Table(
            title=f"DISCOVERED FILE TARGETS ({len(self.targets)})",
            box=box.ROUNDED,
            border_style="cyan",
            show_lines=False,
        )
        table.add_column("#", justify="right", style="dim")
        table.add_column("Ext", style="bold")
        table.add_column("Category", style="cyan")
        table.add_column("Phase", style="green")
        table.add_column("Confidence", justify="right")
        table.add_column("URL", max_width=60, no_wrap=True)

        by_phase = defaultdict(int)
        by_cat = defaultdict(int)

        for i, t in enumerate(self.targets[:50]):  # Show top 50
            conf_style = "green" if t.confidence >= 0.7 else ("yellow" if t.confidence >= 0.4 else "red")
            table.add_row(
                str(i + 1),
                f".{t.extension}" if t.extension else "?",
                t.category,
                t.source_phase[:20],
                f"[{conf_style}]{t.confidence:.0%}[/{conf_style}]",
                t.url[:60],
            )
            by_phase[t.source_phase] += 1
            by_cat[t.category] += 1

        console.print()
        console.print(table)

        if len(self.targets) > 50:
            console.print(f"  [dim]... and {len(self.targets) - 50} more targets[/dim]")

        # Phase breakdown
        console.print("\n[bold]Phase Breakdown:[/bold]")
        for phase, count in sorted(by_phase.items(), key=lambda x: -x[1]):
            console.print(f"  {phase:25s} {count:>4d} targets")

        # Category breakdown
        console.print("\n[bold]Category Breakdown:[/bold]")
        for cat, count in sorted(by_cat.items(), key=lambda x: -x[1]):
            console.print(f"  {cat:15s} {count:>4d}")

    def download_all(self, max_workers=3):
        """Download all discovered file targets with multi-strategy fallback."""
        if not self.targets:
            console.print("[red]No targets to download.[/red]")
            return

        os.makedirs(self.dest_dir, exist_ok=True)

        # Filter to high-confidence targets with known extensions
        downloadable = [t for t in self.targets if t.extension and t.confidence >= 0.3]
        # Dedupe by filename
        seen_names = set()
        unique = []
        for t in downloadable:
            if t.filename not in seen_names:
                seen_names.add(t.filename)
                unique.append(t)
        downloadable = unique

        if not downloadable:
            console.print("[yellow]No downloadable targets found.[/yellow]")
            return

        console.print(f"\n[bold]Downloading {len(downloadable)} files to {self.dest_dir}[/bold]\n")

        stats = {"ok": 0, "fail": 0, "skip": 0}
        stats_lock = threading.Lock()
        fail_reasons = defaultdict(int)

        def _download_one(target):
            dest_path = os.path.join(self.dest_dir, target.filename)
            if os.path.exists(dest_path) and os.path.getsize(dest_path) > 200:
                with stats_lock:
                    stats["skip"] += 1
                return

            # Try direct download first
            try:
                resp = self.session.get(target.url, stream=True, timeout=30)
                if resp.status_code == 200:
                    ct = resp.headers.get("content-type", "").lower()
                    if "text/html" not in ct or target.extension in ("html", "htm"):
                        total = 0
                        with open(dest_path, "wb") as f:
                            for chunk in resp.iter_content(65536):
                                f.write(chunk)
                                total += len(chunk)
                        if total > 100:
                            # Quick HTML check
                            with open(dest_path, "rb") as f:
                                hdr = f.read(30)
                            if target.extension not in ("html", "htm") and b"<html" in hdr.lower():
                                os.remove(dest_path)
                            else:
                                target.downloaded = True
                                target.download_size = total
                                with stats_lock:
                                    stats["ok"] += 1
                                return
                        elif os.path.exists(dest_path):
                            os.remove(dest_path)
            except Exception:
                pass

            # Try referrer bypass
            if not target.downloaded:
                ok, size, reason = self.referrer_bypass.try_download_with_referers(
                    target.url, dest_path, self.domain
                )
                if ok:
                    target.downloaded = True
                    target.download_size = size
                    with stats_lock:
                        stats["ok"] += 1
                    return

            # Try cache sources
            if not target.downloaded:
                cache_results = self.ghost_cache.find_cached_files(target.url, target.extension)
                for cache in cache_results[:3]:
                    try:
                        resp = self.session.get(cache["url"], stream=True, timeout=30)
                        if resp.status_code == 200:
                            total = 0
                            with open(dest_path, "wb") as f:
                                for chunk in resp.iter_content(65536):
                                    f.write(chunk)
                                    total += len(chunk)
                            if total > 100:
                                target.downloaded = True
                                target.download_size = total
                                with stats_lock:
                                    stats["ok"] += 1
                                return
                            elif os.path.exists(dest_path):
                                os.remove(dest_path)
                    except Exception:
                        continue

            with stats_lock:
                stats["fail"] += 1

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("{task.completed}/{task.total}"),
            console=console,
        ) as progress:
            task = progress.add_task("Downloading", total=len(downloadable))

            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {executor.submit(_download_one, t): t for t in downloadable}
                for future in as_completed(futures):
                    progress.advance(task)

        console.print(
            f"\n  [green]{stats['ok']} downloaded[/green] | "
            f"[yellow]{stats['skip']} skipped[/yellow] | "
            f"[red]{stats['fail']} failed[/red]"
        )
        console.print(f"  [dim]Files saved to: {self.dest_dir}[/dim]")


# ──────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────

def main(target_override=None, dest=None, download=True, turbo=False):
    if target_override:
        url = target_override
        do_download = download
        workers = 6 if turbo else 4
        dest_dir = dest
    else:
        parser = argparse.ArgumentParser(
            description="GhostPhase — Universal File Extraction Engine",
            formatter_class=argparse.RawDescriptionHelpFormatter,
            epilog="""
Examples:
  python ghostphase.py https://example.com/gallery
  python ghostphase.py https://deadsite.com --download
  python ghostphase.py https://protectedsite.com/files --turbo
  python ghostphase.py https://oldsite.com --dest D:\\recovered
            """
        )
        parser.add_argument("url", help="Target URL to extract files from")
        parser.add_argument("--download", "-d", action="store_true", help="Download all discovered files")
        parser.add_argument("--turbo", "-t", action="store_true", help="Use more parallel threads")
        parser.add_argument("--dest", default=None, help="Download destination directory")
        parser.add_argument("--workers", type=int, default=4, help="Max parallel workers")
        args = parser.parse_args()

        url = args.url
        do_download = args.download
        workers = 6 if args.turbo else args.workers
        dest_dir = args.dest

    engine = GhostPhase(
        target_url=url,
        dest_dir=dest_dir,
        max_workers=workers,
    )

    targets = engine.run()

    if do_download and targets:
        engine.download_all(max_workers=workers)
    elif targets and not do_download:
        console.print(f"\n[bold]Run with --download to grab all {len(targets)} files[/bold]")


if __name__ == "__main__":
    main()
