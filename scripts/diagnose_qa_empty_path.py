"""诊断 script_dm_qa 的 section_path / title 为空的原因。

排查逻辑：
1. 查 QA 表：section_path 和 title 为空的比例
2. 查 chunks 表：section_path 是否也为空（如果 chunks 也空 => 标题检测失败）
3. 查 documents 表：看处理的是 PDF 还是 docx
4. 如果 chunks 有 section_path 但 QA 没有 => 传递链路 bug
5. 如果 chunks 也空 => build_section_paths 没有检测到任何标题块
"""

import json
import os
import sys

import httpx

# 从 .env 读取配置
from pathlib import Path

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
    resp = httpx.get(url, headers=HEADERS, params=params, timeout=30)
    if resp.status_code >= 400:
        print(f"  ERROR {resp.status_code}: {resp.text[:300]}")
        return []
    data = resp.json()
    return data if isinstance(data, list) else [data]


def main():
    print("=" * 70)
    print("诊断：script_dm_qa section_path / title 为空的原因")
    print("=" * 70)

    # 1. 查所有文档
    print("\n[1] 文档列表")
    docs = fetch("script_dm_documents", params={
        "select": "id,script_id,script_code,file_name,object_key,total_pages,total_chunks,total_qa,is_active,created_at",
        "order": "created_at.desc",
        "limit": "20",
    })
    for d in docs:
        ext = d.get("object_key", "").rsplit(".", 1)[-1] if "." in d.get("object_key", "") else "?"
        print(f"  doc={d['id'][:8]} code={d.get('script_code','')} ext={ext} "
              f"file={d.get('file_name','')[:30]} "
              f"pages={d['total_pages']} chunks={d['total_chunks']} qa={d['total_qa']} "
              f"active={d['is_active']}")

    if not docs:
        print("  没有文档记录，流水线从未跑过。")
        return

    # 取第一个（最新）文档
    doc = docs[0]
    doc_id = doc["id"]
    print(f"\n  -> 诊断最新文档: {doc_id[:8]} (file_name={doc.get('file_name','')})")

    # 2. 查 chunks 的 section_path
    print(f"\n[2] chunks 表 section_path 状态")
    chunks = fetch("script_dm_chunks", params={
        "select": "id,chunk_index,section_path,block_type,content",
        "document_id": f"eq.{doc_id}",
        "order": "chunk_index.asc",
        "limit": "10",
    })
    total_chunks_checked = len(chunks)
    empty_path_chunks = sum(1 for c in chunks if not c.get("section_path"))
    print(f"  抽样 {total_chunks_checked} 条 chunk:")
    print(f"  section_path 为空: {empty_path_chunks}/{total_chunks_checked}")
    for c in chunks[:5]:
        sp = c.get("section_path", [])
        preview = (c.get("content") or "")[:60].replace("\n", " ")
        print(f"    chunk[{c['chunk_index']}] type={c['block_type']} path={sp} content={preview}...")

    # 3. 查 QA 的 section_path 和 title
    print(f"\n[3] QA 表 section_path / title 状态")
    qa_rows = fetch("script_dm_qa", params={
        "select": "id,question,title,section_path,category,page_start",
        "document_id": f"eq.{doc_id}",
        "order": "created_at.asc",
        "limit": "10",
    })
    total_qa_checked = len(qa_rows)
    empty_path_qa = sum(1 for q in qa_rows if not q.get("section_path"))
    empty_title_qa = sum(1 for q in qa_rows if not q.get("title"))
    print(f"  抽样 {total_qa_checked} 条 QA:")
    print(f"  section_path 为空: {empty_path_qa}/{total_qa_checked}")
    print(f"  title 为空: {empty_title_qa}/{total_qa_checked}")
    for q in qa_rows[:5]:
        sp = q.get("section_path", [])
        title = q.get("title", "")
        question = (q.get("question") or "")[:50]
        print(f"    title='{title}' path={sp} q={question}...")

    # 4. 统计全量
    print(f"\n[4] 全量统计（该文档）")
    # chunks 全量 section_path 统计
    all_chunks = fetch("script_dm_chunks", params={
        "select": "section_path",
        "document_id": f"eq.{doc_id}",
    })
    chunks_total = len(all_chunks)
    chunks_empty = sum(1 for c in all_chunks if not c.get("section_path"))
    chunks_has_path = chunks_total - chunks_empty
    print(f"  chunks: {chunks_total} 条, section_path 非空: {chunks_has_path}, 为空: {chunks_empty}")

    # QA 全量统计
    all_qa = fetch("script_dm_qa", params={
        "select": "section_path,title",
        "document_id": f"eq.{doc_id}",
    })
    qa_total = len(all_qa)
    qa_empty_path = sum(1 for q in all_qa if not q.get("section_path"))
    qa_empty_title = sum(1 for q in all_qa if not q.get("title"))
    print(f"  qa:     {qa_total} 条, section_path 非空: {qa_total - qa_empty_path}, 为空: {qa_empty_path}")
    print(f"  qa:     {qa_total} 条, title 非空:      {qa_total - qa_empty_title}, 为空: {qa_empty_title}")

    # 5. 诊断结论
    print(f"\n[5] 诊断结论")
    if chunks_empty == chunks_total and chunks_total > 0:
        print("  ⚠ chunks 的 section_path 全部为空 => 标题检测完全失败")
        print("  根因: build_section_paths() 没有检测到任何 heading 块")
        ext = doc.get("object_key", "").rsplit(".", 1)[-1] if "." in doc.get("object_key", "") else ""
        if ext.lower() in ("docx", "doc"):
            print(f"  文件格式: Word({ext})")
            print("  Word 文档不使用内置标题样式(Heading 1/标题 1)时，")
            print("  doc_extract.py 无法检测标题，且 font_size=0 导致 calibrate_headings 跳过")
        elif ext.lower() == "pdf":
            print(f"  文件格式: PDF")
            print("  PDF 的字号分布可能过于均匀，classify_block 未识别出标题")
            print("  或标题文本被 strip_noise 当作页眉页脚剥离")
    elif chunks_empty == 0 and qa_empty_path == qa_total and qa_total > 0:
        print("  ⚠ chunks 有 section_path 但 QA 全部为空 => 传递链路 bug")
        print("  需检查 embed_and_store 中 source.get('section_path') 的取值")
    elif chunks_empty < chunks_total:
        print(f"  ⚠ chunks 部分为空({chunks_empty}/{chunks_total}) => 部分标题检测失败")
    else:
        print("  数据看起来正常，需进一步排查")

    print()


if __name__ == "__main__":
    main()
