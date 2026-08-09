"""探针：定位 SiliconFlow bge-large-zh-v1.5 的 embedding 输入长度上限。"""
import sys
import httpx

from app.core.config import get_settings


def main() -> int:
    s = get_settings()
    headers = {
        "Authorization": f"Bearer {s.siliconflow_api_key}",
        "Content-Type": "application/json",
    }
    base = s.siliconflow_base_url.rstrip("/")

    # 构造纯中文测试串（每个长度一截），探测 400 出现点
    lengths = [200, 400, 512, 600, 800, 1000, 1200, 1500, 2000]
    unit = "测"  # 单字，确定性长度
    for n in lengths:
        text = unit * n
        payload = {
            "model": s.siliconflow_embed_model,
            "input": [text],
            "encoding_format": "float",
        }
        try:
            resp = httpx.post(f"{base}/embeddings", json=payload, headers=headers, timeout=30)
            ok = resp.status_code == 200
            msg = resp.json().get("message") if not ok else f"dim={len(resp.json()['data'][0]['embedding'])}"
            print(f"len={n:5d} -> {resp.status_code} {msg}")
            if not ok:
                print(f"  >>> 首次 400 出现在约 {n} 字，停止")
                break
        except Exception as exc:  # noqa: BLE001
            print(f"len={n:5d} -> EXC {exc!r}")
            break
    return 0


if __name__ == "__main__":
    sys.exit(main())
