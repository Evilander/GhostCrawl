# GhostCrawl

Multi-agent competitive web crawler for digital archaeology. Uses AI (Claude) for intelligent page analysis to find forgotten files across the Wayback Machine, Common Crawl, and 7+ archive services.

## The Problem

The internet forgets. Sites die, hosting expires, CDNs go offline, and files vanish. The Wayback Machine captures HTML pages reliably, but the actual *files* — the MP3s, ZIPs, PDFs, Flash games, software installers — are often missed by CDX index searches because they were dynamically served, behind JavaScript, or linked from pages that nobody thought to query directly.

## The Solution

GhostCrawl doesn't search for files. It **crawls dead sites through their archived HTML pages**, extracts every download link, and then hunts for those files across 8 different archive services. It's web crawling, but against a ghost.

The core insight: archived HTML pages are the *map* to files that archives captured but nobody knows how to find.

## Quick Start

```bash
pip install -r requirements.txt

# Interactive mode — browse 200+ curated dead sites or enter your own
python ghostcrawl.py

# GOD MODE — deploy an AI agent swarm that competes to find the most files
python ghostcrawl_god_v2.py

# Common Crawl deep mining — bypasses robots.txt entirely
python ghostcrawl_commoncrawl.py deadsite.com/files/*

# Route everything through Tor
python ghostcrawl_god_v2.py --tor
```

### Optional Dependencies

```bash
# AI-powered page analysis (agents use Claude Haiku to find hidden files)
pip install anthropic
export ANTHROPIC_API_KEY=your-key

# Tor routing (anonymous crawling with automatic circuit rotation)
pip install PySocks stem

# Cloudflare bypass (for ghostphase.py extraction engine)
pip install cloudscraper
```

## Architecture

```
                         ┌─────────────────────┐
                         │  ghostcrawl_god_v2   │  ← GOD MODE: 47 AI agents compete
                         │  (main entry point)  │     to find the most files
                         └──────────┬──────────┘
                                    │
              ┌─────────────────────┼─────────────────────┐
              │                     │                     │
    ┌─────────▼─────────┐ ┌────────▼────────┐ ┌─────────▼─────────┐
    │    ghostcrawl.py   │ │  ghostphase.py  │ │   ghostlight.py   │
    │  RequestManager    │ │  7-phase file   │ │  200+ dead sites  │
    │  TorManager        │ │  extraction     │ │  magic bytes      │
    │  CDX/Wayback API   │ │  engine         │ │  file categories  │
    │  8-strategy DL     │ │                 │ │                   │
    └─────────┬─────────┘ └─────────────────┘ └───────────────────┘
              │
    ┌─────────┼──────────────────────────────────┐
    │         │         │         │               │
    ▼         ▼         ▼         ▼               ▼
 commoncrawl  triangulate  livehunt  mindreader  prospector
 (S3 WARC)   (7 archives) (Shodan)  (LLM)       (crypto)
```

## Modules

### Core

| Module | Description |
|--------|-------------|
| `ghostcrawl_god_v2.py` | **Main entry** — Multi-agent AI crawler with 47 competitive personas, real-time leaderboard, TURBO parallel downloads |
| `ghostcrawl.py` | Base module — `RequestManager` (6 rotating sessions, adaptive rate limiting, 429 backoff), `TorManager` (SOCKS5 + circuit renewal), CDX/Wayback API, 8-strategy download fallback |
| `ghostlight.py` | Curated database of 200+ dead/changed websites organized by category, plus magic byte signatures for content validation |
| `ghostphase.py` | 7-phase universal file extraction engine: Ghost Cache, CDN Recon, API Excavation, Sitemap Siege, Referrer Bypass, Embed Extraction, Directory Storm |

### Specialized Crawlers

| Module | Description |
|--------|-------------|
| `ghostcrawl_commoncrawl.py` | Common Crawl S3 deep miner — streams cluster.idx files to find WARC shards, extracts raw page content. Bypasses robots.txt entirely. Includes username triangulation and CDN archaeology |
| `ghostcrawl_triangulate.py` | Cross-archive URL search across 7+ services (Wayback, archive.today, GhostArchive, UK Web Archive, Stanford, Library of Congress, Portuguese Web Archive) |
| `ghostcrawl_livehunt.py` | Live web scanning via Shodan (open directories, exposed servers), GitHub leak detection, URLScan integration |
| `ghostcrawl_mindreader.py` | LLM-powered webmaster behavior profiler — analyzes archived page structures to predict where files were stored |
| `ghostcrawl_prospector.py` | Crypto wallet/bounty hunting + keyword search across archived pages |
| `ghostcrawl_opendir_god.py` | Open directory scanner — finds and harvests Apache autoindex / nginx directory listings |
| `ghostcrawl_platforms.py` | Platform API scrapers (Patreon search, Reddit .json API, Safebooru imageboard) |

## How GOD MODE Works

GOD MODE deploys multiple AI agents that compete to find the most files on a target domain. Each agent has a unique persona with its own search strategy:

1. **Agent Deployment** — You pick a domain and file types. 1-27 agents are assigned different year ranges and strategies.

2. **Competitive Crawling** — Each agent independently crawls archived HTML pages, uses Claude Haiku to analyze page structure, and identifies download links that simple pattern matching would miss.

3. **Shared Discovery** — When one agent finds a file, all agents learn about it. A real-time leaderboard tracks which agent persona is winning.

4. **TURBO Downloads** — Found files are downloaded in parallel with a priority queue (crypto wallets > credentials > flash > audio > video > images), using the 8-strategy fallback system.

47 agent personas include methodical archivists, chaotic treasure hunters, specialists in specific file types, and "survival tier" agents that use unconventional approaches.

```bash
# Launch with 10 agents
python ghostcrawl_god_v2.py --agents 10

# Pick your team
python ghostcrawl_god_v2.py --agent-team

# Specific agents by name
python ghostcrawl_god_v2.py --agent-team CORSAIR,DOOB,PHANTOM

# Non-interactive, download immediately
python ghostcrawl_god_v2.py --domain deadsite.com --types audio,flash --yolo
```

## The 8-Strategy Download Fallback

When GhostCrawl finds a file URL, it doesn't just try once. It cascades through 8 strategies:

1. **Direct download** — Try the live URL first (some "dead" sites still serve files)
2. **CDX timestamp resolution** — Find the best Wayback Machine capture timestamp
3. **Wayback Machine download** — Fetch from `web.archive.org/web/{timestamp}id_/{url}`
4. **archive.today** — Check the archive.today mirror
5. **GhostArchive** — Check ghostarchive.org
6. **UK Web Archive** — Check the British Library's web archive
7. **Stanford Web Archive** — Check Stanford's WARC collection
8. **Library of Congress** — Check the LoC web archive

Each download is validated with magic byte signatures to catch soft 404s and HTML error pages masquerading as files.

## Common Crawl S3 Bypass

The Common Crawl module uses a direct S3 access technique that bypasses the (frequently down) CDX API:

1. Stream `cluster.idx` (50-100MB) line-by-line from S3
2. Binary search for the target domain in SURT format
3. Fetch the specific CDX shard via byte-range request
4. Decompress and parse WARC coordinates
5. Fetch actual page content via byte-range from the WARC file

This works when the Wayback Machine blocks a domain via robots.txt or DMCA, because Common Crawl doesn't respect robots.txt — it crawls everything.

```bash
# Search all 106 CC indexes for a domain
python ghostcrawl_commoncrawl.py deadsite.com

# Search with keywords within archived pages
python ghostcrawl_commoncrawl.py deadsite.com --keyword "rare" "unreleased"

# Extract and download all media from archived HTML
python ghostcrawl_commoncrawl.py deadsite.com --extract-media --download

# Username triangulation — find a username across all CC-crawled platforms
python ghostcrawl_commoncrawl.py --triangulate olduser123

# CDN archaeology — find media hosted on CDN subdomains
python ghostcrawl_commoncrawl.py --cdn deadsite.com
```

## Tor Support

GhostCrawl integrates with Tor for anonymous crawling. The `TorManager` class handles:

- **Auto-detection** — Verifies Tor is running via `check.torproject.org`
- **SOCKS5h routing** — DNS resolution happens through Tor (no DNS leaks)
- **Circuit renewal** — Automatic NEWNYM signal every N requests via the Tor control port
- **Rate limit recovery** — On 429 responses, automatically gets a fresh exit IP
- **Dual backend** — Uses the `stem` library if available, falls back to raw socket control

```bash
# Basic Tor routing
python ghostcrawl_god_v2.py --tor

# Aggressive circuit rotation (new IP every 30 requests)
python ghostcrawl_god_v2.py --tor --tor-renew 30

# Manual SOCKS5 proxy (non-Tor)
python ghostcrawl_god_v2.py --proxy socks5://host:port
```

Requires Tor running locally on port 9050. Install: `apt install tor` (Linux), `brew install tor` (macOS), or download from [torproject.org](https://www.torproject.org/).

## GhostPhase Extraction Engine

The 7-phase extraction engine (`ghostphase.py`) runs a comprehensive sweep against a target:

| Phase | Technique |
|-------|-----------|
| **Ghost Cache** | Google Cache, Wayback Machine, archive.today snapshots |
| **CDN Recon** | Discover CDN subdomains and asset URLs |
| **API Excavation** | Probe common API endpoints for exposed data |
| **Sitemap Siege** | Parse sitemap.xml, robots.txt for hidden paths |
| **Referrer Bypass** | Spoof referrer headers to bypass hotlink protection |
| **Embed Extraction** | Find embedded media in iframes, objects, embeds |
| **Directory Storm** | Brute-force common directory names for autoindex pages |

Phases run concurrently with a `StealthSession` that rotates user agents and optionally uses Cloudscraper for Cloudflare bypass.

## Networking

GhostCrawl's `RequestManager` is built for sustained, respectful-but-persistent crawling:

- **6 rotating sessions** with distinct user agents and referer headers
- **Adaptive rate limiting** — per-endpoint delay multipliers that scale up on 429s and gradually recover on success
- **30% jitter** on all delays to avoid fingerprinting
- **Exponential backoff** on connection errors (3 retries)
- **Session rotation** on rate limits to distribute load
- **Sec-Fetch headers** and DNT for realistic browser fingerprinting

## Environment Variables

All optional. GhostCrawl works without any of them — features gracefully degrade.

| Variable | Used By | Purpose |
|----------|---------|---------|
| `ANTHROPIC_API_KEY` | ghostcrawl_god_v2 | AI page analysis via Claude Haiku |
| `SHODAN_API_KEY` | ghostcrawl_livehunt | Shodan search for exposed servers |
| `GITHUB_TOKEN` | ghostcrawl_livehunt | GitHub code search for leaked files |
| `VIRUSTOTAL_API_KEY` | ghostcrawl_livehunt | URL reputation checking |
| `TOR_CONTROL_PASSWORD` | ghostcrawl (TorManager) | Tor control port authentication |

## Use Cases

- **Lost media recovery** — Find files from dead websites that exist in archives but nobody knows the URLs
- **Digital preservation** — Systematically archive content from dying platforms
- **OSINT research** — Cross-reference usernames and domains across archives
- **Security research** — Find exposed files, open directories, leaked credentials in archived pages
- **Music/art recovery** — Recover MP3s, Flash games, and artwork from dead hosting services

## License

MIT
