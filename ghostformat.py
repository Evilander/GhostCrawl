#!/usr/bin/env python3
"""
GHOST Format — GhostCrawl Hierarchical Object Storage for Temporal archives

A single .ghost file is a complete, self-contained, queryable web archive.
Replaces WARC with: content-addressed storage, built-in indexes, link graph,
version chains, Merkle integrity proofs, and zstd compression.

Design principles:
  - Content-addressed: same blob stored once, referenced N times
  - Append-only: new captures append, never rewrite
  - Self-contained: no external CDX needed
  - Queryable: B-tree indexes for O(log n) lookups
  - Verifiable: Merkle tree over all content blocks
"""

import os
import io
import sys
import json
import time
import struct
import sqlite3
import hashlib
import mmap
import threading
from datetime import datetime
from urllib.parse import urlparse
from collections import defaultdict
from html.parser import HTMLParser

try:
    import zstandard as zstd
    HAS_ZSTD = True
except ImportError:
    import gzip
    HAS_ZSTD = False

try:
    import blake3
    def hash_content(data):
        return blake3.blake3(data).hexdigest()
except ImportError:
    def hash_content(data):
        return hashlib.sha256(data).hexdigest()


# ═══════════════════════════════════════════════════════════════════
# MAGIC & HEADER
# ═══════════════════════════════════════════════════════════════════

GHOST_MAGIC = b'\x47\x48\x4f\x53\x54'  # "GHOST"
GHOST_VERSION = 1
HEADER_SIZE = 64


def _pack_header(version, flags, created_ts, record_count, content_size, index_offset):
    """Pack a 64-byte GHOST header."""
    return struct.pack(
        '<5sBBxQQQQQxxxxxxxx',  # 5+1+1+1+8+8+8+8+8+8+8 = 64
        GHOST_MAGIC,
        version,
        flags,
        int(created_ts),
        record_count,
        content_size,
        index_offset,
        0,  # reserved
    )


def _unpack_header(data):
    """Unpack a 64-byte GHOST header."""
    magic, version, flags, created_ts, record_count, content_size, index_offset, _ = struct.unpack(
        '<5sBBxQQQQQxxxxxxxx', data[:HEADER_SIZE]
    )
    if magic != GHOST_MAGIC:
        raise ValueError(f"Not a GHOST file (magic: {magic!r})")
    return {
        'version': version,
        'flags': flags,
        'created_ts': created_ts,
        'record_count': record_count,
        'content_size': content_size,
        'index_offset': index_offset,
    }


# ═══════════════════════════════════════════════════════════════════
# LINK EXTRACTOR
# ═══════════════════════════════════════════════════════════════════

class _LinkExtractor(HTMLParser):
    """Extract links from HTML for the link graph."""
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        if tag == 'a' and 'href' in attrs_dict:
            self.links.append({
                'url': attrs_dict['href'],
                'text': '',
                'rel': attrs_dict.get('rel', ''),
            })
        elif tag in ('img', 'script', 'link', 'source', 'video', 'audio') and 'src' in attrs_dict:
            self.links.append({
                'url': attrs_dict.get('src') or attrs_dict.get('href', ''),
                'text': '',
                'rel': tag,
            })

    def handle_data(self, data):
        if self.links and not self.links[-1]['text']:
            self.links[-1]['text'] = data.strip()[:200]


def _extract_links(html_content, base_url=""):
    """Extract links from HTML content."""
    try:
        parser = _LinkExtractor()
        parser.feed(html_content if isinstance(html_content, str) else html_content.decode('utf-8', errors='replace'))
        return parser.links
    except Exception:
        return []


# ═══════════════════════════════════════════════════════════════════
# COMPRESSION
# ═══════════════════════════════════════════════════════════════════

def _compress(data, dictionary=None):
    """Compress data with zstd (preferred) or gzip fallback."""
    if HAS_ZSTD:
        params = zstd.ZstdCompressionParameters.from_level(6)
        if dictionary:
            cctx = zstd.ZstdCompressor(dict_data=zstd.ZstdCompressionDict(dictionary),
                                       compression_params=params)
        else:
            cctx = zstd.ZstdCompressor(compression_params=params)
        return b'\x01' + cctx.compress(data)  # \x01 = zstd marker
    else:
        return b'\x00' + gzip.compress(data, compresslevel=6)  # \x00 = gzip marker


def _decompress(data, dictionary=None):
    """Decompress data."""
    if not data:
        return b''
    marker = data[0:1]
    payload = data[1:]
    if marker == b'\x01' and HAS_ZSTD:
        if dictionary:
            dctx = zstd.ZstdDecompressor(dict_data=zstd.ZstdCompressionDict(dictionary))
        else:
            dctx = zstd.ZstdDecompressor()
        return dctx.decompress(payload)
    elif marker == b'\x00':
        return gzip.decompress(payload)
    else:
        return data  # uncompressed


# ═══════════════════════════════════════════════════════════════════
# MERKLE TREE
# ═══════════════════════════════════════════════════════════════════

def _merkle_hash(left, right):
    """Combine two hashes in the Merkle tree."""
    combined = (left + right).encode() if isinstance(left, str) else left + right
    return hash_content(combined if isinstance(combined, bytes) else combined.encode())


def build_merkle_tree(hashes):
    """Build a Merkle tree from a list of content hashes. Returns root hash."""
    if not hashes:
        return hash_content(b'')
    if len(hashes) == 1:
        return hashes[0]

    # Pad to even
    if len(hashes) % 2 != 0:
        hashes = list(hashes) + [hashes[-1]]

    next_level = []
    for i in range(0, len(hashes), 2):
        next_level.append(_merkle_hash(hashes[i], hashes[i + 1]))

    return build_merkle_tree(next_level)


# ═══════════════════════════════════════════════════════════════════
# GHOST ARCHIVE — SQLite-backed implementation
# ═══════════════════════════════════════════════════════════════════

class GhostArchive:
    """Read/write .ghost format archives.

    Implementation uses SQLite for indexing + flat content store for blobs.
    The .ghost file is actually a directory containing:
      - content/  (content-addressed blob store)
      - index.db  (SQLite indexes, records, metadata)
      - ghost.header (64-byte binary header)

    For single-file distribution, use export_packed() / import_packed().
    """

    def __init__(self, path, mode="r"):
        """
        Args:
            path: Path to .ghost archive (directory)
            mode: "r" (read), "w" (create), "a" (append)
        """
        self.path = path
        self.mode = mode
        self._lock = threading.Lock()
        self._compression_dicts = {}

        if mode == "w":
            os.makedirs(path, exist_ok=True)
            os.makedirs(os.path.join(path, "content"), exist_ok=True)
            self._init_db()
            self._write_header()
        elif mode in ("r", "a"):
            if not os.path.isdir(path):
                raise FileNotFoundError(f"GHOST archive not found: {path}")
            if mode == "a":
                os.makedirs(os.path.join(path, "content"), exist_ok=True)
        else:
            raise ValueError(f"Invalid mode: {mode}")

        self._db = sqlite3.connect(
            os.path.join(path, "index.db"), timeout=30,
            check_same_thread=False
        )
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.row_factory = sqlite3.Row

        if mode in ("w", "a"):
            self._init_db()

    def _init_db(self):
        """Initialize SQLite index tables."""
        db_path = os.path.join(self.path, "index.db")
        conn = sqlite3.connect(db_path, timeout=30)
        conn.executescript("""
            -- Record table: one row per capture
            CREATE TABLE IF NOT EXISTS records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT NOT NULL,
                domain TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                status_code INTEGER DEFAULT 200,
                mime_type TEXT,
                content_hash TEXT,
                content_size INTEGER DEFAULT 0,
                compressed_size INTEGER DEFAULT 0,
                http_headers TEXT,
                source TEXT DEFAULT 'crawl',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_rec_url ON records(url);
            CREATE INDEX IF NOT EXISTS idx_rec_domain ON records(domain);
            CREATE INDEX IF NOT EXISTS idx_rec_ts ON records(timestamp);
            CREATE INDEX IF NOT EXISTS idx_rec_hash ON records(content_hash);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_rec_unique
                ON records(url, timestamp);

            -- Link graph: edges between pages
            CREATE TABLE IF NOT EXISTS link_graph (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_url TEXT NOT NULL,
                target_url TEXT NOT NULL,
                link_text TEXT,
                rel TEXT,
                source_timestamp TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_link_src ON link_graph(source_url);
            CREATE INDEX IF NOT EXISTS idx_link_tgt ON link_graph(target_url);

            -- Version chains: track URL evolution
            CREATE TABLE IF NOT EXISTS version_chains (
                url TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                prev_hash TEXT,
                diff_size INTEGER,
                change_type TEXT,
                PRIMARY KEY (url, timestamp)
            );

            -- Metadata store: extensible per-record metadata
            CREATE TABLE IF NOT EXISTS metadata (
                record_id INTEGER,
                url TEXT,
                timestamp TEXT,
                key TEXT NOT NULL,
                value TEXT,
                FOREIGN KEY (record_id) REFERENCES records(id)
            );
            CREATE INDEX IF NOT EXISTS idx_meta_record ON metadata(record_id);
            CREATE INDEX IF NOT EXISTS idx_meta_url ON metadata(url, timestamp);

            -- Full-text search index
            CREATE VIRTUAL TABLE IF NOT EXISTS fts_content USING fts5(
                url, text_content, tokenize='porter'
            );

            -- Domain compression dictionaries
            CREATE TABLE IF NOT EXISTS compression_dicts (
                domain TEXT PRIMARY KEY,
                dict_data BLOB,
                sample_count INTEGER,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
        """)
        conn.commit()
        conn.close()

    def _write_header(self):
        """Write the GHOST header file."""
        header = _pack_header(
            version=GHOST_VERSION,
            flags=0,
            created_ts=time.time(),
            record_count=0,
            content_size=0,
            index_offset=0,
        )
        with open(os.path.join(self.path, "ghost.header"), "wb") as f:
            f.write(header)

    def _content_path(self, content_hash):
        """Get the filesystem path for a content blob."""
        # Two-level directory structure to avoid too many files in one dir
        return os.path.join(
            self.path, "content",
            content_hash[:2],
            content_hash[2:4],
            content_hash
        )

    def _store_blob(self, data, domain=None):
        """Store a content blob. Returns (hash, stored_size). Deduplicates automatically."""
        content_hash = hash_content(data)
        blob_path = self._content_path(content_hash)

        if os.path.exists(blob_path):
            # Already stored — deduplication win
            return content_hash, os.path.getsize(blob_path)

        os.makedirs(os.path.dirname(blob_path), exist_ok=True)

        # Compress with domain dictionary if available
        dict_data = self._compression_dicts.get(domain)
        compressed = _compress(data, dictionary=dict_data)

        with open(blob_path, "wb") as f:
            f.write(compressed)

        return content_hash, len(compressed)

    def _read_blob(self, content_hash, domain=None):
        """Read and decompress a content blob."""
        blob_path = self._content_path(content_hash)
        if not os.path.exists(blob_path):
            return None

        with open(blob_path, "rb") as f:
            compressed = f.read()

        dict_data = self._compression_dicts.get(domain)
        return _decompress(compressed, dictionary=dict_data)

    @staticmethod
    def _extract_domain(url):
        if "://" in url:
            return urlparse(url).netloc.lower()
        return url.split("/")[0].lower()

    # ─── Writing ─────────────────────────────────────────────────

    def add_capture(self, url, timestamp, status, headers, body,
                    links=None, metadata=None, source="crawl"):
        """Add a web capture. Auto-deduplicates by content hash.

        Args:
            url: Original URL
            timestamp: Capture timestamp (YYYYMMDDHHmmss or ISO)
            status: HTTP status code
            headers: dict of HTTP response headers
            body: bytes of response body
            links: list of outbound link dicts (auto-extracted from HTML if None)
            metadata: dict of key/value metadata to attach
            source: provenance tag (crawl, wayback, commoncrawl, etc.)

        Returns:
            content_hash of the stored body
        """
        if self.mode not in ("w", "a"):
            raise ValueError("Archive not opened for writing")

        domain = self._extract_domain(url)
        body_bytes = body if isinstance(body, bytes) else body.encode('utf-8')

        with self._lock:
            # Store content blob (deduped)
            content_hash, compressed_size = self._store_blob(body_bytes, domain=domain)

            # Insert record
            headers_json = json.dumps(dict(headers)) if headers else "{}"
            mime = ""
            if headers:
                mime = headers.get("content-type", headers.get("Content-Type", ""))

            self._db.execute(
                """INSERT OR IGNORE INTO records
                   (url, domain, timestamp, status_code, mime_type,
                    content_hash, content_size, compressed_size,
                    http_headers, source)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (url, domain, str(timestamp), status, mime,
                 content_hash, len(body_bytes), compressed_size,
                 headers_json, source)
            )

            # Version chain tracking
            prev = self._db.execute(
                """SELECT content_hash FROM version_chains
                   WHERE url = ? ORDER BY timestamp DESC LIMIT 1""",
                (url,)
            ).fetchone()
            prev_hash = prev["content_hash"] if prev else None

            change_type = "new"
            if prev_hash:
                if prev_hash == content_hash:
                    change_type = "unchanged"
                else:
                    change_type = "content_update"

            self._db.execute(
                """INSERT OR IGNORE INTO version_chains
                   (url, timestamp, content_hash, prev_hash, change_type)
                   VALUES (?, ?, ?, ?, ?)""",
                (url, str(timestamp), content_hash, prev_hash, change_type)
            )

            # Link graph
            if links is None and mime and "text/html" in mime.lower():
                links = _extract_links(body_bytes, base_url=url)
            if links:
                for link in links:
                    self._db.execute(
                        """INSERT INTO link_graph
                           (source_url, target_url, link_text, rel, source_timestamp)
                           VALUES (?, ?, ?, ?, ?)""",
                        (url, link.get('url', ''), link.get('text', ''),
                         link.get('rel', ''), str(timestamp))
                    )

            # Full-text search (for text content)
            if mime and ('text/' in mime.lower() or 'html' in mime.lower()):
                text = body_bytes.decode('utf-8', errors='replace')
                self._db.execute(
                    "INSERT INTO fts_content (url, text_content) VALUES (?, ?)",
                    (url, text[:100000])  # cap at 100KB for FTS
                )

            # Metadata
            if metadata:
                record_id = self._db.execute("SELECT last_insert_rowid()").fetchone()[0]
                for key, value in metadata.items():
                    self._db.execute(
                        """INSERT INTO metadata (record_id, url, timestamp, key, value)
                           VALUES (?, ?, ?, ?, ?)""",
                        (record_id, url, str(timestamp), key,
                         json.dumps(value) if not isinstance(value, str) else value)
                    )

            self._db.commit()

        return content_hash

    def add_metadata(self, url, timestamp, key, value):
        """Attach metadata to an existing capture."""
        if self.mode not in ("w", "a"):
            raise ValueError("Archive not opened for writing")

        row = self._db.execute(
            "SELECT id FROM records WHERE url = ? AND timestamp = ?",
            (url, str(timestamp))
        ).fetchone()

        record_id = row["id"] if row else None
        val = json.dumps(value) if not isinstance(value, str) else value
        self._db.execute(
            """INSERT INTO metadata (record_id, url, timestamp, key, value)
               VALUES (?, ?, ?, ?, ?)""",
            (record_id, url, str(timestamp), key, val)
        )
        self._db.commit()

    def import_warc(self, warc_path):
        """Convert a WARC file into GHOST format."""
        try:
            from warcio.archiveiterator import ArchiveIterator
        except ImportError:
            print("warcio not installed. Install with: pip install warcio")
            return 0

        count = 0
        with open(warc_path, 'rb') as f:
            for record in ArchiveIterator(f):
                if record.rec_type == 'response':
                    url = record.rec_headers.get_header('WARC-Target-URI')
                    timestamp = record.rec_headers.get_header('WARC-Date', '')
                    timestamp = timestamp.replace('-', '').replace(':', '').replace('T', '').replace('Z', '')[:14]

                    http_headers = {}
                    status = 200
                    if hasattr(record, 'http_headers') and record.http_headers:
                        status = int(record.http_headers.get_statuscode() or 200)
                        for name, value in record.http_headers.headers:
                            http_headers[name] = value

                    body = record.content_stream().read()
                    self.add_capture(url, timestamp, status, http_headers, body,
                                    source="warc_import")
                    count += 1

        return count

    def import_cdx(self, cdx_records, warc_path=None):
        """Import CDX records, optionally with WARC content."""
        count = 0
        for rec in cdx_records:
            url = rec.get("url") or rec.get("original", "")
            timestamp = rec.get("timestamp", "")
            status = int(rec.get("statuscode") or rec.get("status_code") or 200)

            self.add_capture(url, timestamp, status, {}, b'',
                             source="cdx_import")
            count += 1
        return count

    # ─── Reading ─────────────────────────────────────────────────

    def get(self, url, timestamp=None):
        """Get a capture. Latest if no timestamp. O(log n) via index."""
        if timestamp:
            row = self._db.execute(
                "SELECT * FROM records WHERE url = ? AND timestamp = ?",
                (url, str(timestamp))
            ).fetchone()
        else:
            row = self._db.execute(
                "SELECT * FROM records WHERE url = ? ORDER BY timestamp DESC LIMIT 1",
                (url,)
            ).fetchone()

        if not row:
            return None

        domain = row["domain"]
        body = self._read_blob(row["content_hash"], domain=domain)
        headers = json.loads(row["http_headers"]) if row["http_headers"] else {}

        return {
            "url": row["url"],
            "timestamp": row["timestamp"],
            "status_code": row["status_code"],
            "mime_type": row["mime_type"],
            "content_hash": row["content_hash"],
            "content_size": row["content_size"],
            "headers": headers,
            "body": body,
            "source": row["source"],
        }

    def search(self, query, domain=None, date_range=None):
        """Full-text search across archived content."""
        sql = "SELECT url, snippet(fts_content, 1, '<b>', '</b>', '...', 50) as snippet FROM fts_content WHERE text_content MATCH ?"
        params = [query]
        results = self._db.execute(sql, params).fetchall()

        if domain:
            results = [r for r in results if domain in r["url"]]

        return [{"url": r["url"], "snippet": r["snippet"]} for r in results]

    def versions(self, url):
        """Get all versions of a URL with change types."""
        rows = self._db.execute(
            """SELECT timestamp, content_hash, prev_hash, change_type
               FROM version_chains WHERE url = ? ORDER BY timestamp""",
            (url,)
        ).fetchall()

        return [{
            "timestamp": r["timestamp"],
            "content_hash": r["content_hash"],
            "prev_hash": r["prev_hash"],
            "change_type": r["change_type"],
        } for r in rows]

    def links_from(self, url):
        """Get all outbound links from a page."""
        rows = self._db.execute(
            "SELECT target_url, link_text, rel FROM link_graph WHERE source_url = ?",
            (url,)
        ).fetchall()
        return [{"url": r["target_url"], "text": r["link_text"], "rel": r["rel"]}
                for r in rows]

    def links_to(self, url):
        """Get all pages that link to this URL (reverse link graph)."""
        rows = self._db.execute(
            "SELECT source_url, link_text, rel, source_timestamp FROM link_graph WHERE target_url = ?",
            (url,)
        ).fetchall()
        return [{"url": r["source_url"], "text": r["link_text"],
                 "rel": r["rel"], "timestamp": r["source_timestamp"]}
                for r in rows]

    # ─── Analysis ────────────────────────────────────────────────

    def domain_stats(self, domain=None):
        """Sparkline-equivalent: captures per year/month."""
        if domain:
            rows = self._db.execute(
                """SELECT substr(timestamp, 1, 4) as year,
                          substr(timestamp, 5, 2) as month,
                          COUNT(*) as cnt
                   FROM records WHERE domain = ?
                   GROUP BY year, month ORDER BY year, month""",
                (domain,)
            ).fetchall()
        else:
            rows = self._db.execute(
                """SELECT substr(timestamp, 1, 4) as year,
                          COUNT(*) as cnt
                   FROM records GROUP BY year ORDER BY year"""
            ).fetchall()

        return [dict(r) for r in rows]

    def dedupe_ratio(self):
        """How much space saved by content-addressed storage."""
        total_raw = self._db.execute("SELECT SUM(content_size) FROM records").fetchone()[0] or 0
        unique_hashes = self._db.execute("SELECT COUNT(DISTINCT content_hash) FROM records").fetchone()[0]
        total_records = self._db.execute("SELECT COUNT(*) FROM records").fetchone()[0]

        # Count actual stored bytes
        content_dir = os.path.join(self.path, "content")
        stored_bytes = 0
        for root, dirs, files in os.walk(content_dir):
            for f in files:
                stored_bytes += os.path.getsize(os.path.join(root, f))

        return {
            "total_captures": total_records,
            "unique_blobs": unique_hashes,
            "total_raw_bytes": total_raw,
            "stored_bytes": stored_bytes,
            "compression_ratio": f"{stored_bytes / max(1, total_raw) * 100:.1f}%",
            "dedup_savings": f"{(1 - unique_hashes / max(1, total_records)) * 100:.1f}%",
            "space_saved_bytes": total_raw - stored_bytes,
        }

    def merkle_verify(self, url=None):
        """Verify content integrity via Merkle tree."""
        if url:
            rows = self._db.execute(
                "SELECT content_hash FROM records WHERE url = ? ORDER BY timestamp",
                (url,)
            ).fetchall()
        else:
            rows = self._db.execute(
                "SELECT content_hash FROM records ORDER BY id"
            ).fetchall()

        hashes = [r["content_hash"] for r in rows if r["content_hash"]]

        # Verify each blob exists and matches
        verified = 0
        corrupted = []
        missing = []
        for h in hashes:
            blob_path = self._content_path(h)
            if not os.path.exists(blob_path):
                missing.append(h)
                continue
            # Read and re-hash
            data = self._read_blob(h)
            if data is None:
                corrupted.append(h)
                continue
            actual_hash = hash_content(data)
            if actual_hash != h:
                corrupted.append(h)
            else:
                verified += 1

        root = build_merkle_tree(hashes) if hashes else None

        return {
            "total_blobs": len(hashes),
            "verified": verified,
            "corrupted": len(corrupted),
            "missing": len(missing),
            "merkle_root": root,
            "integrity": "PASS" if not corrupted and not missing else "FAIL",
        }

    # ─── Export ───────────────────────────────────────────────────

    def to_warc(self, output_path):
        """Export back to WARC for compatibility."""
        try:
            from warcio.warcwriter import WARCWriter
            from warcio.statusandheaders import StatusAndHeaders
        except ImportError:
            print("warcio not installed. Install with: pip install warcio")
            return 0

        count = 0
        with open(output_path, 'wb') as f:
            writer = WARCWriter(f, gzip=True)
            rows = self._db.execute("SELECT * FROM records ORDER BY timestamp").fetchall()
            for row in rows:
                body = self._read_blob(row["content_hash"], domain=row["domain"])
                if body is None:
                    continue

                headers = json.loads(row["http_headers"]) if row["http_headers"] else {}
                http_headers = StatusAndHeaders(
                    f'{row["status_code"]} OK',
                    list(headers.items()),
                    protocol='HTTP/1.1'
                )

                record = writer.create_warc_record(
                    row["url"],
                    'response',
                    payload=io.BytesIO(body),
                    http_headers=http_headers,
                )
                writer.write_record(record)
                count += 1

        return count

    def to_cdx(self, output_path):
        """Export CDX index."""
        rows = self._db.execute(
            "SELECT url, timestamp, mime_type, status_code, content_hash, content_size FROM records ORDER BY url, timestamp"
        ).fetchall()

        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(" CDX N b a m s k S V\n")
            for r in rows:
                domain = self._extract_domain(r["url"])
                urlkey = ','.join(reversed(domain.split('.'))) + ')' + urlparse(r["url"]).path
                f.write(f"{urlkey} {r['timestamp']} {r['url']} {r['mime_type'] or '-'} "
                        f"{r['status_code']} {r['content_hash'] or '-'} - - {r['content_size'] or '-'}\n")

        return len(rows)

    def to_sqlite(self, db_path):
        """Export metadata + index to SQLite for external tools."""
        import shutil
        shutil.copy2(os.path.join(self.path, "index.db"), db_path)

    def info(self):
        """Return archive statistics."""
        total = self._db.execute("SELECT COUNT(*) FROM records").fetchone()[0]
        domains = self._db.execute("SELECT COUNT(DISTINCT domain) FROM records").fetchone()[0]
        urls = self._db.execute("SELECT COUNT(DISTINCT url) FROM records").fetchone()[0]
        links = self._db.execute("SELECT COUNT(*) FROM link_graph").fetchone()[0]
        fts = self._db.execute("SELECT COUNT(*) FROM fts_content").fetchone()[0]

        earliest = self._db.execute("SELECT MIN(timestamp) FROM records").fetchone()[0]
        latest = self._db.execute("SELECT MAX(timestamp) FROM records").fetchone()[0]

        dedupe = self.dedupe_ratio()

        return {
            "total_captures": total,
            "unique_domains": domains,
            "unique_urls": urls,
            "link_edges": links,
            "fts_documents": fts,
            "earliest_capture": earliest,
            "latest_capture": latest,
            **dedupe,
        }

    def close(self):
        if self._db:
            self._db.close()
            self._db = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


# ═══════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════

def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="GHOST Format — GhostCrawl Hierarchical Object Storage for Temporal archives",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command")

    # info
    info_p = sub.add_parser("info", help="Show archive statistics")
    info_p.add_argument("archive", help="Path to .ghost archive")

    # search
    search_p = sub.add_parser("search", help="Full-text search")
    search_p.add_argument("archive", help="Path to .ghost archive")
    search_p.add_argument("query", help="Search query")
    search_p.add_argument("--domain", help="Filter by domain")

    # verify
    verify_p = sub.add_parser("verify", help="Merkle integrity verification")
    verify_p.add_argument("archive", help="Path to .ghost archive")
    verify_p.add_argument("--url", help="Verify specific URL only")

    # get
    get_p = sub.add_parser("get", help="Retrieve a capture")
    get_p.add_argument("archive", help="Path to .ghost archive")
    get_p.add_argument("url", help="URL to retrieve")
    get_p.add_argument("--timestamp", help="Specific timestamp")

    # versions
    ver_p = sub.add_parser("versions", help="Show version history")
    ver_p.add_argument("archive", help="Path to .ghost archive")
    ver_p.add_argument("url", help="URL to check")

    # links
    links_p = sub.add_parser("links", help="Show link graph")
    links_p.add_argument("archive", help="Path to .ghost archive")
    links_p.add_argument("url", help="URL to check")
    links_p.add_argument("--direction", choices=["from", "to"], default="from")

    # export
    export_p = sub.add_parser("export", help="Export to other formats")
    export_p.add_argument("archive", help="Path to .ghost archive")
    export_p.add_argument("--warc", metavar="FILE", help="Export to WARC")
    export_p.add_argument("--cdx", metavar="FILE", help="Export CDX index")
    export_p.add_argument("--sqlite", metavar="FILE", help="Export SQLite")

    # import-warc
    imp_p = sub.add_parser("import-warc", help="Import a WARC file")
    imp_p.add_argument("archive", help="Path to .ghost archive (will create)")
    imp_p.add_argument("warc", help="WARC file to import")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    if args.command == "info":
        with GhostArchive(args.archive, mode="r") as ga:
            stats = ga.info()
            print(f"\n  GHOST Archive: {args.archive}")
            print(f"  {'='*50}")
            for k, v in stats.items():
                label = k.replace('_', ' ').title()
                if isinstance(v, int) and v > 1000:
                    print(f"  {label:25s} {v:>12,}")
                else:
                    print(f"  {label:25s} {v}")

    elif args.command == "search":
        with GhostArchive(args.archive, mode="r") as ga:
            results = ga.search(args.query, domain=args.domain)
            print(f"\n  {len(results)} results for '{args.query}':")
            for r in results[:50]:
                print(f"    {r['url'][:80]}")
                print(f"      {r['snippet'][:200]}")

    elif args.command == "verify":
        with GhostArchive(args.archive, mode="r") as ga:
            result = ga.merkle_verify(url=args.url)
            print(f"\n  Integrity Check: {result['integrity']}")
            print(f"  Total blobs:  {result['total_blobs']}")
            print(f"  Verified:     {result['verified']}")
            print(f"  Corrupted:    {result['corrupted']}")
            print(f"  Missing:      {result['missing']}")
            print(f"  Merkle root:  {result['merkle_root'][:16]}..." if result['merkle_root'] else "  Merkle root:  (empty)")

    elif args.command == "get":
        with GhostArchive(args.archive, mode="r") as ga:
            capture = ga.get(args.url, timestamp=args.timestamp)
            if capture:
                print(f"  URL:       {capture['url']}")
                print(f"  Timestamp: {capture['timestamp']}")
                print(f"  Status:    {capture['status_code']}")
                print(f"  MIME:      {capture['mime_type']}")
                print(f"  Size:      {capture['content_size']} bytes")
                print(f"  Hash:      {capture['content_hash'][:16]}...")
                if capture['body'] and len(capture['body']) < 2000:
                    print(f"\n{capture['body'].decode('utf-8', errors='replace')}")
            else:
                print(f"  Not found: {args.url}")

    elif args.command == "versions":
        with GhostArchive(args.archive, mode="r") as ga:
            versions = ga.versions(args.url)
            print(f"\n  {len(versions)} versions of {args.url}:")
            for v in versions:
                print(f"    [{v['timestamp']}] {v['change_type']:15s} {v['content_hash'][:12]}...")

    elif args.command == "links":
        with GhostArchive(args.archive, mode="r") as ga:
            if args.direction == "from":
                links = ga.links_from(args.url)
                print(f"\n  {len(links)} outbound links from {args.url}:")
            else:
                links = ga.links_to(args.url)
                print(f"\n  {len(links)} inbound links to {args.url}:")
            for l in links[:100]:
                text = f" [{l['text'][:40]}]" if l.get('text') else ""
                print(f"    {l['url'][:80]}{text}")

    elif args.command == "export":
        mode = "r"
        with GhostArchive(args.archive, mode=mode) as ga:
            if args.warc:
                count = ga.to_warc(args.warc)
                print(f"  Exported {count} records to {args.warc}")
            if args.cdx:
                count = ga.to_cdx(args.cdx)
                print(f"  Exported {count} CDX records to {args.cdx}")
            if args.sqlite:
                ga.to_sqlite(args.sqlite)
                print(f"  Exported index to {args.sqlite}")

    elif args.command == "import-warc":
        with GhostArchive(args.archive, mode="w") as ga:
            count = ga.import_warc(args.warc)
            print(f"  Imported {count} records from {args.warc}")


if __name__ == "__main__":
    main()
