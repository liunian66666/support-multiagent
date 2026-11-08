"""FAQ 快答检查点旁路图（从 agent_factory.py 拆出）。"""
from langgraph.graph import END, START, MessagesState, StateGraph


def build_faq_bridge(checkpointer):
    graph = StateGraph(MessagesState)

    def _record(state):
        # 输入已携带 [HumanMessage(q), AIMessage(a)]，add_messages reducer 会自动追加，
        # 节点无需再产出消息；返回空 dict 表示本步无新增字段。
        return {}

    graph.add_node("record", _record)
    graph.add_edge(START, "record")
    graph.add_edge("record", END)
    return graph.compile(checkpointer=checkpointer)
