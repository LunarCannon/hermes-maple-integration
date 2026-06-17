#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
hermes_home="${HERMES_HOME:-$HOME/.hermes}"

mkdir -p "$hermes_home/plugins/maple" "$HOME/.local/bin"
cp "$repo_dir/plugins/maple/plugin.yaml" "$hermes_home/plugins/maple/plugin.yaml"
cp "$repo_dir/plugins/maple/__init__.py" "$hermes_home/plugins/maple/__init__.py"
cp "$repo_dir/bin/maple-agent" "$HOME/.local/bin/maple-agent"
chmod 700 "$HOME/.local/bin/maple-agent"

echo "Installed Maple plugin to $hermes_home/plugins/maple"
echo "Installed maple-agent to $HOME/.local/bin/maple-agent"
echo
echo "Next steps:"
echo "  hermes plugins enable maple"
echo "  hermes tools enable maple"
echo "  hermes tools enable --platform telegram maple   # optional gateway platform"
echo "  hermes gateway restart                          # if using gateway"
