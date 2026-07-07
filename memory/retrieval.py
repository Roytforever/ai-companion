"""轻量级向量检索 —— 纯 Python TF-IDF + 余弦相似度（无第三方依赖）

用于记忆的语义化召回：在用户提问时，从「历史消息 + 蒸馏摘要」中
找出最相关的片段注入上下文，避免把全部历史无差别回传给模型。

之所以不引入 sentence-transformers / numpy：
- 项目依赖保持精简（requirements.txt 无 ML 依赖）；
- 本地单用户场景下记忆量级小（数百条），TF-IDF 足够且零成本；
- 中文采用「字符 unigram + bigram」切分，英文按词，兼顾中英文召回。
"""

import math
import re
from collections import Counter

_CJK = r"[\u4e00-\u9fff]"
_WORD = r"[a-z0-9]+"


def _tokenize(text: str) -> list[str]:
    """中英文混合分词

    - 英文/数字：连续字母数字（长度>=2）作为一个 token
    - 中文：逐字 unigram + 相邻 bigram（捕捉局部语义）
    """
    text = (text or "").lower()
    tokens: list[str] = []
    # 英文/数字词
    for m in re.findall(_WORD, text):
        if len(m) >= 2:
            tokens.append(m)
    # 中文片段：提取连续 CJK，做 unigram + bigram
    for cjk_run in re.findall(_CJK + r"+", text):
        for ch in cjk_run:
            tokens.append("c:" + ch)
        for i in range(len(cjk_run) - 1):
            tokens.append("b:" + cjk_run[i] + cjk_run[i + 1])
    return tokens


class TfidfRetriever:
    """在内存语料上做 TF-IDF 检索，返回与 query 最相似的文档。"""

    def __init__(self):
        self._docs: list[str] = []
        self._doc_ids: list = []
        self._idf: dict[str, float] = {}
        self._doc_vectors: list[dict[str, float]] = []
        self._N = 0

    def fit(self, documents: list[str], doc_ids: list | None = None):
        """构建索引。documents 与 doc_ids 一一对应。"""
        self._docs = documents
        self._doc_ids = doc_ids if doc_ids is not None else list(range(len(documents)))
        self._N = len(documents)

        # 文档词频
        doc_tfs: list[Counter] = []
        df: Counter = Counter()
        for doc in documents:
            tf = Counter(_tokenize(doc))
            doc_tfs.append(tf)
            for term in tf:
                df[term] += 1

        # IDF（平滑）
        self._idf = {
            term: math.log((self._N + 1) / (cnt + 1)) + 1.0
            for term, cnt in df.items()
        }

        # 文档向量（L2 归一化）
        self._doc_vectors = []
        for tf in doc_tfs:
            vec = {term: (cnt / max(1, sum(tf.values()))) * self._idf[term]
                   for term, cnt in tf.items()}
            norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
            self._doc_vectors.append({t: v / norm for t, v in vec.items()})
        return self

    @staticmethod
    def _query_vector(query: str, idf: dict[str, float]) -> dict[str, float]:
        tf = Counter(_tokenize(query))
        total = max(1, sum(tf.values()))
        vec = {term: (cnt / total) * idf.get(term, 0.0) for term, cnt in tf.items()}
        norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
        return {t: v / norm for t, v in vec.items()}

    def rank(self, query: str, top_k: int = 3) -> list[tuple]:
        """返回 [(doc_id, score, text), ...] 按相似度降序，过滤零分。"""
        if self._N == 0:
            return []
        qv = self._query_vector(query, self._idf)
        if not qv:
            return []
        scored: list[tuple] = []
        for idx, dvec in enumerate(self._doc_vectors):
            score = sum(qv[t] * dvec[t] for t in qv if t in dvec)
            if score > 0:
                scored.append((self._doc_ids[idx], score, self._docs[idx]))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]
