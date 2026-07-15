"""两层记忆。

短期记忆（trim_history）：单次会话内对话变长时按 token 预算裁剪——system 永远保留、
最新一条无条件保留，其余从新到旧累加到预算为止。复用项目一的裁剪思路。
关键约束：裁剪不能切断 assistant(tool_calls) 与其 tool 结果的配对，否则 LLM 报错，
所以按"整轮"边界裁剪，不在工具调用中间下刀。

长期记忆（MemoryStore）：跨会话。会话结束时用 LLM 提炼用户的持久事实/偏好，
bge-m3 向量化后存 sqlite-vec；下次同一用户来，按当前问题语义召回 top-k 注入上下文。
"""

import logging
import sqlite3
from functools import lru_cache

import sqlite_vec
import tiktoken
from openai import OpenAI

from agent.config import settings

logger = logging.getLogger(__name__)
EMBED_DIM = 1024  # bge-m3


# ---------------- 短期：token 裁剪 ----------------


@lru_cache(maxsize=1)
def _enc() -> tiktoken.Encoding:
    return tiktoken.get_encoding("cl100k_base")


def _msg_tokens(m) -> int:
    # 兼容 dict 与 LangChain Message 对象
    content = m.get("content", "") if isinstance(m, dict) else getattr(m, "content", "")
    return len(_enc().encode(str(content))) + 4


def _role(m) -> str:
    if isinstance(m, dict):
        return m.get("role", "")
    return {"system": "system", "human": "user", "ai": "assistant", "tool": "tool"}.get(
        m.type, m.type
    )


def _has_tool_calls(m) -> bool:
    if isinstance(m, dict):
        return bool(m.get("tool_calls"))
    return bool(getattr(m, "tool_calls", None))


def trim_history(messages: list, budget: int) -> list:
    """按 token 预算裁剪对话历史。system 全保留；对话部分从新到旧保留，
    但遇到 tool 消息时必须连带它前面的 assistant(tool_calls)，不拆散配对。"""
    system = [m for m in messages if _role(m) == "system"]
    dialog = [m for m in messages if _role(m) != "system"]

    used = sum(_msg_tokens(m) for m in system)
    kept_rev: list = []
    for i, m in enumerate(reversed(dialog)):
        cost = _msg_tokens(m)
        if i > 0 and used + cost > budget:
            break
        kept_rev.append(m)
        used += cost
    kept = list(reversed(kept_rev))

    # 修边界：若最前面是"孤儿 tool 消息"（其 assistant 被裁掉了），一并丢弃
    while kept and _role(kept[0]) == "tool":
        kept.pop(0)
    return system + kept


# ---------------- 长期：向量记忆 ----------------

_MEM_EXTRACT_PROMPT = """从下面这轮客服对话里，提炼关于【用户】的持久事实或偏好，
用于以后跨会话个性化服务。每条一句话，只提炼真正长期有用的（身份、负责的系统/模块、
反复关心的问题、明确的偏好）。忽略一次性的闲聊。最多 3 条，没有就返回空列表。

只输出 JSON 对象：{{"memories": ["事实1", "事实2"]}}，没有则 {{"memories": []}}

对话：
{conversation}"""


class MemoryStore:
    def __init__(self) -> None:
        self._embed_client = OpenAI(
            api_key=settings.embedding_api_key, base_url=settings.embedding_base_url, timeout=30
        )
        self._llm = OpenAI(
            api_key=settings.deepseek_api_key, base_url=settings.deepseek_base_url, timeout=60
        )
        self.db = self._connect()

    def _connect(self) -> sqlite3.Connection:
        import os

        os.makedirs(os.path.dirname(settings.memory_db_path) or ".", exist_ok=True)
        # check_same_thread=False：recall/写入经 asyncio.to_thread 在 worker 线程执行，
        # 而连接建于主线程。本项目记忆操作是串行的，跨线程复用连接安全。
        db = sqlite3.connect(settings.memory_db_path, check_same_thread=False)
        db.enable_load_extension(True)
        sqlite_vec.load(db)
        db.enable_load_extension(False)
        db.execute(
            "CREATE TABLE IF NOT EXISTS memories ("
            "id INTEGER PRIMARY KEY, user_id TEXT, text TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP)"
        )
        db.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS mem_vec USING vec0(embedding float[{EMBED_DIM}])"
        )
        return db

    def _embed(self, text: str) -> list[float]:
        resp = self._embed_client.embeddings.create(model=settings.embedding_model, input=[text])
        return resp.data[0].embedding

    def remember(self, user_id: str, texts: list[str]) -> int:
        for text in texts:
            cur = self.db.execute(
                "INSERT INTO memories (user_id, text) VALUES (?, ?)", (user_id, text)
            )
            self.db.execute(
                "INSERT INTO mem_vec (rowid, embedding) VALUES (?, ?)",
                (cur.lastrowid, sqlite_vec.serialize_float32(self._embed(text))),
            )
        self.db.commit()
        return len(texts)

    def recall(self, user_id: str, query: str, k: int | None = None) -> list[str]:
        """按语义召回该用户最相关的 k 条记忆。"""
        k = k or settings.memory_recall_k
        rows = self.db.execute(
            "SELECT m.text FROM mem_vec v JOIN memories m ON m.id = v.rowid "
            "WHERE m.user_id = ? AND v.embedding MATCH ? AND k = ? ORDER BY v.distance",
            (user_id, sqlite_vec.serialize_float32(self._embed(query)), k),
        ).fetchall()
        return [r[0] for r in rows]

    def extract_and_store(self, user_id: str, conversation: str) -> list[str]:
        """会话结束时调用：LLM 提炼持久记忆并入库，返回新增的记忆文本。"""
        import json

        try:
            resp = self._llm.chat.completions.create(
                model=settings.llm_model,
                messages=[
                    {
                        "role": "user",
                        "content": _MEM_EXTRACT_PROMPT.format(conversation=conversation[:4000]),
                    }
                ],
                temperature=0.2,
                response_format={"type": "json_object"},
            )
            data = json.loads(resp.choices[0].message.content)
            facts = data.get("memories", []) if isinstance(data, dict) else data
        except Exception as e:
            logger.warning("记忆提炼失败: %s", e)
            return []
        facts = [f.strip() for f in facts if isinstance(f, str) and f.strip()][:3]
        if facts:
            self.remember(user_id, facts)
        return facts

    def close(self) -> None:
        self.db.close()
