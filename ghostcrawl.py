#!/usr/bin/env python3
# Optional: pip install pysocks  (needed for --proxy socks5:// support)
"""
GhostCrawl - Dead Site Crawler via Wayback Machine

The innovation: instead of searching CDX for binary files (which misses
dynamically-served content), we CRAWL the dead site through its archived
HTML pages, extract download links, and recover the actual files.

Approach:
  1. Get a site's archived page list from CDX (HTML pages are well-captured)
  2. Fetch those archived pages from the Wayback Machine
  3. Parse HTML for file links (hrefs to .mp3, .zip, .pdf, etc.)
  4. Parse directory listings (Apache autoindex, nginx, etc.)
  5. Check if linked files exist in the Wayback Machine
  6. Download what's there

This is web crawling, but against a ghost.
"""

import requests
import re
import os
import sys
import time
import json
import hashlib
import random
import socket
import threading
from urllib.parse import urljoin, urlparse, unquote, quote
from html.parser import HTMLParser
from datetime import datetime
from collections import defaultdict
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn
from rich.table import Table
from rich import box

from InquirerPy import inquirer

console = Console()


class RequestManager:
    USER_AGENTS = [
        # Chrome 2024-2026
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        # Firefox
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:128.0) Gecko/20100101 Firefox/128.0",
        "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:127.0) Gecko/20100101 Firefox/127.0",
        # Edge
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0",
        # Safari
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15",
        # Opera
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 OPR/110.0.0.0",
        # Older but still common
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:120.0) Gecko/20100101 Firefox/120.0",
        # Googlebot (useful for sites that serve content to crawlers)
        "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
        # Wget/curl style (some open directories prefer these)
        "Mozilla/5.0 (compatible; Wget/1.21.4)",
    ]

    # Referer strings to rotate through for bypass
    REFERERS = [
        "https://www.google.com/",
        "https://www.google.com/search?q=site:",
        "https://duckduckgo.com/",
        "https://web.archive.org/",
        "https://www.reddit.com/",
        "",  # no referer
    ]

    # Per-endpoint base delays (seconds)
    ENDPOINT_DELAYS = {
        "sparkline": 0.5,
        "calendar": 1.0,
        "cdx": 1.5,
        "download": 0.8,
    }

    def __init__(self, proxy=None, pool_size=6, tor_manager=None):
        self.sessions = []
        self.tor_manager = tor_manager
        if tor_manager and not proxy:
            proxy = tor_manager.proxy
        self.proxy = proxy
        self._current_session_idx = 0
        self._lock = threading.Lock()

        # Adaptive delay multipliers per endpoint
        self._delay_multiplier = {ep: 1.0 for ep in self.ENDPOINT_DELAYS}
        self._consecutive_429s = {ep: 0 for ep in self.ENDPOINT_DELAYS}

        retry_strategy = Retry(
            total=4,
            backoff_factor=1.5,
            status_forcelist=[500, 502, 503],
            allowed_methods=["GET", "HEAD"],
            raise_on_status=False,
        )
        adapter = HTTPAdapter(
            max_retries=retry_strategy,
            pool_connections=10,
            pool_maxsize=20,
        )

        uas = random.sample(self.USER_AGENTS, min(pool_size, len(self.USER_AGENTS)))
        for ua in uas:
            session = requests.Session()
            referer = random.choice(self.REFERERS)
            headers = {
                "User-Agent": ua,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip, deflate, br",
                "Connection": "keep-alive",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-User": "?1",
                "Upgrade-Insecure-Requests": "1",
                "DNT": "1",
            }
            if referer:
                headers["Referer"] = referer
            session.headers.update(headers)
            session.mount("https://", adapter)
            session.mount("http://", adapter)
            if proxy:
                session.proxies = {"http": proxy, "https": proxy}
            self.sessions.append(session)

    def _get_endpoint(self, url):
        if "__wb/sparkline" in url:
            return "sparkline"
        if "__wb/calendarcaptures" in url or "calendar" in url:
            return "calendar"
        if "/cdx/" in url:
            return "cdx"
        return "download"

    def _get_delay(self, endpoint):
        base = self.ENDPOINT_DELAYS.get(endpoint, 1.0)
        delay = base * self._delay_multiplier[endpoint]
        # +-30% jitter
        jitter = delay * 0.3
        return max(0.1, delay + random.uniform(-jitter, jitter))

    def _rotate_session(self):
        if len(self.sessions) > 1:
            old = self._current_session_idx
            while self._current_session_idx == old:
                self._current_session_idx = random.randint(0, len(self.sessions) - 1)

    @property
    def _session(self):
        return self.sessions[self._current_session_idx]

    def get(self, url, **kwargs):
        endpoint = self._get_endpoint(url)

        if self.tor_manager:
            self.tor_manager.maybe_renew()

        # Pre-request delay
        delay = self._get_delay(endpoint)
        time.sleep(delay)

        # Retry on connection errors with exponential backoff
        last_exc = None
        for attempt in range(3):
            try:
                resp = self._session.get(url, **kwargs)
                break
            except (requests.exceptions.ConnectionError, ConnectionRefusedError) as e:
                last_exc = e
                self._rotate_session()
                if attempt < 2:
                    backoff = (2 ** attempt) + random.uniform(0, 1)
                    time.sleep(backoff)
        else:
            raise last_exc

        if resp.status_code == 429:
            self._consecutive_429s[endpoint] += 1
            # Increase delay multiplier
            self._delay_multiplier[endpoint] = min(
                self._delay_multiplier[endpoint] * 1.5, 10.0
            )
            # Renew Tor circuit on rate limit
            if self.tor_manager:
                self.tor_manager.renew_circuit()
            # Cool-down after 3+ consecutive 429s
            if self._consecutive_429s[endpoint] >= 3:
                cooldown = random.uniform(30, 60)
                console.print(f"  [yellow]Rate limited on {endpoint} — cooling down {cooldown:.0f}s[/yellow]")
                time.sleep(cooldown)
                self._consecutive_429s[endpoint] = 0
            # Rotate to a different session
            self._rotate_session()
        else:
            # Gradual recovery on success
            self._consecutive_429s[endpoint] = 0
            if self._delay_multiplier[endpoint] > 1.0:
                self._delay_multiplier[endpoint] = max(
                    1.0, self._delay_multiplier[endpoint] * 0.9
                )

        return resp


class TorManager:
    """Manage Tor SOCKS5 proxy with automatic circuit renewal."""

    SOCKS_PORT = 9050
    CONTROL_PORT = 9051
    CHECK_URL = "https://check.torproject.org/api/ip"

    def __init__(self, control_password=None, renew_every=50):
        self.proxy = f"socks5h://127.0.0.1:{self.SOCKS_PORT}"
        self.control_password = control_password or os.environ.get("TOR_CONTROL_PASSWORD", "")
        self.renew_every = renew_every
        self._request_count = 0
        self._lock = threading.Lock()

    def verify(self):
        """Check if Tor is running and traffic routes through it."""
        try:
            resp = requests.get(
                self.CHECK_URL,
                proxies={"http": self.proxy, "https": self.proxy},
                timeout=15,
            )
            data = resp.json()
            if data.get("IsTor"):
                return True, data.get("IP", "unknown")
        except Exception as e:
            return False, str(e)
        return False, "Not routing through Tor"

    def renew_circuit(self):
        """Send NEWNYM signal to Tor control port for a fresh circuit."""
        try:
            from stem.control import Controller
            import stem
            with Controller.from_port(port=self.CONTROL_PORT) as controller:
                if self.control_password:
                    controller.authenticate(password=self.control_password)
                else:
                    controller.authenticate()
                controller.signal(stem.Signal.NEWNYM)
                time.sleep(5)
                return True
        except ImportError:
            return self._renew_raw()
        except Exception:
            return False

    def _renew_raw(self):
        """Renew circuit via raw socket when stem is not installed."""
        try:
            with socket.create_connection(("127.0.0.1", self.CONTROL_PORT), timeout=10) as sock:
                sock.recv(1024)
                if self.control_password:
                    sock.sendall(f'AUTHENTICATE "{self.control_password}"\r\n'.encode())
                else:
                    sock.sendall(b'AUTHENTICATE\r\n')
                resp = sock.recv(1024).decode()
                if "250" not in resp:
                    return False
                sock.sendall(b'SIGNAL NEWNYM\r\n')
                resp = sock.recv(1024).decode()
                return "250" in resp
        except Exception:
            return False

    def maybe_renew(self):
        """Increment request counter and renew circuit when threshold is reached."""
        with self._lock:
            self._request_count += 1
            if self._request_count >= self.renew_every:
                self._request_count = 0
                return self.renew_circuit()
        return False


request_manager = None


def get_request_manager(proxy=None, tor_manager=None):
    global request_manager
    if request_manager is None:
        request_manager = RequestManager(proxy=proxy, tor_manager=tor_manager)
    return request_manager


CDX = "https://web.archive.org/cdx/search/cdx"
WB = "https://web.archive.org/web"
DEFAULT_DEST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads")

# Fallback CDX endpoints — tried in order when primary Wayback is down
CDX_FALLBACKS = [
    {
        "name": "arquivo.pt",
        "cdx": "https://arquivo.pt/wayback/cdx",
        "wb": "https://arquivo.pt/wayback",
        "jsonl": True,  # Arquivo returns JSONL, not JSON array
    },
]

# File extensions we care about
INTERESTING_EXTENSIONS = {
    "audio": {"mp3", "flac", "wav", "ogg", "aac", "m4a", "wma", "opus", "aiff",
              "mid", "midi", "mod", "xm", "s3m", "it", "ra", "ram", "au", "snd", "ape"},
    "video": {"mp4", "avi", "mkv", "wmv", "flv", "mov", "webm", "m4v", "mpg", "mpeg",
              "rm", "rmvb", "asf", "3gp", "ogv", "divx", "vob", "ts"},
    "flash": {"swf", "fla", "flv", "dcr", "dir", "dxr", "spl"},
    "archive": {"zip", "rar", "7z", "tar", "gz", "bz2", "tgz", "xz", "lzh", "ace", "cab", "arj"},
    "document": {"pdf", "doc", "docx", "xls", "xlsx", "pptx", "epub", "rtf", "djvu", "chm", "ps", "tex"},
    "software": {"exe", "msi", "dmg", "apk", "iso", "deb", "rpm", "bin", "img"},
    "image": {"png", "jpg", "jpeg", "gif", "bmp", "tiff", "psd", "svg", "ico", "pcx", "tga", "webp"},
    "data": {"json", "xml", "csv", "sql", "db", "sqlite", "dat", "nfo", "diz", "txt",
             "log", "cfg", "ini", "conf", "bak", "old", "orig", "dump", "tar"},
    "crypto": {"wallet", "key", "pem", "p12", "pfx", "jks", "keystore", "aes",
               "gpg", "pgp", "seed", "mnemonic", "priv", "sec", "enc"},
    "credentials": {"env", "htpasswd", "htaccess", "npmrc", "pypirc", "netrc",
                    "pgpass", "my.cnf", "credentials", "shadow", "passwd"},
    "web": {"htm", "html", "php", "asp", "cgi", "shtml"},
}

ALL_EXTENSIONS = set()
for exts in INTERESTING_EXTENSIONS.values():
    ALL_EXTENSIONS.update(exts)


class LinkExtractor(HTMLParser):
    """Extract all links from HTML, focusing on file download links."""

    def __init__(self, base_url=""):
        super().__init__()
        self.links = []
        self.file_links = []
        self.base_url = base_url
        self.in_link = False
        self.current_href = ""
        self.current_text = ""

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            for name, value in attrs:
                if name == "href" and value:
                    full_url = urljoin(self.base_url, value)
                    self.links.append(full_url)

                    # Check if it's a file link
                    ext = self._get_extension(full_url)
                    if ext in ALL_EXTENSIONS:
                        self.file_links.append({
                            "url": full_url,
                            "extension": ext,
                            "category": self._categorize(ext),
                        })

                    self.in_link = True
                    self.current_href = full_url
                    self.current_text = ""

        # Also check src attributes (audio/video/embed/object/source)
        if tag in ("audio", "video", "source", "embed", "object", "iframe"):
            for name, value in attrs:
                if name in ("src", "data") and value:
                    full_url = urljoin(self.base_url, value)
                    ext = self._get_extension(full_url)
                    if ext in ALL_EXTENSIONS:
                        self.file_links.append({
                            "url": full_url,
                            "extension": ext,
                            "category": self._categorize(ext),
                        })

        # Check data-* attributes (many players use data-src, data-url, data-file, etc.)
        for name, value in attrs:
            if value and name.startswith("data-") and ("http" in str(value) or "/" in str(value)):
                ext = self._get_extension(str(value))
                if ext in ALL_EXTENSIONS:
                    full_url = urljoin(self.base_url, str(value))
                    self.file_links.append({
                        "url": full_url,
                        "extension": ext,
                        "category": self._categorize(ext),
                        "found_via": f"data-{name}",
                    })

    def handle_endtag(self, tag):
        if tag == "a" and self.in_link:
            # Check if link text suggests a download
            text_lower = self.current_text.lower()
            if any(kw in text_lower for kw in ["download", "descargar", "mp3", "zip", "pdf",
                                                  "click here", "get it", "grab", "save"]):
                ext = self._get_extension(self.current_href)
                if ext not in ALL_EXTENSIONS:
                    # Link text suggests download but URL doesn't have known extension
                    # Could be a dynamic download link - flag it
                    self.file_links.append({
                        "url": self.current_href,
                        "extension": "unknown",
                        "category": "potential_download",
                        "link_text": self.current_text.strip()[:100],
                    })
            self.in_link = False

    def handle_data(self, data):
        if self.in_link:
            self.current_text += data

    def _get_extension(self, url):
        path = urlparse(url).path.lower()
        # Handle query strings
        path = path.split("?")[0]
        if "." in path:
            ext = path.rsplit(".", 1)[-1]
            if len(ext) <= 6:  # Reasonable extension length
                return ext
        return ""

    def _categorize(self, ext):
        for category, exts in INTERESTING_EXTENSIONS.items():
            if ext in exts:
                return category
        return "other"


class DirectoryParser(HTMLParser):
    """Parse Apache/nginx directory listings for file links."""

    def __init__(self, base_url=""):
        super().__init__()
        self.files = []
        self.base_url = base_url
        self.is_directory_listing = False

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            for name, value in attrs:
                if name == "href" and value:
                    # Skip parent directory and sorting links
                    if value in ("../", "?C=N;O=D", "?C=M;O=A", "?C=S;O=A", "?C=D;O=A"):
                        self.is_directory_listing = True  # Confirms it's a directory listing
                        continue
                    full_url = urljoin(self.base_url, value)
                    self.files.append(full_url)

    def handle_data(self, data):
        if "Index of" in data or "Parent Directory" in data:
            self.is_directory_listing = True


def extract_urls_from_text(html_text, base_url=""):
    """
    Extract file URLs from raw text (JavaScript, JSON, inline data).
    This catches URLs that HTML parsing misses - audio players loading
    files through JS, JSON configs, embedded data URIs, etc.
    """
    file_links = []

    # Build extension pattern
    ext_pattern = "|".join(sorted(ALL_EXTENSIONS))

    # Pattern 1: Quoted URLs with file extensions
    # Catches: "https://cdn.example.com/file.mp3", '/path/to/file.zip'
    url_pattern = re.compile(
        r'''["'](https?://[^\s"'<>]+\.(?:''' + ext_pattern + r''')(?:\?[^\s"'<>]*)?)["']''',
        re.IGNORECASE
    )

    # Pattern 2: Relative paths with file extensions
    # Catches: "/audio/320/12345.mp3", "files/song.flac"
    rel_pattern = re.compile(
        r'''["'](/[^\s"'<>]+\.(?:''' + ext_pattern + r''')(?:\?[^\s"'<>]*)?)["']''',
        re.IGNORECASE
    )

    # Pattern 3: URLs in JSON (often audio player configs)
    # Catches: "file":"http://example.com/song.mp3"
    json_pattern = re.compile(
        r'''"(?:file|url|src|source|audio|video|download|path|href|mp3|stream)":\s*"(https?://[^\s"]+\.(?:''' + ext_pattern + r''')[^"]*)"''',
        re.IGNORECASE
    )

    seen = set()
    for pattern, pattern_name in [
        (url_pattern, "quoted_url"),
        (rel_pattern, "relative_path"),
        (json_pattern, "json_config"),
    ]:
        for match in pattern.finditer(html_text):
            url = match.group(1)
            if url.startswith("/"):
                url = urljoin(base_url, url)

            if url in seen:
                continue
            seen.add(url)

            ext = url.rsplit(".", 1)[-1].lower().split("?")[0] if "." in url.split("/")[-1] else ""
            if ext in ALL_EXTENSIONS:
                category = "other"
                for cat, exts in INTERESTING_EXTENSIONS.items():
                    if ext in exts:
                        category = cat
                        break

                file_links.append({
                    "url": url,
                    "extension": ext,
                    "category": category,
                    "found_via": pattern_name,
                })

    return file_links


def wayback_sparkline(url):
    """
    Query the Wayback Machine's internal sparkline API.
    Returns years with captures - sometimes exposes snapshots not in CDX.
    """
    try:
        spark_url = f"https://web.archive.org/__wb/sparkline?output=json&url={quote(url)}&collection=web"
        resp = get_request_manager().get(spark_url, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            years = data.get("years", {})
            # years is {"2005": [0,0,3,0,1,...], "2006": [...]} - captures per month
            active_years = {y: sum(months) for y, months in years.items() if sum(months) > 0}
            return active_years
    except Exception:
        pass
    return {}


def cdx_discover_years(domain):
    """Fallback year discovery via CDX when sparkline is blocked.

    Tries Wayback, then falls back to Arquivo.pt and Archive-It.
    """
    years = {}
    try:
        # Earliest capture
        params = {
            "url": domain, "matchType": "domain", "output": "json",
            "limit": 1, "fl": "timestamp",
        }
        resp = _cdx_request(params, timeout=15)
        if resp:
            data = resp.json()
            if len(data) > 1:
                earliest = str(data[1][0])[:4]
                years[earliest] = 1

        # Latest capture
        params["sort"] = "reverse"
        resp = _cdx_request(params, timeout=15)
        if resp:
            data = resp.json()
            if len(data) > 1:
                latest = str(data[1][0])[:4]
                years[latest] = 1

        # Fill in the range
        if len(years) >= 2:
            low = int(min(years.keys()))
            high = int(max(years.keys()))
            for y in range(low, high + 1):
                years[str(y)] = years.get(str(y), 1)
    except Exception:
        pass
    return years


def wayback_calendar_snapshots(url, year):
    """
    Query the Wayback Machine's calendar API for a specific year.
    This sometimes finds snapshots that CDX misses.
    """
    try:
        cal_url = f"https://web.archive.org/__wb/calendarcaptures/2?url={quote(url)}&date={year}"
        resp = get_request_manager().get(cal_url, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            # Returns array of [timestamp, ...] entries
            items = data.get("items", [])
            timestamps = []
            for item in items:
                if isinstance(item, list) and len(item) > 0:
                    timestamps.append(str(item[0]))
            return timestamps
    except Exception:
        pass
    return []


class _FallbackResponse:
    """Wraps a fallback CDX response to normalize JSONL → JSON array format."""
    def __init__(self, resp, jsonl=False):
        self._resp = resp
        self._jsonl = jsonl
        self.status_code = resp.status_code

    def json(self):
        if not self._jsonl:
            return self._resp.json()
        # Convert JSONL to the Wayback JSON array format: [headers, row1, row2, ...]
        records = []
        for line in self._resp.text.strip().split("\n"):
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        if not records:
            return []
        keys = list(records[0].keys())
        result = [keys]
        for rec in records:
            result.append([rec.get(k, "") for k in keys])
        return result


def _cdx_request(params, timeout=20):
    """Make a CDX request with automatic fallback to alternative archives."""
    global _wayback_alive
    # Try primary Wayback first (skip if already known dead)
    if _wayback_alive is not False:
        try:
            resp = get_request_manager().get(CDX, params=params, timeout=timeout)
            if resp.status_code == 200:
                return resp
        except (requests.exceptions.ConnectionError, ConnectionRefusedError):
            _wayback_alive = False
            console.print("  [yellow]Wayback CDX unreachable — switching to fallbacks[/yellow]")
        except Exception:
            pass

    # Try fallbacks
    for fb in CDX_FALLBACKS:
        try:
            console.print(f"  [dim]Trying fallback: {fb['name']}...[/dim]")
            resp = get_request_manager().get(fb["cdx"], params=params, timeout=timeout)
            if resp.status_code == 200:
                console.print(f"  [green]Fallback {fb['name']} responded![/green]")
                return _FallbackResponse(resp, jsonl=fb.get("jsonl", False))
        except Exception:
            continue

    return None


def cdx_get_pages(domain, from_year=None, to_year=None, limit=200):
    """Get a list of archived HTML pages for a domain, year by year if needed.

    Automatically falls back to Arquivo.pt and Archive-It if Wayback is down.
    """
    pages = {}

    years = range(from_year or 1996, (to_year or 2026) + 1)
    for year in years:
        params = {
            "url": domain,
            "matchType": "domain",
            "output": "json",
            "limit": limit,
            "fl": "timestamp,original,mimetype,statuscode",
            "filter": ["statuscode:200", "mimetype:text/html"],
            "collapse": "urlkey",
            "from": str(year),
            "to": str(year + 1),
        }
        resp = _cdx_request(params)
        if resp:
            try:
                data = resp.json()
                if len(data) > 1:
                    for row in data[1:]:
                        record = dict(zip(data[0], row))
                        url = record["original"]
                        if url not in pages:
                            pages[url] = record["timestamp"]
            except Exception:
                pass

    return pages


def fetch_archived_page(timestamp, url):
    """Fetch an archived page from the Wayback Machine."""
    # Use id_ to get raw content (no Wayback toolbar injection)
    wb_url = f"{WB}/{timestamp}id_/{url}"
    try:
        resp = get_request_manager().get(wb_url, timeout=30)
        if resp.status_code == 200:
            return resp.text, wb_url
        return None, None
    except Exception:
        return None, None


def check_wayback_exists(url):
    """Quick check if a URL has any Wayback snapshot."""
    try:
        params = {"url": url, "matchType": "exact", "output": "json", "limit": 1,
                  "fl": "timestamp,statuscode,length", "filter": "statuscode:200"}
        resp = get_request_manager().get(CDX, params=params, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            if len(data) > 1:
                record = dict(zip(data[0], data[1]))
                return record
        return None
    except Exception:
        return None


# Track whether Wayback is reachable this session to avoid repeated timeouts
_wayback_alive = None  # None = unknown, True = reachable, False = dead


def _check_wayback_alive():
    """Quick connectivity check — cached for the session."""
    global _wayback_alive
    if _wayback_alive is not None:
        return _wayback_alive
    try:
        resp = requests.head("https://web.archive.org/", timeout=5)
        _wayback_alive = resp.status_code < 500
    except Exception:
        _wayback_alive = False
    return _wayback_alive


def _try_direct_download(original_url, dest_path, timeout=30):
    """Try downloading directly from the original URL (live web). Returns (success, size, reason)."""
    try:
        rm = get_request_manager()
        ext = original_url.rsplit(".", 1)[-1].lower().split("?")[0] if "." in original_url else ""

        # Try both http and https variants
        urls_to_try = []
        if original_url.startswith("http://"):
            urls_to_try.append(original_url)
            urls_to_try.append(original_url.replace("http://", "https://", 1))
        elif original_url.startswith("https://"):
            urls_to_try.append(original_url)
            urls_to_try.append(original_url.replace("https://", "http://", 1))
        else:
            urls_to_try.append(f"https://{original_url}")
            urls_to_try.append(f"http://{original_url}")

        for url in urls_to_try:
            try:
                # Quick HEAD check first to avoid wasting time on 404s
                try:
                    head_resp = rm._session.head(url, timeout=min(timeout, 8), allow_redirects=True)
                    if head_resp.status_code in (404, 403, 410, 451, 500, 502, 503):
                        continue
                    # Check content-type from HEAD
                    head_ct = head_resp.headers.get("content-type", "").lower()
                    if "text/html" in head_ct and ext not in ("html", "htm", "php", "asp"):
                        continue
                except requests.exceptions.RequestException:
                    pass  # HEAD failed but GET might work

                resp = rm.get(url, stream=True, timeout=timeout, allow_redirects=True)
                if resp.status_code != 200:
                    continue

                ct = resp.headers.get("content-type", "").lower()
                if "text/html" in ct and ext not in ("html", "htm", "php", "asp"):
                    continue

                # Check for soft 404s (200 status but redirect to homepage or error page)
                final_url = resp.url if hasattr(resp, 'url') else url
                if final_url != url:
                    final_path = urlparse(final_url).path.rstrip("/")
                    orig_path = urlparse(url).path.rstrip("/")
                    # If redirected to root or a completely different path, it's a soft 404
                    if final_path in ("", "/") and orig_path not in ("", "/"):
                        continue
                    if "error" in final_path.lower() or "404" in final_path.lower():
                        continue

                total = 0
                os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
                with open(dest_path, "wb") as f:
                    for chunk in resp.iter_content(65536):
                        f.write(chunk)
                        total += len(chunk)

                if total < 100:
                    if os.path.exists(dest_path):
                        os.remove(dest_path)
                    continue

                # Use magic byte validation
                if not _validate_content_magic(dest_path, ext):
                    os.remove(dest_path)
                    continue

                return True, total, "ok (direct)"
            except (requests.exceptions.ConnectionError, requests.exceptions.ReadTimeout):
                continue
            except Exception:
                continue
    except Exception:
        pass
    return False, 0, "direct download failed"


def _try_download_from_archive(wb_base, timestamp, original_url, dest_path, timeout=60,
                                override_timestamps=None):
    """Try downloading a file from a specific archive. Returns (success, size, reason).

    If override_timestamps is provided, uses those instead of hardcoded fallbacks.
    """
    if override_timestamps:
        timestamps_to_try = list(override_timestamps)
    else:
        timestamps_to_try = [timestamp] if timestamp else []
        # Broader fallback range — more years, more chances
        timestamps_to_try.extend([
            "20260101000000", "20250101000000", "20240101000000",
            "20230101000000", "20220101000000", "20210101000000",
            "20200101000000", "20190101000000", "20180101000000",
            "20170101000000", "20160101000000", "20150101000000",
            "20140101000000", "20120101000000", "20100101000000",
            "20080101000000", "20060101000000", "20050101000000",
            "20040101000000", "20030101000000", "20020101000000",
            "20010101000000", "20000101000000", "19990101000000",
            "19980101000000", "0",
        ])
    seen = set()
    unique_ts = []
    for ts in timestamps_to_try:
        if ts and ts not in seen:
            seen.add(ts)
            unique_ts.append(ts)

    last_reason = "unknown"
    for ts in unique_ts:
        dl_url = f"{wb_base}/{ts}id_/{original_url}"
        try:
            resp = get_request_manager().get(dl_url, stream=True, timeout=timeout, allow_redirects=True)

            if resp.status_code in (301, 302):
                # Follow redirects manually for some archives
                redirect_url = resp.headers.get("Location", "")
                if redirect_url:
                    try:
                        resp = get_request_manager().get(redirect_url, stream=True, timeout=timeout, allow_redirects=True)
                        if resp.status_code != 200:
                            last_reason = f"redirect_{resp.status_code}"
                            continue
                    except Exception:
                        last_reason = f"redirect_failed"
                        continue
                else:
                    last_reason = f"http_{resp.status_code}"
                    continue
            elif resp.status_code in (404, 503):
                last_reason = f"http_{resp.status_code}"
                continue
            elif resp.status_code == 429:
                last_reason = "rate_limited"
                # Proper exponential backoff for rate limiting
                wait = random.uniform(5, 15)
                time.sleep(wait)
                continue
            elif resp.status_code != 200:
                last_reason = f"http_{resp.status_code}"
                continue

            ct = resp.headers.get("content-type", "").lower()
            ext = original_url.rsplit(".", 1)[-1].lower().split("?")[0] if "." in original_url else ""
            if "text/html" in ct and ext not in ("html", "htm", "php", "asp", "shtml"):
                last_reason = "not_archived (html served instead of file)"
                continue

            total = 0
            os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
            with open(dest_path, "wb") as f:
                for chunk in resp.iter_content(65536):
                    f.write(chunk)
                    total += len(chunk)

            if total < 200 and ext not in ("txt", "nfo", "csv", "diz", "srt", "sub"):
                if os.path.exists(dest_path):
                    try:
                        with open(dest_path, "r", errors="replace") as f:
                            content = f.read(200)
                        if "<html" in content.lower() or "not found" in content.lower() or "404" in content:
                            os.remove(dest_path)
                            last_reason = "not_archived (tiny html error)"
                            continue
                    except Exception:
                        pass

            if total > 0 and ext not in ("html", "htm", "php", "asp", "shtml"):
                if not _validate_content_magic(dest_path, ext):
                    os.remove(dest_path)
                    last_reason = "not_archived (failed magic check)"
                    continue

            if total == 0:
                if os.path.exists(dest_path):
                    os.remove(dest_path)
                last_reason = "empty file"
                continue

            return True, total, "ok"

        except requests.exceptions.ConnectionError:
            last_reason = "connection_refused"
            break
        except requests.exceptions.ReadTimeout:
            last_reason = "timeout"
            time.sleep(1)
            continue
        except Exception as e:
            if os.path.exists(dest_path):
                try:
                    os.remove(dest_path)
                except OSError:
                    pass
            last_reason = str(e)[:60]
            continue

    return False, 0, last_reason


# Additional archive sources beyond Wayback and CDX_FALLBACKS
# Each has a wb (wayback-style base URL) for download, or a pattern for direct fetch
EXTRA_ARCHIVES = [
    # archive.today / archive.ph — separate snapshot system, great for recent pages
    {"name": "archive.today", "wb": "https://archive.ph", "style": "redirect"},
    # Ghostarchive — YouTube/social media snapshots
    {"name": "ghostarchive", "wb": "https://ghostarchive.org/archive", "style": "redirect"},
    # UK Web Archive (British Library) — great for .uk domains
    {"name": "ukwebarchive", "wb": "https://www.webarchive.org.uk/wayback/archive", "style": "wayback"},
    # Stanford Web Archive
    {"name": "stanford", "wb": "https://swap.stanford.edu", "style": "wayback"},
    # Library of Congress
    {"name": "loc", "wb": "https://webarchive.loc.gov/all", "style": "wayback"},
    # Iceland Web Archive
    {"name": "vefsafn", "wb": "https://wayback.vefsafn.is/wayback", "style": "wayback"},
    # Croatia Web Archive
    {"name": "haw", "wb": "https://haw.nsk.hr/wayback", "style": "wayback"},
]


# Magic bytes for content validation — detect real files vs HTML error pages
MAGIC_BYTES = {
    # Audio
    b'\xff\xfb': 'mp3', b'\xff\xf3': 'mp3', b'\xff\xf2': 'mp3',
    b'ID3': 'mp3', b'fLaC': 'flac', b'RIFF': 'wav/avi',
    b'OggS': 'ogg', b'MThd': 'midi',
    # Video
    b'\x00\x00\x00\x1c': 'mp4/mov', b'\x00\x00\x00\x18': 'mp4',
    b'\x00\x00\x00\x20': 'mp4', b'\x1a\x45\xdf\xa3': 'mkv/webm',
    b'FLV\x01': 'flv',
    # Images
    b'\x89PNG': 'png', b'\xff\xd8\xff': 'jpg', b'GIF87a': 'gif',
    b'GIF89a': 'gif', b'BM': 'bmp', b'WEBP': 'webp',
    # Archives
    b'PK\x03\x04': 'zip', b'PK\x05\x06': 'zip', b'Rar!\x1a\x07': 'rar',
    b'7z\xbc\xaf\x27\x1c': '7z', b'\x1f\x8b': 'gz',
    # Documents
    b'%PDF': 'pdf', b'\xd0\xcf\x11\xe0': 'doc/xls',
    # Flash
    b'FWS': 'swf', b'CWS': 'swf', b'ZWS': 'swf',
    # Executables
    b'MZ': 'exe/dll',
}


def _validate_content_magic(filepath, expected_ext):
    """Check if file content matches expected type via magic bytes.
    Returns True if content looks legit, False if it's an HTML error page."""
    try:
        with open(filepath, 'rb') as f:
            header = f.read(32)
        if len(header) < 4:
            return False

        # HTML check — most common failure mode
        html_sigs = [b'<!DOCTYPE', b'<html', b'<HTML', b'<head', b'<HEAD',
                     b'<?xml', b'<!-- ', b'<TITLE', b'<title']
        if expected_ext not in ('html', 'htm', 'php', 'asp', 'shtml', 'xml'):
            for sig in html_sigs:
                if header.startswith(sig) or sig in header[:100]:
                    return False

        # If we can identify the magic bytes, check they match something reasonable
        for magic, ftype in MAGIC_BYTES.items():
            if header.startswith(magic):
                return True  # Known file type — good

        # For text-based formats, allow them
        if expected_ext in ('txt', 'nfo', 'csv', 'diz', 'srt', 'sub', 'json', 'xml',
                           'sql', 'key', 'pem', 'wallet', 'dat', 'cfg', 'ini', 'log'):
            return True

        # Unknown magic but not HTML — probably fine
        return True
    except Exception:
        return False


def _cdx_best_timestamps(original_url, limit=10):
    """Query CDX for the best available timestamps for a specific file URL.
    Returns list of (timestamp, length) tuples sorted by file size descending."""
    timestamps = []
    try:
        params = {
            "url": original_url,
            "matchType": "exact",
            "output": "json",
            "limit": limit,
            "fl": "timestamp,statuscode,length,mimetype",
            "filter": "statuscode:200",
            "collapse": "digest",  # Dedupe identical snapshots
        }
        resp = _cdx_request(params, timeout=12)
        if resp:
            data = resp.json()
            if len(data) > 1:
                for row in data[1:]:
                    record = dict(zip(data[0], row))
                    ts = record.get("timestamp", "")
                    length = int(record.get("length", 0) or 0)
                    mime = record.get("mimetype", "")
                    # Skip HTML error pages served for binary files
                    ext = original_url.rsplit(".", 1)[-1].lower().split("?")[0] if "." in original_url else ""
                    if "text/html" in mime and ext not in ("html", "htm", "php", "asp"):
                        continue
                    timestamps.append((ts, length))
                # Sort by size descending — biggest snapshot = most likely the real file
                timestamps.sort(key=lambda x: x[1], reverse=True)
    except Exception:
        pass
    return timestamps


def _head_check(url, timeout=8):
    """Quick HEAD request to check if a URL is likely a real file.
    Returns (exists, content_length, content_type) or (False, 0, '')."""
    try:
        rm = get_request_manager()
        # Use a fresh session for HEAD to avoid polluting state
        resp = rm._session.head(url, timeout=timeout, allow_redirects=True)
        if resp.status_code == 200:
            ct = resp.headers.get("content-type", "").lower()
            cl = int(resp.headers.get("content-length", 0) or 0)
            return True, cl, ct
        elif resp.status_code == 405:
            # HEAD not allowed — need to try GET, but it's probably there
            return True, 0, ""
    except Exception:
        pass
    return False, 0, ""


def download_from_wayback(timestamp, original_url, dest_path):
    """
    Download a file with aggressive multi-source fallback strategy.

    Tries in order:
      1. Direct download from original URL (live web)
      2. CDX lookup → best available timestamps from Wayback
      3. Wayback Machine with CDX-discovered timestamps
      4. Arquivo.pt and other CDX_FALLBACKS
      5. Extra archives (UK Web Archive, Stanford, LoC, etc.)
      6. Wayback with hardcoded fallback timestamps as last resort

    Returns (success, size, reason).
    """
    dest_dir = os.path.dirname(dest_path)
    if dest_dir:
        os.makedirs(dest_dir, exist_ok=True)

    ext = original_url.rsplit(".", 1)[-1].lower().split("?")[0] if "." in original_url else ""

    # Strategy 1: Direct download from original URL
    ok, size, reason = _try_direct_download(original_url, dest_path, timeout=15)
    if ok:
        return True, size, reason

    # Strategy 2: CDX lookup for best timestamps (the secret sauce)
    cdx_timestamps = _cdx_best_timestamps(original_url, limit=8)
    cdx_ts_list = [ts for ts, _ in cdx_timestamps]

    # Strategy 3: Try Wayback with CDX-discovered timestamps first
    if cdx_ts_list and _check_wayback_alive():
        ok, size, reason = _try_download_from_archive(
            WB, None, original_url, dest_path, timeout=60,
            override_timestamps=cdx_ts_list
        )
        if ok and _validate_content_magic(dest_path, ext):
            return True, size, "ok (via wayback/cdx)"
        elif ok:
            # Downloaded but failed magic check — remove and try next
            try: os.remove(dest_path)
            except OSError: pass

    # Strategy 4: Try Wayback with provided timestamp + fallbacks
    if _check_wayback_alive():
        ok, size, reason = _try_download_from_archive(WB, timestamp, original_url, dest_path, timeout=60)
        if ok and _validate_content_magic(dest_path, ext):
            return True, size, "ok (via wayback)"
        elif ok:
            try: os.remove(dest_path)
            except OSError: pass

    # Strategy 5: CDX fallbacks (Arquivo.pt etc.)
    for fb in CDX_FALLBACKS:
        ok, size, reason = _try_download_from_archive(
            fb["wb"], timestamp, original_url, dest_path, timeout=60,
            override_timestamps=cdx_ts_list[:3] if cdx_ts_list else None
        )
        if ok and _validate_content_magic(dest_path, ext):
            return True, size, f"ok (via {fb['name']})"
        elif ok:
            try: os.remove(dest_path)
            except OSError: pass

    # Strategy 6: Extra archives (wayback-style)
    for arch in EXTRA_ARCHIVES:
        if arch.get("style") != "wayback":
            continue
        try:
            ok, size, reason = _try_download_from_archive(
                arch["wb"], timestamp, original_url, dest_path, timeout=45,
                override_timestamps=cdx_ts_list[:2] if cdx_ts_list else None
            )
            if ok and _validate_content_magic(dest_path, ext):
                return True, size, f"ok (via {arch['name']})"
            elif ok:
                try: os.remove(dest_path)
                except OSError: pass
        except Exception:
            continue

    # Strategy 7: archive.today redirect-style (different URL pattern)
    for arch in EXTRA_ARCHIVES:
        if arch.get("style") != "redirect":
            continue
        try:
            redirect_url = f"{arch['wb']}/{original_url}"
            rm = get_request_manager()
            resp = rm.get(redirect_url, stream=True, timeout=30, allow_redirects=True)
            if resp.status_code == 200:
                ct = resp.headers.get("content-type", "").lower()
                if "text/html" not in ct or ext in ("html", "htm"):
                    total = 0
                    with open(dest_path, "wb") as f:
                        for chunk in resp.iter_content(65536):
                            f.write(chunk)
                            total += len(chunk)
                    if total > 200 and _validate_content_magic(dest_path, ext):
                        return True, total, f"ok (via {arch['name']})"
                    try: os.remove(dest_path)
                    except OSError: pass
        except Exception:
            continue

    # Strategy 8: Wayback as absolute last resort (even if health check failed)
    if not _check_wayback_alive():
        ok, size, reason = _try_download_from_archive(WB, timestamp, original_url, dest_path, timeout=90)
        if ok and _validate_content_magic(dest_path, ext):
            return True, size, "ok (via wayback-lastresort)"
        elif ok:
            try: os.remove(dest_path)
            except OSError: pass

    return False, 0, "all sources failed"


def sanitize_filename(name):
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    name = name.strip(". ")
    if len(name) > 200:
        base, ext = os.path.splitext(name)
        name = base[:196] + ext
    return name


def crawl_dead_site(domain, target_types=None, from_year=None, to_year=None,
                    max_pages=100, dest_dir=None):
    """
    Crawl a dead site through the Wayback Machine.

    1. Get archived HTML pages from CDX
    2. Fetch and parse each page for file links
    3. Check if linked files exist in the Wayback Machine
    4. Download what's there
    """
    if target_types is None:
        target_types = set(ALL_EXTENSIONS)
    else:
        target_types = set(target_types)

    console.print(f"\n[bold yellow]GHOSTCRAWL[/bold yellow] - Crawling [cyan]{domain}[/cyan] through the Wayback Machine")
    console.print(f"[dim]Looking for: {', '.join(sorted(target_types)[:15])}{'...' if len(target_types) > 15 else ''}[/dim]\n")

    # Step 0: Check sparkline for capture years (fast, internal API)
    console.print("[bold]Step 0:[/bold] Checking capture history...")
    spark_years = wayback_sparkline(f"http://{domain}/")
    if spark_years:
        total_captures = sum(spark_years.values())
        year_range = f"{min(spark_years.keys())}-{max(spark_years.keys())}"
        console.print(f"  [green]{total_captures} captures across {year_range}[/green]")
        top_years = sorted(spark_years.items(), key=lambda x: -x[1])[:5]
        console.print(f"  Best years: {', '.join(f'{y}({c})' for y, c in top_years)}")
        if not from_year:
            from_year = int(min(spark_years.keys()))
        if not to_year:
            to_year = int(max(spark_years.keys()))
    else:
        console.print(f"  [yellow]Sparkline blocked — trying CDX year discovery...[/yellow]")
        cdx_years = cdx_discover_years(domain)
        if cdx_years:
            year_range = f"{min(cdx_years.keys())}-{max(cdx_years.keys())}"
            console.print(f"  [green]CDX found captures across {year_range}[/green]")
            if not from_year:
                from_year = int(min(cdx_years.keys()))
            if not to_year:
                to_year = int(max(cdx_years.keys()))
        else:
            console.print(f"  [dim]No year data available — using defaults (2000-2024)[/dim]")
            if not from_year:
                from_year = 2000
            if not to_year:
                to_year = 2024

    # Step 1: Get archived pages
    console.print("\n[bold]Step 1:[/bold] Discovering archived pages...")
    pages = cdx_get_pages(domain, from_year, to_year, limit=max_pages)
    console.print(f"  Found [green]{len(pages)}[/green] unique archived pages\n")

    if not pages:
        console.print("[yellow]No archived pages found for this domain.[/yellow]")
        return []

    # Step 2: Crawl pages for file links
    console.print("[bold]Step 2:[/bold] Crawling archived pages for file links...")
    all_file_links = {}
    directory_listings = []
    pages_crawled = 0

    with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
                  BarColumn(), TextColumn("{task.completed}/{task.total}"), console=console) as progress:
        task = progress.add_task("Crawling", total=min(len(pages), max_pages))

        for url, timestamp in list(pages.items())[:max_pages]:
            html, wb_url = fetch_archived_page(timestamp, url)
            if html:
                # Parse for file links
                extractor = LinkExtractor(base_url=url)
                try:
                    extractor.feed(html)
                except Exception:
                    pass

                for link in extractor.file_links:
                    link_url = link["url"]
                    ext = link.get("extension", "")
                    if ext in target_types:
                        if link_url not in all_file_links:
                            all_file_links[link_url] = link
                            all_file_links[link_url]["found_on"] = url
                    elif link.get("category") == "potential_download" and ext not in ALL_EXTENSIONS:
                        # Unknown extension - worth trying
                        if link_url not in all_file_links:
                            all_file_links[link_url] = link
                            all_file_links[link_url]["found_on"] = url

                # Extract URLs from JavaScript/JSON embedded in page
                # This catches audio players that load files via JS
                js_urls = extract_urls_from_text(html, url)
                for link in js_urls:
                    if link.get("extension", "") in target_types and link["url"] not in all_file_links:
                        all_file_links[link["url"]] = link
                        all_file_links[link["url"]]["found_on"] = url

                # Check for directory listings
                dir_parser = DirectoryParser(base_url=url)
                try:
                    dir_parser.feed(html)
                except Exception:
                    pass
                if dir_parser.is_directory_listing:
                    directory_listings.append({"url": url, "files": dir_parser.files})

                pages_crawled += 1

            progress.advance(task)

    console.print(f"  Crawled [green]{pages_crawled}[/green] pages")
    console.print(f"  Found [green]{len(all_file_links)}[/green] file links")
    if directory_listings:
        console.print(f"  Found [green]{len(directory_listings)}[/green] directory listings!")

    # Add files from directory listings
    for dl in directory_listings:
        for file_url in dl["files"]:
            ext = file_url.rsplit(".", 1)[-1].lower() if "." in file_url.split("/")[-1] else ""
            if ext in target_types and file_url not in all_file_links:
                all_file_links[file_url] = {
                    "url": file_url,
                    "extension": ext,
                    "category": "directory_listing",
                    "found_on": dl["url"],
                }

    if not all_file_links:
        console.print("\n[yellow]No file links found in crawled pages.[/yellow]")
        return []

    # Show breakdown
    by_category = defaultdict(list)
    for url, info in all_file_links.items():
        by_category[info.get("category", "other")].append(info)

    console.print(f"\n[bold]File links by type:[/bold]")
    for cat, links in sorted(by_category.items(), key=lambda x: -len(x[1])):
        console.print(f"  {cat}: {len(links)}")

    # Step 3: Pre-filter and prepare for download
    console.print(f"\n[bold]Step 3:[/bold] Pre-filtering {len(all_file_links)} file URLs...")

    # Pre-filter: remove obvious junk
    # - Tracking/analytics URLs
    # - URLs on major CDNs that wouldn't be archived (Google, Facebook, etc.)
    # - Duplicate filenames (keep first found)
    JUNK_DOMAINS = {"google-analytics.com", "facebook.com", "twitter.com", "googleapis.com",
                     "cloudflare.com", "amazon.com", "amazonaws.com", "akamai.net",
                     "doubleclick.net", "googlesyndication.com", "adsense", "adserver"}

    recoverable = []
    seen_filenames = set()
    filtered_count = 0
    for url, info in all_file_links.items():
        # Skip junk domains
        parsed = urlparse(url)
        host = parsed.hostname or ""
        if any(junk in host for junk in JUNK_DOMAINS):
            filtered_count += 1
            continue

        # Skip duplicate filenames
        fname = url.split("/")[-1].split("?")[0].lower()
        if fname in seen_filenames:
            filtered_count += 1
            continue
        seen_filenames.add(fname)

        # Use the timestamp of the page we found this link on
        found_page = info.get("found_on", "")
        ts = pages.get(found_page, "20100101000000")
        info["wayback_timestamp"] = ts
        info["wayback_size"] = 0
        recoverable.append(info)

    console.print(f"  {len(recoverable)} files to attempt ({filtered_count} filtered as junk/dupes)")
    console.print(f"  [dim]Each file tries up to 4 timestamps to find the best snapshot[/dim]")

    if not recoverable:
        return []

    # Display results
    table = Table(title="Recoverable Files", box=box.ROUNDED)
    table.add_column("File", style="cyan", max_width=50)
    table.add_column("Type", width=10)
    table.add_column("Size", justify="right", width=10)

    for info in recoverable[:30]:
        fname = info["url"].split("/")[-1].split("?")[0]
        if len(fname) > 50:
            fname = fname[:47] + "..."
        size_kb = info.get("wayback_size", 0) // 1024
        size_str = f"{size_kb}KB" if size_kb < 1024 else f"{size_kb // 1024}MB"
        table.add_row(fname, info.get("extension", "?"), size_str)

    console.print(table)
    if len(recoverable) > 30:
        console.print(f"  [dim]...and {len(recoverable) - 30} more[/dim]")

    # Step 4: Download
    if dest_dir:
        download = inquirer.confirm(
            message=f"Download {len(recoverable)} files to {dest_dir}?",
            default=True,
        ).execute()

        if download:
            os.makedirs(dest_dir, exist_ok=True)
            console.print(f"\n[bold]Step 4:[/bold] Downloading...")

            stats = {"ok": 0, "fail": 0, "skip": 0}
            fail_reasons = defaultdict(int)
            for info in recoverable:
                fname = sanitize_filename(unquote(info["url"].split("/")[-1].split("?")[0]))
                if not fname:
                    fname = f"file_{info.get('wayback_timestamp', 'unknown')}"
                dest_path = os.path.join(dest_dir, fname)

                if os.path.exists(dest_path) and os.path.getsize(dest_path) > 1000:
                    stats["skip"] += 1
                    continue

                ts = info.get("wayback_timestamp", "")
                ok, size, reason = download_from_wayback(ts, info["url"], dest_path)
                if ok:
                    console.print(f"  [green]OK[/green] {fname[:60]} ({size // 1024}KB)")
                    stats["ok"] += 1
                else:
                    fail_reasons[reason] += 1
                    stats["fail"] += 1

            console.print(f"\n  [green]{stats['ok']} downloaded[/green] | "
                          f"[yellow]{stats['skip']} skipped[/yellow] | "
                          f"[red]{stats['fail']} failed[/red]")

            if fail_reasons:
                console.print(f"\n  [bold]Failure breakdown:[/bold]")
                for reason, count in sorted(fail_reasons.items(), key=lambda x: -x[1]):
                    console.print(f"    [red]{count:>3d}[/red] {reason}")

    return recoverable


def mode_browse_targets():
    """Browse curated domain list and pick targets to crawl."""
    # Import the curated database from ghostlight
    try:
        from ghostlight import CURATED_TARGETS
    except ImportError:
        console.print("[red]Could not import CURATED_TARGETS from ghostlight.py[/red]")
        console.print("[dim]Make sure ghostlight.py is in the same directory.[/dim]")
        return None

    while True:
        categories = list(CURATED_TARGETS.keys())
        cat_choices = [{"name": f"{cat} ({len(CURATED_TARGETS[cat])} sites)", "value": cat} for cat in categories]
        cat_choices.append({"name": "<< Back", "value": "__back__"})
        selected_cat = inquirer.select(message="Pick a category:", choices=cat_choices).execute()

        if selected_cat == "__back__":
            return None

        targets = CURATED_TARGETS[selected_cat]

        from rich.table import Table
        table = Table(title=selected_cat, box=box.ROUNDED)
        table.add_column("#", width=3)
        table.add_column("Domain", style="cyan", max_width=40)
        table.add_column("Years", width=12)
        table.add_column("Status", width=12)
        table.add_column("What It Was", style="dim", max_width=50)

        for i, t in enumerate(targets, 1):
            status_style = "red" if "dead" in t["status"].lower() else "yellow" if "changed" in t["status"].lower() else "green"
            table.add_row(str(i), t["domain"], t["years"], f"[{status_style}]{t['status']}[/{status_style}]", t["note"][:50])

        console.print(table)

        target_choices = [{"name": f"{t['domain']} - {t['note'][:50]}", "value": i} for i, t in enumerate(targets)]
        target_choices.append({"name": "<< Back to categories", "value": "__back__"})
        selected = inquirer.select(message="Select a domain to crawl:", choices=target_choices).execute()

        if selected == "__back__":
            continue  # back to category picker

        target = targets[selected]
        domain = target["domain"]

        # Skip archive.org direct links
        if domain.startswith("archive.org/"):
            console.print(f"[yellow]This is a direct Archive.org collection - use Ghostlight to browse it instead.[/yellow]")
            continue

        return domain


def main():
    import argparse
    parser = argparse.ArgumentParser(description="GhostCrawl - Dead Site Crawler")
    parser.add_argument("--proxy", help="SOCKS5 proxy (e.g. socks5://127.0.0.1:9050)")
    parser.add_argument("--tor", action="store_true", help="Route all traffic through Tor (requires Tor on port 9050)")
    parser.add_argument("--tor-renew", type=int, default=50, metavar="N", help="Renew Tor circuit every N requests (default: 50)")
    parser.add_argument("--dest", help=f"Download directory (default: {DEFAULT_DEST})")
    args, _ = parser.parse_known_args()

    tor_mgr = None
    if args.tor:
        tor_mgr = TorManager(renew_every=args.tor_renew)
        ok, info = tor_mgr.verify()
        if ok:
            console.print(f"[bold green]Tor connected — exit IP: {info}[/bold green]")
        else:
            console.print(f"[bold red]Tor connection failed: {info}[/bold red]")
            console.print("[dim]Make sure Tor is running on port 9050 (install: apt install tor / brew install tor)[/dim]")
            sys.exit(1)

    get_request_manager(proxy=args.proxy, tor_manager=tor_mgr)

    console.print("\n[bold yellow]GHOSTCRAWL[/bold yellow] - Dead Site Crawler via Wayback Machine")
    console.print("[dim]Crawls dead sites through their archived HTML to find and recover files[/dim]\n")

    entry_mode = inquirer.select(
        message="How to pick a target?",
        choices=[
            {"name": "Browse curated domain list (250+ sites)", "value": "browse"},
            {"name": "Enter a domain manually", "value": "manual"},
            {"name": "\U0001f525 GOD MODE - Competitive AI agent swarm", "value": "god"},
        ],
    ).execute()

    if entry_mode == "god":
        from ghostcrawl_god_v2 import god_mode_main
        god_mode_main()
        return

    if entry_mode == "browse":
        domain = mode_browse_targets()
        if not domain:
            return
    else:
        domain = inquirer.text(message="Domain to crawl (e.g. deadsite.com):").execute()
        if not domain.strip():
            return

    domain = domain.strip().replace("http://", "").replace("https://", "").rstrip("/")

    type_choices = [{"name": f"{cat} ({', '.join(sorted(exts)[:5])}...)", "value": cat}
                    for cat, exts in INTERESTING_EXTENSIONS.items()]
    selected_types = inquirer.checkbox(
        message="File types to hunt for:",
        choices=type_choices,
    ).execute()

    target_exts = set()
    if selected_types:
        for cat in selected_types:
            target_exts.update(INTERESTING_EXTENSIONS[cat])
    else:
        target_exts = ALL_EXTENSIONS

    from_year = inquirer.text(message="Start year (default 2000):", default="2000").execute()
    to_year = inquirer.text(message="End year (default 2024):", default="2024").execute()

    max_pages = inquirer.text(message="Max pages to crawl (default 100):", default="100").execute()

    base_dest = args.dest if args.dest else DEFAULT_DEST
    dest_dir = os.path.join(base_dest, sanitize_filename(domain))

    crawl_dead_site(
        domain,
        target_types=target_exts,
        from_year=int(from_year),
        to_year=int(to_year),
        max_pages=int(max_pages),
        dest_dir=dest_dir,
    )


if __name__ == "__main__":
    main()
