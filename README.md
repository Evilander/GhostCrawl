# GhostCrawl

Multi-agent competitive web crawler for digital archaeology. Uses AI (Claude) for intelligent page analysis to find forgotten files across the Wayback Machine, Common Crawl, and 7+ archive services.

## What It Does

Instead of searching CDX indexes for binary files (which misses dynamically-served content), GhostCrawl **crawls dead sites through their archived HTML pages**, extracts download links, and recovers the actual files. It's web crawling, but against a ghost.

## Quick Start

```bash
pip install -r requirements.txt

# Basic mode - browse curated domains or enter your own
python ghostcrawl.py

# GOD MODE - multi-agent AI-powered competitive crawling
python ghostcrawl_god_v2.py

# Route through Tor
python ghostcrawl_god_v2.py --tor

# Common Crawl deep mining (bypasses robots.txt)
python ghostcrawl_commoncrawl.py oldsite.com/files/*
```

### Optional Dependencies

```bash
# AI-powered page analysis (agents use Claude Haiku to find hidden files)
pip install anthropic
export ANTHROPIC_API_KEY=your-key

# Tor routing (anonymous crawling with circuit rotation)
pip install PySocks stem

# Cloudflare bypass (for ghostphase.py)
pip install cloudscraper
```

## Modules

| Module | Description |
|--------|-------------|
| `ghostcrawl_god_v2.py` | **Main entry** — Multi-agent AI-powered crawler with 47 competitive personas |
| `ghostcrawl.py` | Base module — RequestManager, CDX/Wayback, 8-strategy download fallback |
| `ghostlight.py` | Curated domain database (200+ dead/changed sites) + magic bytes validation |
| `ghostphase.py` | 7-phase universal file extraction (cache, CDN, API, sitemap, referrer, embed, directory) |
| `ghostcrawl_commoncrawl.py` | Common Crawl S3 deep miner — direct WARC extraction, bypasses robots.txt |
| `ghostcrawl_platforms.py` | Platform scrapers (Patreon, Reddit, Safebooru) |
| `ghostcrawl_prospector.py` | Crypto wallet/bounty hunter + keyword search across archives |
| `ghostcrawl_triangulate.py` | Cross-archive URL/domain search (7+ archive services) |
| `ghostcrawl_livehunt.py` | Live web scanning (Shodan, GitHub leaks, open directories) |
| `ghostcrawl_mindreader.py` | LLM-powered webmaster behavior profiler |
| `ghostcrawl_opendir_god.py` | Open directory scanner and file harvester |

## Tor Support

GhostCrawl integrates with Tor for anonymous crawling with automatic circuit rotation:

```bash
# Basic Tor routing (requires Tor running on port 9050)
python ghostcrawl_god_v2.py --tor

# Rotate circuit every 30 requests (default: 50)
python ghostcrawl_god_v2.py --tor --tor-renew 30

# Manual proxy (any SOCKS5/HTTP proxy)
python ghostcrawl_god_v2.py --proxy socks5://127.0.0.1:9050
```

On 429 rate limits, Tor circuits are automatically renewed for a fresh exit IP.

## Environment Variables (all optional)

| Variable | Used By | Purpose |
|----------|---------|---------|
| `ANTHROPIC_API_KEY` | ghostcrawl_god_v2 | AI page analysis (Claude Haiku) |
| `SHODAN_API_KEY` | ghostcrawl_livehunt | Live web scanning |
| `GITHUB_TOKEN` | ghostcrawl_livehunt | GitHub leak scanning |
| `VIRUSTOTAL_API_KEY` | ghostcrawl_livehunt | URL analysis |
| `TOR_CONTROL_PASSWORD` | ghostcrawl (TorManager) | Tor circuit renewal auth |

## How GOD MODE Works

GOD MODE deploys multiple AI agents that compete to find the most files on a target domain. Each agent has a unique persona and search strategy — some are methodical archivists, others are chaotic treasure hunters. They share discoveries on a real-time leaderboard and learn from each other's findings.

## License

MIT
