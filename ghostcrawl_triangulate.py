#!/usr/bin/env python3
"""
GhostCrawl Cross-Archive Triangulation ("Omni-Index")

Queries multiple web archives simultaneously for the same URL/file.
When one archive has the HTML but not the binary, another might have it.

Supported archives:
  1. Wayback Machine (CDX API)
  2. CommonCrawl (CC-Index API)
  3. Archive.today / archive.ph (web scraping)
  4. Internet Archive Software Collection (items API)
  5. Arquivo.pt — Portuguese Web Archive (CDX API)
  6. Archive-It — Library/university curated collections (CDX/C API)
  7. urlscan.io — Historical DOM snapshots since 2016
  8. GitHub Code Search — Leaked files, keys, configs in public repos

Usage:
  from ghostcrawl_triangulate import triangulate_url, triangulate_domain

  # Check all archives for a single file
  results = triangulate_url("http://deadsite.com/files/game.zip")

  # Search all archives for a domain
  results = triangulate_domain("deadsite.com", extensions={"zip", "mp3"})
"""

import os
import requests
import re
import time
import json
import hashlib
import subprocess
from urllib.parse import quote, urljoin, urlparse
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict

from rich.console import Console
from rich.table import Table
from rich import box

console = Console()


@dataclass
class ArchiveHit:
    url: str
    archive: str
    timestamp: str = ""
    size: int = 0
    mimetype: str = ""
    status: str = ""
    download_url: str = ""
    confidence: float = 1.0


# ─── Wayback Machine ─────────────────────────────────────────────────────────

CDX_API = "https://web.archive.org/cdx/search/cdx"
WB_BASE = "https://web.archive.org/web"


def _query_wayback(url, limit=5):
    """Query the Wayback Machine CDX API for snapshots of a URL."""
    hits = []
    try:
        params = {
            "url": url,
            "output": "json",
            "limit": limit,
            "fl": "timestamp,original,mimetype,statuscode,length",
            "filter": "statuscode:200",
            "collapse": "digest",
        }
        resp = requests.get(CDX_API, params=params, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            if len(data) > 1:
                for row in data[1:]:
                    record = dict(zip(data[0], row))
                    hits.append(ArchiveHit(
                        url=record["original"],
                        archive="wayback",
                        timestamp=record["timestamp"],
                        size=int(record.get("length", 0) or 0),
                        mimetype=record.get("mimetype", ""),
                        status=record.get("statuscode", ""),
                        download_url=f"{WB_BASE}/{record['timestamp']}id_/{record['original']}",
                    ))
    except Exception as e:
        console.print(f"  [dim]Wayback query failed: {e}[/dim]")
    return hits


def _query_wayback_domain(domain, extensions=None, limit=200):
    """Query Wayback CDX for all files on a domain matching given extensions."""
    hits = []
    try:
        params = {
            "url": domain,
            "matchType": "domain",
            "output": "json",
            "limit": limit,
            "fl": "timestamp,original,mimetype,statuscode,length",
            "filter": "statuscode:200",
            "collapse": "urlkey",
        }
        resp = requests.get(CDX_API, params=params, timeout=30)
        if resp.status_code == 200:
            data = resp.json()
            if len(data) > 1:
                for row in data[1:]:
                    record = dict(zip(data[0], row))
                    original = record["original"]
                    ext = original.rsplit(".", 1)[-1].lower().split("?")[0] if "." in original.split("/")[-1] else ""
                    if extensions and ext not in extensions:
                        continue
                    hits.append(ArchiveHit(
                        url=original,
                        archive="wayback",
                        timestamp=record["timestamp"],
                        size=int(record.get("length", 0) or 0),
                        mimetype=record.get("mimetype", ""),
                        status=record.get("statuscode", ""),
                        download_url=f"{WB_BASE}/{record['timestamp']}id_/{original}",
                    ))
    except Exception as e:
        console.print(f"  [dim]Wayback domain query failed: {e}[/dim]")
    return hits


# ─── CommonCrawl ──────────────────────────────────────────────────────────────

CC_INDEX_API = "https://index.commoncrawl.org"


def _get_cc_indexes(max_indexes=3):
    """Get the most recent CommonCrawl index collection IDs."""
    try:
        resp = requests.get(f"{CC_INDEX_API}/collinfo.json", timeout=10)
        if resp.status_code == 200:
            indexes = resp.json()
            return [idx["cdx-api"] for idx in indexes[:max_indexes]]
    except Exception:
        pass
    # Fallback to known recent indexes
    return [
        f"{CC_INDEX_API}/CC-MAIN-2025-08-index",
        f"{CC_INDEX_API}/CC-MAIN-2024-51-index",
        f"{CC_INDEX_API}/CC-MAIN-2024-42-index",
    ]


def _query_commoncrawl(url, max_indexes=3):
    """Query CommonCrawl indexes for a URL."""
    hits = []
    indexes = _get_cc_indexes(max_indexes)

    for index_url in indexes:
        try:
            params = {"url": url, "output": "json", "limit": 5}
            resp = requests.get(index_url, params=params, timeout=15)
            if resp.status_code == 200 and resp.text.strip():
                for line in resp.text.strip().split("\n"):
                    try:
                        record = json.loads(line)
                        # CommonCrawl stores files in WARC archives — build the download URL
                        warc_file = record.get("filename", "")
                        offset = record.get("offset", "")
                        length = record.get("length", "")
                        download_url = ""
                        if warc_file and offset and length:
                            download_url = (
                                f"https://data.commoncrawl.org/{warc_file}"
                                f"?offset={offset}&length={length}"
                            )
                        hits.append(ArchiveHit(
                            url=record.get("url", url),
                            archive="commoncrawl",
                            timestamp=record.get("timestamp", ""),
                            size=int(record.get("length", 0) or 0),
                            mimetype=record.get("mime", record.get("mime-detected", "")),
                            status=record.get("status", ""),
                            download_url=download_url,
                            confidence=0.9,
                        ))
                    except (json.JSONDecodeError, ValueError):
                        continue
        except Exception:
            continue

    return hits


def _query_commoncrawl_domain(domain, extensions=None, max_indexes=2):
    """Query CommonCrawl for files on a domain."""
    hits = []
    indexes = _get_cc_indexes(max_indexes)

    for index_url in indexes:
        try:
            params = {"url": f"*.{domain}", "output": "json", "limit": 100}
            resp = requests.get(index_url, params=params, timeout=30)
            if resp.status_code == 200 and resp.text.strip():
                for line in resp.text.strip().split("\n"):
                    try:
                        record = json.loads(line)
                        url = record.get("url", "")
                        ext = url.rsplit(".", 1)[-1].lower().split("?")[0] if "." in url.split("/")[-1] else ""
                        if extensions and ext not in extensions:
                            continue
                        hits.append(ArchiveHit(
                            url=url,
                            archive="commoncrawl",
                            timestamp=record.get("timestamp", ""),
                            size=int(record.get("length", 0) or 0),
                            mimetype=record.get("mime", ""),
                            status=record.get("status", ""),
                            confidence=0.9,
                        ))
                    except (json.JSONDecodeError, ValueError):
                        continue
        except Exception:
            continue

    return hits


# ─── Archive.today ────────────────────────────────────────────────────────────


def _query_archive_today(url):
    """Check if Archive.today (archive.ph) has a snapshot of a URL."""
    hits = []
    try:
        check_url = f"https://archive.ph/newest/{url}"
        resp = requests.head(check_url, timeout=10, allow_redirects=True)
        if resp.status_code == 200 and "archive.ph" in resp.url:
            hits.append(ArchiveHit(
                url=url,
                archive="archive.today",
                download_url=resp.url,
                confidence=0.7,  # archive.today mostly captures HTML, not binaries
            ))
    except Exception:
        pass
    return hits


# ─── Internet Archive Software Collection ────────────────────────────────────

IA_SEARCH_API = "https://archive.org/advancedsearch.php"
IA_METADATA_API = "https://archive.org/metadata"


def _query_ia_software(filename, domain=""):
    """Search the Internet Archive's Software/CD-ROM collection for a file."""
    hits = []
    try:
        # Search by filename in the software and web collections
        query_parts = [f'"{filename}"']
        if domain:
            query_parts.append(f'"{domain}"')

        params = {
            "q": " ".join(query_parts),
            "fl[]": ["identifier", "title", "mediatype", "collection"],
            "rows": 10,
            "output": "json",
        }
        resp = requests.get(IA_SEARCH_API, params=params, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            docs = data.get("response", {}).get("docs", [])
            for doc in docs:
                identifier = doc.get("identifier", "")
                if not identifier:
                    continue
                # Check if this item actually contains our file
                meta_resp = requests.get(f"{IA_METADATA_API}/{identifier}/files", timeout=10)
                if meta_resp.status_code == 200:
                    files = meta_resp.json().get("result", [])
                    for f in files:
                        fname = f.get("name", "")
                        if filename.lower() in fname.lower():
                            hits.append(ArchiveHit(
                                url=f"https://archive.org/download/{identifier}/{fname}",
                                archive="ia_software",
                                size=int(f.get("size", 0) or 0),
                                mimetype=f.get("format", ""),
                                download_url=f"https://archive.org/download/{identifier}/{fname}",
                                confidence=0.95,
                            ))
                time.sleep(0.5)  # be nice to IA
    except Exception as e:
        console.print(f"  [dim]IA Software search failed: {e}[/dim]")
    return hits


# ─── Arquivo.pt (Portuguese Web Archive) ─────────────────────────────────

ARQUIVO_CDX = "https://arquivo.pt/wayback/cdx"
ARQUIVO_WB = "https://arquivo.pt/wayback"


def _parse_arquivo_jsonl(text):
    """Parse Arquivo.pt JSONL response (one JSON object per line)."""
    records = []
    for line in text.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def _query_arquivo_pt(url, limit=5):
    """Query Arquivo.pt CDX API — Portuguese web archive, independent from IA."""
    hits = []
    try:
        params = {
            "url": url,
            "output": "json",
            "limit": limit,
            "fl": "timestamp,original,mimetype,statuscode,length",
            "filter": "statuscode:200",
            "collapse": "digest",
        }
        resp = requests.get(ARQUIVO_CDX, params=params, timeout=15)
        if resp.status_code == 200 and resp.text.strip():
            for record in _parse_arquivo_jsonl(resp.text):
                hits.append(ArchiveHit(
                    url=record.get("original", url),
                    archive="arquivo.pt",
                    timestamp=record.get("timestamp", ""),
                    size=int(record.get("length", 0) or 0),
                    mimetype=record.get("mimetype", ""),
                    status=record.get("statuscode", ""),
                    download_url=f"{ARQUIVO_WB}/{record.get('timestamp', '')}id_/{record.get('original', url)}",
                    confidence=0.95,
                ))
    except Exception as e:
        console.print(f"  [dim]Arquivo.pt query failed: {e}[/dim]")
    return hits


def _query_arquivo_pt_domain(domain, extensions=None, limit=200):
    """Query Arquivo.pt for all files on a domain."""
    hits = []
    try:
        params = {
            "url": domain,
            "matchType": "domain",
            "output": "json",
            "limit": limit,
            "fl": "timestamp,original,mimetype,statuscode,length",
            "filter": "statuscode:200",
            "collapse": "urlkey",
        }
        resp = requests.get(ARQUIVO_CDX, params=params, timeout=30)
        if resp.status_code == 200 and resp.text.strip():
            for record in _parse_arquivo_jsonl(resp.text):
                original = record.get("original", "")
                ext = original.rsplit(".", 1)[-1].lower().split("?")[0] if "." in original.split("/")[-1] else ""
                if extensions and ext not in extensions:
                    continue
                hits.append(ArchiveHit(
                    url=original,
                    archive="arquivo.pt",
                    timestamp=record.get("timestamp", ""),
                    size=int(record.get("length", 0) or 0),
                    mimetype=record.get("mimetype", ""),
                    status=record.get("statuscode", ""),
                    download_url=f"{ARQUIVO_WB}/{record.get('timestamp', '')}id_/{original}",
                    confidence=0.95,
                ))
    except Exception as e:
        console.print(f"  [dim]Arquivo.pt domain query failed: {e}[/dim]")
    return hits


# ─── Arquivo.pt TextSearch (Full-Text Search Across Archives) ────────────

ARQUIVO_TEXTSEARCH = "https://arquivo.pt/textsearch"


def _query_arquivo_textsearch(query, domain=None, max_results=20):
    """Arquivo.pt's unique TextSearch API — full-text search across archived pages.

    This is something no other archive offers: search the CONTENT of archived pages.
    """
    hits = []
    try:
        params = {"q": query, "maxItems": max_results, "prettyPrint": "false"}
        if domain:
            params["siteSearch"] = domain
        resp = requests.get(ARQUIVO_TEXTSEARCH, params=params, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            for item in data.get("response_items", []):
                hits.append(ArchiveHit(
                    url=item.get("originalURL", ""),
                    archive="arquivo.pt-text",
                    timestamp=item.get("tstamp", ""),
                    mimetype=item.get("mimeType", ""),
                    download_url=item.get("linkToArchive", ""),
                    confidence=0.85,
                ))
    except Exception as e:
        console.print(f"  [dim]Arquivo.pt TextSearch failed: {e}[/dim]")
    return hits


# ─── Memento TimeMap (Multi-Archive Aggregator) ──────────────────────────

MEMENTO_TIMEMAP = "https://timetravel.mementoweb.org/timemap/json"
MEMENTO_TIMEGATE = "https://timetravel.mementoweb.org/timegate"


def _query_memento(url, limit=10):
    """Query Memento TimeMap — aggregates snapshots from MANY archives at once.

    This hits archives we don't query individually: Library of Congress,
    UK Web Archive, Stanford, Icelandic Web Archive, etc.
    """
    hits = []
    try:
        resp = requests.get(f"{MEMENTO_TIMEMAP}/{url}", timeout=15,
                            headers={"Accept": "application/json"})
        if resp.status_code == 200:
            data = resp.json()
            mementos = data.get("mementos", {}).get("list", [])
            seen_archives = set()
            for m in mementos[:limit]:
                archive_url = m.get("uri", "")
                # Extract archive name from the URL
                archive_host = urlparse(archive_url).hostname or ""
                archive_name = archive_host.split(".")[0] if archive_host else "unknown"

                # Skip wayback since we query it directly
                if "web.archive.org" in archive_url:
                    continue

                hits.append(ArchiveHit(
                    url=url,
                    archive=f"memento:{archive_name}",
                    timestamp=m.get("datetime", "").replace("-", "").replace(":", "").replace("T", "")[:14],
                    download_url=archive_url,
                    confidence=0.85,
                ))
    except Exception as e:
        console.print(f"  [dim]Memento TimeMap query failed: {e}[/dim]")
    return hits


def _query_memento_domain(domain, extensions=None):
    """Memento doesn't support domain-wide search — just return empty."""
    return []


# ─── urlscan.io (Historical DOM Snapshots) ───────────────────────────────

URLSCAN_API = "https://urlscan.io/api/v1"


def _query_urlscan(url_or_domain):
    """Search urlscan.io for historical scans — DOM snapshots since 2016."""
    hits = []
    api_key = os.environ.get("URLSCAN_API_KEY", "")
    headers = {}
    if api_key:
        headers["API-Key"] = api_key

    try:
        parsed = urlparse(url_or_domain) if "://" in url_or_domain else None
        domain = parsed.hostname if parsed else url_or_domain

        params = {"q": f"domain:{domain}", "size": 20}
        resp = requests.get(f"{URLSCAN_API}/search/", params=params, headers=headers, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            for result in data.get("results", []):
                page = result.get("page", {})
                task = result.get("task", {})
                scan_url = page.get("url", "")
                scan_id = result.get("_id", "")

                hits.append(ArchiveHit(
                    url=scan_url,
                    archive="urlscan.io",
                    timestamp=task.get("time", "").replace("-", "").replace(":", "").replace("T", "")[:14],
                    mimetype=page.get("mimeType", "text/html"),
                    status=str(page.get("status", "")),
                    download_url=f"https://urlscan.io/dom/{scan_id}/" if scan_id else "",
                    confidence=0.6,
                ))
    except Exception as e:
        console.print(f"  [dim]urlscan.io query failed: {e}[/dim]")
    return hits


def _query_urlscan_domain(domain, extensions=None):
    """Search urlscan.io for a domain — returns page URLs found in scans."""
    return _query_urlscan(domain)


# ─── GitHub Code Search (Leaked Files & Keys) ────────────────────────────


def _query_github_code(domain, patterns=None):
    """Search GitHub for leaked files referencing a domain.

    Looks for wallet files, .env, configs, private keys in public repos.
    Uses `gh` CLI if available, falls back to API with GITHUB_TOKEN.
    """
    hits = []
    if not patterns:
        patterns = [
            f'"{domain}" filename:wallet.dat',
            f'"{domain}" filename:.env',
            f'"{domain}" filename:config.php',
            f'"{domain}" filename:wp-config.php',
            f'"{domain}" PRIVATE_KEY',
            f'"{domain}" filename:id_rsa',
        ]

    token = os.environ.get("GITHUB_TOKEN", "")

    for pattern in patterns[:4]:  # limit to avoid rate limiting
        try:
            if token:
                headers = {
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github.v3+json",
                }
                params = {"q": pattern, "per_page": 5}
                resp = requests.get("https://api.github.com/search/code", params=params, headers=headers, timeout=10)
                if resp.status_code == 200:
                    for item in resp.json().get("items", []):
                        repo = item.get("repository", {}).get("full_name", "")
                        path = item.get("path", "")
                        html_url = item.get("html_url", "")
                        hits.append(ArchiveHit(
                            url=html_url,
                            archive="github",
                            mimetype=path.rsplit(".", 1)[-1] if "." in path else "",
                            download_url=html_url.replace("github.com", "raw.githubusercontent.com").replace("/blob/", "/") if html_url else "",
                            confidence=0.8,
                        ))
            else:
                # Try gh CLI
                result = subprocess.run(
                    ["gh", "api", "search/code", "-f", f"q={pattern}", "-f", "per_page=3"],
                    capture_output=True, text=True, timeout=15,
                )
                if result.returncode == 0:
                    data = json.loads(result.stdout)
                    for item in data.get("items", []):
                        repo = item.get("repository", {}).get("full_name", "")
                        path = item.get("path", "")
                        html_url = item.get("html_url", "")
                        hits.append(ArchiveHit(
                            url=html_url,
                            archive="github",
                            mimetype=path.rsplit(".", 1)[-1] if "." in path else "",
                            download_url=html_url.replace("github.com", "raw.githubusercontent.com").replace("/blob/", "/") if html_url else "",
                            confidence=0.8,
                        ))
        except Exception:
            continue
        time.sleep(2)  # GitHub rate limit is aggressive

    return hits


# ─── VirusTotal (Historical URL Submissions) ─────────────────────────────

VT_API = "https://www.virustotal.com/api/v3"


def _query_virustotal(domain):
    """Search VirusTotal for historical submissions referencing a domain.

    Reveals file hashes that were once hosted on the domain — proof files existed.
    """
    hits = []
    api_key = os.environ.get("VIRUSTOTAL_API_KEY", "")
    if not api_key:
        return hits

    try:
        headers = {"x-apikey": api_key}
        resp = requests.get(f"{VT_API}/domains/{domain}/urls", headers=headers, timeout=15,
                            params={"limit": 20})
        if resp.status_code == 200:
            data = resp.json()
            for item in data.get("data", []):
                attrs = item.get("attributes", {})
                url = attrs.get("url", "")
                hits.append(ArchiveHit(
                    url=url,
                    archive="virustotal",
                    timestamp=str(attrs.get("last_submission_date", "")),
                    status=str(attrs.get("last_http_response_code", "")),
                    mimetype=attrs.get("last_http_response_content_type", ""),
                    confidence=0.5,  # VT proves existence, can't always download
                ))
    except Exception as e:
        console.print(f"  [dim]VirusTotal query failed: {e}[/dim]")
    return hits


def _query_virustotal_domain(domain, extensions=None):
    """VirusTotal domain search with optional extension filter."""
    hits = _query_virustotal(domain)
    if extensions:
        hits = [h for h in hits if any(h.url.lower().endswith(f".{ext}") for ext in extensions)]
    return hits


# ─── Shodan (Live Open Directory Discovery) ──────────────────────────────

SHODAN_API = "https://api.shodan.io"


def _query_shodan_opendirs(query="title:index of wallet.dat", limit=20):
    """Search Shodan for live open directories with interesting files."""
    hits = []
    api_key = os.environ.get("SHODAN_API_KEY", "")
    if not api_key:
        return hits

    try:
        params = {"key": api_key, "query": query, "minify": True}
        resp = requests.get(f"{SHODAN_API}/shodan/host/search", params=params, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            for match in data.get("matches", [])[:limit]:
                ip = match.get("ip_str", "")
                port = match.get("port", 80)
                hostnames = match.get("hostnames", [])
                host = hostnames[0] if hostnames else ip
                proto = "https" if port == 443 else "http"
                url = f"{proto}://{host}:{port}/"

                hits.append(ArchiveHit(
                    url=url,
                    archive="shodan",
                    mimetype="directory",
                    download_url=url,
                    confidence=0.7,
                ))
    except Exception as e:
        console.print(f"  [dim]Shodan query failed: {e}[/dim]")
    return hits


# ─── Censys (Live Infrastructure Search) ─────────────────────────────────

CENSYS_API = "https://search.censys.io/api/v2"


def _query_censys(domain):
    """Search Censys for live hosts associated with a domain."""
    hits = []
    api_id = os.environ.get("CENSYS_API_ID", "")
    api_secret = os.environ.get("CENSYS_API_SECRET", "")
    if not api_id or not api_secret:
        return hits

    try:
        resp = requests.get(
            f"{CENSYS_API}/hosts/search",
            params={"q": domain, "per_page": 10},
            auth=(api_id, api_secret),
            timeout=15,
        )
        if resp.status_code == 200:
            data = resp.json()
            for hit_data in data.get("result", {}).get("hits", []):
                ip = hit_data.get("ip", "")
                services = hit_data.get("services", [])
                for svc in services:
                    port = svc.get("port", 80)
                    proto = "https" if svc.get("transport_protocol") == "TLS" or port == 443 else "http"
                    url = f"{proto}://{ip}:{port}/"
                    hits.append(ArchiveHit(
                        url=url,
                        archive="censys",
                        mimetype=svc.get("service_name", ""),
                        download_url=url,
                        confidence=0.6,
                    ))
    except Exception as e:
        console.print(f"  [dim]Censys query failed: {e}[/dim]")
    return hits


# ─── Public API ───────────────────────────────────────────────────────────────


def triangulate_url(url, include_ia_software=True, include_live=False):
    """
    Search all supported archives for a single URL.

    Returns a dict keyed by archive name, each containing a list of ArchiveHit.
    """
    results = defaultdict(list)
    filename = url.split("/")[-1].split("?")[0]
    domain = urlparse(url).hostname or ""

    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {
            # Tier 1: Archive CDX APIs
            pool.submit(_query_wayback, url): "wayback",
            pool.submit(_query_arquivo_pt, url): "arquivo.pt",
            pool.submit(_query_memento, url): "memento",
            # Tier 1: Other archives
            pool.submit(_query_commoncrawl, url): "commoncrawl",
            pool.submit(_query_archive_today, url): "archive.today",
            # Tier 2: Alternative sources
            pool.submit(_query_urlscan, url): "urlscan.io",
        }
        if include_ia_software and filename:
            futures[pool.submit(_query_ia_software, filename, domain)] = "ia_software"
        if domain:
            futures[pool.submit(_query_virustotal, domain)] = "virustotal"
            futures[pool.submit(_query_github_code, domain)] = "github"

        for future in as_completed(futures):
            archive = futures[future]
            try:
                hits = future.result()
                results[archive].extend(hits)
            except Exception:
                pass

    return dict(results)


def triangulate_domain(domain, extensions=None, include_ia_software=False, include_live=False):
    """
    Search all archives for files on a domain.

    Returns a merged, deduplicated list of ArchiveHit objects.
    """
    all_hits = []

    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {
            # Tier 1: Archive CDX APIs (all run in parallel)
            pool.submit(_query_wayback_domain, domain, extensions): "wayback",
            pool.submit(_query_arquivo_pt_domain, domain, extensions): "arquivo.pt",
            pool.submit(_query_memento_domain, domain, extensions): "memento",
            pool.submit(_query_commoncrawl_domain, domain, extensions): "commoncrawl",
            # Tier 2: Alternative sources
            pool.submit(_query_urlscan_domain, domain, extensions): "urlscan.io",
            pool.submit(_query_virustotal_domain, domain, extensions): "virustotal",
            pool.submit(_query_github_code, domain): "github",
        }
        # Tier 3: Live discovery (opt-in)
        if include_live:
            futures[pool.submit(_query_censys, domain)] = "censys"

        for future in as_completed(futures):
            try:
                hits = future.result()
                all_hits.extend(hits)
            except Exception:
                pass

    # Deduplicate by URL, keeping the hit with the largest size
    by_url = {}
    for hit in all_hits:
        normalized = hit.url.rstrip("/").lower()
        if normalized not in by_url or hit.size > by_url[normalized].size:
            by_url[normalized] = hit

    return list(by_url.values())


def display_triangulation(results, title="Cross-Archive Triangulation"):
    """Pretty-print triangulation results."""
    table = Table(title=title, box=box.ROUNDED)
    table.add_column("Archive", style="bold", width=16)
    table.add_column("Hits", justify="right", width=6)
    table.add_column("URL / File", style="cyan", max_width=55)
    table.add_column("Size", justify="right", width=10)
    table.add_column("Type", width=15)

    total_hits = 0
    for archive, hits in results.items():
        if not hits:
            table.add_row(archive, "0", "[dim]nothing found[/dim]", "", "")
            continue
        for i, hit in enumerate(hits[:5]):
            fname = hit.url.split("/")[-1].split("?")[0][:55]
            size_str = f"{hit.size // 1024}KB" if hit.size > 0 else "?"
            table.add_row(
                archive if i == 0 else "",
                str(len(hits)) if i == 0 else "",
                fname,
                size_str,
                hit.mimetype[:15] if hit.mimetype else "",
            )
            total_hits += 1
        if len(hits) > 5:
            table.add_row("", "", f"[dim]...and {len(hits) - 5} more[/dim]", "", "")

    console.print(table)

    # Summary
    archives_with_hits = [a for a, h in results.items() if h]
    if len(archives_with_hits) > 1:
        console.print(f"\n  [green]Found across {len(archives_with_hits)} archives![/green] "
                      f"Cross-reference to maximize recovery.")
    elif len(archives_with_hits) == 1:
        console.print(f"\n  Found in [green]{archives_with_hits[0]}[/green] only.")
    else:
        console.print(f"\n  [red]Not found in any archive.[/red]")

    return total_hits


# ─── CLI ──────────────────────────────────────────────────────────────────────


def main(target_override=None):
    import argparse
    if target_override:
        target = target_override.strip()
        extensions = None
        no_ia = False
        live = False
    else:
        parser = argparse.ArgumentParser(description="GhostCrawl Cross-Archive Triangulation")
        parser.add_argument("target", help="URL or domain to search across archives")
        parser.add_argument("--extensions", "-e", help="Comma-separated file extensions to filter (domain mode)")
        parser.add_argument("--no-ia", action="store_true", help="Skip Internet Archive Software Collection search")
        parser.add_argument("--live", action="store_true", help="Include live discovery (Shodan, Censys)")
        args = parser.parse_args()
        target = args.target.strip()
        extensions = args.extensions
        no_ia = args.no_ia
        live = args.live

    # Show which API keys are available
    key_status = []
    for name, env in [("GitHub", "GITHUB_TOKEN"), ("Shodan", "SHODAN_API_KEY"),
                      ("urlscan", "URLSCAN_API_KEY"), ("VirusTotal", "VIRUSTOTAL_API_KEY"),
                      ("Censys", "CENSYS_API_ID")]:
        if os.environ.get(env):
            key_status.append(f"[green]{name}[/green]")
        else:
            key_status.append(f"[dim]{name}[/dim]")

    console.print(f"\n[bold yellow]GHOSTCRAWL TRIANGULATOR[/bold yellow] [dim](Omni-Index v2)[/dim]")
    console.print(f"[dim]Searching all archives for:[/dim] [cyan]{target}[/cyan]")
    console.print(f"[dim]API keys:[/dim] {' '.join(key_status)}\n")

    if target.startswith("http://") or target.startswith("https://"):
        # Single URL mode
        console.print("[bold]Mode:[/bold] Single URL triangulation")
        results = triangulate_url(target, include_ia_software=not no_ia, include_live=live)
        display_triangulation(results, f"Triangulation: {target.split('/')[-1][:40]}")
    else:
        # Domain mode
        ext_set = None
        if extensions:
            ext_set = set(extensions.split(","))
        console.print(f"[bold]Mode:[/bold] Domain-wide search")
        hits = triangulate_domain(target, extensions=ext_set, include_live=live)

        # Group by archive for display
        results = defaultdict(list)
        for hit in hits:
            results[hit.archive].append(hit)
        display_triangulation(dict(results), f"Domain: {target}")

        console.print(f"\n  Total unique files: [green]{len(hits)}[/green]")


if __name__ == "__main__":
    main()
