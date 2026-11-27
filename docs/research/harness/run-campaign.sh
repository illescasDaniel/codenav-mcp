#!/usr/bin/env bash
# Run a small-model campaign, one run at a time per runner; the arm order alternates per task and repeat.
#   bash run-campaign.sh RUNNER "TASKS" "REPS" CAMPAIGN_DIR ["ARMS"]
#   e.g. bash run-campaign.sh haiku "1 3 4 5" "1" results/x            (ARMS defaults to "codenav plain")
#        bash run-campaign.sh luna "8 10" "1 2" results/x "codenav-hint"
set -uo pipefail
here=$(cd "$(dirname "$0")" && pwd)
runner=$1 tasks=$2 reps=$3 campaign=$4 arms=${5:-"codenav plain"}
read -ra forward <<< "$arms"
backward=()
for (( i=${#forward[@]}-1; i>=0; i-- )); do backward+=("${forward[i]}"); done
mkdir -p "$campaign"
for rep in $reps; do
  for task in $tasks; do
    if (( (task + rep) % 2 == 0 )); then order=("${forward[@]}"); else order=("${backward[@]}"); fi
    for arm in "${order[@]}"; do
      echo "$(date +%T) $runner task $task rep $rep $arm"
      bash "$here/run-pilot.sh" "$runner" "$arm" "$task" "$rep" "$campaign" > /dev/null 2>> "$campaign/$runner-errors.log" \
        || echo "  failed (see $campaign/$runner-errors.log)"
    done
  done
done
echo "$(date +%T) $runner done"
