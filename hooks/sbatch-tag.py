#!/usr/bin/env python3
"""Claude Code PostToolUse hook (Bash): tag the session with an Euler job whenever a command submits one with sbatch."""
import json
import re
import subprocess
import sys
from pathlib import Path

hook = json.load(sys.stdin)
cmd = hook.get("tool_input", {}).get("command", "")
if hook.get("tool_name") != "Bash" or "sbatch" not in cmd:
    sys.exit(0)
name = re.search(r"(?:--job-name[= ]|-J ?)['\"]?([\w.-]+)", cmd)
what = name.group(1) if name else "sbatch job"
cc_job = Path(__file__).resolve().parent.parent / "cc-job"
for job in dict.fromkeys(re.findall(r"Submitted batch job (\d+)", json.dumps(hook.get("tool_response", "")))):
    subprocess.run([str(cc_job), "--session", hook["session_id"], "add", "euler", what, "--job", job], capture_output=True)
