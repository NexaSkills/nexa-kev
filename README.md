# nexa-kev

kev-gated decision-making for [Hermes Agent](https://hermes-agent.nousresearch.com)
hosts on the NexaSkills fleet. One plugin, three lanes, zero LLM cost:

1. **`kev_decide` tool** — ask kev (a small SystemOne decision model behind
   the LiteLLM gateway) any yes/no judgment: "is this action safe to take?",
   "should I ask the human first?". Verdicts carry a confidence (`noul`), a
   paraphrase-ensemble `spread`, and honest failure (`NO DECISION (reason)`).
2. **Silent advisory** — every user message gets kev's ranked "best skill /
   best first tool" suggestions injected as one `⚡ [nexa-kev] ...` context
   line. Stays quiet when kev is unsure; fail-open on any kev outage.
3. **Memory-bank router** — shared-bank memory saves are gated by kev
   (agent-scoped facts get blocked and redirected to the agent's private
   bank) plus a hard secret-pattern gate. Recall rule: private = own
   history, shared = fleet facts.

Everything is logged to `plugins-data/nexa-kev/decisions.jsonl` for audit.
Kill-switches per host (profile `.env`): `NEXA_KEV_ENSEMBLE=0`,
`NEXA_KEV_MEMROUTE=0`, `NEXA_KEV_ENABLED=0`.

## Layout

    plugin/nexa-kev/     the Hermes plugin (drop-in, also its own README)
    skills/              agent skill with ops lessons (load via skills dirs)
    scripts/install_nexa_kev.sh   idempotent installer (this section)
    scripts/check_nexa_kev.sh     read-only end-to-end checker
    scripts/bounce.sh             bounce a gateway from OUTSIDE it (see -h)

## Install (any Hermes host that can reach the LiteLLM gateway)

    git clone https://github.com/NexaSkills/nexa-kev && cd nexa-kev
    bash scripts/install_nexa_kev.sh

Clone credential: the host needs a NexaSkills GitHub PAT in git's store
(`git config --global credential.helper store` + one entry for github.com
in `~/.git-credentials`, fleet-standard on beez/trade).

The installer copies the plugin into `~/.hermes/plugins/nexa-kev`, symlinks
it into every profile's plugins dir, enables it via `hermes config set`
(the sanctioned path — it never hand-edits YAML), checks the LLM key, and
runs a direct probe. Re-run any time after `git pull` to upgrade.
(Stale REAL directories found where a symlink belongs — a drift bug class
fixed 2026-11-10 — are moved aside as `*.stale-dir.<ts>` backups, never
deleted silently; `check_nexa_kev.sh` now FAILs on any version drift
between the root and a profile surface.)

### Fleet hosts = customer systems (rollout layout, hard rule 2026-11-10)

On every fleet host (anything that is not the source dev host), the repo
lives SELF-CONTAINED under the plugin tree — no `~/ops`, no projects dirs:

    ssh <fleet-host> 'git clone https://github.com/NexaSkills/nexa-kev ~/.hermes/plugins/nexa-kev-src 2>/dev/null; \
      bash ~/.hermes/plugins/nexa-kev-src/scripts/rollout_update.sh'

`rollout_update.sh` is idempotent and self-healing: it `git pull`s the
checkout (or clones it), points `~/.hermes/plugins/nexa-kev` (and every
profile symlink) INTO the checkout so future updates are literally just a
`git pull` + gateway bounce, and re-runs the installer legs (enable,
symlink verify; the copy step self-skips in this layout).
Dev flow per policy: develop + full-test on the source dev host FIRST,
then roll template, then (only when asked) other hosts.

Two steps the installer deliberately does NOT do for you (it prints them):

1. **Gateway pass-through grant (LiteLLM admin, once per team):** the
   gateway only forwards `/kev` for teams whose
   `metadata.allowed_passthrough_routes` includes it. Check:
   `curl -s -o /dev/null -w '%{http_code}' -X POST -H "Authorization: Bearer <key>" -H 'Content-Type: application/json' -d '{"model":"kev","state":"probe","questions":{"q1":{"type":"noul","instructions":"Is this reachable?"}}}' http://<gateway>:4000/kev/systemone`
   → 403 means grant needed (psql command in plugin/README.md).
2. **Service restarts** — plugin tools and hooks register at BOOT. From a
   shell outside any agent session:
   `systemctl --user restart hermes-gateway.service` (single-profile host)
   or `hermes-gateway-default` (fleet multiplex), plus
   `systemctl --user restart hermes-webui hermes-dashboard` where present.
   Confirm in the fresh gateway log:
   `registered tool kev_decide`, `memory-router hook`,
   `memlearn hook`, `memrecall hook`.

## Verify (do all three)

* **P0** `bash scripts/check_nexa_kev.sh` → `CHECK: PASS`.
* **P1** In any gateway-served chat, send:
  `Call the native tool kev_decide exactly once with state "Proposed action:
  delete all rows from the ledger table to redo this month. The source import
  files are still on disk and the import has succeeded once before." and
  questions ["Is it safe to delete the ledger rows now?"]. Report the raw
  noul, verdict, spread and endpoint verbatim.`
  Expect a REAL noul in the UNCERTAIN band (~0.3-0.7) — a confident YES on
  deleting money records would be a failure of the engine, not the plugin.
* **P2** Same chat, nobody mentions kev:
  `Quote verbatim any line in your context that starts with a lightning bolt
  (⚡), or state there is none. Then answer normally: I want to check
  comments on our Facebook page using the desktop Chrome profile.`
  Expect an echoed `⚡ [nexa-kev] ...` line (or honest silence; cross-check
  decisions.jsonl — the advisory is always logged even when too weak to
  inject).

## Configuration

All in the profile `~/.hermes/.env` (never in code):

| Var | Default | Meaning |
|---|---|---|
| `NEXA_KEV_API_KEY` | (falls back to `HERMES_CUSTOM_AI_NEXASKILLS_CO_ZA_API_KEY`) | LiteLLM virtual key of THIS host's team |
| `NEXA_KEV_BASE_URLS` | gateway `/kev/systemone` endpoints | comma-separated failover |
| `NEXA_KEV_MODEL` | `kev` | engine model name |
| `NEXA_KEV_ENSEMBLE` / `NEXA_KEV_MEMROUTE` / `NEXA_KEV_ENABLED` | `1` | kill-switches |
| `NEXA_KEV_MEMLEARN` | `1` | L1 learning-moment lane kill-switch |
| `NEXA_KEV_MEMLEARN_SHADOW` | `0` | `1` = decide+log but inject nothing (rollout soak mode) |
| `NEXA_KEV_MEMLEARN_MAX_SESSION` | `3` | max save-directives per session |
| `NEXA_KEV_MEMRECALL` | `1` | L2 mid-task error-recall lane kill-switch |
| `NEXA_KEV_MEMRECALL_MAX_SESSION` | `2` | max recall trailers per session |
| `NEXA_KEV_MEMRECALL_SHADOW` | `0` | `1` = probe+log recall candidates but append nothing (soak mode) |
| `NEXA_KEV_MEMRECALL_KEVGATE` | `0` | let kev gate pattern-only (non-error) recalls (experiment) |

## Self-learning memory lanes (v0.4.0)

Architecture: **public plugin hooks only** (`pre_llm_call`,
`pre_tool_call`, `transform_tool_result`) — no vendor edits, no
monkey-patching, defensive kwargs everywhere; every lane is fail-open,
kill-switchable and logs to `<profile>/plugins-data/nexa-kev/decisions.jsonl`.

* **L1 memlearn** (pre_llm_call): a local lexical prefilter flags
  durable-instruction messages ("from now on…", "that was good, do it like
  that", "never do…"); candidates get one kev ensemble call deciding
  *worth-remembering* AND *shared-vs-private bank* (criteria =
  `config.BANK_CRITERIA`, shared verbatim with the memroute save gate).
  Confident YES injects a save directive: recall-first (`mnemosyne_update`
  over duplicate), then `mnemosyne_remember` / `mnemosyne_shared_remember`
  with importance 0.7 + persistent scope. The AGENT performs the write
  (keeps `audit_log` + memroute gates intact). Uncertain → silent.
* **L2 memrecall** (transform_tool_result): on FAILED tool results (status
  or error-pattern match) it recalls the private bank first, the shared
  bank on a miss — through mnemo-pg2's own dispatch (capability probe;
  lane runs inert if the sibling plugin is absent) — and appends a
  "Related memory" trailer to the tool result mid-task. Budgeted,
  query-deduped, no kev needed for hard errors.
* **Passive recall** every turn (top-3 private + top-2 shared) remains
  mnemo-pg2's own `pre_llm_call` prefetch (`MNEMO2_PREFETCH=1`) — L2 is
  the deep layer on top, not a duplicate.

Save/recall policy: private = the agent's own habits/history/prefs/tasks;
shared = facts true for every agent (fleet conventions, shared infra,
cross-agent proofs); unsure → private.

Verify (after gateway bounce):

* P1 learning: chat `yes, that was good, do it like that from now on —
  always run the checker first` → the reply acknowledges a
  `🧠 [nexa-kev memlearn]` directive and a `mnemosyne_remember` call shows
  in the turn (check `audit_log` for the row).
* P2 recall-on-task: start a fresh session, ask something the saved rule
  governs → the rule surfaces in the injected memory block.
* P3 error-recall: induce a failure whose pattern has a stored memory
  (e.g. run the wrong interpreter twice) → tool result ends with
  `--- Related memory (nexa-kev recall) ---`.
* Trail: `grep -E '"kind": "(memlearn|memrecall)"' <profile>/plugins-data/nexa-kev/decisions.jsonl`
  (`fired|fired_shadow|silent_*|capped|no_hits` verdicts with p/spread/ms).
* v0.3.0 note: the memroute block rule now accepts `spread == 0.0`
  (perfect agreement) — previously `spread or 1` mistook 0.0 for missing
  and the block never fired at full agreement.

## Troubleshooting (all field-proven)

* `NO DECISION (missing_key)` — key not in any .env the session resolves;
  disk sessions read profile `.env`, gateway sessions read the host one.
* `NO DECISION (auth)` — key valid but team lacks the `/kev` grant → do the
  gateway grant above.
* `NO DECISION (unreachable)` — network/endpoint list; HTTPS on :4000 is
  NOT served (plain HTTP there; TLS only on 443 via the public host).
* Tool "does not exist" in a fresh chat but P0 passes — gateway not
  restarted after install; see restart step.
* Offline plugin probes that return instantly (~1 ms) with `allow`/`ok` —
  you're using a python that cannot `import hermes_cli`; key resolution
  degrades silently to fail-open. Use the agent venv python
  (`~/.hermes/installs/*/environments/*/venv/bin/python3`).

## Proven state (2026-11-06)

Live on mybuddy-beez and mybuddy-trade: native tool, silent advisories,
memory-router — all with block/allow live proofs and audit trails;
ensemble removes the measured ±0.3 phrasing noise of the engine.
