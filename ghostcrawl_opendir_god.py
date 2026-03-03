#!/usr/bin/env python3
"""
GhostCrawl OpenDir & FTP Hunter

Hunts for lost media outside of the Wayback Machine/Archive.org by finding
live Open Directories ("Index of /") and Anonymous FTP servers on the open web.

Features:
- DorkMaster: Executes advanced search engine dorks to find live open directories.
- Anonymous FTP Scanner: Tests domains for port 21 anonymous access.
- Deep Crawler: Recursively parses open directories to find rare files.
"""

import os
import sys
import time
import random
import argparse
import requests
import re
import ftplib
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

console = Console()

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TARGET_EXTENSIONS = [
    'swf', 'mid', 'midi', 'mod', 'rm', 'ram', 'ra', 
    'zip', 'rar', 'tar.gz', 'iso', 'exe',
    'mp3', 'flac', 'avi', 'mpg', 'mpeg', 'mkv', 'mp4'
]

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64; rv:109.0) Gecko/20100101 Firefox/121.0"
]

def get_proxy_dict(proxy_str):
    if not proxy_str: return None
    return {"http": proxy_str, "https": proxy_str}

import subprocess
import json

def get_decodo_auth():
    try:
        res = subprocess.run(["kv", "get", "DECODO_BASIC_AUTH_TOKEN"], capture_output=True, text=True, timeout=2)
        if res.returncode == 0 and res.stdout.strip():
            return f"Basic {res.stdout.strip()}"
    except Exception:
        pass
    # Fallback token
    return "Basic VTAwMDAzNjI1NDU6UFdfMTVmZWFlMGViOGJlY2E2MTQxYzA1NDlhZDZkYmQzYzYw"

def dork_duckduckgo(query, proxy=None):
    """Scrape DuckDuckGo using the Decodo AI Web Scraping API."""
    url = "https://scraper-api.decodo.com/v2/scrape"
    
    # URL to scrape
    search_url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(query)}"
    
    payload = {
          "url": search_url,
          "headless": "html",
          "geo": "United States",
          "locale": "en-us",
          "device_type": "desktop",
          "session_id": f"GhostCrawl_{random.randint(1000, 9999)}",
          "markdown": False
    }
      
    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "authorization": get_decodo_auth()
    }
    
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=60)
        if resp.status_code == 200:
            data = resp.json()
            # The API returns the scraped HTML in data.html or data.data.html
            html_content = data.get('data', {}).get('html', '')
            if not html_content:
                html_content = data.get('html', '')
                
            urls = re.findall(r'href="//duckduckgo\.com/l/\?uddg=([^"&]+)', html_content)
            if not urls:
                # Fallback if standard DuckDuckGo format is served
                urls = re.findall(r'href="(https?://[^"]+)"', html_content)
                
            decoded_urls = [urllib.parse.unquote(u) for u in urls]
            
            filtered = []
            for u in decoded_urls:
                if "google.com" in u or "duckduckgo.com" in u or "decodo.com" in u:
                    continue
                filtered.append(u)
            return list(set(filtered))
        else:
            console.print(f"[dim red]Decodo API failed: {resp.status_code} - {resp.text}[/dim red]")
    except Exception as e:
        console.print(f"[dim red]Decodo API request failed: {e}[/dim red]")
    return []

def check_anonymous_ftp(host):
    """Test if an FTP server allows anonymous login."""
    try:
        ftp = ftplib.FTP(timeout=5)
        ftp.connect(host, 21)
        ftp.login('anonymous', 'anonymous@example.com')
        files = ftp.nlst()
        ftp.quit()
        return True, files
    except Exception:
        return False, []

def crawl_open_directory(url, proxy=None, depth=0, max_depth=2, visited=None):
    """Recursively crawl an Apache/Nginx open directory."""
    if visited is None:
        visited = set()
    
    if depth > max_depth or url in visited:
        return []
        
    visited.add(url)
    results = []
    
    headers = {"User-Agent": random.choice(USER_AGENTS)}
    try:
        resp = requests.get(url, headers=headers, proxies=get_proxy_dict(proxy), timeout=10)
        if resp.status_code != 200:
            return []
            
        links = re.findall(r'href="([^"]+)"', resp.text)
        
        for link in links:
            if link.startswith(('?', '/', '#')) or link.lower().startswith(('http', 'javascript', 'mailto')):
                continue
                
            full_url = urllib.parse.urljoin(url, link)
            
            if link.endswith('/'):
                if full_url not in visited:
                    results.extend(crawl_open_directory(full_url, proxy, depth+1, max_depth, visited))
            else:
                ext = link.split('.')[-1].lower() if '.' in link else ''
                if ext in TARGET_EXTENSIONS:
                    results.append(full_url)
                    
    except Exception:
        pass
        
    return results

def main():
    parser = argparse.ArgumentParser(description="GhostCrawl OpenDir & FTP Hunter")
    parser.add_argument("--query", required=True, help="Keyword to search for (e.g. 'mario', 'soundtrack', '90s')")
    parser.add_argument("--proxies-file", help="File containing proxies")
    parser.add_argument("--yolo", action="store_true", help="Download discovered files immediately")
    args = parser.parse_args()
    
    console.print(Panel("[bold yellow]🏴‍☠️ GHOSTCRAWL: OPEN DIR & FTP HUNTER 🏴‍☠️[/bold yellow]\n[dim]Leaving the archives. Hunting the live web.[/dim]", expand=False))

    proxies = []
    if args.proxies_file:
        try:
            with open(args.proxies_file, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#'): continue
                    parts = line.split(':')
                    if len(parts) == 4 and not line.startswith('http'):
                        host, port, user, pwd = parts
                        proxies.append(f"http://{user}:{pwd}@{host}:{port}")
                    else:
                        proxies.append(line)
            console.print(f"[dim]Loaded {len(proxies)} proxies.[/dim]")
        except Exception as e:
            console.print(f"[red]Failed to load proxies: {e}[/red]")
            return

    console.print(f"\n[bold cyan]🔍 Executing Open Directory Dorks for '{args.query}'...[/bold cyan]")
    
    # Advanced dorks specifically designed to find live open directories
    dorks = [
        f'intitle:"index of" "{args.query}" +(mp3|swf|zip|rar|iso)',
        f'intitle:"index of" "parent directory" "{args.query}" -html -php',
        f'"{args.query}" "Index of /" "Name" "Last modified" "Size"'
    ]
    
    found_dirs = []
    for dork in dorks:
        proxy = random.choice(proxies) if proxies else None
        console.print(f"  [dim]Running dork:[/dim] {dork}")
        urls = dork_duckduckgo(dork, proxy)
        if urls:
            console.print(f"    [green]→ Found {len(urls)} potential directories[/green]")
        found_dirs.extend(urls)
        time.sleep(random.uniform(2, 4))
        
    found_dirs = list(set(found_dirs))
    console.print(f"\n[bold green]✓ Compiled list of {len(found_dirs)} unique target directories.[/bold green]")
    
    if not found_dirs:
        console.print("[yellow]No open directories found for that query. Try broader keywords.[/yellow]")
        return
        
    console.print("\n[bold cyan]🕷️ Crawling discovered directories & testing for Anonymous FTP...[/bold cyan]")
    
    all_treasures = []
    open_ftps = []
    
    with ThreadPoolExecutor(max_workers=min(10, len(found_dirs)*2)) as executor:
        futures = {}
        for dir_url in found_dirs:
            proxy = random.choice(proxies) if proxies else None
            # Extract base domain to test for anonymous FTP
            domain = urllib.parse.urlparse(dir_url).netloc
            if ":" in domain:
                domain = domain.split(":")[0] # Strip port if present
                
            futures[executor.submit(crawl_open_directory, dir_url, proxy)] = dir_url
            futures[executor.submit(check_anonymous_ftp, domain)] = f"FTP:{domain}"
            
        for future in as_completed(futures):
            target = futures[future]
            try:
                res = future.result()
                if target.startswith("FTP:"):
                    success, files = res
                    if success:
                        domain = target[4:]
                        if domain not in open_ftps:
                            open_ftps.append(domain)
                            console.print(f"  [bold green]🔓 ANONYMOUS FTP OPEN:[/bold green] ftp://{domain} ({len(files)} items in root)")
                else:
                    if res:
                        console.print(f"  [cyan]Scraped {len(res)} files from[/cyan] {target}")
                        for f in res:
                            all_treasures.append(f)
                            fname = urllib.parse.unquote(f.split('/')[-1])
                            ext = fname.split('.')[-1].upper() if '.' in fname else 'FILE'
                            console.print(f"    [dim]>[/dim] [{ext}] {fname}")
            except Exception as e:
                pass
                
    console.print(f"\n[bold yellow]🏆 Total treasures discovered: {len(all_treasures)}[/bold yellow]")
    if open_ftps:
        console.print(f"[bold green]🔓 Total Anonymous FTP servers found: {len(open_ftps)}[/bold green]")
        
    if args.yolo and all_treasures:
        dest_dir = "D:/ghostlight/treasures"
        os.makedirs(dest_dir, exist_ok=True)
        console.print(f"\n[bold magenta]⚡ YOLO MODE ACTIVE - Downloading {len(all_treasures)} files...[/bold magenta]")
        
        def download_file(url, proxy):
            fname = urllib.parse.unquote(url.split("/")[-1])
            dest = os.path.join(dest_dir, fname)
            if os.path.exists(dest): return
            try:
                r = requests.get(url, stream=True, proxies=get_proxy_dict(proxy), timeout=20)
                if r.status_code == 200:
                    with open(dest, 'wb') as f:
                        for chunk in r.iter_content(chunk_size=8192):
                            f.write(chunk)
                    console.print(f"  [green]✓ Downloaded:[/green] {fname}")
            except Exception:
                console.print(f"  [red]✗ Failed:[/red] {fname}")
                
        with ThreadPoolExecutor(max_workers=5) as dl_exec:
            for url in all_treasures:
                proxy = random.choice(proxies) if proxies else None
                dl_exec.submit(download_file, url, proxy)

if __name__ == "__main__":
    main()
