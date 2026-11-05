#!/bin/bash
# Launchscope: rate new launches, judge past ones, push live. Runs every 6h.
cd /Users/jj/Downloads/Claude/Code/launchscope || exit 1
export PATH=/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin
LOG=/tmp/launchscope_cron.log
echo "=== $(date) ===" >> "$LOG"
/usr/bin/python3 build.py >> "$LOG" 2>&1
TOKEN=$(/Users/jj/.claude/bin/vault get GITHUB_TOKEN 2>/dev/null)
[ -z "$TOKEN" ] && { echo "no token" >> "$LOG"; exit 1; }
git add -A
git -c user.name="Launchscope" -c user.email="noreply@launchscope.local" commit -q -m "ratings $(date -u +%F_%H%M)" >> "$LOG" 2>&1
git push -q "https://b00831052:${TOKEN}@github.com/b00831052/launchscope.git" main >> "$LOG" 2>&1 && echo "pushed" >> "$LOG"
