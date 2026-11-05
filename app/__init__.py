"""AI 记忆层 · 实现骨架

对应三份 SQL：
    sql/01_schema.sql    表与索引
    sql/02_retrieval.sql 混合召回（RRF + 衰减）
    sql/03_write.sql     失效式写入 + SKIP LOCKED 队列

分层约定：
    decide.py     纯函数，无 IO、无依赖 —— 裁决的全部规则都在这里，可单测
    embedding.py  向量化接口 + 零依赖的确定性替身（本地跑通/单测用）
    llm.py        两次 LLM 调用（抽取、裁决建议）+ 离线规则替身
    store.py      Postgres 读写，SQL 与 sql/*.sql 一一对应
    worker.py     队列消费循环、失败退避、租约回收
    retrieve.py   应用侧取记忆的唯一入口
"""

__all__ = ["config", "decide", "embedding", "llm", "store", "worker", "retrieve"]
