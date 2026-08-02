"""skills_catalog.py -- discover the slash commands `claude` can actually resolve.

Backs two surfaces in the studio:
  1. the composer's "/" palette (type / -> pick a real command)
  2. the Skills screen

Both previously showed a HARDCODED list of ten strings and claimed "31 total".
There are 39 real entries and the ten strings were not read from anywhere --
fabricated data rendered as fact. This module reads the same directories the
`claude` CLI resolves, so what the palette offers is what will actually run.

SOURCES (read-only, never written):
  ~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/commands/*.md
      -> /<plugin>:<name>          e.g. /core:start
  ~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/skills/<name>/SKILL.md
      -> /<plugin>:<name>          e.g. /core:flow
  ~/.claude/commands/*.md          -> /<name>   e.g. /cp
  <project>/.claude/commands/*.md  -> /<name>   (project-local, highest specificity)

FRONTMATTER IS OPTIONAL. Plugin commands carry `name:` / `description:` YAML;
the operator's own commands (e.g. ~/.claude/commands/cp.md) carry none at all.
A parser that assumed YAML would silently drop exactly the commands the
operator wrote themselves, so the fallback is filename + first prose line.
No PyYAML either -- it is not in requirements.txt and this ships inside a
plugin; the frontmatter shapes in play are simple enough to lex directly.

MULTI-PROVIDER (discover_all)
-----------------------------
The ~/.claude scan above is unchanged -- it works, it finds 37 entries here,
and it stays the definition of what `claude` exposes. What is added is the
same honesty applied to the OTHER configured assistants: read whatever
capability surface each one actually has on disk, and say plainly when that
surface is empty or cannot run.

The temptation here is to normalise every provider into "a list of slash
commands", because that is the shape the palette wants. That would be
fabrication. Codex does not have a slash-command catalog on this machine:
~/.codex/AGENTS.md is a PROMPT/persona file that is always loaded, and
~/.codex/skills/**/SKILL.md are model-selected skills, not typed commands.
So AGENTS.md is surfaced as ONE `instructions` entry with slash=None, and
codex skills carry slash=None too. Inventing "/agents" would put a command
in the palette that resolves to nothing.

Gemini has no config directory at all, so it contributes ZERO entries. Not a
placeholder, not a greyed-out sample -- zero, with the reason attached.

Every entry carries:
  provider          which assistant it came from
  runnable          False when that assistant's binary is not on PATH (and
                    False for `instructions`, which is loaded, never invoked)
  runnable_reason   why, when runnable is False
"""
import os
import re
from pathlib import Path

import providers

CLAUDE_HOME = Path(os.path.expanduser("~/.claude"))
PLUGIN_CACHE = CLAUDE_HOME / "plugins" / "cache"
USER_COMMANDS = CLAUDE_HOME / "commands"

_FM = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.S)

_VER_LEAD = re.compile(r"^(\d+(?:\.\d+)*)(.*)$", re.S)


def _version_key(name):
    """Sort key for a cached plugin VERSION DIRECTORY.

    These were sorted as plain strings, which is lexical: "2.9.0" > "2.63.0" and
    "2.99.0" > "2.100.0". With two versions cached the panel then read the OLDER
    one and reported its commands as current -- a silent wrong answer, and exactly
    the failure the auto-refresh work exists to prevent.

    Ordering, highest last:
      1. the leading dotted-numeric run, compared NUMERICALLY  (2.63.0 > 2.9.0)
      2. release before prerelease                             (2.63.0 > 2.63.0-rc1)
      3. the remaining text, as a deterministic tiebreak       (-rc2 > -rc1)

    A directory with no leading digits ("beta") gets an empty numeric tuple, so it
    sorts below every real version rather than raising.
    """
    m = _VER_LEAD.match(name)
    if not m:
        return ((), 0, name)
    nums = tuple(int(p) for p in m.group(1).split("."))
    rest = m.group(2)
    # 1 == plain release. A suffix of ANY kind (-rc1, +build, .dev) ranks below the
    # bare version, which is the semver rule and the safe direction: never prefer a
    # prerelease build's command list over the released one.
    return (nums, 1 if rest == "" else 0, rest)


def _parse_frontmatter(text):
    """Return (dict, body). Handles `key: value` and YAML folded scalars
    (`key: >` / `key: |` followed by indented lines) -- the description field
    in every shipped SKILL.md uses the folded form."""
    m = _FM.match(text)
    if not m:
        return {}, text
    block, body = m.group(1), text[m.end():]
    out, key, folded = {}, None, []
    for line in block.split("\n"):
        if folded is not None and key and (line.startswith((" ", "\t")) or not line.strip()):
            if line.strip():
                folded.append(line.strip())
            continue
        if key and folded:
            out[key] = " ".join(folded).strip()
            key, folded = None, []
        m2 = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", line)
        if not m2:
            continue
        k, v = m2.group(1), m2.group(2).strip()
        if v in (">", "|", ">-", "|-"):
            key, folded = k, []
        else:
            out[k] = v.strip('"').strip("'")
    if key and folded:
        out[key] = " ".join(folded).strip()
    return out, body


def _first_prose_line(body, limit=200):
    """Fallback description for a file with no frontmatter: the first line
    that is not blank, not a heading, not a rule."""
    for line in body.split("\n"):
        s = line.strip()
        if not s or s.startswith(("#", "---", "```", "<!--")):
            continue
        return s[:limit]
    return ""


def _entry(slash, name, kind, source, text, provider="claude"):
    fm, body = _parse_frontmatter(text)
    desc = (fm.get("description") or _first_prose_line(body) or "").strip()
    return {
        "slash": slash,
        "name": fm.get("name") or name,
        "kind": kind,                 # "command" | "skill" | "prompt" | "instructions"
        "source": source,             # "plugin:<name>" | "user" | "project" | "codex"
        "provider": provider,         # which assistant exposes it
        "description": re.sub(r"\s+", " ", desc)[:300],
        # a command whose frontmatter opts out of model invocation is still
        # typeable by the operator -- surface the flag rather than hiding it
        "model_invocable": fm.get("disable-model-invocation", "false").lower() != "true",
    }


def _read(p):
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def discover(project_dir=None):
    """Every slash command resolvable here, deduped by `slash`.

    Precedence when the same slash appears twice: project > user > plugin
    (most specific wins), matching how the CLI itself resolves them.
    """
    found = {}

    def add(e, rank):
        prev = found.get(e["slash"])
        if prev is None or rank > prev["_rank"]:
            e["_rank"] = rank
            found[e["slash"]] = e

    # --- plugins (rank 0) -------------------------------------------------
    if PLUGIN_CACHE.is_dir():
        for marketplace in sorted(PLUGIN_CACHE.iterdir()):
            if not marketplace.is_dir():
                continue
            for plugin in sorted(marketplace.iterdir()):
                if not plugin.is_dir() or plugin.name.startswith("."):
                    continue
                # newest version dir wins if several are cached
                versions = sorted([d for d in plugin.iterdir()
                                   if d.is_dir() and not d.name.startswith(".")],
                                  key=lambda d: _version_key(d.name))
                if not versions:
                    continue
                root = versions[-1]
                ns = plugin.name
                for md in sorted((root / "commands").glob("*.md")) \
                        if (root / "commands").is_dir() else []:
                    add(_entry("/%s:%s" % (ns, md.stem), md.stem, "command",
                               "plugin:%s" % ns, _read(md)), 0)
                skills_dir = root / "skills"
                if skills_dir.is_dir():
                    for sk in sorted(skills_dir.iterdir()):
                        f = sk / "SKILL.md"
                        if sk.is_dir() and f.is_file():
                            add(_entry("/%s:%s" % (ns, sk.name), sk.name, "skill",
                                       "plugin:%s" % ns, _read(f)), 0)

    # --- user-level (rank 1) ---------------------------------------------
    if USER_COMMANDS.is_dir():
        for md in sorted(USER_COMMANDS.glob("*.md")):
            add(_entry("/%s" % md.stem, md.stem, "command", "user", _read(md)), 1)

    # --- project-local (rank 2) ------------------------------------------
    if project_dir:
        pdir = Path(project_dir) / ".claude" / "commands"
        if pdir.is_dir():
            for md in sorted(pdir.glob("*.md")):
                add(_entry("/%s" % md.stem, md.stem, "command", "project", _read(md)), 2)

    out = sorted(found.values(), key=lambda e: (e["kind"] != "command", e["slash"]))
    # Annotation only -- the scan above is byte-for-byte the one that already
    # returns 37 entries here. A command is only `runnable` if the binary that
    # resolves it is actually on PATH; ~/.claude existing proves nothing.
    claude = providers.provider_by_id("claude") or {}
    for e in out:
        e.pop("_rank", None)
        e["provider"] = "claude"
        e["runnable"] = bool(claude.get("runnable"))
        e["runnable_reason"] = None if claude.get("runnable") else claude.get("reason")
    return out



# ============================ other providers ==============================
# One reader per provider whose on-disk layout we have actually looked at.
# A provider with a config directory but NO reader here is reported as
# "unread", never as empty -- claiming a surface is empty when we simply did
# not implement its parser is the same lie as inventing entries for it.

def _codex_entries(config_path):
    """What ~/.codex actually exposes. Three surfaces exist on disk; none of
    them is a slash-command catalog, so none of them gets a slash.

      AGENTS.md              -> ONE `instructions` entry. This is the persona/
                                prompt file codex always loads. It is not a
                                command; slash stays None.
      skills/**/SKILL.md     -> `skill` entries, same frontmatter dialect the
                                claude scan already lexes (name/description).
                                Codex selects these by name; the operator does
                                not type them, so again no slash.
      prompts/*.md           -> `prompt` entries. Codex DOES resolve these as
                                /<stem> in its own TUI, so these -- and only
                                these -- get a slash. The directory does not
                                exist on this machine, so today this branch
                                contributes nothing.
    """
    out = []

    agents = config_path / "AGENTS.md"
    if agents.is_file():
        e = _entry(None, "AGENTS.md", "instructions", "codex",
                   _read(agents), provider="codex")
        e["path"] = str(agents)
        # Always in context, never invoked -- so `model_invocable` (which means
        # "the model may call this") is the wrong frame. Say which it is.
        e["model_invocable"] = False
        e["always_loaded"] = True
        out.append(e)

    prompts = config_path / "prompts"
    if prompts.is_dir():
        for md in sorted(prompts.glob("*.md")):
            e = _entry("/%s" % md.stem, md.stem, "prompt", "codex",
                       _read(md), provider="codex")
            e["path"] = str(md)
            out.append(e)

    skills = config_path / "skills"
    if skills.is_dir():
        for f in sorted(skills.rglob("SKILL.md")):
            e = _entry(None, f.parent.name, "skill", "codex",
                       _read(f), provider="codex")
            e["path"] = str(f)
            # ".system" vs a user-installed group: keep the on-disk grouping
            # rather than flattening it away
            try:
                e["group"] = str(f.parent.parent.relative_to(skills))
            except ValueError:
                e["group"] = ""
            out.append(e)

    return out


# provider id -> reader. Absence from this map is meaningful (see _READ_NOTE).
_READERS = {
    "claude": lambda cfg, project_dir: discover(project_dir=project_dir),
    "codex": lambda cfg, project_dir: _codex_entries(cfg),
}


def discover_all(project_dir=None):
    """Every capability surface, across every CONFIGURED provider.

    Returns {"items": [...], "providers": [...]}. `providers` is the honest
    half: it carries the per-provider entry count together with installed/
    configured/reason, so a zero can never be mistaken for "nothing installed
    a plugin" when the real answer is "that assistant is not on this machine".
    """
    items, report = [], []

    for p in providers.discover_providers():
        reader = _READERS.get(p["id"])
        note = None

        if not p["configured"]:
            got = []
            note = ("no config directory at %s -- this provider contributes "
                    "nothing, and nothing was invented for it" % p["config_dir"])
        elif reader is None:
            got = []
            note = ("config directory %s exists but this build has no reader "
                    "for its layout -- reporting 0 entries would claim it is "
                    "empty, which is not known" % p["config_dir"])
        else:
            got = reader(Path(p["config_path"]), project_dir)

        for e in got:
            e["provider"] = p["id"]
            if not p["runnable"]:
                e["runnable"] = False
                e["runnable_reason"] = p["reason"]
            elif e.get("kind") == "instructions":
                e["runnable"] = False
                e["runnable_reason"] = ("loaded as standing instructions, not "
                                        "invoked -- there is nothing to run")
            else:
                e["runnable"] = True
                e["runnable_reason"] = None
        items.extend(got)

        if note is None and got and not p["runnable"]:
            note = ("%d capability file(s) read from %s, but none of them can "
                    "run here: %s" % (len(got), p["config_dir"], p["reason"]))
        if note is None and not got and p["configured"]:
            note = "config directory %s exists but exposes no capability files" % p["config_dir"]

        entry = dict(p)
        entry["entries"] = len(got)
        entry["runnable_entries"] = sum(1 for e in got if e.get("runnable"))
        entry["note"] = note
        entry["reader"] = reader is not None
        report.append(entry)

    return {"items": items, "providers": report}


if __name__ == "__main__":
    import json
    bundle = discover_all()
    items = bundle["items"]
    by_kind, by_source, by_provider = {}, {}, {}
    for e in items:
        by_kind[e["kind"]] = by_kind.get(e["kind"], 0) + 1
        by_source[e["source"]] = by_source.get(e["source"], 0) + 1
        by_provider[e["provider"]] = by_provider.get(e["provider"], 0) + 1
    print("total: %d  (claude-only discover(): %d)" % (len(items), len(discover())))
    print("by kind:     %s" % json.dumps(by_kind, sort_keys=True))
    print("by source:   %s" % json.dumps(by_source, sort_keys=True))
    print("by provider: %s" % json.dumps(by_provider, sort_keys=True))
    print()
    for p in bundle["providers"]:
        print("  %-8s entries=%-3d runnable=%-5s %s"
              % (p["id"], p["entries"], p["runnable"], p["note"] or ""))
    print()
    for e in items:
        if e["provider"] != "claude":
            print("  %-10s %-13s %-8s runnable=%-5s %s"
                  % (e["slash"], e["name"], e["kind"], e["runnable"],
                     (e["description"] or "")[:52]))
    missing = [e["name"] for e in items if not e["description"]]
    print()
    print("entries with NO description: %d %s" % (len(missing), missing[:8]))
