"""应用级依赖容器：单例管理检索器 / LLM / 图 / 缓存等，供 API 层与脚本复用。"""
from contextlib import contextmanager
from dataclasses import dataclass


@dataclass
class Container:
    settings: object
    embedder: object
    vector_store: object
    bm25: object
    reranker: object
    retriever: object
    llm: object
    tracer: object
    cache: object
    graph: object
    checkpoint_connection: object | None = None

    @contextmanager
    def db_session(self):
        from app.db.base import SessionLocal

        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()


_container: Container | None = None


def get_container() -> Container:
    global _container
    if _container is None:
        from app.agent.graph import build_graph
        from app.agent.llm import get_llm
        from app.agent.nodes import DiagnosisNodes
        from app.config import get_settings
        from app.rag.bm25 import BM25Retriever
        from app.rag.embeddings import get_embedding_provider
        from app.rag.hybrid import HybridRetriever
        from app.rag.rerank import get_reranker
        from app.rag.vector_store import get_vector_store
        from app.services.cache import get_cache
        from app.services.observability import get_tracer

        settings = get_settings()
        embedder = get_embedding_provider(settings)
        vector_store = get_vector_store(settings)
        bm25 = BM25Retriever()
        reranker = get_reranker(settings)
        retriever = HybridRetriever(vector_store, bm25, embedder, reranker, settings)
        llm = get_llm(settings)
        tracer = get_tracer()
        cache = get_cache()
        nodes = DiagnosisNodes(None)
        connection = None
        checkpointer = None
        if settings.database_url.startswith("postgresql"):
            import psycopg
            from langgraph.checkpoint.postgres import PostgresSaver
            from psycopg.rows import dict_row

            connection = psycopg.connect(settings.database_url.replace("postgresql+psycopg://", "postgresql://"),
                                         autocommit=True, row_factory=dict_row)
            checkpointer = PostgresSaver(connection)
            checkpointer.setup()
        elif settings.checkpoint_path:
            import sqlite3
            from pathlib import Path

            from langgraph.checkpoint.sqlite import SqliteSaver

            Path(settings.checkpoint_path).parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(settings.checkpoint_path, check_same_thread=False)
            checkpointer = SqliteSaver(connection)
        graph = build_graph(nodes, checkpointer)
        _container = Container(
            settings=settings,
            embedder=embedder,
            vector_store=vector_store,
            bm25=bm25,
            reranker=reranker,
            retriever=retriever,
            llm=llm,
            tracer=tracer,
            cache=cache,
            graph=graph,
            checkpoint_connection=connection,
        )
        nodes.container = _container
    return _container


def reset_container() -> None:
    """测试用：重建容器（例如切换配置后）。"""
    global _container
    if _container is not None and _container.checkpoint_connection is not None:
        _container.checkpoint_connection.close()
    _container = None


def retrieval_mode(settings) -> str:
    return getattr(settings, "retrieval_mode", "hybrid")
