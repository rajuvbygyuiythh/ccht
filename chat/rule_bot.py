"""ChatRuleBot — top-level orchestrator for EVA chat replies.

Engine selection (environment variable ``EVA_ENGINE``):

  * ``flow``  (default) — the simplified diagram funnel engine
    (``eva_flow.py``): symmetric input/output txt categories,
    greeting -> age/gender -> country -> flirty -> positive -> share
    snap -> END.  Every reply comes from ``data/output/*.txt``.

  * ``legacy`` — the original full RuleEngine (``chat/rules.py``) with
    style mirroring, memory facts, stage-rule brain and TG fallback.

Both engines share the same data files (``data/input/`` +
``data/output/``) and the same conversation API, so the browser worker,
GUI and tools work with either one unchanged.
"""

from __future__ import annotations

import os
import sys
from typing import Any, Dict, List

_ENGINE_MODE = os.environ.get("EVA_ENGINE", "flow").strip().lower()
if _ENGINE_MODE not in ("flow", "legacy"):
    _ENGINE_MODE = "flow"


def _import_flow_engine():
    """Import eva_flow.EvaFlowBot from the project root."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if root not in sys.path:
        sys.path.insert(0, root)
    from eva_flow import EvaFlowBot  # noqa: E402
    return EvaFlowBot


if _ENGINE_MODE == "legacy":
    # ------------------------------------------------------------------
    # ORIGINAL ENGINE (chat/rules.py)
    # ------------------------------------------------------------------
    from .rules import RuleEngine

    class ChatRuleBot:
        """Stateful chat bot wrapper.

        Usage::

            bot = ChatRuleBot()
            state = bot.new_conversation()
            reply = bot.reply("hey", state)
            reply = bot.reply("where r u from", state)
        """

        def __init__(self) -> None:
            self._engine = RuleEngine()

        # ------------------------------------------------------------------
        # Public API
        # ------------------------------------------------------------------

        def new_conversation(self) -> Dict[str, Any]:
            """Create a fresh conversation state for one chat session."""
            return self._engine.init_conversation()

        def reply(
            self,
            user_message: str,
            state: Dict[str, Any],
        ) -> str:
            """Generate a reply for *user_message* given current *state*.

            The state is mutated in-place so subsequent calls have context.
            """
            return self._engine.decide_reply(user_message, state)

        def get_pending_replies(self, state: Dict[str, Any]) -> List[str]:
            """Get and clear any pending multi-message replies (e.g. hint + text).

            Returns a list of replies to send in order. Empty if no pending.
            """
            pending = state.get("pending_replies", [])
            state["pending_replies"] = []
            return pending

        def last_debug_trace(self) -> List[str]:
            """Debug lines explaining the most recent reply decision.

            Only meaningful right after calling :meth:`reply`.
            """
            return list(self._engine.last_trace)

        def last_debug_summary(self) -> str:
            """One-line 'why this reply' summary of the most recent decision.

            Mirrors the flow-map: PATTERN DETECTOR first-SMS routing, the io TXT
            category that fired, KEYWORD DETECTOR PATH A/B, or the machine stage.
            Only meaningful right after calling :meth:`reply`.
            """
            return self._engine._debug_summary(self._engine._last_emitting_state)

        def last_picked_info(self) -> tuple:
            """Return (file_name, line_number) of the last picked reply.

            Only meaningful right after calling :meth:`reply`.
            """
            io = self._engine.io_engine
            return (io.last_picked_file, io.last_picked_line_num)

        @property
        def pipe_db_size(self) -> int:
            return len(self._engine.pipe_db)

        @property
        def evoflow_rule_count(self) -> int:
            return sum(len(r) for r in self._engine.evoflow_rules.values())

        def stats(self) -> Dict[str, Any]:
            return {
                "pipe_db_entries": self.pipe_db_size,
                "evoflow_rules": self.evoflow_rule_count,
                "personality_spec_chars": len(self._engine.personality_spec),
            }

else:
    # ------------------------------------------------------------------
    # DIAGRAM FLOW ENGINE (eva_flow.py) — default
    # ------------------------------------------------------------------
    _FlowEngine = _import_flow_engine()

    class ChatRuleBot(_FlowEngine):
        """Drop-in ChatRuleBot backed by the diagram funnel engine.

        Inherits the full conversation API (``new_conversation`` /
        ``reply`` / ``get_pending_replies`` / ``last_picked_info`` /
        ``last_debug_summary`` / ``pipe_db_size`` / ``evoflow_rule_count``)
        from :class:`eva_flow.EvaFlowBot`.
        """

        def __repr__(self) -> str:  # pragma: no cover
            return "<ChatRuleBot engine=eva_flow (diagram funnel)>"
