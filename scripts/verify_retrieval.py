"""检索验证：对刚入库的病娇男孩手册做几个真实查询，确认端到端 RAG 可用。"""
import asyncio
import sys

from app.core.config import get_settings
from app.services.dm_service import DMGuideService

DOC = "90e692da-cd82-4b64-9e2d-e5f153cbcc9f"
SCRIPT = "25be02dc-a1ad-433a-97fa-19e4e93ac280"

QUERIES = [
    "萧何有哪七个人格，分别叫什么",
    "搜证阶段每个玩家能搜几次，有什么限制",
    "许彤是怎么死的，凶手是谁",
    "星期一轮到时话术有什么不同",
]


async def main() -> int:
    svc = DMGuideService(get_settings())
    for q in QUERIES:
        # hybrid 默认即以 qa 为主召回：qa 取满 top_k，chunk 仅作补充
        res = await svc.search(query=q, script_id=SCRIPT, document_id=DOC, mode="hybrid", top_k=8)
        print(f"\n=== 查询：{q}（{res.took_ms}ms）===")
        print(f"  [qa 主召回] {len(res.qa)} 条 | [chunk 补充] {len(res.chunks)} 条")

        # hits 是 qa 优先的扁平视图，打印前 5 条验证排序
        print(f"  [hits 排序] 前 5：")
        for h in res.hits[:5]:
            if h.type == "qa":
                q_text = h.payload.get("question", "")[:34]
                print(f"    {h.type:5} | 序分={h.similarity} 原分={h.raw_similarity} | Q: {q_text}")
            else:
                snip = (h.payload.get("content", "") or "")[:34].replace("\n", " ")
                print(f"    {h.type:5} | 序分={h.similarity} 原分={h.raw_similarity} | {snip}")

        if not res.hits:
            print("  （无命中）")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
