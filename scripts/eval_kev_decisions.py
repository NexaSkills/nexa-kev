#!/usr/bin/env python3
"""Live scoring harness for kev ask-context quality (decision eval, not unit).

Replays evals/cases.jsonl through the SAME question text the lanes send
(config.py Q_BANK / Q_SAVE) against the LIVE engine, and reports the
separation margin per lane. Baseline 2026-11-10 (fleet canary rows): bank
doctrine landed 0.61-0.76, i.e. margin vs private cases is the number to
grow; a context change that does not grow margin is not an improvement.

    python3 scripts/eval_kev_decisions.py [--offline]

Offline mode stubs the engine with each lane's prefilter-only signals (CI
smoke), live mode needs the kev grant reachable, exactly like the plugin.
"""
import json
import pathlib
import sys

PLUGIN = pathlib.Path(__file__).resolve().parent.parent / "plugin" / "nexa-kev"
sys.path.insert(0, str(PLUGIN))

import kev_decide as kd  # noqa: E402
import memlearn as ml  # noqa: E402  (source of the real lane question text)

QUESTIONS = {"bank": ml._Q_BANK, "save": ml._Q_SAVE}
EXPECT_YES = {"bank": {"shared": "SAFE/YES", "private": "NO"},
              "save": {"save": "SAFE/YES", "nosave": "NO"}}


import re
_FLEET = re.compile(r"\b(every|all|everyone)\b.*\b(agent|host|machine|fleet)s?\b", re.I)
_HOSTY = re.compile(r"(\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b|/home/\w|port\s+\d{2,5}|\bthis (host|machine|box)\b)", re.I)
_POLICY = re.compile(r"\b(from now on|always|never|policy|rule)\b", re.I)


def signals(text: str) -> str:
    return ("signals: scope-tokens=" + ("fleet-wide" if _FLEET.search(text) else "none")
            + "; host-specific-tokens=" + ("present" if _HOSTY.search(text) else "none")
            + "; speech-act=" + ("standing-policy" if _POLICY.search(text) else "conversational")
            + "\n")


def score(case, offline=False, ctx=False):
    if offline:
        # deterministic stub: prefilter-class style signal only
        s = 0.9 if case["expect"] in ("shared", "save") else 0.2
        return {"noul": s, "verdict": "SAFE/YES" if s >= 0.75 else "NO"}
    body = (signals(case["text"]) if ctx else "") + f"Content under evaluation:\n{case['text']}"
    r = kd.decide(body, [QUESTIONS[case["lane"]]])
    ds = (r.get("decisions") or {})
    return ds.get("q1") or {"noul": None, "verdict": "ERR:" + r.get("reason", "?")}


def main():
    offline = "--offline" in sys.argv
    ctx = "--ctx" in sys.argv
    cases = [json.loads(l) for l in
             (pathlib.Path(__file__).resolve().parent.parent / "evals" /
              "cases.jsonl").read_text().splitlines() if l.strip()]
    rows, fails, margins = [], 0, {}
    for c in cases:
        d = score(c, offline, ctx)
        ok = d.get("verdict") == EXPECT_YES[c["lane"]][c["expect"]]
        fails += 0 if ok else 1
        rows.append((c["id"], c["lane"], c["expect"], d.get("noul"),
                     d.get("verdict"), "OK" if ok else "MISS"))
    w = max(len(r[0]) for r in rows) + 2
    print("\n".join(f"{r[0]:{w}} lane={r[1]:5} expect={r[2]:7} "
                    f"noul={r[3]} verdict={r[4]:9} {r[5]}" for r in rows))
    for lane in ("bank", "save"):
        sel = [r for r in rows if r[1] == lane and isinstance(r[3], float)]
        pos = [r[3] for r in sel if r[2] in ("shared", "save")]
        neg = [r[3] for r in sel if r[2] in ("private", "nosave")]
        if pos and neg:
            margins[lane] = sum(pos)/len(pos) - sum(neg)/len(neg)
            print(f"MARGIN[{lane}] = {margins[lane]:.3f}"
                  f"  (pos mean {sum(pos)/len(pos):.3f} / neg mean "
                  f"{sum(neg)/len(neg):.3f})")
    # pass = per-lane separation margin above frozen baselines (verdict
    # strings are informational; separation is what context work moves).
    BASELINE = {"bank": 0.280, "save": 0.761}
    bad = [] if offline else [l for l, m in margins.items()
                 if m < BASELINE[l] - 0.05]
    print(f"margins={ {k: round(v, 3) for k, v in margins.items()} } "
          f"baselines={BASELINE}")
    print(f"EVAL: {'PASS' if not bad else 'REGRESSION ' + ','.join(bad)}"
          + (" (offline-stub)" if offline else "")
          + f"  ({fails} verdict-MISS informational)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
