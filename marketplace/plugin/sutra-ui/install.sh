#!/usr/bin/env bash
# install.sh -- package the Sutra panel as an installable macOS .app AND a CLI.
#
# Installs two launchers that both serve the SAME FastAPI app (app:app) out of
# this source tree, against the REAL registry (~/.sutra-native/user-kit):
#
#   1. "Sutra.app"  -> /Applications if writable without sudo, else ~/Applications
#   2. "sutra-ui"   -> ~/.local/bin (a CLI on PATH)
#
# Both point at THIS checkout on purpose. The panel's write path goes through
# ../lib/placement_engine.py against the live registry, so vendoring a frozen
# copy of the engine into /Applications would let a stale engine mutate a live
# registry. One source of truth beats a self-contained bundle here.
#
# Invariants carried over from sutra-ui.sh (do not relax):
#   * binds 127.0.0.1 ONLY
#   * REFUSES to start if ANTHROPIC_API_KEY is set (Max-plan billing guard)
#   * no hardcoded port -- a free one is chosen at launch
#
# NEVER uses sudo. Idempotent: safe to re-run -- re-running IS the update path: it
# quits a running Sutra.app, replaces the bundle and re-stages the runtime.
# `--uninstall` removes both.
#
# Usage:
#   ./install.sh
#   ./install.sh --update            # same thing, named for what you want
#   ./install.sh --uninstall
#   SUTRA_APPS_DIR=~/Applications ./install.sh     # force the app destination
set -euo pipefail

# The checkout is the SOURCE OF TRUTH but may live under ~/Desktop, which macOS
# TCC protects: a Finder-launched app cannot read it. The runtime is therefore
# STAGED into Application Support (never TCC-protected) and launchers point there.
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_REPO="$(cd "$REPO/.." && pwd)"        # marketplace/plugin -- holds lib/
CHECKOUT_ROOT="$(cd "$REPO/../../.." && pwd)"  # the git checkout (sutra/)
APP_NAME="Sutra"
BUNDLE_ID="os.sutra.ui"
CLI_NAME="sutra-ui"
CLI_DIR="$HOME/.local/bin"
CLI_PATH="$CLI_DIR/$CLI_NAME"

# Staged runtime. SRC keeps its position UNDER a "plugin" dir because org_api.py
# resolves its engine as parents[1]/"lib" -- flattening the tree breaks that import.
SUTRA_STAGE="$HOME/Library/Application Support/$APP_NAME"
SRC="$SUTRA_STAGE/plugin/sutra-ui"
STAGE_LIB="$SUTRA_STAGE/plugin/lib"
VENV="$SUTRA_STAGE/venv"
PY="$VENV/bin/python"
SUTRA_CANONICAL_PORT=8330                    # the installed app ALWAYS serves here
# App icon. assets/sutra-app-icon.svg is the mark on a BLACK plate; the website mark is
# transparent by design (it sits on a light page), and macOS composites a transparent icon
# straight onto the Dock, where gold-on-nothing reads as grey. Prefer the plated icon and
# fall back to the bare mark only if the asset is missing, so an older checkout still builds.
MARK_SVG="$REPO/assets/sutra-app-icon.svg"
[ -f "$MARK_SVG" ] || MARK_SVG="$REPO/../../../website/sutra-mark.svg"
LSREGISTER="/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"

WROTE=()          # every path this run created or refreshed
NOTES=()          # fallbacks / manual follow-ups worth printing at the end

say()  { printf '%s\n' "$*"; }
step() { printf '\n== %s\n' "$*"; }
note() { NOTES+=("$*"); }
wrote(){ WROTE+=("$1"); }
die()  { printf 'install.sh: %s\n' "$*" >&2; exit 2; }


# --------------------------------------------------------------------------
# preflight -- fail EARLY and in plain language.
# This installer builds a macOS .app bundle: it drives sips + iconutil for the
# icon, codesign for the signature, LaunchServices to register it and osascript
# for the failure dialog. None of that exists on Linux, and the previous
# behaviour was to run halfway and then die about a missing `sips`, which reads
# like a bug in Sutra rather than an unsupported platform.
# --------------------------------------------------------------------------
if [ "$(uname -s)" != "Darwin" ]; then
  die "macOS only.

  This installer builds a .app bundle (sips / iconutil / codesign / osascript);
  there is no Linux or Windows path yet.

  You can still run the panel directly from this directory:
    python3 -m venv .venv
    .venv/bin/pip install -r requirements.txt
    .venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 8330
  then open http://127.0.0.1:8330"
fi

command -v python3 >/dev/null 2>&1 \
  || die "python3 not found. Install Python 3.9 or newer, then re-run this script."


# --------------------------------------------------------------------------
# destination for the .app: /Applications only if it is writable WITHOUT sudo
# --------------------------------------------------------------------------
pick_apps_dir() {
  if [ -n "${SUTRA_APPS_DIR:-}" ]; then
    mkdir -p "$SUTRA_APPS_DIR"
    printf '%s\n' "$SUTRA_APPS_DIR"
    return
  fi
  if [ -d /Applications ] && [ -w /Applications ]; then
    printf '%s\n' /Applications
  else
    mkdir -p "$HOME/Applications"
    printf '%s\n' "$HOME/Applications"
  fi
}


# --------------------------------------------------------------------------
# uninstall
# --------------------------------------------------------------------------
do_uninstall() {
  local removed=() kept=()
  local d p
  for d in /Applications "$HOME/Applications" ${SUTRA_APPS_DIR:+"$SUTRA_APPS_DIR"}; do
    if [ -d "$d/$APP_NAME.app" ]; then
      rm -r -f "$d/$APP_NAME.app" 2>/dev/null || true
      if [ ! -d "$d/$APP_NAME.app" ]; then
        removed+=("$d/$APP_NAME.app")
      else
        kept+=("$d/$APP_NAME.app  (could not remove without elevated rights -- left in place)")
      fi
    fi
  done
  if [ -e "$CLI_PATH" ]; then
    rm -f "$CLI_PATH" && removed+=("$CLI_PATH")
  fi
  if [ -d "$REPO/electron/dist" ]; then
    rm -r -f "$REPO/electron/dist" 2>/dev/null || true
    [ -d "$REPO/electron/dist" ] || removed+=("$REPO/electron/dist  (packaged Electron bundle)")
  fi
  if [ -d "$SUTRA_STAGE" ]; then
    rm -r -f "$SUTRA_STAGE" 2>/dev/null || true
    [ -d "$SUTRA_STAGE" ] || removed+=("$SUTRA_STAGE  (staged runtime + venv)")
  fi
  if [ -d "$HOME/.sutra-native/run" ]; then
    rm -f "$HOME/.sutra-native/run"/sutra-ui-*.pid "$HOME/.sutra-native/run/sutra-app.log" 2>/dev/null || true
    rmdir "$HOME/.sutra-native/run" 2>/dev/null && removed+=("$HOME/.sutra-native/run  (runtime pidfiles + app log)") || true
  fi
  [ -x "$LSREGISTER" ] && "$LSREGISTER" -u "/Applications/$APP_NAME.app" >/dev/null 2>&1 || true

  say "Sutra uninstall"
  if [ ${#removed[@]} -eq 0 ]; then
    say "  removed: nothing -- no Sutra.app in /Applications or ~/Applications, no $CLI_PATH"
  else
    say "  removed:"
    for p in "${removed[@]}"; do say "    $p"; done
  fi
  if [ ${#kept[@]} -gt 0 ]; then
    say "  NOT removed:"
    for p in "${kept[@]}"; do say "    $p"; done
  fi
  say ""
  say "  left alone (not installed by this script):"
  say "    $REPO                      (your checkout -- the source of truth)"
  say "    ~/.sutra-native/user-kit   (your real registry -- never touched)"
  say ""
  say "  Sutra does NOT need Full Disk Access. If an older build asked for it,"
  say "  you can safely revoke it in"
  say "    System Settings > Privacy & Security > Full Disk Access"
  exit 0
}

# --------------------------------------------------------------------------
# 0. stage the runtime out of the TCC-protected checkout.
#    --delete is scoped to the two directories THIS script owns and is never
#    pointed at $SUTRA_STAGE itself, so nothing else under Application Support is at
#    risk. Excludes keep the venv, git metadata and tests out of the payload.
# --------------------------------------------------------------------------
stage_runtime() {
  step "stage runtime"
  command -v rsync >/dev/null 2>&1 || die "rsync not found -- cannot stage the runtime"
  mkdir -p "$SRC" "$STAGE_LIB" || die "cannot create $SUTRA_STAGE"
  rsync -a --delete \
    --exclude '.venv/' --exclude '.git/' --exclude '__pycache__/' \
    --exclude '*.pyc' --exclude 'test_*' --exclude '.DS_Store' \
    "$REPO"/ "$SRC"/ || die "staging $REPO -> $SRC failed"
  rsync -a --delete \
    --exclude '__pycache__/' --exclude '*.pyc' --exclude '.DS_Store' \
    "$PLUGIN_REPO/lib"/ "$STAGE_LIB"/ || die "staging lib -> $STAGE_LIB failed"
  [ -f "$SRC/app.py" ] || die "staging incomplete: no app.py at $SRC"
  [ -f "$STAGE_LIB/placement_engine.py" ] \
    || die "staging incomplete: no placement_engine.py at $STAGE_LIB"
  say "  staged to $SRC"
  wrote "$SUTRA_STAGE"
}

# --------------------------------------------------------------------------
# Quit a RUNNING Sutra.app before its bundle is replaced.
#
# Installing over a running app deletes the bundle out from under a live process:
# the window survives on a deleted inode, its "Check for updates" and relaunch are
# broken, and the next launch can pick up a half-copied bundle. macOS has no
# atomic swap for this -- the fix is to stop the app first and confirm it stopped.
#
# `osascript quit` is a polite Apple-event quit (state is saved). We then WAIT for
# the process to actually go away rather than assuming, because the copy below is
# destructive. If it will not exit we say so and stop, instead of deleting anyway.
# --------------------------------------------------------------------------
quit_running_app() {
  local waited=0
  pgrep -x "$APP_NAME" >/dev/null 2>&1 || return 0
  say "$APP_NAME is running -- asking it to quit before replacing the bundle"
  osascript -e "tell application \"$APP_NAME\" to quit" >/dev/null 2>&1 || true
  while pgrep -x "$APP_NAME" >/dev/null 2>&1; do
    [ "$waited" -ge 10 ] && break
    sleep 1
    waited=$((waited + 1))
  done
  if pgrep -x "$APP_NAME" >/dev/null 2>&1; then
    die "$APP_NAME is still running after ${waited}s and would be replaced underneath
    itself. Quit it (Cmd-Q) and re-run. Nothing has been changed."
  fi
  say "$APP_NAME quit cleanly"
}

case "${1:-}" in
  --uninstall)      do_uninstall ;;
  # --update is a documented ALIAS for a plain re-run, not a second code path: the
  # installer is already idempotent and already replaces the bundle. Having the verb
  # people look for beats having them wonder whether re-running is safe.
  --update|--reinstall) SUTRA_UPDATING=1 ;;
  -h|--help)        sed -n '2,25p' "$0"; exit 0 ;;
  "")               ;;
  *)                die "unknown argument: ${1}  (try --help)" ;;
esac

quit_running_app


stage_runtime          # MUST run before anything reads $SRC

# --------------------------------------------------------------------------
# 1. venv + deps (requirements.txt ONLY -- no new packages)
# --------------------------------------------------------------------------
step "venv"
if [ ! -x "$PY" ]; then
  say "creating $VENV"
  python3 -m venv "$VENV" || die "python3 -m venv failed"
  wrote "$VENV"
else
  say "reusing $VENV ($("$PY" -V 2>&1))"
fi
[ -x "$PY" ] || die "no interpreter at $PY after venv creation"

[ -f "$SRC/requirements.txt" ] || die "requirements.txt missing at $SRC"
say "installing deps from requirements.txt"
"$PY" -m pip install --disable-pip-version-check --quiet -r "$SRC/requirements.txt" \
  || die "pip install -r requirements.txt failed"
"$PY" - <<'PYEOF' || die "runtime deps not importable -- refusing to install a broken launcher"
import importlib
mods = ("fastapi", "uvicorn", "websockets")
for m in mods:
    importlib.import_module(m)
print("  deps ok: " + ", ".join(
    "%s %s" % (m, getattr(importlib.import_module(m), "__version__", "?")) for m in mods))
PYEOF


# --------------------------------------------------------------------------
# 2. icon: SVG -> iconset -> .icns using ONLY built-in sips + iconutil.
#    If anything fails we ship without an icon rather than add a dependency.
# --------------------------------------------------------------------------
make_icon() {
  local svg="$1" out="$2" ws iconset px name spec
  [ -f "$svg" ] || return 1
  command -v sips     >/dev/null 2>&1 || return 1
  command -v iconutil >/dev/null 2>&1 || return 1

  ws="$(mktemp -d "${TMPDIR:-/tmp}/sutra-icon.XXXXXX")" || return 1
  iconset="$ws/$APP_NAME.iconset"
  mkdir -p "$iconset"

  sips -s format png -z 1024 1024 "$svg" --out "$ws/base.png" >/dev/null 2>&1 \
    || { rm -r -f "$ws"; return 1; }

  for spec in "16 icon_16x16" "32 icon_16x16@2x" "32 icon_32x32" "64 icon_32x32@2x" \
              "128 icon_128x128" "256 icon_128x128@2x" "256 icon_256x256" \
              "512 icon_256x256@2x" "512 icon_512x512" "1024 icon_512x512@2x"; do
    px="${spec%% *}"; name="${spec#* }"
    sips -z "$px" "$px" "$ws/base.png" --out "$iconset/$name.png" >/dev/null 2>&1 \
      || { rm -r -f "$ws"; return 1; }
  done

  iconutil -c icns "$iconset" -o "$out" >/dev/null 2>&1 || { rm -r -f "$ws"; return 1; }
  rm -r -f "$ws"
  [ -s "$out" ]
}


# --------------------------------------------------------------------------
# 3. the launcher body, shared by the .app and the CLI.
#    emit_launcher <mode:app|cli> <dest>
# --------------------------------------------------------------------------
emit_launcher() {
  local mode="$1" dest="$2" tmp
  tmp="$(mktemp "${TMPDIR:-/tmp}/sutra-launcher.XXXXXX")"

  cat > "$tmp" <<'SUTRA_LAUNCHER_EOF'
#!/usr/bin/env bash
# Generated by sutra-ui/install.sh -- do not edit here; re-run install.sh.
#
# Serves the Sutra panel (FastAPI app:app) on 127.0.0.1 on a FREE port, waits
# for it to actually answer, opens your default browser, and stays in the
# foreground until it is told to quit -- then stops the server cleanly.
set -uo pipefail

SUTRA_UI_HOME="__SUTRA_HOME__"
SUTRA_UI_VENV="__SUTRA_VENV__"   # venv lives beside the staged app, NOT inside it
# log_reader.py defaults its BASE to parents[2] of its own file. From the staged
# location that is the stage root, not your checkout, so the governance-log
# views would silently read the wrong tree. Pin it to the real repo.
# NOTE: this points at the CHECKOUT root. log_reader's sources (.sutra/,
# .enforcement/, holding/) live in whichever project Sutra governance actually
# runs in, which is not knowable at install time -- so set SUTRA_REPO_ROOT
# yourself to read another project's logs. Without that the log views are empty
# rather than wrong, and app.py surfaces the miss instead of inventing data.
export SUTRA_REPO_ROOT="${SUTRA_REPO_ROOT:-__SUTRA_REPO__}"
CANONICAL_PORT="__SUTRA_PORT__"  # the .app always serves here
MODE="__SUTRA_MODE__"
HOST="127.0.0.1"          # loopback ONLY. A browser terminal is machine access.
OPEN_BROWSER=1
PORT_OVERRIDE="${SUTRA_UI_PORT:-}"
RUN_DIR="$HOME/.sutra-native/run"
LOGFILE="$RUN_DIR/sutra-app.log"
PIDFILE=""
SRV_PID=""

log() { printf 'sutra-ui: %s\n' "$*" >&2; }

# LaunchServices can start a script-based bundle under Rosetta (x86_64) even on
# Apple Silicon. The venv holds NATIVE arm64 wheels (pydantic_core), so python
# would then die with "incompatible architecture". Re-exec natively, once.
if [ "$(sysctl -n sysctl.proc_translated 2>/dev/null || echo 0)" = "1" ] \
   && [ -z "${SUTRA_UI_ARCH_REEXEC:-}" ] \
   && command -v arch >/dev/null 2>&1; then
  export SUTRA_UI_ARCH_REEXEC=1
  exec arch -arm64 "$0" "$@"
fi

alert() {   # alert <title> <message>  -- GUI only; no-op in the CLI
  [ "$MODE" = "app" ] || return 1
  command -v osascript >/dev/null 2>&1 || return 1
  local t m
  t=$(printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g')
  m=$(printf '%s' "$2" | tr '\n' ' ' | sed 's/\\/\\\\/g; s/"/\\"/g')
  osascript -e "display alert \"$t\" message \"$m\" as critical" >/dev/null 2>&1 || true
}

die() {
  log "$*"
  if [ "$MODE" = "app" ]; then
    alert "Sutra could not start" "$* -- full log: $LOGFILE"
  fi
  exit 2
}

# macOS blocks Finder-launched apps from reading Desktop/Documents/Downloads
# until the user grants it. The interpreter and the app code both live in
# SUTRA_UI_HOME, so if that is denied nothing else here can work. Say so
# precisely instead of failing later with a confusing symptom.
tcc_die() {
  local msg="macOS is blocking Sutra from reading its own program files in $SUTRA_UI_HOME. Sutra installs its runtime under ~/Library/Application Support/Sutra, which is NOT a protected folder, so this should not happen -- it usually means the install is stale or the staged copy was moved. Fix: re-run install.sh from your Sutra checkout. Full Disk Access is NOT required and you should not need to grant it."
  log "$msg"
  if [ "$MODE" = "app" ] && command -v osascript >/dev/null 2>&1; then
    local m resp
    m=$(printf '%s' "$msg" | sed 's/\\/\\\\/g; s/"/\\"/g')
    resp=$(osascript -e "display alert \"Sutra needs file access\" message \"$m\" buttons {\"Open Settings\", \"Quit\"} default button \"Open Settings\"" 2>/dev/null || true)
    case "$resp" in
      *"Open Settings"*)
        open "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles" >/dev/null 2>&1 \
          || open "x-apple.systempreferences:com.apple.preference.security" >/dev/null 2>&1 || true
        ;;
    esac
  fi
  exit 3
}

usage() {
  cat <<USAGE
sutra-ui -- open the Sutra panel in your browser (localhost only).

  sutra-ui                 pick a free port, serve, open the browser
  sutra-ui --port 7000     use a specific port instead
  sutra-ui --no-open       serve without opening a browser
  sutra-ui --help          this text

Environment:
  SUTRA_UI_PORT       same as --port
  SUTRA_UI_NO_OPEN=1  same as --no-open
  SUTRA_UI_WORKDIR    directory the embedded claude session works in (default: cwd)
  SUTRA_UI_DEPT       marketing|sales|cs|hr|finance|devpm -- tunes the tasks panel

Always binds 127.0.0.1. Refuses to start if ANTHROPIC_API_KEY is set.
USAGE
}

if [ "$MODE" = "cli" ]; then
  while [ $# -gt 0 ]; do
    case "$1" in
      -h|--help)   usage; exit 0 ;;
      --no-open)   OPEN_BROWSER=0; shift ;;
      --port)      PORT_OVERRIDE="${2:-}"; [ -n "$PORT_OVERRIDE" ] || die "--port needs a number"; shift 2 ;;
      --port=*)    PORT_OVERRIDE="${1#*=}"; shift ;;
      *)           die "unknown argument: $1  (try --help)" ;;
    esac
  done
  # the CLI works in whatever directory you invoked it from, like sutra-ui.sh
  export SUTRA_UI_WORKDIR="${SUTRA_UI_WORKDIR:-$PWD}"
fi
[ "${SUTRA_UI_NO_OPEN:-0}" = "1" ] && OPEN_BROWSER=0

mkdir -p "$RUN_DIR" 2>/dev/null || true

# A Finder-launched app has no terminal: send everything to a log so a failed
# launch is diagnosable instead of silent.
if [ "$MODE" = "app" ]; then
  exec >>"$LOGFILE" 2>&1
  printf '\n===== Sutra launch %s =====\n' "$(date '+%Y-%m-%d %H:%M:%S')"
fi

# --- Max-plan billing guard (#1 invariant, mirrors sutra-ui.sh) -------------
if [ -n "${ANTHROPIC_API_KEY:-}" ]; then
  die "REFUSING TO START: ANTHROPIC_API_KEY is set. That routes through the API (per-token billing), not your Max plan. Fix: unset ANTHROPIC_API_KEY, then make sure 'claude' is logged in (claude /login)."
fi

PY="$SUTRA_UI_VENV/bin/python"
[ -x "$PY" ] || die "python venv missing at $PY -- re-run install.sh from your Sutra checkout"
[ -f "$SUTRA_UI_HOME/app.py" ] || die "app.py missing at $SUTRA_UI_HOME -- re-run install.sh"

# --- preflight: can the interpreter actually run and read its own venv? -----
# Under macOS file-access restrictions the exec succeeds but every open() is
# refused, which otherwise surfaces much later as a nonsense error.
if ! PYCHECK="$("$PY" -c 'import sys; sys.stdout.write("ok")' 2>&1)" || [ "$PYCHECK" != "ok" ]; then
  case "$PYCHECK" in
    *"Operation not permitted"*|*PermissionError*|*"not permitted"*) tcc_die ;;
    *) die "cannot run $PY: $PYCHECK" ;;
  esac
fi

# --- reap orphans from a previous run that was SIGKILLed --------------------
for stale in "$RUN_DIR"/*.pid; do
  [ -e "$stale" ] || continue
  spid="$(cat "$stale" 2>/dev/null || true)"
  if [ -n "$spid" ] && kill -0 "$spid" 2>/dev/null; then
    # only reap a process that is OUR uvicorn AND has been orphaned (ppid 1)
    if ps -o ppid=,command= -p "$spid" 2>/dev/null | grep -q "^ *1 .*uvicorn.*app:app"; then
      log "reaping orphaned server pid $spid from a previous run"
      kill -TERM "$spid" 2>/dev/null || true
    else
      continue
    fi
  fi
  rm -f "$stale" 2>/dev/null || true
done

free_port() {   # an ephemeral port that is NEVER the canonical one
  "$PY" -c '
import socket, sys
reserved = int(sys.argv[1])
for _ in range(64):
    s = socket.socket(); s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]; s.close()
    if port != reserved:
        print(port); sys.exit(0)
sys.exit("no free port other than %d" % reserved)
' "$CANONICAL_PORT"
}

port_owner() {   # pid listening on <port>, empty if free (or lsof unavailable)
  command -v lsof >/dev/null 2>&1 || return 0
  lsof -ti "tcp:$1" -sTCP:LISTEN 2>/dev/null | head -1
}

# "a uvicorn serving app:app" is NOT Sutra -- that is how every FastAPI project
# in the world is started. Ask the SERVER to identify itself: /api/org/health is
# Sutra's own route and returns a lint_scope key nothing else serves.
is_sutra_server() {   # is_sutra_server <host> <port>
  "$PY" -c '
import json, sys, urllib.request
url = "http://%s:%s/api/org/health" % (sys.argv[1], sys.argv[2])
try:
    with urllib.request.urlopen(url, timeout=2) as r:
        if r.getcode() != 200:
            sys.exit(1)
        body = json.loads(r.read().decode("utf-8", "replace"))
except Exception:
    sys.exit(1)
sys.exit(0 if isinstance(body, dict) and "lint_scope" in body else 1)
' "$1" "$2"
}

# Second, independent identity signal: the listening process must be running
# out of OUR staged directory. Skipped (returns true) only when lsof is absent,
# in which case the endpoint check stands alone -- documented, not silent.
pid_cwd_is_ours() {   # pid_cwd_is_ours <pid>
  command -v lsof >/dev/null 2>&1 || return 0
  local cwd
  cwd=$(lsof -a -p "$1" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -1)
  [ -z "$cwd" ] && return 1
  [ "$cwd" = "$SUTRA_UI_HOME" ]
}

probe_200() {
  "$PY" -c '
import sys, urllib.request
try:
    with urllib.request.urlopen(sys.argv[1], timeout=2) as r:
        sys.exit(0 if r.getcode() == 200 else 1)
except Exception:
    sys.exit(1)
' "$1"
}

# A PORT probe is not a PROCESS probe. If our uvicorn dies on "address already
# in use", a stale Sutra still answers 200 and the old code called that success,
# then exited -- leaving the user driving a server this launcher does not own.
# Correlate every poll with our own child: if it dies, fail immediately.
wait_up_owned() {   # wait_up_owned <url> <pid> -> 0 only if OUR pid serves it
  local url="$1" pid="$2" i=0
  while [ "$i" -lt 120 ]; do
    kill -0 "$pid" 2>/dev/null || return 2      # our child died -> never adopt
    probe_200 "$url" && return 0
    sleep 0.25
    i=$((i + 1))
  done
  return 1
}

cleanup() {
  trap - EXIT INT TERM HUP
  if [ -n "$SRV_PID" ] && kill -0 "$SRV_PID" 2>/dev/null; then
    kill -TERM "$SRV_PID" 2>/dev/null || true
    for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
      kill -0 "$SRV_PID" 2>/dev/null || break
      sleep 0.25
    done
    if kill -0 "$SRV_PID" 2>/dev/null; then kill -KILL "$SRV_PID" 2>/dev/null || true; fi
  fi
  if [ -n "$PIDFILE" ]; then rm -f "$PIDFILE" 2>/dev/null || true; fi
}
trap cleanup EXIT INT TERM HUP

cd "$SUTRA_UI_HOME" || die "cannot cd to $SUTRA_UI_HOME"

# --- start, health-check, retry on a different port if the bind raced ------
# Port policy: the .app is PINNED to the canonical port so Sutra is always at
# the same URL; the CLI takes an ephemeral port (never the canonical one) unless
# --port says otherwise. A FIXED port is never silently swapped for another.
# The .app must ALWAYS be reachable at the same URL, so it ignores SUTRA_UI_PORT
# entirely; honouring it would reopen the very hole the pin closes. The CLI
# still honours --port / SUTRA_UI_PORT.
if [ "$MODE" = "app" ] && [ -n "$PORT_OVERRIDE" ]; then
  log "ignoring SUTRA_UI_PORT=$PORT_OVERRIDE -- the Sutra app always serves on $CANONICAL_PORT"
  PORT_OVERRIDE=""
fi

PORT_IS_FIXED=0
if [ -n "$PORT_OVERRIDE" ] || [ "$MODE" = "app" ]; then
  PORT_IS_FIXED=1
fi

started=0
for attempt in 1 2 3; do
  if [ -n "$PORT_OVERRIDE" ]; then
    PORT="$PORT_OVERRIDE"
  elif [ "$MODE" = "app" ]; then
    PORT="$CANONICAL_PORT"
  else
    PORT_OUT="$(free_port 2>&1)"
    PORT="$(printf '%s' "$PORT_OUT" | tr -d '[:space:]')"
    case "$PORT" in
      ''|*[!0-9]*)
        case "$PORT_OUT" in
          *"Operation not permitted"*|*PermissionError*) tcc_die ;;
        esac
        die "could not find a free port: ${PORT_OUT:-no output from python}" ;;
    esac
  fi

  # Single instance: if a healthy Sutra already owns this port, hand the user
  # over to it instead of starting a second one or adopting it silently.
  occupant="$(port_owner "$PORT" 2>/dev/null || true)"
  if [ -n "$occupant" ]; then
    if is_sutra_server "$HOST" "$PORT"; then
      if pid_cwd_is_ours "$occupant"; then
        log "Sutra is already running on port $PORT (pid $occupant) -- opening that one"
        URL="http://$HOST:$PORT/${SUTRA_UI_DEPT:+?dept=$SUTRA_UI_DEPT}"
        if [ "$OPEN_BROWSER" = "1" ] && command -v open >/dev/null 2>&1; then
          open "$URL" >/dev/null 2>&1 || true
        fi
        exit 0
      fi
      why="it answers /api/org/health but runs from a different directory than $SUTRA_UI_HOME (a second, older install?)"
    else
      why="it did not answer /api/org/health, so it is not Sutra"
    fi
    if [ "$PORT_IS_FIXED" = "1" ]; then
      die "port $PORT is held by pid $occupant: $why. Quit it (kill $occupant) and try again."
    fi
    continue                      # auto-assigned: just roll another port
  fi

  "$PY" -m uvicorn app:app --host "$HOST" --port "$PORT" --log-level warning &
  SRV_PID=$!
  PIDFILE="$RUN_DIR/sutra-ui-$PORT.pid"
  printf '%s\n' "$SRV_PID" > "$PIDFILE" 2>/dev/null || PIDFILE=""

  URL="http://$HOST:$PORT/${SUTRA_UI_DEPT:+?dept=$SUTRA_UI_DEPT}"
  if wait_up_owned "http://$HOST:$PORT/" "$SRV_PID"; then
    started=1
    break
  fi

  log "server did not answer on port $PORT (attempt $attempt)"
  kill -TERM "$SRV_PID" 2>/dev/null || true
  wait "$SRV_PID" 2>/dev/null || true
  SRV_PID=""
  if [ -n "$PIDFILE" ]; then rm -f "$PIDFILE"; fi
  PIDFILE=""
  if [ "$PORT_IS_FIXED" = "1" ]; then
    die "port $PORT would not serve -- check $LOGFILE"
  fi
done
[ "$started" = "1" ] || die "server failed to start after 3 attempts"

printf 'Sutra UI  ->  %s\n' "$URL"
printf '  bind:   %s (loopback only)\n' "$HOST:$PORT"
printf '  auth:   Claude Max subscription (no API key)\n'
if [ "$MODE" = "app" ]; then printf '  stop:   quit Sutra\n'; else printf '  stop:   Ctrl-C\n'; fi

if [ "$OPEN_BROWSER" = "1" ] && command -v open >/dev/null 2>&1; then
  open "$URL" >/dev/null 2>&1 || log "could not open a browser; go to $URL"
fi

wait "$SRV_PID"
SUTRA_LAUNCHER_EOF

  # Substitute the placeholders. '|' delimiter because paths contain '/'.
  # '&' is special in sed replacement text and Application Support paths are
  # user-controlled, so escape it (and any backslash) before substituting.
  local esc_src esc_venv esc_repo
  esc_src=$(printf '%s' "$SRC"   | sed -e 's/[\\&|]/\\&/g')
  esc_venv=$(printf '%s' "$VENV" | sed -e 's/[\\&|]/\\&/g')
  esc_repo=$(printf '%s' "$CHECKOUT_ROOT" | sed -e 's/[\\&|]/\\&/g')
  sed -e "s|__SUTRA_HOME__|$esc_src|g" \
      -e "s|__SUTRA_VENV__|$esc_venv|g" \
      -e "s|__SUTRA_REPO__|$esc_repo|g" \
      -e "s|__SUTRA_PORT__|$SUTRA_CANONICAL_PORT|g" \
      -e "s|__SUTRA_MODE__|$mode|g" "$tmp" > "$dest"
  rm -f "$tmp"
  chmod 755 "$dest"
}


# --------------------------------------------------------------------------
# 4. build the .app bundle (staged, then swapped into place)
# --------------------------------------------------------------------------
step "app bundle"
APPS_DIR="$(pick_apps_dir)"
if [ "$APPS_DIR" != "/Applications" ]; then
  note "installed the app to $APPS_DIR (either /Applications was not writable without sudo, or SUTRA_APPS_DIR overrode it)."
fi
APP="$APPS_DIR/$APP_NAME.app"

# --- Electron desktop shell (preferred when its toolchain is present) -------
# The script bundle further down is the FALLBACK: it needs no node toolchain,
# so it stays. But once electron/ has been installed, a re-run of install.sh
# must NOT silently replace the Electron app the operator is actually using --
# that is a footgun, not an upgrade. SUTRA_SKIP_ELECTRON=1 forces the fallback.
SUTRA_ELECTRON_APP=""
ARCH="$(uname -m)"
# Say WHICH app is being installed. electron/node_modules is gitignored, so a
# fresh clone never has it -- silently shipping the script bundle left people
# thinking they had the desktop app when they did not.
if [ "${SUTRA_SKIP_ELECTRON:-0}" = "1" ]; then
  note "SUTRA_SKIP_ELECTRON=1 -- installed the script-based app on purpose."
elif [ ! -d "$REPO/electron/node_modules" ] && command -v npm >/dev/null 2>&1; then
  # The desktop app is the POINT of this installer, and electron/node_modules is
  # gitignored, so every fresh clone landed here and silently got the script
  # bundle -- i.e. a browser window -- while the operator believed they had
  # installed a desktop app. Telling them to run one obvious command themselves
  # is not a design; run it. Failure still falls through to the note below, so a
  # machine with no network is no worse off than before.
  say "installing Electron dependencies (first run: this downloads Electron, ~1-2 min)"
  if ( cd "$REPO/electron" && npm install --no-audit --no-fund ) >/dev/null 2>&1; then
    say "electron/node_modules installed"
  else
    note "npm install failed in $REPO/electron, so the script-based app was
    installed instead. Run it by hand to see the error:
      cd \"$REPO/electron\" && npm install"
  fi
fi
if [ "${SUTRA_SKIP_ELECTRON:-0}" != "1" ] && [ ! -d "$REPO/electron/node_modules" ]; then
  note "Installed the SCRIPT-BASED app, not the Electron desktop app.
    electron/node_modules is missing and npm is not on PATH. To get the desktop app:
      cd \"$REPO/electron\" && npm install
      cd \"$REPO\" && ./install.sh"
elif [ "${SUTRA_SKIP_ELECTRON:-0}" != "1" ] && ! command -v npx >/dev/null 2>&1; then
  note "Installed the SCRIPT-BASED app: npx (Node.js) was not found on PATH.
    Install Node 18+ and re-run this script to get the Electron desktop app."
fi
if [ "${SUTRA_SKIP_ELECTRON:-0}" != "1" ] \
   && [ -d "$REPO/electron/node_modules" ] \
   && command -v npx >/dev/null 2>&1; then
  say "building the Electron desktop shell"
  # electron-packager reads build/<App>.icns (see --icon below). make_icon only ran
  # for the SCRIPT bundle, further down, so the Electron app shipped with whatever
  # stale .icns happened to be committed -- or none. Regenerate it here, from the
  # same black-plated source, so both bundles carry the same current icon.
  mkdir -p "$REPO/electron/build"
  if make_icon "$MARK_SVG" "$REPO/electron/build/$APP_NAME.icns"; then
    say "icon: refreshed electron/build/$APP_NAME.icns from $(basename "$MARK_SVG")"
  else
    note "could not rasterise $MARK_SVG for the Electron bundle; it will use whatever icon is already in electron/build/"
  fi
  # --no-install: without it, a missing local bin makes npx silently DOWNLOAD
  # and run the deprecated legacy `electron-packager` package from the
  # registry -- remote code execution during install, with output suppressed.
  if ( cd "$REPO/electron" \
       && npx --no-install electron-packager . "$APP_NAME" --platform=darwin --arch="$ARCH" \
            --out=dist --overwrite --app-bundle-id="$BUNDLE_ID" --app-version=1.0.0 \
            --icon=build/"$APP_NAME".icns --prune=true ) >/dev/null 2>&1; then
    SUTRA_ELECTRON_APP="$REPO/electron/dist/$APP_NAME-darwin-$ARCH/$APP_NAME.app"
    [ -d "$SUTRA_ELECTRON_APP" ] || SUTRA_ELECTRON_APP=""
  fi
  [ -n "$SUTRA_ELECTRON_APP" ] || note "the Electron build failed, so the script-based bundle was installed instead. Build it by hand to see the error:  (cd $REPO/electron && npm install && npm run package)"
fi

if [ -n "$SUTRA_ELECTRON_APP" ]; then
  rm -r -f "$APP"
  mkdir -p "$APPS_DIR"
  cp -R "$SUTRA_ELECTRON_APP" "$APP" || die "could not copy the Electron bundle to $APP"
  # --deep because the bundle carries the Electron framework and helper apps.
  if command -v codesign >/dev/null 2>&1; then
    codesign --force --deep --sign - "$APP" >/dev/null 2>&1 \
      || note "codesign failed on the Electron bundle; it still runs."
  fi
  touch "$APP"
  wrote "$APP"
  [ -x "$LSREGISTER" ] && "$LSREGISTER" -f "$APP" >/dev/null 2>&1 || true
  say "installed the Electron desktop shell to $APP"
else

STAGE="$(mktemp -d "${TMPDIR:-/tmp}/sutra-app.XXXXXX")"
trap 'rm -r -f "$STAGE"' EXIT
B="$STAGE/$APP_NAME.app"
mkdir -p "$B/Contents/MacOS" "$B/Contents/Resources"

emit_launcher app "$B/Contents/MacOS/$APP_NAME"

ICON_LINE=""
if make_icon "$MARK_SVG" "$B/Contents/Resources/$APP_NAME.icns"; then
  ICON_LINE="	<key>CFBundleIconFile</key>
	<string>$APP_NAME</string>"
  say "icon: built $APP_NAME.icns from $(basename "$MARK_SVG") using built-in sips + iconutil"
else
  rm -f "$B/Contents/Resources/$APP_NAME.icns"
  say "icon: built-in tools could not rasterise $MARK_SVG -- shipping without an icon"
  note "No app icon: sips/iconutil could not rasterise the SVG. Shipped iconless on purpose rather than adding a dependency."
fi

# The usage strings let macOS name Sutra in the file-access prompt instead of
# denying silently; the ad-hoc signature below gives TCC a stable identity to
# attach the grant to.
cat > "$B/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>CFBundleName</key>
	<string>$APP_NAME</string>
	<key>CFBundleDisplayName</key>
	<string>$APP_NAME</string>
	<key>CFBundleIdentifier</key>
	<string>$BUNDLE_ID</string>
	<key>CFBundleExecutable</key>
	<string>$APP_NAME</string>
	<key>CFBundlePackageType</key>
	<string>APPL</string>
	<key>CFBundleInfoDictionaryVersion</key>
	<string>6.0</string>
	<key>CFBundleShortVersionString</key>
	<string>1.0</string>
	<key>CFBundleVersion</key>
	<string>1</string>
$ICON_LINE
	<key>LSUIElement</key>
	<false/>
	<key>LSMinimumSystemVersion</key>
	<string>11.0</string>
	<key>NSHighResolutionCapable</key>
	<true/>
	<key>LSArchitecturePriority</key>
	<array>
		<string>arm64</string>
		<string>x86_64</string>
	</array>
	<key>NSDesktopFolderUsageDescription</key>
	<string>Sutra runs the panel server from your Sutra checkout.</string>
	<key>NSDocumentsFolderUsageDescription</key>
	<string>Sutra runs the panel server from your Sutra checkout.</string>
	<key>NSDownloadsFolderUsageDescription</key>
	<string>Sutra runs the panel server from your Sutra checkout.</string>
</dict>
</plist>
PLIST

plutil -lint "$B/Contents/Info.plist" >/dev/null || die "generated Info.plist is malformed"

rm -r -f "$APP"
mkdir -p "$APPS_DIR"
cp -R "$B" "$APP" || die "could not copy the bundle to $APP"
touch "$APP"
wrote "$APP"
wrote "$APP/Contents/MacOS/$APP_NAME"
wrote "$APP/Contents/Info.plist"
if [ -f "$APP/Contents/Resources/$APP_NAME.icns" ]; then wrote "$APP/Contents/Resources/$APP_NAME.icns"; fi

# ad-hoc signature: built into macOS (no new dependency), and without it TCC
# has no stable identity for the bundle so any granted access is forgotten.
if command -v codesign >/dev/null 2>&1; then
  if codesign --force --sign - "$APP" >/dev/null 2>&1; then
    say "signed $APP_NAME.app ad-hoc (codesign -s -)"
  else
    note "codesign failed; the app still runs but macOS may forget any file-access grant between launches."
  fi
fi
[ -x "$LSREGISTER" ] && "$LSREGISTER" -f "$APP" >/dev/null 2>&1 || true
say "installed $APP"
fi   # end: Electron shell vs script-bundle fallback


# --------------------------------------------------------------------------
# 5. the CLI
# --------------------------------------------------------------------------
step "cli"
mkdir -p "$CLI_DIR"
emit_launcher cli "$CLI_PATH"
wrote "$CLI_PATH"
say "installed $CLI_PATH"

case ":$PATH:" in
  *":$CLI_DIR:"*) say "$CLI_DIR is already on PATH" ;;
  *)
    note "$CLI_DIR is NOT on your PATH. Add this line to your shell profile yourself (this script does not edit it):

      export PATH=\"\$HOME/.local/bin:\$PATH\"

    For zsh that file is ~/.zshrc. Open a new shell afterwards, or run: source ~/.zshrc
    Until then, invoke the CLI by full path: $CLI_PATH"
    ;;
esac


# --------------------------------------------------------------------------
# 6. protected-location warning (the one thing that bites the .app)
# --------------------------------------------------------------------------
case "$REPO/" in
  "$HOME/Desktop/"*|"$HOME/Documents/"*|"$HOME/Downloads/"*)
    note "Your checkout lives under a macOS protected folder:
      $REPO
    That is fine. install.sh copied the runtime and built the venv under
      $SUTRA_STAGE
    which macOS does NOT protect, so Sutra.app reads only from there and needs
    no Full Disk Access grant. The consequence: the .app runs the STAGED copy,
    so after editing the checkout you must re-run install.sh to pick it up."
    ;;
esac


# --------------------------------------------------------------------------
# 7. summary
# --------------------------------------------------------------------------
step "summary"
say "paths written:"
for p in "${WROTE[@]}"; do say "  $p"; done
say ""
say "runtime files created on first launch (not by this script):"
say "  ~/.sutra-native/run/sutra-ui-<port>.pid"
say "  ~/.sutra-native/run/sutra-app.log   (why a Finder launch failed)"
say ""
say "left untouched:"
say "  $REPO                     (your checkout -- edit here, then re-run install.sh)"
say "  ~/.sutra-native/user-kit  (the real registry the panel reads)"
say ""
note "Governance-log views read from SUTRA_REPO_ROOT, which this install set to
      $CHECKOUT_ROOT
    Those views stay EMPTY unless that tree actually contains .sutra/ and
    .enforcement/ logs. Point SUTRA_REPO_ROOT at your governed project to change it."
say "the app serves on a fixed port:"
say "  http://127.0.0.1:$SUTRA_CANONICAL_PORT"
say ""
say "run it:"
say "  open -a $APP_NAME"
say "  $CLI_NAME                 # from any directory (picks a free port)"
say "  $REPO/install.sh --uninstall"

if [ ${#NOTES[@]} -gt 0 ]; then
  say ""
  say "notes:"
  for n in "${NOTES[@]}"; do say "  - $n"; done
fi
