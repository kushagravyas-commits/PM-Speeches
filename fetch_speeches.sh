#!/usr/bin/env bash
# Fetch each speech page via jina reader proxy, extract editorial headline + youtube embed.
set -u
cd "$(dirname "$0")"
mkdir -p pages
RESULTS="results.tsv"
MAXJOBS=3

fetch_one() {
  local serial="$1" url="$2"
  local raw="pages/${serial}.txt"
  if [ ! -s "$raw" ] || ! grep -q "^Title:" "$raw"; then
    for attempt in 1 2 3; do
      curl -sS -m 100 --compressed "https://r.jina.ai/${url}" -o "$raw" 2>/dev/null
      if [ -s "$raw" ] && grep -q "^Title:" "$raw"; then break; fi
      sleep 3
    done
  fi
  local title="" yt="" vid="" status="FAIL"
  if [ -s "$raw" ] && grep -q "^Title:" "$raw"; then
    title=$(grep -m1 "^Title:" "$raw" | sed 's/^Title:[[:space:]]*//')
    vid=$(grep -oiE "youtube\.com/watch\?v=[A-Za-z0-9_-]+" "$raw" | head -1 | sed -E 's#.*v=##')
    [ -n "$vid" ] && yt="https://www.youtube.com/embed/${vid}"
    status="OK"
  fi
  printf '%s\t%s\t%s\t%s\n' "$serial" "$status" "$title" "$yt" > "pages/${serial}.result"
  echo "[$status] $serial  ${title:0:55}"
}

# Job pool
while IFS=$'\t' read -r ln serial url; do
  fetch_one "$serial" "$url" &
  while [ "$(jobs -r | wc -l)" -ge "$MAXJOBS" ]; do sleep 0.4; done
done < worklist.tsv
wait

# Combine results in worklist order
: > "$RESULTS"
while IFS=$'\t' read -r ln serial url; do
  [ -f "pages/${serial}.result" ] && cat "pages/${serial}.result" >> "$RESULTS"
done < worklist.tsv
echo "DONE. $(wc -l < "$RESULTS") results written to $RESULTS"
echo "Failures: $(grep -c $'\tFAIL\t' "$RESULTS" || true)"
