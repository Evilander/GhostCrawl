#!/usr/bin/env python3
"""
GhostCrawl Common Crawl Deep Miner

The nuclear option for when Wayback Machine blocks you (robots.txt / DMCA).
Common Crawl doesn't respect robots.txt — it crawls EVERYTHING and stores
the raw WARC files on S3, searchable via their CDX index API.

Features:
  - Search ALL 120+ Common Crawl indexes (2008-present) for any domain
  - Extract actual page content from WARC files (not just URLs)
  - Full-text keyword search WITHIN archived pages
  - Extract all media URLs, links, form data from archived HTML
  - Reconstruct forum threads from fragments
  - Download recovered media files
  - CDN archaeology: find media hosted on separate CDN domains

Usage:
  python ghostcrawl_commoncrawl.py <domain> [--keyword WORD] [--extract-media] [--download]
  python ghostcrawl_commoncrawl.py oldsite.com/files/ --keyword "rare"
  python ghostcrawl_commoncrawl.py deadmusic.com --extract-media --download
"""

import requests
import os
import sys
import json
import gzip
import time
import re
import hashlib
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse, urljoin, unquote
from collections import defaultdict
from datetime import datetime

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

DEFAULT_DEST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads")
CC_INDEX_BASE = "https://index.commoncrawl.org"
CC_DATA_BASE = "https://data.commoncrawl.org"

# ═══════════════════════════════════════════════════════════════════
# COMMON CRAWL INDEX MANAGER
# ═══════════════════════════════════════════════════════════════════

class CCIndexManager:
    """
    Manages Common Crawl index discovery and searching.

    Uses TWO methods for resilience:
    1. Primary: Direct S3 access to CDX index shards (bypasses index.commoncrawl.org)
    2. Fallback: Standard CDX API via index.commoncrawl.org

    The S3 approach reads cluster.idx to find which shard file contains a given
    domain, then does a byte-range fetch of just that shard — works even when
    the index server is completely down.
    """

    # Known CC crawl IDs (subset covering major crawls 2013-2025)
    KNOWN_CRAWLS = [
        "CC-MAIN-2013-20", "CC-MAIN-2013-48",
        "CC-MAIN-2014-10", "CC-MAIN-2014-15", "CC-MAIN-2014-23",
        "CC-MAIN-2014-35", "CC-MAIN-2014-41", "CC-MAIN-2014-42",
        "CC-MAIN-2014-49", "CC-MAIN-2014-52",
        "CC-MAIN-2015-06", "CC-MAIN-2015-11", "CC-MAIN-2015-14",
        "CC-MAIN-2015-18", "CC-MAIN-2015-22", "CC-MAIN-2015-27",
        "CC-MAIN-2015-32", "CC-MAIN-2015-35", "CC-MAIN-2015-40",
        "CC-MAIN-2015-48",
        "CC-MAIN-2016-07", "CC-MAIN-2016-18", "CC-MAIN-2016-22",
        "CC-MAIN-2016-26", "CC-MAIN-2016-30", "CC-MAIN-2016-36",
        "CC-MAIN-2016-40", "CC-MAIN-2016-44", "CC-MAIN-2016-50",
        "CC-MAIN-2017-04", "CC-MAIN-2017-09", "CC-MAIN-2017-13",
        "CC-MAIN-2017-17", "CC-MAIN-2017-22", "CC-MAIN-2017-26",
        "CC-MAIN-2017-30", "CC-MAIN-2017-34", "CC-MAIN-2017-39",
        "CC-MAIN-2017-43", "CC-MAIN-2017-47", "CC-MAIN-2017-51",
        "CC-MAIN-2018-05", "CC-MAIN-2018-09", "CC-MAIN-2018-13",
        "CC-MAIN-2018-17", "CC-MAIN-2018-22", "CC-MAIN-2018-26",
        "CC-MAIN-2018-30", "CC-MAIN-2018-34", "CC-MAIN-2018-39",
        "CC-MAIN-2018-43", "CC-MAIN-2018-47", "CC-MAIN-2018-51",
        "CC-MAIN-2019-04", "CC-MAIN-2019-09", "CC-MAIN-2019-13",
        "CC-MAIN-2019-18", "CC-MAIN-2019-22", "CC-MAIN-2019-26",
        "CC-MAIN-2019-30", "CC-MAIN-2019-35", "CC-MAIN-2019-39",
        "CC-MAIN-2019-43", "CC-MAIN-2019-47", "CC-MAIN-2019-51",
        "CC-MAIN-2020-05", "CC-MAIN-2020-10", "CC-MAIN-2020-16",
        "CC-MAIN-2020-24", "CC-MAIN-2020-29", "CC-MAIN-2020-34",
        "CC-MAIN-2020-40", "CC-MAIN-2020-45", "CC-MAIN-2020-50",
        "CC-MAIN-2021-04", "CC-MAIN-2021-10", "CC-MAIN-2021-17",
        "CC-MAIN-2021-21", "CC-MAIN-2021-25", "CC-MAIN-2021-31",
        "CC-MAIN-2021-39", "CC-MAIN-2021-43", "CC-MAIN-2021-49",
        "CC-MAIN-2022-05", "CC-MAIN-2022-21", "CC-MAIN-2022-27",
        "CC-MAIN-2022-33", "CC-MAIN-2022-40", "CC-MAIN-2022-49",
        "CC-MAIN-2023-06", "CC-MAIN-2023-14", "CC-MAIN-2023-23",
        "CC-MAIN-2023-40", "CC-MAIN-2023-50",
        "CC-MAIN-2024-10", "CC-MAIN-2024-18", "CC-MAIN-2024-22",
        "CC-MAIN-2024-26", "CC-MAIN-2024-30", "CC-MAIN-2024-33",
        "CC-MAIN-2024-38", "CC-MAIN-2024-42", "CC-MAIN-2024-46",
        "CC-MAIN-2024-51",
        "CC-MAIN-2025-05", "CC-MAIN-2025-08",
    ]

    def __init__(self, rate_limit=0.3):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                          'AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36',
        })
        self.rate_limit = rate_limit
        self.indexes = []
        self._load_indexes()

    def _load_indexes(self):
        """Load CC index list. Try API first, fall back to hardcoded list."""
        try:
            r = self.session.get(f"{CC_INDEX_BASE}/collinfo.json", timeout=10)
            if r.status_code == 200:
                self.indexes = r.json()
                print(f"[CC] Loaded {len(self.indexes)} indexes from API")
                return
        except Exception:
            pass

        # API down — use hardcoded crawl IDs
        self.indexes = [
            {"id": cid, "cdx-api": f"{CC_INDEX_BASE}/{cid}-index"}
            for cid in self.KNOWN_CRAWLS
        ]
        print(f"[CC] Index API down, using {len(self.indexes)} known crawl IDs")

    def _domain_to_surt(self, domain):
        """Convert domain to SURT format for cluster.idx binary search.
        e.g., 'example.com' -> 'com,example)'
        """
        # Strip protocol and path
        domain = domain.split('//')[- 1].split('/')[0].split('?')[0]
        domain = domain.replace('www.', '')
        parts = domain.split('.')
        parts.reverse()
        return ','.join(parts) + ')'

    def _find_shard_ranges(self, crawl_id, domain):
        """Find which CDX shard(s) contain data for a domain using cluster.idx.
        Streams the file line-by-line to avoid OOM on large indexes."""

        surt = self._domain_to_surt(domain)
        # e.g., for "example.com" -> "com,example)" — we need to match any entry
        # whose SURT prefix starts with this
        surt_prefix = surt.rstrip(')')  # "com,example" for partial matching

        url = f"{CC_DATA_BASE}/cc-index/collections/{crawl_id}/indexes/cluster.idx"

        try:
            r = self.session.get(url, timeout=45, stream=True)
            if r.status_code != 200:
                return []

            ranges = []
            prev_entry = None
            found = False

            for raw_line in r.iter_lines():
                if not raw_line:
                    continue
                line = raw_line.decode('utf-8', errors='replace') if isinstance(raw_line, bytes) else raw_line

                entry_surt = line.split('\t')[0].split(' ')[0]

                if not found:
                    if entry_surt >= surt:
                        found = True
                        if prev_entry:
                            ranges.append(prev_entry)
                        ranges.append(line)
                    else:
                        prev_entry = line
                else:
                    # Grab one entry past our domain to ensure we have the full range
                    ranges.append(line)
                    # Stop once we've clearly passed our domain
                    if not entry_surt.startswith(surt_prefix):
                        break

            r.close()

            if not ranges and prev_entry:
                ranges.append(prev_entry)

        except Exception:
            return []

        # Parse shard info from matching entries
        shard_info = []
        seen = set()
        for entry in ranges:
            parts = entry.split('\t')
            if len(parts) >= 4:
                shard_file = parts[1]
                offset = int(parts[2])
                length = int(parts[3])
                key = (shard_file, offset, length)
                if key not in seen:
                    seen.add(key)
                    shard_info.append({
                        'shard': shard_file,
                        'offset': offset,
                        'length': length,
                    })

        return shard_info

    def _search_shard(self, crawl_id, shard_info, url_pattern):
        """Fetch a CDX shard via byte-range and extract matching records."""
        shard_url = f"{CC_DATA_BASE}/cc-index/collections/{crawl_id}/indexes/{shard_info['shard']}"

        try:
            r = self.session.get(shard_url, headers={
                'Range': f"bytes={shard_info['offset']}-{shard_info['offset'] + shard_info['length'] - 1}"
            }, timeout=30)

            if r.status_code not in (200, 206):
                return []

            text = gzip.decompress(r.content).decode('utf-8', errors='replace')
            records = []

            # Extract domain from url_pattern for matching
            match_domain = url_pattern.split('/')[0].replace('*.', '').replace('www.', '')
            match_path = '/'.join(url_pattern.split('/')[1:]) if '/' in url_pattern else ''
            match_path = match_path.rstrip('*')

            for line in text.strip().split('\n'):
                parts = line.split(' ', 2)
                if len(parts) < 3:
                    continue
                try:
                    data = json.loads(parts[2])
                    url = data.get('url', '')
                    url_clean = url.replace('http://', '').replace('https://', '').replace('www.', '')

                    if match_domain in url_clean:
                        if not match_path or match_path in url_clean:
                            data['_cc_index'] = crawl_id
                            records.append(data)
                except (json.JSONDecodeError, KeyError):
                    pass

            return records
        except Exception:
            return []

    def search_s3_direct(self, url_pattern, year_range=None, max_indexes=None, callback=None):
        """
        Search CC indexes via direct S3 access (bypasses index server).
        This is the nuclear option — works when index.commoncrawl.org is down.
        """
        results = []
        seen_urls = set()

        # Extract domain from pattern
        domain = url_pattern.split('/')[0].replace('*', '').strip('.')

        crawls = [idx.get('id', '') for idx in self.indexes if idx.get('id')]

        if year_range:
            start_year, end_year = year_range
            crawls = [c for c in crawls
                      if any(str(y) in c for y in range(start_year, end_year + 1))]

        if max_indexes:
            crawls = crawls[:max_indexes]

        total = len(crawls)
        for i, crawl_id in enumerate(crawls):
            if callback:
                callback(crawl_id, 0, len(results), f"[{i+1}/{total}] scanning cluster.idx")

            time.sleep(self.rate_limit)

            shards = self._find_shard_ranges(crawl_id, domain)
            if not shards:
                continue

            crawl_new = 0
            for shard in shards:
                time.sleep(self.rate_limit)
                records = self._search_shard(crawl_id, shard, url_pattern)

                for rec in records:
                    url = rec.get('url', '')
                    if url and url not in seen_urls:
                        seen_urls.add(url)
                        results.append(rec)
                        crawl_new += 1

            if crawl_new > 0 and callback:
                callback(crawl_id, crawl_new, len(results))

        return results

    def search_all(self, url_pattern, limit_per_index=50, max_indexes=None,
                   year_range=None, callback=None):
        """
        Search Common Crawl indexes for a URL pattern.
        Tries direct S3 access first, falls back to CDX API.
        """
        # Try S3 direct first (more reliable)
        results = self.search_s3_direct(url_pattern, year_range=year_range,
                                        max_indexes=max_indexes, callback=callback)
        if results:
            return results

        # Fallback to CDX API
        seen_urls = set()
        indexes_to_search = list(self.indexes)

        if year_range:
            start_year, end_year = year_range
            indexes_to_search = [
                idx for idx in indexes_to_search
                if any(str(y) in (idx.get('id', '') or idx.get('name', ''))
                       for y in range(start_year, end_year + 1))
            ]

        if max_indexes:
            indexes_to_search = indexes_to_search[:max_indexes]

        for i, idx_info in enumerate(indexes_to_search):
            idx_url = idx_info.get('cdx-api', '')
            idx_id = idx_info.get('id', idx_info.get('name', f'index-{i}'))

            if not idx_url:
                continue

            time.sleep(self.rate_limit)

            try:
                r = self.session.get(idx_url, params={
                    "url": url_pattern,
                    "output": "json",
                    "limit": limit_per_index,
                }, timeout=15)

                if r.status_code == 200 and r.text.strip():
                    new_count = 0
                    for line in r.text.strip().split('\n'):
                        try:
                            record = json.loads(line)
                            url = record.get('url', '')
                            if url and url not in seen_urls:
                                seen_urls.add(url)
                                record['_cc_index'] = idx_id
                                results.append(record)
                                new_count += 1
                        except json.JSONDecodeError:
                            pass

                    if new_count > 0 and callback:
                        callback(idx_id, new_count, len(results))

            except requests.exceptions.ReadTimeout:
                if callback:
                    callback(idx_id, -1, len(results))
            except Exception:
                pass

        return results


# ═══════════════════════════════════════════════════════════════════
# WARC CONTENT FETCHER
# ═══════════════════════════════════════════════════════════════════

class WARCFetcher:
    """Fetches and extracts content from Common Crawl WARC files on S3."""

    def __init__(self, rate_limit=0.5):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'GhostCrawl-CC/1.0',
        })
        self.rate_limit = rate_limit
        self.cache = {}  # url -> content

    def fetch_record(self, record):
        """
        Fetch a single WARC record and return the decompressed content.
        Returns (url, html_content, headers) or None on failure.
        """
        filename = record.get('filename', '')
        offset = int(record.get('offset', 0))
        length = int(record.get('length', 0))
        url = record.get('url', '')

        if not filename or not length:
            return None

        if url in self.cache:
            return self.cache[url]

        time.sleep(self.rate_limit)

        try:
            warc_url = f"{CC_DATA_BASE}/{filename}"
            r = self.session.get(warc_url, headers={
                "Range": f"bytes={offset}-{offset+length-1}",
            }, timeout=45)

            if r.status_code not in (200, 206):
                return None

            # Decompress gzip
            try:
                raw = gzip.decompress(r.content)
            except Exception:
                raw = r.content

            text = raw.decode('utf-8', errors='replace')

            # Parse WARC record — extract HTTP response body
            # WARC format: WARC header \r\n\r\n HTTP header \r\n\r\n body
            parts = text.split('\r\n\r\n', 2)
            if len(parts) >= 3:
                warc_header = parts[0]
                http_header = parts[1]
                body = parts[2]
            elif len(parts) == 2:
                http_header = parts[0]
                body = parts[1]
            else:
                body = text

            result = (url, body, {})
            self.cache[url] = result
            return result

        except Exception as e:
            return None

    def fetch_batch(self, records, max_workers=3, callback=None):
        """Fetch multiple WARC records with optional parallelism."""
        results = []
        total = len(records)

        for i, record in enumerate(records):
            result = self.fetch_record(record)
            if result:
                results.append(result)
                if callback:
                    callback(i + 1, total, result[0])

        return results


# ═══════════════════════════════════════════════════════════════════
# CONTENT ANALYZER
# ═══════════════════════════════════════════════════════════════════

class ContentAnalyzer:
    """Analyzes HTML content extracted from WARC files."""

    @staticmethod
    def extract_media_urls(html, base_url=""):
        """Extract all media URLs from HTML content."""
        media = {
            'images': [],
            'videos': [],
            'audio': [],
            'documents': [],
            'archives': [],
            'other': [],
        }

        # Image sources
        for pattern in [
            r'src="([^"]*\.(?:jpg|jpeg|png|gif|webp|bmp|tiff|svg)[^"]*)"',
            r"src='([^']*\.(?:jpg|jpeg|png|gif|webp|bmp|tiff|svg)[^']*)'",
            r'data-src="([^"]*\.(?:jpg|jpeg|png|gif|webp)[^"]*)"',
            r'data-original="([^"]*\.(?:jpg|jpeg|png|gif|webp)[^"]*)"',
            r'data-lazy="([^"]*\.(?:jpg|jpeg|png|gif|webp)[^"]*)"',
            r'background-image:\s*url\(["\']?([^"\')\s]+\.(?:jpg|jpeg|png|gif|webp))["\']?\)',
        ]:
            for match in re.findall(pattern, html, re.IGNORECASE):
                url = ContentAnalyzer._resolve_url(match, base_url)
                if url:
                    media['images'].append(url)

        # Full-size image links (thumbnails linking to originals)
        for match in re.findall(r'href="([^"]*\.(?:jpg|jpeg|png|gif|webp|bmp)[^"]*)"', html, re.IGNORECASE):
            url = ContentAnalyzer._resolve_url(match, base_url)
            if url and url not in media['images']:
                media['images'].append(url)

        # Video sources
        for pattern in [
            r'src="([^"]*\.(?:mp4|avi|mkv|wmv|flv|mov|webm|m4v|mpg|mpeg)[^"]*)"',
            r'href="([^"]*\.(?:mp4|avi|mkv|wmv|flv|mov|webm|m4v)[^"]*)"',
            r'data-src="([^"]*\.(?:mp4|webm|m4v)[^"]*)"',
            r'<source[^>]*src="([^"]*\.(?:mp4|webm|ogg)[^"]*)"',
        ]:
            for match in re.findall(pattern, html, re.IGNORECASE):
                url = ContentAnalyzer._resolve_url(match, base_url)
                if url:
                    media['videos'].append(url)

        # Audio
        for pattern in [
            r'src="([^"]*\.(?:mp3|wav|flac|ogg|aac|m4a|wma|mid|midi)[^"]*)"',
            r'href="([^"]*\.(?:mp3|wav|flac|ogg|aac|m4a|wma|mid|midi)[^"]*)"',
        ]:
            for match in re.findall(pattern, html, re.IGNORECASE):
                url = ContentAnalyzer._resolve_url(match, base_url)
                if url:
                    media['audio'].append(url)

        # Documents
        for match in re.findall(r'href="([^"]*\.(?:pdf|doc|docx|xls|xlsx|ppt|pptx|txt|csv|rtf)[^"]*)"',
                                html, re.IGNORECASE):
            url = ContentAnalyzer._resolve_url(match, base_url)
            if url:
                media['documents'].append(url)

        # Archives
        for match in re.findall(r'href="([^"]*\.(?:zip|rar|7z|tar|gz|bz2|tgz)[^"]*)"',
                                html, re.IGNORECASE):
            url = ContentAnalyzer._resolve_url(match, base_url)
            if url:
                media['archives'].append(url)

        # Flash / legacy
        for pattern in [
            r'src="([^"]*\.(?:swf|dcr|dir)[^"]*)"',
            r'data="([^"]*\.(?:swf|dcr|dir)[^"]*)"',
            r'value="([^"]*\.(?:swf|dcr|dir)[^"]*)"',
        ]:
            for match in re.findall(pattern, html, re.IGNORECASE):
                url = ContentAnalyzer._resolve_url(match, base_url)
                if url:
                    media['other'].append(url)

        # Deduplicate
        for key in media:
            media[key] = list(dict.fromkeys(media[key]))

        return media

    @staticmethod
    def extract_all_links(html, base_url=""):
        """Extract all links from HTML."""
        links = []
        for match in re.findall(r'href="([^"]+)"', html, re.IGNORECASE):
            url = ContentAnalyzer._resolve_url(match, base_url)
            if url:
                links.append(url)
        return list(dict.fromkeys(links))

    @staticmethod
    def keyword_search(html, keywords, context_chars=120):
        """Search HTML content for keywords, return matches with context."""
        # Strip HTML tags for text search
        text = re.sub(r'<[^>]+>', ' ', html)
        text = re.sub(r'\s+', ' ', text)
        text_lower = text.lower()

        matches = []
        for keyword in keywords:
            kw_lower = keyword.lower()
            for m in re.finditer(re.escape(kw_lower), text_lower):
                idx = m.start()
                context = text[max(0, idx - context_chars):idx + len(keyword) + context_chars].strip()
                matches.append({
                    'keyword': keyword,
                    'position': idx,
                    'context': context,
                })

        return matches

    @staticmethod
    def extract_forum_data(html):
        """Extract structured data from imageboard/forum HTML."""
        data = {
            'thread_subjects': [],
            'post_count': 0,
            'image_count': 0,
            'poster_names': [],
            'dates': [],
            'reply_texts': [],
        }

        # Chan-style boards (Kusaba, Tinyboard, vichan)
        subjects = re.findall(r'class="[^"]*(?:subject|filetitle)[^"]*"[^>]*>([^<]+)<', html)
        data['thread_subjects'] = [s.strip() for s in subjects if s.strip()]

        # Post count
        posts = re.findall(r'class="[^"]*(?:post|reply|postblock)[^"]*"', html)
        data['post_count'] = len(posts)

        # Images
        images = re.findall(r'(?:\.jpg|\.jpeg|\.png|\.gif)', html, re.IGNORECASE)
        data['image_count'] = len(images) // 2  # rough: each image appears in thumb + full

        # Poster names
        names = re.findall(r'class="[^"]*(?:postername|name)[^"]*"[^>]*>([^<]+)<', html)
        data['poster_names'] = list(set(n.strip() for n in names if n.strip() and n.strip() != 'Anonymous'))

        # Dates
        dates = re.findall(r'\d{2}/\d{2}/\d{2,4}\s*\(\w+\)\s*\d{2}:\d{2}', html)
        data['dates'] = dates[:5]  # first 5

        # Reply text snippets
        replies = re.findall(r'<blockquote>(.*?)</blockquote>', html, re.DOTALL)
        for reply in replies[:20]:
            clean = re.sub(r'<[^>]+>', ' ', reply).strip()
            clean = re.sub(r'\s+', ' ', clean)
            if len(clean) > 10:
                data['reply_texts'].append(clean[:200])

        return data

    @staticmethod
    def _resolve_url(url, base_url):
        """Resolve relative URLs to absolute."""
        if not url or url.startswith('data:') or url.startswith('javascript:'):
            return None
        if url.startswith('//'):
            return 'http:' + url
        if url.startswith('http'):
            return url
        if base_url and url.startswith('/'):
            parsed = urlparse(base_url)
            return f"{parsed.scheme}://{parsed.netloc}{url}"
        if base_url:
            return urljoin(base_url, url)
        return None


# ═══════════════════════════════════════════════════════════════════
# USERNAME TRIANGULATOR
# ═══════════════════════════════════════════════════════════════════

class UsernameTriangulator:
    """
    Cross-platform username search across Common Crawl archives.
    Given a username, find it across every platform that CC has crawled.
    """

    PLATFORMS = [
        # Social
        ("twitter.com/{username}", "Twitter"),
        ("instagram.com/{username}", "Instagram"),
        ("facebook.com/{username}", "Facebook"),
        ("tiktok.com/@{username}", "TikTok"),
        ("reddit.com/user/{username}", "Reddit"),
        ("reddit.com/u/{username}", "Reddit"),
        ("tumblr.com/{username}", "Tumblr (subdomain check)"),
        ("{username}.tumblr.com", "Tumblr"),
        ("myspace.com/{username}", "MySpace"),
        # Content
        ("youtube.com/user/{username}", "YouTube"),
        ("youtube.com/@{username}", "YouTube"),
        ("soundcloud.com/{username}", "SoundCloud"),
        ("flickr.com/photos/{username}", "Flickr"),
        ("deviantart.com/{username}", "DeviantArt"),
        ("{username}.deviantart.com", "DeviantArt"),
        # Gaming
        ("twitch.tv/{username}", "Twitch"),
        ("steamcommunity.com/id/{username}", "Steam"),
        # Tech
        ("github.com/{username}", "GitHub"),
        ("keybase.io/{username}", "Keybase"),
        # File hosting profiles
        ("mediafire.com/{username}", "MediaFire"),
        ("mega.nz/{username}", "MEGA"),
    ]

    def __init__(self):
        self.cc_manager = CCIndexManager(rate_limit=0.2)

    def triangulate(self, username, max_indexes=20, callback=None):
        """
        Search for a username across all known platform URL patterns.
        Returns dict of platform -> list of CC records found.
        """
        results = {}

        for url_template, platform in self.PLATFORMS:
            url_pattern = url_template.format(username=username)

            if callback:
                callback('searching', platform, url_pattern)

            records = self.cc_manager.search_all(
                url_pattern,
                limit_per_index=5,
                max_indexes=max_indexes,
            )

            if records:
                results[platform] = records
                if callback:
                    callback('found', platform, len(records))

        return results


# ═══════════════════════════════════════════════════════════════════
# CDN ARCHAEOLOGIST
# ═══════════════════════════════════════════════════════════════════

class CDNArchaeologist:
    """
    Find media files hosted on CDN domains that are separate from the main site.
    When a site blocks archiving, its CDN (CloudFront, Akamai, etc.) might not.
    """

    CDN_PATTERNS = [
        # CloudFront
        "{hash}.cloudfront.net",
        "d{hash}.cloudfront.net",
        # Akamai
        "{domain}.akamaized.net",
        # CloudFlare
        "{domain}.r2.cloudflarestorage.com",
        # Custom CDNs
        "cdn.{domain}",
        "cdn2.{domain}",
        "cdn3.{domain}",
        "media.{domain}",
        "img.{domain}",
        "images.{domain}",
        "static.{domain}",
        "assets.{domain}",
        "files.{domain}",
        "uploads.{domain}",
        "content.{domain}",
        "storage.{domain}",
        "i.{domain}",
        "f.{domain}",
        "dl.{domain}",
    ]

    def __init__(self):
        self.cc_manager = CCIndexManager(rate_limit=0.3)
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': 'GhostCrawl-CC/1.0'})

    def find_cdn_domains(self, domain, max_indexes=15):
        """
        Search for CDN variants of a domain in Common Crawl.
        Returns dict of cdn_domain -> list of records.
        """
        results = {}

        cdn_variants = []
        for pattern in self.CDN_PATTERNS:
            if '{domain}' in pattern:
                cdn_variants.append(pattern.format(domain=domain))
            elif '{hash}' in pattern:
                continue  # skip hash-based patterns

        for cdn_domain in cdn_variants:
            records = self.cc_manager.search_all(
                f"{cdn_domain}/*",
                limit_per_index=10,
                max_indexes=max_indexes,
            )
            if records:
                results[cdn_domain] = records

        return results

    def find_cdn_from_html(self, html_content, main_domain):
        """
        Extract CDN domains referenced in HTML that differ from the main domain.
        """
        all_urls = re.findall(r'(?:src|href|data-src|poster|background)="(https?://[^"]+)"',
                              html_content, re.IGNORECASE)
        cdn_domains = set()

        for url in all_urls:
            try:
                parsed = urlparse(url)
                if parsed.netloc and parsed.netloc != main_domain and main_domain not in parsed.netloc:
                    cdn_domains.add(parsed.netloc)
            except Exception:
                pass

        return cdn_domains


# ═══════════════════════════════════════════════════════════════════
# DEAD LINK RESURRECTOR
# ═══════════════════════════════════════════════════════════════════

class DeadLinkResurrector:
    """
    Takes a list of dead URLs and tries to recover them from:
    1. Common Crawl WARC files (direct content)
    2. Wayback Machine CDX (if not excluded)
    3. CDN archaeology (alternate hosting)
    """

    def __init__(self):
        self.cc_manager = CCIndexManager(rate_limit=0.3)
        self.warc_fetcher = WARCFetcher(rate_limit=0.5)
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': 'GhostCrawl-CC/1.0'})

    def resurrect(self, urls, dest_dir=None, callback=None):
        """
        Attempt to recover dead URLs from all available archives.
        Returns dict of url -> recovery_result.
        """
        results = {}

        for url in urls:
            result = {
                'url': url,
                'status': 'not_found',
                'source': None,
                'content_type': None,
                'size': 0,
                'saved_path': None,
            }

            # 1. Try Common Crawl
            cc_records = self.cc_manager.search_all(url, limit_per_index=3, max_indexes=30)
            if cc_records:
                # Try to fetch the actual content
                for record in cc_records[:3]:
                    fetched = self.warc_fetcher.fetch_record(record)
                    if fetched:
                        _, content, _ = fetched
                        result['status'] = 'recovered'
                        result['source'] = f"Common Crawl ({record.get('_cc_index', '?')})"
                        result['content_type'] = record.get('mime', 'unknown')
                        result['size'] = len(content)

                        if dest_dir:
                            filepath = self._save_content(url, content, dest_dir, record.get('mime', ''))
                            result['saved_path'] = filepath

                        break

            # 2. Try Wayback CDX if CC failed
            if result['status'] == 'not_found':
                try:
                    r = self.session.get("https://web.archive.org/cdx/search/cdx", params={
                        "url": url,
                        "output": "json",
                        "fl": "timestamp,original,statuscode",
                        "limit": 1,
                        "filter": "statuscode:200",
                    }, timeout=10)

                    if r.status_code == 200 and r.text.strip():
                        data = r.json()
                        if len(data) > 1:
                            ts = data[1][0]
                            wb_url = f"https://web.archive.org/web/{ts}id_/{url}"
                            dl = self.session.get(wb_url, timeout=30)
                            if dl.status_code == 200 and len(dl.content) > 100:
                                result['status'] = 'recovered'
                                result['source'] = f"Wayback Machine ({ts})"
                                result['size'] = len(dl.content)

                                if dest_dir:
                                    filepath = self._save_content(url, dl.content, dest_dir)
                                    result['saved_path'] = filepath
                except Exception:
                    pass

            results[url] = result

            if callback:
                callback(url, result)

        return results

    def _save_content(self, url, content, dest_dir, mime_type=""):
        """Save recovered content to disk."""
        os.makedirs(dest_dir, exist_ok=True)

        # Generate filename from URL
        parsed = urlparse(url)
        filename = os.path.basename(parsed.path) or 'index.html'
        # Sanitize
        filename = re.sub(r'[<>:"/\\|?*]', '_', filename)

        filepath = os.path.join(dest_dir, filename)

        # Handle duplicates
        base, ext = os.path.splitext(filepath)
        counter = 1
        while os.path.exists(filepath):
            filepath = f"{base}_{counter}{ext}"
            counter += 1

        if isinstance(content, str):
            with open(filepath, 'w', encoding='utf-8', errors='replace') as f:
                f.write(content)
        else:
            with open(filepath, 'wb') as f:
                f.write(content)

        return filepath


# ═══════════════════════════════════════════════════════════════════
# MAIN CRAWLER
# ═══════════════════════════════════════════════════════════════════

class CommonCrawlMiner:
    """
    Main interface for Common Crawl deep mining operations.
    """

    def __init__(self):
        self.cc_manager = CCIndexManager()
        self.warc_fetcher = WARCFetcher()
        self.analyzer = ContentAnalyzer()

    def mine_domain(self, domain_pattern, keywords=None, extract_media=False,
                    download=False, dest_dir=None, year_range=None,
                    max_indexes=None, verbose=True):
        """
        Full mining operation on a domain.

        Args:
            domain_pattern: URL pattern to search (e.g., "oldsite.com/files/*")
            keywords: List of keywords to search within page content
            extract_media: Whether to extract media URLs from HTML
            download: Whether to download extracted media
            dest_dir: Download destination
            year_range: Tuple of (start_year, end_year)
            max_indexes: Max number of CC indexes to search
            verbose: Print progress
        """
        if verbose:
            print(f"\n{'='*60}")
            print(f"COMMON CRAWL DEEP MINE: {domain_pattern}")
            print(f"{'='*60}")

        # Phase 1: Search all CC indexes
        if verbose:
            print(f"\n[Phase 1] Searching Common Crawl indexes...")

        def search_callback(idx_id, count, total, msg=None):
            if verbose:
                if count == -1:
                    print(f"  {idx_id}: TIMEOUT (large dataset)")
                elif count > 0:
                    print(f"  {idx_id}: +{count} pages (total: {total})")
                elif msg:
                    print(f"  {msg}", end='\r', flush=True)

        records = self.cc_manager.search_all(
            domain_pattern,
            limit_per_index=50,
            max_indexes=max_indexes,
            year_range=year_range,
            callback=search_callback,
        )

        if verbose:
            print(f"\n  Total unique pages found: {len(records)}")

        if not records:
            if verbose:
                print("  No results. Domain may not have been crawled by Common Crawl.")
            return {'records': [], 'pages': [], 'media': {}, 'keyword_hits': []}

        # Phase 2: Fetch WARC content
        if verbose:
            print(f"\n[Phase 2] Fetching page content from WARC files...")

        def fetch_callback(current, total, url):
            if verbose:
                print(f"  [{current}/{total}] {url[:80]}")

        pages = self.warc_fetcher.fetch_batch(records, callback=fetch_callback)

        if verbose:
            print(f"\n  Successfully fetched: {len(pages)} pages")

        # Phase 3: Analyze content
        all_media = defaultdict(list)
        keyword_hits = []

        if verbose and (keywords or extract_media):
            print(f"\n[Phase 3] Analyzing content...")

        for url, html, headers in pages:
            # Keyword search
            if keywords:
                matches = self.analyzer.keyword_search(html, keywords)
                for match in matches:
                    match['url'] = url
                    keyword_hits.append(match)

            # Media extraction
            if extract_media:
                media = self.analyzer.extract_media_urls(html, url)
                for category, urls in media.items():
                    all_media[category].extend(urls)

            # Forum data
            forum_data = self.analyzer.extract_forum_data(html)
            if forum_data['thread_subjects']:
                if verbose:
                    print(f"  {url.split('/')[-1]}: {forum_data['post_count']} posts, "
                          f"{forum_data['image_count']} images")
                    for subj in forum_data['thread_subjects'][:5]:
                        print(f"    Subject: {subj}")

        # Deduplicate media
        for key in all_media:
            all_media[key] = list(dict.fromkeys(all_media[key]))

        # Results summary
        if verbose:
            if keyword_hits:
                print(f"\n[Results] Keyword matches: {len(keyword_hits)}")
                for hit in keyword_hits:
                    print(f"  [{hit['keyword']}] in {hit['url'].split('/')[-1]}")
                    print(f"    ...{hit['context']}...")

            if extract_media:
                total_media = sum(len(v) for v in all_media.values())
                print(f"\n[Results] Media URLs extracted: {total_media}")
                for cat, urls in all_media.items():
                    if urls:
                        print(f"  {cat}: {len(urls)}")

        # Phase 4: Download if requested
        if download and dest_dir and all_media:
            if verbose:
                print(f"\n[Phase 4] Downloading media to {dest_dir}...")
            self._download_media(all_media, dest_dir, verbose)

        return {
            'records': records,
            'pages': pages,
            'media': dict(all_media),
            'keyword_hits': keyword_hits,
        }

    def _download_media(self, media_dict, dest_dir, verbose=True):
        """Download extracted media files."""
        os.makedirs(dest_dir, exist_ok=True)
        session = requests.Session()
        session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                          'AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36',
        })

        total = sum(len(v) for v in media_dict.values())
        downloaded = 0
        errors = 0

        for category, urls in media_dict.items():
            cat_dir = os.path.join(dest_dir, category)
            os.makedirs(cat_dir, exist_ok=True)

            for url in urls:
                try:
                    r = session.get(url, timeout=30)
                    if r.status_code == 200 and len(r.content) > 500:
                        filename = os.path.basename(urlparse(url).path) or f"file_{downloaded}"
                        filename = re.sub(r'[<>:"/\\|?*]', '_', filename)
                        filepath = os.path.join(cat_dir, filename)

                        with open(filepath, 'wb') as f:
                            f.write(r.content)
                        downloaded += 1
                    else:
                        errors += 1
                except Exception:
                    errors += 1

                time.sleep(0.3)

        if verbose:
            print(f"  Downloaded: {downloaded}/{total} (errors: {errors})")


# ═══════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════

def main(target_override=None, mode=None, dest=None):
    if target_override:
        # Called from god_v2 — bypass argparse
        class Args:
            pass
        args = Args()
        args.domain = target_override if mode in (None, 'mine') else None
        args.keyword = None
        args.extract_media = True
        args.download = True
        args.dest = dest or DEFAULT_DEST
        args.years = None
        args.max_indexes = None
        args.triangulate = target_override if mode == 'triangulate' else None
        args.cdn = target_override if mode == 'cdn' else None
        args.resurrect = None  # resurrect needs a file, not a domain
    else:
        parser = argparse.ArgumentParser(
            description="GhostCrawl Common Crawl Deep Miner - bypass robots.txt exclusions",
            formatter_class=argparse.RawDescriptionHelpFormatter,
            epilog="""
Examples:
  # Search for a domain across all CC indexes
  python ghostcrawl_commoncrawl.py oldsite.com/files/*

  # Search with keywords
  python ghostcrawl_commoncrawl.py oldsite.com/files/* --keyword rare unreleased

  # Extract and download media
  python ghostcrawl_commoncrawl.py deadmusic.com --extract-media --download

  # Username triangulation
  python ghostcrawl_commoncrawl.py --triangulate olduser123

  # CDN archaeology
  python ghostcrawl_commoncrawl.py --cdn deadsite.com

  # Resurrect dead links
  python ghostcrawl_commoncrawl.py --resurrect urls.txt

  # Limit to specific years
  python ghostcrawl_commoncrawl.py example.com --years 2010 2015
            """,
        )

        parser.add_argument('domain', nargs='?', help='Domain/URL pattern to mine')
        parser.add_argument('--keyword', '-k', nargs='+', help='Keywords to search within page content')
        parser.add_argument('--extract-media', '-m', action='store_true', help='Extract media URLs from HTML')
        parser.add_argument('--download', '-d', action='store_true', help='Download extracted media')
        parser.add_argument('--dest', default=DEFAULT_DEST, help='Download destination')
        parser.add_argument('--years', nargs=2, type=int, metavar=('START', 'END'), help='Year range filter')
        parser.add_argument('--max-indexes', type=int, default=None, help='Max CC indexes to search')
        parser.add_argument('--triangulate', '-t', metavar='USERNAME', help='Triangulate a username across platforms')
        parser.add_argument('--cdn', metavar='DOMAIN', help='CDN archaeology for a domain')
        parser.add_argument('--resurrect', '-r', metavar='FILE', help='Resurrect dead URLs from a file')

        args = parser.parse_args()

    if args.triangulate:
        print(f"\n{'='*60}")
        print(f"USERNAME TRIANGULATION: {args.triangulate}")
        print(f"{'='*60}\n")

        tri = UsernameTriangulator()

        def tri_callback(action, platform, data):
            if action == 'searching':
                print(f"  Searching {platform}...", end=' ', flush=True)
            elif action == 'found':
                print(f"FOUND! ({data} records)")
            else:
                print()

        results = tri.triangulate(args.triangulate, callback=tri_callback)

        if results:
            print(f"\n{'='*60}")
            print(f"FOUND ON {len(results)} PLATFORMS:")
            print(f"{'='*60}")
            for platform, records in results.items():
                print(f"\n  {platform}: {len(records)} archived pages")
                for rec in records[:5]:
                    print(f"    [{rec.get('timestamp', '?')}] {rec.get('url', '?')[:100]}")
        else:
            print("\nNo results found across any platform.")

    elif args.cdn:
        print(f"\nCDN Archaeology: {args.cdn}")
        archaeologist = CDNArchaeologist()
        results = archaeologist.find_cdn_domains(args.cdn)
        if results:
            for cdn_domain, records in results.items():
                print(f"\n  {cdn_domain}: {len(records)} records")
                for rec in records[:5]:
                    print(f"    {rec.get('url', '?')[:100]}")
        else:
            print("No CDN domains found.")

    elif args.resurrect:
        with open(args.resurrect, 'r', encoding='utf-8') as f:
            urls = [line.strip() for line in f if line.strip()]

        print(f"\nResurrecting {len(urls)} dead URLs...")
        resurrector = DeadLinkResurrector()
        dest = os.path.join(args.dest, 'resurrected')

        def res_callback(url, result):
            status = result['status']
            source = result.get('source', '?')
            print(f"  [{status}] {url[:80]}")
            if status == 'recovered':
                print(f"    Source: {source} | Size: {result['size']}")

        results = resurrector.resurrect(urls, dest_dir=dest, callback=res_callback)
        recovered = sum(1 for r in results.values() if r['status'] == 'recovered')
        print(f"\nRecovered: {recovered}/{len(urls)}")

    elif args.domain:
        miner = CommonCrawlMiner()
        dest = os.path.join(args.dest, 'cc_mine',
                           args.domain.split('/')[0].replace('.', '_'))

        year_range = tuple(args.years) if args.years else None

        results = miner.mine_domain(
            args.domain,
            keywords=args.keyword,
            extract_media=args.extract_media,
            download=args.download,
            dest_dir=dest,
            year_range=year_range,
            max_indexes=args.max_indexes,
        )
    else:
        parser.print_help()


if __name__ == '__main__':
    main()
