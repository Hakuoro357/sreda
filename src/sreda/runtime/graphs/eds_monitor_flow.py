from typing import TypedDict

from langgraph.graph import END, START, StateGraph


class EDSMonitorState(TypedDict, total=False):
    tenant_id: str
    workspace_id: str
    account_id: str
    claim_id: str
    normalized_claim_transient: dict
    analysis_result: dict
    should_send: bool


def prepare_claim(state: EDSMonitorState) -> EDSMonitorState:
    return state


def analyze_claim(state: EDSMonitorState) -> EDSMonitorState:
    return {**state, "analysis_result": {}, "should_send": True}


def build_eds_monitor_graph():
    graph = StateGraph(EDSMonitorState)
    graph.add_node("prepare_claim", prepare_claim)
    graph.add_node("analyze_claim", analyze_claim)
    graph.add_edge(START, "prepare_claim")
    graph.add_edge("prepare_claim", "analyze_claim")
    graph.add_edge("analyze_claim", END)
    return graph.compile()
