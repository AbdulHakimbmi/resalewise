#!/usr/bin/env python3
"""Make the Open Graph / Twitter image URLs absolute once you know the live URL.

Social sites (Facebook, LinkedIn, X, Slack...) require absolute URLs for
og:image. The pages ship with relative paths so they work anywhere; run this
after deploying:

    python scripts/set_site_url.py https://your-site.onrender.com

It rewrites the og:image / twitter:image paths to absolute URLs and adds an
og:url tag per page. Re-running with a different URL is safe.
"""
import glob, os, re, sys

def main():
    if len(sys.argv) != 2 or not re.match(r"^https?://[^/\s]+", sys.argv[1]):
        sys.exit("usage: set_site_url.py https://your-domain  (no trailing path)")
    base = sys.argv[1].rstrip("/")
    root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
    changed = 0
    for path in sorted(glob.glob(os.path.join(root, "*.html"))):
        name = os.path.basename(path)
        s = open(path, encoding="utf-8").read()
        o = s
        s = re.sub(r'content="(?:https?://[^"/]+/)?(?:[^"]*/)?assets/images/og-image\.png"',
                   'content="%s/assets/images/og-image.png"' % base, s)
        s = re.sub(r'\n?[ \t]*<meta property="og:url" content="[^"]*">', "", s)
        if name != "404.html":
            page = "" if name == "index.html" else name
            s = s.replace('<meta property="og:type"', '<meta property="og:url" content="%s/%s">\n    <meta property="og:type"' % (base, page), 1)
        if s != o:
            open(path, "w", encoding="utf-8").write(s); changed += 1
    print("updated %d page(s) for %s" % (changed, base))

if __name__ == "__main__":
    main()
