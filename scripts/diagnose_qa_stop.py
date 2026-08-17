"""诊断 script_dm_qa 不再写入 + chunk 过短问题。

查三件事：
1. 最近 job 运行状态（QA 生成/嵌入阶段有没有失败）
2. 各文档 chunks / qa 写入量（force 重跑是否执行、QA 是否为 0）
3. 最新文档 chunk 长度分布（是否真的过短）
"""

from pathlib import Path
import httpx

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"

def load_env():
    env = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env

env = load_env()
SUPABASE_URL = env.get("SUPABASE_URL", "").rstrip("/")
SERVICE_KEY = env.get("SUPABASE_SERVICE_ROLE_KEY", "")
REST_URL = f"{SUPABASE_URL}/rest/v1"
HEADERS = {
    "apikey": SERVICE_KEY,
    "Authorization": f"Bearer {SERVICE_KEY}",
    "Content-Type": "application/json",
    "Accept": "application/json",
}

def fetch(path, params=None):
    url = f"{REST_URL}/{path}"
    resp = httpx.get(url, headers=HEADERS, params=params, timeout=60)
    if resp.status_code >= 400:
        print(f"  ERROR {resp.status_code}: {resp.text[:300]}")
        return []
    data = resp.json()
    return data if isinstance(data, list) else [data]

def main():
    print("=" * 72)
    print("[1] 最近 15 条 job 运行状态")
    print("=" * 72)
    jobs = fetch("script_dm_jobs", params={
        "select": "id,script_id,script_code,status,stage_detail,total_chunks,total_qa,error_message,created_at,updated_at",
        "order": "created_at.desc",
        "limit": "15",
    })
    for j in jobs:
        print(f"  job={j['id'][:8]} code={j.get('script_code','')[:28]:28s} "
              f"status={j.get('status',''):12s} chunks={j.get('total_chunks')} qa={j.get('total_qa')} "
              f"detail={str(j.get('stage_detail') or '')[:40]}")
        err = j.get("error_message")
        if err:
            print(f"    !! error: {str(err)[:160]}")
    print(f"  （共 {len(jobs)} 条）")

    print()
    print("=" * 72)
    print("[2] 各文档 chunks / qa 写入量（按创建时间倒序）")
    print("=" * 72)
    docs = fetch("script_dm_documents", params={
        "select": "id,script_code,file_name,object_key,total_pages,total_chunks,total_qa,is_active,created_at",
        "order": "created_at.desc",
        "limit": "15",
    })
    for d in docs:
        print(f"  doc={d['id'][:8]} code={d.get('script_code','')[:24]:24s} "
              f"file={str(d.get('file_name',''))[:28]:28s} "
              f"pages={d['total_pages']} chunks={d['total_chunks']} qa={d['total_qa']} "
              f"active={d['is_active']} created={str(d.get('created_at',''))[:19]}")

    # 最新文档 chunk 长度分布
    if docs:
        doc = docs[0]
        doc_id = doc["id"]
        print()
        print("=" * 72)
        print(f"[3] 最新文档 {doc_id[:8]} ({doc.get('file_name','')}) chunk 长度分布")
        print("=" * 72)
        chunks = fetch("script_dm_chunks", params={
            "select": "char_count,chunk_index,section_path,block_type",
            "document_id": f"eq.{doc_id}",
            "order": "chunk_index.asc",
            "limit": "500",
        })
        if chunks:
            lens = sorted(c.get("char_count") or 0 for c in chunks)
            n = len(lens)
            avg = sum(lens) / n
            p50 = lens[n // 2]
            p90 = lens[int(n * 0.9)]
            short = sum(1 for x in lens if x < 100)
            tiny = sum(1 for x in lens if x < 40)
            print(f"  chunks={n} min={lens[0]} p50={p50} avg={avg:.0f} p90={p90} max={lens[-1]}")
            print(f"  <40 字: {tiny} ({tiny*100//n}%)  <100 字: {short} ({short*100//n}%)")
            print()
            print("  最短 8 条：")
            for c in chunks[:8]:
                sp = c.get("section_path") or []
                print(f"    chunk[{c['chunk_index']}] len={c.get('char_count')} "
                      f"type={c.get('block_type')} path={' > '.join(sp) if sp else '(空)'}")
            print()
            print("  空 section_path 占比：")
            empty = sum(1 for c in chunks if not c.get("section_path"))
            print(f"    {empty}/{n} = {empty*100//n}%")
        else:
            print("  无 chunk 数据")

        print()
        print("=" * 72)
        print(f"[4] 最新文档 QA 写入量")
        print("=" * 72)
        qa_rows = fetch("script_dm_qa", params={
            "select": "id,question,title,created_at",
            "document_id": f"eq.{doc_id}",
            "limit": "10",
            "order": "created_at.desc",
        })
        print(f"  QA 条数(limit 10 抽样): {len(qa_rows)}")
        for q in qa_rows[:5]:
            print(f"    title='{q.get('title','')}' q={str(q.get('question',''))[:50]} created={str(q.get('created_at',''))[:19]}")

if __name__ == "__main__":
    main()
