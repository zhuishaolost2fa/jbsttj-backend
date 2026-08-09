"""最小验证：新 SiliconFlow Key 是否对 chat / embedding 都有效。"""
import sys
import traceback

from app.core.config import get_settings
from app.services.llm import SiliconFlowClient


def main() -> int:
    settings = get_settings()
    key = settings.siliconflow_api_key
    print(f"[info] base_url   = {settings.siliconflow_base_url}")
    print(f"[info] chat_model = {settings.siliconflow_chat_model}")
    print(f"[info] embed_model= {settings.siliconflow_embed_model}")
    print(f"[info] key prefix = {key[:8]}...{key[-4:]}  len={len(key)}")

    client = SiliconFlowClient(settings)

    # 1) chat 最小请求
    try:
        ans = client.chat(
            [{"role": "user", "content": "只回复两个字：可用"}],
            max_tokens=16,
            temperature=0.0,
        )
        print(f"[chat] OK -> {ans.strip()!r}")
    except Exception as exc:  # noqa: BLE001
        print(f"[chat] FAIL -> {exc!r}")
        traceback.print_exc()
        return 2

    # 2) embedding 最小请求
    try:
        vec = client.embed_query("测试向量维度")
        print(f"[embed] OK -> dim={len(vec)}")
    except Exception as exc:  # noqa: BLE001
        print(f"[embed] FAIL -> {exc!r}")
        traceback.print_exc()
        return 3

    print("[done] 新 Key 对 chat 与 embedding 均验证通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
