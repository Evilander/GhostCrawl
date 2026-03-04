#!/usr/bin/env python3
"""
GhostIndex — Proprietary CDX/Sparkline Index

Local SQLite-backed cache that eliminates dependency on external CDX APIs.
Cache-first architecture: check local DB before hitting Wayback/CC/Arquivo.

Growth strategy:
  Phase 1: Cache-on-read (every remote CDX call gets cached)
  Phase 2: Pre-populate from Common Crawl S3 shards
  Phase 3: Own crawler records discoveries as it crawls
"""

import os
import json
import time
import sqlite3
import threading
from datetime import datetime
from urllib.parse import urlparse
from collections import defaultdict


class GhostIndex:
    """Proprietary CDX index — cache-first, Wayback-fallback."""

    _instance = None
    _lock = threading.Lock()

    def __init__(self, db_path=None):
        if db_path is None:
            db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ghostindex.db")
        self.db_path = db_path
        self._local = threading.local()
        self._init_db()
        # Stats tracking
        self.hits = 0
        self.misses = 0

    @classmethod
    def get_instance(cls, db_path=None):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls(db_path=db_path)
        return cls._instance

    def _get_conn(self):
        if not hasattr(self._local, 'conn') or self._local.conn is None:
            self._local.conn = sqlite3.connect(self.db_path, timeout=30)
            self._local.conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn.execute("PRAGMA cache_size=-64000")  # 64MB cache
            self._local.conn.row_factory = sqlite3.Row
        return self._local.conn

    def _init_db(self):
        conn = self._get_conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS cdx_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                domain TEXT NOT NULL,
                url TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                status_code INTEGER DEFAULT 200,
                mime_type TEXT,
                content_length INTEGER,
                digest TEXT,
                source TEXT DEFAULT 'wayback',
                warc_file TEXT,
                warc_offset INTEGER,
                warc_length INTEGER,
                cached_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_cdx_domain ON cdx_records(domain);
            CREATE INDEX IF NOT EXISTS idx_cdx_url ON cdx_records(url, timestamp);
            CREATE INDEX IF NOT EXISTS idx_cdx_digest ON cdx_records(digest);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_cdx_unique
                ON cdx_records(url, timestamp, source);

            CREATE TABLE IF NOT EXISTS sparkline_cache (
                domain TEXT PRIMARY KEY,
                sparkline_json TEXT,
                capture_count INTEGER,
                earliest TEXT,
                latest TEXT,
                last_updated DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS domain_meta (
                domain TEXT PRIMARY KEY,
                total_captures INTEGER DEFAULT 0,
                total_unique_urls INTEGER DEFAULT 0,
                last_cdx_query DATETIME,
                last_sparkline_query DATETIME,
                notes TEXT
            );
        """)
        conn.commit()

    # ─── Query Methods ───────────────────────────────────────────

    def cdx_search(self, url, match_type="exact", limit=200,
                   status_filter=200, collapse="digest"):
        """Local CDX search. Returns list of dicts or None if no cached data."""
        conn = self._get_conn()

        if match_type == "exact":
            where = "url = ?"
            params = [url]
        elif match_type == "domain":
            domain = self._extract_domain(url)
            where = "domain = ?"
            params = [domain]
        elif match_type == "prefix":
            where = "url LIKE ?"
            params = [url + "%"]
        else:
            where = "url = ?"
            params = [url]

        if status_filter:
            where += " AND status_code = ?"
            params.append(status_filter)

        query = f"SELECT * FROM cdx_records WHERE {where} ORDER BY timestamp DESC"
        if limit:
            query += f" LIMIT {int(limit)}"

        rows = conn.execute(query, params).fetchall()
        if not rows:
            self.misses += 1
            return None

        self.hits += 1

        if collapse == "digest":
            seen = set()
            deduped = []
            for row in rows:
                d = row["digest"]
                if d and d in seen:
                    continue
                if d:
                    seen.add(d)
                deduped.append(dict(row))
            return deduped

        return [dict(row) for row in rows]

    def sparkline(self, domain):
        """Local sparkline lookup. Returns {year: count} dict or None."""
        domain = self._extract_domain(domain)
        conn = self._get_conn()

        row = conn.execute(
            "SELECT sparkline_json FROM sparkline_cache WHERE domain = ?",
            (domain,)
        ).fetchone()

        if row and row["sparkline_json"]:
            self.hits += 1
            return json.loads(row["sparkline_json"])

        # Try computing from CDX records
        computed = self._compute_sparkline(domain)
        if computed:
            self.hits += 1
            return computed

        self.misses += 1
        return None

    def discover_years(self, domain):
        """Instant year discovery from cached sparkline/CDX data."""
        spark = self.sparkline(domain)
        if spark:
            return spark
        return None

    def best_timestamps(self, url, limit=10):
        """Local lookup for best timestamps of a specific URL."""
        conn = self._get_conn()
        rows = conn.execute(
            """SELECT timestamp, content_length FROM cdx_records
               WHERE url = ? AND status_code = 200
               ORDER BY content_length DESC
               LIMIT ?""",
            (url, limit)
        ).fetchall()

        if not rows:
            return None

        self.hits += 1
        return [(row["timestamp"], row["content_length"] or 0) for row in rows]

    def has_domain(self, domain):
        """Check if we have any data for a domain."""
        domain = self._extract_domain(domain)
        conn = self._get_conn()
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM cdx_records WHERE domain = ?",
            (domain,)
        ).fetchone()
        return row["cnt"] > 0

    # ─── Ingest Methods ──────────────────────────────────────────

    def ingest_cdx_response(self, url, records, source="wayback"):
        """Cache a CDX API response. records = list of dicts with CDX fields."""
        if not records:
            return 0
        conn = self._get_conn()
        domain = self._extract_domain(url)
        count = 0
        for rec in records:
            try:
                conn.execute(
                    """INSERT OR IGNORE INTO cdx_records
                       (domain, url, timestamp, status_code, mime_type,
                        content_length, digest, source)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        domain,
                        rec.get("url") or rec.get("original") or url,
                        rec.get("timestamp", ""),
                        int(rec.get("statuscode") or rec.get("status_code") or 200),
                        rec.get("mimetype") or rec.get("mime_type", ""),
                        int(rec.get("length") or rec.get("content_length") or 0),
                        rec.get("digest", ""),
                        source,
                    )
                )
                count += 1
            except (sqlite3.IntegrityError, ValueError):
                continue
        conn.commit()
        self._update_domain_meta(domain)
        return count

    def ingest_cdx_json(self, url, json_data, source="wayback"):
        """Cache raw CDX JSON response (Wayback format: [headers, row1, row2, ...])."""
        if not json_data or len(json_data) < 2:
            return 0
        headers = json_data[0]
        records = []
        for row in json_data[1:]:
            records.append(dict(zip(headers, row)))
        return self.ingest_cdx_response(url, records, source)

    def ingest_commoncrawl_shard(self, domain, shard_records):
        """Bulk import from Common Crawl S3 shard parsing."""
        if not shard_records:
            return 0
        conn = self._get_conn()
        domain = self._extract_domain(domain)
        count = 0
        for rec in shard_records:
            try:
                conn.execute(
                    """INSERT OR IGNORE INTO cdx_records
                       (domain, url, timestamp, status_code, mime_type,
                        content_length, digest, source,
                        warc_file, warc_offset, warc_length)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 'commoncrawl', ?, ?, ?)""",
                    (
                        domain,
                        rec.get("url", ""),
                        rec.get("timestamp", ""),
                        int(rec.get("status") or rec.get("statuscode") or 200),
                        rec.get("mime") or rec.get("mime-detected") or "",
                        int(rec.get("length") or 0),
                        rec.get("digest", ""),
                        rec.get("filename", ""),
                        int(rec.get("offset") or 0),
                        int(rec.get("length") or 0),
                    )
                )
                count += 1
            except (sqlite3.IntegrityError, ValueError):
                continue
        conn.commit()
        self._update_domain_meta(domain)
        return count

    def ingest_own_crawl(self, url, timestamp, status, mime, length, digest):
        """Record our own crawl discoveries."""
        conn = self._get_conn()
        domain = self._extract_domain(url)
        try:
            conn.execute(
                """INSERT OR IGNORE INTO cdx_records
                   (domain, url, timestamp, status_code, mime_type,
                    content_length, digest, source)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'own_crawl')""",
                (domain, url, timestamp, status, mime, length, digest)
            )
            conn.commit()
        except sqlite3.IntegrityError:
            pass

    def import_cdx_file(self, filepath):
        """Bulk import from a CDX dump file (IIPC/Wayback format)."""
        conn = self._get_conn()
        count = 0
        with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith(' CDX') or line.startswith('#'):
                    continue
                parts = line.split()
                if len(parts) < 7:
                    continue
                try:
                    # Standard CDX format: urlkey timestamp original mimetype statuscode digest length
                    url = parts[2] if len(parts) > 2 else parts[0]
                    domain = self._extract_domain(url)
                    conn.execute(
                        """INSERT OR IGNORE INTO cdx_records
                           (domain, url, timestamp, status_code, mime_type,
                            content_length, digest, source)
                           VALUES (?, ?, ?, ?, ?, ?, ?, 'cdx_import')""",
                        (
                            domain,
                            url,
                            parts[1],
                            int(parts[4]) if parts[4] != '-' else 200,
                            parts[3] if parts[3] != '-' else '',
                            int(parts[6]) if parts[6] != '-' else 0,
                            parts[5] if parts[5] != '-' else '',
                        )
                    )
                    count += 1
                    if count % 10000 == 0:
                        conn.commit()
                except (ValueError, IndexError, sqlite3.IntegrityError):
                    continue
        conn.commit()
        return count

    def cache_sparkline(self, domain, sparkline_data):
        """Cache a sparkline API response."""
        domain = self._extract_domain(domain)
        conn = self._get_conn()
        if not sparkline_data:
            return
        capture_count = sum(sparkline_data.values()) if sparkline_data else 0
        years = sorted(sparkline_data.keys()) if sparkline_data else []
        conn.execute(
            """INSERT OR REPLACE INTO sparkline_cache
               (domain, sparkline_json, capture_count, earliest, latest, last_updated)
               VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
            (
                domain,
                json.dumps(sparkline_data),
                capture_count,
                years[0] if years else None,
                years[-1] if years else None,
            )
        )
        conn.commit()

    # ─── Sparkline Computation ───────────────────────────────────

    def _compute_sparkline(self, domain):
        """Compute sparkline from cached CDX records."""
        conn = self._get_conn()
        rows = conn.execute(
            """SELECT substr(timestamp, 1, 4) as year, COUNT(*) as cnt
               FROM cdx_records WHERE domain = ?
               GROUP BY year ORDER BY year""",
            (domain,)
        ).fetchall()

        if not rows:
            return None

        sparkline = {}
        for row in rows:
            y = row["year"]
            if y and len(y) == 4:
                sparkline[y] = row["cnt"]

        if sparkline:
            self.cache_sparkline(domain, sparkline)
        return sparkline

    def _update_domain_meta(self, domain):
        """Update domain metadata after ingestion."""
        conn = self._get_conn()
        stats = conn.execute(
            """SELECT COUNT(*) as total, COUNT(DISTINCT url) as unique_urls
               FROM cdx_records WHERE domain = ?""",
            (domain,)
        ).fetchone()
        conn.execute(
            """INSERT INTO domain_meta (domain, total_captures, total_unique_urls, last_cdx_query)
               VALUES (?, ?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(domain) DO UPDATE SET
                   total_captures = excluded.total_captures,
                   total_unique_urls = excluded.total_unique_urls,
                   last_cdx_query = CURRENT_TIMESTAMP""",
            (domain, stats["total"], stats["unique_urls"])
        )
        conn.commit()

    # ─── Stats & Utilities ───────────────────────────────────────

    def stats(self):
        """Return index statistics."""
        conn = self._get_conn()
        total_records = conn.execute("SELECT COUNT(*) as cnt FROM cdx_records").fetchone()["cnt"]
        total_domains = conn.execute("SELECT COUNT(DISTINCT domain) FROM cdx_records").fetchone()[0]
        total_urls = conn.execute("SELECT COUNT(DISTINCT url) FROM cdx_records").fetchone()[0]
        sparkline_count = conn.execute("SELECT COUNT(*) FROM sparkline_cache").fetchone()[0]

        by_source = {}
        for row in conn.execute("SELECT source, COUNT(*) as cnt FROM cdx_records GROUP BY source"):
            by_source[row["source"]] = row["cnt"]

        db_size = os.path.getsize(self.db_path) if os.path.exists(self.db_path) else 0

        return {
            "total_records": total_records,
            "total_domains": total_domains,
            "total_urls": total_urls,
            "sparkline_cached": sparkline_count,
            "by_source": by_source,
            "db_size_mb": round(db_size / 1024 / 1024, 2),
            "cache_hits": self.hits,
            "cache_misses": self.misses,
            "hit_rate": f"{self.hits / max(1, self.hits + self.misses) * 100:.1f}%",
        }

    def top_domains(self, limit=20):
        """Show domains with most cached records."""
        conn = self._get_conn()
        rows = conn.execute(
            """SELECT domain, COUNT(*) as cnt, COUNT(DISTINCT url) as urls
               FROM cdx_records GROUP BY domain ORDER BY cnt DESC LIMIT ?""",
            (limit,)
        ).fetchall()
        return [(row["domain"], row["cnt"], row["urls"]) for row in rows]

    @staticmethod
    def _extract_domain(url):
        """Extract domain from URL or return as-is if already a domain."""
        if "://" in url:
            return urlparse(url).netloc.lower()
        # Strip path if present
        return url.split("/")[0].lower()

    def close(self):
        if hasattr(self._local, 'conn') and self._local.conn:
            self._local.conn.close()
            self._local.conn = None


# ═══════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════

def main():
    import argparse
    parser = argparse.ArgumentParser(description="GhostIndex — Proprietary CDX Cache")
    parser.add_argument("--stats", action="store_true", help="Show index statistics")
    parser.add_argument("--top-domains", type=int, default=0, metavar="N", help="Show top N domains")
    parser.add_argument("--import-cdx", metavar="FILE", help="Import a CDX dump file")
    parser.add_argument("--search", metavar="URL", help="Search cached CDX records for a URL")
    parser.add_argument("--sparkline", metavar="DOMAIN", help="Show sparkline for a domain")
    parser.add_argument("--db", default=None, help="Path to ghostindex.db")
    args = parser.parse_args()

    gi = GhostIndex(db_path=args.db)

    if args.stats:
        s = gi.stats()
        print(f"\n  GhostIndex Statistics")
        print(f"  {'='*40}")
        print(f"  Records:    {s['total_records']:,}")
        print(f"  Domains:    {s['total_domains']:,}")
        print(f"  URLs:       {s['total_urls']:,}")
        print(f"  Sparklines: {s['sparkline_cached']:,}")
        print(f"  DB Size:    {s['db_size_mb']} MB")
        print(f"  Hit Rate:   {s['hit_rate']}")
        if s['by_source']:
            print(f"  Sources:")
            for src, cnt in sorted(s['by_source'].items(), key=lambda x: -x[1]):
                print(f"    {src}: {cnt:,}")

    if args.top_domains:
        top = gi.top_domains(args.top_domains)
        print(f"\n  Top {args.top_domains} Domains:")
        for domain, cnt, urls in top:
            print(f"    {domain:40s} {cnt:>8,} records  {urls:>6,} URLs")

    if args.import_cdx:
        print(f"  Importing {args.import_cdx}...")
        count = gi.import_cdx_file(args.import_cdx)
        print(f"  Imported {count:,} records")

    if args.search:
        records = gi.cdx_search(args.search, limit=20)
        if records:
            print(f"\n  {len(records)} cached records for {args.search}:")
            for r in records[:20]:
                print(f"    [{r['timestamp']}] {r['status_code']} {r['mime_type']} "
                      f"{r['content_length'] or '?'} bytes — {r['source']}")
        else:
            print(f"  No cached records for {args.search}")

    if args.sparkline:
        spark = gi.sparkline(args.sparkline)
        if spark:
            print(f"\n  Sparkline for {args.sparkline}:")
            for year in sorted(spark.keys()):
                bar = "#" * min(50, spark[year])
                print(f"    {year}: {bar} ({spark[year]})")
        else:
            print(f"  No sparkline data for {args.sparkline}")

    gi.close()


if __name__ == "__main__":
    main()
