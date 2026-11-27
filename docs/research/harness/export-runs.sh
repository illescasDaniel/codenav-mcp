#!/usr/bin/env bash
# Copy the reviewable part of a small-model campaign into docs/research/data/ (no raw streams, no local paths).
#   bash export-runs.sh CAMPAIGN_DIR DEST_DIR
# Per run: answer.txt (the agent's answer.md, kept as plain text: its links point into the run's clone), metadata.txt, changes.patch, diffstat.txt, ty.txt and the pytest summary line; one prompt per
# task in DEST/prompts/; the campaign's results.md and summary.md. Home-directory paths become "~".
set -euo pipefail
campaign=$(cd "$1" && pwd) dest=$2
mkdir -p "$dest/prompts"
scrub() { sed -e "s#$HOME#~#g" -e 's#/mnt/nas/[^ )"]*/codenav-mcp-video#<video-folder>#g' "$1"; }
for f in results.md summary.md; do [[ -f $campaign/$f ]] && scrub "$campaign/$f" > "$dest/$f"; done
for run in "$campaign"/*/task-* "$campaign"/*/*/task-*; do
  [[ -d $run && -f $run/prompt.txt ]] || continue
  rel=${run#"$campaign"/}
  mkdir -p "$dest/$rel"
  [[ -f $run/answer.md ]] && scrub "$run/answer.md" > "$dest/$rel/answer.txt"
  for f in metadata.txt changes.patch diffstat.txt ty.txt; do
    [[ -f $run/$f ]] && scrub "$run/$f" > "$dest/$rel/$f"
  done
  [[ -f $run/pytest.txt ]] && tail -n 1 "$run/pytest.txt" > "$dest/$rel/pytest-summary.txt"
  task=$(basename "$run" | cut -d- -f2)
  [[ -f $dest/prompts/task-$task.txt ]] || scrub "$run/prompt.txt" > "$dest/prompts/task-$task.txt"
done
if grep -rIl -e "$HOME" -e '@gmail' "$dest" >/dev/null; then
  echo "private paths or addresses left in $dest:" >&2
  grep -rIl -e "$HOME" -e '@gmail' "$dest" >&2
  exit 1
fi
