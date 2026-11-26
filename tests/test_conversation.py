from agent.memory.conversation import ConversationStore, InMemoryConversationRepository


def test_conversation_history_is_tenant_and_user_scoped():
    store = ConversationStore(InMemoryConversationRepository())
    store.append("s1", "t1", "u1", "user", "hello")
    store.append("s1", "t1", "u1", "assistant", "hi")
    store.append("s1", "t2", "u1", "user", "secret")
    store.append("s1", "t1", "u2", "user", "other")

    history = store.list("s1", "t1", "u1")
    assert [item.content for item in history] == ["hello", "hi"]
    assert store.list("s1", "t2", "u1")[0].content == "secret"


def test_conversation_limit_and_delete():
    store = ConversationStore(InMemoryConversationRepository())
    for index in range(5):
        store.append("s1", "t1", "u1", "user", str(index))
    assert [item.content for item in store.list("s1", "t1", "u1", limit=2)] == ["3", "4"]
    store.delete("s1", "t1", "u1")
    assert store.list("s1", "t1", "u1") == []
