"""adjudicate/graph.py -- the LangGraph definition wiring the five nodes.

escalation_check --(no_action|auto_flag|insufficient_data)--> END
                  --(escalated)--> fetch_context -> vlm_evidence -> structure_output -> policy_adjudicate -> END
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from adjudicate.nodes.escalation_check import (
    PATH_AUTO_FLAG,
    PATH_ESCALATED,
    PATH_INSUFFICIENT_DATA,
    PATH_NO_ACTION,
    escalation_check,
)
from adjudicate.nodes.fetch_context import fetch_context
from adjudicate.nodes.policy_adjudicate import policy_adjudicate
from adjudicate.nodes.structure_output import structure_output
from adjudicate.nodes.vlm_evidence import vlm_evidence
from adjudicate.state import AdjudicateState


def build_graph():
    graph = StateGraph(AdjudicateState)

    graph.add_node("escalation_check", escalation_check)
    graph.add_node("fetch_context", fetch_context)
    graph.add_node("vlm_evidence", vlm_evidence)
    graph.add_node("structure_output", structure_output)
    graph.add_node("policy_adjudicate", policy_adjudicate)

    graph.add_edge(START, "escalation_check")
    graph.add_conditional_edges(
        "escalation_check",
        lambda state: state["path"],
        {
            PATH_NO_ACTION: END,
            PATH_AUTO_FLAG: END,
            PATH_INSUFFICIENT_DATA: END,
            PATH_ESCALATED: "fetch_context",
        },
    )
    graph.add_edge("fetch_context", "vlm_evidence")
    graph.add_edge("vlm_evidence", "structure_output")
    graph.add_edge("structure_output", "policy_adjudicate")
    graph.add_edge("policy_adjudicate", END)

    return graph.compile()


_compiled = None


def get_app():
    global _compiled
    if _compiled is None:
        _compiled = build_graph()
    return _compiled
