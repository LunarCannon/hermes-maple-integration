"""Maple Proxy Hermes plugin.

Provides native tools and slash commands for routing selected Hermes requests to Maple Proxy.

Security model reminder:
- Slash commands avoid the main LLM seeing the command payload, but Telegram and
  Hermes gateway/session plumbing still see the message.
- Tool calls are visible to the current model in normal agent context.
- For privacy-ish data, prefer paths and local files over raw pasted content.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from datetime import datetime, timezone
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from tools.registry import tool_error, tool_result

DEFAULT_BASE_URLS = [
    "http://127.0.0.1:8787/v1",
    "http://127.0.0.1:3000/v1",
    "http://127.0.0.1:8080/v1",
]
DEFAULT_MODEL = os.environ.get("MAPLE_MODEL", "kimi-k2-6")
DEFAULT_TIMEOUT = float(os.environ.get("MAPLE_TIMEOUT", "300"))
HIGH_RISK_RE = re.compile(
    r"(?i)\b(seed phrase|mnemonic|private key|xprv|zprv|yprv|wallet seed|signing key|api[_ -]?key|bearer\s+[A-Za-z0-9._-]{12,})\b"
)
HERMES_HOME = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))).expanduser()
MAPLE_STATE_DIR = HERMES_HOME / "maple"
MAPLE_THREAD_STORE = Path(os.environ.get("MAPLE_THREAD_STORE", str(MAPLE_STATE_DIR / "active-sessions.json"))).expanduser()
SESSION_ID_RE = re.compile(r"^session_id:\s*(\S+)\s*$", re.MULTILINE)
RESUME_LINE_RE = re.compile(r"^↻ Resumed session .*$", re.MULTILINE)


class MaplePluginError(RuntimeError):
    pass


def _normalize_base_url(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    if not url:
        raise MaplePluginError("empty Maple base URL")
    if not url.endswith("/v1"):
        url = f"{url}/v1"
    return url


def _candidate_base_urls(explicit: Optional[str] = None) -> List[str]:
    urls: List[str] = []
    for raw in [explicit, os.environ.get("MAPLE_BASE_URL"), *DEFAULT_BASE_URLS]:
        if raw:
            url = _normalize_base_url(raw)
            if url not in urls:
                urls.append(url)
    return urls


def _health_url(base_url: str) -> str:
    return _normalize_base_url(base_url)[:-3] + "/health"


def _auth_headers(extra: Optional[dict] = None) -> dict:
    headers = dict(extra or {})
    # Prefer explicit client auth only if provided. The systemd service is
    # configured with MAPLE_API_KEY from ~/.hermes/secrets/maple.env, so in the
    # normal path no Authorization header is necessary and no key is loaded here.
    api_key = os.environ.get("MAPLE_API_KEY")
    if api_key and "Authorization" not in headers:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _request_json(method: str, url: str, *, body: Optional[dict] = None, timeout: float = DEFAULT_TIMEOUT) -> Any:
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = _auth_headers({"Accept": "application/json"})
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            if not raw.strip():
                return None
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        raise MaplePluginError(f"HTTP {e.code} from {url}: {raw[:1200]}") from e
    except urllib.error.URLError as e:
        raise MaplePluginError(f"could not reach {url}: {e.reason}") from e
    except TimeoutError as e:
        raise MaplePluginError(f"timeout reaching {url}") from e


def _detect_base_url(explicit: Optional[str] = None) -> str:
    failures: List[str] = []
    for base in _candidate_base_urls(explicit):
        try:
            _request_json("GET", _health_url(base), timeout=min(3.0, DEFAULT_TIMEOUT))
            return base
        except MaplePluginError as e:
            failures.append(f"{base}: {e}")
    raise MaplePluginError("no healthy Maple Proxy found. Tried:\n" + "\n".join(f"- {f}" for f in failures))


def _service_status() -> Dict[str, Any]:
    try:
        proc = subprocess.run(
            ["systemctl", "--user", "is-active", "maple-proxy.service"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5,
        )
        return {"available": True, "active": proc.stdout.strip(), "exit_code": proc.returncode}
    except Exception as e:
        return {"available": False, "error": f"{type(e).__name__}: {e}"}


def _health_payload(base_url: Optional[str] = None) -> Dict[str, Any]:
    base = _detect_base_url(base_url)
    health = _request_json("GET", _health_url(base), timeout=min(5.0, DEFAULT_TIMEOUT))
    return {"success": True, "base_url": base, "health": health, "service": _service_status()}


def _models_payload(base_url: Optional[str] = None) -> Dict[str, Any]:
    base = _detect_base_url(base_url)
    result = _request_json("GET", f"{base}/models")
    models = result.get("data", []) if isinstance(result, dict) else []
    ids = [m.get("id") for m in models if isinstance(m, dict) and m.get("id")]
    return {"success": True, "base_url": base, "models": ids, "raw": result}


def _stream_chat(
    *,
    prompt: str,
    model: str = DEFAULT_MODEL,
    system: Optional[str] = None,
    base_url: Optional[str] = None,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> Dict[str, Any]:
    if not prompt or not str(prompt).strip():
        raise MaplePluginError("prompt is required")
    if HIGH_RISK_RE.search(prompt):
        raise MaplePluginError(
            "Refusing direct Maple chat with high-risk secret-looking content. "
            "Put the material in a local file and use /maple file <path> --prompt <instruction>, "
            "or do not process seed/private-key material at all."
        )
    base = _detect_base_url(base_url)
    messages: List[Dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": str(system)})
    messages.append({"role": "user", "content": str(prompt)})
    payload: Dict[str, Any] = {"model": model or DEFAULT_MODEL, "messages": messages, "stream": True}
    if temperature is not None:
        payload["temperature"] = temperature
    if max_tokens is not None:
        payload["max_tokens"] = int(max_tokens)

    headers = _auth_headers({"Content-Type": "application/json", "Accept": "text/event-stream"})
    req = urllib.request.Request(
        f"{base}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    chunks: List[str] = []
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            for raw_line in resp:
                line = raw_line.decode("utf-8", "replace").strip()
                if not line or line.startswith(":") or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    event = json.loads(data)
                except json.JSONDecodeError:
                    continue
                for choice in event.get("choices", []) or []:
                    content = (choice.get("delta") or {}).get("content")
                    if content:
                        chunks.append(content)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        raise MaplePluginError(f"HTTP {e.code} from Maple chat: {raw[:1500]}") from e
    except urllib.error.URLError as e:
        raise MaplePluginError(f"could not reach Maple chat endpoint at {base}: {e.reason}") from e
    text = "".join(chunks)
    return {"success": True, "base_url": base, "model": model or DEFAULT_MODEL, "text": text}


def _write_optional_output(result: Dict[str, Any], output_path: Optional[str]) -> Dict[str, Any]:
    if not output_path:
        return result
    path = Path(output_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(result.get("text", ""), encoding="utf-8")
    result = dict(result)
    result["output_path"] = str(path)
    # Keep text out of tool result when caller asks for an artifact.
    result["text_chars"] = len(result.get("text", ""))
    result.pop("text", None)
    return result


def _handle_maple_health(args: dict, **kw) -> str:
    try:
        return tool_result(_health_payload(args.get("base_url")))
    except Exception as e:
        return tool_error(f"Maple health failed: {e}")


def _handle_maple_models(args: dict, **kw) -> str:
    try:
        return tool_result(_models_payload(args.get("base_url")))
    except Exception as e:
        return tool_error(f"Maple models failed: {e}")


def _handle_maple_chat(args: dict, **kw) -> str:
    try:
        result = _stream_chat(
            prompt=str(args.get("prompt") or ""),
            model=str(args.get("model") or DEFAULT_MODEL),
            system=args.get("system"),
            base_url=args.get("base_url"),
            temperature=args.get("temperature"),
            max_tokens=args.get("max_tokens"),
            timeout=float(args.get("timeout") or DEFAULT_TIMEOUT),
        )
        return tool_result(_write_optional_output(result, args.get("output_path")))
    except Exception as e:
        return tool_error(f"Maple chat failed: {e}")


def _handle_maple_file(args: dict, **kw) -> str:
    try:
        raw_path = str(args.get("file_path") or args.get("path") or "").strip()
        if not raw_path:
            return tool_error("file_path is required")
        path = Path(raw_path).expanduser()
        if not path.exists() or not path.is_file():
            return tool_error(f"file not found: {path}")
        instruction = str(args.get("prompt") or args.get("instruction") or "Summarize this file.")
        text = path.read_text(encoding="utf-8", errors="replace")
        prompt = f"{instruction}\n\n--- file: {path} ---\n{text}"
        result = _stream_chat(
            prompt=prompt,
            model=str(args.get("model") or DEFAULT_MODEL),
            system=args.get("system"),
            base_url=args.get("base_url"),
            temperature=args.get("temperature"),
            max_tokens=args.get("max_tokens"),
            timeout=float(args.get("timeout") or DEFAULT_TIMEOUT),
        )
        result["input_path"] = str(path)
        return tool_result(_write_optional_output(result, args.get("output_path")))
    except Exception as e:
        return tool_error(f"Maple file failed: {e}")


def _json_or_text(handler, args: dict, *, text_key: str = "text") -> str:
    raw = handler(args)
    try:
        data = json.loads(raw)
    except Exception:
        return raw
    if not data.get("success"):
        return data.get("error") or data.get("message") or json.dumps(data, indent=2)
    if text_key in data:
        return data[text_key] or "(empty Maple response)"
    return json.dumps(data, indent=2, sort_keys=True)


def _format_status() -> str:
    try:
        data = _health_payload()
    except Exception as e:
        return f"Maple: not healthy\n{e}"
    service = data.get("service", {})
    health = data.get("health", {})
    return (
        "Maple: healthy\n"
        f"base_url: {data.get('base_url')}\n"
        f"service: {service.get('active', 'unknown')}\n"
        f"proxy: {health.get('service')} {health.get('version')} status={health.get('status')}\n"
        f"default_model: {DEFAULT_MODEL}"
    )


def _format_models() -> str:
    try:
        data = _models_payload()
    except Exception as e:
        return f"Maple models failed: {e}"
    return "Maple models:\n" + "\n".join(f"- {m}" for m in data.get("models", []))


def _split_file_args(raw: str) -> Dict[str, str]:
    raw = raw.strip()
    if not raw:
        raise MaplePluginError("Usage: /maple file <path> --prompt <instruction>")
    try:
        parts = shlex.split(raw)
    except ValueError as e:
        raise MaplePluginError(f"Could not parse args: {e}") from e
    if not parts:
        raise MaplePluginError("Usage: /maple file <path> --prompt <instruction>")
    file_path = parts[0]
    prompt_parts: List[str] = []
    output_path = ""
    model = ""
    i = 1
    while i < len(parts):
        tok = parts[i]
        if tok in {"--prompt", "-p"} and i + 1 < len(parts):
            prompt_parts.append(parts[i + 1]); i += 2; continue
        if tok in {"--output", "-o"} and i + 1 < len(parts):
            output_path = parts[i + 1]; i += 2; continue
        if tok == "--model" and i + 1 < len(parts):
            model = parts[i + 1]; i += 2; continue
        prompt_parts.append(tok); i += 1
    return {"file_path": file_path, "prompt": " ".join(prompt_parts).strip() or "Summarize this file.", "output_path": output_path, "model": model}


def _maple_slash(raw_args: str) -> str:
    raw = (raw_args or "").strip()
    if not raw or raw in {"help", "--help", "-h"}:
        return (
            "Maple commands:\n"
            "/maple status\n"
            "/maple models\n"
            "/maple chat <non-sensitive prompt>\n"
            "/maple file <path> --prompt <instruction> [--output <path>]\n"
            "/maple agent <path-or-prompt>  (one-shot maple-private profile)\n"
            "/maple thread start [--name NAME] <task>\n"
            "/maple thread ask [--name NAME] <follow-up>\n"
            "/maple thread status [--name NAME]\n"
            "/maple thread end [--name NAME]\n"
            "Aliases: /maple start, /maple ask, /maple end"
        )
    sub, _, rest = raw.partition(" ")
    sub = sub.lower()
    if sub in {"status", "health"}:
        return _format_status()
    if sub in {"models", "model"}:
        return _format_models()
    if sub == "chat":
        if not rest.strip():
            return "Usage: /maple chat <non-sensitive prompt>"
        return _json_or_text(_handle_maple_chat, {"prompt": rest.strip()})
    if sub == "file":
        try:
            parsed = _split_file_args(rest)
            args = {"file_path": parsed["file_path"], "prompt": parsed["prompt"]}
            if parsed.get("output_path"):
                args["output_path"] = parsed["output_path"]
            if parsed.get("model"):
                args["model"] = parsed["model"]
            return _json_or_text(_handle_maple_file, args)
        except Exception as e:
            return f"Maple file failed: {e}"
    if sub == "agent":
        return _run_maple_agent(rest.strip())
    if sub == "thread":
        return _maple_thread_slash(rest.strip())
    if sub == "start":
        return _thread_start(rest.strip())
    if sub == "ask":
        return _thread_ask(rest.strip())
    if sub == "end":
        return _thread_end(rest.strip())
    return f"Unknown /maple subcommand: {sub}\nTry /maple help"


def _run_maple_agent(raw: str) -> str:
    if not raw:
        return "Usage: /maple agent <path-or-prompt>"
    try:
        argv = shlex.split(raw)
    except ValueError as e:
        return f"maple-agent args parse failed: {e}"
    if not argv:
        return "Usage: /maple agent <path-or-prompt>"
    rc, output = _run_maple_agent_argv(argv)
    if rc != 0:
        return f"maple-agent failed ({rc}):\n{output[-2000:]}"
    return _clean_agent_output(output) or "maple-agent completed with no output."


def _maple_agent_wrapper() -> Path:
    wrapper = Path.home() / ".local/bin/maple-agent"
    if not wrapper.exists():
        raise MaplePluginError("maple-agent wrapper is not installed yet.")
    return wrapper


def _run_maple_agent_argv(argv: List[str], timeout: int = 600) -> Tuple[int, str]:
    wrapper = _maple_agent_wrapper()
    try:
        proc = subprocess.run(
            [str(wrapper), *argv],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        partial = (e.stdout or "") if isinstance(e.stdout, str) else ""
        return 124, (partial + "\nmaple-agent timed out after 10 minutes.").strip()
    return proc.returncode, proc.stdout or ""


def _clean_agent_output(output: str) -> str:
    text = SESSION_ID_RE.sub("", output or "")
    text = RESUME_LINE_RE.sub("", text)
    return "\n".join(line for line in text.splitlines() if line.strip()).strip()


def _extract_session_id(output: str) -> str:
    matches = SESSION_ID_RE.findall(output or "")
    return matches[-1] if matches else ""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _thread_store_load() -> Dict[str, Dict[str, Any]]:
    try:
        if not MAPLE_THREAD_STORE.exists():
            return {}
        data = json.loads(MAPLE_THREAD_STORE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _thread_store_save(data: Dict[str, Dict[str, Any]]) -> None:
    MAPLE_THREAD_STORE.parent.mkdir(parents=True, exist_ok=True)
    tmp = MAPLE_THREAD_STORE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(MAPLE_THREAD_STORE)
    try:
        MAPLE_THREAD_STORE.chmod(0o600)
    except Exception:
        pass


def _normalize_thread_name(name: str) -> str:
    name = (name or "default").strip()
    name = re.sub(r"[^A-Za-z0-9_.-]+", "-", name).strip("-._")
    return name[:80] or "default"


def _extract_name(raw: str) -> Tuple[str, str]:
    """Parse optional --name/-n from the start of raw args."""
    try:
        parts = shlex.split(raw or "")
    except ValueError:
        return "default", raw.strip()
    if len(parts) >= 2 and parts[0] in {"--name", "-n"}:
        name = _normalize_thread_name(parts[1])
        # Slash handlers receive one raw arg string. After consuming --name,
        # treat the remaining tokens as a natural-language prompt unless the
        # prompt intentionally starts with a wrapper flag like --file.
        rest = " ".join(parts[2:]).strip()
        return name, rest
    return "default", (raw or "").strip()


def _task_argv(raw_task: str) -> List[str]:
    raw_task = (raw_task or "").strip()
    if not raw_task:
        return []
    # Preserve normal free-form prompts as one argument. Let advanced users pass
    # wrapper flags by starting with a flag, e.g. --file path --prompt ...
    if raw_task.startswith("-"):
        try:
            return shlex.split(raw_task)
        except ValueError:
            return [raw_task]
    return [raw_task]


def _thread_start(raw: str) -> str:
    name, task = _extract_name(raw)
    if not task:
        return "Usage: /maple thread start [--name NAME] <task-or-file>"
    rc, output = _run_maple_agent_argv(_task_argv(task))
    clean = _clean_agent_output(output)
    if rc != 0:
        return f"Maple thread start failed ({rc}):\n{output[-2000:]}"
    session_id = _extract_session_id(output)
    if not session_id:
        return "Maple thread start failed: no session_id returned.\n" + clean[-2000:]
    store = _thread_store_load()
    now = _now_iso()
    store[name] = {
        "session_id": session_id,
        "created_at": now,
        "updated_at": now,
        "last_prompt": task[:500],
    }
    _thread_store_save(store)
    return f"Started Maple thread `{name}` ({session_id}).\n\n{clean}".strip()


def _thread_ask(raw: str) -> str:
    name, prompt = _extract_name(raw)
    if not prompt:
        return "Usage: /maple thread ask [--name NAME] <follow-up>"
    store = _thread_store_load()
    entry = store.get(name)
    if not entry:
        return f"No active Maple thread `{name}`. Start one with: /maple thread start [--name {name}] <task>"
    session_id = str(entry.get("session_id") or "")
    if not session_id:
        return f"Maple thread `{name}` is missing a session_id. End it and start a new one."
    rc, output = _run_maple_agent_argv(["--resume", session_id, prompt])
    clean = _clean_agent_output(output)
    if rc != 0:
        return f"Maple thread ask failed ({rc}):\n{output[-2000:]}"
    new_session_id = _extract_session_id(output) or session_id
    entry.update({"session_id": new_session_id, "updated_at": _now_iso(), "last_prompt": prompt[:500]})
    store[name] = entry
    _thread_store_save(store)
    return clean or "(empty Maple thread response)"


def _thread_status(raw: str = "") -> str:
    name, _ = _extract_name(raw)
    store = _thread_store_load()
    if not store:
        return "No active Maple threads. Start one with: /maple thread start <task>"
    if name != "default" or raw.strip().startswith(("--name", "-n")):
        entry = store.get(name)
        if not entry:
            return f"No active Maple thread `{name}`."
        return (
            f"Maple thread `{name}`\n"
            f"session_id: {entry.get('session_id')}\n"
            f"created_at: {entry.get('created_at')}\n"
            f"updated_at: {entry.get('updated_at')}\n"
            f"last_prompt: {entry.get('last_prompt', '')[:180]}"
        )
    lines = ["Active Maple threads:"]
    for key, entry in sorted(store.items()):
        lines.append(f"- {key}: {entry.get('session_id')} updated={entry.get('updated_at')}")
    return "\n".join(lines)


def _thread_end(raw: str = "") -> str:
    name, _ = _extract_name(raw)
    store = _thread_store_load()
    if name not in store:
        return f"No active Maple thread `{name}`."
    sid = store[name].get("session_id")
    del store[name]
    _thread_store_save(store)
    return f"Ended Maple thread `{name}` ({sid})."


def _maple_thread_slash(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw or raw in {"help", "--help", "-h"}:
        return (
            "Maple thread commands:\n"
            "/maple thread start [--name NAME] <task-or-file>\n"
            "/maple thread ask [--name NAME] <follow-up>\n"
            "/maple thread status [--name NAME]\n"
            "/maple thread end [--name NAME]\n"
            "Short aliases: /maple start, /maple ask, /maple end"
        )
    sub, _, rest = raw.partition(" ")
    sub = sub.lower()
    if sub in {"start", "new", "begin"}:
        return _thread_start(rest.strip())
    if sub in {"ask", "send", "continue", "reply"}:
        return _thread_ask(rest.strip())
    if sub in {"status", "list", "ls"}:
        return _thread_status(rest.strip())
    if sub in {"end", "stop", "close", "clear"}:
        return _thread_end(rest.strip())
    return f"Unknown /maple thread subcommand: {sub}\nTry /maple thread help"


def register(ctx) -> None:
    ctx.register_tool("maple_health", "maple", MAPLE_HEALTH_SCHEMA, _handle_maple_health, check_fn=lambda: True, emoji="🍁")
    ctx.register_tool("maple_models", "maple", MAPLE_MODELS_SCHEMA, _handle_maple_models, check_fn=lambda: True, emoji="🍁")
    ctx.register_tool("maple_chat", "maple", MAPLE_CHAT_SCHEMA, _handle_maple_chat, check_fn=lambda: True, emoji="🍁")
    ctx.register_tool("maple_file", "maple", MAPLE_FILE_SCHEMA, _handle_maple_file, check_fn=lambda: True, emoji="🍁")
    ctx.register_command("maple", _maple_slash, description="Maple Proxy status, models, chat, file, agent, and stateful thread commands.", args_hint="status|models|chat|file|agent|thread ...")
    ctx.register_command("maple-status", lambda raw: _format_status(), description="Check Maple Proxy health.")
    ctx.register_command("maple-models", lambda raw: _format_models(), description="List Maple models.")
    ctx.register_command("maple-chat", lambda raw: _json_or_text(_handle_maple_chat, {"prompt": raw.strip()}), description="Run a non-sensitive prompt through Maple/Kimi.", args_hint="<prompt>")
    ctx.register_command("maple-file", lambda raw: _json_or_text(_handle_maple_file, _split_file_args(raw)), description="Run a local file through Maple/Kimi.", args_hint="<path> --prompt <instruction>")
    ctx.register_command("maple-agent", lambda raw: _run_maple_agent(raw.strip()), description="Spawn the maple-private Hermes profile via maple-agent.", args_hint="<path-or-prompt>")
    ctx.register_command("maple-thread", lambda raw: _maple_thread_slash(raw.strip()), description="Manage a resumable Maple-backed Hermes thread.", args_hint="start|ask|status|end ...")
    ctx.register_command("maple-start", lambda raw: _thread_start(raw.strip()), description="Start the default resumable Maple thread.", args_hint="<task>")
    ctx.register_command("maple-ask", lambda raw: _thread_ask(raw.strip()), description="Ask a follow-up in the default Maple thread.", args_hint="<follow-up>")
    ctx.register_command("maple-end", lambda raw: _thread_end(raw.strip()), description="End the default Maple thread.")


MAPLE_HEALTH_SCHEMA = {
    "name": "maple_health",
    "description": "Check local Maple Proxy health and service status.",
    "parameters": {
        "type": "object",
        "properties": {"base_url": {"type": "string", "description": "Optional Maple base URL, default http://127.0.0.1:8787/v1"}},
        "required": [],
    },
}

MAPLE_MODELS_SCHEMA = {
    "name": "maple_models",
    "description": "List available Maple models from local Maple Proxy.",
    "parameters": {
        "type": "object",
        "properties": {"base_url": {"type": "string", "description": "Optional Maple base URL."}},
        "required": [],
    },
}

MAPLE_CHAT_SCHEMA = {
    "name": "maple_chat",
    "description": "Run a non-sensitive prompt through Maple Proxy. Prefer maple_file for sensitive-ish content.",
    "parameters": {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "Prompt text. Do not include secrets, seed phrases, private keys, or raw PII."},
            "model": {"type": "string", "description": "Maple model; defaults to kimi-k2-6."},
            "system": {"type": "string", "description": "Optional system prompt."},
            "max_tokens": {"type": "integer"},
            "temperature": {"type": "number"},
            "output_path": {"type": "string", "description": "Optional local path to write the response instead of returning full text."},
            "base_url": {"type": "string"},
            "timeout": {"type": "number"},
        },
        "required": ["prompt"],
    },
}

MAPLE_FILE_SCHEMA = {
    "name": "maple_file",
    "description": "Run a local file through Maple Proxy using a short instruction. Preferred for privacy-ish content because the chat only needs the file path.",
    "parameters": {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "Local file path to read and send to Maple."},
            "prompt": {"type": "string", "description": "Instruction for Maple."},
            "model": {"type": "string", "description": "Maple model; defaults to kimi-k2-6."},
            "system": {"type": "string"},
            "max_tokens": {"type": "integer"},
            "temperature": {"type": "number"},
            "output_path": {"type": "string", "description": "Optional local path to write the response instead of returning full text."},
            "base_url": {"type": "string"},
            "timeout": {"type": "number"},
        },
        "required": ["file_path"],
    },
}
