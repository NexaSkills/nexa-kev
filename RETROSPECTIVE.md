# Retrospective — 2026-11-06: nexa-kev from zero to fleet (beez + trade + repo)

Built nexa-kev v0.1 -> v0.3.0 in one day and deployed to two hosts plus a
self-service repo. Every claim below is from today's audit trails
(decisions.jsonl), git log, gateway logs, and session tool history.

## Timeline (SAST)

| ~time | step |
|---|---|
| 14:26 | work order received; plugin skeleton; endpoint research |
| 14:32–15:52 | v0.1 native kev_decide; gateway /kev passthrough discovery; beez bounce + live proof |
| 16:25 | v0.2.0 pre_llm_call advisory proven live (⚡ lines, 12/24 honest silences in day's trail) |
| 16:45–17:05 | v0.2.1 dotenv key-resolution fix (disk-lane missing_key root cause) |
| 17:39–18:41 | trade rollout (team grant, bounces, P0–P2 proofs both hosts) |
| 18:00–18:20 | measured engine phrasing noise (noul 0.50/0.82/0.77, spread 0.31 on one scenario) -> v0.2.2 paraphrase ensemble (median + spread demotion), 6/6 offline battery, live proof |
| 18:37–18:50 | v0.3.0 memory-router (pre_tool_call): secret gate + kev shared/private classification; 7/7 battery; live block AND allow on both hosts |
| 19:00–19:30 | repo NexaSkills/nexa-kev: installer + checker + bounce scripts; install+check PASS from fresh clones on BOTH hosts |

## Mistakes made today (each with root cause + the rule it earned)

M1. Shipped an installer containing `mapfile -p` — a flag that does not
    exist. Root cause: wrote plausible bash and let the fresh-clone test be
    its first execution. RULE: every script path must execute at least once
    before push; "wrote it" is not "it works".

M2. Installer trusted inherited HERMES_HOME — inside gateway-served shells
    that env points at a PROFILE home, not the host root, so installs and
    `hermes config get/set` would have hit the wrong scope. RULE: treat
    inherited HERMES_HOME as poisoned in any script run from an agent
    session; derive host root from $HOME (or an explicit override var).

M3. Two patch-tool incidents: (a) an append-style old_string silently
    REPLACED `_as_float` (restored same day), (b) a `replace_all` clobbered
    an unrelated comment and an env-var name. RULE: patches need unique
    contextual anchors; `replace_all` only for genuinely uniform strings,
    and re-grep the file right after one touches anything semantic.

M4. Offline test batteries first ran under an interpreter that cannot
    `import hermes_cli`; dotenv key resolution silently degraded to
    fail-open and the hooks LOOKED like they passed (all "allow" in ~1 ms).
    Caught because 1 ms is impossible for a ~250 ms kev call. RULE: sanity-
    check timings against expected cost; an instant verdict from a networked
    decision is a tell that no decision happened. Offline plugin tests must
    use the agent venv python (`~/.hermes/installs/*/environments/*/venv`).

M5. Declared "ready for cross-host test" before cross-host test. The second
    host then failed on an unproven assumption (M6). RULE: the Nth host is
    part of the definition of done, not a follow-up.

M6. Assumed every fleet host has the GitHub PAT in git's credential store.
    trade's `~/.git-credentials` was empty — their existing repos were
    rsync-seeded, never cloned, so the assumption had never been exercised.
    RULE: probe assumptions directly (one `git ls-remote`) instead of
    inferring from "other repos work here".

M7. Fired a gateway bounce followed by verification steps in the SAME tool
    call; the bounce killed my own turn mid-call (orphan-recovery prompt).
    Harmless (session survived, as the playbook documents) but avoidable.
    RULE: fire-and-exit the bounce command; verify in the NEXT call.

M8. Hand-rolled curl calls against /kev/systemone failed twice on the
    questions wire shape before I consulted the plugin's own
    `normalize_questions` — which already encoded the correct contract.
    RULE: before hand-crafting a request, grep the working code that
    already makes it; the codebase is the wire-shape oracle.

M9. (pre-v0.2.1) Disk-lane sessions returned NO DECISION (missing_key)
    while gateway-lane worked — the two lanes resolve keys differently.
    RULE: exercise EVERY session lane (gateway, disk chat, cron) at first
    smoke, not just the convenient one.

M10. Version bumps silently no-op'd twice: a sed pattern missed a quoted
    YAML value, and a bump chained with a blocked restart command never ran
    at all (the block aborted the chain before the sed). RULE: verify
    version strings with a follow-up grep as part of the same step, never
    trust chained commands after an operation that can pre-block.

## What worked (keep doing)

W1. Measure before tune: the 3-framing sensitivity experiment (spread 0.31
    on identical scenarios) EXPLAINED the day's threshold wobble
    (0.672 vs 0.726) and dictated the ensemble design. Without the number,
    the fix would have been a guess.
W2. Contract-first hook work: grep'd `pre_tool_call` kwargs and return
    semantics before writing memroute.py — hook wiring needed zero rework.
W3. Fail-open + audit trail: remote verification of trade came from
    decisions.jsonl rows (0.976 block_private), not from trusting an agent's
    prose. Ground truth > narration, every time.
W4. Honest silence as a feature: half the day's advisories were legitimately
    weak and stayed quiet; the injected ones were correct nearly always
    (litellm-admin x4, bounce skill at the right second).
W5. Meta-verification: when kev's own verdict was on trial, asking kev the
    second-order questions (0.83 / 0.42) exposed exactly where confidence
    should not be trusted (proceeding without the human).
W6. Existing skills paid rent: the bounce-from-inside playbook (setsid +
    encoded verb + PID-diff verification) worked again under live conditions.
W7. Encoding policy INTO scripts (installer refuses to edit YAML by hand,
    prints admin/restart steps instead of doing them) — the rules survive
    whoever runs it.

## Standing insights

* Confidence is robustness-under-rephrasing, not a raw probability. Report
  median + spread with every verdict; disagreement across framings is the
  honest UNCERTAIN trigger.
* A decision lane must say one of three things: a number, a named failure
  (missing_key/auth/unreachable), or silence. Anything else is bluffing.
* Lanes differ (gateway vs disk vs cron): keys, tool visibility, env
  hygiene. Verify per lane; never generalize from one.
* The fleet is bigger than its habits: trade "worked" without a single
  clone credential because nothing had ever needed one.

## Action items taken from this retro

* Both hosts + repo carry the rules in docs/skill (this file is the anchor).
* Installer E2E from fresh clone on a second host is now part of the
  definition of done for anything fleet-facing.
* Follow-up candidate (not yet built): recall-side hinting once the
  post_tool_call callback contract is verified (save-side is done).
