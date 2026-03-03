#!/usr/bin/env python3
"""
GhostCrawl Prospector — Treasure Hunting Tools

Three concrete monetization engines:

1. CRYPTO VAULT HUNTER
   Scans dead sites from 2009-2016 for wallet files, private keys,
   bitcoin configs, and other crypto artifacts buried in archived pages.

2. DOMAIN PROSPECTOR
   Cross-references Wayback CDX capture density with current WHOIS
   availability to find high-value expired domains you can register for $10.

3. LOST MEDIA BOUNTY SCANNER
   Searches known lost media bounty lists and cross-references against
   Wayback availability to find content people are actively paying for.

All tools use the existing GhostCrawl infrastructure (CDX, Wayback, proxies).
"""

import os
import json
import re
import time
from dataclasses import dataclass
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

from rich.console import Console
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, MofNCompleteColumn
from rich import box

console = Console()

# ═══════════════════════════════════════════════════════════════════════════════
# SHARED: CDX helpers
# ═══════════════════════════════════════════════════════════════════════════════

CDX = "https://web.archive.org/cdx/search/cdx"
WB = "https://web.archive.org/web"

CDX_FALLBACKS = [
    ("arquivo.pt", "https://arquivo.pt/wayback/cdx", "https://arquivo.pt/wayback", True),  # JSONL format
]


def _get_rm():
    from ghostcrawl import get_request_manager
    return get_request_manager()


def _cdx_query(params, timeout=25):
    """Run a CDX query with automatic fallback to alternative archives."""
    # Try primary Wayback
    try:
        resp = _get_rm().get(CDX, params=params, timeout=timeout)
        if resp.status_code == 200:
            data = resp.json()
            if len(data) > 1:
                keys = data[0]
                return [dict(zip(keys, row)) for row in data[1:]]
    except Exception:
        pass

    # Try fallbacks
    for name, cdx_url, _wb_url, is_jsonl in CDX_FALLBACKS:
        try:
            console.print(f"  [dim]CDX fallback: {name}...[/dim]")
            resp = _get_rm().get(cdx_url, params=params, timeout=timeout)
            if resp.status_code == 200 and resp.text.strip():
                if is_jsonl:
                    # Parse JSONL format
                    import json as _json
                    records = []
                    for line in resp.text.strip().split("\n"):
                        line = line.strip()
                        if line:
                            try:
                                records.append(_json.loads(line))
                            except _json.JSONDecodeError:
                                continue
                    if records:
                        console.print(f"  [green]{name} responded with {len(records)} rows[/green]")
                        return records
                else:
                    data = resp.json()
                    if len(data) > 1:
                        keys = data[0]
                        console.print(f"  [green]{name} responded with {len(data)-1} rows[/green]")
                        return [dict(zip(keys, row)) for row in data[1:]]
        except Exception:
            continue

    return []


# ═══════════════════════════════════════════════════════════════════════════════
# 1. CRYPTO VAULT HUNTER
# ═══════════════════════════════════════════════════════════════════════════════

# File patterns that could contain crypto wealth
CRYPTO_FILE_PATTERNS = [
    "wallet.dat", "wallet.dat.bak", "wallet.bak",
    "bitcoin.conf", ".bitcoin/wallet.dat",
    "*.key", "*.pem", "*.p12", "*.pfx", "*.keystore",
    "backup.zip", "backup.tar.gz", "backup.rar", "site_backup*",
    "*.sql.gz", "*.sql.bz2", "db_backup*", "database.sql",
    ".env", "config.php", "wp-config.php", "settings.py",
    "config.ini", "config.yml", "config.json",
    ".htpasswd", "passwd", "shadow",
    "id_rsa", "id_dsa", "id_ed25519", "*.gpg", "*.asc",
    "electrum.dat", "multibit.key", "*.wallet",
    "mining.conf", "cgminer.conf", "bfgminer.conf", "pooler.conf",
]

# URL path segments that indicate crypto/financial content
CRYPTO_PATH_SIGNALS = [
    "bitcoin", "btc", "crypto", "wallet", "mining",
    "blockchain", "satoshi", "litecoin", "ltc", "dogecoin",
    "ethereum", "eth", "monero", "xmr",
    "backup", "bak", "old", "archive", "dump",
    "config", "conf", "private", "secret", "keys",
    "admin", "cpanel", "phpmyadmin", "wp-admin",
    "_vti_pvt", "_vti_cnf",  # FrontPage server extensions leak configs
]

# Domains known to have had crypto communities (2009-2016)
CRYPTO_ERA_DOMAINS = [
    {"domain": "bitcointalk.org", "note": "Original Bitcoin forum. Users posted wallet files, shared keys early on.", "years": "2009-present", "era": "genesis"},
    {"domain": "bitcoin.org", "note": "Satoshi's original site.", "years": "2008-present", "era": "genesis"},
    {"domain": "mtgox.com", "note": "Handled 70% of all BTC trades. Hacked 2014. $460M lost.", "years": "2010-2014", "era": "exchange"},
    {"domain": "btc-e.com", "note": "Sketchy exchange. Seized by FBI 2017. $4B laundered.", "years": "2011-2017", "era": "exchange"},
    {"domain": "bitcoinforum.com", "note": "Early forum. Many users shared configs/wallets.", "years": "2011-2016", "era": "forum"},
    {"domain": "freebitcoins.appspot.com", "note": "Free bitcoin faucet. Gave away 5 BTC at a time.", "years": "2010-2012", "era": "faucet"},
    {"domain": "bitcoinfaucet.com", "note": "Gavin Andresen's faucet. 5 BTC per visitor.", "years": "2010-2012", "era": "faucet"},
    {"domain": "silkroad6ownowfk.onion", "note": "Silk Road. $1.2B in BTC transactions.", "years": "2011-2013", "era": "darknet"},
    {"domain": "localbitcoins.com", "note": "P2P bitcoin trading.", "years": "2012-present", "era": "exchange"},
    {"domain": "bitinstant.com", "note": "Charlie Shrem's exchange. Shut down.", "years": "2011-2014", "era": "exchange"},
    {"domain": "hackforums.net", "note": "Hacker forum. Crypto discussions, wallet tools.", "years": "2005-present", "era": "forum"},
    {"domain": "nulled.to", "note": "Cracking forum. Leaked databases with crypto.", "years": "2012-present", "era": "forum"},
    {"domain": "halfin.org", "note": "Hal Finney - received first BTC transaction from Satoshi.", "years": "2009-2014", "era": "genesis"},
    {"domain": "instawallet.org", "note": "Browser-based Bitcoin wallet. Hacked 2013.", "years": "2011-2013", "era": "wallet"},
    {"domain": "inputs.io", "note": "Bitcoin wallet service. 4,100 BTC stolen.", "years": "2013", "era": "wallet"},
    {"domain": "blockchain.info", "note": "Block explorer + web wallet. Still alive as blockchain.com.", "years": "2011-present", "era": "explorer"},
    {"domain": "easywallet.org", "note": "Simple web wallet. Shut down.", "years": "2012-2014", "era": "wallet"},
    {"domain": "multibit.org", "note": "Popular early Bitcoin wallet software.", "years": "2011-2017", "era": "wallet"},
    {"domain": "electrum.org", "note": "Lightweight Bitcoin wallet.", "years": "2011-present", "era": "wallet"},
    {"domain": "lolcow.farm", "note": "Forum with crypto donation addresses.", "years": "2014-present", "era": "forum"},
]

@dataclass
class CryptoHit:
    domain: str
    url: str
    timestamp: str
    file_type: str
    confidence: str  # high, medium, low
    reason: str
    size: int = 0
    wayback_url: str = ""


def _classify_crypto_confidence(url, mimetype=""):
    """Classify how likely a URL is to contain crypto treasure."""
    path = urlparse(url).path.lower()
    filename = path.split("/")[-1] if "/" in path else path

    high_patterns = ["wallet.dat", ".key", "id_rsa", "id_dsa", "id_ed25519",
                     "bitcoin.conf", "electrum.dat", "multibit.key", ".wallet",
                     "mining.conf", "cgminer.conf"]
    for p in high_patterns:
        if p in filename:
            return "high", f"Direct crypto/key file: {filename}"

    if any(filename.endswith(ext) for ext in [".zip", ".tar.gz", ".rar", ".7z", ".bak"]):
        if any(sig in path for sig in ["backup", "dump", "export", "old", "archive"]):
            return "high", f"Backup archive in suspicious path: {path}"

    medium_patterns = [".env", "config.php", "wp-config", "settings.py",
                       "config.ini", "config.yml", "config.json",
                       ".htpasswd", "passwd", ".sql"]
    for p in medium_patterns:
        if p in filename:
            return "medium", f"Config/credential file: {filename}"

    if any(d in path for d in ["/_vti_pvt/", "/private/", "/secret/", "/keys/",
                                "/backup/", "/.bitcoin/", "/admin/"]):
        return "medium", f"File in sensitive directory: {path}"

    if any(sig in path for sig in ["bitcoin", "btc", "crypto", "wallet", "mining"]):
        return "low", f"Crypto-related path: {path}"

    return "low", f"Potential interest: {path}"


def crypto_vault_scan(domain, from_year=2009, to_year=2017, deep=False):
    """
    Scan a domain's Wayback archive for crypto wallet files and secrets.
    Returns list of CryptoHit.
    """
    hits = []
    console.print(f"\n[bold red]CRYPTO VAULT HUNTER[/bold red]")
    console.print(f"[dim]Scanning {domain} ({from_year}-{to_year}) for buried treasure...[/dim]\n")

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Querying CDX for all archived files...", total=None)

        all_files = _cdx_query({
            "url": domain,
            "matchType": "domain",
            "output": "json",
            "limit": 5000,
            "fl": "timestamp,original,mimetype,statuscode,length",
            "filter": "statuscode:200",
            "collapse": "urlkey",
            "from": str(from_year),
            "to": str(to_year + 1),
        }, timeout=45)

        progress.update(task, completed=len(all_files), total=len(all_files),
                       description=f"CDX returned {len(all_files)} archived files")

        if not all_files:
            console.print("[yellow]No archived files found for this domain/period.[/yellow]")
            return hits

        task2 = progress.add_task("Analyzing files for crypto signals...", total=len(all_files))

        for record in all_files:
            url = record.get("original", "")
            path = urlparse(url).path.lower()
            mimetype = record.get("mimetype", "")
            size = int(record.get("length", 0) or 0)
            timestamp = record.get("timestamp", "")

            if mimetype == "text/html" and not any(d in path for d in
                    ["/private/", "/secret/", "/backup/", "/admin/", "/_vti_pvt/"]):
                progress.advance(task2)
                continue

            is_interesting = False
            file_type = "unknown"
            filename = path.split("/")[-1] if "/" in path else ""
            for pattern in CRYPTO_FILE_PATTERNS:
                if "*" in pattern:
                    regex = pattern.replace(".", r"\.").replace("*", ".*")
                    if re.search(regex, filename):
                        is_interesting = True
                        file_type = pattern
                        break
                elif pattern in filename or pattern in path:
                    is_interesting = True
                    file_type = pattern
                    break

            if not is_interesting:
                for signal in CRYPTO_PATH_SIGNALS:
                    if signal in path:
                        if mimetype != "text/html" or any(d in path for d in
                                ["/private/", "/secret/", "/backup/", "/admin/"]):
                            is_interesting = True
                            file_type = f"path_signal:{signal}"
                            break

            if is_interesting:
                confidence, reason = _classify_crypto_confidence(url, mimetype)
                wb_url = f"{WB}/{timestamp}id_/{url}"
                hits.append(CryptoHit(
                    domain=domain,
                    url=url,
                    timestamp=timestamp,
                    file_type=file_type,
                    confidence=confidence,
                    reason=reason,
                    size=size,
                    wayback_url=wb_url,
                ))

            progress.advance(task2)

    # Phase 3: If deep mode, also check common hidden paths via CDX exact match
    if deep:
        console.print("[dim]Deep scan: checking common hidden paths...[/dim]")
        hidden_paths = [
            "wallet.dat", "backup.zip", "backup.sql", "dump.sql",
            ".env", "config.php", "wp-config.php.bak", "wp-config.php.old",
            ".git/config", ".svn/entries", "server-status",
            "_vti_pvt/service.pwd", "_vti_pvt/authors.pwd",
            "admin/backup.zip", "private/keys.zip",
            ".bitcoin/wallet.dat", "bitcoin.conf",
            ".ssh/id_rsa", ".ssh/authorized_keys",
        ]

        with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
                      BarColumn(), MofNCompleteColumn(), console=console) as progress:
            task3 = progress.add_task("Probing hidden paths...", total=len(hidden_paths))

            for hidden in hidden_paths:
                for proto in ["http", "https"]:
                    url = f"{proto}://{domain}/{hidden}"
                    results = _cdx_query({
                        "url": url,
                        "matchType": "exact",
                        "output": "json",
                        "limit": 1,
                        "fl": "timestamp,original,statuscode,length,mimetype",
                        "filter": "statuscode:200",
                    }, timeout=10)

                    for r in results:
                        confidence, reason = _classify_crypto_confidence(url)
                        size = int(r.get("length", 0) or 0)
                        ts = r.get("timestamp", "")
                        hits.append(CryptoHit(
                            domain=domain,
                            url=url,
                            timestamp=ts,
                            file_type=hidden,
                            confidence=confidence,
                            reason=f"[DEEP] {reason}",
                            size=size,
                            wayback_url=f"{WB}/{ts}id_/{url}",
                        ))

                progress.advance(task3)

    return hits


def display_crypto_hits(hits):
    """Display crypto scan results sorted by confidence."""
    if not hits:
        console.print("[yellow]No crypto artifacts found.[/yellow]")
        return

    # Sort: high > medium > low
    order = {"high": 0, "medium": 1, "low": 2}
    hits.sort(key=lambda h: (order.get(h.confidence, 3), -h.size))

    table = Table(title="CRYPTO VAULT SCAN RESULTS", box=box.HEAVY_EDGE)
    table.add_column("Conf", width=6)
    table.add_column("Type", width=20)
    table.add_column("URL", max_width=60, no_wrap=True)
    table.add_column("Size", width=10, justify="right")
    table.add_column("Date", width=10)
    table.add_column("Reason", max_width=40)

    conf_style = {"high": "bold red", "medium": "bold yellow", "low": "dim"}

    for hit in hits:
        style = conf_style.get(hit.confidence, "dim")
        size_str = f"{hit.size:,}" if hit.size else "?"
        date_str = hit.timestamp[:8] if hit.timestamp else "?"
        table.add_row(
            f"[{style}]{hit.confidence.upper()}[/{style}]",
            hit.file_type[:20],
            hit.url,
            size_str,
            date_str,
            hit.reason[:40],
        )

    console.print(table)

    high_count = sum(1 for h in hits if h.confidence == "high")
    med_count = sum(1 for h in hits if h.confidence == "medium")
    console.print(f"\n  [bold red]{high_count} HIGH[/bold red] / "
                  f"[bold yellow]{med_count} MEDIUM[/bold yellow] / "
                  f"[dim]{len(hits) - high_count - med_count} LOW[/dim] confidence hits")

    if high_count:
        console.print(f"\n[bold green]HIGH-PRIORITY DOWNLOAD URLS:[/bold green]")
        for hit in hits:
            if hit.confidence == "high":
                console.print(f"  {hit.wayback_url}")


# ═══════════════════════════════════════════════════════════════════════════════
# 2. DOMAIN PROSPECTOR
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class DomainProspect:
    domain: str
    first_seen: str
    last_seen: str
    total_captures: int
    peak_captures_year: int
    peak_captures_count: int
    category_guess: str
    estimated_value: str  # $, $$, $$$
    whois_status: str  # available, taken, unknown
    reason: str


def _estimate_domain_value(domain, total_captures, peak_year):
    """Heuristic domain value estimation."""
    score = 0
    reasons = []

    # Short domains are worth more
    name = domain.split(".")[0]
    if len(name) <= 3:
        score += 50
        reasons.append("ultra-short name")
    elif len(name) <= 5:
        score += 30
        reasons.append("short name")
    elif len(name) <= 8:
        score += 10

    # .com premium
    if domain.endswith(".com"):
        score += 30
        reasons.append(".com TLD")
    elif domain.endswith(".org"):
        score += 15
    elif domain.endswith(".net"):
        score += 10

    # Traffic indicator (more captures = more traffic)
    if total_captures > 10000:
        score += 40
        reasons.append(f"massive archive ({total_captures:,} captures)")
    elif total_captures > 1000:
        score += 25
        reasons.append(f"heavy archive ({total_captures:,} captures)")
    elif total_captures > 100:
        score += 10

    # Dictionary word bonus
    common_words = ["game", "music", "video", "shop", "store", "mail", "chat",
                    "forum", "news", "tech", "code", "data", "file", "free",
                    "web", "net", "app", "site", "host", "cloud", "crypto",
                    "trade", "pay", "cash", "gold", "money", "bank"]
    if name.lower() in common_words:
        score += 40
        reasons.append("dictionary word domain")
    elif any(w in name.lower() for w in common_words):
        score += 15
        reasons.append("contains keyword")

    # Peak activity in golden eras
    if 2005 <= peak_year <= 2012:
        score += 10
        reasons.append("Web 2.0 era peak")

    if score >= 80:
        return "$$$", ", ".join(reasons)
    elif score >= 40:
        return "$$", ", ".join(reasons)
    else:
        return "$", ", ".join(reasons) or "standard domain"


def _check_domain_available(domain):
    """Check if a domain might be available via RDAP/WHOIS heuristics."""
    try:
        import socket
        # Quick DNS check - if it doesn't resolve, it might be available
        try:
            socket.getaddrinfo(domain, 80, socket.AF_INET, socket.SOCK_STREAM)
            return "taken"
        except socket.gaierror:
            return "possibly_available"
    except Exception:
        return "unknown"


def domain_prospector_scan(category=None, min_captures=50, check_availability=True):
    """
    Scan curated dead domains for expired high-value domains.
    """
    console.print(f"\n[bold green]DOMAIN PROSPECTOR[/bold green]")
    console.print(f"[dim]Finding expired domains worth registering...[/dim]\n")

    try:
        from ghostlight import CURATED_TARGETS
    except ImportError:
        console.print("[red]Could not import CURATED_TARGETS from ghostlight.py[/red]")
        return []

    # Gather all dead/changed domains
    candidates = []
    categories = [category] if category and category in CURATED_TARGETS else list(CURATED_TARGETS.keys())

    for cat in categories:
        for target in CURATED_TARGETS[cat]:
            status = target.get("status", "").lower()
            if any(s in status for s in ["dead", "frozen", "defunct"]):
                candidates.append((cat, target))

    console.print(f"  Found [cyan]{len(candidates)}[/cyan] dead/frozen domains to evaluate\n")

    prospects = []

    with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
                  BarColumn(), MofNCompleteColumn(), console=console) as progress:
        task = progress.add_task("Evaluating domains...", total=len(candidates))

        for cat, target in candidates:
            domain = target["domain"]

            # Skip archive.org direct links
            if domain.startswith("archive.org/"):
                progress.advance(task)
                continue

            # Single CDX query: get timestamps collapsed by month to derive
            # first_seen, last_seen, total captures, and peak year
            year_counts = defaultdict(int)
            first_seen = "?"
            last_seen = "?"
            total_captures = 0
            try:
                resp = _get_rm().get(CDX, params={
                    "url": domain, "matchType": "domain", "output": "json",
                    "limit": 5000, "fl": "timestamp", "collapse": "timestamp:6",
                }, timeout=20)
                if resp.status_code == 200:
                    data = resp.json()
                    total_captures = max(0, len(data) - 1)
                    if total_captures > 0:
                        first_seen = data[1][0][:4]
                        last_seen = data[-1][0][:4]
                        for row in data[1:]:
                            year_counts[row[0][:4]] += 1
            except Exception:
                pass

            if total_captures < min_captures:
                progress.advance(task)
                continue

            peak_year = max(year_counts, key=year_counts.get) if year_counts else "2005"
            peak_count = year_counts.get(peak_year, 0)

            # Check availability
            whois_status = "unknown"
            if check_availability:
                whois_status = _check_domain_available(domain)

            value, reason = _estimate_domain_value(
                domain, total_captures, int(peak_year))

            prospects.append(DomainProspect(
                domain=domain,
                first_seen=first_seen,
                last_seen=last_seen,
                total_captures=total_captures,
                peak_captures_year=int(peak_year),
                peak_captures_count=peak_count,
                category_guess=cat,
                estimated_value=value,
                whois_status=whois_status,
                reason=reason,
            ))

            progress.advance(task)
            time.sleep(0.3)  # rate limit

    return prospects


def display_domain_prospects(prospects):
    """Display domain prospector results."""
    if not prospects:
        console.print("[yellow]No prospects found.[/yellow]")
        return

    # Sort by value then availability
    value_order = {"$$$": 0, "$$": 1, "$": 2}
    avail_order = {"possibly_available": 0, "unknown": 1, "taken": 2}
    prospects.sort(key=lambda p: (value_order.get(p.estimated_value, 3),
                                   avail_order.get(p.whois_status, 3)))

    table = Table(title="DOMAIN PROSPECTOR RESULTS", box=box.HEAVY_EDGE)
    table.add_column("Value", width=6)
    table.add_column("Status", width=12)
    table.add_column("Domain", style="cyan", max_width=35)
    table.add_column("Captures", width=10, justify="right")
    table.add_column("Active", width=12)
    table.add_column("Peak", width=6)
    table.add_column("Category", max_width=25)
    table.add_column("Why", max_width=30)

    for p in prospects:
        val_style = {"$$$": "bold green", "$$": "yellow", "$": "dim"}.get(p.estimated_value, "dim")
        avail_style = {"possibly_available": "bold green", "taken": "red", "unknown": "yellow"}.get(p.whois_status, "dim")

        table.add_row(
            f"[{val_style}]{p.estimated_value}[/{val_style}]",
            f"[{avail_style}]{p.whois_status}[/{avail_style}]",
            p.domain,
            f"{p.total_captures:,}",
            f"{p.first_seen}-{p.last_seen}",
            str(p.peak_captures_year),
            p.category_guess[:25],
            p.reason[:30],
        )

    console.print(table)

    available = [p for p in prospects if p.whois_status == "possibly_available"]
    if available:
        console.print(f"\n[bold green]POTENTIALLY AVAILABLE DOMAINS ({len(available)}):[/bold green]")
        val_map = {"$$$": "bold green", "$$": "yellow", "$": "dim"}
        for p in available:
            style = val_map.get(p.estimated_value, "dim")
            console.print(f"  [{style}]{p.estimated_value}[/{style}] {p.domain} -- {p.reason}")


# ═══════════════════════════════════════════════════════════════════════════════
# 3. LOST MEDIA BOUNTY SCANNER
# ═══════════════════════════════════════════════════════════════════════════════

# Known lost media items that people actively search for
# These are items from Lost Media Wiki, Reddit r/lostmedia, etc.
LOST_MEDIA_BOUNTIES = [
    {"query": "cracks.am", "category": "software", "bounty": "community fame", "description": "Legendary cracking forum database dumps"},
    {"query": "oink.cd", "category": "music", "bounty": "community fame", "description": "Private music tracker. 180K users. Raided 2007."},
    {"query": "what.cd", "category": "music", "bounty": "community fame", "description": "Largest music tracker ever. 400K torrents. Died 2016."},
    {"query": "demonoid.me", "category": "mixed", "bounty": "community fame", "description": "Semi-private tracker. Database leaked/lost."},
    {"query": "megaupload.com", "category": "files", "bounty": "$$$$", "description": "50M daily users. FBI seized. Data still on servers."},
    {"query": "clock.avi", "category": "video", "bounty": "community", "description": "Lost creepypasta video. Years of searching."},
    {"query": "candle cove", "category": "video", "bounty": "community", "description": "Supposedly lost TV show. Creepypasta origin."},
    {"query": "saki sanobashi", "category": "anime", "bounty": "$$", "description": "Lost anime. Active community search since 2015."},
    {"query": "shaye saint john", "category": "video", "bounty": "community", "description": "Art project lost videos. Some recovered."},
    {"query": "most mysterious song", "category": "music", "bounty": "$$$", "description": "Unknown 80s song. Worldwide search since 2007."},
    {"query": "dragonball rap", "category": "music", "bounty": "community", "description": "Original fan raps from early internet."},
    {"query": "windows neptune", "category": "software", "bounty": "$$", "description": "Unreleased Windows build. Partial ISOs exist."},
    {"query": "longhorn", "category": "software", "bounty": "$", "description": "Early Windows Vista builds."},
    {"query": "polybius", "category": "game", "bounty": "$$$$", "description": "Legendary lost arcade game. CIA connection rumors."},
    {"query": "ben drowned", "category": "game", "bounty": "community", "description": "Haunted Majora's Mask cartridge. ARG/creepypasta."},
    {"query": "timecube.com", "category": "web", "bounty": "community", "description": "Gene Ray's Time Cube theory. Archived."},
    {"query": "youareananidiot.org", "category": "web", "bounty": "community", "description": "Classic trojan/popup site."},
    {"query": "hampsterdance.com", "category": "web", "bounty": "nostalgia", "description": "Original hampster dance."},
]


@dataclass
class BountyHit:
    bounty_name: str
    bounty_description: str
    bounty_value: str
    domain: str
    url: str
    timestamp: str
    mimetype: str
    size: int
    wayback_url: str


def lost_media_bounty_scan(max_results_per_bounty=10):
    """
    Scan Wayback CDX for content matching known lost media bounties.
    """
    console.print(f"\n[bold magenta]LOST MEDIA BOUNTY SCANNER[/bold magenta]")
    console.print(f"[dim]Cross-referencing {len(LOST_MEDIA_BOUNTIES)} known bounties against Wayback archive...[/dim]\n")

    all_hits = []

    with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
                  BarColumn(), MofNCompleteColumn(), console=console) as progress:
        task = progress.add_task("Scanning bounties...", total=len(LOST_MEDIA_BOUNTIES))

        for bounty in LOST_MEDIA_BOUNTIES:
            query = bounty["query"]

            # CDX search — use domain matchType for domain-like queries,
            # prefix for everything else (CDX doesn't support wildcard in url field)
            is_domain = "." in query and "/" not in query and " " not in query
            results = _cdx_query({
                "url": query,
                "matchType": "domain" if is_domain else "prefix",
                "output": "json",
                "limit": max_results_per_bounty,
                "fl": "timestamp,original,mimetype,statuscode,length",
                "filter": "statuscode:200",
                "collapse": "urlkey",
            }, timeout=15)

            for r in results:
                url = r.get("original", "")
                ts = r.get("timestamp", "")
                mime = r.get("mimetype", "")
                size = int(r.get("length", 0) or 0)

                # Skip trivial HTML pages for file-type bounties
                if bounty["category"] in ["music", "video", "software", "game"]:
                    if mime == "text/html" and size < 50000:
                        continue

                all_hits.append(BountyHit(
                    bounty_name=query,
                    bounty_description=bounty["description"],
                    bounty_value=bounty["bounty"],
                    domain=urlparse(url).netloc,
                    url=url,
                    timestamp=ts,
                    mimetype=mime,
                    size=size,
                    wayback_url=f"{WB}/{ts}id_/{url}",
                ))

            progress.advance(task)
            time.sleep(0.5)  # rate limit CDX

    return all_hits


def display_bounty_hits(hits):
    """Display lost media bounty scan results."""
    if not hits:
        console.print("[yellow]No bounty matches found.[/yellow]")
        return

    # Group by bounty
    by_bounty = defaultdict(list)
    for hit in hits:
        by_bounty[hit.bounty_name].append(hit)

    table = Table(title="LOST MEDIA BOUNTY MATCHES", box=box.HEAVY_EDGE)
    table.add_column("Bounty", width=8)
    table.add_column("Target", style="cyan", max_width=25)
    table.add_column("Hits", width=5, justify="right")
    table.add_column("Description", max_width=50)
    table.add_column("Best File", max_width=40)
    table.add_column("Size", width=10, justify="right")

    for bounty_name, bounty_hits in sorted(by_bounty.items(),
            key=lambda x: -max(h.size for h in x[1])):
        best = max(bounty_hits, key=lambda h: h.size)
        val_style = {"$$$$": "bold red", "$$$": "bold green", "$$": "yellow",
                     "$": "dim", "community": "cyan", "community fame": "magenta",
                     "nostalgia": "blue"}.get(best.bounty_value, "dim")

        table.add_row(
            f"[{val_style}]{best.bounty_value}[/{val_style}]",
            bounty_name,
            str(len(bounty_hits)),
            best.bounty_description[:50],
            best.url.split("/")[-1][:40] if "/" in best.url else best.url[:40],
            f"{best.size:,}" if best.size else "?",
        )

    console.print(table)
    console.print(f"\n  Total: [bold]{len(hits)}[/bold] matches across [bold]{len(by_bounty)}[/bold] bounties")


# ═══════════════════════════════════════════════════════════════════════════════
# 4. KEYWORD HUNTER — Search specific domains for keywords in archived pages
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class KeywordHit:
    domain: str
    url: str
    timestamp: str
    mimetype: str
    size: int
    wayback_url: str
    snippet: str = ""


def keyword_search(domains, keyword, from_year=None, to_year=None, max_pages=200):
    """
    Search archived pages of given domains for a keyword/phrase.
    Fetches page content from Wayback and does text matching.
    """
    console.print(f"\n[bold blue]KEYWORD HUNTER[/bold blue]")
    console.print(f"[dim]Searching {len(domains)} domain(s) for:[/dim] [bold cyan]\"{keyword}\"[/bold cyan]\n")

    all_hits = []

    for domain in domains:
        console.print(f"[bold]Scanning {domain}...[/bold]")

        # Get all archived HTML pages for this domain
        pages = _cdx_query({
            "url": domain,
            "matchType": "domain",
            "output": "json",
            "limit": max_pages,
            "fl": "timestamp,original,mimetype,statuscode,length",
            "filter": ["statuscode:200", "mimetype:text/html"],
            "collapse": "urlkey",
            **({"from": str(from_year)} if from_year else {}),
            **({"to": str(to_year + 1)} if to_year else {}),
        }, timeout=30)

        if not pages:
            console.print(f"  [yellow]No archived pages found for {domain}[/yellow]")
            continue

        console.print(f"  Found [cyan]{len(pages)}[/cyan] archived pages to search")

        with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
                      BarColumn(), MofNCompleteColumn(), console=console) as progress:
            task = progress.add_task(f"Searching {domain}...", total=len(pages))

            keyword_lower = keyword.lower()

            def _check_page(record):
                url = record.get("original", "")
                ts = record.get("timestamp", "")
                wb_url = f"{WB}/{ts}id_/{url}"
                try:
                    resp = _get_rm().get(wb_url, timeout=20)
                    if resp.status_code == 200:
                        text = resp.text
                        text_lower = text.lower()
                        idx = text_lower.find(keyword_lower)
                        if idx >= 0:
                            start = max(0, idx - 100)
                            end = min(len(text), idx + len(keyword) + 100)
                            snippet = text[start:end].replace("\n", " ").replace("\r", " ").strip()
                            snippet = re.sub(r'<[^>]+>', ' ', snippet)
                            snippet = re.sub(r'\s+', ' ', snippet).strip()
                            return KeywordHit(
                                domain=domain, url=url, timestamp=ts,
                                mimetype=record.get("mimetype", ""),
                                size=int(record.get("length", 0) or 0),
                                wayback_url=wb_url, snippet=snippet[:200],
                            )
                except Exception:
                    pass
                return None

            with ThreadPoolExecutor(max_workers=8) as executor:
                futures = {executor.submit(_check_page, rec): rec for rec in pages}
                for future in as_completed(futures):
                    result = future.result()
                    if result:
                        all_hits.append(result)
                    progress.advance(task)

        time.sleep(0.5)  # rate limit between domains

    return all_hits


def display_keyword_hits(hits, keyword):
    """Display keyword search results."""
    if not hits:
        console.print(f"[yellow]No pages containing \"{keyword}\" found.[/yellow]")
        return

    table = Table(title=f"KEYWORD RESULTS: \"{keyword}\"", box=box.HEAVY_EDGE)
    table.add_column("Domain", style="cyan", max_width=25)
    table.add_column("URL", max_width=50, no_wrap=True)
    table.add_column("Date", width=10)
    table.add_column("Snippet", max_width=60)

    for hit in hits:
        snippet = hit.snippet
        date_str = f"{hit.timestamp[:4]}-{hit.timestamp[4:6]}-{hit.timestamp[6:8]}" if len(hit.timestamp) >= 8 else hit.timestamp

        table.add_row(
            hit.domain,
            hit.url,
            date_str,
            snippet[:60],
        )

    console.print(table)
    console.print(f"\n  [bold]{len(hits)}[/bold] pages contain [cyan]\"{keyword}\"[/cyan]")

    console.print(f"\n[bold]Wayback URLs:[/bold]")
    for hit in hits[:20]:
        console.print(f"  {hit.wayback_url}")
    if len(hits) > 20:
        console.print(f"  [dim]...and {len(hits) - 20} more[/dim]")


def keyword_hunter_main():
    """Standalone entry point for keyword hunter from god_v2 menu."""
    from InquirerPy import inquirer

    console.print(f"\n[bold blue]KEYWORD HUNTER[/bold blue]")
    console.print(f"[dim]Search archived pages of specific domains for keywords/phrases[/dim]\n")

    while True:
        source = inquirer.select(
            message="How to pick target domains?",
            choices=[
                {"name": "Browse curated domain list", "value": "browse"},
                {"name": "Enter domain(s) manually (comma-separated)", "value": "manual"},
                {"name": "<< Back / Exit", "value": "exit"},
            ],
        ).execute()

        if source == "exit":
            return

        domains = []
        if source == "browse":
            try:
                from ghostlight import CURATED_TARGETS
                categories = list(CURATED_TARGETS.keys())
                cat_choices = [{"name": f"{cat} ({len(CURATED_TARGETS[cat])} sites)", "value": cat} for cat in categories]
                cat_choices.append({"name": "<< Back", "value": "__back__"})
                selected_cat = inquirer.select(message="Pick a category:", choices=cat_choices).execute()
                if selected_cat == "__back__":
                    continue

                targets = CURATED_TARGETS[selected_cat]
                domain_choices = [
                    {"name": f"{t['domain']} — {t['note'][:50]}", "value": t["domain"]}
                    for t in targets if not t["domain"].startswith("archive.org/")
                ]
                if not domain_choices:
                    console.print("[yellow]No crawlable domains in this category.[/yellow]")
                    continue

                while True:
                    selected = inquirer.checkbox(
                        message="Select domains (space to toggle, enter to confirm):",
                        choices=domain_choices,
                    ).execute()
                    if selected:
                        domains = selected
                        break
                    console.print("[yellow]No domains selected — toggle with space, then press enter.[/yellow]")
            except ImportError:
                console.print("[red]Could not import curated targets.[/red]")
                continue
        else:
            raw = inquirer.text(message="Domain(s) to search (comma-separated, e.g. geocities.com, angelfire.com):").execute()
            if not raw.strip():
                continue
            domains = [d.strip().replace("http://", "").replace("https://", "").rstrip("/")
                       for d in raw.split(",") if d.strip()]

        if not domains:
            continue

        keyword = inquirer.text(message="Keyword or phrase to search for:").execute()
        if not keyword.strip():
            continue

        from_yr = inquirer.text(message="From year (blank = all):", default="").execute()
        to_yr = inquirer.text(message="To year (blank = all):", default="").execute()
        from_year = int(from_yr) if from_yr.strip() else None
        to_year = int(to_yr) if to_yr.strip() else None

        hits = keyword_search(domains, keyword.strip(), from_year=from_year, to_year=to_year)
        display_keyword_hits(hits, keyword.strip())

        if hits:
            save = inquirer.confirm(message="Save results to JSON?", default=True).execute()
            if save:
                fname = f"keyword_{keyword.strip().replace(' ', '_')[:30]}_{int(time.time())}.json"
                with open(fname, "w") as f:
                    json.dump([{
                        "domain": h.domain, "url": h.url, "timestamp": h.timestamp,
                        "snippet": h.snippet, "wayback_url": h.wayback_url,
                    } for h in hits], f, indent=2)
                console.print(f"[green]Saved to {fname}[/green]")

        again = inquirer.confirm(message="Search again?", default=True).execute()
        if not again:
            return


# ═══════════════════════════════════════════════════════════════════════════════
# INTERACTIVE CLI
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    from InquirerPy import inquirer

    console.print(f"\n[bold yellow]GHOSTCRAWL PROSPECTOR[/bold yellow]")
    console.print(f"[dim]Treasure hunting tools for buried internet wealth[/dim]\n")

    while True:
        mode = inquirer.select(
            message="Pick your tool:",
            choices=[
                {"name": "CRYPTO VAULT HUNTER — Scan for wallet files & keys on dead sites", "value": "crypto"},
                {"name": "DOMAIN PROSPECTOR — Find expired high-value domains", "value": "domains"},
                {"name": "LOST MEDIA BOUNTY — Match bounty lists against Wayback", "value": "bounty"},
                {"name": "FULL SWEEP — Run all three tools", "value": "sweep"},
                {"name": "<< Back / Exit", "value": "exit"},
            ],
        ).execute()

        if mode == "exit":
            return

        if mode == "crypto":
            source = inquirer.select(
                message="Target selection:",
                choices=[
                    {"name": "Browse known crypto-era domains", "value": "list"},
                    {"name": "Enter a domain manually", "value": "manual"},
                    {"name": "<< Back", "value": "back"},
                ],
            ).execute()

            if source == "back":
                continue

            if source == "list":
                domain_choices = [
                    {"name": f"{d['domain']} — {d['note'][:50]}", "value": d["domain"]}
                    for d in CRYPTO_ERA_DOMAINS
                    if not d["domain"].endswith(".onion")  # skip onion
                ]
                domain_choices.append({"name": "<< Back", "value": "__back__"})
                domain = inquirer.select(message="Pick a domain:", choices=domain_choices).execute()
                if domain == "__back__":
                    continue
            else:
                domain = inquirer.text(message="Domain to scan (e.g. deadsite.com):").execute()
                if not domain.strip():
                    continue
                domain = domain.strip().replace("http://", "").replace("https://", "").rstrip("/")

            deep = inquirer.confirm(message="Deep scan (check hidden paths too)?", default=True).execute()
            hits = crypto_vault_scan(domain, deep=deep)
            display_crypto_hits(hits)

            if hits:
                save = inquirer.confirm(message="Save results to JSON?", default=True).execute()
                if save:
                    fname = f"crypto_scan_{domain.replace('.', '_')}_{int(time.time())}.json"
                    with open(fname, "w") as f:
                        json.dump([{
                            "domain": h.domain, "url": h.url, "timestamp": h.timestamp,
                            "file_type": h.file_type, "confidence": h.confidence,
                            "reason": h.reason, "size": h.size, "wayback_url": h.wayback_url,
                        } for h in hits], f, indent=2)
                    console.print(f"[green]Saved to {fname}[/green]")

        elif mode == "domains":
            prospects = domain_prospector_scan()
            display_domain_prospects(prospects)

            if prospects:
                save = inquirer.confirm(message="Save results to JSON?", default=True).execute()
                if save:
                    fname = f"domain_prospects_{int(time.time())}.json"
                    with open(fname, "w") as f:
                        json.dump([{
                            "domain": p.domain, "first_seen": p.first_seen,
                            "last_seen": p.last_seen, "total_captures": p.total_captures,
                            "estimated_value": p.estimated_value, "whois_status": p.whois_status,
                            "reason": p.reason,
                        } for p in prospects], f, indent=2)
                    console.print(f"[green]Saved to {fname}[/green]")

        elif mode == "bounty":
            hits = lost_media_bounty_scan()
            display_bounty_hits(hits)

            if hits:
                save = inquirer.confirm(message="Save results to JSON?", default=True).execute()
                if save:
                    fname = f"bounty_scan_{int(time.time())}.json"
                    with open(fname, "w") as f:
                        json.dump([{
                            "bounty": h.bounty_name, "description": h.bounty_description,
                            "value": h.bounty_value, "url": h.url, "size": h.size,
                            "wayback_url": h.wayback_url,
                        } for h in hits], f, indent=2)
                    console.print(f"[green]Saved to {fname}[/green]")

        elif mode == "sweep":
            console.print("[bold]Running full sweep...[/bold]\n")

            # Crypto scan across all known domains
            console.print("[bold red]═══ CRYPTO VAULT HUNTER ═══[/bold red]")
            all_crypto = []
            for entry in CRYPTO_ERA_DOMAINS[:10]:  # top 10 for sweep
                if entry["domain"].endswith(".onion"):
                    continue
                hits = crypto_vault_scan(entry["domain"], deep=False)
                all_crypto.extend(hits)
                time.sleep(1)
            display_crypto_hits(all_crypto)

            # Domain prospector
            console.print(f"\n[bold green]═══ DOMAIN PROSPECTOR ═══[/bold green]")
            prospects = domain_prospector_scan()
            display_domain_prospects(prospects)

            # Bounty scan
            console.print(f"\n[bold magenta]═══ LOST MEDIA BOUNTY ═══[/bold magenta]")
            bounty_hits = lost_media_bounty_scan()
            display_bounty_hits(bounty_hits)

            # Summary
            console.print(f"\n[bold yellow]═══ SWEEP SUMMARY ═══[/bold yellow]")
            console.print(f"  Crypto hits:    [red]{len(all_crypto)}[/red]")
            console.print(f"  Domain prospects: [green]{len(prospects)}[/green]")
            console.print(f"  Bounty matches: [magenta]{len(bounty_hits)}[/magenta]")


if __name__ == "__main__":
    main()
