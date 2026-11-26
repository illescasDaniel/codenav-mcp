"""server.json (MCP Registry manifest) stays in sync with pyproject.toml."""

import json
import pathlib
import tomllib


ROOT = pathlib.Path(__file__).resolve().parent.parent


def _server_json() -> dict:
	return json.loads((ROOT / "server.json").read_text())


def _pyproject() -> dict:
	return tomllib.loads((ROOT / "pyproject.toml").read_text())


def test_given_manifest_then_name_version_and_description_match_pyproject():
	manifest = _server_json()
	project = _pyproject()["project"]
	assert manifest["version"] == project["version"]
	assert manifest["description"] == " ".join(project["description"].split())
	assert len(manifest["description"]) <= 100
	assert manifest["name"] == "io.github.illescasDaniel/codenav-mcp"


def test_given_manifest_then_package_entry_matches_the_published_package():
	manifest = _server_json()
	package = manifest["packages"][0]
	assert package["registryType"] == "pypi"
	assert package["identifier"] == "codenav-mcp"
	assert package["version"] == manifest["version"]
	assert package["transport"]["type"] == "stdio"


def test_given_documented_settings_then_each_is_declared_in_the_manifest():
	manifest = _server_json()
	declared = {env["name"] for env in manifest["packages"][0]["environmentVariables"]}
	configured = {
		cell.strip().strip("`")
		for line in (ROOT / "docs/configuration.md").read_text().splitlines()
		if line.startswith("| `CODENAV_MCP_")
		for cell in [line.split("|")[1]]
	}
	assert declared == configured
