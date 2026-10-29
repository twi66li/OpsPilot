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
        graph = build_graph(nodes)
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
        )
        nodes.container = _container
    return _container


def reset_container() -> None:
    """测试用：重建容器（例如切换配置后）。"""
    global _container
    _container = None


def retrieval_mode(settings) -> str:
    return getattr(settings, "retrieval_mode", "hybrid")
