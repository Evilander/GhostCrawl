#!/usr/bin/env python3
"""
GhostCrawl MindReader — LLM-Simulated Server Architecture

Instead of brute-force directory fuzzing, we ask an LLM to "think like the
webmaster" and generate the most probable hidden directory/file tree.

Given:
  - The visible directory structure from crawled pages
  - The year the site was active
  - Server software hints (FrontPage, Apache, etc.)
  - The site's topic/genre
  - Any visible naming conventions

The LLM generates:
  - High-probability hidden directories
  - Predicted file paths based on naming patterns
  - Likely backup/admin paths based on era-appropriate conventions

This is psychological profiling of a webmaster, 20 years after the fact.
"""

import os
import json
import re
from urllib.parse import urlparse
from dataclasses import dataclass, field
from collections import defaultdict

from rich.console import Console
from rich.table import Table
from rich.tree import Tree
from rich import box

console = Console()


@dataclass
class MindReaderProfile:
    domain: str
    visible_paths: list = field(default_factory=list)
    server_software: str = ""
    active_years: str = ""
    site_topic: str = ""
    naming_conventions: list = field(default_factory=list)
    file_extensions_seen: set = field(default_factory=set)
    predicted_paths: list = field(default_factory=list)
    confidence_scores: dict = field(default_factory=dict)


def _extract_structure(crawled_urls):
    """Extract the visible directory tree from a list of crawled URLs."""
    dirs = set()
    files = set()
    extensions = set()
    conventions = []

    for url in crawled_urls:
        parsed = urlparse(url)
        path = parsed.path.rstrip("/")
        if not path:
            continue

        parts = path.split("/")
        # Build directory tree
        for i in range(1, len(parts)):
            dirs.add("/".join(parts[:i]) + "/")

        # Track files and extensions
        filename = parts[-1] if parts else ""
        if "." in filename:
            ext = filename.rsplit(".", 1)[-1].lower()
            extensions.add(ext)
            files.add(path)

    # Detect naming conventions
    filenames = [url.split("/")[-1] for url in crawled_urls if "." in url.split("/")[-1]]

    # Sequential numbering
    numbered = [f for f in filenames if re.search(r'\d{2,}', f)]
    if len(numbered) > 2:
        conventions.append(f"sequential_numbering (e.g., {numbered[0]})")

    # Date-based dirs
    date_dirs = [d for d in dirs if re.search(r'/(19|20)\d{2}/', d)]
    if date_dirs:
        conventions.append(f"date_directories (e.g., {date_dirs[0]})")

    # Underscore vs hyphen vs camelCase
    underscored = [f for f in filenames if "_" in f]
    hyphenated = [f for f in filenames if "-" in f]
    if len(underscored) > len(hyphenated):
        conventions.append("underscore_naming")
    elif len(hyphenated) > len(underscored):
        conventions.append("hyphen_naming")

    return sorted(dirs), sorted(files), extensions, conventions


def _detect_server_software(html_samples):
    """Guess the server software from HTML content clues."""
    clues = []
    combined = " ".join(html_samples[:5])[:50000]

    if "FrontPage" in combined or "vti_cnf" in combined or "_vti_bin" in combined:
        clues.append("Microsoft FrontPage")
    if "generator" in combined.lower() and "dreamweaver" in combined.lower():
        clues.append("Macromedia Dreamweaver")
    if "wp-content" in combined or "wordpress" in combined.lower():
        clues.append("WordPress")
    if "phpBB" in combined:
        clues.append("phpBB Forum")
    if "vBulletin" in combined:
        clues.append("vBulletin Forum")
    if "Powered by" in combined:
        match = re.search(r'Powered by\s+([A-Za-z0-9\s]+)', combined)
        if match:
            clues.append(match.group(1).strip()[:30])
    if "<meta name=\"generator\"" in combined.lower():
        match = re.search(r'<meta\s+name=["\']generator["\']\s+content=["\']([^"\']+)', combined, re.I)
        if match:
            clues.append(match.group(1).strip()[:40])

    return ", ".join(clues) if clues else "Unknown (likely hand-coded HTML)"


def _detect_site_topic(crawled_urls, html_samples):
    """Infer the site's topic from URLs and content."""
    combined_urls = " ".join(crawled_urls).lower()
    combined_html = " ".join(html_samples[:3])[:20000].lower()
    all_text = combined_urls + " " + combined_html

    topic_signals = {
        "music": ["mp3", "audio", "music", "song", "album", "track", "band", "guitar", "midi", "remix"],
        "gaming": ["game", "rom", "wad", "doom", "quake", "mod", "level", "map", "cheat", "walkthrough"],
        "warez/piracy": ["crack", "keygen", "serial", "warez", "appz", "gamez", "0day"],
        "demoscene": ["demo", "scene", "intro", "prod", "party", "compo", "pouet"],
        "anime/manga": ["anime", "manga", "fansub", "episode", "ova", "dubbed"],
        "web hosting/personal": ["guestbook", "visitor", "counter", "webring", "geocities"],
        "forum/community": ["forum", "thread", "post", "member", "board", "bulletin"],
        "software/tools": ["download", "software", "tool", "util", "app", "program", "freeware", "shareware"],
        "educational": ["course", "lecture", "tutorial", "lesson", "homework", "syllabus"],
        "art/graphics": ["gallery", "artwork", "render", "texture", "wallpaper", "graphic"],
    }

    scores = {}
    for topic, keywords in topic_signals.items():
        score = sum(1 for kw in keywords if kw in all_text)
        if score > 0:
            scores[topic] = score

    if not scores:
        return "general/unknown"

    sorted_topics = sorted(scores.items(), key=lambda x: -x[1])
    return sorted_topics[0][0]


def build_profile(domain, crawled_urls, html_samples=None):
    """Build a MindReader profile from crawled data."""
    html_samples = html_samples or []

    dirs, files, extensions, conventions = _extract_structure(crawled_urls)
    server = _detect_server_software(html_samples)
    topic = _detect_site_topic(crawled_urls, html_samples)

    # Guess active years from URLs
    years = set()
    for url in crawled_urls:
        for match in re.finditer(r'(19|20)\d{2}', url):
            years.add(match.group())

    profile = MindReaderProfile(
        domain=domain,
        visible_paths=[d for d in dirs[:50]],
        server_software=server,
        active_years=f"{min(years)}-{max(years)}" if years else "unknown",
        site_topic=topic,
        naming_conventions=conventions,
        file_extensions_seen=extensions,
    )

    return profile


def generate_predictions_with_llm(profile, api_key=None):
    """
    Use Claude to generate predicted hidden paths based on the webmaster profile.

    Returns a list of dicts: [{"path": "/hidden/dir/", "confidence": 0.8, "reasoning": "..."}]
    """
    try:
        import anthropic
    except ImportError:
        console.print("[yellow]anthropic package not installed — using heuristic fallback[/yellow]")
        return generate_predictions_heuristic(profile)

    if not api_key:
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        console.print("[yellow]No ANTHROPIC_API_KEY — using heuristic fallback[/yellow]")
        return generate_predictions_heuristic(profile)

    client = anthropic.Anthropic(api_key=api_key)

    prompt = f"""You are simulating the mind of a webmaster who ran {profile.domain} during {profile.active_years}.

KNOWN FACTS:
- Server software: {profile.server_software}
- Site topic: {profile.site_topic}
- File extensions seen: {', '.join(sorted(profile.file_extensions_seen))}
- Naming conventions: {', '.join(profile.naming_conventions) or 'none detected'}
- Visible directory structure:
{chr(10).join('  ' + p for p in profile.visible_paths[:30])}

TASK: Think like this webmaster. Based on the era, the server software, and the site's purpose, predict the most likely HIDDEN directories and files that exist but aren't linked from the visible pages.

Consider:
- Server-default directories (cgi-bin, _vti_cnf, wp-admin, etc.)
- Common backup patterns (.bak, ~, .old, .orig)
- Admin/config paths typical for that era
- Content that matches the site topic but isn't linked
- Test/staging directories webmasters commonly create
- Upload directories that might have user content

Return ONLY a JSON array. Each element: {{"path": "/predicted/path/", "confidence": 0.0-1.0, "reasoning": "brief explanation"}}

Generate 15-25 predictions, sorted by confidence (highest first). Only include paths with confidence >= 0.3."""

    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )

        text = resp.content[0].text.strip()
        # Extract JSON from response
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()

        predictions = json.loads(text)
        profile.predicted_paths = predictions
        return predictions

    except Exception as e:
        console.print(f"[yellow]LLM prediction failed ({e}) — using heuristic fallback[/yellow]")
        return generate_predictions_heuristic(profile)


def generate_predictions_heuristic(profile):
    """
    Rule-based fallback when LLM isn't available.
    Uses era-appropriate server knowledge to generate predictions.
    """
    predictions = []
    domain = profile.domain
    topic = profile.site_topic
    server = profile.server_software.lower()
    extensions = profile.file_extensions_seen

    # Server-specific paths
    if "frontpage" in server:
        for path in ["/_vti_cnf/", "/_vti_pvt/", "/_vti_log/", "/_vti_txt/",
                      "/_vti_bin/", "/_private/", "/images/", "/cgi-bin/"]:
            predictions.append({"path": path, "confidence": 0.8, "reasoning": "FrontPage server extension directory"})

    if "wordpress" in server:
        for path in ["/wp-content/uploads/", "/wp-includes/", "/wp-admin/",
                      "/wp-content/backup/", "/wp-content/cache/"]:
            predictions.append({"path": path, "confidence": 0.7, "reasoning": "WordPress default directory"})

    if "phpbb" in server or "vbulletin" in server:
        for path in ["/attachments/", "/uploads/", "/customavatars/", "/backup/"]:
            predictions.append({"path": path, "confidence": 0.7, "reasoning": "Forum software upload directory"})

    # Universal patterns
    universal = [
        ("/backup/", 0.5, "Common backup directory"),
        ("/old/", 0.4, "Common archive of old content"),
        ("/test/", 0.4, "Development/testing directory"),
        ("/temp/", 0.35, "Temporary files"),
        ("/admin/", 0.4, "Admin panel"),
        ("/cgi-bin/", 0.5, "CGI scripts directory"),
        ("/data/", 0.45, "Data storage"),
        ("/files/", 0.5, "Generic file storage"),
        ("/upload/", 0.45, "Upload directory"),
        ("/uploads/", 0.45, "Upload directory (plural)"),
        ("/media/", 0.5, "Media files"),
        ("/assets/", 0.4, "Static assets"),
        ("/archive/", 0.45, "Archived content"),
        ("/downloads/", 0.5, "Downloads section"),
        ("/private/", 0.35, "Private content (often misconfigured)"),
    ]
    for path, conf, reason in universal:
        predictions.append({"path": path, "confidence": conf, "reasoning": reason})

    # Topic-specific paths
    if topic == "music":
        for path in ["/mp3/", "/audio/", "/music/", "/tracks/", "/albums/", "/mixes/",
                      "/midi/", "/wav/", "/flac/", "/ogg/"]:
            predictions.append({"path": path, "confidence": 0.6, "reasoning": f"Music site — likely {path} directory"})

    elif topic == "gaming":
        for path in ["/wads/", "/maps/", "/mods/", "/roms/", "/saves/", "/patches/",
                      "/levels/", "/demos/", "/tools/", "/utils/"]:
            predictions.append({"path": path, "confidence": 0.6, "reasoning": f"Gaming site — likely {path} directory"})

    elif topic == "demoscene":
        for path in ["/prods/", "/demos/", "/intros/", "/music/", "/gfx/", "/code/",
                      "/party/", "/compo/", "/releases/"]:
            predictions.append({"path": path, "confidence": 0.6, "reasoning": f"Demoscene site — likely {path} directory"})

    elif topic == "software/tools":
        for path in ["/releases/", "/bin/", "/dist/", "/src/", "/doc/", "/patches/"]:
            predictions.append({"path": path, "confidence": 0.55, "reasoning": f"Software site — likely {path} directory"})

    # Sequential predictions based on visible structure
    for visible_dir in profile.visible_paths:
        # Year-based: if /2003/ exists, try adjacent years
        year_match = re.search(r'/(19|20)(\d{2})/', visible_dir)
        if year_match:
            base_year = int(year_match.group(1) + year_match.group(2))
            for delta in [-2, -1, 1, 2]:
                predicted_year = base_year + delta
                predicted = visible_dir.replace(str(base_year), str(predicted_year))
                predictions.append({
                    "path": predicted,
                    "confidence": 0.6,
                    "reasoning": f"Adjacent year to {base_year}"
                })

    # Backup file predictions
    for ext in extensions:
        if ext in ("html", "htm", "php", "asp"):
            predictions.append({
                "path": f"/index.{ext}.bak",
                "confidence": 0.35,
                "reasoning": f"Backup of index file"
            })
            predictions.append({
                "path": f"/index.{ext}~",
                "confidence": 0.3,
                "reasoning": f"Editor backup of index file"
            })

    # Deduplicate and sort by confidence
    seen = set()
    unique = []
    for p in predictions:
        if p["path"] not in seen:
            seen.add(p["path"])
            unique.append(p)

    unique.sort(key=lambda x: -x["confidence"])
    profile.predicted_paths = unique
    return unique


def display_predictions(predictions, domain=""):
    """Pretty-print predictions as a confidence-ranked table."""
    table = Table(title=f"MindReader Predictions{' — ' + domain if domain else ''}", box=box.ROUNDED)
    table.add_column("Conf", justify="right", width=6)
    table.add_column("Predicted Path", style="cyan", max_width=45)
    table.add_column("Reasoning", style="dim", max_width=50)

    for pred in predictions[:30]:
        conf = pred["confidence"]
        style = "green" if conf >= 0.7 else "yellow" if conf >= 0.5 else "dim"
        bar = "█" * int(conf * 10) + "░" * (10 - int(conf * 10))
        table.add_row(
            f"[{style}]{bar}[/{style}]",
            pred["path"],
            pred["reasoning"][:50],
        )

    console.print(table)
    if len(predictions) > 30:
        console.print(f"  [dim]...and {len(predictions) - 30} more predictions[/dim]")

    high_conf = [p for p in predictions if p["confidence"] >= 0.6]
    console.print(f"\n  [green]{len(high_conf)}[/green] high-confidence predictions (≥0.6)")
    console.print(f"  [yellow]{len(predictions)}[/yellow] total predictions to verify")


def build_verification_urls(domain, predictions, base_timestamp="20050101000000"):
    """Convert predictions to Wayback Machine URLs for verification."""
    urls = []
    for pred in predictions:
        path = pred["path"]
        if not path.startswith("/"):
            path = "/" + path
        original = f"http://{domain}{path}"
        wayback_url = f"https://web.archive.org/web/{base_timestamp}id_/{original}"
        urls.append({
            "original": original,
            "wayback_url": wayback_url,
            "confidence": pred["confidence"],
            "reasoning": pred["reasoning"],
        })
    return urls


# ─── CLI ──────────────────────────────────────────────────────────────────────


def main():
    import argparse
    from InquirerPy import inquirer

    parser = argparse.ArgumentParser(description="GhostCrawl MindReader — LLM Webmaster Profiler")
    parser.add_argument("domain", nargs="?", default=None, help="Domain to profile (interactive if omitted)")
    parser.add_argument("--urls-file", help="File with crawled URLs (one per line)")
    parser.add_argument("--html-dir", help="Directory with saved HTML files for analysis")
    parser.add_argument("--heuristic-only", action="store_true", help="Skip LLM, use heuristic predictions only")
    args = parser.parse_args()

    console.print(f"\n[bold yellow]GHOSTCRAWL MINDREADER[/bold yellow]")

    domain = args.domain
    if not domain:
        # Interactive domain selection
        while True:
            mode = inquirer.select(
                message="How to pick a target?",
                choices=[
                    {"name": "Browse curated domain list", "value": "browse"},
                    {"name": "Enter a domain manually", "value": "manual"},
                    {"name": "Back / Exit", "value": "exit"},
                ],
            ).execute()

            if mode == "exit":
                return

            if mode == "browse":
                try:
                    from ghostcrawl import mode_browse_targets
                    domain = mode_browse_targets()
                    if not domain:
                        continue
                except ImportError:
                    console.print("[red]Could not import browse targets. Enter domain manually.[/red]")
                    continue
            else:
                domain = inquirer.text(message="Domain to profile (e.g. deadsite.com):").execute()
                if not domain.strip():
                    continue

            domain = domain.strip().replace("http://", "").replace("https://", "").rstrip("/")
            break

    console.print(f"[dim]Profiling the webmaster of[/dim] [cyan]{domain}[/cyan]\n")

    # Load crawled URLs
    crawled_urls = []
    if args.urls_file:
        with open(args.urls_file, encoding='utf-8') as f:
            crawled_urls = [line.strip() for line in f if line.strip()]
    else:
        # Quick CDX query to get visible structure
        console.print("[dim]No URL file provided — querying Wayback CDX for visible pages...[/dim]")
        try:
            import requests
            params = {
                "url": domain, "matchType": "domain", "output": "json",
                "limit": 200, "fl": "original", "collapse": "urlkey",
            }
            resp = requests.get("https://web.archive.org/cdx/search/cdx", params=params, timeout=20)
            if resp.status_code == 200:
                data = resp.json()
                if len(data) > 1:
                    crawled_urls = [row[0] for row in data[1:]]
        except Exception as e:
            console.print(f"[red]CDX query failed: {e}[/red]")

    if not crawled_urls:
        console.print("[red]No URLs to analyze. Provide --urls-file or check the domain.[/red]")
        return

    console.print(f"  Analyzing [green]{len(crawled_urls)}[/green] known URLs...")

    # Load HTML samples if available
    html_samples = []
    if args.html_dir and os.path.isdir(args.html_dir):
        for fname in os.listdir(args.html_dir)[:10]:
            fpath = os.path.join(args.html_dir, fname)
            if os.path.isfile(fpath):
                with open(fpath, errors="replace") as f:
                    html_samples.append(f.read()[:10000])

    # Build profile
    profile = build_profile(domain, crawled_urls, html_samples)

    console.print(f"\n[bold]Webmaster Profile:[/bold]")
    console.print(f"  Server: [cyan]{profile.server_software}[/cyan]")
    console.print(f"  Active: [cyan]{profile.active_years}[/cyan]")
    console.print(f"  Topic:  [cyan]{profile.site_topic}[/cyan]")
    console.print(f"  Naming: [cyan]{', '.join(profile.naming_conventions) or 'no clear pattern'}[/cyan]")
    console.print(f"  Extensions seen: [cyan]{', '.join(sorted(profile.file_extensions_seen)[:15])}[/cyan]")
    console.print(f"  Visible dirs: [cyan]{len(profile.visible_paths)}[/cyan]\n")

    # Generate predictions
    if args.heuristic_only:
        predictions = generate_predictions_heuristic(profile)
    else:
        predictions = generate_predictions_with_llm(profile)

    display_predictions(predictions, domain)

    # Build verification URLs
    verification = build_verification_urls(domain, predictions)
    console.print(f"\n  [dim]Run these {len(verification)} URLs through the Wayback Machine to verify.[/dim]")
    console.print(f"  [dim]High-confidence paths should be checked first.[/dim]")


if __name__ == "__main__":
    main()
