#!/usr/bin/env bash

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

usage() {
	cat <<'EOF2'
Build and upload this package to PyPI (or TestPyPI).

Usage: scripts/publish.sh [options]

Options:
  --testpypi        Upload to https://test.pypi.org instead of https://pypi.org
  --skip-existing   Skip files already on the index (re-run after a partial upload)
  --build-only      Build and `twine check` into dist/, upload nothing
  -y, --yes         Pass twine --non-interactive (no credential prompts)
  -h, --help        Show this help

Credentials come from ~/.pypirc ([pypi] / [testpypi] sections) or
TWINE_USERNAME / TWINE_PASSWORD. An index never accepts the same version
twice, so bump `version` in pyproject.toml first.

Install upload tools: uv sync --group uploader   (task: sync-uploader)
Verify a published version: uv run task test-package [-- --testpypi]
EOF2
}

twine_args=()
build_only=false
while [[ $# -gt 0 ]]; do
	case "$1" in
	--testpypi)
		twine_args+=(--repository testpypi)
		shift
		;;
	--skip-existing)
		twine_args+=(--skip-existing)
		shift
		;;
	--build-only)
		build_only=true
		shift
		;;
	-y | --yes)
		twine_args+=(--non-interactive)
		shift
		;;
	-h | --help)
		usage
		exit 0
		;;
	*)
		echo "error: unknown option: $1" >&2
		usage >&2
		exit 1
		;;
	esac
done

cd "${repo_root}"

if ! uv run python -c "import twine" 2>/dev/null; then
	echo "error: missing upload dependencies; run: uv sync --group uploader" >&2
	exit 1
fi

pypirc="${HOME}/.pypirc"
if [[ ! -f "${pypirc}" && -z "${TWINE_PASSWORD:-}" ]]; then
	echo "warning: no ${pypirc} and TWINE_PASSWORD is unset; twine will prompt for credentials" >&2
elif [[ -f "${pypirc}" ]]; then
	pypirc_mode="$(stat -c '%a' "${pypirc}" 2>/dev/null || stat -f '%OLp' "${pypirc}")"
	if [[ "${pypirc_mode}" != "600" ]]; then
		echo "warning: ${pypirc} permissions are ${pypirc_mode}; chmod 600 is recommended" >&2
	fi
fi

rm -rf dist
uv build
uv run twine check dist/*

if [[ "${build_only}" == "true" ]]; then
	echo "Built (not uploaded): ${repo_root}/dist"
	exit 0
fi

uv run twine upload ${twine_args[@]+"${twine_args[@]}"} dist/*
