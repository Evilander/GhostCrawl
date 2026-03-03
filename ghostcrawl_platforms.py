#!/usr/bin/env python3
"""
GhostCrawl Platform Scrapers - Multi-platform content extraction.

Integrates APIs and extraction techniques for multiple platforms:
  - Patreon: Search API (profiles, avatars, post counts)
  - Imageboards: Safebooru (open JSON API with direct file URLs)
  - Reddit: .json suffix for subreddit/user/search data with media URLs
"""

import requests
import os
import sys
import time
import re
from urllib.parse import quote

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

DEFAULT_DEST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads")


class PlatformSession:
    """Base session with anti-detection headers."""

    def __init__(self, rate_limit=0.5):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                          'AppleWebKit/537.36 (KHTML, like Gecko) '
                          'Chrome/131.0.0.0 Safari/537.36',
            'Accept': '*/*',
            'Accept-Language': 'en-US,en;q=0.9',
        })
        self.rate_limit = rate_limit

    def get(self, url, **kwargs):
        time.sleep(self.rate_limit)
        kwargs.setdefault('timeout', 15)
        return self.session.get(url, **kwargs)

    def download(self, url, filepath, min_size=500):
        try:
            r = self.get(url, timeout=30)
            if r.status_code == 200 and len(r.content) > min_size:
                os.makedirs(os.path.dirname(filepath), exist_ok=True)
                with open(filepath, 'wb') as f:
                    f.write(r.content)
                return len(r.content)
        except Exception:
            pass
        return 0


# ===========================================================================
#  PATREON - Search API
# ===========================================================================

class PatreonScraper:
    """
    Patreon search API is open - returns:
      - Creator profiles with avatar URLs, patron counts, post stats
      - Campaign data with cover photos
    """

    def __init__(self, dest=DEFAULT_DEST):
        self.session = PlatformSession(rate_limit=1.0)
        self.session.session.headers['Referer'] = 'https://www.patreon.com/'
        self.dest = os.path.join(dest, 'patreon')

    def search(self, query, limit=20):
        """Search Patreon creators."""
        url = f"https://www.patreon.com/api/search?q={quote(query)}&page%5Bcount%5D={limit}"
        r = self.session.get(url)
        if r.status_code != 200:
            return []

        data = r.json()
        results = []
        for item in data.get('data', []):
            attrs = item.get('attributes', {})
            results.append({
                'id': item.get('id', ''),
                'name': attrs.get('creator_name') or attrs.get('name', ''),
                'creation_name': attrs.get('creation_name', ''),
                'patron_count': attrs.get('patron_count', 0),
                'post_count': attrs.get('post_statistics', {}).get('total', 0),
                'is_nsfw': attrs.get('is_nsfw', False),
                'avatar_url': attrs.get('avatar_photo_url', ''),
                'thumb_url': attrs.get('thumb', ''),
                'url': attrs.get('url', ''),
                'summary': attrs.get('summary', ''),
            })

        print(f"  Patreon search '{query}': {len(results)} results")
        return results

    def get_campaign(self, vanity):
        """Get campaign data by vanity URL."""
        url = f"https://www.patreon.com/api/campaigns?filter%5Bvanity%5D={vanity}"
        r = self.session.get(url)
        if r.status_code != 200:
            return None

        data = r.json()
        campaigns = data.get('data', [])
        if not campaigns:
            return None

        campaign = campaigns[0]
        attrs = campaign.get('attributes', {})
        return {
            'id': campaign.get('id'),
            'avatar_url': attrs.get('avatar_photo_url', ''),
            'cover_url': attrs.get('cover_photo_url', ''),
            'created_at': attrs.get('created_at', ''),
            'creation_count': attrs.get('creation_count', 0),
            'creation_name': attrs.get('creation_name', ''),
            'currency': attrs.get('currency', ''),
        }

    def download_avatars(self, results):
        """Download creator avatars."""
        os.makedirs(self.dest, exist_ok=True)
        count = 0
        total = 0
        for r in results:
            url = r.get('avatar_url') or r.get('thumb_url')
            if not url:
                continue
            name = re.sub(r'[^\w-]', '_', r['name'])[:50]
            filepath = os.path.join(self.dest, f"{name}_avatar.jpg")
            size = self.session.download(url, filepath)
            if size > 0:
                count += 1
                total += size

        print(f"  Avatars: {count}/{len(results)} downloaded ({total / 1024:.0f} KB)")
        return count


# ===========================================================================
#  IMAGEBOARDS - Open JSON APIs
# ===========================================================================

class ImageboardScraper:
    """
    Imageboard APIs that serve direct file URLs without auth:
      - Safebooru: Moebooru-compatible JSON API (SFW anime artwork)
    """

    BOARDS = {
        'safebooru': {
            'url': 'https://safebooru.org/index.php?page=dapi&s=post&q=index&json=1',
            'file_key': 'file_url',
        },
    }

    def __init__(self, dest=DEFAULT_DEST):
        self.session = PlatformSession(rate_limit=1.0)
        self.dest = os.path.join(dest, 'imageboards')

    def search(self, board, tags, limit=50, page=1):
        if board not in self.BOARDS:
            print(f"  Unknown board: {board}")
            return []

        config = self.BOARDS[board]
        params = {'limit': limit, 'tags': tags, 'pid': page - 1}

        r = self.session.get(config['url'], params=params)
        if r.status_code != 200:
            print(f"  {board}: HTTP {r.status_code}")
            return []

        posts = r.json()
        if not isinstance(posts, list):
            posts = posts.get('posts', posts.get('post', []))
        print(f"  {board} '{tags}': {len(posts)} results")
        return posts

    def extract_urls(self, board, posts):
        """Extract file URLs from imageboard posts."""
        config = self.BOARDS[board]
        urls = []

        for post in posts:
            file_url = post.get(config['file_key'])

            if file_url:
                urls.append({
                    'url': file_url,
                    'id': post.get('id'),
                    'tags': post.get('tags', post.get('tag_string', '')),
                    'score': post.get('score', 0),
                    'board': board,
                })

        return urls

    def download_posts(self, board, tags, limit=50):
        """Search and download images from an imageboard."""
        posts = self.search(board, tags, limit)
        urls = self.extract_urls(board, posts)

        dest_dir = os.path.join(self.dest, board, tags.replace(' ', '_')[:30])
        os.makedirs(dest_dir, exist_ok=True)

        count = 0
        total = 0

        for item in urls:
            url = item['url']
            ext = url.rsplit('.', 1)[-1].split('?')[0] if '.' in url else 'jpg'
            fname = f"{board}_{item['id']}.{ext}"
            filepath = os.path.join(dest_dir, fname)

            if os.path.exists(filepath):
                continue

            size = self.session.download(url, filepath)
            if size > 0:
                count += 1
                total += size

        print(f"  Downloaded: {count}/{len(urls)} ({total / 1024 / 1024:.1f} MB)")
        return count, total


# ===========================================================================
#  REDDIT - JSON API
# ===========================================================================

class RedditScraper:
    """Reddit .json API for extracting media from subreddits/users."""

    def __init__(self, dest=DEFAULT_DEST):
        self.session = PlatformSession(rate_limit=2.0)
        self.dest = os.path.join(dest, 'reddit')

    def get_subreddit(self, subreddit, sort='hot', limit=50, after=None):
        """Get posts from a subreddit."""
        url = f"https://www.reddit.com/r/{subreddit}/{sort}.json?limit={limit}"
        if after:
            url += f"&after={after}"

        r = self.session.get(url)
        if r.status_code != 200:
            return []

        data = r.json()
        posts = [child['data'] for child in data.get('data', {}).get('children', [])]
        print(f"  r/{subreddit}/{sort}: {len(posts)} posts")
        return posts

    def extract_media(self, posts):
        """Extract media URLs from Reddit posts."""
        media = []
        for p in posts:
            # Direct image URLs
            url = p.get('url_overridden_by_dest') or p.get('url', '')
            if any(url.endswith(ext) for ext in ['.jpg', '.jpeg', '.png', '.gif', '.mp4', '.webm']):
                media.append({
                    'url': url,
                    'title': p.get('title', '')[:80],
                    'subreddit': p.get('subreddit', ''),
                    'score': p.get('score', 0),
                    'id': p.get('id', ''),
                })

            # Reddit preview images
            preview = p.get('preview', {})
            images = preview.get('images', [])
            for img in images:
                source = img.get('source', {})
                img_url = source.get('url', '').replace('&amp;', '&')
                if img_url:
                    media.append({
                        'url': img_url,
                        'title': p.get('title', '')[:80],
                        'subreddit': p.get('subreddit', ''),
                        'score': p.get('score', 0),
                        'id': p.get('id', '') + '_preview',
                        'width': source.get('width', 0),
                        'height': source.get('height', 0),
                    })

            # Reddit gallery
            gallery = p.get('media_metadata', {})
            for media_id, meta in gallery.items():
                if meta.get('status') == 'valid':
                    src = meta.get('s', {})
                    img_url = src.get('u', '').replace('&amp;', '&')
                    if img_url:
                        media.append({
                            'url': img_url,
                            'title': p.get('title', '')[:80],
                            'subreddit': p.get('subreddit', ''),
                            'score': p.get('score', 0),
                            'id': media_id,
                        })

        return media

    def download_subreddit(self, subreddit, sort='hot', limit=50):
        """Download media from a subreddit."""
        posts = self.get_subreddit(subreddit, sort, limit)
        media = self.extract_media(posts)

        dest_dir = os.path.join(self.dest, subreddit)
        os.makedirs(dest_dir, exist_ok=True)

        count = 0
        total = 0
        for item in media:
            url = item['url']
            ext = url.split('?')[0].rsplit('.', 1)[-1] if '.' in url.split('?')[0] else 'jpg'
            fname = f"{item['id']}.{ext}"
            filepath = os.path.join(dest_dir, fname)

            if os.path.exists(filepath):
                continue

            size = self.session.download(url, filepath)
            if size > 0:
                count += 1
                total += size

        print(f"  Downloaded: {count}/{len(media)} ({total / 1024 / 1024:.1f} MB)")
        return count, total


# ===========================================================================
#  CLI
# ===========================================================================

def main():
    import argparse
    parser = argparse.ArgumentParser(description='GhostCrawl Platform Scrapers')
    parser.add_argument('platform', choices=['patreon', 'imageboard', 'reddit'],
                        help='Platform to scrape')
    parser.add_argument('query', help='Search query / subreddit / tags')
    parser.add_argument('--dest', default=DEFAULT_DEST)
    parser.add_argument('--limit', type=int, default=30)
    parser.add_argument('--download', action='store_true', help='Download content')
    parser.add_argument('--board', default='safebooru', help='Imageboard name')
    parser.add_argument('--sort', default='hot', help='Reddit sort order')

    args = parser.parse_args()

    if args.platform == 'patreon':
        pt = PatreonScraper(dest=args.dest)
        results = pt.search(args.query, limit=args.limit)
        if args.download:
            pt.download_avatars(results)
        for r in results[:10]:
            print(f"  {r['name']} | {r['patron_count']:,} patrons | {r['post_count']} posts | "
                  f"NSFW={r['is_nsfw']}")

    elif args.platform == 'imageboard':
        ib = ImageboardScraper(dest=args.dest)
        if args.download:
            ib.download_posts(args.board, args.query, limit=args.limit)
        else:
            posts = ib.search(args.board, args.query, limit=args.limit)
            urls = ib.extract_urls(args.board, posts)
            print(f"  {len(urls)} file URLs found")

    elif args.platform == 'reddit':
        rd = RedditScraper(dest=args.dest)
        if args.download:
            rd.download_subreddit(args.query, sort=args.sort, limit=args.limit)
        else:
            posts = rd.get_subreddit(args.query, sort=args.sort, limit=args.limit)
            media = rd.extract_media(posts)
            print(f"  {len(media)} media URLs found")


if __name__ == '__main__':
    main()
