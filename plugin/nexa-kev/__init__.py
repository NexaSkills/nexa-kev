"""nexa-kev — general-purpose kev decision helper plugin.

Exposes one tool, ``kev_decide``: any agent turn, lifecycle hook, or script
can ask kev (via the gateway /kev/systemone passthrough) for a calibrated
yes/no decision on a free-text question. Honest verdicts, graceful
degradation, no routing logic, standalone from hermes-kev-router.

Both tool gates are satisfied:
  * plugin.yaml declares ``provides_tools: [kev_decide]``
  * register() calls ``ctx.register_tool(...)`` with a real JSON schema.

Hooks/scripts can also import the module-level function directly:
    from kev_decide import decide   # (or import the package)
    result = decide("Proposed action: ...", ["Is this safe?"])
"""

from __future__ import annotations

import logging

try:  # package-relative (normal host load) with flat fallback (dev/tests)
    from . import kev_decide as _kd
    from . import advisory as _adv
except ImportError:  # pragma: no cover
    import kev_decide as _kd  # type: ignore
    import advisory as _adv  # type: ignore

logger = logging.getLogger("hermes.plugins.nexa-kev")

TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "state": {
            "type": "string",
            "description": (
                "Compact factual description of the situation and the "
                "proposed action/goal (what kev should judge). Free text; "
                "trimmed to ~1200 chars on the wire."
            ),
        },
        "questions": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "One or more yes/no questions about the state, e.g. "
                '["Is it safe to reply to a health probe?"]. Each becomes a '
                "noul question q1, q2, ... in order."
            ),
        },
        "properties": {
            "type": "object",
            "description": (
                "Optional key/value facts appended to the state "
                "(e.g. {\"reversible\": \"yes\", \"blast_radius\": \"low\"})."
            ),
            "additionalProperties": True,
        },
    },
    "required": ["state", "questions"],
}


def decide(state_text, questions, properties=None, cfg=None):
    """Module-level entry point for hooks/scripts (never raises)."""
    return _kd.decide(state_text, questions, properties, cfg=cfg)


def register(ctx) -> None:
    try:
        ctx.register_tool(
            name="kev_decide",
            toolset="nexa-kev",
            schema=TOOL_SCHEMA,
            handler=_kd.decide_tool_handler,
            description=(
                "Ask kev for a decision on a state/action + yes/no questions. "
                "Returns raw probabilities + honest verdicts "
                "(SAFE/YES p>=0.7, UNSAFE/NO p<=0.3, else UNCERTAIN), or "
                "'NO DECISION (<reason>)' when kev is unreachable."
            ),
            requires_env=[],
        )
        logger.info("nexa-kev: registered tool kev_decide")
    except Exception as e:  # registration must never break the host
        logger.warning("nexa-kev: register_tool failed: %s", e)
    try:
        ctx.register_hook("pre_llm_call", _adv.pre_llm_call)
        logger.info("nexa-kev: registered pre_llm_call advisory hook")
    except Exception as e:
        logger.warning("nexa-kev: register_hook(pre_llm_call) failed: %s", e)
    try:
        from . import memroute as _mr
        ctx.register_hook("pre_tool_call", _mr.pre_tool_call)
        logger.info("nexa-kev: registered pre_tool_call memory-router hook")
    except Exception as e:
        logger.warning("nexa-kev: register_hook(pre_tool_call) failed: %s", e)
    try:
        from . import memlearn as _ml
        ctx.register_hook("pre_llm_call", _ml.pre_llm_call)
        logger.info("nexa-kev: registered pre_llm_call memlearn hook")
    except Exception as e:
        logger.warning("nexa-kev: register_hook(pre_llm_call/memlearn) failed: %s", e)
    try:
        from . import memrecall as _mrc
        ctx.register_hook("transform_tool_result", _mrc.transform_tool_result)
        logger.info("nexa-kev: registered transform_tool_result memrecall hook")
    except Exception as e:
        logger.warning("nexa-kev: register_hook(transform_tool_result) failed: %s", e)
