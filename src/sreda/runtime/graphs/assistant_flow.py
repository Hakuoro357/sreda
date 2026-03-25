from typing import TypedDict

from langgraph.graph import END, START, StateGraph


class AssistantState(TypedDict, total=False):
    tenant_id: str
    workspace_id: str
    assistant_id: str
    thread_id: str
    incoming_message: str
    final_response: str


def load_context(state: AssistantState) -> AssistantState:
    return state


def generate_response(state: AssistantState) -> AssistantState:
    return {**state, "final_response": "TODO"}


def build_assistant_graph():
    graph = StateGraph(AssistantState)
    graph.add_node("load_context", load_context)
    graph.add_node("generate_response", generate_response)
    graph.add_edge(START, "load_context")
    graph.add_edge("load_context", "generate_response")
    graph.add_edge("generate_response", END)
    return graph.compile()
