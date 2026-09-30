"""Spawn an installed MCP server console script and check the stdio handshake + tool list.

Usage: smoke_mcp_package.py <console-script> <expected-name>
Raw newline-delimited JSON-RPC, so it does not depend on the ``mcp`` client API version.
"""

import json
import subprocess
import sys


def main() -> int:
	command, expected = sys.argv[1], sys.argv[2]
	proc = subprocess.Popen(  # noqa: S603
		[command],
		stdin=subprocess.PIPE,
		stdout=subprocess.PIPE,
		stderr=subprocess.PIPE,
		text=True,
	)
	stdin, stdout = proc.stdin, proc.stdout
	if stdin is None or stdout is None:
		raise RuntimeError("stdio pipes missing")

	def request(message: dict) -> dict | None:
		stdin.write(json.dumps(message) + "\n")
		stdin.flush()
		if "id" not in message:
			return None
		return json.loads(stdout.readline())

	try:
		init = request(
			{
				"jsonrpc": "2.0",
				"id": 1,
				"method": "initialize",
				"params": {
					"protocolVersion": "2025-06-18",
					"capabilities": {},
					"clientInfo": {"name": "smoke", "version": "0"},
				},
			}
		)
		if not init or "result" not in init:
			raise RuntimeError(f"{expected}: initialize failed: {init}")
		request({"jsonrpc": "2.0", "method": "notifications/initialized"})
		tools = request({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
		if not tools or "result" not in tools:
			raise RuntimeError(f"{expected}: tools/list failed: {tools}")
		names = sorted(tool["name"] for tool in tools["result"]["tools"])
		if "symbol_info" not in names or "outline" not in names:
			raise RuntimeError(f"{expected}: unexpected tools {names}")
		print(f"{expected}: initialize ok, {len(names)} tools ({', '.join(names)})")
		return 0
	finally:
		proc.kill()
		err = proc.stderr.read() if proc.stderr else ""
		if err.strip():
			print(f"[{expected} stderr]\n{err.strip()[-600:]}", file=sys.stderr)


if __name__ == "__main__":
	sys.exit(main())
