# nexa-kev

General-purpose decision helper for Hermes Agent. One tool, `kev_decide`,
that asks the kev decision model (via the LiteLLM gateway's
`/kev/systemone` passthrough) for a calibrated yes/no probability on any
free-text question — from an agent turn, a lifecycle hook, or a script.

## Tool

`kev_decide(state, questions, properties?)` -> JSON:

- `status: "ok"` — `decisions: {qid: {noul: p, verdict: ...}}` plus
  `endpoint`, `latency_ms`, `usage`, `model`.
- `status: "no_decision"` — clean structured failure; the agent sees
  `NO DECISION (<reason>)`. Never raises, never hangs (per-endpoint 10 s
  timeout + ~12 s overall deadline), never retry-storms (one attempt per
  endpoint; 401/403 stops immediately).

Verdict mapping (honest, never coerced): `p >= 0.7` -> `SAFE/YES`,
`p <= 0.3` -> `UNSAFE/NO`, in between -> `UNCERTAIN`.

## Use from a script/hook

    # load the plugin dir directly (no agent loop needed)
    result = decide("Proposed action: restart the gateway.",
                    ["Is this safe right now?"],
                    properties={"reversible": "yes"})

## Config (all optional — defaults work out of the box)

Resolution order: `NEXA_KEV_*` env > `plugins.entries.nexa-kev` in
config.yaml > defaults.

| Key           | Env                          | Default |
|---------------|------------------------------|---------|
| base_urls     | NEXA_KEV_BASE_URLS           | http://192.168.1.10:4000/kev/systemone, http://100.89.135.14:4000/kev/systemone, https://ai.nexaskills.co.za/kev/systemone |
| key_env       | NEXA_KEV_KEY_ENV             | HERMES_CUSTOM_AI_NEXASKILLS_CO_ZA_API_KEY (KEV_ROUTER_API_KEY honoured as legacy fallback) |
| model         | NEXA_KEV_MODEL               | kev |
| timeout       | NEXA_KEV_TIMEOUT             | 10 (s per endpoint) |
| deadline      | NEXA_KEV_DEADLINE            | max(12, timeout+2) (s overall) |
| yes_min       | NEXA_KEV_YES_MIN             | 0.7 |
| no_max        | NEXA_KEV_NO_MAX              | 0.3 |
| max_state_chars | NEXA_KEV_MAX_STATE_CHARS   | 1200 |
| hook_enabled  | NEXA_KEV_HOOK                | on (pre_llm_call advisory; `off` = kill-switch) |
| advisory_min  | NEXA_KEV_ADVISORY_MIN        | 0.35 (silence below this) |
| advisory_max_items | NEXA_KEV_ADVISORY_MAX_ITEMS | 5 per hint line |
| advisory_max_criteria | NEXA_KEV_ADVISORY_MAX_CRITERIA | 64 options per choice question (kev per-question token cap; a lexical prefilter picks the 64 most task-relevant skills/tools) |
| advisory_rel_min | NEXA_KEV_ADVISORY_REL_MIN   | 4.0 — relative floor: inject when top_prob x n_options >= 4 (beat uniform baseline 4x; fixes softmax dilution on big catalogs) |
| decision_log  | NEXA_KEV_DECISION_LOG        | <hermes_home>/plugins-data/nexa-kev/decisions.jsonl |

## Advisory hook (v0.2.0)

Once per user message, kev answers two `choice` questions (best skill, best
first tool) in ONE gateway call (~1-2 s). Confident answers inject
`⚡ [nexa-kev] skill: X (0.72) · tool: Y (0.55)` into the turn context and
are logged to `decision_log`; unsure answers stay silent. Any kev outage is
invisible (fail-open). Kill-switch: `NEXA_KEV_HOOK=off`.

No secrets in source. Auth failures (401/403) return
`NO DECISION (auth)` at once.

## End-to-end verification prompts (run in this order)

P0 — data layer (host terminal, no session needed):
    python3 -c "import importlib.util,sys,json;from pathlib import Path;h=Path.home()/'.hermes/plugins/nexa-kev';s=importlib.util.spec_from_file_location('nk',str(h/'__init__.py'),submodule_search_locations=[str(h)]);m=importlib.util.module_from_spec(s);sys.modules['nk']=m;s.loader.exec_module(m);print(json.dumps(m.decide('Health probe. No task running.',['Is it safe to reply?'])))"
PASS = "status":"ok" with a numeric noul >= ~0.6. missing_key/auth/
unreachable name the broken layer (config / gateway policy / network).

RESTART DIRECTIVE (before any session-level test): plugin tools and the
advisory hook register at BOOT — every long-lived surface that serves
agent sessions must be restarted ONCE after install/upgrade, FROM A SHELL
OUTSIDE any agent session (the in-session guard refuses otherwise):
    systemctl --user restart hermes-gateway.service        # single-profile host
    systemctl --user restart hermes-gateway-default        # fleet multiplex host (trade)
    # or: hermes gateway restart   (only from an external terminal)
    systemctl --user restart hermes-webui hermes-dashboard # webui/dashboard sessions (verified unit names on trade 2026-11-06)
Unit names vary per host — confirm with:
    systemctl --user list-units --type=service | grep -iE "gateway|webui|dashboard"
Confirm in the fresh gateway log: "nexa-kev: registered tool kev_decide".

P1 — native tool (gateway-served chat, e.g. Telegram):
    Call the native tool kev_decide exactly once with state "Proposed
    action: delete all rows from the ledger table to redo this month. The
    source import files are still on disk and the import has succeeded
    once before." and questions ["Is it safe to delete the ledger rows
    now?"]. Report the raw noul and verdict verbatim.
PASS = agent reports a REAL noul. Expected sense: destructive-but-
reversible => UNCERTAIN band (0.3-0.7), never a confident YES.

P2 — silent advisory (same chat, nobody mentions kev):
    Quote verbatim any line in your context that starts with a lightning
    bolt, or state there is none. Then answer normally: I want to check
    comments on our Facebook page using the desktop Chrome profile.
PASS = agent echoes a "⚡ [nexa-kev] skill: ..." line (or honestly none if
kev was silent-worthy — cross-check plugins-data/nexa-kev/decisions.jsonl,
which logs every advisory regardless of injection).

Vetting rules for whoever runs these: a verdict with no tool output
behind it = fabrication = FAIL; P0 ok but P1 "tool does not exist" =
gateway not restarted (do the restart directive, retest P1).

## New-host rollout checklist (follow in order; verify each step)

1. Copy plugin: `rsync -a --exclude __pycache__ ~/.hermes/plugins/nexa-kev/ <host>:~/.hermes/plugins/nexa-kev/`
2. Per profile: `ln -s ~/.hermes/plugins/nexa-kev ~/.hermes/profiles/<p>/plugins/nexa-kev`
   (profile sessions scan the PROFILE plugins dir; root dir serves the multiplex gateway boot).
3. Enable: add `nexa-kev` to `plugins.enabled` in root + each profile config.yaml —
   scripted splice + `.bak` backup (it is a YAML LIST: never `hermes config set`).
4. Key: put the team's kev-allowed key into `.env` (root + every profile .env):
   `NEXA_KEV_KEY_ENV=NEXA_KEV_API_KEY` + `NEXA_KEV_API_KEY=<key>` (chmod 600).
   No process-env export needed: from v0.2.1 the config resolves keys dotenv-aware
   (pre-0.2.1 had an env-only bug -> `NO DECISION (missing_key)` on disk-lane sessions).
5. Gateway policy: team `allowed_passthrough_routes` must include `/kev` —
   see the admin section above; verify with a direct probe:
   `python3 -c` load plugin `decide('Health probe.', ['Is it safe?'])` -> expect
   status ok + noul (NOT missing_key/auth/unreachable).
6. Activate sessions: restart EVERY serving surface — gateway
   (hermes-gateway.service / hermes-gateway-default), webui and dashboard
   (systemctl --user restart hermes-webui hermes-dashboard) — from an
   external shell; plugin tools register at BOOT. Disk-lane
   `hermes -p X chat` sessions may hide plugin tools from tool_search
   until that restart — gateway-served sessions get them natively.
   Exact commands + per-host unit discovery: RESTART DIRECTIVE above.
7. Prove: gateway chat call of kev_decide + one message where the ⚡ advisory
   fires with nobody mentioning kev; trail in `plugins-data/nexa-kev/decisions.jsonl`.

## Memory-bank routing (v0.3.0, shared vs private)

`pre_tool_call` hook (`memroute.py`) gates SAVES into shared banks
(`mnemosyne_shared_remember[_canonical]`, `mnemosyne_remember_canonical`):
1. hard secret-pattern gate (regex, no kev): credentials never hit the
   fleet-wide bank. 2. kev ensemble question "should this stay PRIVATE?"
   (3 framings via kev_decide); confident YES with agreement (spread<=0.2)
   -> BLOCK with an actionable re-save instruction. Private saves are
   never touched (safe default). Fail-open on any error; everything
   (block+allow) is logged to decisions.jsonl as kind=memroute.
Kill-switch: NEXA_KEV_MEMROUTE=0. Recall side (v1 = convention, hook
pending post_tool_call contract verification): an agent trusts its
PRIVATE bank for its own history/preferences and the SHARED bank for
fleet facts; on conflict, agent-specific wins from private. Quick kev
check when unsure: kev_decide(state=recall query, ["Is this question
about fleet-wide facts (shared) or this agent's own history (private)?"]).
Proven live 2026-11-06: agent-scoped shared-save BLOCKED on native lane,
fleet convention saved; offline battery 7/7.

## Verdict confidence (v0.2.2 paraphrase ensemble)

Measured: the engine's noul for the SAME scenario shifts by up to ~0.3
depending on phrasing — enough to flip verdicts at the 0.70 threshold.
kev_decide therefore asks each noul question in THREE framings inside the
same single call (base + strict-facts + worst-case-recovery): the verdict
uses the MEDIAN, and disagreement (spread > 0.2) is demoted to UNCERTAIN
— confidence must survive rephrasing to count. Output carries
`spread` + variant base value for transparency. Kill-switch:
`NEXA_KEV_ENSEMBLE=0` (falls back to single-ask). Advisory lane unchanged
(choice questions). Verified on ledger scenario: stable UNCERTAIN 0.682
(spread 0.015) vs the pre-fix 0.672/0.726 flip.

## Required one-time gateway setup (per team)

The LiteLLM gateway exposes kev via a core pass-through route (`/kev` ->
the kev model box). Access is gated by the **team's**
`allowed_passthrough_routes` metadata — NOT by anything in the plugin or
host config. A team that was never set up sees HTTP 403
`Key/team not allowed to access passthrough route /kev/systemone`, and the
plugin correctly renders `NO DECISION (auth)`. (Key-level control also
exists but is an Enterprise-only LiteLLM feature — do not pursue it.)
Done once for team mybuddy-beez on 2026-11-06.

Check current state (on the gateway host; DB container name may vary):

    docker exec -i <litellm-db-container> psql -U <dbuser> -d <dbname> -x -c \
      'select team_id, team_alias, metadata from "LiteLLM_TeamTable" where team_alias = '"'"'<team-alias>'"'"';'

Or just probe the route with a key from that team — 200 = allowed,
403 = needs the update below.

Grant the route to a team (run from a terminal; export the two variables
first — never hardcode the master key into files or history):

    export GATEWAY_URL="https://ai.nexaskills.co.za"
    export MASTER_KEY="********"   # gateway admin, interactive only
    export TEAM_ID="<uuid of the team>"          # from /v2/team/list
    export EXISTING_METADATA='<the team metadata JSON as returned>'
    curl -s -X POST "$GATEWAY_URL/team/update" \
      -H "Authorization: Bearer $MASTER_KEY" -H 'Content-Type: application/json' \
      -d "{\"team_id\":\"$TEAM_ID\",\"metadata\":{\"guardrails\":[],\"model_rpm_limit\":{},\"model_tpm_limit\":{},\"disable_global_guardrails\":false,\"allowed_passthrough_routes\":[\"/kev\"],\"opted_out_global_guardrails\":[],\"soft_budget_alerting_emails\":[]}}"

Notes: metadata updates REPLACE the whole dict, so preserve any other
team metadata fields in the body; the route value is the config path
prefix `"/kev"` (not the full `/kev/systemone` URL path). Verify with the
probe afterwards. Fleet teams already granted: mybuddy-beez (2026-11-06).

## Enable

    plugins.enabled:  add "nexa-kev"        (key = manifest name)
    plugins.entries.nexa-kev: {}            # optional overrides

Standalone: nothing is shared with or imported from hermes-kev-router.
