#!/usr/bin/env python3
"""
Ghostlight - Lost File Recovery Engine

A digital archaeology tool for finding and recovering files from the
Wayback Machine, archive.today, and Common Crawl. Fills the gap between
"the CDX API exists" and "I found lost media."

Features:
  - Domain archaeology: scan any domain for archived files by type
  - Dead link recovery: batch-check dead URLs across multiple archives
  - File type hunting: find specific file types across archived domains
  - Defunct host excavation: pre-configured searches for dead services
  - Smart adaptive rate limiting (60 req/min CDX limit)
  - File signature verification (magic bytes)
  - Persistent download history
  - Interactive Rich TUI

Usage:
  python ghostlight.py
"""

import requests
import json
import re
import os
import sys
import time
import hashlib
import struct
import argparse
from urllib.parse import quote, unquote, urlparse
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from collections import defaultdict

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, DownloadColumn, TransferSpeedColumn, TimeRemainingColumn
from rich.prompt import Prompt, Confirm
from rich.text import Text
from rich.rule import Rule
from rich import box
from rich.live import Live
from rich.layout import Layout
from rich.tree import Tree

from InquirerPy import inquirer
from InquirerPy.separator import Separator

console = Console()

# ═══════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════

DEFAULT_DEST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads")
HISTORY_FILE = os.path.join(os.path.dirname(__file__), ".ghostlight_history.json")
CDX_ENDPOINT = "https://web.archive.org/cdx/search/cdx"
WAYBACK_BASE = "https://web.archive.org/web"
ARCHIVE_TODAY = "https://archive.org/wayback/available"
HEADERS = {"User-Agent": "Ghostlight/1.0 (digital archaeology tool; contact: ghostlight@example.com)"}

# Adaptive rate limiting
MIN_DELAY = 1.0       # Minimum seconds between CDX requests
MAX_DELAY = 10.0      # Maximum backoff
BACKOFF_FACTOR = 2.0  # Multiply delay by this on 429
MAX_CDX_RESULTS = 150000

# ═══════════════════════════════════════════════════════════════════
# FILE TYPE PRESETS
# ═══════════════════════════════════════════════════════════════════

FILE_PRESETS = {
    "Audio": {
        "extensions": ["mp3", "flac", "wav", "ogg", "aac", "m4a", "wma", "opus", "aiff"],
        "mimetypes": ["audio/mpeg", "audio/flac", "audio/wav", "audio/ogg", "audio/aac",
                       "audio/mp4", "audio/x-ms-wma", "audio/opus", "audio/aiff"],
    },
    "Video": {
        "extensions": ["mp4", "avi", "mkv", "wmv", "flv", "mov", "webm", "m4v", "mpg", "mpeg"],
        "mimetypes": ["video/mp4", "video/x-msvideo", "video/x-matroska", "video/x-ms-wmv",
                       "video/x-flv", "video/quicktime", "video/webm", "video/mpeg"],
    },
    "Archives": {
        "extensions": ["zip", "rar", "7z", "tar.gz", "tgz", "bz2", "tar", "gz", "xz"],
        "mimetypes": ["application/zip", "application/x-rar-compressed", "application/x-7z-compressed",
                       "application/gzip", "application/x-tar", "application/x-bzip2"],
    },
    "Documents": {
        "extensions": ["pdf", "doc", "docx", "xls", "xlsx", "pptx", "ppt", "epub", "rtf", "odt"],
        "mimetypes": ["application/pdf", "application/msword", "application/vnd.ms-excel",
                       "application/vnd.ms-powerpoint", "application/epub+zip"],
    },
    "Software": {
        "extensions": ["exe", "msi", "dmg", "apk", "iso", "deb", "rpm", "appimage"],
        "mimetypes": ["application/x-msdownload", "application/x-apple-diskimage",
                       "application/vnd.android.package-archive", "application/x-iso9660-image"],
    },
    "Images": {
        "extensions": ["png", "jpg", "jpeg", "gif", "bmp", "svg", "tiff", "webp", "psd", "ico"],
        "mimetypes": ["image/png", "image/jpeg", "image/gif", "image/bmp", "image/svg+xml",
                       "image/tiff", "image/webp"],
    },
    "Data": {
        "extensions": ["json", "xml", "csv", "sql", "db", "sqlite", "yaml", "yml", "toml"],
        "mimetypes": ["application/json", "text/xml", "text/csv", "application/sql"],
    },
    "Everything interesting": {
        "extensions": ["mp3", "flac", "wav", "ogg", "mp4", "avi", "mkv", "zip", "rar", "7z",
                        "tar.gz", "pdf", "doc", "exe", "iso", "apk", "epub", "psd", "sql", "db"],
        "mimetypes": [],  # Use extension matching for the combo
    },
}

# ═══════════════════════════════════════════════════════════════════
# DEFUNCT HOST DATABASE
# ═══════════════════════════════════════════════════════════════════

DEFUNCT_HOSTS = {
    # File hosting
    "Megaupload": {"domain": "megaupload.com", "patterns": ["megaupload.com/?d=*"], "died": "Jan 2012", "note": "FBI seizure. 50M daily users."},
    "RapidShare": {"domain": "rapidshare.com", "patterns": ["rapidshare.com/files/*"], "died": "Mar 2015", "note": "Revenue collapsed post-Megaupload."},
    "Zippyshare": {"domain": "zippyshare.com", "patterns": ["zippyshare.com/v/*"], "died": "Mar 2023", "note": "17 years. Ad-blocking killed revenue."},
    "Hotfile": {"domain": "hotfile.com", "patterns": ["hotfile.com/dl/*"], "died": "Dec 2013", "note": "MPAA lawsuit, $80M settlement."},
    "FileSonic": {"domain": "filesonic.com", "patterns": ["filesonic.com/file/*"], "died": "Aug 2012", "note": "Post-Megaupload panic shutdown."},
    "FileServe": {"domain": "fileserve.com", "patterns": ["fileserve.com/file/*"], "died": "2012", "note": "Same cascade."},
    "Sendspace": {"domain": "sendspace.com", "patterns": ["sendspace.com/file/*"], "died": "Limping", "note": "Barely functional."},
    "4shared": {"domain": "4shared.com", "patterns": ["4shared.com/file/*", "4shared.com/audio/*", "4shared.com/video/*"], "died": "Purging", "note": "Aggressive content removal."},

    # Web hosting
    "GeoCities": {"domain": "geocities.com", "patterns": ["geocities.com/*/*"], "died": "Oct 2009", "note": "38M websites. Biggest loss in web history."},
    "Angelfire": {"domain": "angelfire.com", "patterns": ["angelfire.com/*/*"], "died": "Free tier dead", "note": "Lycos-owned."},
    "Tripod": {"domain": "tripod.com", "patterns": ["members.tripod.com/*"], "died": "Free tier dead", "note": "Lycos-owned."},
    "FortuneCity": {"domain": "fortunecity.com", "patterns": ["fortunecity.com/*/*"], "died": "Defunct", "note": "Archive Team partial save."},

    # Social / Content
    "MySpace (Music)": {"domain": "myspace.com", "patterns": ["myspace.com/*/music"], "died": "Data lost ~2019", "note": "50M songs lost. 490k recovered."},
    "Vine": {"domain": "vine.co", "patterns": ["vine.co/v/*"], "died": "Jan 2017", "note": "6-second videos. Partial Archive Team save."},

    # Forums
    "InvisionFree": {"domain": "invisionfree.com", "patterns": ["*.invisionfree.com/*"], "died": "Migrated/dead", "note": "600k+ boards since 2002."},
    "EZBoard": {"domain": "ezboard.com", "patterns": ["*.ezboard.com/*"], "died": "Defunct", "note": "Multiple transitions, eventually died."},

    # Old software
    "Download.com (old)": {"domain": "download.com", "patterns": ["download.com/*/3000-*"], "died": "Changed format", "note": "Old direct download links dead."},
    "Tucows": {"domain": "tucows.com", "patterns": ["tucows.com/preview/*"], "died": "Changed format", "note": "Shareware archive."},
}

# ═══════════════════════════════════════════════════════════════════
# CURATED TARGET DATABASE - Interesting domains to search
# ═══════════════════════════════════════════════════════════════════

CURATED_TARGETS = {
    "Music Blogs (MP3 golden era)": [
        {"domain": "muxtape.com", "note": "Online mixtape platform. Users uploaded full MP3s. Shut down 2008 by RIAA.", "years": "2008", "status": "dead"},
        {"domain": "stereogum.com", "note": "Indie music blog with MP3 downloads. Still alive but changed format.", "years": "2002-present", "status": "changed"},
        {"domain": "pitchforkmedia.com", "note": "Early Pitchfork domain. Hosted MP3 downloads of reviewed tracks.", "years": "1995-2008", "status": "redirects to pitchfork.com"},
        {"domain": "fluxblog.org", "note": "Pioneering MP3 blog. One of the first to share music online.", "years": "2002-2014", "status": "dead"},
        {"domain": "said-the-gramophone.com", "note": "Canadian MP3 blog. Literary writing about music + free downloads.", "years": "2003-2020", "status": "dead"},
        {"domain": "gorilla-vs-bear.com", "note": "Influential indie MP3 blog from Dallas.", "years": "2005-present", "status": "changed"},
        {"domain": "discobelle.net", "note": "MP3 blog for electronic/disco. Free downloads.", "years": "2007-2015", "status": "dead"},
        {"domain": "thehypemachine.com", "note": "Aggregator of MP3 blogs. Indexed downloadable tracks.", "years": "2005-present", "status": "limping"},
        {"domain": "elbo.ws", "note": "Music blog aggregator. Similar to Hype Machine.", "years": "2006-2013", "status": "dead"},
        {"domain": "datpiff.com", "note": "Mixtape hosting. Full albums as free downloads.", "years": "2005-present", "status": "alive but changed"},
        {"domain": "hotnewhiphop.com", "note": "Hip-hop blog with mixtape downloads.", "years": "2008-present", "status": "alive"},
        {"domain": "livemixtapes.com", "note": "Hip-hop/R&B mixtape hosting.", "years": "2005-present", "status": "alive but purging"},
        {"domain": "hulkshare.com", "note": "Music-focused file sharing. Huge hip-hop scene.", "years": "2009-2016", "status": "dead"},
        {"domain": "ifyoumakeit.com", "note": "Punk/DIY music blog with free downloads and live videos.", "years": "2006-2019", "status": "dead"},
        {"domain": "daytrotter.com", "note": "Live session recordings. Artists recorded exclusive studio sessions for download.", "years": "2006-2017", "status": "dead (was Paste Studios)"},
    ],
    "Music Platforms (dead/changed)": [
        {"domain": "mp3.com", "note": "THE original music sharing site. 1.7M songs partially archived. Artist uploads.", "years": "1997-2003", "status": "dead (name recycled)"},
        {"domain": "garageband.com", "note": "Independent artist platform (before Apple stole the name).", "years": "1999-2010", "status": "dead"},
        {"domain": "iuma.com", "note": "Internet Underground Music Archive. First site for indie music online.", "years": "1993-2006", "status": "dead"},
        {"domain": "purevolume.com", "note": "Emo/pop-punk band hosting. Full song downloads.", "years": "2003-2018", "status": "dead"},
        {"domain": "virb.com", "note": "Artist website builder with music hosting.", "years": "2006-2018", "status": "dead"},
        {"domain": "imeem.com", "note": "Social music platform. Full song streaming + downloads.", "years": "2003-2009", "status": "dead"},
        {"domain": "last.fm", "note": "Had free MP3 download section. Still alive but downloads gone.", "years": "2002-present", "status": "changed"},
        {"domain": "myspace.com", "note": "50M songs lost in server migration. 490k recovered (Dragon Hoard).", "years": "2003-present", "status": "gutted"},
    ],
    "Comedy Sites": [
        {"domain": "aspecialthing.com", "note": "THE alt-comedy forum. Louis CK, Patton Oswalt posted. Ground zero for 2000s comedy.", "years": "2001-2013", "status": "dead"},
        {"domain": "wackbag.com", "note": "Opie & Anthony fan forum. Every appearance catalogued.", "years": "2000-present", "status": "ghost town"},
        {"domain": "sternfannetwork.com", "note": "Howard Stern fan board. 15 years of clip trading.", "years": "2000-2015", "status": "dead"},
        {"domain": "rooftopcomedy.com", "note": "6,500+ comedians recorded live. Acquired by Audible 2014.", "years": "2007-2014", "status": "dead (absorbed)"},
        {"domain": "superdeluxe.com", "note": "Turner/Adult Swim comedy platform. Tim Heidecker, Maria Bamford content.", "years": "2007-2008, 2015-2018", "status": "dead twice"},
        {"domain": "seeso.com", "note": "NBC comedy streaming. Had exclusive SNL seasons 6-29.", "years": "2016-2017", "status": "dead"},
        {"domain": "comedycentral.com", "note": "25 years of clips WIPED by Paramount in June 2024. Daily Show, Colbert, all gone.", "years": "1999-2024", "status": "wiped"},
        {"domain": "splitsider.com", "note": "Best comedy journalism site. Had Second City archives. Absorbed into Vulture.", "years": "2010-2018", "status": "dead"},
        {"domain": "collegehumor.com", "note": "Dominant comedy video site. Gutted 2020. Became Dropout.", "years": "2006-2020", "status": "dead"},
        {"domain": "funnyordie.com", "note": "Will Ferrell's comedy video site. Multiple layoffs.", "years": "2007-present", "status": "gutted"},
        {"domain": "comedydynamics.com", "note": "Major comedy label. Old site versions had downloads.", "years": "2008-present", "status": "changed"},
        {"domain": "earwolf.com", "note": "Comedy podcast network. Became Stitcher Premium, then died.", "years": "2010-2023", "status": "absorbed/dead"},
        {"domain": "cookdandbombd.co.uk", "note": "UK comedy forum. Has bootleg recordings section.", "years": "2005-present", "status": "alive"},
    ],
    "Comedy Blogs (MP3 sharing)": [
        {"domain": "comedyalbum.blogspot.com", "note": "Comedy album sharing blog. Download links.", "years": "2008-2013", "status": "dormant"},
        {"domain": "stand-up-comedy-torrent.blogspot.com", "note": "Torrent index for comedy specials.", "years": "2009-2012", "status": "links dead"},
        {"domain": "classic-standup-comedy.blogspot.com", "note": "Classic standup recordings sharing.", "years": "2008-2014", "status": "dormant"},
        {"domain": "openmindsaturatedbrain.blogspot.com", "note": "Punk/comedy cross-genre sharing blog.", "years": "2008-2021", "status": "archived on IA"},
    ],
    "Comedy Archives (Internet Archive)": [
        {"domain": "archive.org/details/opieandanthonyarchive", "note": "Complete O&A radio show archive.", "years": "1995-2014", "status": "alive"},
        {"domain": "archive.org/details/toughcrowd", "note": "156 eps of Tough Crowd w/ Colin Quinn. NEVER officially released.", "years": "2002-2004", "status": "alive"},
        {"domain": "archive.org/details/Norm_Macdonald_Live", "note": "Complete Norm Macdonald Live. Pulled from YouTube 2018.", "years": "2013-2017", "status": "alive"},
        {"domain": "archive.org/details/patrice-o-neal-on-oa", "note": "Every Patrice O'Neal O&A appearance.", "years": "2001-2011", "status": "alive"},
        {"domain": "archive.org/details/ron-fez", "note": "Years of Ron & Fez radio show.", "years": "2001-2015", "status": "alive"},
        {"domain": "archive.org/details/insomniac-with-dave-attell_202506", "note": "Complete Insomniac with Dave Attell series.", "years": "2001-2004", "status": "alive"},
        {"domain": "archive.org/details/sta-1673333957", "note": "Gilbert Gottfried podcast archive. 2.4GB.", "years": "2014-2022", "status": "alive"},
        {"domain": "archive.org/details/myspace_dragon_hoard_2010", "note": "490k MySpace songs recovered. 1.3TB.", "years": "2008-2010", "status": "alive"},
    ],
    "Software & Shareware": [
        {"domain": "tucows.com", "note": "Massive shareware archive. Fully mirrored on archive.org.", "years": "1993-present", "status": "changed"},
        {"domain": "nonags.com", "note": "Freeware-only download site.", "years": "1998-2015", "status": "dead"},
        {"domain": "snapfiles.com", "note": "Curated freeware downloads.", "years": "2001-present", "status": "alive"},
        {"domain": "majorgeeks.com", "note": "Software download site. Survived the era.", "years": "2002-present", "status": "alive"},
        {"domain": "oldversion.com", "note": "Old versions of software. Invaluable.", "years": "2004-present", "status": "alive"},
        {"domain": "filehippo.com", "note": "Software downloads with version history.", "years": "2004-present", "status": "alive"},
        {"domain": "simtel.net", "note": "Mirror network for DOS/Windows shareware. Massive.", "years": "1982-2010", "status": "dead"},
        {"domain": "winsite.com", "note": "Windows software archive.", "years": "1995-2015", "status": "dead"},
        {"domain": "download.com", "note": "CNET download portal. Old versions had direct exe links.", "years": "1996-present", "status": "changed"},
    ],
    "Gaming Sites": [
        {"domain": "fileplanet.com", "note": "Game mods, patches, demos. Massive archive.", "years": "1997-2016", "status": "dead"},
        {"domain": "gamefront.com", "note": "Game file hosting. Mods, maps, patches.", "years": "2000-2016", "status": "dead"},
        {"domain": "moddb.com", "note": "Game mod hosting. Still alive.", "years": "2002-present", "status": "alive"},
        {"domain": "newgrounds.com", "note": "Flash games and animations. Survived the Flash apocalypse.", "years": "1999-present", "status": "alive"},
        {"domain": "kongregate.com", "note": "Flash gaming portal. Games still playable via Ruffle.", "years": "2006-2020", "status": "limping"},
        {"domain": "homeoftheunderdogs.net", "note": "Abandonware. Huge library of old games.", "years": "1998-2009", "status": "dead"},
        {"domain": "abandonia.com", "note": "DOS game abandonware archive.", "years": "1999-present", "status": "alive"},
        {"domain": "dosgamesarchive.com", "note": "DOS game downloads.", "years": "1999-present", "status": "alive"},
    ],
    "Old Web Hosts (personal sites)": [
        {"domain": "geocities.com", "note": "38 MILLION personal websites. Biggest web loss ever. 900GB saved by Archive Team.", "years": "1994-2009", "status": "dead"},
        {"domain": "angelfire.com", "note": "Free web hosting. Many music/art pages.", "years": "1996-present", "status": "free tier dead"},
        {"domain": "tripod.com", "note": "Free web hosting. Band pages, personal sites.", "years": "1995-present", "status": "free tier dead"},
        {"domain": "fortunecity.com", "note": "Free web host. Community-organized like GeoCities.", "years": "1997-2012", "status": "dead"},
        {"domain": "freewebs.com", "note": "Free website builder. Became Webs.com.", "years": "2001-present", "status": "changed"},
        {"domain": "homestead.com", "note": "Website builder with free tier.", "years": "1996-2007", "status": "merged"},
        {"domain": "xoom.com", "note": "Free web hosting with 25MB space.", "years": "1996-2001", "status": "dead"},
    ],
    "Forums & Communities": [
        {"domain": "invisionfree.com", "note": "600k+ free forum boards. Music, gaming, everything.", "years": "2002-2015", "status": "dead"},
        {"domain": "ezboard.com", "note": "Free forum hosting. Millions of boards.", "years": "1999-2011", "status": "dead"},
        {"domain": "delphi.com", "note": "Delphi Forums. One of the oldest online communities.", "years": "1983-2020", "status": "dead"},
        {"domain": "boardhost.com", "note": "Simple free forum hosting.", "years": "1998-present", "status": "alive"},
    ],
    "Ebooks & Documents": [
        {"domain": "library.nu", "note": "Massive ebook library. 400k+ books. Shut down by publishers.", "years": "2009-2012", "status": "dead"},
        {"domain": "gigapedia.com", "note": "Earlier name for library.nu. Academic ebooks.", "years": "2005-2009", "status": "dead"},
        {"domain": "textz.com", "note": "Experimental text sharing. Theory, philosophy, art texts.", "years": "2002-2007", "status": "dead"},
        {"domain": "scribd.com", "note": "Document hosting. Used to be fully free.", "years": "2007-present", "status": "paywalled"},
    ],
    "Blog Networks (defunct)": [
        {"domain": "theawl.com", "note": "Smart cultural writing. Spawned Splitsider, The Hairpin.", "years": "2009-2018", "status": "dead"},
        {"domain": "thehairpin.com", "note": "Women's culture blog. Part of The Awl network.", "years": "2010-2018", "status": "dead"},
        {"domain": "gawker.com", "note": "Gossip/media blog. Killed by Hulk Hogan lawsuit.", "years": "2002-2016", "status": "dead"},
        {"domain": "suck.com", "note": "Early web culture writing. Pioneering internet criticism.", "years": "1995-2001", "status": "dead"},
        {"domain": "salon.com", "note": "Early web magazine. Changed radically.", "years": "1995-present", "status": "changed"},
    ],
    "Demoscene / Tracker Music": [
        {"domain": "scene.org", "note": "Demoscene file archive. MOD/XM/IT tracker music.", "years": "1996-present", "status": "alive"},
        {"domain": "modarchive.org", "note": "Massive tracker music archive. 300k+ modules.", "years": "1997-present", "status": "alive"},
        {"domain": "pouet.net", "note": "Demoscene productions database.", "years": "2000-present", "status": "alive"},
        {"domain": "aminet.net", "note": "Amiga software/music archive.", "years": "1992-present", "status": "alive"},
        {"domain": "traxinspace.com", "note": "Tracker music community.", "years": "1998-2007", "status": "dead"},
        {"domain": "keygenmusic.net", "note": "Keygen music archive. Chiptune art of crack soundtracks.", "years": "2005-present", "status": "alive"},
    ],
    "Crypto / Bitcoin (2009-2016)": [
        {"domain": "mtgox.com", "note": "Largest BTC exchange. Hacked, 850K BTC lost. THE crypto disaster.", "years": "2010-2014", "status": "dead"},
        {"domain": "btc-e.com", "note": "Shady exchange. Seized by FBI. $4B laundered.", "years": "2011-2017", "status": "dead (seized)"},
        {"domain": "cryptsy.com", "note": "Altcoin exchange. Founder stole 13K BTC + 300K LTC.", "years": "2013-2016", "status": "dead"},
        {"domain": "bitcoinica.com", "note": "BTC margin trading. Hacked twice.", "years": "2011-2012", "status": "dead"},
        {"domain": "bitfloor.com", "note": "NYC exchange. 24K BTC stolen from unencrypted backup.", "years": "2012-2013", "status": "dead"},
        {"domain": "tradehill.com", "note": "#2 BTC exchange after Mt. Gox.", "years": "2011-2013", "status": "dead"},
        {"domain": "localbitcoins.com", "note": "P2P local trading. 29K daily trades at peak.", "years": "2012-2023", "status": "dead"},
        {"domain": "freebitcoins.appspot.com", "note": "Gavin Andresen's original BTC faucet. Gave away 19,700 BTC.", "years": "2010-2011", "status": "dead"},
        {"domain": "multibit.org", "note": "Popular lightweight wallet. Many users lost funds when abandoned.", "years": "2011-2017", "status": "dead"},
        {"domain": "brainwallet.org", "note": "Brain wallet generator. Notoriously insecure, wallets swept.", "years": "2011-2015", "status": "dead"},
        {"domain": "blockexplorer.com", "note": "First-ever Bitcoin block explorer. By Theymos.", "years": "2010-2019", "status": "dead"},
        {"domain": "deepbit.net", "note": "Dominant mining pool. Had 45% hashrate.", "years": "2011-2013", "status": "dead"},
        {"domain": "btcguild.com", "note": "Major pool, nearly 50% hashrate.", "years": "2011-2015", "status": "dead"},
        {"domain": "satoshidice.com", "note": "On-chain dice. Was 50%+ of ALL BTC transactions.", "years": "2012-present", "status": "changed"},
        {"domain": "just-dice.com", "note": "BTC dice game. $2B+ in wagers.", "years": "2013-2016", "status": "dead"},
        {"domain": "namecoin.org", "note": "First altcoin. Decentralized DNS (.bit domains).", "years": "2011-present", "status": "barely alive"},
        {"domain": "anoncoin.net", "note": "One of the first privacy coins. I2P integration.", "years": "2013-2018", "status": "dead"},
    ],
    "Warez / Scene": [
        {"domain": "astalavista.box.sk", "note": "THE warez search engine of the 90s/2000s.", "years": "1994-2021", "status": "dead"},
        {"domain": "crackz.ws", "note": "Crack/serial/keygen search and download.", "years": "1999-2010", "status": "dead"},
        {"domain": "gamecopyworld.com", "note": "Game no-CD cracks and patches. Still alive somehow.", "years": "1998-present", "status": "alive"},
        {"domain": "defacto2.net", "note": "Premier scene history archive. NFOs, cracktros. 500+ cracktros in browser.", "years": "2000-present", "status": "alive"},
        {"domain": "nfohump.com", "note": "Scene NFO file archive.", "years": "2006-2015", "status": "dead"},
        {"domain": "vcdquality.com", "note": "Video scene release database.", "years": "2002-present", "status": "barely alive"},
        {"domain": "shroo.ms", "note": "Massive NFO collections.", "years": "2003-2010", "status": "dead"},
        {"domain": "slyck.com", "note": "P2P/filesharing news and forums.", "years": "2001-2016", "status": "dead"},
    ],
    "Hacker / Security / Zines": [
        {"domain": "phrack.org", "note": "Premier hacker e-zine. 71 issues since 1985.", "years": "1985-present", "status": "alive"},
        {"domain": "2600.com", "note": "The Hacker Quarterly. Founded by Emmanuel Goldstein.", "years": "1984-present", "status": "alive"},
        {"domain": "textfiles.com", "note": "100K+ BBS text files. THE definitive BBS archive.", "years": "1998-present", "status": "alive"},
        {"domain": "totse.com", "note": "Temple of the Full Moon. Underground texts. Notorious.", "years": "1989-2009", "status": "dead"},
        {"domain": "w00w00.org", "note": "Security group. Members founded WhatsApp and Napster.", "years": "1996-2005", "status": "dead"},
        {"domain": "cultdeadcow.com", "note": "Cult of the Dead Cow. Created Back Orifice.", "years": "1984-present", "status": "alive"},
        {"domain": "l0pht.com", "note": "L0pht Heavy Industries. Testified before Congress 1998.", "years": "1992-2000", "status": "dead"},
        {"domain": "milw0rm.com", "note": "Exploit database. Inherited by Exploit-DB.", "years": "2004-2010", "status": "dead"},
        {"domain": "packetstormsecurity.com", "note": "Security tools, exploits, advisories since 1998.", "years": "1998-present", "status": "alive"},
        {"domain": "osvdb.org", "note": "Open Source Vulnerability Database.", "years": "2004-2016", "status": "dead"},
        {"domain": "preterhuman.net", "note": "Higher Intellect document archive.", "years": "2000-present", "status": "alive"},
    ],
    "Video Platforms (dead)": [
        {"domain": "video.google.com", "note": "Google Video. Before YouTube acquisition.", "years": "2005-2012", "status": "dead"},
        {"domain": "stage6.com", "note": "DivX HD video sharing. First HD video platform.", "years": "2006-2008", "status": "dead"},
        {"domain": "metacafe.com", "note": "Short-form video. 50M+ monthly visitors.", "years": "2003-2021", "status": "dead"},
        {"domain": "blip.tv", "note": "Web series hosting. Killed after Disney acquisition.", "years": "2005-2015", "status": "dead"},
        {"domain": "veoh.com", "note": "Long-form video. No length limits.", "years": "2005-2017", "status": "dead"},
        {"domain": "justin.tv", "note": "Live streaming. Became Twitch.", "years": "2007-2014", "status": "dead (became Twitch)"},
        {"domain": "ustream.tv", "note": "Live streaming. Became IBM Watson Media.", "years": "2007-2019", "status": "dead"},
        {"domain": "stickam.com", "note": "Early live streaming and chat.", "years": "2005-2013", "status": "dead"},
        {"domain": "vid.me", "note": "YouTube alternative. Domain later hijacked.", "years": "2014-2017", "status": "dead"},
        {"domain": "revver.com", "note": "First to share ad revenue with uploaders.", "years": "2005-2011", "status": "dead"},
    ],
    "Academic / Research": [
        {"domain": "library.nu", "note": "400K+ ebooks. Largest shadow library before LibGen.", "years": "2009-2012", "status": "dead"},
        {"domain": "gigapedia.com", "note": "Earlier name for library.nu. Academic ebooks.", "years": "2005-2009", "status": "dead"},
        {"domain": "textz.com", "note": "Experimental text sharing. Theory, philosophy, art.", "years": "2002-2007", "status": "dead"},
        {"domain": "monoskop.org", "note": "Media art and humanities archive.", "years": "2004-present", "status": "alive"},
        {"domain": "openculture.com", "note": "Free courses, books, audio. Massive index.", "years": "2006-present", "status": "alive"},
    ],
    "P2P / File Sharing": [
        {"domain": "napster.com", "note": "Started it all. 80M users at peak.", "years": "1999-2001", "status": "dead (name recycled)"},
        {"domain": "limewire.com", "note": "On 1/3 of all PCs worldwide.", "years": "2000-2010", "status": "dead"},
        {"domain": "kazaa.com", "note": "Settled with RIAA for $100M.", "years": "2001-2006", "status": "dead"},
        {"domain": "oink.cd", "note": "OiNK's Pink Palace. Best music tracker ever. 180K members.", "years": "2004-2007", "status": "dead (raided)"},
        {"domain": "what.cd", "note": "Successor to OiNK. Internet's largest curated music database.", "years": "2007-2016", "status": "dead (seized)"},
        {"domain": "demonoid.com", "note": "Semi-private tracker. Founder died in accident 2018.", "years": "2003-2018", "status": "dead"},
        {"domain": "isohunt.com", "note": "Torrent search. Settled with MPAA for $110M.", "years": "2003-2013", "status": "dead"},
    ],
    "Radio / Audio Archives": [
        {"domain": "etree.org", "note": "Live music recording trading. Legal bootleg community.", "years": "2000-present", "status": "alive"},
        {"domain": "archive.org/details/etree", "note": "100K+ live concert recordings.", "years": "2000-present", "status": "alive"},
        {"domain": "archive.org/details/GratefulDead", "note": "16K+ Grateful Dead live recordings.", "years": "1965-1995", "status": "alive"},
        {"domain": "live365.com", "note": "Internet radio platform. Thousands of stations.", "years": "1999-2016", "status": "dead (relaunched)"},
        {"domain": "shoutcast.com", "note": "Internet radio directory and streaming.", "years": "1998-present", "status": "alive but changed"},
        {"domain": "accuradio.com", "note": "Curated internet radio channels.", "years": "2000-present", "status": "alive"},
    ],
    "Underground / Zines / DIY": [
        {"domain": "zinelibrary.info", "note": "Digital zine archive. Anarchist, punk, DIY culture.", "years": "2004-present", "status": "alive"},
        {"domain": "crimethinc.com", "note": "Anarchist collective. Free zines, books, media.", "years": "1996-present", "status": "alive"},
        {"domain": "indymedia.org", "note": "Independent media network. Global activist journalism.", "years": "1999-present", "status": "barely alive"},
        {"domain": "boingboing.net", "note": "Counterculture blog. Directory of wonderful things.", "years": "1988-present", "status": "alive"},
    ],
    "Government / Public Data": [
        {"domain": "data.gov", "note": "US open government data. 300K+ datasets. Periodically purged.", "years": "2009-present", "status": "alive but purged"},
        {"domain": "spaceflight.nasa.gov", "note": "NASA spaceflight history, photos, mission data.", "years": "1995-2020", "status": "dead (migrated)"},
        {"domain": "cryptome.org", "note": "Leaked/declassified government documents. Whistleblower archive.", "years": "1996-present", "status": "alive"},
        {"domain": "nsarchive.gwu.edu", "note": "National Security Archive. Declassified US docs.", "years": "1985-present", "status": "alive"},
        {"domain": "climate.gov", "note": "Climate data/research. Threatened with admin changes.", "years": "2010-present", "status": "at risk"},
        {"domain": "eot.us.archive.org", "note": "End of Term Archive. Gov sites preserved during transitions.", "years": "2008-present", "status": "alive"},
    ],
    "Art / Design / Creative": [
        {"domain": "cgtalk.com", "note": "CGSociety forums. Premier 3D/VFX community. Shutting down 2024.", "years": "1999-2024", "status": "dead"},
        {"domain": "conceptart.org", "note": "Concept art community. Millions of posts, tutorials.", "years": "2002-2020", "status": "dead"},
        {"domain": "worth1000.com", "note": "Photoshop contest site. Incredible manipulations.", "years": "2002-2014", "status": "dead (became DesignCrowd)"},
        {"domain": "elfwood.com", "note": "Fantasy/sci-fi art community. 25K+ artists.", "years": "1996-2016", "status": "dead"},
        {"domain": "3dbuzz.com", "note": "3D/game dev tutorials. Released 225GB archive FREE when dying.", "years": "2002-2020", "status": "dead (archive on IA)"},
        {"domain": "digitaltutors.com", "note": "3D/VFX tutorials. Acquired by Pluralsight.", "years": "2002-2016", "status": "dead (absorbed)"},
        {"domain": "sxc.hu", "note": "Stock.xchng. Free stock photos. 400K+ images.", "years": "2001-2014", "status": "dead (became FreeImages)"},
        {"domain": "freemusicarchive.org", "note": "Free Music Archive. CC-licensed music library.", "years": "2009-2019", "status": "dead (data on IA)"},
        {"domain": "opengameart.org", "note": "Free game art assets. Sprites, textures, sounds.", "years": "2009-present", "status": "alive"},
        {"domain": "openclipart.org", "note": "Free SVG clipart library.", "years": "2004-present", "status": "alive"},
        {"domain": "webmonkey.com", "note": "Wired's web development tutorial site.", "years": "1996-2012", "status": "dead"},
    ],
    "Photography / Image Hosting (dead)": [
        {"domain": "panoramio.com", "note": "Google's geo-tagged photo service. Millions of photos.", "years": "2005-2016", "status": "dead"},
        {"domain": "webshots.com", "note": "Photo sharing and wallpapers. Millions of uploads.", "years": "1999-2015", "status": "dead"},
        {"domain": "tinypic.com", "note": "Free image hosting. Broke millions of forum posts when dying.", "years": "2004-2019", "status": "dead"},
        {"domain": "twitpic.com", "note": "Twitter image hosting. 600M+ images.", "years": "2008-2014", "status": "dead (IA saved)"},
        {"domain": "fotolog.com", "note": "Photo blogging. 32M registered users.", "years": "2002-2016", "status": "dead"},
        {"domain": "indafoto.hu", "note": "Hungarian photo hosting. 13.6M photos lost April 2025.", "years": "2005-2025", "status": "dead"},
        {"domain": "gfycat.com", "note": "GIF/video hosting. Used across Reddit.", "years": "2013-2023", "status": "dead"},
        {"domain": "minus.com", "note": "Image hosting with 10GB free. Used heavily on Reddit.", "years": "2010-2015", "status": "dead"},
        {"domain": "eyeem.com", "note": "Photography marketplace/community. Dead Jan 2026.", "years": "2011-2026", "status": "dead"},
    ],
    "Tech / Startups (dead)": [
        {"domain": "programmableweb.com", "note": "API directory. 24K+ APIs catalogued. Dead 2023.", "years": "2005-2023", "status": "dead"},
        {"domain": "gigaom.com", "note": "Major tech blog. Went bankrupt.", "years": "2006-2015", "status": "dead (name revived)"},
        {"domain": "readwriteweb.com", "note": "Top tech blog. Sold and gutted.", "years": "2003-2015", "status": "dead"},
        {"domain": "about.com", "note": "Massive how-to site. Split into Dotdash brands.", "years": "1997-2017", "status": "dead (became Dotdash)"},
        {"domain": "squidoo.com", "note": "Seth Godin's lens-based content platform.", "years": "2005-2014", "status": "dead (merged HubPages)"},
        {"domain": "posterous.com", "note": "Simple blogging platform. Acquired by Twitter, killed.", "years": "2008-2013", "status": "dead"},
        {"domain": "path.com", "note": "Social network. 50M users. Sold for nothing.", "years": "2010-2018", "status": "dead"},
        {"domain": "alexa.com", "note": "Web traffic ranking. THE internet ranking site.", "years": "1996-2022", "status": "dead"},
        {"domain": "del.icio.us", "note": "Social bookmarking. Tagged link database. Pioneered tagging.", "years": "2003-2017", "status": "dead"},
        {"domain": "stumbleupon.com", "note": "Serendipitous content discovery. 40M users.", "years": "2001-2018", "status": "dead"},
        {"domain": "evolt.org", "note": "Web dev community. Had old browser archive (every IE, Netscape).", "years": "1998-2012", "status": "dead"},
        {"domain": "hotscripts.com", "note": "Script/code directory. PHP, Perl, JavaScript.", "years": "1998-2018", "status": "dead"},
    ],
    "University / Research Labs": [
        {"domain": "research.att.com", "note": "AT&T Research / Bell Labs. Computing history.", "years": "1990s-present", "status": "changed"},
        {"domain": "research.sun.com", "note": "Sun Microsystems Labs. Java, ZFS, Solaris.", "years": "1995-2010", "status": "dead (Oracle)"},
        {"domain": "labs.google.com", "note": "Google Labs. Experimental products before shutdown.", "years": "2002-2011", "status": "dead"},
        {"domain": "research.yahoo.com", "note": "Yahoo Research. Pioneering web science.", "years": "2005-2017", "status": "dead"},
        {"domain": "ftp.cse.ucsc.edu", "note": "UCSC CS dept FTP. Software, papers, datasets.", "years": "1990s-2010s", "status": "varies"},
        {"domain": "ai.mit.edu", "note": "MIT AI Lab. Historic CS research.", "years": "1959-2003", "status": "merged into CSAIL"},
    ],
    "Bootleg / Tape Trading": [
        {"domain": "etree.org", "note": "Live music trading. BitTorrent was literally written for this site.", "years": "2000-present", "status": "alive"},
        {"domain": "dimeadozen.org", "note": "Live music bootleg torrents. Strict quality standards.", "years": "2003-present", "status": "alive"},
        {"domain": "thetradersden.org", "note": "Lossless live music trading.", "years": "2004-present", "status": "alive"},
        {"domain": "shnflac.net", "note": "SHN/FLAC lossless bootleg community.", "years": "2002-2012", "status": "dead"},
        {"domain": "furthurnet.com", "note": "Grateful Dead tape trading network.", "years": "2001-2010", "status": "dead"},
    ],
    "Sound / Sample Libraries": [
        {"domain": "flashkit.com", "note": "Flash sound loops and effects. Loops archived on IA.", "years": "1999-2017", "status": "dead"},
        {"domain": "samplenet.co.uk", "note": "Free samples. Classified as lost media.", "years": "2000-2012", "status": "dead"},
        {"domain": "hammersound.net", "note": "Free SoundFont instruments. Partial archive on ibiblio.", "years": "1998-2010", "status": "dead"},
        {"domain": "freesound.org", "note": "CC-licensed sound effects. 500K+ sounds.", "years": "2005-present", "status": "alive"},
        {"domain": "soundsnap.com", "note": "Sound effects library. Was free, now paid.", "years": "2006-present", "status": "changed"},
    ],
    "Early Podcast Hosting": [
        {"domain": "odeo.com", "note": "Early podcast platform. Team pivoted to create TWITTER.", "years": "2005-2007", "status": "dead"},
        {"domain": "podshow.com", "note": "Adam Curry's podcast network. Became Mevio.", "years": "2005-2012", "status": "dead"},
        {"domain": "podcastalley.com", "note": "Early podcast directory.", "years": "2004-2015", "status": "dead"},
        {"domain": "hipcast.com", "note": "First commercial podcast host (was audioblog.com).", "years": "2003-2012", "status": "dead"},
    ],
    "Fanfiction / Literary Archives": [
        {"domain": "fictionalley.org", "note": "17 years of Harry Potter fanfic. Migrated to AO3.", "years": "2001-2018", "status": "dead"},
        {"domain": "onemanga.com", "note": "Manga reading. 1M+ monthly visitors.", "years": "2006-2010", "status": "dead"},
        {"domain": "smackjeeves.com", "note": "Webcomic hosting. Thousands of comics.", "years": "2005-2020", "status": "dead"},
        {"domain": "theforge.biz", "note": "Indie RPG design forum. Massively influential.", "years": "2001-2012", "status": "dead"},
    ],
    "Sacred / Religious Texts": [
        {"domain": "sacred-texts.com", "note": "1700+ books on religion, mythology, folklore. Treasure trove.", "years": "1997-present", "status": "alive"},
        {"domain": "gnosis.org", "note": "Gnostic texts, Nag Hammadi library.", "years": "1994-present", "status": "alive"},
        {"domain": "esotericarchives.com", "note": "Grimoires, occult manuscripts, hermetic texts.", "years": "2000-present", "status": "alive"},
    ],
    "Genealogy / Records": [
        {"domain": "rootsweb.com", "note": "Oldest genealogy community. WorldConnect trees DELETED 2023.", "years": "1993-2023", "status": "frozen/gutted"},
        {"domain": "ellisisland.org", "note": "Ellis Island passenger records. 65M arrivals.", "years": "2001-present", "status": "changed"},
        {"domain": "accessgenealogy.com", "note": "Free genealogy records, Native American records.", "years": "1999-present", "status": "alive"},
    ],
    "Misc Treasure Troves": [
        {"domain": "recipezaar.com", "note": "Recipe community. Became Food.com after $25M acquisition.", "years": "1999-2009", "status": "dead"},
        {"domain": "soar.berkeley.edu", "note": "UC Berkeley Usenet recipe archive.", "years": "1990s-2010s", "status": "dead"},
        {"domain": "drkoop.com", "note": "Former US Surgeon General's health site. Went bankrupt.", "years": "1998-2002", "status": "dead"},
        {"domain": "fortunecity.com", "note": "Free web host. 1.275M accounts archived before death.", "years": "1997-2012", "status": "dead"},
        {"domain": "tess.uspto.gov", "note": "USPTO trademark search. Retired Nov 2023 after 25 years.", "years": "1998-2023", "status": "dead"},
        {"domain": "everyspec.com", "note": "Military/defense specifications and standards.", "years": "2000-present", "status": "alive"},
    ],
    "Flash / Shockwave / Java Applets": [
        {"domain": "newgrounds.com", "note": "Flash games/animations archive. Still alive, preserved Flash.", "years": "1999-present", "status": "alive"},
        {"domain": "flashkit.com", "note": "Flash resources, tutorials, sound loops.", "years": "1999-2017", "status": "dead"},
        {"domain": "albinoblacksheep.com", "note": "Flash animation host. Classic viral content.", "years": "2000-present", "status": "alive"},
        {"domain": "miniclip.com", "note": "Flash game portal. Pivoted to mobile.", "years": "2001-present", "status": "changed"},
        {"domain": "stickdeath.com", "note": "Stick figure Flash animations.", "years": "2000-2005", "status": "dead"},
        {"domain": "homestarrunner.com", "note": "Iconic Flash cartoon. Strong Bad. Preserved.", "years": "2000-present", "status": "alive"},
        {"domain": "z0r.de", "note": "German flash loop portal. Thousands of loops.", "years": "2004-present", "status": "changed"},
        {"domain": "addictinggames.com", "note": "Flash game aggregator. Pivoted away.", "years": "2002-present", "status": "changed"},
    ],
    "Anime / Manga / Fansubs": [
        {"domain": "animesuki.com", "note": "Fansub tracker/listing. Major hub for fansub distribution.", "years": "2002-2015", "status": "dead"},
        {"domain": "dattebayo.com", "note": "Naruto fansub group. Legendary speed-subbers.", "years": "2004-2009", "status": "dead"},
        {"domain": "tokyotosho.info", "note": "Anime torrent tracker/index.", "years": "2005-present", "status": "changed"},
        {"domain": "bakabt.me", "note": "Private anime torrent tracker. Went private.", "years": "2003-present", "status": "changed"},
        {"domain": "mangafox.me", "note": "Manga reader. DMCA'd repeatedly.", "years": "2006-2018", "status": "dead"},
        {"domain": "onemanga.com", "note": "Manga reading. 1M+ monthly visitors.", "years": "2006-2010", "status": "dead"},
    ],
    "Maps / GIS / Geospatial": [
        {"domain": "multimap.com", "note": "UK mapping service. Acquired by Bing Maps.", "years": "1996-2010", "status": "dead"},
        {"domain": "mapquest.com", "note": "Original web mapping. Still alive but irrelevant.", "years": "1996-present", "status": "changed"},
        {"domain": "terraserver.microsoft.com", "note": "Microsoft satellite imagery. Shut down.", "years": "1998-2012", "status": "dead"},
        {"domain": "flashearth.com", "note": "Multi-source satellite viewer.", "years": "2006-2018", "status": "dead"},
    ],
    "Encyclopedias / Reference (pre-Wikipedia)": [
        {"domain": "everything2.com", "note": "Collaborative encyclopedia. Predates Wikipedia.", "years": "1999-present", "status": "alive"},
        {"domain": "h2g2.com", "note": "BBC's Hitchhiker's Guide inspired encyclopedia.", "years": "1999-present", "status": "changed"},
        {"domain": "nupedia.com", "note": "Wikipedia's predecessor. Expert-written.", "years": "2000-2003", "status": "dead"},
        {"domain": "howstuffworks.com", "note": "Explainer articles. Acquired by Discovery.", "years": "1998-present", "status": "changed"},
        {"domain": "about.com", "note": "Expert guides on everything. Became Dotdash.", "years": "1997-2017", "status": "dead"},
    ],
    "Chat / IM / Social (dead)": [
        {"domain": "aol.com", "note": "AIM chat logs, member pages, hometown sites.", "years": "1993-2017", "status": "changed"},
        {"domain": "icq.com", "note": "ICQ messenger pages. User homepages.", "years": "1996-present", "status": "changed"},
        {"domain": "xanga.com", "note": "Blogging/social platform. 50M+ users at peak.", "years": "2000-2014", "status": "dead"},
        {"domain": "friendster.com", "note": "Pre-MySpace social network. 100M users.", "years": "2002-2015", "status": "dead"},
        {"domain": "orkut.com", "note": "Google's social network. Huge in Brazil.", "years": "2004-2014", "status": "dead"},
        {"domain": "bebo.com", "note": "Social network. Bought for $850M, died.", "years": "2005-2013", "status": "dead"},
    ],
    "Science Fiction / Fantasy Communities": [
        {"domain": "sff.net", "note": "SF&F author/fan community. Hosting, mailing lists.", "years": "1995-2018", "status": "dead"},
        {"domain": "scifi.com", "note": "Syfy channel old domain. Fan forums, content.", "years": "1995-2009", "status": "dead"},
        {"domain": "baen.com/library", "note": "Baen Free Library. Free ebook downloads.", "years": "1999-present", "status": "alive"},
        {"domain": "ansible.co.uk", "note": "Dave Langford's SF newsletter archive.", "years": "1979-present", "status": "alive"},
    ],
    "DIY / Maker / Electronics": [
        {"domain": "instructables.com", "note": "DIY project instructions. Acquired by Autodesk.", "years": "2005-present", "status": "alive"},
        {"domain": "makezine.com", "note": "Make Magazine online. Maker culture hub.", "years": "2005-present", "status": "changed"},
        {"domain": "hackaday.com", "note": "Hardware hacking blog.", "years": "2004-present", "status": "alive"},
        {"domain": "ladyada.net", "note": "Limor Fried's electronics projects. Became Adafruit.", "years": "2004-present", "status": "changed"},
    ],
    "Linguistics / Dictionaries / Language": [
        {"domain": "yourdictionary.com", "note": "Online dictionary and language resources.", "years": "1996-present", "status": "alive"},
        {"domain": "wordspy.com", "note": "New word tracker. Documented neologisms.", "years": "1998-present", "status": "changed"},
        {"domain": "takeourword.com", "note": "Etymology magazine.", "years": "1998-2007", "status": "dead"},
        {"domain": "worldwidewords.org", "note": "Michael Quinion's word origin investigations.", "years": "1996-2017", "status": "dead"},
    ],
    "Web Design / Development Archives": [
        {"domain": "webmonkey.com", "note": "Wired's web dev tutorial site.", "years": "1996-2013", "status": "dead"},
        {"domain": "alistapart.com", "note": "Web standards advocacy. CSS Zen Garden era.", "years": "1998-present", "status": "alive"},
        {"domain": "csszengarden.com", "note": "CSS design showcase. Iconic.", "years": "2003-present", "status": "alive"},
        {"domain": "k10k.net", "note": "Kaliber 10000. Design inspiration/news.", "years": "1998-2013", "status": "dead"},
        {"domain": "dynamicdrive.com", "note": "DHTML/JavaScript code library.", "years": "1998-present", "status": "changed"},
    ],
    "Banned / Quarantined Subreddits": [
        {"domain": "reddit.com/r/fatpeoplehate", "note": "150K subscribers. Banned June 2015 for harassment.", "years": "2013-2015", "status": "dead"},
        {"domain": "reddit.com/r/coontown", "note": "Racist subreddit. Banned August 2015.", "years": "2014-2015", "status": "dead"},
        {"domain": "reddit.com/r/jailbait", "note": "20K subscribers. Banned October 2011. CNN investigation.", "years": "2008-2011", "status": "dead"},
        {"domain": "reddit.com/r/the_donald", "note": "790K subscribers. Quarantined 2019, banned June 2020.", "years": "2015-2020", "status": "dead"},
        {"domain": "reddit.com/r/watchpeopledie", "note": "400K subscribers. Banned March 2019 after Christchurch.", "years": "2012-2019", "status": "dead"},
        {"domain": "reddit.com/r/incels", "note": "40K subscribers. Banned November 2017 for violence incitement.", "years": "2016-2017", "status": "dead"},
        {"domain": "reddit.com/r/deepfakes", "note": "90K subscribers. Banned February 2018.", "years": "2017-2018", "status": "dead"},
        {"domain": "reddit.com/r/shoplifting", "note": "Shoplifting tips/stories. Banned March 2018.", "years": "2014-2018", "status": "dead"},
        {"domain": "reddit.com/r/darknetmarkets", "note": "DNM discussion/reviews. Banned March 2018.", "years": "2013-2018", "status": "dead"},
        {"domain": "reddit.com/r/piracy", "note": "Massive piracy discussion hub. Quarantined, partially restored.", "years": "2008-present", "status": "changed"},
        {"domain": "reddit.com/r/megalinks", "note": "Direct Mega.nz links to pirated content. Banned April 2018.", "years": "2016-2018", "status": "dead"},
        {"domain": "reddit.com/r/gundeals", "note": "Firearms deals. Banned then reinstated after backlash.", "years": "2011-present", "status": "changed"},
        {"domain": "reddit.com/r/chapo_trap_house", "note": "Left-wing podcast sub. 160K users. Banned June 2020.", "years": "2017-2020", "status": "dead"},
        {"domain": "reddit.com/r/gendercritical", "note": "65K subscribers. Banned June 2020.", "years": "2013-2020", "status": "dead"},
        {"domain": "reddit.com/r/creepshots", "note": "Non-consensual photos. Banned October 2012.", "years": "2011-2012", "status": "dead"},
        {"domain": "reddit.com/r/pizzagate", "note": "Conspiracy sub. Banned November 2016.", "years": "2016", "status": "dead"},
        {"domain": "reddit.com/r/great_awakening", "note": "QAnon sub. 70K subscribers. Banned September 2018.", "years": "2018", "status": "dead"},
        {"domain": "reddit.com/r/opieandanthony", "note": "Radio show fan sub. Banned for targeted harassment.", "years": "2012-2019", "status": "dead"},
        {"domain": "reddit.com/r/sanctionedsuicide", "note": "Suicide discussion. Banned March 2018.", "years": "2014-2018", "status": "dead"},
        {"domain": "reddit.com/r/beatingwomen", "note": "Violence content. Banned June 2014.", "years": "2012-2014", "status": "dead"},
        {"domain": "reddit.com/r/milliondollarextreme", "note": "MDE fan sub. 30K users. Banned September 2018.", "years": "2015-2018", "status": "dead"},
        {"domain": "reddit.com/r/cringeanarchy", "note": "500K subscribers. Quarantined then banned April 2019.", "years": "2014-2019", "status": "dead"},
        {"domain": "reddit.com/r/frenworld", "note": "Baby-talk meme sub masking extremism. Banned June 2019.", "years": "2019", "status": "dead"},
        {"domain": "reddit.com/r/honkler", "note": "Clown meme sub. Banned June 2019.", "years": "2019", "status": "dead"},
        {"domain": "reddit.com/r/physical_removal", "note": "Anarcho-capitalist violence sub. Banned August 2017.", "years": "2016-2017", "status": "dead"},
        {"domain": "reddit.com/r/uncensorednews", "note": "100K subscribers. Alt-right news. Banned September 2018.", "years": "2016-2018", "status": "dead"},
        {"domain": "reddit.com/r/european", "note": "Far-right European politics. Quarantined then banned.", "years": "2014-2016", "status": "dead"},
        {"domain": "reddit.com/r/altright", "note": "Alt-right organizing hub. Banned February 2017.", "years": "2016-2017", "status": "dead"},
        {"domain": "reddit.com/r/braincels", "note": "Incel community successor. 50K users. Banned October 2019.", "years": "2017-2019", "status": "dead"},
        {"domain": "reddit.com/r/whitebeauty", "note": "White supremacist sub disguised as appreciation. Banned.", "years": "2015-2020", "status": "dead"},
        {"domain": "reddit.com/r/NaturalHair", "note": "Not the hair sub - banned for being a covert racist sub.", "years": "2017-2020", "status": "dead"},
        {"domain": "reddit.com/r/consumeproduct", "note": "Anti-consumerism masking antisemitism. 90K users. Banned June 2020.", "years": "2019-2020", "status": "dead"},
        {"domain": "reddit.com/r/smuggies", "note": "MS Paint meme sub. Banned June 2020.", "years": "2018-2020", "status": "dead"},
        {"domain": "reddit.com/r/wojak", "note": "Meme sub. Banned for hate content June 2020.", "years": "2019-2020", "status": "dead"},
        {"domain": "reddit.com/r/soyboys", "note": "Mocking sub. Banned June 2020.", "years": "2017-2020", "status": "dead"},
        {"domain": "reddit.com/r/gore", "note": "Graphic violence content. Banned 2018.", "years": "2008-2018", "status": "dead"},
        {"domain": "reddit.com/r/spacedicks", "note": "Shock content. One of Reddit's most infamous subs. Banned.", "years": "2010-2018", "status": "dead"},
        {"domain": "reddit.com/r/lolicons", "note": "Animated CSAM. Banned February 2012.", "years": "2010-2012", "status": "dead"},
        {"domain": "reddit.com/r/TheFappening", "note": "Leaked celeb photos. 100K users in days. Banned September 2014.", "years": "2014", "status": "dead"},
        {"domain": "reddit.com/r/CBTS_Stream", "note": "QAnon predecessor. 20K users. Banned March 2018.", "years": "2017-2018", "status": "dead"},
        {"domain": "reddit.com/r/NoNewNormal", "note": "COVID denial. 120K subscribers. Banned September 2021.", "years": "2020-2021", "status": "dead"},
        {"domain": "reddit.com/r/ivermectin", "note": "COVID misinformation. Quarantined September 2021.", "years": "2020-present", "status": "changed"},
        {"domain": "reddit.com/r/NNN", "note": "No New Normal offshoot. Banned.", "years": "2021", "status": "dead"},
        {"domain": "reddit.com/r/DarkNetMarketsNoobs", "note": "DNM beginner guide sub. Banned March 2018.", "years": "2014-2018", "status": "dead"},
        {"domain": "reddit.com/r/DNMSuperlist", "note": "Darknet market listings. Banned.", "years": "2015-2018", "status": "dead"},
        {"domain": "reddit.com/r/stealthy", "note": "Shoplifting successor sub. Banned quickly.", "years": "2018", "status": "dead"},
        {"domain": "reddit.com/r/hamptonbrandon", "note": "IRL streamer fan sub. Banned for brigading.", "years": "2018-2019", "status": "dead"},
        {"domain": "reddit.com/r/Ice_Poseidon2", "note": "IRL streamer drama sub. Banned 2019.", "years": "2019", "status": "dead"},
    ],
    "Archive.org Collections (direct)": [
        {"domain": "archive.org/details/tucows", "note": "Full Tucows mirror. 32K+ apps.", "years": "1993-2021", "status": "alive"},
        {"domain": "archive.org/details/mp3com2", "note": "MP3.com full archive. 1.7M songs.", "years": "1997-2003", "status": "alive"},
        {"domain": "archive.org/details/geocities", "note": "GeoCities archive. 38M websites.", "years": "1994-2009", "status": "alive"},
        {"domain": "archive.org/details/ftpsites", "note": "FTP Site Boneyard.", "years": "various", "status": "alive"},
        {"domain": "archive.org/details/software", "note": "General software collection.", "years": "various", "status": "alive"},
        {"domain": "archive.org/details/folksoundomy_comedy", "note": "Comedy audio archive.", "years": "various", "status": "alive"},
        {"domain": "archive.org/details/scenenotices", "note": "3M+ NFO files from warez scene (2000-2012).", "years": "2000-2012", "status": "alive"},
        {"domain": "archive.org/details/essential-keygen-music", "note": "Keygen music collection.", "years": "various", "status": "alive"},
        {"domain": "archive.org/details/stage6", "note": "Stage6 DivX video archive.", "years": "2006-2008", "status": "alive"},
        {"domain": "archive.org/details/bbshistory", "note": "BBS history collection.", "years": "various", "status": "alive"},
        {"domain": "archive.org/details/stand-up-comedy", "note": "Stand-up comedy collection.", "years": "various", "status": "alive"},
        {"domain": "archive.org/details/etree", "note": "100K+ live concert recordings.", "years": "various", "status": "alive"},
        {"domain": "archive.org/details/3dbuzz", "note": "3DBuzz 225GB tutorial archive. Released free on death.", "years": "2002-2020", "status": "alive"},
        {"domain": "archive.org/details/flashkit-sound-loops", "note": "FlashKit sound loops and effects.", "years": "1999-2017", "status": "alive"},
        {"domain": "archive.org/details/fortunecity", "note": "FortuneCity web host archive. 1.275M accounts.", "years": "1997-2012", "status": "alive"},
        {"domain": "archive.org/details/librivoxaudio", "note": "LibriVox public domain audiobooks.", "years": "2005-present", "status": "alive"},
        {"domain": "archive.org/details/cultdeadcow", "note": "Cult of the Dead Cow text files.", "years": "1984-present", "status": "alive"},
    ],
    "Retro Computing / Emulation": [
        {"domain": "emuparadise.me", "note": "Largest ROM site ever. Removed downloads 2018 due to Nintendo.", "years": "2000-2018", "status": "dead (direct links exist with workaround)"},
        {"domain": "coolrom.com", "note": "ROM downloads. Stripped most ROMs 2019.", "years": "2003-present", "status": "gutted"},
        {"domain": "romnation.net", "note": "ROM archive. Taken down.", "years": "2002-2012", "status": "dead"},
        {"domain": "zophar.net", "note": "Emulator and ROM community. Great resource docs.", "years": "1997-present", "status": "alive but stale"},
        {"domain": "nesdev.org", "note": "NES development wiki and forums.", "years": "2000-present", "status": "alive"},
        {"domain": "romhacking.net", "note": "ROM hacking community. Translations, hacks, utilities.", "years": "2002-present", "status": "alive"},
        {"domain": "snesmusic.org", "note": "SNES music archive. SPC files.", "years": "2000-present", "status": "alive"},
        {"domain": "vgmusic.com", "note": "Video game MIDI archive. 30K+ MIDIs.", "years": "1996-present", "status": "alive"},
        {"domain": "the-underdogs.info", "note": "Abandonware game archive. Mirror of Home of the Underdogs.", "years": "2005-2012", "status": "dead"},
        {"domain": "lemonamiga.com", "note": "Amiga game database and downloads.", "years": "2001-present", "status": "alive"},
        {"domain": "worldofspectrum.org", "note": "ZX Spectrum game archive. Comprehensive.", "years": "1995-present", "status": "alive"},
        {"domain": "atarimania.com", "note": "Atari game/software archive. Disk images.", "years": "2003-present", "status": "alive"},
        {"domain": "pouet.net", "note": "Demoscene productions database. Downloads.", "years": "2000-present", "status": "alive"},
    ],
    "Torrent Sites (historical)": [
        {"domain": "suprnova.org", "note": "First major torrent index. Shut down by MPAA.", "years": "2002-2004", "status": "dead"},
        {"domain": "mininova.org", "note": "Successor to Suprnova. 6M daily visitors.", "years": "2005-2010", "status": "dead"},
        {"domain": "torrentspy.com", "note": "Torrent search. $111M MPAA judgment.", "years": "2003-2008", "status": "dead"},
        {"domain": "btjunkie.org", "note": "Torrent aggregator. Voluntarily shut down.", "years": "2005-2012", "status": "dead"},
        {"domain": "kickasstorrents.to", "note": "KAT. Owner arrested in Poland.", "years": "2008-2016", "status": "dead"},
        {"domain": "torrentz.eu", "note": "Meta-search for torrents.", "years": "2003-2016", "status": "dead"},
        {"domain": "extratorrent.cc", "note": "Top 5 torrent site. Vanished overnight.", "years": "2006-2017", "status": "dead"},
        {"domain": "demonoid.com", "note": "Semi-private tracker. Founder died 2018.", "years": "2003-2018", "status": "dead"},
    ],
    "Open Directories & FTP Servers": [
        {"domain": "ftp.cdrom.com", "note": "Walnut Creek CD-ROM FTP. THE shareware source.", "years": "1991-2000", "status": "dead (mirrored on IA)"},
        {"domain": "ftp.funet.fi", "note": "Finnish university FTP. Linux, Amiga, more.", "years": "1990-present", "status": "alive"},
        {"domain": "ftp.scene.org", "note": "Demoscene file archive.", "years": "1998-present", "status": "alive"},
        {"domain": "metalab.unc.edu", "note": "UNC Metalab (was sunsite). Massive Linux/software archive.", "years": "1992-present", "status": "changed (ibiblio.org)"},
        {"domain": "ibiblio.org", "note": "Successor to sunsite/metalab. Free collections.", "years": "2000-present", "status": "alive"},
        {"domain": "the-eye.eu", "note": "Massive open directory. Mirrors of everything.", "years": "2017-present", "status": "alive"},
        {"domain": "ftp.hp.com", "note": "HP FTP. Old printer drivers, firmware, docs.", "years": "1990s-present", "status": "intermittent"},
    ],
    "Lost Media / Preservation Projects": [
        {"domain": "lostmediawiki.com", "note": "Wiki documenting lost media across all formats.", "years": "2012-present", "status": "alive"},
        {"domain": "archive.org/details/flash_gamelist", "note": "Flashpoint archive. 100K+ Flash games preserved.", "years": "2018-present", "status": "alive"},
        {"domain": "bluemaxima.org/flashpoint", "note": "Flashpoint project. Largest Flash preservation effort.", "years": "2018-present", "status": "alive"},
        {"domain": "myspleen.org", "note": "Private tracker for rare/lost TV, movies, VHS rips.", "years": "2004-present", "status": "alive (private)"},
        {"domain": "kinolibrary.com", "note": "Historical stock footage library.", "years": "2000-present", "status": "alive"},
        {"domain": "archive.org/details/prelinger", "note": "Prelinger Archives. 9000+ ephemeral films.", "years": "1983-present", "status": "alive"},
        {"domain": "ubuweb.com", "note": "Avant-garde art, film, poetry, music. All free.", "years": "1996-present", "status": "alive"},
        {"domain": "rfrm.nl", "note": "Reformatted. Lost media recovery community.", "years": "2020-present", "status": "alive"},
    ],
    "BBS Archives / Textfiles": [
        {"domain": "textfiles.com", "note": "Jason Scott's BBS text file archive. THE source.", "years": "1998-present", "status": "alive"},
        {"domain": "artscene.textfiles.com", "note": "ANSI/ASCII art archive from BBS era.", "years": "1998-present", "status": "alive"},
        {"domain": "bbsarchives.com", "note": "BBS software and file archives.", "years": "2000-2015", "status": "dead"},
        {"domain": "defacto2.net", "note": "Scene/warez group NFO file archive.", "years": "1996-present", "status": "alive"},
        {"domain": "sixteen-colors.net", "note": "ANSI art group archive. Pack releases.", "years": "2000-present", "status": "alive"},
    ],
    "Early Social Media / Web 2.0 Dead": [
        {"domain": "digg.com", "note": "Social news aggregator. Was THE front page of internet before Reddit.", "years": "2004-2012", "status": "dead (name recycled)"},
        {"domain": "technorati.com", "note": "Blog search engine. Tracked 100M+ blogs.", "years": "2002-2014", "status": "dead"},
        {"domain": "jaiku.com", "note": "Microblogging (acquired by Google, killed).", "years": "2006-2012", "status": "dead"},
        {"domain": "pownce.com", "note": "Microblogging by Digg founder. Short-lived.", "years": "2007-2008", "status": "dead"},
        {"domain": "spring.me", "note": "Was Formspring. Q&A social network.", "years": "2009-2015", "status": "dead"},
        {"domain": "app.net", "note": "Paid Twitter alternative. No ads. Died anyway.", "years": "2012-2017", "status": "dead"},
        {"domain": "ello.co", "note": "Ad-free social network. Pivoted to art platform.", "years": "2014-2023", "status": "dead"},
        {"domain": "google-plus.com", "note": "Google+. Had 500M users, still failed.", "years": "2011-2019", "status": "dead"},
    ],
    "Investigative / Leaks / Transparency": [
        {"domain": "cryptome.org", "note": "Leaked/declassified docs since 1996. Pre-WikiLeaks.", "years": "1996-present", "status": "alive"},
        {"domain": "wikileaks.org", "note": "Massive document leaks. Cable Gate, Iraq War Logs.", "years": "2006-present", "status": "alive"},
        {"domain": "pacer.gov", "note": "US federal court documents. RECAP project mirrors them free.", "years": "2001-present", "status": "alive"},
        {"domain": "courtlistener.com", "note": "Free legal opinion search. RECAP archive.", "years": "2010-present", "status": "alive"},
        {"domain": "documentcloud.org", "note": "Investigative journalism document platform.", "years": "2009-present", "status": "alive"},
        {"domain": "muckrock.com", "note": "FOIA request platform. Thousands of released documents.", "years": "2010-present", "status": "alive"},
        {"domain": "icij.org", "note": "Panama Papers, Paradise Papers, Pandora Papers.", "years": "1997-present", "status": "alive"},
    ],
    "Music Trackers / Private (historical)": [
        {"domain": "oink.cd", "note": "OiNK. Best music tracker ever. Raided by IFPI.", "years": "2004-2007", "status": "dead"},
        {"domain": "what.cd", "note": "Successor to OiNK. 3.3M torrents. Seized by French police.", "years": "2007-2016", "status": "dead"},
        {"domain": "waffles.ch", "note": "Music tracker. Ran alongside What.cd.", "years": "2007-2017", "status": "dead"},
        {"domain": "passtheheadphones.me", "note": "Early What.cd replacement attempt.", "years": "2016-2017", "status": "dead"},
        {"domain": "apollo.rip", "note": "What.cd successor. Defunct.", "years": "2016-2019", "status": "dead"},
    ],
    "Classic Web Directories & Search": [
        {"domain": "dmoz.org", "note": "Open Directory Project. Human-curated web directory.", "years": "1998-2017", "status": "dead (mirrored)"},
        {"domain": "yahoo.com/dir", "note": "Yahoo Directory. THE original web index.", "years": "1994-2014", "status": "dead"},
        {"domain": "lii.org", "note": "Librarians' Internet Index. Curated academic links.", "years": "1997-2015", "status": "dead"},
        {"domain": "altavista.com", "note": "THE search engine before Google.", "years": "1995-2013", "status": "dead"},
        {"domain": "ask.com", "note": "Ask Jeeves. Natural language search.", "years": "1996-present", "status": "changed"},
        {"domain": "hotbot.com", "note": "Wired's search engine. Powered by Inktomi.", "years": "1996-2002", "status": "dead"},
        {"domain": "excite.com", "note": "Search engine + portal. Turned down buying Google.", "years": "1995-2005", "status": "dead"},
        {"domain": "lycos.com", "note": "Search engine. 'Go get it!' Most visited site 1999.", "years": "1995-present", "status": "zombie"},
    ],
    "Vintage Video / TV Archives": [
        {"domain": "tvtorrents.com", "note": "Private TV torrent tracker. Comprehensive episode database.", "years": "2003-2013", "status": "dead"},
        {"domain": "btntv.com", "note": "BroadcasTheNet. Elite TV tracker.", "years": "2005-present", "status": "alive (private)"},
        {"domain": "mvgroup.org", "note": "Documentary torrent community.", "years": "2003-present", "status": "alive"},
        {"domain": "paleofuture.com", "note": "Retro-future blog. How past imagined future. Absorbed by Gizmodo.", "years": "2007-2019", "status": "dead"},
        {"domain": "archive.org/details/publicdomainmovies", "note": "Public domain feature films.", "years": "various", "status": "alive"},
        {"domain": "archive.org/details/classic_tv", "note": "Classic TV show collections on IA.", "years": "various", "status": "alive"},
    ],
    "Private Image Hosting (leaked/dead)": [
        {"domain": "photobucket.com", "note": "Millions of 'private' photos were publicly indexed. Many personal/intimate uploads found by Google. Broke the internet in 2017 when they killed 3rd party embedding.", "years": "2003-present", "status": "changed (paywalled)"},
        {"domain": "imageshack.com", "note": "Image hosting. Lost millions of images in 2014 redesign. 'Private' albums indexed.", "years": "2003-present", "status": "changed"},
        {"domain": "imgur.com", "note": "Image hosting. 'Private' unlinked uploads were findable via sequential IDs until 2023.", "years": "2009-present", "status": "changed"},
        {"domain": "postimage.org", "note": "Free image hosting. Minimal moderation. Commonly used for anonymous sharing.", "years": "2004-present", "status": "alive"},
        {"domain": "imgbb.com", "note": "Image hosting with auto-delete. Used for temporary sharing.", "years": "2014-present", "status": "alive"},
        {"domain": "turboimagehost.com", "note": "Free image hosting popular on forums. Lax moderation.", "years": "2006-2018", "status": "dead"},
        {"domain": "imgsrc.ru", "note": "Russian image hosting. Known for poorly moderated albums.", "years": "2006-present", "status": "changed"},
        {"domain": "imagetwist.com", "note": "Image hosting for forums. Paid model.", "years": "2010-present", "status": "alive"},
        {"domain": "acidimg.cc", "note": "Forum image host. No moderation.", "years": "2012-present", "status": "alive"},
        {"domain": "imgbox.com", "note": "Image hosting used by media sharing communities.", "years": "2010-present", "status": "alive"},
        {"domain": "flickr.com", "note": "Yahoo photo hosting. Millions of CC-licensed photos. Also hosted 'private' collections.", "years": "2004-present", "status": "changed"},
        {"domain": "snapchat.com", "note": "Ephemeral messaging. The Snappening (2014) leaked 200K images from 3rd party apps.", "years": "2011-present", "status": "alive"},
    ],
    "File Hosting Services (dead/seized)": [
        {"domain": "megaupload.com", "note": "Kim Dotcom's empire. Seized by FBI Jan 2012. 150M users, 50M daily.", "years": "2005-2012", "status": "dead (seized)"},
        {"domain": "rapidshare.com", "note": "Largest cyberlocker. Swiss. $32M revenue/yr at peak.", "years": "2002-2015", "status": "dead"},
        {"domain": "mediafire.com", "note": "File hosting with 'open' links. Many public folder links still work via Wayback.", "years": "2006-present", "status": "alive"},
        {"domain": "hotfile.com", "note": "File hosting. Lost $80M MPAA lawsuit.", "years": "2009-2013", "status": "dead"},
        {"domain": "filesonic.com", "note": "File hosting. Killed sharing after MegaUpload raid.", "years": "2010-2012", "status": "dead"},
        {"domain": "fileserve.com", "note": "File hosting. Disabled sharing after SOPA scare.", "years": "2010-2012", "status": "dead"},
        {"domain": "uploaded.net", "note": "German file hosting. Survived by operating from Switzerland.", "years": "2009-present", "status": "changed"},
        {"domain": "zippyshare.com", "note": "Free no-registration file hosting. Shut down March 2023.", "years": "2006-2023", "status": "dead"},
        {"domain": "depositfiles.com", "note": "Russian file hosting. Survived multiple legal threats.", "years": "2004-present", "status": "alive"},
        {"domain": "turbobit.net", "note": "Russian file hosting. Popular on warez forums.", "years": "2008-present", "status": "alive"},
        {"domain": "freakshare.com", "note": "German file hosting. Small but persistent.", "years": "2008-2020", "status": "dead"},
        {"domain": "netload.in", "note": "German file hosting. Shut down.", "years": "2006-2015", "status": "dead"},
        {"domain": "sendspace.com", "note": "Simple file sending service. 300MB free.", "years": "2005-present", "status": "alive"},
        {"domain": "bayfiles.com", "note": "Created by Pirate Bay founders.", "years": "2011-2022", "status": "dead"},
        {"domain": "anonfiles.com", "note": "Anonymous file hosting. No registration. Closed 2023.", "years": "2018-2023", "status": "dead"},
        {"domain": "gofile.io", "note": "Anonymous file hosting. Open server API.", "years": "2019-present", "status": "alive"},
        {"domain": "catbox.moe", "note": "Weeb-themed file hosting. 200MB limit. Anonymous.", "years": "2016-present", "status": "alive"},
        {"domain": "pixeldrain.com", "note": "Simple file/image hosting. Growing.", "years": "2018-present", "status": "alive"},
        {"domain": "1fichier.com", "note": "French file hosting. Survived most others.", "years": "2011-present", "status": "alive"},
        {"domain": "mega.nz", "note": "Kim Dotcom's successor to MegaUpload. E2E encrypted.", "years": "2013-present", "status": "alive"},
    ],
    "Leaked Databases / Breach Archives": [
        {"domain": "haveibeenpwned.com", "note": "Troy Hunt's breach notification service. Indexes billions of breached records.", "years": "2013-present", "status": "alive"},
        {"domain": "leakedsource.com", "note": "Search engine for stolen databases. Seized 2017.", "years": "2015-2017", "status": "dead (seized)"},
        {"domain": "dehashed.com", "note": "Breach search engine. Active.", "years": "2017-present", "status": "alive"},
        {"domain": "databases.today", "note": "Leaked database index. Multiple domain changes.", "years": "2019-present", "status": "changing"},
        {"domain": "raidforums.com", "note": "Database trading forum. Seized by FBI Feb 2022.", "years": "2015-2022", "status": "dead (seized)"},
        {"domain": "breachforums.is", "note": "RaidForums successor. Admin arrested March 2023. Kept reviving.", "years": "2022-present", "status": "changing"},
        {"domain": "nulled.to", "note": "Cracking/leaked accounts forum. Breached itself in 2016.", "years": "2015-present", "status": "alive"},
        {"domain": "cracked.to", "note": "Account cracking/combo list forum.", "years": "2018-present", "status": "alive"},
        {"domain": "ogusers.com", "note": "OG username trading/SIM swapping forum. Breached 2019.", "years": "2017-2021", "status": "dead"},
        {"domain": "mpgh.net", "note": "Multi Player Game Hacking forum. Game cheats + leaked databases.", "years": "2006-present", "status": "alive"},
    ],
    "Underground Forums / Darknet (clearnet archives)": [
        {"domain": "silkroad6ownowfk.onion", "note": "Silk Road. First major darknet market. Archived on Wayback.", "years": "2011-2013", "status": "dead (seized)"},
        {"domain": "hackforums.net", "note": "Notorious hacking/cracking forum. Breached multiple times.", "years": "2005-present", "status": "alive"},
        {"domain": "darkode.com", "note": "Elite cybercrime forum. FBI takedown 2015.", "years": "2009-2015", "status": "dead (seized)"},
        {"domain": "antichat.com", "note": "Russian hacking forum. One of the oldest.", "years": "2000-present", "status": "alive"},
        {"domain": "exploit.in", "note": "Russian exploit/vulnerability marketplace.", "years": "2005-present", "status": "alive"},
        {"domain": "xss.is", "note": "Russian cybercrime forum (was DaMaGeLaB).", "years": "2013-present", "status": "alive"},
        {"domain": "dread (onion)", "note": "Reddit-style forum for darknet markets. Clearnet mirrors exist.", "years": "2018-present", "status": "alive (Tor)"},
        {"domain": "the-eye.eu", "note": "Massive open directory with mirrors of breaches, leaks, and archives.", "years": "2017-present", "status": "alive"},
    ],
    "Usenet Archives / Groups": [
        {"domain": "groups.google.com", "note": "Google Groups (was DejaNews). 20+ years of Usenet archived. alt.binaries.* had massive file sharing.", "years": "2001-present", "status": "alive (gutted)"},
        {"domain": "dejanews.com", "note": "Original Usenet archive. 500M+ messages. Acquired by Google 2001.", "years": "1995-2001", "status": "dead (became Google Groups)"},
        {"domain": "newzbin.com", "note": "Usenet NZB index. First site ordered blocked by UK ISPs.", "years": "2003-2012", "status": "dead"},
        {"domain": "binsearch.info", "note": "Free Usenet binary search.", "years": "2006-present", "status": "alive"},
        {"domain": "nzbindex.com", "note": "NZB file search index.", "years": "2007-present", "status": "alive"},
        {"domain": "nzbking.com", "note": "Usenet NZB search engine.", "years": "2012-present", "status": "alive"},
        {"domain": "usenetarchives.com", "note": "Historical Usenet message archive.", "years": "2010-present", "status": "alive"},
    ],
    "Content Aggregators / Link Farms (dead)": [
        {"domain": "ebaums world.com", "note": "Content aggregation. Ripped Flash games, videos from everywhere.", "years": "2001-present", "status": "changed"},
        {"domain": "wimp.com", "note": "Viral video aggregator.", "years": "2008-present", "status": "changed"},
        {"domain": "break.com", "note": "Funny/viral video aggregator. 20M monthly visitors.", "years": "2006-2018", "status": "dead"},
        {"domain": "fazed.org", "note": "Shock/viral content aggregator.", "years": "2003-2015", "status": "dead"},
        {"domain": "heavyr.com", "note": "Extreme content aggregator.", "years": "2007-present", "status": "alive"},
        {"domain": "bestgore.com", "note": "Shock content. Owner Mark Marek arrested. Site seized.", "years": "2008-2020", "status": "dead (seized)"},
        {"domain": "documentingreality.com", "note": "Forum for shock/reality content.", "years": "2006-present", "status": "changed"},
        {"domain": "liveleak.com", "note": "Uncensored news/reality video. Became ItemFix.", "years": "2006-2021", "status": "dead"},
        {"domain": "ogrish.com", "note": "Shock content site. Became LiveLeak.", "years": "2000-2006", "status": "dead (became LiveLeak)"},
        {"domain": "rotten.com", "note": "OG shock site. Rotten Library was actually valuable.", "years": "1996-2017", "status": "dead"},
        {"domain": "somethingawful.com", "note": "Humor/community site. Forums birthed many internet subcultures.", "years": "1999-present", "status": "alive"},
    ],
}

# ═══════════════════════════════════════════════════════════════════
# MAGIC BYTES (file signature verification)
# ═══════════════════════════════════════════════════════════════════

MAGIC_BYTES = {
    b'\xff\xfb': 'mp3', b'\xff\xf3': 'mp3', b'\xff\xf2': 'mp3',
    b'ID3': 'mp3',
    b'fLaC': 'flac',
    b'RIFF': 'wav/avi',
    b'OggS': 'ogg',
    b'\x00\x00\x00\x18ftypmp4': 'mp4', b'\x00\x00\x00\x1cftypmp4': 'mp4',
    b'\x00\x00\x00\x20ftypmp4': 'mp4',
    b'\x1aE\xdf\xa3': 'mkv/webm',
    b'PK\x03\x04': 'zip/epub/docx/apk',
    b'Rar!\x1a\x07': 'rar',
    b'7z\xbc\xaf\x27\x1c': '7z',
    b'\x1f\x8b': 'gzip',
    b'%PDF': 'pdf',
    b'\x89PNG': 'png',
    b'\xff\xd8\xff': 'jpeg',
    b'GIF87a': 'gif', b'GIF89a': 'gif',
    b'MZ': 'exe/dll',
    b'\xd0\xcf\x11\xe0': 'doc/xls/ppt (OLE)',
}


# ═══════════════════════════════════════════════════════════════════
# CDX API CLIENT
# ═══════════════════════════════════════════════════════════════════

class CDXClient:
    """Multi-source CDX client using cdx_toolkit + raw API + Common Crawl."""

    def __init__(self):
        self.delay = MIN_DELAY
        self.last_request = 0
        self.total_requests = 0
        self.total_429s = 0
        # cdx_toolkit handles pagination, retries, and rate limiting internally
        try:
            import cdx_toolkit
            self.ia_fetcher = cdx_toolkit.CDXFetcher(source='ia')
            self.cc_fetcher = cdx_toolkit.CDXFetcher(source='cc')
            self.has_toolkit = True
        except Exception:
            self.ia_fetcher = None
            self.cc_fetcher = None
            self.has_toolkit = False

    def _throttle(self):
        elapsed = time.time() - self.last_request
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)
        self.last_request = time.time()
        self.total_requests += 1

    def _handle_429(self):
        self.total_429s += 1
        self.delay = min(self.delay * BACKOFF_FACTOR, MAX_DELAY)

    def _handle_success(self):
        self.delay = max(self.delay * 0.9, MIN_DELAY)

    def toolkit_search(self, url, extensions=None, limit=500):
        """Use cdx_toolkit for reliable paginated search. Handles timeouts internally."""
        if not self.has_toolkit:
            return []

        results = []
        clean_url = url.lstrip("*.") + "/*"

        # IA uses CDX-native filter syntax (no = prefix)
        ia_filters = ['statuscode:200', '!mimetype:text/html', '!mimetype:text/css',
                       '!mimetype:application/javascript', '!mimetype:text/javascript',
                       '!mimetype:image/gif', '!mimetype:image/jpeg', '!mimetype:image/png']
        # Common Crawl uses = prefix syntax
        cc_filters = ['=status:200', '!=mime:text/html', '!=mime:text/css',
                       '!=mime:application/javascript', '!=mime:text/javascript',
                       '!=mime:image/gif', '!=mime:image/jpeg', '!=mime:image/png']

        try:
            console.print(f"    [dim]cdx_toolkit: searching Wayback Machine...[/dim]")
            count = 0
            for capture in self.ia_fetcher.iter(clean_url, filter=ia_filters, limit=limit):
                rec = {
                    "timestamp": capture.data.get("timestamp", ""),
                    "original": capture.data.get("url", ""),
                    "mimetype": capture.data.get("mime", ""),
                    "statuscode": capture.data.get("status", ""),
                    "digest": capture.data.get("digest", ""),
                    "length": capture.data.get("length", "0"),
                }
                # Filter by extension if specified
                if extensions:
                    orig_lower = rec["original"].lower()
                    if any(orig_lower.endswith(f".{ext}") or f".{ext}?" in orig_lower for ext in extensions):
                        results.append(rec)
                        count += 1
                else:
                    results.append(rec)
                    count += 1

                if count >= limit:
                    break

            if results:
                console.print(f"    [dim]cdx_toolkit (Wayback): +{len(results)} files[/dim]")
        except Exception as e:
            console.print(f"    [dim]cdx_toolkit Wayback error: {str(e)[:60]}[/dim]")

        # Also try Common Crawl
        try:
            console.print(f"    [dim]cdx_toolkit: searching Common Crawl...[/dim]")
            cc_count = 0
            seen_digests = {r.get("digest") for r in results}
            for capture in self.cc_fetcher.iter(clean_url, filter=cc_filters, limit=min(limit, 100)):
                digest = capture.data.get("digest", "")
                if digest in seen_digests:
                    continue
                rec = {
                    "timestamp": capture.data.get("timestamp", ""),
                    "original": capture.data.get("url", ""),
                    "mimetype": capture.data.get("mime", ""),
                    "statuscode": capture.data.get("status", ""),
                    "digest": digest,
                    "length": capture.data.get("length", "0"),
                }
                if extensions:
                    orig_lower = rec["original"].lower()
                    if any(orig_lower.endswith(f".{ext}") or f".{ext}?" in orig_lower for ext in extensions):
                        results.append(rec)
                        seen_digests.add(digest)
                        cc_count += 1
                else:
                    results.append(rec)
                    seen_digests.add(digest)
                    cc_count += 1

                if cc_count >= min(limit, 100):
                    break

            if cc_count > 0:
                console.print(f"    [dim]cdx_toolkit (Common Crawl): +{cc_count} files[/dim]")
        except Exception as e:
            console.print(f"    [dim]Common Crawl: {str(e)[:60]}[/dim]")

        return results

    def smart_search(self, url, extensions=None, match_type="domain",
                     from_date=None, to_date=None, limit=10000):
        """
        Multi-strategy search that actually finds files.

        Key insights from testing:
        - CDX often records binaries as MIME type "unk" or "unknown"
        - Complex regex filters cause CDX server timeouts
        - Simple single-MIME queries are fastest and most reliable
        - matchType=domain on big sites times out; try host first

        Strategy order (fast/simple first, complex last):
          1. Single MIME type queries (one per type, fast)
          2. URL extension filter (catches unk MIME)
          3. Non-web-junk catch-all (everything that isn't HTML/CSS/JS/images)
        """
        all_results = {}

        # Clean URL - remove redundant *. prefix if matchType is domain
        clean_url = url.lstrip("*.")

        # Build per-MIME queries (simple, fast, reliable)
        ext_to_mime = {}
        for preset in FILE_PRESETS.values():
            for i, ext in enumerate(preset["extensions"]):
                if i < len(preset["mimetypes"]):
                    ext_to_mime[ext] = preset["mimetypes"][i]

        strategies = []

        if extensions:
            # Strategy 1: Individual MIME type queries (most reliable)
            unique_mimes = list(set(ext_to_mime.get(e) for e in extensions if e in ext_to_mime))
            for mime in unique_mimes[:8]:  # Cap at 8 most common to avoid excessive queries
                strategies.append((f"MIME:{mime.split('/')[-1]}", ["statuscode:200", f"mimetype:{re.escape(mime)}"]))

            # Strategy 2: URL extension filter (catches files with unk/unknown MIME)
            # Split into small batches to avoid complex regex timeouts
            for i in range(0, len(extensions), 5):
                batch = extensions[i:i+5]
                ext_pattern = build_extension_filter(batch)
                strategies.append((f"ext:.{'/.'.join(batch[:3])}...", ["statuscode:200", ext_pattern]))

        # Strategy 3: Non-web catch-all (last resort, broadest)
        strategies.append(("Non-web content", [
            "statuscode:200",
            "!mimetype:text/html",
            "!mimetype:text/css",
            "!mimetype:text/javascript",
            "!mimetype:application/javascript",
            "!mimetype:image/gif",
            "!mimetype:image/jpeg",
            "!mimetype:image/png",
        ]))

        # PRIMARY: Use cdx_toolkit (handles pagination, retries, timeouts)
        if self.has_toolkit:
            toolkit_results = self.toolkit_search(clean_url, extensions=extensions, limit=limit)
            for r in toolkit_results:
                key = r.get("digest", r.get("original", ""))
                if key not in all_results:
                    all_results[key] = r

        # FALLBACK: Raw CDX API if toolkit found nothing or isn't available
        if not all_results:
            console.print(f"    [dim]Trying raw CDX API...[/dim]")
            match_types_to_try = ["host", match_type] if match_type == "domain" else [match_type]

            for mt in match_types_to_try:
                for strategy_name, filters in strategies:
                    try:
                        results = self.search(
                            clean_url, match_type=mt, filters=filters,
                            collapse="digest", from_date=from_date, to_date=to_date,
                            limit=min(limit, 5000),
                        )
                        new_count = 0
                        for r in results:
                            key = r.get("digest", r.get("original", ""))
                            if key not in all_results:
                                all_results[key] = r
                                new_count += 1
                        if new_count > 0:
                            console.print(f"    [dim]{strategy_name}: +{new_count} files[/dim]")
                    except Exception:
                        pass
                    time.sleep(1.5)

                if all_results:
                    break

        return list(all_results.values())

    def search(self, url, match_type="domain", filters=None, collapse=None,
               from_date=None, to_date=None, limit=10000, fields=None):
        """
        Query the CDX API. Returns list of dicts.
        """
        self._throttle()

        params = {
            "url": url,
            "matchType": match_type,
            "output": "json",
            "limit": limit,
        }

        if fields:
            params["fl"] = ",".join(fields)
        else:
            params["fl"] = "timestamp,original,mimetype,statuscode,digest,length"

        if filters:
            params["filter"] = filters

        if collapse:
            params["collapse"] = collapse

        if from_date:
            params["from"] = str(from_date)
        if to_date:
            params["to"] = str(to_date)

        max_retries = 3
        for attempt in range(max_retries):
            try:
                resp = requests.get(CDX_ENDPOINT, params=params, headers=HEADERS, timeout=90)

                if resp.status_code == 429:
                    self._handle_429()
                    console.print(f"  [yellow]Rate limited. Backing off to {self.delay:.1f}s...[/yellow]")
                    time.sleep(self.delay)
                    continue

                resp.raise_for_status()
                self._handle_success()

                data = resp.json()
                if not data or len(data) < 2:
                    return []

                headers = data[0]
                results = []
                for row in data[1:]:
                    record = dict(zip(headers, row))
                    results.append(record)

                return results

            except requests.exceptions.ReadTimeout:
                wait = (attempt + 1) * 15
                if attempt < max_retries - 1:
                    console.print(f"  [yellow]CDX timeout (attempt {attempt+1}/{max_retries}), retrying in {wait}s...[/yellow]")
                    time.sleep(wait)
                else:
                    console.print(f"  [red]CDX timed out after {max_retries} attempts[/red]")
                    return []
            except requests.exceptions.JSONDecodeError:
                return []
            except Exception as e:
                console.print(f"  [red]CDX error: {e}[/red]")
                return []
        return []

    def search_archive_items(self, query, mediatype="", rows=25):
        """
        Search archive.org ITEMS (not Wayback CDX).
        This is a completely different endpoint that's usually much faster.
        Returns items with their file lists.
        """
        search_url = "https://archive.org/advancedsearch.php"
        q = f'("{query}")'
        if mediatype:
            q += f" AND mediatype:{mediatype}"

        params = {
            "q": q,
            "fl[]": ["identifier", "title", "mediatype", "downloads", "item_size"],
            "rows": rows,
            "output": "json",
            "sort[]": "downloads desc",
        }

        try:
            resp = requests.get(search_url, params=params, headers=HEADERS, timeout=30)
            resp.raise_for_status()
            docs = resp.json().get("response", {}).get("docs", [])
            return docs
        except Exception as e:
            console.print(f"    [dim]Archive.org search error: {e}[/dim]")
            return []

    def get_item_files(self, identifier, extensions=None):
        """
        Get all files in an archive.org item using the metadata API.
        Bypasses CDX entirely. Usually very fast.
        """
        url = f"https://archive.org/metadata/{identifier}/files"
        try:
            resp = requests.get(url, headers=HEADERS, timeout=30)
            resp.raise_for_status()
            files = resp.json().get("result", [])

            results = []
            for f in files:
                name = f.get("name", "")
                fmt = f.get("format", "")
                size = int(f.get("size", 0))

                # Filter by extension if specified
                if extensions:
                    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
                    if ext not in extensions:
                        continue

                # Skip metadata files
                if name.endswith("_meta.xml") or name.endswith("_files.xml") or name == "__ia_thumb.jpg":
                    continue

                dl_url = f"https://archive.org/download/{identifier}/{requests.utils.quote(name)}"
                results.append({
                    "title": name,
                    "url": dl_url,
                    "original": dl_url,
                    "timestamp": "",
                    "mimetype": fmt,
                    "statuscode": "200",
                    "digest": "",
                    "length": str(size),
                    "source": f"archive.org/{identifier}",
                })

            return results
        except Exception as e:
            console.print(f"    [dim]Metadata API error: {e}[/dim]")
            return []

    def check_availability(self, url, timestamp=None):
        """Check if a URL has any Wayback snapshot."""
        try:
            params = {"url": url}
            if timestamp:
                params["timestamp"] = str(timestamp)
            resp = requests.get(ARCHIVE_TODAY, params=params, headers=HEADERS, timeout=10)
            data = resp.json()
            snapshots = data.get("archived_snapshots", {})
            if snapshots.get("closest", {}).get("available"):
                return snapshots["closest"]
            return None
        except Exception:
            return None


# ═══════════════════════════════════════════════════════════════════
# DOWNLOAD HISTORY
# ═══════════════════════════════════════════════════════════════════

class DownloadHistory:
    def __init__(self, path=HISTORY_FILE):
        self.path = path
        self.data = self._load()

    def _load(self):
        if os.path.exists(self.path):
            with open(self.path, "r", encoding="utf-8") as f:
                return json.load(f)
        return {"downloaded": {}, "checked": {}, "stats": {"total_files": 0, "total_bytes": 0, "total_searches": 0}}

    def save(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2)

    def is_downloaded(self, url):
        return hashlib.md5(url.encode()).hexdigest() in self.data["downloaded"]

    def mark_downloaded(self, url, filename, size=0):
        key = hashlib.md5(url.encode()).hexdigest()
        self.data["downloaded"][key] = {"url": url, "filename": filename, "size": size, "date": datetime.now().isoformat()}
        self.data["stats"]["total_files"] += 1
        self.data["stats"]["total_bytes"] += size

    @property
    def count(self):
        return len(self.data["downloaded"])


# ═══════════════════════════════════════════════════════════════════
# FILE UTILITIES
# ═══════════════════════════════════════════════════════════════════

def sanitize_filename(name):
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    name = name.strip(". ")
    if len(name) > 200:
        base, ext = os.path.splitext(name)
        name = base[:196] + ext
    return name


def verify_magic_bytes(data):
    """Check file signature against known magic bytes."""
    for magic, filetype in MAGIC_BYTES.items():
        if data[:len(magic)] == magic:
            return filetype
    return "unknown"


def build_extension_filter(extensions):
    """Build a CDX filter regex for file extensions."""
    escaped = [re.escape(ext) for ext in extensions]
    return f"original:.*\\.({'|'.join(escaped)})$"


def build_mime_filter(mimetypes):
    """Build a CDX filter for MIME types."""
    return f"mimetype:{'|'.join(re.escape(m) for m in mimetypes)}"


def build_wayback_url(timestamp, original_url, raw=True):
    """Build a Wayback Machine download URL."""
    flag = "id_/" if raw else "/"
    return f"{WAYBACK_BASE}/{timestamp}{flag}{original_url}"


def download_wayback_file(url, dest_path, history, progress=None, task_id=None):
    """Download a file from the Wayback Machine."""
    if history.is_downloaded(url):
        return "skip", 0

    if os.path.exists(dest_path) and os.path.getsize(dest_path) > 1000:
        history.mark_downloaded(url, os.path.basename(dest_path), os.path.getsize(dest_path))
        return "skip", 0

    try:
        resp = requests.get(url, headers=HEADERS, stream=True, timeout=120)
        if resp.status_code == 429:
            time.sleep(5)
            return "rate_limited", 0
        resp.raise_for_status()

        total = int(resp.headers.get("content-length", 0))
        if progress and task_id and total:
            progress.update(task_id, total=total)

        downloaded = 0
        first_chunk_data = b""
        with open(dest_path, "wb") as f:
            for chunk in resp.iter_content(65536):
                f.write(chunk)
                downloaded += len(chunk)
                if progress and task_id:
                    progress.update(task_id, advance=len(chunk))
                if not first_chunk_data:
                    first_chunk_data = chunk[:20]

        # Verify it's not an HTML error page masquerading as a file
        if first_chunk_data and (b"<!DOCTYPE" in first_chunk_data or b"<html" in first_chunk_data or b"<HTML" in first_chunk_data):
            os.remove(dest_path)
            return "html_error", 0

        history.mark_downloaded(url, os.path.basename(dest_path), downloaded)
        return "downloaded", downloaded

    except Exception as e:
        if os.path.exists(dest_path):
            try:
                os.remove(dest_path)
            except OSError:
                pass
        return "failed", 0


# ═══════════════════════════════════════════════════════════════════
# INTERACTIVE UI
# ═══════════════════════════════════════════════════════════════════

BANNER = r"""
[bold yellow]
   _____ _               _   _ _       _     _
  / ____| |             | | | (_)     | |   | |
 | |  __| |__   ___  ___| |_| |_  __ _| |__ | |_
 | | |_ | '_ \ / _ \/ __| __| | |/ _` | '_ \| __|
 | |__| | | | | (_) \__ \ |_| | | (_| | | | | |_
  \_____|_| |_|\___/|___/\__|_|_|\__, |_| |_|\__|
                                  __/ |
                                 |___/
[/bold yellow]
[dim]Lost File Recovery Engine - Digital Archaeology[/dim]
[dim]Wayback Machine CDX | Archive.org | Common Crawl[/dim]
"""


def show_banner():
    console.print(BANNER)
    history = DownloadHistory()
    if history.count > 0:
        mb = history.data["stats"]["total_bytes"] / 1024 / 1024
        console.print(f"  [dim]History: {history.count} files recovered | {mb:.0f} MB[/dim]\n")


def main_menu():
    choices = [
        {"name": "Browse Targets       - Curated list of interesting domains to search", "value": "browse_targets"},
        {"name": "Domain Archaeology   - Scan a domain for archived files", "value": "domain_scan"},
        {"name": "File Type Hunt       - Search for specific file types on a domain", "value": "filetype_hunt"},
        {"name": "Defunct Host Dig      - Excavate dead file hosts (Megaupload, GeoCities...)", "value": "defunct_dig"},
        {"name": "Dead Link Recovery    - Batch-check dead URLs against archives", "value": "dead_links"},
        {"name": "Custom CDX Query      - Raw CDX API query builder", "value": "custom_cdx"},
        Separator(),
        {"name": "Recovery Stats        - View download history", "value": "stats"},
        {"name": "Exit", "value": "exit"},
    ]
    return inquirer.select(message="What would you like to excavate?", choices=choices, pointer=">").execute()


def pick_directory(default=DEFAULT_DEST):
    console.print(f"\n[bold]Output directory:[/bold] [cyan]{default}[/cyan]")
    change = inquirer.confirm(message="Change output directory?", default=False).execute()
    if change:
        new_dir = inquirer.text(message="Enter output directory:", default=default).execute()
        if new_dir.strip():
            return new_dir.strip()
    return default


def pick_file_types():
    """Let user select which file types to search for."""
    choices = [{"name": f"{name} ({', '.join(p['extensions'][:5])}...)", "value": name}
               for name, p in FILE_PRESETS.items()]
    selected = inquirer.checkbox(
        message="Select file types to hunt for (space to toggle):",
        choices=choices,
    ).execute()
    return selected


def pick_date_range():
    """Let user optionally set a date range."""
    use_dates = inquirer.confirm(message="Filter by date range?", default=False).execute()
    if not use_dates:
        return None, None

    from_date = inquirer.text(message="From year (e.g. 2005):", default="").execute()
    to_date = inquirer.text(message="To year (e.g. 2015):", default="").execute()
    return from_date or None, to_date or None


# ═══════════════════════════════════════════════════════════════════
# MODE: DOMAIN ARCHAEOLOGY
# ═══════════════════════════════════════════════════════════════════

def mode_browse_targets(dest_root):
    """Browse curated list of interesting domains to search."""
    # Pick category
    categories = list(CURATED_TARGETS.keys())
    cat_choices = [{"name": f"{cat} ({len(CURATED_TARGETS[cat])} sites)", "value": cat} for cat in categories]
    selected_cat = inquirer.select(message="Pick a category:", choices=cat_choices).execute()

    targets = CURATED_TARGETS[selected_cat]

    # Display the targets
    table = Table(title=selected_cat, box=box.ROUNDED)
    table.add_column("#", width=3)
    table.add_column("Domain", style="cyan", max_width=40)
    table.add_column("Years", width=12)
    table.add_column("Status", width=12)
    table.add_column("What It Was", style="dim", max_width=50)

    for i, t in enumerate(targets, 1):
        status_style = "red" if t["status"] == "dead" else "yellow" if "changed" in t["status"] or "dead" in t["status"] or "gutted" in t["status"] else "green"
        table.add_row(str(i), t["domain"], t["years"], f"[{status_style}]{t['status']}[/{status_style}]", t["note"][:50])

    console.print(table)

    # Let user pick domains to scan
    target_choices = [{"name": f"{t['domain']} - {t['note'][:60]}", "value": i} for i, t in enumerate(targets)]
    selected = inquirer.checkbox(
        message="Select domains to scan (space to toggle):",
        choices=target_choices,
    ).execute()

    if not selected:
        return

    # Pick file types
    file_types = pick_file_types()
    if not file_types:
        file_types = ["Everything interesting"]

    extensions = []
    for name in file_types:
        extensions.extend(FILE_PRESETS[name]["extensions"])

    cdx = CDXClient()
    history = DownloadHistory()

    for idx in selected:
        target = targets[idx]
        domain = target["domain"]

        # Skip archive.org direct links - those aren't CDX searchable
        if domain.startswith("archive.org/details/"):
            collection_id = domain.replace("archive.org/details/", "")
            console.print(f"\n[bold cyan]{target['note'][:60]}[/bold cyan]")
            console.print(f"[dim]This is a direct Archive.org collection. Fetching file list...[/dim]")
            try:
                files = fetch_archive_files(collection_id)
                if files:
                    console.print(f"[green]Found {len(files)} files[/green]")
                    download = inquirer.confirm(message=f"Download {len(files)} files?", default=False).execute()
                    if download:
                        dest_dir = os.path.join(dest_root, sanitize_filename(collection_id))
                        os.makedirs(dest_dir, exist_ok=True)
                        _download_results([{"timestamp": "", "original": f["url"], "mimetype": "", "statuscode": "200", "digest": "", "length": "0"} if "timestamp" not in f else f for f in files], dest_dir, history)
                else:
                    console.print("[yellow]No MP3 files found.[/yellow]")
                    console.print(f"[dim]Browse manually: https://{domain}[/dim]")
            except Exception as e:
                console.print(f"[red]Error: {e}[/red]")
            continue

        console.print(f"\n[bold]Scanning [cyan]{domain}[/cyan]...[/bold]")
        console.print(f"[dim]{target['note']}[/dim]")

        results = cdx.smart_search(
            f"*.{domain}" if not domain.startswith("*.") else domain,
            extensions=extensions,
            match_type="domain",
            limit=20000,
        )

        if results:
            _display_scan_results(domain, results, file_types)
            download = inquirer.confirm(message=f"Download {len(results)} files from {domain}?", default=False).execute()
            if download:
                dest_dir = os.path.join(dest_root, sanitize_filename(domain))
                os.makedirs(dest_dir, exist_ok=True)
                _download_results(results, dest_dir, history)
            else:
                _save_results(domain, results, dest_root)
        else:
            console.print(f"[yellow]No matching files found on {domain}.[/yellow]")

    history.save()


def mode_domain_scan(dest_root):
    """Scan an entire domain for archived files."""
    domain = inquirer.text(message="Enter domain to scan (e.g. oldsite.com):").execute()
    if not domain.strip():
        return

    domain = domain.strip().replace("http://", "").replace("https://", "").rstrip("/")

    selected_types = pick_file_types()
    if not selected_types:
        console.print("[yellow]No file types selected.[/yellow]")
        return

    from_date, to_date = pick_date_range()

    # Build filters
    all_extensions = []
    all_mimes = []
    for type_name in selected_types:
        preset = FILE_PRESETS[type_name]
        all_extensions.extend(preset["extensions"])
        all_mimes.extend(preset["mimetypes"])

    cdx = CDXClient()
    history = DownloadHistory()

    console.print(f"\n[bold]Scanning [cyan]{domain}[/cyan] for {', '.join(selected_types)}...[/bold]")
    console.print(f"[dim]Running multi-strategy search (MIME + extension + fallback)...[/dim]")

    results = cdx.smart_search(
        f"*.{domain}" if "." in domain else domain,
        extensions=all_extensions,
        match_type="domain",
        from_date=from_date,
        to_date=to_date,
        limit=50000,
    )

    if not results:
        console.print("[yellow]No files found in the Wayback Machine for this domain.[/yellow]")
        console.print("[dim]The domain may not have been crawled, or files weren't captured.[/dim]")
        return

    # Analyze results
    _display_scan_results(domain, results, selected_types)

    # Ask to download
    downloadable = [r for r in results if r.get("original")]
    if not downloadable:
        return

    download = inquirer.confirm(message=f"Download {len(downloadable)} files?", default=False).execute()
    if download:
        folder = sanitize_filename(domain)
        dest_dir = os.path.join(dest_root, folder)
        os.makedirs(dest_dir, exist_ok=True)
        _download_results(downloadable, dest_dir, history)
        history.save()
    else:
        # Save results to JSON
        _save_results(domain, results, dest_root)


def _display_scan_results(domain, results, selected_types):
    """Display formatted scan results."""
    # Group by extension
    by_ext = defaultdict(list)
    for r in results:
        url = r.get("original", "")
        ext = url.rsplit(".", 1)[-1].lower() if "." in url.split("/")[-1] else "unknown"
        by_ext[ext].append(r)

    # Size estimate
    total_size = sum(int(r.get("length", 0)) for r in results)
    size_mb = total_size / 1024 / 1024

    table = Table(title=f"Archived Files on {domain}", box=box.ROUNDED)
    table.add_column("Extension", style="cyan")
    table.add_column("Count", justify="right", style="green")
    table.add_column("Est. Size", justify="right")
    table.add_column("Date Range")
    table.add_column("Sample URL", style="dim", max_width=50)

    for ext in sorted(by_ext.keys(), key=lambda x: -len(by_ext[x])):
        items = by_ext[ext]
        ext_size = sum(int(r.get("length", 0)) for r in items) / 1024 / 1024
        timestamps = [r.get("timestamp", "") for r in items if r.get("timestamp")]
        date_range = ""
        if timestamps:
            earliest = min(timestamps)[:4]
            latest = max(timestamps)[:4]
            date_range = f"{earliest}-{latest}" if earliest != latest else earliest

        sample = items[0].get("original", "")
        if len(sample) > 50:
            sample = "..." + sample[-47:]

        table.add_row(
            f".{ext}",
            str(len(items)),
            f"{ext_size:.1f} MB" if ext_size > 0.1 else "<0.1 MB",
            date_range,
            sample,
        )

    console.print(table)
    console.print(f"\n  [bold]Total:[/bold] {len(results)} unique files | ~{size_mb:.1f} MB compressed")
    console.print(f"  [dim]CDX requests: {CDXClient().total_requests} | Rate limit hits: {CDXClient().total_429s}[/dim]")


# ═══════════════════════════════════════════════════════════════════
# MODE: FILE TYPE HUNT
# ═══════════════════════════════════════════════════════════════════

def mode_filetype_hunt(dest_root):
    """Search for specific file types on a domain."""
    domain = inquirer.text(message="Enter domain (e.g. oldmusicblog.com):").execute()
    if not domain.strip():
        return

    domain = domain.strip().replace("http://", "").replace("https://", "").rstrip("/")

    # Specific extension input
    console.print("[dim]Enter file extensions separated by commas, or choose presets.[/dim]")
    mode = inquirer.select(
        message="How to specify file types?",
        choices=[
            {"name": "Choose from presets", "value": "preset"},
            {"name": "Enter extensions manually (e.g. mp3,flac,wav)", "value": "manual"},
        ],
    ).execute()

    if mode == "preset":
        selected = pick_file_types()
        if not selected:
            return
        extensions = []
        for name in selected:
            extensions.extend(FILE_PRESETS[name]["extensions"])
    else:
        ext_input = inquirer.text(message="Extensions (comma-separated):").execute()
        extensions = [e.strip().lstrip(".") for e in ext_input.split(",") if e.strip()]

    if not extensions:
        console.print("[yellow]No extensions specified.[/yellow]")
        return

    from_date, to_date = pick_date_range()
    cdx = CDXClient()

    console.print(f"\n[bold]Hunting for .{', .'.join(extensions[:10])} on [cyan]{domain}[/cyan]...[/bold]")
    console.print(f"[dim]Running multi-strategy search...[/dim]")

    results = cdx.smart_search(
        f"*.{domain}",
        extensions=extensions,
        match_type="domain",
        from_date=from_date,
        to_date=to_date,
        limit=50000,
    )

    if not results:
        console.print("[yellow]No matching files found.[/yellow]")
        return

    _display_scan_results(domain, results, extensions)

    download = inquirer.confirm(message=f"Download {len(results)} files?", default=False).execute()
    if download:
        history = DownloadHistory()
        folder = sanitize_filename(f"{domain}_files")
        dest_dir = os.path.join(dest_root, folder)
        os.makedirs(dest_dir, exist_ok=True)
        _download_results(results, dest_dir, history)
        history.save()
    else:
        _save_results(domain, results, dest_root)


# ═══════════════════════════════════════════════════════════════════
# MODE: DEFUNCT HOST EXCAVATION
# ═══════════════════════════════════════════════════════════════════

def mode_defunct_dig(dest_root):
    """Excavate dead file hosting services."""
    # Show the database
    table = Table(title="Defunct Host Database", box=box.ROUNDED)
    table.add_column("Service", style="cyan")
    table.add_column("Domain")
    table.add_column("Died", style="red")
    table.add_column("Note", style="dim", max_width=40)

    for name, info in sorted(DEFUNCT_HOSTS.items()):
        table.add_row(name, info["domain"], info["died"], info["note"])

    console.print(table)

    # Select hosts
    choices = [{"name": f"{name} ({info['domain']})", "value": name} for name, info in sorted(DEFUNCT_HOSTS.items())]
    selected = inquirer.checkbox(
        message="Select hosts to excavate (space to toggle):",
        choices=choices,
    ).execute()

    if not selected:
        return

    selected_types = pick_file_types()
    if not selected_types:
        selected_types = ["Everything interesting"]

    extensions = []
    for type_name in selected_types:
        extensions.extend(FILE_PRESETS[type_name]["extensions"])

    cdx = CDXClient()
    history = DownloadHistory()
    all_results = {}

    for host_name in selected:
        host = DEFUNCT_HOSTS[host_name]
        domain = host["domain"]

        console.print(f"\n[bold]Excavating [cyan]{host_name}[/cyan] ({domain})...[/bold]")

        for pattern in host["patterns"]:
            console.print(f"  [dim]Pattern: {pattern}[/dim]")

            with console.status(f"[bold]Querying CDX for {pattern}..."):
                # Try with extension filter first
                ext_filter = build_extension_filter(extensions)
                results = cdx.search(
                    pattern,
                    match_type="prefix" if "*" in pattern else "exact",
                    filters=["statuscode:200", ext_filter],
                    collapse="digest",
                    limit=10000,
                )

                # If no extension-filtered results, try without filter to see what's there
                if not results:
                    results = cdx.search(
                        pattern.replace("*", ""),
                        match_type="prefix",
                        filters=["statuscode:200", "!mimetype:text/html", "!mimetype:text/css", "!mimetype:application/javascript"],
                        collapse="digest",
                        limit=5000,
                    )

            if results:
                console.print(f"  [green]Found {len(results)} files![/green]")
                all_results[host_name] = all_results.get(host_name, []) + results
            else:
                console.print(f"  [dim]Nothing found for this pattern.[/dim]")

    # Summary
    if not all_results:
        console.print("\n[yellow]No files found across selected hosts.[/yellow]")
        return

    console.print(Rule("[bold]Excavation Results[/bold]"))
    total = 0
    for host_name, results in all_results.items():
        console.print(f"  [bold green]{host_name}[/bold green]: {len(results)} files")
        total += len(results)

        by_mime = defaultdict(int)
        for r in results[:100]:
            by_mime[r.get("mimetype", "unknown")] += 1
        for mime, cnt in sorted(by_mime.items(), key=lambda x: -x[1])[:5]:
            console.print(f"    [dim]{mime}: {cnt}[/dim]")

    console.print(f"\n  [bold]Total: {total} files across {len(all_results)} hosts[/bold]")

    # Download or save
    download = inquirer.confirm(message=f"Download {total} files?", default=False).execute()
    if download:
        for host_name, results in all_results.items():
            dest_dir = os.path.join(dest_root, "defunct_hosts", sanitize_filename(host_name))
            os.makedirs(dest_dir, exist_ok=True)
            console.print(f"\n[bold]Downloading {host_name} -> {dest_dir}[/bold]")
            _download_results(results, dest_dir, history)
        history.save()
    else:
        for host_name, results in all_results.items():
            _save_results(host_name, results, dest_root)


# ═══════════════════════════════════════════════════════════════════
# MODE: DEAD LINK RECOVERY
# ═══════════════════════════════════════════════════════════════════

def mode_dead_links(dest_root):
    """Batch-check dead URLs against archives."""
    console.print("[dim]Enter dead URLs one per line, or paste a file path containing URLs.[/dim]")
    input_mode = inquirer.select(
        message="How to provide URLs?",
        choices=[
            {"name": "Paste URLs (one per line, Ctrl+D or empty line to finish)", "value": "paste"},
            {"name": "Load from file", "value": "file"},
        ],
    ).execute()

    urls = []
    if input_mode == "file":
        filepath = inquirer.text(message="Path to URL list file:").execute()
        if os.path.exists(filepath):
            with open(filepath, "r", encoding="utf-8") as f:
                urls = [line.strip() for line in f if line.strip() and line.strip().startswith("http")]
        else:
            console.print("[red]File not found.[/red]")
            return
    else:
        console.print("[dim]Paste URLs (empty line to finish):[/dim]")
        while True:
            line = input().strip()
            if not line:
                break
            if line.startswith("http"):
                urls.append(line)

    if not urls:
        console.print("[yellow]No URLs provided.[/yellow]")
        return

    console.print(f"\n[bold]Checking {len(urls)} URLs against archives...[/bold]")

    cdx = CDXClient()
    found = []
    not_found = []

    with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
                  BarColumn(), TextColumn("{task.completed}/{task.total}"), console=console) as progress:
        task = progress.add_task("Checking", total=len(urls))

        for url in urls:
            # Check Wayback CDX
            results = cdx.search(url, match_type="exact", filters=["statuscode:200"], limit=1)
            if results:
                r = results[0]
                wb_url = build_wayback_url(r["timestamp"], r["original"])
                found.append({"original": url, "wayback_url": wb_url, "timestamp": r["timestamp"],
                              "mimetype": r.get("mimetype", ""), "record": r})
            else:
                # Try availability API as fallback
                avail = cdx.check_availability(url)
                if avail:
                    found.append({"original": url, "wayback_url": avail["url"],
                                  "timestamp": avail.get("timestamp", ""), "mimetype": "", "record": avail})
                else:
                    not_found.append(url)

            progress.advance(task)

    # Display results
    console.print(Rule("[bold]Recovery Results[/bold]"))
    console.print(f"  [green]Recoverable: {len(found)}[/green]")
    console.print(f"  [red]Lost: {len(not_found)}[/red]")

    if found:
        table = Table(title="Recoverable URLs", box=box.ROUNDED)
        table.add_column("Original URL", style="dim", max_width=50)
        table.add_column("Snapshot Date")
        table.add_column("Type")

        for f in found[:50]:
            orig = f["original"]
            if len(orig) > 50:
                orig = orig[:47] + "..."
            ts = f["timestamp"][:8] if f["timestamp"] else "?"
            table.add_row(orig, ts, f.get("mimetype", ""))

        console.print(table)
        if len(found) > 50:
            console.print(f"  [dim]...and {len(found) - 50} more[/dim]")

    if not_found:
        console.print(f"\n  [red]Lost URLs ({len(not_found)}):[/red]")
        for url in not_found[:10]:
            console.print(f"    [dim]{url[:80]}[/dim]")
        if len(not_found) > 10:
            console.print(f"    [dim]...and {len(not_found) - 10} more[/dim]")

    # Download recoverable
    if found:
        download = inquirer.confirm(message=f"Download {len(found)} recovered files?", default=True).execute()
        if download:
            history = DownloadHistory()
            dest_dir = os.path.join(dest_root, "recovered_links")
            os.makedirs(dest_dir, exist_ok=True)

            # Convert to download format
            dl_records = []
            for f in found:
                r = f.get("record", {})
                if isinstance(r, dict) and "timestamp" in r and "original" in r:
                    dl_records.append(r)

            _download_results(dl_records, dest_dir, history)
            history.save()


# ═══════════════════════════════════════════════════════════════════
# MODE: CUSTOM CDX QUERY
# ═══════════════════════════════════════════════════════════════════

def mode_custom_cdx(dest_root):
    """Build and execute a custom CDX query."""
    url = inquirer.text(message="URL pattern (e.g. *.example.com, site.com/files/*):").execute()
    if not url.strip():
        return

    match_type = inquirer.select(
        message="Match type:",
        choices=[
            {"name": "domain - Host + all subdomains", "value": "domain"},
            {"name": "prefix - All URLs starting with pattern", "value": "prefix"},
            {"name": "host - Exact host only", "value": "host"},
            {"name": "exact - Exact URL only", "value": "exact"},
        ],
    ).execute()

    # Filters
    filters = ["statuscode:200"]

    add_mime = inquirer.confirm(message="Add MIME type filter?", default=False).execute()
    if add_mime:
        mime = inquirer.text(message="MIME regex (e.g. audio/.*, application/pdf):").execute()
        if mime.strip():
            filters.append(f"mimetype:{mime.strip()}")

    add_ext = inquirer.confirm(message="Add file extension filter?", default=False).execute()
    if add_ext:
        ext = inquirer.text(message="Extensions (comma-separated, e.g. mp3,pdf,zip):").execute()
        if ext.strip():
            exts = [e.strip().lstrip(".") for e in ext.split(",")]
            filters.append(build_extension_filter(exts))

    exclude_html = inquirer.confirm(message="Exclude HTML/CSS/JS?", default=True).execute()
    if exclude_html:
        filters.extend(["!mimetype:text/html", "!mimetype:text/css", "!mimetype:application/javascript"])

    from_date, to_date = pick_date_range()

    limit = inquirer.text(message="Max results (default 10000):", default="10000").execute()
    limit = int(limit) if limit.isdigit() else 10000

    # Execute
    cdx = CDXClient()
    console.print(f"\n[bold]Executing CDX query...[/bold]")
    console.print(f"  [dim]URL: {url}[/dim]")
    console.print(f"  [dim]Filters: {filters}[/dim]")

    with console.status("[bold]Querying..."):
        results = cdx.search(
            url.strip(),
            match_type=match_type,
            filters=filters,
            collapse="digest",
            from_date=from_date,
            to_date=to_date,
            limit=limit,
        )

    if not results:
        console.print("[yellow]No results.[/yellow]")
        return

    console.print(f"[green]Found {len(results)} records[/green]")

    # Display
    table = Table(box=box.ROUNDED)
    table.add_column("Timestamp", width=14)
    table.add_column("URL", style="cyan", max_width=60)
    table.add_column("MIME", style="dim")
    table.add_column("Size", justify="right")

    for r in results[:30]:
        url_str = r.get("original", "")
        if len(url_str) > 60:
            url_str = "..." + url_str[-57:]
        size = int(r.get("length", 0))
        size_str = f"{size/1024:.0f}K" if size > 0 else "?"
        table.add_row(r.get("timestamp", ""), url_str, r.get("mimetype", ""), size_str)

    console.print(table)
    if len(results) > 30:
        console.print(f"  [dim]...and {len(results) - 30} more[/dim]")

    download = inquirer.confirm(message=f"Download {len(results)} files?", default=False).execute()
    if download:
        history = DownloadHistory()
        folder = sanitize_filename(url.strip().replace("*.", "").replace("/*", ""))
        dest_dir = os.path.join(dest_root, folder)
        os.makedirs(dest_dir, exist_ok=True)
        _download_results(results, dest_dir, history)
        history.save()
    else:
        _save_results(url.strip(), results, dest_root)


# ═══════════════════════════════════════════════════════════════════
# MODE: STATS
# ═══════════════════════════════════════════════════════════════════

def mode_stats():
    history = DownloadHistory()
    stats = history.data["stats"]
    mb = stats["total_bytes"] / 1024 / 1024
    gb = mb / 1024

    panel_text = (
        f"[bold]Files recovered:[/bold] {history.count}\n"
        f"[bold]Data recovered:[/bold] {mb:.0f} MB ({gb:.2f} GB)\n"
        f"[bold]Total searches:[/bold] {stats.get('total_searches', 0)}\n"
        f"[bold]History file:[/bold] {HISTORY_FILE}"
    )
    console.print(Panel(panel_text, title="Ghostlight Recovery Stats", border_style="yellow"))


# ═══════════════════════════════════════════════════════════════════
# SHARED DOWNLOAD & SAVE
# ═══════════════════════════════════════════════════════════════════

def _download_results(results, dest_dir, history):
    """Download a batch of CDX results."""
    stats = {"downloaded": 0, "skipped": 0, "failed": 0, "rate_limited": 0}

    with Progress(
        SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
        BarColumn(), TextColumn("{task.completed}/{task.total}"),
        TextColumn("[dim]{task.fields[file]}[/dim]"),
        console=console,
    ) as progress:
        task = progress.add_task("Downloading", total=len(results), file="")

        for r in results:
            original = r.get("original", "")
            timestamp = r.get("timestamp", "")

            if not original or not timestamp:
                progress.advance(task)
                continue

            # Build filename from URL
            parsed = urlparse(original)
            filename = unquote(parsed.path.split("/")[-1]) or "index.html"
            filename = sanitize_filename(f"{timestamp}_{filename}")
            dest_path = os.path.join(dest_dir, filename)

            # Build download URL
            wb_url = build_wayback_url(timestamp, original, raw=True)

            progress.update(task, file=filename[:40])
            status, size = download_wayback_file(wb_url, dest_path, history)
            stats[status if status in stats else "failed"] += 1

            if status == "downloaded":
                time.sleep(1.0)  # Respect rate limits
            elif status == "rate_limited":
                time.sleep(5.0)

            progress.advance(task)

    console.print(
        f"  [green]{stats['downloaded']} downloaded[/green] | "
        f"[yellow]{stats['skipped']} skipped[/yellow] | "
        f"[red]{stats['failed']} failed[/red]"
        + (f" | [yellow]{stats['rate_limited']} rate limited[/yellow]" if stats['rate_limited'] else "")
    )


def _save_results(label, results, dest_root):
    """Save scan results to JSON."""
    os.makedirs(os.path.join(dest_root, "scans"), exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_label = sanitize_filename(label)
    filepath = os.path.join(dest_root, "scans", f"{safe_label}_{timestamp}.json")
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump({"query": label, "count": len(results), "results": results}, f, indent=2)
    console.print(f"  [dim]Results saved to {filepath}[/dim]")


# ═══════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Ghostlight - Lost File Recovery Engine")
    parser.add_argument("--dest", "-d", default=DEFAULT_DEST, help="Output directory")
    args = parser.parse_args()

    show_banner()
    dest_root = pick_directory(args.dest)
    os.makedirs(dest_root, exist_ok=True)
    console.print(f"[dim]Output: {dest_root}[/dim]\n")

    while True:
        try:
            action = main_menu()

            if action == "exit":
                console.print("\n[bold yellow]The past is never dead. It's not even past.[/bold yellow]\n")
                break
            elif action == "browse_targets":
                mode_browse_targets(dest_root)
            elif action == "domain_scan":
                mode_domain_scan(dest_root)
            elif action == "filetype_hunt":
                mode_filetype_hunt(dest_root)
            elif action == "defunct_dig":
                mode_defunct_dig(dest_root)
            elif action == "dead_links":
                mode_dead_links(dest_root)
            elif action == "custom_cdx":
                mode_custom_cdx(dest_root)
            elif action == "stats":
                mode_stats()

            console.print()

        except KeyboardInterrupt:
            console.print("\n[bold yellow]The past is never dead. It's not even past.[/bold yellow]\n")
            break
        except Exception as e:
            console.print(f"\n[red]Error: {e}[/red]\n")


if __name__ == "__main__":
    main()
