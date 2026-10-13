#!/bin/sh
# Publish site/ (without the API cache) to the gh-pages branch.
set -e
cd "$(dirname "$0")"
tmp=$(mktemp -d)
rsync -a --delete --exclude _cache site/ "$tmp/"
touch "$tmp/.nojekyll"
remote=${DEPLOY_REMOTE:-$(git remote get-url origin)}
cd "$tmp"
git init -q -b gh-pages
git add -A
git commit -q -m "Build $(date '+%Y-%m-%d %H:%M')"
git push -f "$remote" gh-pages
rm -rf "$tmp"
