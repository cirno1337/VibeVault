#!/usr/bin/env bash
# Hooks the VibeVault collector into Claude Code for the current user and installs the
# systemd safety-net timer. Idempotent; backs up ~/.claude/settings.json first.
# Assumes the collector lives in ~/.local/share/vibevault/venv and is configured in
# ~/.config/vibevault/collector.toml.
set -euo pipefail
COLLECTOR="$HOME/.local/share/vibevault/venv/bin/vibevault-collector"
SETTINGS="$HOME/.claude/settings.json"
[ -x "$COLLECTOR" ] || { echo "collector not found: $COLLECTOR" >&2; exit 1; }
mkdir -p "$(dirname "$SETTINGS")"
[ -f "$SETTINGS" ] || echo '{}' > "$SETTINGS"
cp "$SETTINGS" "$SETTINGS.bak-vibevault-$(date +%Y%m%d%H%M%S)"

python3 - "$SETTINGS" "$COLLECTOR hook" <<'EOF'
import json, sys
path, cmd = sys.argv[1], sys.argv[2]
with open(path) as f:
    s = json.load(f)
hook = {"type": "command", "command": cmd, "timeout": 5}
hooks = s.setdefault("hooks", {})
for ev in ["SessionStart", "UserPromptSubmit", "PostToolUse", "Stop", "SessionEnd"]:
    lst = hooks.setdefault(ev, [])
    if any(h.get("command") == cmd for e in lst for h in e.get("hooks", [])):
        continue
    entry = {"hooks": [hook]}
    if ev == "PostToolUse":
        entry = {"matcher": "*", "hooks": [hook]}
    lst.append(entry)
with open(path, "w") as f:
    json.dump(s, f, indent=2)
    f.write("\n")
EOF
echo "hooks added to $SETTINGS"

UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"
cat > "$UNIT_DIR/vibevault-collector.service" <<'EOF'
[Unit]
Description=VibeVault collector sync
[Service]
Type=oneshot
ExecStart=%h/.local/share/vibevault/venv/bin/vibevault-collector sync --quiet
EOF
cat > "$UNIT_DIR/vibevault-collector.timer" <<'EOF'
[Unit]
Description=VibeVault collector sync timer
[Timer]
OnBootSec=1min
OnUnitActiveSec=2min
[Install]
WantedBy=timers.target
EOF
systemctl --user daemon-reload
systemctl --user enable --now vibevault-collector.timer
echo "timer enabled"
