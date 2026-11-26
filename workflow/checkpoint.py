"""LangGraph checkpointer factory with optional Postgres persistence."""
import warnings


class CheckpointUnavailable(RuntimeError):
    pass


def native_saver_of(store):
    """Return the native LangGraph saver used by ``compile(checkpointer=...)``."""
    if store is None:
        return None
    native = getattr(store, "native", None)
    if native is not None:
        return native
    inner = getattr(store, "_inner", None)
    if inner is not None:
        return inner
    if hasattr(store, "get_tuple") and hasattr(store, "put"):
        return store
    return None


def _config_for(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


def _unwrap_checkpoint(raw):
    if raw is None:
        return None
    if isinstance(raw, dict) and "channel_values" not in raw and "checkpoint" not in raw:
        # Plain state dict (e.g. mock saver returning state directly).
        return dict(raw)
    ckpt = getattr(raw, "checkpoint", None)
    if isinstance(ckpt, dict):
        values = ckpt.get("channel_values")
        return dict(values) if isinstance(values, dict) else dict(ckpt)
    if isinstance(raw, dict) and isinstance(raw.get("checkpoint"), dict):
        ckpt = raw["checkpoint"]
        values = ckpt.get("channel_values")
        return dict(values) if isinstance(values, dict) else dict(ckpt)
    if isinstance(raw, dict):
        return dict(raw)
    return None


class _SaverAdapter:
    """把 langgraph 原生 saver 包成 get/put，并保留原生对象做真 checkpoint。

    put/get(thread_id, state) 为兼容接口，内部委托原生 saver 的
    put(config, checkpoint, ...)/get_tuple(config)（RunnableConfig
    configurable.thread_id）。内存 mirror 仅作离线兜底。
    """

    def __init__(self, inner, context=None):
        self._inner = inner
        self._context = context
        self._mem: dict[str, dict] = {}

    @property
    def native(self):
        return self._inner

    @property
    def is_native(self) -> bool:
        inner = self._inner
        return hasattr(inner, "get_tuple") and hasattr(inner, "put")

    def get_tuple(self, config: dict):
        """原生形状透传，供 LangGraph 编译时使用。"""
        return self._inner.get_tuple(config)

    def put(self, thread_id: str, state: dict) -> None:
        payload = dict(state)
        # 离线 mirror 先行，保证原生失败时仍可读。
        self._mem[thread_id] = dict(payload)
        inner = self._inner
        if not hasattr(inner, "put"):
            return
        config = _config_for(thread_id)
        try:
            try:
                inner.put(config, dict(payload), {}, {})
            except TypeError:
                inner.put(config, dict(payload))
        except Exception as exc:
            warnings.warn(f"native checkpointer put failed, using in-memory mirror: {exc}", stacklevel=2)

    def get(self, thread_id: str) -> dict | None:
        inner = self._inner
        if hasattr(inner, "get_tuple"):
            config = _config_for(thread_id)
            try:
                raw = inner.get_tuple(config)
                unwrapped = _unwrap_checkpoint(raw)
                if unwrapped is not None:
                    self._mem[thread_id] = dict(unwrapped)
                    return dict(unwrapped)
            except Exception as exc:
                warnings.warn(f"native checkpointer get failed, using in-memory mirror: {exc}", stacklevel=2)
        v = self._mem.get(thread_id)
        return dict(v) if v is not None else None

    def close(self) -> None:
        """释放 PostgresSaver 的底层连接（from_conn_string 是上下文管理器）。"""
        ctx, self._context = self._context, None
        if ctx is not None:
            try:
                ctx.__exit__(None, None, None)
            except Exception:
                pass


def _with_connect_timeout(dsn: str, seconds: int = 3) -> str:
    if not dsn or "connect_timeout=" in dsn:
        return dsn
    sep = "&" if "?" in dsn else "?"
    return f"{dsn}{sep}connect_timeout={seconds}"


def get_checkpointer(dsn: str | None = None):
    import os

    from infrastructure.pg.connector import agent_dsn
    explicit = dsn is not None
    dsn = dsn if explicit else agent_dsn()
    strict = os.getenv("AGENT_STRICT_PERSISTENCE", "").lower() in {"1", "true", "yes", "on"}
    if dsn:
        try:
            from langgraph.checkpoint.postgres import PostgresSaver  # type: ignore
            # langgraph-checkpoint-postgres >= 2 returns a context manager.
            ctx = PostgresSaver.from_conn_string(_with_connect_timeout(dsn))
            saver = ctx.__enter__()
            try:
                saver.setup()
            except Exception:
                ctx.__exit__(None, None, None)
                raise
            return _SaverAdapter(saver, context=ctx)
        except Exception as exc:
            if explicit or strict:
                raise CheckpointUnavailable("Postgres checkpointer is unavailable") from exc
            warnings.warn(f"PostgresSaver unavailable ({exc}), falling back to memory saver", stacklevel=2)
    from langgraph.checkpoint.memory import MemorySaver
    return MemorySaver()
