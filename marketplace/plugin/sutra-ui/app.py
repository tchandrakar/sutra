"""Sutra UI — read-only local governance dashboard (Step 1: Panel A turn feed).

One FastAPI process: serves the static page, exposes a state snapshot, a paged
log read, and an SSE live-tail. Reads only — never writes a governance file.
Run: python3 -m uvicorn app:app --host 127.0.0.1 --port 7000
"""
import asyncio
import fcntl
import json
import os
import pty
import signal
import struct
import termios
from pathlib import Path

from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

import log_reader as lr
import session_reader as sr
import org_api
import providers

app = FastAPI(title="Sutra UI", docs_url=None, redoc_url=None)

# --- DNS-rebinding defence -------------------------------------------------
# Binding to 127.0.0.1 keeps other machines out; it does NOT keep out a page
# the operator visits. A hostile site can point its own DNS name at 127.0.0.1
# and reach this server through the browser -- and then the Host header is the
# attacker's name, not ours. Reject any Host that is not literal loopback.
# TrustedHostMiddleware covers websocket scopes as well as http.
ALLOWED_HOSTS = [h.strip() for h in
                 os.environ.get("SUTRA_UI_ALLOWED_HOSTS", "127.0.0.1,localhost,[::1]").split(",")
                 if h.strip()]
app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)

app.include_router(org_api.router)
HERE = Path(__file__).resolve().parent


def _origin_ok(ws):
    """Same-origin gate for the websockets.

    The browser same-origin policy does NOT cover WebSocket handshakes and no
    preflight is sent, so without this any page the operator visits can open
    ws://127.0.0.1:<port>/ws/chat, drive the agent with its own prompt and read
    every token frame back. /ws/term is worse -- it writes attacker bytes
    straight into the PTY. Loopback binding stops other machines, not the
    operator's own browser. Allow only loopback origins.

    A missing Origin means a non-browser client (curl, the test suite, the
    Electron shell). Per RFC 6455 a browser MUST send Origin on a cross-origin
    handshake and a page cannot suppress it, so absent-Origin is not a
    browser-reachable bypass.
    """
    origin = ws.headers.get("origin")
    if not origin:
        return True
    extra = [o.strip() for o in
             os.environ.get("SUTRA_UI_ALLOWED_ORIGINS", "").split(",") if o.strip()]
    if origin in extra:
        return True
    try:
        u = urlparse(origin)
    except ValueError:
        return False
    return u.scheme in ("http", "https") and u.hostname in ("127.0.0.1", "localhost", "::1")


async def _reject_cross_origin(ws):
    """Deny a disallowed handshake BEFORE accept(). Returns True if rejected.

    close() before accept() denies the handshake outright (the client sees a
    403) -- never accept a socket we intend to refuse.
    """
    if _origin_ok(ws):
        return False
    await ws.close(code=1008)
    return True

# persistent (non-transient) marker files for the state panel — see README §4
STATE_MARKERS = ("active-role", "structure-first-active", ".last-reset-ts")

# --- chat wrapper config: drives an AI CLI as a subprocess (Max-plan auth, no API key) ---
# CLAUDE_BIN is the ws_term (PTY) default and the back-compatible env name.
# ws_chat no longer uses it: it resolves the ACTIVE provider through
# providers.py on every connect, so switching providers in the UI takes effect
# on the next message instead of on the next server restart.
CLAUDE_BIN = os.environ.get("SUTRA_UI_CLAUDE_BIN", "claude")
WORKDIR = os.path.expanduser(os.environ.get("SUTRA_UI_WORKDIR", "~/sutra-ui-workspace"))
# Module-level default, kept for the env-var contract (SAFETY rule 4 /
# test_perm_mode_default). The live value ws_chat sends is read per-connect
# from ~/.sutra-ui/settings.json, which falls back to exactly this env var.
PERM_MODE = os.environ.get("SUTRA_UI_PERMISSION_MODE", "plan")
INIT_CMD = os.environ.get("SUTRA_UI_INIT", "/core:start")          # run every fresh session so Sutra fires
AUTO_CAVEMAN = os.environ.get("SUTRA_UI_AUTO_CAVEMAN", "1") == "1"  # token-saving default (non-Max friendly)
INIT_DELAY = float(os.environ.get("SUTRA_UI_INIT_DELAY", "3.5"))    # secs to let the TUI boot before typing


def _ensure_workdir(path=None):
    """Both socket handlers spawn a subprocess with cwd=<workdir>. If that
    directory does not exist, create_subprocess_exec raises FileNotFoundError
    BEFORE a single frame is written, the socket dies, and the operator sees a
    UI that simply does nothing -- no error text, no output, no clue. WORKDIR
    defaults to ~/sutra-ui-workspace, which nothing else on the system creates,
    so on a fresh machine that was the guaranteed state. ws_chat did the
    makedirs; ws_term did not. Both paths go through here now.

    Returns the usable directory, or None if it cannot be created -- the caller
    reports that to the client rather than dying mid-handshake."""
    target = path or WORKDIR
    try:
        os.makedirs(target, exist_ok=True)
    except OSError:
        return None
    return target if os.path.isdir(target) else None


# Create it ONCE, at import, rather than only on the first socket connect.
# The per-handler calls below stay (a directory can be removed while the server
# runs), but doing it here means the failure is visible in the server's own
# startup rather than as a socket that dies mid-handshake on the first message
# the operator ever sends. None => could not be created; the handlers still
# report that to the client instead of raising FileNotFoundError from
# create_subprocess_exec.
WORKDIR_READY = _ensure_workdir()


def _panel_html() -> str:
    """The Tier-3 org/reorg studio: the reviewed design shell, wired to the real
    /api/org/* endpoints (org_api.py -> placement_engine.py). Markup and CSS
    are byte-identical to the reviewed design; only the data layer differs
    (seed constants replaced with fetch()).
    """
    return (HERE / "static" / "panel.html").read_text(encoding="utf-8")


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    """THE app. This previously served term.html (the xterm console), so the
    front door showed a completely different UI from the studio, and the studio
    was reachable only if you already knew to type /panel. Anyone who opened
    the server saw the wrong product. The studio IS the app; the older
    surfaces remain reachable under /legacy/* below.
    """
    return _panel_html()


@app.get("/panel", response_class=HTMLResponse)
def panel_page() -> str:
    """Alias for /, so existing links and bookmarks keep working."""
    return _panel_html()


# --- legacy surfaces -------------------------------------------------------
# Pre-existing dashboards, moved off the front door rather than deleted --
# they are working tools that predate this work, not mine to remove. The old
# paths still resolve so nothing that linked to them breaks.

@app.get("/legacy/term", response_class=HTMLResponse)
def legacy_term() -> str:
    return (HERE / "static" / "term.html").read_text(encoding="utf-8")


@app.get("/legacy/panels", response_class=HTMLResponse)
@app.get("/panels", response_class=HTMLResponse)
def panels() -> str:
    return (HERE / "static" / "index.html").read_text(encoding="utf-8")


@app.get("/legacy/sessions", response_class=HTMLResponse)
@app.get("/sessions", response_class=HTMLResponse)
def sessions_page() -> str:
    return (HERE / "static" / "sessions.html").read_text(encoding="utf-8")


@app.get("/api/sessions")
def api_sessions(limit: int = 100):
    return sr.list_sessions(limit)


@app.get("/api/sessions/{sid}")
def api_session(sid: str):
    data = sr.read_session(sid)
    if data is None:
        raise HTTPException(status_code=404, detail="session not found")
    return data


@app.get("/api/state")
def state() -> dict:
    base = lr.BASE / ".claude"
    out = {}
    for name in STATE_MARKERS:
        p = base / name
        out[name] = p.read_text(encoding="utf-8").strip() if p.exists() else None
    return out


@app.get("/api/logs/{source}")
def logs(source: str, n: int = 50):
    try:
        path = lr.resolve(source)
    except KeyError:
        raise HTTPException(status_code=404, detail="unknown source")
    return lr.read_tail(path, n)


def _sse(row: dict) -> str:
    return "data: " + json.dumps(row) + "\n\n"


@app.get("/sse/{source}")
async def sse(source: str):
    try:
        path = lr.resolve(source)
    except KeyError:
        raise HTTPException(status_code=404, detail="unknown source")

    async def gen():
        # 1) backlog
        for row in lr.read_tail(path, 50):
            yield _sse(row)
        yield ": backlog-end\n\n"
        # 2) live tail — poll, survive truncation, buffer partial lines
        offset = path.stat().st_size if path.exists() else 0
        buf = b""
        while True:
            await asyncio.sleep(0.5)
            if not path.exists():
                continue
            size = path.stat().st_size
            if size < offset:          # truncated / rotated -> reset
                offset, buf = 0, b""
            if size > offset:
                with path.open("rb") as f:
                    f.seek(offset)
                    chunk = f.read()
                    offset = f.tell()
                buf += chunk
                parts = buf.split(b"\n")
                buf = parts.pop()       # last element = partial remainder, keep buffering
                for raw in parts:
                    row = lr.parse(raw.decode("utf-8", "replace"))
                    if row is not None:
                        yield _sse(row)

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.websocket("/ws/chat")
async def ws_chat(ws: WebSocket):
    """Chat <-> background `claude -p`. One subprocess per message; --resume keeps
    the conversation. Inherits the logged-in Max subscription (no API key in env).

    Frames out: {"type":"start"} {"type":"session","id":...} {"type":"token","text":...}
                {"type":"tool","name":...} {"type":"done","session":...}
                {"type":"error","detail":...}
    Frames in:  {"message": "<text>", "resume": "<claude session id>"|null}
                `resume` seeds the thread when the browser reconnects a pane that
                already has a Claude session (a new socket otherwise starts cold).
    """
    if await _reject_cross_origin(ws):
        return
    await ws.accept()
    # Same refusal ws_term already makes: a key in the server env bills the API
    # instead of the Max plan. Silently spending the operator's API credit
    # because a stray key was exported is not an acceptable default.
    if os.environ.get("ANTHROPIC_API_KEY"):
        await ws.send_json({"type": "error", "detail":
            "Refused: ANTHROPIC_API_KEY is set in the server environment -- that bills "
            "the API, not your Max plan. Unset it and restart the server."})
        await ws.close()
        return
    # --- resolve the ACTIVE provider, per connect ------------------------
    # Hardcoding CLAUDE_BIN meant the provider selector in the UI was
    # decoration: whatever you picked, the server still spawned `claude`.
    # Resolve it here instead, and REFUSE clearly rather than handing an
    # unrunnable name to create_subprocess_exec -- which fails as a socket
    # that dies mid-handshake with no text on screen.
    detail = providers.active_provider_detail()
    active_id = detail["id"]
    if active_id is None:
        lines = ["  - %s: %s" % (p["id"], p["reason"] or "?")
                 for p in providers.discover_providers()]
        await ws.send_json({"type": "error", "code": "no-provider", "detail":
            "No AI provider is usable here -- a provider must be installed, "
            "configured, AND have a chat adapter in this build:\n"
            + "\n".join(lines)})
        await ws.close()
        return

    prov = providers.provider_by_id(active_id)
    if not prov["bin_path"]:
        # Reachable if the binary disappears between the settings write and
        # this connect (uninstall, PATH change, a stale settings.json).
        await ws.send_json({"type": "error", "code": "provider-missing", "detail":
            "Active provider %r cannot be started: %s" % (active_id, prov["reason"])})
        await ws.close()
        return

    if active_id != "claude":
        # Honest refusal instead of a confusing crash: the frames below parse
        # Claude Code's `--output-format stream-json` protocol. Spawning
        # another vendor's CLI with these flags would fail on argument
        # parsing and report as though the provider were broken. No adapter
        # has been written, so say that.
        await ws.send_json({"type": "error", "code": "no-adapter", "detail":
            "Active provider is %r (%s at %s). This chat channel speaks Claude "
            "Code's --output-format stream-json protocol and no adapter has "
            "been written for %s, so it is not being run rather than run "
            "wrongly. Use the provider selector to switch to claude, or the "
            "terminal tab." % (active_id, prov["name"], prov["bin_path"], active_id)})
        await ws.close()
        return

    settings = providers.load_settings()
    # Clamp at the point of USE, not just where it was written: a settings.json
    # from an older build, hand-edited, or written by another local process
    # would otherwise reach the spawn below with the ceiling raised.
    perm_mode = providers.effective_permission_mode(settings["permission_mode"])
    workdir = settings["workdir"] or WORKDIR
    if not providers.workdir_allowed(workdir):
        workdir = WORKDIR
    agent_bin = prov["bin_path"]

    if _ensure_workdir(workdir) is None:
        await ws.send_json({"type": "error", "detail":
            "workdir %s does not exist and could not be created" % workdir})
        await ws.close()
        return

    # One frame the client can render as a status line: which binary, which
    # permission mode, and (when acceptEdits/bypassPermissions is on) the fact
    # that this session may write files without asking.
    await ws.send_json({
        "type": "provider",
        "id": active_id,
        "name": prov["name"],
        "bin": agent_bin,
        "source": detail["source"],
        "permission_mode": perm_mode,
        "permission_note": providers.PERMISSION_MODE_NOTES.get(perm_mode),
        "writes_files": perm_mode in ("acceptEdits", "bypassPermissions"),
        "workdir": workdir,
    })

    session_id = None
    resume_unverified = False   # session id came from the client, not from a live run
    dead_seeds = set()          # client-supplied ids claude has already rejected
    try:
        while True:
            raw = await ws.receive_text()
            seed = None
            try:
                payload = json.loads(raw)
                msg = payload.get("message", "")
                seed = payload.get("resume")
            except (ValueError, AttributeError):
                msg = raw
            if not msg.strip():
                continue
            if (session_id is None and seed and isinstance(seed, str)
                    and seed not in dead_seeds
                    and "/" not in seed and ".." not in seed):
                session_id, resume_unverified = seed, True

            args = [
                agent_bin, "-p", msg,
                "--output-format", "stream-json",
                "--verbose", "--include-partial-messages",
                "--permission-mode", perm_mode,
            ]
            if session_id:
                args += ["--resume", session_id]

            await ws.send_json({"type": "start"})
            try:
                proc = await asyncio.create_subprocess_exec(
                    *args, cwd=workdir,
                    stdin=asyncio.subprocess.DEVNULL,   # inheriting uvicorn's stdin makes
                    stdout=asyncio.subprocess.PIPE,     # claude wait 3s for piped input on
                    stderr=asyncio.subprocess.PIPE,     # EVERY message -- 3s of dead air per turn
                    env=dict(os.environ),      # no ANTHROPIC_API_KEY -> subscription auth
                )
            except OSError as e:
                # Real cause, verbatim -- a dead socket taught the operator nothing.
                await ws.send_json({"type": "error", "detail":
                    "could not start %r in %s: %s" % (agent_bin, workdir, e)})
                continue

            got_text = got_result = False
            result_error = None
            async for line in proc.stdout:
                try:
                    ev = json.loads(line.decode("utf-8", "replace"))
                except ValueError:
                    continue
                # capture session id from the first event that carries one
                if session_id is None and ev.get("session_id"):
                    session_id = ev["session_id"]
                    await ws.send_json({"type": "session", "id": session_id})
                t = ev.get("type")
                if t == "stream_event":
                    delta = (ev.get("event") or {}).get("delta") or {}
                    if delta.get("type") == "text_delta" and delta.get("text"):
                        got_text = True
                        await ws.send_json({"type": "token", "text": delta["text"]})
                elif t == "assistant":
                    for blk in (ev.get("message") or {}).get("content", []):
                        # fallback when partial deltas are absent: emit full text blocks
                        if blk.get("type") == "text" and blk.get("text") and not got_text:
                            await ws.send_json({"type": "token", "text": blk["text"]})
                        elif blk.get("type") == "tool_use":
                            # tool_use blocks never arrive as text deltas, so this must
                            # run regardless of got_text -- gating it behind the text
                            # fallback meant a streaming turn reported zero tool calls.
                            await ws.send_json({"type": "tool", "name": blk.get("name", "")})
                elif t == "result":
                    got_result = True
                    # A `result` event is NOT proof of success: a failed run (stale
                    # --resume, permission abort, API error) emits one with
                    # is_error/subtype set and THEN exits non-zero. Sending "done"
                    # here painted a failed turn as answered-with-empty-text, and the
                    # real error arrived a frame later -- where the client attributed
                    # it to whatever turn came next. Hold it and report once, below.
                    if ev.get("is_error") or (ev.get("subtype") or "success") != "success":
                        result_error = str(ev.get("result") or ev.get("subtype")
                                           or "claude reported an error")[:600]
                    else:
                        await ws.send_json({"type": "done", "session": session_id})

            err = (await proc.stderr.read()).decode("utf-8", "replace")
            rc = await proc.wait()
            failed = (rc != 0) or (result_error is not None)
            if failed:
                # stderr carries the specific cause ("No conversation found with
                # session ID: ..."); the result payload is the fallback.
                detail = err.strip()[:600] or result_error or ("claude exited " + str(rc))
                frame = {"type": "error", "detail": detail}
                if resume_unverified:
                    # The id the browser handed us may be stale or from another
                    # machine. Drop it so the NEXT message starts a fresh thread
                    # instead of failing identically forever -- and remember it, or
                    # the client re-sends the same dead id on every message and the
                    # channel never recovers. `resume_reset` tells the client to
                    # forget it too.
                    frame["detail"] = detail + (
                        "  (resumed session %s was rejected -- the next message "
                        "will start a new thread)" % session_id)
                    frame["resume_reset"] = True
                    dead_seeds.add(session_id)
                    session_id = None
                    resume_unverified = False
                await ws.send_json(frame)
            elif not got_result:
                # process ended without a result event: still close the turn out
                await ws.send_json({"type": "done", "session": session_id})
            else:
                resume_unverified = False
    except WebSocketDisconnect:
        pass


@app.websocket("/ws/term")
async def ws_term(ws: WebSocket):
    """Run the real `claude` TUI in a PTY and relay raw bytes <-> xterm.js.

    This renders the ACTUAL terminal (full parity) — it does NOT parse Claude's
    output. Drives the logged-in `claude` binary => Max-plan billing, no API key.
    """
    if await _reject_cross_origin(ws):
        return
    await ws.accept()
    if os.environ.get("ANTHROPIC_API_KEY"):
        await ws.send_text("\r\n\x1b[31mRefused: ANTHROPIC_API_KEY is set — that bills the API, not your Max plan.\x1b[0m\r\n")
        await ws.close()
        return

    # optional: resume an existing session, in its original cwd
    resume = ws.query_params.get("resume")
    req_cwd = ws.query_params.get("cwd")
    # CREATE the requested workdir rather than silently falling back to WORKDIR when it
    # does not exist yet. The chat path already does this (_ensure_workdir before spawn),
    # so the old isdir() test made the two disagree: the pane header said
    # ~/sutra-work-verified while the shell prompt sat in ~/sutra-ui-workspace, with
    # nothing on screen explaining the difference. Confined to the same root the settings
    # writer validates against, so this cannot be pointed at an arbitrary path.
    workdir = WORKDIR
    if req_cwd and providers.workdir_allowed(req_cwd):
        workdir = _ensure_workdir(req_cwd) or WORKDIR
    # ws_term never created WORKDIR: on a fresh machine the PTY spawn below
    # raised FileNotFoundError and the terminal socket died on connect.
    workdir = _ensure_workdir(workdir) or os.path.expanduser("~")

    # ?shell=1 -> the operator's OWN login shell, not the claude TUI. This endpoint
    # only ever ran `claude`, so the studio's terminal pane could not be used as a
    # terminal: no git, no ls, no build. $SHELL is what Terminal.app itself uses
    # (zsh on macOS since Catalina); falling back to /bin/zsh then /bin/sh keeps it
    # working when $SHELL is unset, as it is under a launchd/Finder launch.
    # `-l` makes it a LOGIN shell so the operator's PATH, aliases and rc files apply
    # -- without it, `claude`, `node` and `brew` are typically not even on PATH here.
    plain_shell = ws.query_params.get("shell") == "1"
    if plain_shell:
        sh = os.environ.get("SHELL") or "/bin/zsh"
        if not os.path.isfile(sh):
            sh = "/bin/zsh" if os.path.isfile("/bin/zsh") else "/bin/sh"
        args = [sh, "-l"]
    else:
        args = [CLAUDE_BIN]
        if resume and "/" not in resume and ".." not in resume:
            args += ["--resume", resume]

    # Fix #2/#4: classic (non-alt-screen) renderer + correct TERM/locale reduce TUI corruption
    env = dict(os.environ)
    env.setdefault("TERM", "xterm-256color")
    env.setdefault("LANG", "en_US.UTF-8")
    env.setdefault("LC_ALL", "en_US.UTF-8")
    env["CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN"] = "1"

    master, slave = pty.openpty()
    proc = await asyncio.create_subprocess_exec(
        *args, cwd=workdir, env=env,
        stdin=slave, stdout=slave, stderr=slave, start_new_session=True,
    )
    os.close(slave)
    loop = asyncio.get_event_loop()

    async def pump_out():
        """PTY master -> browser. Send RAW bytes (#3): never decode server-side —
        a 64KB read can split a multibyte UTF-8 char; xterm.js decodes the stream safely."""
        try:
            while True:
                data = await loop.run_in_executor(None, os.read, master, 65536)
                if not data:
                    break
                await ws.send_bytes(data)
        except (OSError, RuntimeError, WebSocketDisconnect):
            pass

    reader = asyncio.create_task(pump_out())

    # Auto-fire Sutra (/core:start) + token-saving caveman on each FRESH session.
    # Skipped on resume (already activated in the original session).
    async def autostart():
        # INIT_CMD and /caveman are claude SLASH COMMANDS. In shell mode they would be
        # typed straight into the operator's zsh, which would run "/core:start" as a
        # path and print "no such file or directory" into a brand-new terminal.
        if resume or plain_shell:
            return
        caveman = ws.query_params.get("caveman", "1" if AUTO_CAVEMAN else "0") == "1"
        await asyncio.sleep(INIT_DELAY)
        if INIT_CMD:
            os.write(master, (INIT_CMD + "\r").encode("utf-8"))
        if caveman:
            await asyncio.sleep(1.5)
            os.write(master, "/caveman\r".encode("utf-8"))

    starter = asyncio.create_task(autostart())
    try:
        while True:
            msg = await ws.receive_text()
            try:
                m = json.loads(msg)
            except ValueError:
                continue
            kind = m.get("t")
            if kind == "i":                       # keystroke / injected text
                os.write(master, m.get("d", "").encode("utf-8"))
            elif kind == "r":                     # resize
                rows, cols = int(m.get("r", 24)), int(m.get("c", 80))
                fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    except WebSocketDisconnect:
        pass
    finally:
        reader.cancel()
        starter.cancel()
        try:
            proc.send_signal(signal.SIGHUP)
        except ProcessLookupError:
            pass
        try:
            os.close(master)
        except OSError:
            pass


# static assets (css/js if added later); index is served by "/" above
app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
