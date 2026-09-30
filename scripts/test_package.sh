#!/usr/bin/env bash

set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/.." && pwd)"

usage() {
	cat <<'EOF2'
Install this package's current version from the index into a fresh venv
(outside the repo) and run an MCP stdio handshake against its console script.

Usage: scripts/test_package.sh [options]

Options:
  --testpypi   Install from https://test.pypi.org (dependencies still come from PyPI)
  -h, --help   Show this help
EOF2
}

install_args=()
while [[ $# -gt 0 ]]; do
	case "$1" in
	--testpypi)
		# PyPI first, TestPyPI only as a fallback, so a squatted dependency on
		# TestPyPI can never shadow the real one.
		install_args=(--default-index https://test.pypi.org/simple/ --index https://pypi.org/simple/)
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

read -r name version < <(python3 -c "
import tomllib
p = tomllib.load(open('${repo_root}/pyproject.toml', 'rb'))['project']
print(p['name'], p['version'])")

work="$(mktemp -d)"
trap 'rm -rf "${work}"' EXIT
uv venv --quiet "${work}/venv"

echo "Installing ${name}==${version}"
uv pip install --python "${work}/venv" ${install_args[@]+"${install_args[@]}"} "${name}==${version}"

# Run from a throwaway project so nothing in this checkout can satisfy a lookup.
mkdir -p "${work}/project"
printf 'def hello() -> str:\n\treturn "hi"\n' >"${work}/project/app.py"
printf '.card { color: red; }\n' >"${work}/project/app.css"
cd "${work}/project"
"${work}/venv/bin/python" "${script_dir}/smoke_mcp_package.py" "${work}/venv/bin/${name}" "${name}"
echo "OK"
