"""org_api.py -- Tier-3 panel backend: read-mostly registry endpoints + one
write endpoint (POST /api/classify, exactly one write_placement() call).

SAFETY (see marketplace/plugin/sutra-ui -- ground truth for this module):
  - Calls ONLY: load_domains, live_refs, tenant_refs, domain_path, charters_for,
    all_placements, charter_body_files, mece_report, lint_full, classify,
    gather_evidence, write_placement, _read_jsonl(CURRENT), charter_view,
    load_sidecar (render-only merge, same family as charter_view), tokenize
    (pure lexer, no I/O -- passed to reorg_sim.matched_terms_for so the
    "which terms matched" answer is lexed by the same function
    score_domains() uses, rather than a near-copy that disagrees at the edges).
  - NEVER calls: resolve, mint_domain, restructure, retire, unretire,
    charter_retire, charter_reassign, consolidate, reconcile, repair,
    verify_charters. test_forbidden_calls.py greps this file for those names
    and fails the build if any appear -- a provable negative.
  - Writes to disk in exactly two places:
      (a) write_placement() -> SUTRA_NATIVE_HOME (classify endpoint only, one
          placement per call)
      (b) the draft file under DRAFTS_DIR (outside SUTRA_NATIVE_HOME, mirrors
          ~/.sutra-ui/drafts/ per the design doc's §8.5.9 "What it writes")
  - SUTRA_NATIVE_HOME resolution: the REAL registry (the engine's own default,
    ~/.sutra-native/user-kit) unless the env var overrides it. No fixture
    default -- an operator must never be shown seeded data dressed as theirs.
    The active root is logged loudly at import so a running server is never
    ambiguous about which registry it reads and writes.
"""
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

logger = logging.getLogger("sutra-ui.org")

# --------------------------------------------------------------- registry ---
# NO FIXTURE DEFAULT. This used to point at a seeded scratch registry so the
# panel always had a full-looking org to render. That was a mock component:
# it showed invented departments, charters and placements as though they were
# yours. The panel now reads the REAL registry -- whatever placement_engine
# resolves from SUTRA_NATIVE_HOME, defaulting to the engine's own default
# (~/.sutra-native/user-kit). If that registry is nearly empty, the panel
# shows it nearly empty, because that is the truth.
#
# Tests still seed an isolated tempfile.mkdtemp() and set SUTRA_NATIVE_HOME
# explicitly -- that is test isolation, not a mock shown to an operator.
_ACTIVE_HOME = os.environ.get("SUTRA_NATIVE_HOME") or os.path.expanduser(
    "~/.sutra-native/user-kit")
os.environ["SUTRA_NATIVE_HOME"] = _ACTIVE_HOME

logger.warning("=" * 72)
logger.warning("SUTRA_NATIVE_HOME (registry root) = %s", _ACTIVE_HOME)
logger.warning("=" * 72)
print("=" * 72, file=sys.stderr)
print("[org_api] SUTRA_NATIVE_HOME (registry root) = %s" % _ACTIVE_HOME, file=sys.stderr)
print("[org_api] WRITES: POST /api/classify appends ONE placement row here.", file=sys.stderr)
print("=" * 72, file=sys.stderr)

_LIB_DIR = str(Path(__file__).resolve().parents[1] / "lib")
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

import placement_engine as E  # noqa: E402  (path insert must precede this import)
import reorg_sim as R  # noqa: E402

import providers  # provider registry + ~/.sutra-ui/settings.json (no engine access)

# --------------------------------------------------------------- drafts ----
# Draft plan storage. Outside SUTRA_NATIVE_HOME by design (§8.5.9 "What it
# writes"): the analogue of ~/.sutra-ui/drafts/ for this dev/test setup.
DRAFTS_DIR = Path(os.path.expanduser(
    os.environ.get("SUTRA_UI_DRAFTS", "~/.sutra-ui/drafts")))
DRAFT_PATH = DRAFTS_DIR / "draft.json"

router = APIRouter(prefix="/api", tags=["org"])


# ------------------------------------------------------- tenant scoping ----
# Every read endpoint below used to hardcode
#   os.environ.get("PLACEMENT_TENANT", "T-local")
# which makes the tenant a PROPERTY OF THE SERVER PROCESS. A tenant-first UI
# needs it to be a property of the REQUEST: pick a tenant on the landing
# screen, and every subsequent read is scoped to it without restarting
# uvicorn with a different env var.
#
# The override is passed EXPLICITLY into the engine calls that already take a
# tenant_id (tenant_refs / charters_for / all_placements / mece_report). It is
# deliberately NOT implemented by mutating os.environ["PLACEMENT_TENANT"] for
# the duration of the request: FastAPI runs these sync endpoints in a thread
# pool, so a global mutation would leak across concurrent requests and scope
# one operator's read to another's tenant. (placement_engine reads
# PLACEMENT_TENANT only in its CLI main(), never in the library functions used
# here -- verified -- so passing it explicitly is complete, not partial.)
#
# Absent ?tenant=, behaviour is byte-identical to before.

def _tenant_of_record(tenant=None):
    """The tenant this request is about: ?tenant= if given, else the server's
    PLACEMENT_TENANT, else "T-local" (the engine's own default)."""
    return (tenant or "").strip() or os.environ.get("PLACEMENT_TENANT", "T-local")


def _scope(tenant=None, all_tenants=False):
    """tenant_id to pass to the engine: None means "every tenant"."""
    return None if all_tenants else _tenant_of_record(tenant)


def _charter_ids_in_scope(scope, tenant_id):
    """Unique charter ids homed anywhere in `scope` (live AND retired domains,
    same reason org_charters() does not filter on liveness)."""
    seen = set()
    for ref in scope:
        for body in E.charters_for(ref, tenant_id=tenant_id):
            cid = body.get("id")
            if cid:
                seen.add(cid)
    return seen


# ------------------------------------------------------------- GET /tree ---

@router.get("/org/tree")
def org_tree(include_retired: bool = False, all_tenants: bool = False,
             tenant: Optional[str] = None):
    """Port of the CLI's `tree` (placement_engine.py ~:2566).

    domains = load_domains(); scope = tenant_refs(domains, tenant_id);
    shown = scope if include_retired else live_refs(scope).
    tenant_id resolution: ?tenant= overrides the PLACEMENT_TENANT env var,
    which defaults to "T-local" -- the same default the engine's own
    classify()/mece_report() use. When all_tenants=true, scope is skipped
    (tenant_refs(domains, None) == all); ?tenant= is then still used for the
    ordering below, so "which tenant leads" follows the UI's selection.
    """
    tenant_of_record = _tenant_of_record(tenant)
    tenant_id = None if all_tenants else tenant_of_record
    domains = E.load_domains()
    scope = E.tenant_refs(domains, tenant_id)
    shown = scope if include_retired else E.live_refs(scope)

    rows = []
    for ref, d in shown.items():
        rows.append({
            "ref": ref,
            "path": E.domain_path(ref, domains),
            "name": d.get("name"),
            "origin": d.get("origin"),
            "status": d.get("status", "active"),
            "successor_refs": d.get("successor_refs") or [],
            "touched": d.get("touched_by_operator"),
            "parent_ref": d.get("parent_ref"),
            # raw fields the panel's own client-side logic reads directly --
            # dPath() re-derives the D-path from ts_minted_ms + parent_ref,
            # inTenant()/tenant chips filter on tenant_id, the department
            # inspector prints description + mint_evidence and shows
            # retired_at_ms/retire_reason_code. "path" above is kept too
            # (server-precomputed via the same tie-break as dPath()) so
            # callers that want it pre-derived still get it; the raw fields
            # let the panel recompute identically once the adapter wiring
            # in app.py's /panel lands.
            "tenant_id": d.get("tenant_id"),
            "description": d.get("description"),
            "mint_evidence": d.get("mint_evidence") or [],
            "ts_minted_ms": d.get("ts_minted_ms"),
            "retired_at_ms": d.get("retired_at_ms"),
            "retire_reason_code": d.get("retire_reason_code"),
        })
    # Sort TENANT OF RECORD FIRST, then by path. Sorting by path alone is a
    # tie: D-numbering is per-tenant, so T-local's root and T-acme's root are
    # BOTH "D0" and the winner was whichever the underlying dict happened to
    # yield first. The panel consumes this order directly -- the org chart's
    # root row, the Tenants table, and (worse) the create-department parent
    # default, which took `the first row with no parent_ref`. That could be
    # ANOTHER tenant's root, so the dropdown read "D0 Sutra Labs" while the
    # copyable CLI string carried --parent <T-acme root>. Ordering here makes
    # the tenant of record deterministically lead; ts_minted_ms + ref break
    # any remaining tie so the order is stable across reads.
    rows.sort(key=lambda r: (0 if r.get("tenant_id") == tenant_of_record else 1,
                             r["path"], r.get("ts_minted_ms") or 0, r["ref"]))
    return rows


# ---------------------------------------------------------- GET /charters --

def _merged_charter_view(cid):
    """body + sidecar merge for render, via the engine's own charter_view()
    (handles the case where no sidecar exists on disk too)."""
    return E.charter_view(cid)


@router.get("/org/charters")
def org_charters(all_tenants: bool = False, tenant: Optional[str] = None):
    """EVERY charter in the tenant scope -- including ones homed to a RETIRED
    domain, merged body+sidecar per §2.2 (charter_view() does the merge).

    Scoping this to live_refs() was a real bug: a charter stranded on a
    tombstone is exactly the ORG-002 defect the Charters screen exists to
    surface (it renders the owner cell with a "tombstone" pill). Filtering it
    out made the one row the operator most needs to see the one row they
    could not see -- and silently dropped the rail count from 12 to 11 with
    no indication anything was hidden. Scope by TENANT (the §6 boundary),
    never by liveness.
    """
    tenant_id = _scope(tenant, all_tenants)
    domains = E.load_domains()
    scope = E.tenant_refs(domains, tenant_id)

    seen_ids = set()
    out = []
    for ref in scope:  # every domain in tenant scope, live AND retired
        for body in E.charters_for(ref, tenant_id=tenant_id):
            cid = body.get("id")
            if not cid or cid in seen_ids:
                continue
            seen_ids.add(cid)
            view = _merged_charter_view(cid)
            if view:
                out.append(view)
    out.sort(key=lambda c: c.get("id") or "")
    return out


# -------------------------------------------------------- GET /placements --

@router.get("/org/placements")
def org_placements(all_tenants: bool = False, tenant: Optional[str] = None):
    """write_placement()'s body dict (placement_engine.py, untouched by this
    tier) never stores a 'mode' key -- the panel's classifyTask()/runTurn()
    write mode:'match'|'floor' onto every placement row client-side and read
    p.mode==='floor' pervasively (Placements screen's 'held at ancestor'
    banner + per-row held pill, session rail 'held' badges in both Recent and
    By-department groupings, By-department held counts, SCREENS.health's
    held-count summary). Derive it here instead of editing the core engine:
    mode is a pure function of the placement's own stored confidence vs the
    engine's own CONFIDENCE_FLOOR (the same comparison classify() itself
    makes at write time -- see placement_engine.py's classify()), so this is
    not a guess, it's the identical predicate re-applied read-side."""
    tenant_id = _scope(tenant, all_tenants)
    rows = E.all_placements(tenant_id=tenant_id)
    for row in rows:
        conf = row.get("confidence")
        row["mode"] = "floor" if (conf is not None and conf < E.CONFIDENCE_FLOOR) else "match"
    rows.sort(key=lambda p: p.get("ts_ms", 0), reverse=True)
    return rows


# -------------------------------------------------------------- GET /stats -

@router.get("/org/stats")
def org_stats(tenant: Optional[str] = None):
    """Port of the CLI's `stats` (placement_engine.py ~:2757).

    Registry-wide by default (unchanged). With ?tenant=<id> every count is
    narrowed to that tenant -- domains via tenant_refs, charters via
    charters_for over that scope, placements/current rows via their own
    tenant_id field. `scope` names which of the two you are looking at, so a
    smaller number can never be mistaken for the registry shrinking.
    """
    _all = E.load_domains()
    if not tenant:
        return {
            "scope": "all-tenants",
            "tenant_id": None,
            "domains": len(_all),
            "domains_live": len(E.live_refs(_all)),
            "domains_retired": len(_all) - len(E.live_refs(_all)),
            "charters": len(E.charter_body_files()),
            "placements": len(E.all_placements()),
            "current_rows": len(E._read_jsonl(E.CURRENT)),
            "confidence_floor": E.CONFIDENCE_FLOOR,
        }

    tenant_id = _tenant_of_record(tenant)
    scope = E.tenant_refs(_all, tenant_id)
    live = E.live_refs(scope)
    return {
        "scope": "tenant",
        "tenant_id": tenant_id,
        "domains": len(scope),
        "domains_live": len(live),
        "domains_retired": len(scope) - len(live),
        "charters": len(_charter_ids_in_scope(scope, tenant_id)),
        "placements": len(E.all_placements(tenant_id=tenant_id)),
        "current_rows": sum(1 for r in E._read_jsonl(E.CURRENT)
                            if r.get("tenant_id") == tenant_id),
        "confidence_floor": E.CONFIDENCE_FLOOR,
    }


# ------------------------------------------------------------- GET /health -

@router.get("/org/health")
def org_health(tenant: Optional[str] = None):
    """MECE is per-tenant (mece_report takes a tenant_id and defaults to
    "T-local"); lint_full() walks the whole log and has no tenant argument, so
    it is reported as registry-wide rather than mislabelled as scoped."""
    tenant_id = _tenant_of_record(tenant)
    return {"mece": E.mece_report(tenant_id), "lint": E.lint_full(),
            "tenant_id": tenant_id, "lint_scope": "all-tenants"}


# ------------------------------------------------------------ GET /history -

@router.get("/org/history")
def org_history(tenant: Optional[str] = None):
    """domains/INDEX.jsonl, newest first, plus DERIVED completeness metadata.

    `history_complete_from_ms` does NOT exist as a stored field anywhere in
    placement_engine.py -- it is a design-doc concept (§5.4 / the as-of view),
    and inventing it as though the engine wrote it would be a fabrication.
    It IS honestly derivable: the Phase 0 work enriched `domain_restructured`
    events with before/after snapshots, so the boundary between "we know what
    changed" and "we only know THAT something changed" is the earliest event
    carrying a `before` key. Rows older than that are legacy: replaying them
    cannot reconstruct prior state, which is exactly why an as-of view before
    that timestamp must refuse rather than guess.

    Returned as `derived: true` so no caller mistakes it for stored state.
    """
    rows = E._read_jsonl(E.DOMAIN_INDEX)
    # ?tenant= drops rows belonging to a DIFFERENT tenant. Rows carrying no
    # tenant_id at all are kept: their tenant is unknown, and silently hiding
    # an event because a field is absent would put a hole in the timeline that
    # nothing on screen accounts for.
    if tenant:
        want = _tenant_of_record(tenant)
        rows = [r for r in rows
                if r.get("tenant_id") is None or r.get("tenant_id") == want]
    enriched = [r for r in rows if r.get("before") is not None]
    complete_from = min((r.get("ts_ms") or 0) for r in enriched) if enriched else None
    # `legacy_events` is printed under the timeline as "N legacy row(s) below
    # the line", so it has to count the rows the timeline actually greys out.
    # The timeline's predicate is `event === "domain_restructured" && !before`
    # -- a MINT carries no `before` because there is no prior state to
    # snapshot, not because anything was lost. Counting every row without a
    # `before` key reported 5 legacy rows under a timeline showing 1.
    legacy = [r for r in rows
              if r.get("event") == "domain_restructured" and r.get("before") is None]
    return {
        "events": sorted(rows, key=lambda r: r.get("ts_ms") or 0, reverse=True),
        "meta": {
            "tenant_id": _tenant_of_record(tenant) if tenant else None,
            "domain_index_lines": len(rows),
            "history_complete_from_ms": complete_from,
            "derived": True,
            "enriched_events": len(enriched),
            # restructure rows that cannot be replayed -- the greyed rows
            "legacy_events": len(legacy),
            # the broader "no before key at all" count, kept so the narrower
            # number above is never mistaken for it (mints live in the gap)
            "events_without_before": len(rows) - len(enriched),
        },
    }


# ------------------------------------------------------------- GET /skills -

@router.get("/skills")
def api_skills():
    """Every slash command `claude` can actually resolve here.

    The Skills screen previously rendered TEN HARDCODED STRINGS and claimed
    "31 total". Both numbers were invented -- nothing was read from disk. This
    reads the same directories the CLI resolves (see skills_catalog.py), so
    what the screen lists is what will actually run, and the count is real.

    Deliberately generic: it walks plugins/cache/<marketplace>/<plugin>/<ver>/
    with no hardcoded plugin name, so a newly installed plugin appears with
    zero code change. That is what "AI agnostic" means here -- read what the
    assistant exposes, do not invent a parallel format for it.

    MULTI-PROVIDER: the ~/.claude scan is unchanged. Every OTHER configured
    provider is read the same way -- whatever capability files actually exist
    in its config dir -- and providers with no config dir contribute nothing.
    `providers` carries the per-provider entry count next to installed/
    configured/reason, so "0 entries" is always accompanied by WHY.

    Entries are not normalised into a fake common shape. Codex's AGENTS.md is
    a persona/prompt file, so it arrives as kind="instructions" with
    slash=null; it is not dressed up as a slash command that codex would not
    resolve. Anything whose provider binary is missing carries runnable=false
    plus the reason, so the palette can refuse to offer it.
    """
    import skills_catalog
    bundle = skills_catalog.discover_all(
        project_dir=os.environ.get("CLAUDE_PROJECT_DIR"))
    items = bundle["items"]
    by_kind, by_source, by_provider = {}, {}, {}
    for e in items:
        by_kind[e["kind"]] = by_kind.get(e["kind"], 0) + 1
        by_source[e["source"]] = by_source.get(e["source"], 0) + 1
        by_provider[e["provider"]] = by_provider.get(e["provider"], 0) + 1
    return {"items": items, "total": len(items),
            "by_kind": by_kind, "by_source": by_source,
            "by_provider": by_provider,
            "runnable": sum(1 for e in items if e.get("runnable")),
            "providers": bundle["providers"]}


# ------------------------------------------------------------- GET /search -

def _hay(*parts):
    out = []
    for p in parts:
        if p is None:
            continue
        if isinstance(p, (list, tuple)):
            out.extend(str(x) for x in p)
        else:
            out.append(str(p))
    return " ".join(out).lower()


@router.get("/org/search")
def org_search(q: str = "", limit: int = 40, tenant: Optional[str] = None):
    """Real retrieval across the registry. The rail item was DISABLED with the
    label "no index" -- true only if the answer had to be an FTS5 index. At
    this registry's actual scale (tens of domains, tens of charters, low
    thousands of placements at worst) a linear scan over already-loaded dicts
    answers in microseconds. Shipping a greyed-out item was the wrong call:
    it advertised a capability and delivered nothing.

    Matches on the fields a person would actually search by, and says WHICH
    field hit so the result is explainable rather than an opaque ranking.
    """
    term = (q or "").strip().lower()
    if not term:
        return {"query": "", "results": [], "counts": {}}

    # Registry-wide unless ?tenant= is given. (This local was previously
    # computed and never used -- search silently spanned every tenant while
    # reading as though it were scoped. Now the scoping is real and opt-in.)
    tenant_id = _tenant_of_record(tenant) if tenant else None
    domains = E.load_domains()
    results = []

    for ref, d in domains.items():
        fields = []
        if term in _hay(d.get("name")):
            fields.append("name")
        if term in _hay(d.get("description")):
            fields.append("description")
        if term in _hay(d.get("mint_evidence")):
            fields.append("mint_evidence")
        if fields:
            results.append({
                "kind": "domain", "ref": ref,
                "title": d.get("name"), "path": E.domain_path(ref, domains),
                "subtitle": d.get("description") or "",
                "matched_on": fields, "status": d.get("status", "active"),
                "tenant_id": d.get("tenant_id"),
                # name hits rank above evidence hits -- a department called
                # "Platform" should beat one that merely lists it as a term
                "score": 3 if "name" in fields else (2 if "description" in fields else 1),
            })

    # charter_body_files() returns FILENAMES ("C-<sha>.json"), not ids and not
    # dicts -- charter_view() wants the bare id, so passing the filename
    # straight through returned None for every charter and made charters
    # silently unsearchable. Strip the extension.
    for fname in E.charter_body_files():
        cid = fname[:-5] if fname.endswith(".json") else fname
        c = E.charter_view(cid)
        if not isinstance(c, dict):
            continue
        fields = []
        if term in _hay(c.get("title")):
            fields.append("title")
        if term in _hay(c.get("purpose")):
            fields.append("purpose")
        if term in _hay(c.get("scope_in")):
            fields.append("scope_in")
        if fields:
            owner = domains.get(c.get("domain_ref")) or {}
            results.append({
                "kind": "charter", "ref": c.get("id"),
                "title": c.get("title"), "path": c.get("id"),
                "subtitle": c.get("purpose") or "",
                "matched_on": fields, "status": c.get("status", "active"),
                "tenant_id": c.get("tenant_id"),
                "owner": owner.get("name"),
                "owner_retired": owner.get("status") == "retired",
                "score": 3 if "title" in fields else 2,
            })

    for p in E.all_placements():
        wid = (p.get("work_ref") or {}).get("id") or ""
        if term in wid.lower():
            d = domains.get(p.get("domain_ref")) or {}
            results.append({
                "kind": "placement", "ref": p.get("id"),
                "title": wid, "path": p.get("id"),
                "subtitle": "filed to %s" % (d.get("name") or p.get("domain_ref") or "?"),
                "matched_on": ["work_ref"], "status": p.get("phase"),
                "tenant_id": p.get("tenant_id"),
                "confidence": p.get("confidence"),
                "score": 2,
            })

    if tenant_id is not None:
        results = [r for r in results if r.get("tenant_id") == tenant_id]

    results.sort(key=lambda r: (-r["score"], r["kind"], str(r["title"] or "")))
    counts = {}
    for r in results:
        counts[r["kind"]] = counts.get(r["kind"], 0) + 1
    return {"query": q, "results": results[:limit], "counts": counts,
            "tenant_id": tenant_id,
            "truncated": len(results) > limit}


# ---------------------------------------------------------- POST /classify -

class ClassifyRequest(BaseModel):
    text: str
    session_id: Optional[str] = None


def _pick_charter_for_write(domains, ref):
    """Domain's own active charter, else nearest ancestor's -- the simple
    walk classify_task/pickCharter uses, so the charter picked for the
    WRITE matches the charter surfaced in the RESPONSE."""
    return R.pick_charter(domains, _charters_view_for_pick(domains), ref)


def _charters_view_for_pick(domains):
    """Merged charter views (status lives in the sidecar in this codebase;
    R.pick_charter reads c.get('status') so it needs the merged view, not
    the raw hashed body)."""
    out = []
    seen = set()
    for ref in domains:
        for body in E.charters_for(ref):
            cid = body.get("id")
            if not cid or cid in seen:
                continue
            seen.add(cid)
            v = E.charter_view(cid)
            if v:
                out.append(v)
    return out


@router.post("/classify")
def api_classify(req: ClassifyRequest, tenant: Optional[str] = None):
    """gather_evidence + classify() [NEVER resolve()], resolve a charter via
    a simple pick_charter walk, write_placement() exactly once with
    work_ref={"kind":"task","id":text}. Never 500s on an unreachable charter
    -- write_placement's I-P2 ValueError is caught and surfaced as
    {"blocked": str(e)}."""
    text = (req.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")

    # ?tenant= scopes the WRITE as well as the read: the placement is filed
    # under the tenant the UI is currently showing, not under whatever the
    # server was started with. Same resolution order as the read endpoints.
    tenant_id = _tenant_of_record(tenant)
    domains = E.load_domains()

    evidence = E.gather_evidence(utterance=text)
    domain_ref, confidence, mode = E.classify(evidence, tenant_id, domains)

    if domain_ref is None:
        return {
            "blocked": "classify() returned no candidate domain (mode=none) "
                       "-- nothing to route work_ref against",
            "mode": "none",
            "confidence": confidence,
        }

    charter = _pick_charter_for_write(domains, domain_ref)
    charter_id = charter.get("id") if charter else None

    result = {
        "mode": mode,
        "domain_ref": domain_ref,
        "domain_path": E.domain_path(domain_ref, domains),
        "domain_name": (domains.get(domain_ref) or {}).get("name"),
        "confidence": confidence,
        # WHY this domain won, not what was typed: intersect the utterance's
        # terms with the winner's own name tokens + mint_evidence -- the exact
        # set score_domains() overlaps against. Returning the raw tokens made
        # the panel render "Filed to Sutra Labs on `quarterly` `report`" for a
        # floor hold whose mint_evidence contains neither word. [] is honest
        # and the panel already renders that case without the "on ..." clause.
        "matched_terms": R.matched_terms_for(domains.get(domain_ref),
                                             evidence.get("terms"),
                                             tokenizer=E.tokenize),
        "charter": charter,
    }
    if mode == "floor":
        result["held_at_ref"] = domain_ref
        result["held_at_path"] = E.domain_path(domain_ref, domains)

    if charter_id is None:
        return {
            "blocked": "no charter is reachable from this department -- "
                       "write_placement rejects a null charter_id",
            **result,
        }

    try:
        placement = E.write_placement(
            work_ref={"kind": "task", "id": text},
            domain_ref=domain_ref,
            charter_id=charter_id,
            origin="hook",
            confidence=confidence,
            created={"domains": [], "charters": []},
            tenant_id=tenant_id,
            phase="open",
        )
    except ValueError as exc:
        return {"blocked": str(exc), **result}

    result["placement"] = placement
    return result


# ----------------------------------------------------------- POST /simulate -

class SimulateOp(BaseModel):
    op: str
    ref: str
    target: Optional[str] = None


class SimulateRequest(BaseModel):
    ops: List[Dict[str, Any]] = []
    base: Optional[Dict[str, Any]] = None
    # the CLIENT's Date.now(). reorg_sim used to derive "now" as
    # max(placement.ts_ms) to stay pure while the panel banded charter
    # freshness against the browser clock -- so ORG-009/ORG-020 and the
    # freshness pills measured from different instants and could contradict
    # each other on the same screen. One clock, sent by the side that also
    # renders the pills. Absent (a curl, a test) -> server wall clock.
    now_ms: Optional[int] = None


@router.post("/org/simulate")
def org_simulate(req: SimulateRequest):
    """Calls reorg_sim.simulate() against a FRESH load_domains() read --
    never a cached copy."""
    domains = E.load_domains()  # fresh read, every call
    charters = _charters_view_for_pick(domains)
    placements = E.all_placements()

    base = req.base
    base_mismatch = None
    if base:
        current_rows = len(E._read_jsonl(E.CURRENT))
        captured_lines = base.get("domain_index_lines")
        if captured_lines is not None and captured_lines != len(E._read_jsonl(E.DOMAIN_INDEX)):
            base_mismatch = {"_mismatch": True}

    now_ms = req.now_ms if req.now_ms is not None else int(time.time() * 1000)
    sim = R.simulate(req.ops or [], domains, charters, placements,
                     base=base_mismatch, now_ms=now_ms)
    return {
        "domains2": sim["domains2"],
        "findings": sim["findings"],
        "max_depth": sim["max_depth"],
        "not_checked": [{"code": c, "reason": r} for c, r in R.NOT_CHECKED],
    }


# ------------------------------------------------------------- GET/POST draft

@router.get("/org/draft")
def org_draft_get():
    if not DRAFT_PATH.exists():
        return {"ops": [], "rationale": "", "plan_origin": "studio-drag",
                "base": None, "validated_at_ms": None}
    try:
        return json.loads(DRAFT_PATH.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        raise HTTPException(status_code=500, detail="draft file unreadable")


class DraftRequest(BaseModel):
    ops: List[Dict[str, Any]] = []
    rationale: str = ""
    base: Optional[Dict[str, Any]] = None
    validated_at_ms: Optional[int] = None  # ignored -- server always forces null


@router.post("/org/draft")
def org_draft_post(req: DraftRequest):
    """Writes ONE draft file under DRAFTS_DIR (outside SUTRA_NATIVE_HOME).
    plan_origin is fixed to "studio-drag"; validated_at_ms is ALWAYS forced
    to null server-side regardless of what the client sends -- only
    `org validate` (CLI, out of scope for this tier) may set it."""
    DRAFTS_DIR.mkdir(parents=True, exist_ok=True)
    draft = {
        "ops": req.ops or [],
        "rationale": req.rationale or "",
        "plan_origin": "studio-drag",
        "base": req.base,
        "validated_at_ms": None,  # forced, never trusts the client
        "saved_at_ms": int(time.time() * 1000),
    }
    tmp = DRAFT_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(draft, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(DRAFT_PATH)
    return draft


# =========================================================== providers ======
# providers.py owns the availability logic and the settings file; these
# endpoints are transport only. Nothing here touches placement_engine.


@router.get("/providers")
def api_providers():
    """Every catalogued AI provider with its LIVE state, plus which one is
    active.

    `installed` is shutil.which() and nothing else. A provider is offered to
    the UI as selectable only when installed AND configured AND this build has
    an adapter for it -- see POST /providers/active, which refuses the rest.
    The adapter signal is a property of THIS CODEBASE, not the machine: an
    installed CLI we cannot drive is not selectable. When a provider is not
    usable, `reason` names the missing half and the path involved, so the UI
    never has to render a bare "unavailable".
    """
    detail = providers.active_provider_detail()
    return {
        "providers": providers.discover_providers(),
        "active": detail["id"],
        "active_source": detail["source"],
        # overrides (SUTRA_UI_PROVIDER / settings.json) that named something
        # unrunnable and were therefore NOT honoured -- surfaced rather than
        # silently substituted
        "ignored": detail["ignored"],
    }


class ActiveProviderRequest(BaseModel):
    id: str


@router.post("/providers/active")
def api_providers_set_active(req: ActiveProviderRequest):
    """Persist the active provider to ~/.sutra-ui/settings.json.

    REFUSES with 400 + the specific reason when the id is unknown or when the
    provider is not runnable (binary missing, never configured, or no adapter
    in this build). Letting the
    UI select a provider that cannot start would move the failure to the first
    chat message, where it surfaces as a dead websocket rather than an answer.
    """
    try:
        settings = providers.save_settings(provider=req.id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except OSError as exc:
        raise HTTPException(status_code=500,
                            detail="could not write settings: %s" % exc)
    return {
        "active": settings["provider"],
        "provider": providers.provider_by_id(settings["provider"]),
        "settings": settings,
        "providers": providers.discover_providers(),
    }


# ============================================================ settings ======

@router.get("/settings")
def api_settings_get():
    """Panel settings: {provider, permission_mode, workdir}.

    Stored in ~/.sutra-ui/settings.json (NOT under SUTRA_NATIVE_HOME -- these
    are preferences, not governance state). Values absent or invalid in the
    file fall back to documented defaults, and `invalid_stored_values` reports
    anything that was ignored instead of quietly correcting it.

    permission_mode is one of plan|acceptEdits|bypassPermissions, default
    "plan":
      plan               the agent plans and proposes; every edit needs an
                         explicit approval. This is the default (SAFETY rule 4).
      acceptEdits        THE AGENT WRITES FILES WITHOUT ASKING. It can create,
                         modify and delete files under `workdir` with no
                         per-edit prompt. Choose it deliberately.
      bypassPermissions  everything auto-approved, including shell commands.
    """
    unlocked = providers.unsafe_modes_allowed()
    return {
        "settings": providers.load_settings(),
        "permission_modes": [
            {"id": m, "note": providers.PERMISSION_MODE_NOTES.get(m),
             "default": m == providers.DEFAULT_PERMISSION_MODE,
             "writes_files": m in providers.UNSAFE_PERMISSION_MODES,
             # A control the server will refuse must say so BEFORE it is
             # clicked. Without this the panel offered three modes, accepted
             # clicks on all three, and answered two of them with a 400 -- which
             # reads as "settings are broken" rather than "this is gated".
             "settable": m not in providers.UNSAFE_PERMISSION_MODES or unlocked,
             "requires_unlock": m in providers.UNSAFE_PERMISSION_MODES}
            for m in providers.PERMISSION_MODES
        ],
        "unsafe_modes_allowed": unlocked,
        "unsafe_modes_env": providers.UNSAFE_MODES_ENV,
        "providers": providers.discover_providers(),
    }


class SettingsRequest(BaseModel):
    provider: Optional[str] = None
    permission_mode: Optional[str] = None
    workdir: Optional[str] = None
    onboarded: Optional[bool] = None


@router.post("/settings")
def api_settings_post(req: SettingsRequest):
    """Partial update -- only the keys present in the body are written.

    Same refusal as POST /providers/active: a provider that is not runnable is
    rejected with 400 and the reason, never stored. An unknown permission_mode
    is rejected rather than silently downgraded, so the operator is never told
    a mode was applied when it was not.
    """
    if (req.provider is None and req.permission_mode is None
            and req.workdir is None and req.onboarded is None):
        raise HTTPException(
            status_code=400,
            detail="nothing to update -- send at least one of: provider, "
                   "permission_mode, workdir, onboarded")
    try:
        settings = providers.save_settings(
            provider=req.provider,
            permission_mode=req.permission_mode,
            workdir=req.workdir,
            onboarded=req.onboarded,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except OSError as exc:
        raise HTTPException(status_code=500,
                            detail="could not write settings: %s" % exc)
    return {"settings": settings}


# ============================================================= tenants ======

@router.get("/tenants")
def api_tenants():
    """Every tenant that actually exists in the registry, with real counts.

    Derived, not stored: placement_engine has no tenant table. A tenant is a
    `tenant_id` observed on a domain or a placement, so this unions those two
    sources and counts each tenant's scope with the engine's own functions
    (tenant_refs / charters_for / all_placements). If the registry holds one
    tenant, this returns one row -- it does not pad the list to make a
    tenant-picker look populated.

    The tenant of record (PLACEMENT_TENANT, default "T-local") is always
    present and flagged is_default, even at zero counts: the UI has to be able
    to show the tenant every unscoped write would land in, including the case
    where that tenant is empty.
    """
    domains = E.load_domains()
    placements = E.all_placements()
    default_id = _tenant_of_record()

    ids = {default_id}
    for d in domains.values():
        if d.get("tenant_id"):
            ids.add(d["tenant_id"])
    for p in placements:
        if p.get("tenant_id"):
            ids.add(p["tenant_id"])

    out = []
    for tid in ids:
        scope = E.tenant_refs(domains, tid)
        live = E.live_refs(scope)
        roots = [(ref, d) for ref, d in scope.items() if not d.get("parent_ref")]
        roots.sort(key=lambda rd: (rd[1].get("ts_minted_ms") or 0, rd[0]))
        out.append({
            "tenant_id": tid,
            "domains": len(scope),
            "charters": len(_charter_ids_in_scope(scope, tid)),
            "placements": len(E.all_placements(tenant_id=tid)),
            "is_default": tid == default_id,
            # extras the landing screen would otherwise re-derive client-side
            "domains_live": len(live),
            "domains_retired": len(scope) - len(live),
            "root_ref": roots[0][0] if roots else None,
            "root_name": roots[0][1].get("name") if roots else None,
        })

    out.sort(key=lambda r: (0 if r["is_default"] else 1, r["tenant_id"]))
    return out
