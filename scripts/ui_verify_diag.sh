#!/bin/bash
# One-shot diagnostic: is the web server serving the NEW dashboard HTML?
cd /home/gabriel/cryptobot || exit 1
echo "== port 8765 owner =="
ss -ltnp 2>/dev/null | grep 8765 || echo "NOTHING LISTENING on 8765"
echo "== bot processes =="
ps -eo pid,lstart,cmd --no-headers | grep '[m]ain.py' || echo "no main.py process"
echo "== file on disk =="
ls -la ui/web_dashboard.html
echo "markers on disk: $(grep -c 'wrsplit\|feebadge\|subtabs' ui/web_dashboard.html)"
echo "== git status of file =="
git status --porcelain ui/web_dashboard.html
echo "== served HTML =="
code=$(curl -s -o /tmp/served_dash.html -w '%{http_code}' http://127.0.0.1:8765/)
echo "HTTP $code, size $(wc -c < /tmp/served_dash.html 2>/dev/null || echo 0)"
echo "markers served: $(grep -c 'wrsplit\|feebadge\|subtabs' /tmp/served_dash.html 2>/dev/null || echo 0)"
echo "arb tab still served: $(grep -c 'data-tab=\"arb\"' /tmp/served_dash.html 2>/dev/null || echo 0)"
echo "== response headers =="
curl -sI http://127.0.0.1:8765/ | head -8
