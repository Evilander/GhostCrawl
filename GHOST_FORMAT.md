# GHOST Format Specification v1.0

**GHOST** = **G**hostCrawl **H**ierarchical **O**bject **S**torage for **T**emporal archives

A self-contained, queryable web archive format designed for digital archaeology.

## Overview

A `.ghost` archive is a directory containing:
```
archive.ghost/
├── ghost.header      # 64-byte binary header
├── index.db          # SQLite database (indexes, records, metadata, FTS, link graph)
└── content/          # Content-addressed blob store
    ├── ab/
    │   └── cd/
    │       └── abcd1234...  # Compressed content blob
    └── ...
```

## Header Format

The `ghost.header` file is exactly 64 bytes:

| Offset | Size | Type     | Field          | Description                    |
|--------|------|----------|----------------|--------------------------------|
| 0      | 5    | bytes    | magic          | `0x47 0x48 0x4F 0x53 0x54` ("GHOST") |
| 5      | 1    | uint8    | version        | Format version (currently 1)   |
| 6      | 1    | uint8    | flags          | Bit flags (reserved)           |
| 7      | 1    | padding  | -              | Alignment padding              |
| 8      | 8    | uint64le | created_ts     | Unix timestamp of creation     |
| 16     | 8    | uint64le | record_count   | Total capture records          |
| 24     | 8    | uint64le | content_size   | Total raw content bytes        |
| 32     | 8    | uint64le | index_offset   | Reserved for packed format      |
| 40     | 8    | uint64le | reserved       | Reserved for future use        |
| 48     | 16   | padding  | -              | Alignment to 64 bytes          |

All integers are little-endian.

## Content Store

Content-addressed blob storage using a two-level directory hierarchy:

```
content/{hash[0:2]}/{hash[2:4]}/{full_hash}
```

Each blob file contains:
- **Byte 0**: Compression marker
  - `0x00` = gzip compressed
  - `0x01` = zstandard compressed
  - `0x02` = uncompressed
- **Bytes 1+**: Compressed/raw content

### Content Hashing
- **Preferred**: BLAKE3 (256-bit) — faster than SHA-256, collision-resistant
- **Fallback**: SHA-256 — when blake3 library not available
- Hash is computed over the **raw (uncompressed)** content

### Deduplication
Same content archived across N timestamps → stored **once**, referenced N times. The content hash is the universal key. Deduplication is automatic and transparent.

### Compression
- **Preferred**: Zstandard (zstd) level 6
  - Optional per-domain compression dictionaries for 10-30% better ratios on HTML
  - Dictionaries stored in `compression_dicts` table
- **Fallback**: gzip level 6

## Index Database (SQLite)

The `index.db` file uses SQLite with WAL journal mode for concurrent read access.

### Table: `records`
One row per web capture.

| Column          | Type     | Description                          |
|-----------------|----------|--------------------------------------|
| id              | INTEGER  | Auto-incrementing primary key        |
| url             | TEXT     | Original URL                         |
| domain          | TEXT     | Extracted domain name                |
| timestamp       | TEXT     | Capture timestamp (YYYYMMDDHHmmss)   |
| status_code     | INTEGER  | HTTP response status code            |
| mime_type       | TEXT     | Content-Type from HTTP headers       |
| content_hash    | TEXT     | BLAKE3/SHA-256 hash → Content Store  |
| content_size    | INTEGER  | Raw (uncompressed) content size      |
| compressed_size | INTEGER  | Stored (compressed) size             |
| http_headers    | TEXT     | JSON-encoded HTTP response headers   |
| source          | TEXT     | Provenance: crawl/wayback/commoncrawl/warc_import |
| created_at      | DATETIME | When this record was added            |

**Indexes:**
- `(url)` — URL lookup
- `(domain)` — Domain-scoped queries
- `(timestamp)` — Temporal queries
- `(content_hash)` — Hash-based lookup
- `(url, timestamp)` — UNIQUE constraint

### Table: `link_graph`
Directed edges between pages.

| Column           | Type    | Description                |
|------------------|---------|----------------------------|
| id               | INTEGER | Primary key                |
| source_url       | TEXT    | Page containing the link   |
| target_url       | TEXT    | URL being linked to        |
| link_text        | TEXT    | Anchor text (max 200 char) |
| rel              | TEXT    | Relationship type          |
| source_timestamp | TEXT    | Timestamp of source page   |

**Indexes:**
- `(source_url)` — Forward link lookup
- `(target_url)` — Reverse link lookup

### Table: `version_chains`
Track how URLs evolve over time.

| Column       | Type | Description                            |
|-------------|------|----------------------------------------|
| url         | TEXT | URL being tracked                      |
| timestamp   | TEXT | Capture timestamp                      |
| content_hash| TEXT | Content hash of this version           |
| prev_hash   | TEXT | Content hash of previous version       |
| diff_size   | INTEGER | Size of binary diff (optional)      |
| change_type | TEXT | new/unchanged/content_update/redirect/soft_404 |

**Primary key:** `(url, timestamp)`

### Table: `metadata`
Extensible key-value metadata per capture.

| Column    | Type    | Description                    |
|-----------|---------|--------------------------------|
| record_id | INTEGER | FK to records.id              |
| url       | TEXT    | URL (for queries without FK)   |
| timestamp | TEXT    | Capture timestamp              |
| key       | TEXT    | Metadata key                   |
| value     | TEXT    | Metadata value (JSON or string)|

Common keys:
- `ai_analysis` — AI-generated page analysis (from god_v2 agents)
- `entities` — Extracted named entities (people, organizations, dates)
- `classification` — Content category tags
- `provenance` — Which crawler/archive provided this capture
- `operator_fingerprint` — CMS/server software detection

### Virtual Table: `fts_content`
SQLite FTS5 full-text search index.

| Column       | Type | Description           |
|-------------|------|-----------------------|
| url         | TEXT | Source URL             |
| text_content| TEXT | Extracted text content |

Tokenizer: `porter` (English stemming)

### Table: `compression_dicts`
Per-domain Zstandard compression dictionaries.

| Column       | Type     | Description              |
|-------------|----------|--------------------------|
| domain      | TEXT     | Domain name (PK)         |
| dict_data   | BLOB     | Trained zstd dictionary  |
| sample_count| INTEGER  | Pages used for training  |
| created_at  | DATETIME | Training timestamp       |

## Merkle Tree

Content integrity is verified via a Merkle tree:

1. Leaves = content hashes from all records (ordered by record ID)
2. Internal nodes = BLAKE3(left_child + right_child)
3. If odd number of leaves, last leaf is duplicated
4. Root hash = fingerprint of entire archive

Verification can be:
- **Full**: Re-hash every blob, rebuild tree, compare root
- **Partial**: Verify a single blob exists and its hash matches
- **Audit**: Verify any blob with O(log n) proof path

## Append-Only Design

New captures are appended by:
1. Store content blob (if not already present via dedup)
2. INSERT record row
3. INSERT link graph edges
4. INSERT version chain entry
5. UPDATE FTS index
6. Merkle root is lazily recomputed on next `verify()`

Archives grow monotonically. No existing data is ever rewritten.

## CLI Usage

```bash
# Create from WARC
python ghostformat.py import-warc archive.ghost file.warc

# Show stats
python ghostformat.py info archive.ghost

# Full-text search
python ghostformat.py search archive.ghost "search query"

# Retrieve a capture
python ghostformat.py get archive.ghost "http://example.com/page.html"

# Version history
python ghostformat.py versions archive.ghost "http://example.com/page.html"

# Link graph
python ghostformat.py links archive.ghost "http://example.com/" --direction from
python ghostformat.py links archive.ghost "http://example.com/" --direction to

# Integrity verification
python ghostformat.py verify archive.ghost

# Export
python ghostformat.py export archive.ghost --warc output.warc.gz
python ghostformat.py export archive.ghost --cdx output.cdx
python ghostformat.py export archive.ghost --sqlite output.db
```

## Comparison

| Feature | WARC | WACZ | GHOST |
|---------|------|------|-------|
| Self-contained | No (needs CDX) | Yes (ZIP) | Yes (directory) |
| Random access | No (sequential) | Partial | Yes (SQLite + hash) |
| Deduplication | No | No | Yes (content-addressed) |
| Built-in search | No | No | Yes (FTS5) |
| Link graph | No | No | Yes |
| Version tracking | No | No | Yes |
| AI metadata | No | No | Yes (extensible) |
| Integrity proof | No | No | Yes (Merkle tree) |
| Compression | gzip | gzip | zstd + dictionaries |
| Append-friendly | Yes | No (ZIP repack) | Yes |

## Dependencies

Required: Python 3.8+, SQLite 3.35+ (for FTS5)

Optional:
- `blake3` — faster content hashing (falls back to SHA-256)
- `zstandard` — better compression (falls back to gzip)
- `warcio` — WARC import/export support
