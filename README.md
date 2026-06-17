# Hermes Maple Integration

A lightweight way to use [Maple Proxy](https://github.com/OpenSecretCloud/maple-proxy) from [Hermes Agent](https://hermes-agent.nousresearch.com/) **without making Maple your global/default Hermes model**.

This repo gives you four lanes:

| Lane | Command | Who is the LLM? | Separate Hermes profile? | Best for |
|---|---|---:|---:|---|
| Plugin slash command | `/maple chat ...` | Maple | No | quick non-sensitive Maple replies |
| Plugin file command | `/maple file /path --prompt ...` | Maple | No | local-file workflows where chat should only contain a path/instruction |
| Profile-backed agent | `/maple agent ...` or `maple-agent ...` | Maple | Yes | using Maple as the actual Hermes agent brain for one task |
| Resumable Maple thread | `/maple thread start ...` then `/maple thread ask ...` | Maple | Yes | multi-step Maple conversations without changing your default profile |

The main Hermes profile remains whatever you already use. Maple is available on demand.

## What gets installed

```text
$HERMES_HOME/plugins/maple/
  plugin.yaml
  __init__.py

~/.local/bin/maple-agent
```

The plugin registers:

```text
/maple status
/maple models
/maple chat <prompt>
/maple file <path> --prompt <instruction> [--output <path>]
/maple agent <path-or-prompt>
/maple thread start [--name NAME] <task-or-file>
/maple thread ask [--name NAME] <follow-up>
/maple thread status [--name NAME]
/maple thread end [--name NAME]

/maple-status
/maple-models
/maple-chat <prompt>
/maple-file <path> --prompt <instruction>
/maple-agent <path-or-prompt>
```

And tool calls:

```text
maple_health
maple_models
maple_chat
maple_file
```

Slash-command responses are visually labeled by default:

```text
🍁 Maple thread: project-a
━━━━━━━━━━━━━━━━━━━━━━━━
...
```

Set `MAPLE_RESPONSE_BADGE_ENABLED=0` to disable the label, or `MAPLE_RESPONSE_BADGE='Maple:'` to customize the default badge text.

## Prerequisites

1. Hermes Agent installed and working.
2. A local Maple Proxy running with OpenAI-compatible endpoints:

```text
GET  /health
GET  /v1/models
POST /v1/chat/completions
```

This integration assumes Maple Proxy is reachable at:

```text
http://127.0.0.1:8787/v1
```

Override with `MAPLE_BASE_URL` if needed.

3. If your Maple Proxy requires client auth, expose `MAPLE_API_KEY` to the Hermes gateway/profile environment. If the proxy itself is already started with the Maple key and local clients do not need auth, no client key is needed.

## Install

```bash
git clone https://github.com/LunarCannon/hermes-maple-integration.git
cd hermes-maple-integration
./scripts/install.sh
```

Then enable the plugin/toolset and restart Hermes:

```bash
hermes plugins enable maple
hermes tools enable maple
hermes tools enable --platform telegram maple   # if you use Telegram gateway
hermes gateway restart                          # if using gateway
```

For a CLI-only session, start a new `hermes` process after enabling tools.

## Configure the optional `maple-private` profile

The plugin commands `/maple chat` and `/maple file` do not require a separate profile. The `/maple agent` command does. Create it like this:

```bash
hermes profile create maple-private --no-alias \
  --description 'Maple-backed one-shot Hermes profile using local Maple Proxy.'

hermes --profile maple-private config set model.provider custom
hermes --profile maple-private config set model.default kimi-k2-6
hermes --profile maple-private config set model.base_url http://127.0.0.1:8787/v1
hermes --profile maple-private config set model.api_key '${MAPLE_API_KEY}'
hermes --profile maple-private config set memory.memory_enabled false
hermes --profile maple-private config set memory.user_profile_enabled false
hermes --profile maple-private config set privacy.redact_pii true
```

If your local proxy does not require client auth, you can omit `model.api_key`. If it does, put the key in an env file sourced by `maple-agent`, for example:

```bash
mkdir -p ~/.hermes/secrets
chmod 700 ~/.hermes/secrets
printf 'MAPLE_API_KEY=%s\n' 'your-key-here' > ~/.hermes/secrets/maple.env
chmod 600 ~/.hermes/secrets/maple.env
```

The included `maple-agent` wrapper sources:

```text
${HERMES_HOME:-~/.hermes}/secrets/maple.env
```

## Usage

### Health and model discovery

```text
/maple status
/maple models
```

CLI equivalents:

```bash
python - <<'PY'
from hermes_cli.plugins import PluginManager
m = PluginManager(); m.discover_and_load(force=True)
print(m._plugin_commands['maple-status']['handler'](''))
PY
```

### Quick Maple response

```text
/maple chat Explain BIP-39 checksums in two paragraphs.
```

This bypasses the main Hermes LLM. The gateway routes the slash command directly to the plugin handler, which calls Maple Proxy.

### File workflow

```text
/maple file /home/me/notes/input.md --prompt "Summarize this and remove direct identifiers." --output /tmp/maple-summary.md
```

The chat contains only the path/instruction. The plugin reads the local file and sends it to Maple.

### Profile-backed one-shot agent

```text
/maple agent /home/me/tasks/private-task.md
```

Or from shell:

```bash
maple-agent --file /home/me/tasks/private-task.md --prompt 'Complete this task and return only the final answer.'
```

This spawns:

```bash
hermes --profile maple-private chat -q ... -Q --toolsets file,terminal
```

It does **not** switch your current/default Hermes profile. It is a one-shot subprocess.

### Resumable Maple thread

Use this when you want multi-step follow-up while keeping Maple as the LLM/backend for the thread:

```text
/maple thread start [--name NAME] <task-or-file>
/maple thread ask [--name NAME] <follow-up>
/maple thread status [--name NAME]
/maple thread end [--name NAME]
```

Short aliases for the default thread:

```text
/maple start <task>
/maple ask <follow-up>
/maple end

/maple-start <task>
/maple-ask <follow-up>
/maple-end
```

Internally, `start` captures the Maple-backed Hermes `session_id`; `ask` resumes it with:

```bash
maple-agent --resume <session_id> "follow-up prompt"
```

Thread pointers are stored in:

```text
${HERMES_HOME:-~/.hermes}/maple/active-sessions.json
```

Current Hermes plugin command handlers receive raw args only, not platform chat/thread metadata. That means the `default` Maple thread is global to the Hermes home. In shared bot/group contexts, use `--name` to avoid collisions:

```text
/maple thread start --name project-a Review /path/to/file.md
/maple thread ask --name project-a clarify the second recommendation
/maple thread end --name project-a
```

## Privacy boundary

This integration is compartmentalization, not magic dust.

- `/maple chat` and `/maple file` bypass the main Hermes model, but the gateway still receives the slash-command message.
- `/maple file` is better than pasting sensitive text because chat contains a local path and instruction, not the whole document.
- `/maple agent` is a one-shot lane where Maple is the actual Hermes model backend for that subprocess.
- `/maple thread start/ask` is the multi-step version: it resumes the same Maple-backed Hermes session for follow-ups.
- Do not process seed phrases, private keys, signing keys, or mnemonic backups through any LLM.

## Troubleshooting

### Plugin does not show up

```bash
hermes plugins list --user --plain
hermes plugins enable maple
hermes tools enable maple
hermes gateway restart
```

Tool and plugin changes usually require a fresh session or gateway restart.

### Maple health fails

```bash
curl -fsS http://127.0.0.1:8787/health && echo
systemctl --user status maple-proxy.service
```

If your proxy is on another port:

```bash
export MAPLE_BASE_URL=http://127.0.0.1:3000/v1
```

### `maple-agent` gets HTTP 401

The `maple-private` profile is sending the wrong or no key. Either:

- start Maple Proxy so local clients do not need a client Authorization header, or
- set `MAPLE_API_KEY` in `${HERMES_HOME:-~/.hermes}/secrets/maple.env`, or
- set a literal/test key in the profile only if you understand the storage tradeoff.

## License

MIT
