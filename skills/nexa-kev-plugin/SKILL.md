---
name: nexa-kev-plugin
version: 1.0.0
author: Mike (NexaSkills)
description: Use when operating the nexa-kev kev_decide plugin.
---

## When to Use
Building, enabling, verifying, or debugging the nexa-kev plugin
(kev_decide tool) on beez or fleet Hermes hosts.

# nexa-kev plugin ops

Plugin: `~/.hermes/plugins/nexa-kev/` (standalone; never edit hermes-kev-router).
Tool `kev_decide` -> gateway `POST <base>/kev/systemone` (NOT kev-router's
/v1/systemone). Defaults live in config.py; all overridable via
`NEXA_KEV_*` env or `plugins.entries.nexa-kev`.

## Endpoints (verified 2026-11-06)
1. `http://192.168.1.10:4000/kev/systemone` (LAN, primary)
2. `http://100.89.135.14:4000/kev/systemone` (tailnet)
3. `https://ai.nexaskills.co.za/kev/systemone` (public 443)
HTTPS on :4000 does NOT exist (SSL WRONG_VERSION_NUMBER). Auth: Bearer key
from `HERMES_CUSTOM_AI_NEXASKILLS_CO_ZA_API_KEY` (root .env line ~77 +
profile .env); 401/403 => immediate `NO DECISION (auth)` (no fallthrough).

## Gotchas
- Profile sessions scan `<profile>/plugins/`, NOT root `~/.hermes/plugins/`
  — new user plugins must be symlinked into each profile that should load
  them (pattern: mnemo-pg2/lcm2-pg symlinks). Root scan serves the
  default/multiplex gateway boot.
- Manifest name == `plugins.enabled`/`plugins.entries` key (`nexa-kev`).
- `plugins.enabled` is a YAML LIST: never `hermes config set` it; use the
  scripted-splice + backup recipe (hermes-plugin-authoring skill).
- Gateway-served chat sessions take plugin tools at gateway BOOT
  (plugins_activation.py: Python tools deferred to next session). After
  enabling, prove via `hermes -p <p> chat -q` only AFTER a gateway bounce;
  meanwhile in-process + registry-dispatch proofs are valid.
## Memory-bank routing (v0.3.0)
- pre_tool_call hook memroute.py blocks shared-bank saves that are
  agent-scoped (kev ensemble private-question, spread<=0.2 agreement) or
  contain secret patterns (hard regex). Private saves never blocked;
  everything fail-open + logged kind=memroute. Kill: NEXA_KEV_MEMROUTE=0.
- Harness pitfall (verified): offline plugin tests MUST use the agent venv
  python (installs/c3ba7568*/environments/e4804904*/venv — hermes_cli
  importable) + HERMES_HOME=<profile>; tools-python or system python
  silently degrade dotenv resolution to missing_key (fail-open -> looks
  like a no-op, ~1-2ms tells you kev never ran).
- Recall side v1 = convention: private bank for own history/prefs, shared
  for fleet facts; unsure -> kev_decide choice question on query type.

## Confidence model (v0.2.2)
- Engine noul is phrasing-sensitive (measured spread up to 0.31 on one
  scenario). kev_decide runs 3 framings per noul question IN ONE call
  (base/strict-facts/worst-case-recovery); verdict = median, and spread
  >0.2 demotes to UNCERTAIN. Output fields: spread, variants.base.
  Kill-switch NEXA_KEV_ENSEMBLE=0. Advisory lane (choice picks) unchanged.

## New host/team onboarding
FULL 7-step checklist in the plugin README ('New-host rollout checklist')
— copy+symlinks+splice-enable, .env key pair (NEXA_KEV_KEY_ENV +
NEXA_KEV_API_KEY; v0.2.1 resolves dotenv-aware, earlier builds returned
NO DECISION (missing_key) on disk-lane sessions — fixed), team gateway
policy, boot activation, prove via tool + silent advisory.
- Gateway access gate = the TEAM's LiteLLM metadata
  `allowed_passthrough_routes: ["/kev"]` (team-level; key-level control is
  Enterprise-only, don't chase it). Without it: 403 -> plugin renders
  NO DECISION (auth). Full check + /team/update commands (variable-based,
  no secrets) live in the plugin README section 'Required one-time gateway
  setup'. Teams granted: mybuddy-beez + mybuddy-trade 2026-11-06.
- Trade agents' own skill nexa-kev-usage (profiles/pip) claims "both keys
  blocked, team not granted" — OBSOLETE (written pre-v0.2.1-fix); team IS
  granted and plugin works there. Correct that note if you load it.

- Activation: plugin tools + advisory hook register at BOOT only. After
  install/upgrade, restart gateway from a shell OUTSIDE any agent session:
  `systemctl --user restart hermes-gateway.service` (single-profile host) /
  `systemctl --user restart hermes-gateway-default` (fleet multiplex; trade
  = hermes-gateway-default) / `hermes gateway restart` from external terminal.
  ALSO restart the other session surfaces once: `systemctl --user restart
  hermes-webui hermes-dashboard` (verified trade unit names 2026-11-06;
  confirm per host via list-units grep gateway|webui|dashboard).
  Verify fresh log line 'nexa-kev: registered tool kev_decide'.
- End-to-end verification = P0 (host-terminal direct decide probe), P1
  (chat: native kev_decide call, ledger scenario => noul 0.3-0.7 UNCERTAIN),
  P2 (chat: quote ⚡ line nobody asked for). Exact prompts in README section
  'End-to-end verification prompts'.
- Gateway bounce cannot be issued from inside the gateway (guard scans
  referenced script contents recursively, even two-level wrappers and
  systemd-run timers). Give Jan the external one-liner
  `systemctl --user restart hermes-gateway`. persist_on_release poller
  jobs do NOT survive the bounce (killed with the old gateway) — run the
  post-bounce proofs yourself in the next turn.
- Failure-proof in gateway-served chat sessions: the gateway never sees
  CLI-shell env vars; temporarily point the plugin at the dead port via
  `hermes -p <p> config set plugins.entries.nexa-kev.base_urls
  http://192.168.1.10:4999/kev/systemone` (scalar set/unset is the safe
  sanctioned path; config is re-read per call, no bounce needed), then
  `config unset` immediately after.
- Registered plugin tools land in the tool_search DEFERRED catalog
  (visible defs stay ~19-20 core); model reaches them via tool_search ->
  tool_call by exact name `kev_decide`, or natively after bounce refresh.

## Advisory hook (v0.2.0)
- pre_llm_call: once per distinct user message/session (hash dedup), two
  `choice` questions (skill, tool) in ONE gateway call. KEV PER-QUESTION
  TOKEN CAP: 150 criteria x 60-char descs = HTTP 400 ("too long", 2921
  tok); 64 options/question ~ 1-2 s; 128 names-only ~ 2.7 s. Plugin caps
  to 64 via lexical-overlap prefilter (NEXA_KEV_ADVISORY_MAX_CRITERIA);
  40-char descs on 96 options already = 5 s — do not raise the cap.
- unsure (p < NEXA_KEV_ADVISORY_MIN 0.35 absolute OR top_prob x n < 4
  relative) => silent None. On 64-option catalogs absolute 0.35 rarely
  fires (softmax dilution) — the RELATIVE rule (NEXA_KEV_ADVISORY_REL_MIN,
  default 4x uniform) is the one that actually injects; log showed absa
  task 0.14x65=9 -> informative, chit-chat 0.037x65=2.4 -> correctly
  silent. Decision rows: skill_probs in decisions.jsonl is a 10-entry
  SLICE of the wire probs — recompute n from the slice, not the full set.
  Confident =>
  {'context': '⚡ [nexa-kev] ...'} + row in plugins-data/nexa-kev/
  decisions.jsonl (authoritative trail; echo line is model-compliance).
- Hook edits need ANOTHER gateway bounce (register() at boot).

## Rolling nexa-kev out to another fleet host
- Procedure (all via `ssh host 'bash -s' < script.sh` — ssh one-liners with
  nested quotes break eval): rsync the plugin dir to their
  `~/.hermes/plugins/nexa-kev`; symlink into EVERY profile's `plugins/`
  dir; scripted-splice `nexa-kev` into root + each profile
  `plugins.enabled` (list splice + .bak copy, same recipe as beez).
- Their API key often lives INLINE in their config.yaml
  (`model.api_key`/`providers.nexa-litellm.api_key`), never in .env.
  Preferred install: a DEDICATED kev key installed as the pair
  `NEXA_KEV_KEY_ENV=NEXA_KEV_API_KEY` + `NEXA_KEV_API_KEY=***` in root
  .env AND every profile .env (gateway reads root .env; disk sessions read
  profile .env) — keeps kev traffic attributed separately from their
  model-routing key. Reusing the inline key under the
  HERMES_CUSTOM_AI_NEXASKILLS_CO_ZA_API_KEY name works but mixes
  attribution; if used, extract it host-side (secret never leaves the host).
- PROBE EARLY with their key before declaring success: a direct curl to
  both endpoints printing status codes only. `403 ... allowed_passthrough
  routes` is a LiteLLM TEAM-metadata policy gap (grant via /team/update —
  see litellm-admin; key-level params are Enterprise-gated dead ends),
  NOT a plugin bug; the plugin correctly returns NO DECISION (auth) in
  that case.
- Cloned hosts inherit the SOURCE host's key/team (no per-host team
  exists) — match the key tail against the LiteLLM inventory file
  instead of assuming a team named after the host.

## Verification one-liners
    # direct call through plugin code (no agent loop): load via
    # spec_from_file_location(submodule_search_locations=[dir]) then
    # decide('Health probe. No task running.', ['Is it safe to reply?'])
    # forced failure: NEXA_KEV_BASE_URLS=http://192.168.1.10:4999/kev/systemone
    # auth failure: NEXA_KEV_KEY_ENV=nonexistent -> NO DECISION (auth) ~0.03s
