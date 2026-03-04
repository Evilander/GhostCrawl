#!/usr/bin/env python3
"""
GhostCrawl Live Hunt — Real-Time Treasure Discovery

Unlike the archive triangulator (which searches the past), LiveHunt searches
the LIVE internet for exposed treasures right now:

  1. SHODAN OPEN DIRECTORY HUNTER
     Searches Shodan for misconfigured servers with open directory listings
     containing wallet.dat, .env files, backups, private keys, etc.

  2. CENSYS INFRASTRUCTURE SCANNER
     Finds live hosts with exposed services, backup ports, debug panels.

  3. GITHUB LEAK SCANNER
     Searches GitHub for accidentally committed secrets, wallet files,
     private keys, and database dumps referencing target domains.

  4. OPEN DIRECTORY ENUMERATOR
     Given a URL to an open directory, recursively enumerates all files,
     classifies them by type, and identifies high-value targets.

All functions return ArchiveHit objects for compatibility with the
triangulation display system.

Env vars (all optional — functions degrade gracefully):
  SHODAN_API_KEY, CENSYS_API_ID, CENSYS_API_SECRET,
  GITHUB_TOKEN, VIRUSTOTAL_API_KEY
"""

import os
import re
import json
import time
import subprocess
import requests
from urllib.parse import urljoin, urlparse, unquote
from html.parser import HTMLParser
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict

from rich.console import Console
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn
from rich import box

console = Console()


@dataclass
class LiveHit:
    url: str
    source: str
    hit_type: str = ""  # "wallet", "key", "backup", "config", "database", "media"
    size: int = 0
    details: str = ""
    confidence: float = 0.5


# Patterns that indicate high-value files
TREASURE_PATTERNS = {
    "wallet": [
        "wallet.dat", "wallet.dat.bak", "wallet.bak", "electrum.dat",
        "multibit.key", ".wallet", "keystore.json",
    ],
    "key": [
        "id_rsa", "id_dsa", "id_ed25519", ".pem", ".p12", ".pfx",
        ".keystore", ".gpg", ".asc", "private.key", "privkey.pem",
    ],
    "config": [
        ".env", "config.php", "wp-config.php", "settings.py",
        "config.ini", "config.yml", ".htpasswd", "database.yml",
        "application.properties", "secrets.json", "credentials.json",
    ],
    "backup": [
        "backup.zip", "backup.tar.gz", "backup.rar", "site_backup",
        "db_backup", "database.sql", ".sql.gz", ".sql.bz2",
        "dump.sql", "full_backup",
    ],
    "crypto": [
        "bitcoin.conf", ".bitcoin", "btcwallet",
        "mnemonic.txt", "seed.txt", "recovery.txt",
    ],
}

TREASURE_QUERIES_SHODAN = [
    'title:"index of" "wallet.dat"',
    'title:"index of" ".env"',
    'title:"index of" "backup.sql"',
    'title:"index of" "id_rsa"',
    'title:"index of" ".bitcoin"',
    'http.html:"wallet.dat" port:80,8080,8443',
    'title:"index of" "backup.zip"',
    'title:"index of" "database.sql"',
    'title:"index of" ".pem"',
    'title:"index of" "private" "key"',
]


# ═══════════════════════════════════════════════════════════════════════════
# 1. SHODAN OPEN DIRECTORY HUNTER
# ═══════════════════════════════════════════════════════════════════════════

def shodan_hunt(queries=None, max_results_per_query=20):
    """Search Shodan for open directories with treasure files.

    Returns list of LiveHit objects.
    """
    api_key = os.environ.get("SHODAN_API_KEY", "")
    if not api_key:
        console.print("[yellow]SHODAN_API_KEY not set — skipping Shodan hunt[/yellow]")
        return []

    if not queries:
        queries = TREASURE_QUERIES_SHODAN

    all_hits = []
    seen_ips = set()

    with Progress(SpinnerColumn(), TextColumn("{task.description}"),
                  BarColumn(), TextColumn("{task.completed}/{task.total}")) as progress:
        task = progress.add_task("Shodan hunting...", total=len(queries))

        for query in queries:
            try:
                params = {"key": api_key, "query": query, "minify": True}
                resp = requests.get("https://api.shodan.io/shodan/host/search",
                                    params=params, timeout=15)
                if resp.status_code == 200:
                    data = resp.json()
                    for match in data.get("matches", [])[:max_results_per_query]:
                        ip = match.get("ip_str", "")
                        if ip in seen_ips:
                            continue
                        seen_ips.add(ip)

                        port = match.get("port", 80)
                        hostnames = match.get("hostnames", [])
                        host = hostnames[0] if hostnames else ip
                        proto = "https" if port == 443 else "http"
                        url = f"{proto}://{host}:{port}/"
                        http_data = match.get("http", {}) or match.get("data", "")

                        hit_type = _classify_treasure(query + " " + str(http_data))
                        all_hits.append(LiveHit(
                            url=url,
                            source="shodan",
                            hit_type=hit_type,
                            details=f"Query: {query[:50]}",
                            confidence=0.7,
                        ))
                elif resp.status_code == 401:
                    console.print("[red]Shodan API key invalid[/red]")
                    break
            except Exception as e:
                console.print(f"  [dim]Shodan query failed: {e}[/dim]")

            progress.advance(task)
            time.sleep(1)  # Shodan rate limit

    return all_hits


# ═══════════════════════════════════════════════════════════════════════════
# 2. GITHUB LEAK SCANNER
# ═══════════════════════════════════════════════════════════════════════════

GITHUB_LEAK_QUERIES = [
    'filename:wallet.dat',
    'filename:.env PRIVATE_KEY',
    'filename:id_rsa',
    'filename:credentials.json password',
    '"BEGIN RSA PRIVATE KEY"',
    'filename:bitcoin.conf rpcpassword',
    'filename:mnemonic.txt',
    'filename:seed.txt',
]


def github_hunt(domain=None, queries=None, max_per_query=10):
    """Search GitHub for leaked secrets and wallet files.

    If domain is provided, all queries are scoped to that domain.
    """
    token = os.environ.get("GITHUB_TOKEN", "")
    if not queries:
        queries = GITHUB_LEAK_QUERIES

    all_hits = []

    for query in queries[:6]:  # GitHub search rate limit is 10 req/min
        full_query = f'"{domain}" {query}' if domain else query
        try:
            if token:
                headers = {
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github.v3+json",
                }
                resp = requests.get(
                    "https://api.github.com/search/code",
                    params={"q": full_query, "per_page": max_per_query},
                    headers=headers, timeout=15,
                )
                if resp.status_code == 200:
                    for item in resp.json().get("items", []):
                        repo = item.get("repository", {}).get("full_name", "")
                        path = item.get("path", "")
                        html_url = item.get("html_url", "")
                        raw_url = html_url.replace("github.com", "raw.githubusercontent.com").replace("/blob/", "/") if html_url else ""

                        hit_type = _classify_treasure(path)
                        all_hits.append(LiveHit(
                            url=html_url,
                            source="github",
                            hit_type=hit_type,
                            details=f"{repo}/{path}",
                            confidence=0.8,
                        ))
                elif resp.status_code == 403:
                    console.print("[yellow]GitHub rate limit hit — pausing[/yellow]")
                    time.sleep(30)
            else:
                # Fallback: gh CLI
                result = subprocess.run(
                    ["gh", "api", "search/code", "-f", f"q={full_query}", "-f", f"per_page={max_per_query}"],
                    capture_output=True, text=True, timeout=15,
                )
                if result.returncode == 0:
                    data = json.loads(result.stdout)
                    for item in data.get("items", []):
                        path = item.get("path", "")
                        html_url = item.get("html_url", "")
                        hit_type = _classify_treasure(path)
                        all_hits.append(LiveHit(
                            url=html_url,
                            source="github",
                            hit_type=hit_type,
                            details=path,
                            confidence=0.8,
                        ))
        except Exception:
            continue
        time.sleep(6)  # GitHub: 10 searches/min for authenticated users

    return all_hits


# ═══════════════════════════════════════════════════════════════════════════
# 3. OPEN DIRECTORY ENUMERATOR
# ═══════════════════════════════════════════════════════════════════════════

class _DirListingParser(HTMLParser):
    """Parse Apache/nginx autoindex directory listings."""

    def __init__(self):
        super().__init__()
        self.links = []
        self._in_a = False

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href", "")
            if href and not href.startswith("?") and not href.startswith("/"):
                self.links.append(href)


def enumerate_opendir(url, max_depth=3, max_files=500):
    """Recursively enumerate an open directory, classify files by type.

    Returns list of LiveHit objects for each interesting file found.
    """
    hits = []
    visited = set()
    queue = [(url.rstrip("/") + "/", 0)]
    file_count = 0

    while queue and file_count < max_files:
        current_url, depth = queue.pop(0)
        if current_url in visited or depth > max_depth:
            continue
        visited.add(current_url)

        try:
            resp = requests.get(current_url, timeout=10,
                                headers={"User-Agent": "Mozilla/5.0 (compatible; GhostCrawl/1.0)"})
            if resp.status_code != 200:
                continue

            parser = _DirListingParser()
            parser.feed(resp.text)

            for link in parser.links:
                full_url = urljoin(current_url, link)
                if full_url in visited:
                    continue

                # Directory — add to queue
                if link.endswith("/"):
                    if depth < max_depth:
                        queue.append((full_url, depth + 1))
                    continue

                # File — classify it
                file_count += 1
                filename = unquote(link.split("?")[0])
                hit_type = _classify_treasure(filename)

                if hit_type:  # Only record interesting files
                    hits.append(LiveHit(
                        url=full_url,
                        source="opendir",
                        hit_type=hit_type,
                        details=filename,
                        confidence=0.9,
                    ))
        except Exception:
            continue

    return hits


# ═══════════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════════

def _classify_treasure(text):
    """Classify a filename/path into a treasure type."""
    text_lower = text.lower()
    for category, patterns in TREASURE_PATTERNS.items():
        for pattern in patterns:
            if pattern.lower() in text_lower:
                return category
    return ""


def display_livehunt_results(hits, title="Live Hunt Results"):
    """Pretty-print live hunt results."""
    if not hits:
        console.print(f"\n[dim]No results for {title}[/dim]")
        return

    table = Table(title=title, box=box.ROUNDED)
    table.add_column("Source", style="bold", width=10)
    table.add_column("Type", width=10)
    table.add_column("URL", style="cyan", max_width=60)
    table.add_column("Details", style="dim", max_width=30)
    table.add_column("Conf", justify="right", width=5)

    # Sort by confidence descending
    hits.sort(key=lambda h: h.confidence, reverse=True)

    type_colors = {
        "wallet": "bold red",
        "key": "bold yellow",
        "crypto": "bold red",
        "config": "yellow",
        "backup": "green",
        "database": "green",
        "media": "blue",
    }

    for hit in hits[:50]:
        type_style = type_colors.get(hit.hit_type, "white")
        conf_str = f"{hit.confidence:.0%}"
        table.add_row(
            hit.source,
            f"[{type_style}]{hit.hit_type or '?'}[/{type_style}]",
            hit.url[:60],
            hit.details[:30],
            conf_str,
        )

    console.print(table)

    # Summary by type
    by_type = defaultdict(int)
    by_source = defaultdict(int)
    for hit in hits:
        by_type[hit.hit_type or "unknown"] += 1
        by_source[hit.source] += 1

    console.print(f"\n  [bold]Total:[/bold] {len(hits)} hits")
    console.print(f"  [bold]By type:[/bold] {', '.join(f'{t}: {c}' for t, c in sorted(by_type.items(), key=lambda x: -x[1]))}")
    console.print(f"  [bold]By source:[/bold] {', '.join(f'{s}: {c}' for s, c in sorted(by_source.items(), key=lambda x: -x[1]))}")


# ═══════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════

def main(target_override=None, mode=None):
    if target_override:
        # Called from god_v2 — bypass argparse
        class Args:
            pass
        args = Args()
        args.shodan = mode in ('shodan', 'all')
        args.github = mode in ('github', 'all')
        args.opendir = target_override if mode == 'opendir' else None
        args.domain = target_override if mode != 'opendir' else None
        args.all = mode == 'all'
    else:
        import argparse
        parser = argparse.ArgumentParser(description="GhostCrawl Live Hunt — Real-Time Treasure Discovery")
        parser.add_argument("--shodan", action="store_true", help="Run Shodan open directory hunt")
        parser.add_argument("--github", action="store_true", help="Run GitHub leak scanner")
        parser.add_argument("--opendir", type=str, help="Enumerate a specific open directory URL")
        parser.add_argument("--domain", type=str, help="Target domain for GitHub/Shodan scoping")
        parser.add_argument("--all", action="store_true", help="Run all hunts")
        args = parser.parse_args()

    console.print(f"\n[bold red]GHOSTCRAWL LIVE HUNT[/bold red] [dim]— Real-Time Treasure Discovery[/dim]\n")

    # Show available API keys
    keys = {"SHODAN_API_KEY": "Shodan", "GITHUB_TOKEN": "GitHub",
            "CENSYS_API_ID": "Censys", "VIRUSTOTAL_API_KEY": "VirusTotal"}
    available = [v for k, v in keys.items() if os.environ.get(k)]
    missing = [v for k, v in keys.items() if not os.environ.get(k)]
    if available:
        console.print(f"  [green]Keys found:[/green] {', '.join(available)}")
    if missing:
        console.print(f"  [dim]Keys missing:[/dim] {', '.join(missing)}")
    console.print()

    all_hits = []

    if args.shodan or args.all:
        console.print("[bold]Shodan Open Directory Hunt[/bold]")
        hits = shodan_hunt()
        all_hits.extend(hits)
        display_livehunt_results(hits, "Shodan Results")

    if args.github or args.all:
        console.print("\n[bold]GitHub Leak Scanner[/bold]")
        hits = github_hunt(domain=args.domain)
        all_hits.extend(hits)
        display_livehunt_results(hits, "GitHub Results")

    if args.opendir:
        console.print(f"\n[bold]Open Directory Enumeration:[/bold] {args.opendir}")
        hits = enumerate_opendir(args.opendir)
        all_hits.extend(hits)
        display_livehunt_results(hits, f"OpenDir: {args.opendir[:40]}")

    if all_hits:
        console.print(f"\n[bold green]Grand total: {len(all_hits)} live treasures found[/bold green]")
    elif not (args.shodan or args.github or args.opendir or args.all):
        console.print("[dim]Use --shodan, --github, --opendir <url>, or --all[/dim]")


if __name__ == "__main__":
    main()
