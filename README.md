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
   `registered tool kev_decide` and `memory-router hook`.

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
