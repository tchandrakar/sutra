"""providers.py -- which AI CLIs are ACTUALLY usable on this machine.

The panel was hardcoded to `claude`. Making it "multi-provider" by listing
three names in a dropdown would be worse than hardcoding: it would offer the
operator two choices that cannot run, and only reveal that after they pick
one. So availability is decided by two INDEPENDENT observations, both made
fresh on every call:

  installed   = shutil.which(<bin>) is not None      -- can we exec it?
  configured  = <config dir> is a directory          -- has it ever been set up?

  adapter     = <id> in ADAPTERS                     -- can WE drive it?

Neither is inferred from the others. A provider is `runnable` only when ALL
THREE hold, and `reason` states exactly which one failed, naming the path or
the missing capability, so the UI never says "unavailable" without saying why.

The third signal exists because the first two are properties of the MACHINE and
the third is a property of THIS CODEBASE. Installing the codex CLI makes codex
installed+configured within seconds, but the panel still cannot drive it: the
chat channel speaks Claude's `-p --output-format stream-json` protocol and
app.py refuses any other provider outright. Without `adapter`, installing a CLI
silently promoted it to selectable and the failure only surfaced after the
operator picked it and sent a message. That is precisely the "offer a choice
that cannot run" failure this module was written to prevent.

When an adapter is added, add its id to ADAPTERS -- that is the only change
required here.

SETTINGS live here too (rather than in org_api) because three callers need
the same file and the same validation: the settings endpoints, the provider
selector, and app.py's ws_chat. One reader, one writer, one set of defaults.

Reads: PATH, and the config dirs (existence only -- never their contents).
Writes: exactly one file, ~/.sutra-ui/settings.json, via save_settings().
        Never anything under SUTRA_NATIVE_HOME.
"""
import json
import os
import shutil
import subprocess
from pathlib import Path

# ------------------------------------------------------------ login PATH ---
# A .app launched from Finder/Dock inherits launchd's PATH -- typically
# /usr/bin:/bin:/usr/sbin:/sbin -- NOT the PATH from the operator's shell rc.
# Every user-installed CLI lives outside that set: Homebrew puts `claude` in
# /opt/homebrew/bin, npm -g in ~/.npm-global/bin, and so on. The result was a
# desktop app reporting "binary 'claude' not on PATH (config found at ~/.claude)"
# on a machine where `claude` runs fine in any terminal -- chat dead, with the
# reason pointing at the wrong thing. This is a property of GUI LAUNCH, not of
# the machine, so it is repaired here rather than documented as a limitation.
_LOGIN_PATH_DONE = False


def _login_shell_path():
    """The PATH the operator's own login shell produces, or None.

    Asking the shell beats hardcoding directories: it picks up nvm, asdf, pyenv
    and hand-edited rc files, none of which are guessable. `-l` runs the login rc
    chain, which is what Terminal.app itself does.
    """
    sh = os.environ.get("SHELL") or "/bin/zsh"
    if not os.path.isfile(sh):
        sh = "/bin/zsh" if os.path.isfile("/bin/zsh") else "/bin/sh"
    try:
        # `command -p echo` sidesteps an rc-defined echo alias mangling the output.
        out = subprocess.run([sh, "-l", "-c", 'command -p echo "$PATH"'],
                             capture_output=True, text=True, timeout=8)
    except (OSError, subprocess.SubprocessError):
        return None
    lines = [l.strip() for l in (out.stdout or "").splitlines() if l.strip()]
    if not lines:
        return None
    # An rc file that prints a banner puts junk on earlier lines; PATH is the last
    # thing echoed. Require it to actually look like a PATH before trusting it.
    cand = lines[-1]
    return cand if (os.pathsep in cand and cand.startswith("/")) else None


def ensure_login_path():
    """Merge the login shell's PATH into this process, once, only if needed.

    A NO-OP when a catalogued binary already resolves: the CLI and dev-server
    launches inherit a correct PATH, and spawning a login shell every start would
    add latency and execute the operator's rc files for nothing. Only a GUI launch
    needs this.

    APPENDS rather than replaces, so a PATH deliberately set for this process still
    takes precedence over whatever the rc files say.

    Returns True only when it actually changed PATH.
    """
    global _LOGIN_PATH_DONE
    if _LOGIN_PATH_DONE:
        return False
    _LOGIN_PATH_DONE = True

    if any(shutil.which(spec["bin"]) for spec in _CATALOG):
        return False                     # PATH already resolves something; leave it

    extra = _login_shell_path()
    if not extra:
        return False
    have = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    added = [p for p in extra.split(os.pathsep) if p and p not in have]
    if not added:
        return False
    os.environ["PATH"] = os.pathsep.join(have + added)
    return True

# ------------------------------------------------------------- settings ----
# Outside SUTRA_NATIVE_HOME by design -- panel preferences are not governance
# state. SUTRA_UI_SETTINGS exists so tests can point at a tempdir instead of
# the operator's real file.
SETTINGS_PATH = Path(os.path.expanduser(
    os.environ.get("SUTRA_UI_SETTINGS", "~/.sutra-ui/settings.json")))

# The permission modes the `claude` CLI's --permission-mode accepts that make
# sense for a panel-driven session.
#   plan               read/plan only; every edit needs an explicit approval.
#   acceptEdits        AUTO-APPROVES file writes/edits by the spawned agent.
#                      It will create, modify and delete files under `workdir`
#                      with no per-edit prompt. Opt in deliberately.
#   bypassPermissions  approves everything, including shell commands. Widest.
PERMISSION_MODES = ("plan", "acceptEdits", "bypassPermissions")
DEFAULT_PERMISSION_MODE = "plan"
DEFAULT_WORKDIR = "~/sutra-ui-workspace"

# Modes that let the spawned agent act without asking. The panel's settings
# endpoint is unauthenticated by construction (it is a localhost control
# plane), so anything that can reach the port could otherwise raise the
# ceiling to "auto-approve shell commands" and the operator would only learn
# about it from a status frame. Selecting these requires an explicit
# server-side opt-in the operator sets when STARTING the server -- i.e. out of
# band from anything reachable over the socket.
UNSAFE_PERMISSION_MODES = ("acceptEdits", "bypassPermissions")
UNSAFE_MODES_ENV = "SUTRA_UI_ALLOW_UNSAFE_PERM_MODES"


def unsafe_modes_allowed():
    return os.environ.get(UNSAFE_MODES_ENV, "") == "1"


def effective_permission_mode(mode):
    """Clamp a stored/env mode down to `plan` unless unsafe modes are enabled.

    Gating only the WRITE path (save_settings) is not enough: a settings.json
    left behind by an older build, edited by hand, or written by another local
    process would still reach the subprocess spawn. Callers must pass the mode
    through here at the point of USE, not trust what was persisted.
    """
    if mode in UNSAFE_PERMISSION_MODES and not unsafe_modes_allowed():
        return DEFAULT_PERMISSION_MODE
    return mode if mode in PERMISSION_MODES else DEFAULT_PERMISSION_MODE


def workdir_allowed(path):
    """True if `path` is inside a directory the panel may use as an agent cwd.

    The workdir becomes the spawned agent's cwd, so an arbitrary path turns the
    chat endpoint into a read oracle over anywhere on disk. Confine it to $HOME
    (or an explicit operator-set root) unless unsafe modes are enabled.
    """
    root = os.path.realpath(os.path.expanduser(
        os.environ.get("SUTRA_UI_WORKDIR_ROOT", "~")))
    target = os.path.realpath(os.path.expanduser(path))
    return target == root or target.startswith(root + os.sep)

PERMISSION_MODE_NOTES = {
    "plan": "read-only planning: the agent proposes edits, you approve each one.",
    "acceptEdits": "the agent WRITES FILES without asking -- it can create, "
                   "modify and delete files under the workdir. Opt in knowingly.",
    "bypassPermissions": "everything is auto-approved, including shell commands "
                         "-- the widest setting there is.",
}

# Providers this codebase can actually DRIVE. Keep in lockstep with app.py's
# ws_chat guard (`if active_id != "claude": ... no adapter`). Adding an id here
# without writing its adapter re-creates the bug this set exists to prevent.
ADAPTERS = frozenset({"claude"})

# ------------------------------------------------------------- catalog -----
# Order is precedence order for the "first runnable provider" fallback.
# `default` marks the one the panel ships pointed at.
_CATALOG = (
    {"id": "claude", "name": "Claude Code", "bin": "claude",
     "config_dir": "~/.claude", "default": True},
    {"id": "codex", "name": "OpenAI Codex", "bin": "codex",
     "config_dir": "~/.codex", "default": False},
    {"id": "gemini", "name": "Gemini CLI", "bin": "gemini",
     "config_dir": "~/.gemini", "default": False},
)


def _bin_for(pid, fallback):
    """SUTRA_UI_CLAUDE_BIN predates this module (app.py read it directly); the
    generic form keeps that override working and gives the other two the same
    escape hatch, e.g. SUTRA_UI_CODEX_BIN=/opt/foo/codex."""
    return os.environ.get("SUTRA_UI_%s_BIN" % pid.upper(), fallback)


def _describe(spec):
    """One provider's live state. `installed` is shutil.which() and NOTHING
    else -- a config directory is not evidence of a binary, and this function
    must never claim otherwise."""
    binary = _bin_for(spec["id"], spec["bin"])
    bin_path = shutil.which(binary)
    installed = bin_path is not None

    cfg_display = spec["config_dir"]
    cfg_path = Path(os.path.expanduser(cfg_display))
    configured = cfg_path.is_dir()

    adapter = spec["id"] in ADAPTERS

    if installed and configured and adapter:
        reason = None
    elif installed and configured and not adapter:
        reason = ("no chat adapter yet -- this panel drives Claude's "
                  "stream-json protocol only, so %s cannot be used here even "
                  "though it is installed at %s" % (spec["name"], bin_path))
    elif configured and not installed:
        reason = "binary %r not on PATH (config found at %s)" % (binary, cfg_display)
    elif installed and not configured:
        reason = "binary %r found at %s but no config directory at %s" % (
            binary, bin_path, cfg_display)
    else:
        reason = "no binary and no config directory"

    return {
        "id": spec["id"],
        "name": spec["name"],
        "bin": binary,
        "installed": installed,
        "configured": configured,
        "config_dir": cfg_display,
        "reason": reason,
        "default": spec["default"],
        # extras -- not part of the required shape, but the UI would otherwise
        # have to re-derive them and could disagree with this module
        "adapter": adapter,
        "runnable": installed and configured and adapter,
        "bin_path": bin_path,
        "config_path": str(cfg_path),
    }


def discover_providers():
    """Every catalogued provider, in precedence order, with live state."""
    return [_describe(spec) for spec in _CATALOG]


def provider_by_id(pid):
    """One provider's live state, or None if `pid` is not catalogued."""
    if not pid:
        return None
    for spec in _CATALOG:
        if spec["id"] == pid:
            return _describe(spec)
    return None


def runnable_providers():
    return [p for p in discover_providers() if p["runnable"]]


def provider_bin(pid):
    """Absolute path to the provider's binary, or None. Callers spawn this;
    None means do not spawn -- report the provider's `reason` instead."""
    p = provider_by_id(pid)
    return p["bin_path"] if p else None


# ------------------------------------------------------------ settings io --

def _raw_settings():
    """The settings file as-is, or {} when absent/unreadable. Never raises --
    a corrupt preferences file must not take the panel down."""
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _clean_permission_mode(value):
    return value if value in PERMISSION_MODES else None


# -------------------------------------------------------------- active -----

def active_provider_detail():
    """Resolve the ACTIVE provider and say where the choice came from.

    Precedence, highest first:
      1. SUTRA_UI_PROVIDER   -- explicit operator intent for this process
      2. settings.json       -- the last thing chosen in the UI
      3. first runnable      -- catalog order (claude leads, and is `default`)
      4. None                -- nothing on this machine can run

    An override at (1) or (2) is honoured ONLY if it names a runnable
    provider. Handing back an id whose binary is absent would push the failure
    down into create_subprocess_exec, where it surfaces as a dead socket. When
    an override is dropped, `ignored` records what was asked for and why, so
    the UI can say so out loud rather than silently substituting.
    """
    ignored = []

    env_choice = os.environ.get("SUTRA_UI_PROVIDER")
    file_choice = _raw_settings().get("provider")

    for source, choice in (("env", env_choice), ("settings", file_choice)):
        if not choice:
            continue
        p = provider_by_id(choice)
        if p is None:
            ignored.append({"source": source, "id": choice,
                            "reason": "unknown provider id"})
            continue
        if not p["runnable"]:
            ignored.append({"source": source, "id": choice, "reason": p["reason"]})
            continue
        return {"id": p["id"], "source": source, "ignored": ignored}

    for p in discover_providers():
        if p["runnable"]:
            return {"id": p["id"], "source": "fallback", "ignored": ignored}

    return {"id": None, "source": "none", "ignored": ignored}


def active_provider():
    """The active provider id, or None when nothing on this machine is runnable."""
    return active_provider_detail()["id"]


def load_settings():
    """Effective settings: file values where valid, documented defaults
    otherwise. Always returns all three contract keys (provider,
    permission_mode, workdir) plus metadata explaining how each was reached.

    permission_mode defaults to "plan" (SAFETY rule 4 / test_perm_mode_default);
    SUTRA_UI_PERMISSION_MODE still supplies the default when no valid value is
    stored, so existing deployments keep their behaviour.
    """
    raw = _raw_settings()
    invalid = {}

    mode = _clean_permission_mode(raw.get("permission_mode"))
    if raw.get("permission_mode") is not None and mode is None:
        invalid["permission_mode"] = raw.get("permission_mode")
    if mode is None:
        mode = _clean_permission_mode(
            os.environ.get("SUTRA_UI_PERMISSION_MODE")) or DEFAULT_PERMISSION_MODE

    workdir = raw.get("workdir")
    if not isinstance(workdir, str) or not workdir.strip():
        if workdir is not None:
            invalid["workdir"] = workdir
        workdir = os.environ.get("SUTRA_UI_WORKDIR", DEFAULT_WORKDIR)
    workdir = os.path.expanduser(workdir)

    detail = active_provider_detail()
    stored = raw.get("provider")
    if stored and provider_by_id(stored) is None:
        invalid["provider"] = stored

    # The mode that will ACTUALLY reach the subprocess spawn. `permission_mode`
    # above is what is stored/requested; the two diverge whenever an unsafe mode
    # is on file without the out-of-band opt-in. Reporting only the stored value
    # let the panel state "nothing will prompt you per edit" while ws_chat was
    # in fact spawning `plan` -- the UI asserted authority the agent did not
    # have. Both values ship, and the UI renders the effective one.
    effective = effective_permission_mode(mode)
    clamped = effective != mode

    # First run is a PROPERTY OF THE SETTINGS FILE, not a browser flag: the
    # onboarding explains what this panel will do with the operator's machine
    # (which CLI it drives, where it writes, what authority the agent gets), so
    # clearing localStorage or opening it in another browser must not skip it.
    onboarded = raw.get("onboarded") is True

    return {
        "provider": detail["id"],
        "permission_mode": mode,
        "workdir": workdir,
        "onboarded": onboarded,
        # metadata -- the three keys above are the contract; these explain them
        "provider_source": detail["source"],
        "provider_stored": stored,
        "provider_ignored": detail["ignored"],
        "permission_mode_note": PERMISSION_MODE_NOTES.get(mode),
        # effective-vs-stored: what runs, whether it was clamped, and how to unlock
        "permission_mode_effective": effective,
        "permission_mode_effective_note": PERMISSION_MODE_NOTES.get(effective),
        "permission_mode_clamped": clamped,
        "permission_mode_clamp_reason": (
            "%r auto-approves agent actions, so it is not honoured unless the "
            "server was started with %s=1. The session runs as %r instead."
            % (mode, UNSAFE_MODES_ENV, effective)) if clamped else None,
        "unsafe_modes_allowed": unsafe_modes_allowed(),
        "unsafe_modes_env": UNSAFE_MODES_ENV,
        # The root a workdir must sit under. Surfaced so the picker can state the
        # constraint UP FRONT rather than letting the operator type a path and
        # discover the rule from a 400.
        "workdir_root": os.path.realpath(os.path.expanduser(
            os.environ.get("SUTRA_UI_WORKDIR_ROOT", "~"))),
        "settings_path": str(SETTINGS_PATH),
        "settings_file_exists": SETTINGS_PATH.exists(),
        "invalid_stored_values": invalid,
    }


def save_settings(provider=None, permission_mode=None, workdir=None, onboarded=None):
    """Merge a partial update into the settings file and return load_settings().

    Validates BEFORE writing: an unknown or unrunnable provider, or an unknown
    permission_mode, raises ValueError carrying the specific reason. Written
    tmp+replace so a crash mid-write cannot leave a truncated file.
    """
    raw = _raw_settings()

    if provider is not None:
        p = provider_by_id(provider)
        if p is None:
            raise ValueError(
                "unknown provider %r -- known ids: %s"
                % (provider, ", ".join(s["id"] for s in _CATALOG)))
        if not p["runnable"]:
            raise ValueError("provider %r is not runnable: %s" % (provider, p["reason"]))
        raw["provider"] = p["id"]

    if permission_mode is not None:
        if permission_mode not in PERMISSION_MODES:
            raise ValueError(
                "unknown permission_mode %r -- must be one of: %s"
                % (permission_mode, ", ".join(PERMISSION_MODES)))
        if permission_mode in UNSAFE_PERMISSION_MODES and not unsafe_modes_allowed():
            raise ValueError(
                "permission_mode %r auto-approves agent actions and cannot be set "
                "over the API. Restart the server with %s=1 to enable it."
                % (permission_mode, UNSAFE_MODES_ENV))
        raw["permission_mode"] = permission_mode

    if workdir is not None:
        if not isinstance(workdir, str) or not workdir.strip():
            raise ValueError("workdir must be a non-empty path string")
        expanded = os.path.expanduser(workdir.strip())
        if not workdir_allowed(expanded):
            raise ValueError(
                "workdir %r is outside the allowed root. Set SUTRA_UI_WORKDIR_ROOT "
                "when starting the server to widen it." % expanded)
        raw["workdir"] = expanded

    if onboarded is not None:
        if not isinstance(onboarded, bool):
            raise ValueError("onboarded must be a boolean")
        raw["onboarded"] = onboarded

    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = SETTINGS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(raw, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(SETTINGS_PATH)
    return load_settings()


if __name__ == "__main__":
    for prov in discover_providers():
        print("%-8s installed=%-5s configured=%-5s  %s"
              % (prov["id"], prov["installed"], prov["configured"],
                 prov["reason"] or "runnable (%s)" % prov["bin_path"]))
    print()
    print("active:   %s" % json.dumps(active_provider_detail(), sort_keys=True))
    print("settings: %s" % json.dumps(load_settings(), indent=2, sort_keys=True))
