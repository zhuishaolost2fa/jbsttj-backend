# DM 手册「ask 问答」前端对接方案

> 适用接口：`POST /api/v1/dm-guide/ask`（本次新增的**扁平接口**）
> 配套接口：`GET /api/v1/scripts/{code}/dm-guide`（查询索引状态）、
> `GET /api/v1/scripts/{code}/import-status`（上传→解析→可问答整体进度）
> 旧接口仍保留：`POST /api/v1/scripts/{id_or_code}/dm-guide/ask`（路径式，向后兼容）

---

## 1. 这次改了什么

原来的 `ask` 接口把剧本标识放在 **URL 路径**里
（`/scripts/{id_or_code}/dm-guide/ask`），前端必须先拿到 `scriptId` 或 `code`、
再拼出路径才能调用。

现在新增一个**扁平接口**：剧本标识和询问都放在**请求体**里，前端只要手上有
剧本的 `code`（列表页、详情页、带本页到处都能拿到），直接 `POST` 即可，
不必再维护「code → 路径」的映射。

后端逻辑：先用 `code` 解析出剧本，再用「询问」**向量化**后在该剧本手册内做**向量检索**。
**默认（不传 `useLlm`）不调用大模型**，直接把向量最近命中的答案文本（qa 答案或原文块）
透出为 `answer`，端到端只花一次 embedding + 向量查询，几十毫秒级、零 LLM 额度——
适合带本时高频即时查规则。需要 LLM 把命中内容**合成**成一条带引用出处的答案时，
把 `useLlm` 设为 `true`（会消耗 LLM 额度）。检索范围被 `code` 严格限定在该剧本手册内，
不会跨剧本串味。

---

## 2. 接口契约

### 2.1 请求

`POST /api/v1/dm-guide/ask`

请求头：

| Header | 必填 | 说明 |
| --- | --- | --- |
| `Authorization` | 是 | `Bearer <supabase_access_token>`（与上传接口同一套鉴权） |
| `Content-Type` | 是 | `application/json` |

请求体（字段名用 camelCase 或 snake_case 都行，后端 pydantic 自动转换）：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `code` | string | **是** | 剧本业务编码（code）或 UUID，界定检索范围 |
| `询问` | string | **是** | 自然语言问题（中文键名，对前端更顺手）。也可写作 `question` |
| `question` | string | 否 | 与 `询问` 二选一，二者都传以 `询问` 为准时取非空值 |
| `mode` | string | 否 | 检索模式：`chunk` / `qa` / `hybrid`（默认 `hybrid`） |
| `topK` | integer | 否 | 参与回答的参考条数上限，1~30（默认 6） |
| `minSimilarity` | number | 否 | 相似度下限 0~1，不传用服务端默认（中文语义检索低于 0.25 基本是噪声） |
| `category` | string | 否 | 只看某一类问答（仅 `qa`/`hybrid` 生效）：`rule`/`clue`/`character`/`timeline`/`host_tip`/`general` |
| `useLlm` | boolean | 否 | 是否用 LLM 合成答案。**默认 `false`**：直接返回向量最近命中的答案文本，速度最优、零额度；`true` 时调用 LLM 生成带引用出处的答案 |

> 注意：`询问` 与 `question` 的校验是一致的——都会被 `trim` 且不能为空；
> 空串会返回 422。

**示例请求体：**

```json
{
  "code": "xiaochikuaican",
  "询问": "搜证阶段每人能搜几次？",
  "mode": "hybrid",
  "topK": 6
}
```

> 速度最优、不消耗 LLM 额度：以上请求（`useLlm` 缺省为 `false`）直接返回向量最近命中，
> 不经过大模型。只有在确需 LLM 润色合成时，才显式加上 `"useLlm": true`。

### 2.2 响应

`200 OK`，`application/json`：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `question` | string | 回显的原始问题 |
| `answer` | string | 默认是**向量最近命中的答案文本**（qa 答案或原文块，不调 LLM，速度最快）；仅当 `useLlm=true` 时才是 LLM 合成的、带引用出处的答案 |
| `sources` | array | 引用来源（qa 优先），每条含 `type` / `similarity` / `sectionPath` / `pageStart` / `pageEnd` 等 |
| `mode` | string | 实际使用的检索模式 |
| `documentId` | string \| null | 命中的文档 ID |
| `tookMs` | integer | 端到端耗时（毫秒，含向量化 + 检索；仅 `useLlm=true` 时还含 LLM 生成） |

**示例响应：**

```json
{
  "question": "搜证阶段每人能搜几次？",
  "answer": "每位玩家在搜证阶段最多搜索 3 次，每次限翻找 2 件物证。（见参考2，P12）",
  "sources": [
    {
      "type": "qa",
      "similarity": 0.86,
      "question": "搜证阶段每人能搜几次？",
      "answer": "每位玩家在搜证阶段最多搜索 3 次……",
      "sectionPath": ["第三章 案件还原", "3.2 关键物证"],
      "pageStart": 12,
      "pageEnd": 12
    }
  ],
  "mode": "hybrid",
  "documentId": "9f1c...",
  "tookMs": 1342
}
```

### 2.3 错误码

| HTTP | `code` | 触发场景 | 前端建议处理 |
| --- | --- | --- | --- |
| 401 | — | 未登录 / token 失效 | 跳转登录 |
| 409 | `dm_not_indexed` | 该剧本手册尚未完成索引 | 引导用户等待解析（见 §3），不要重试询问 |
| 422 | `code_required` | 请求体缺 `code` | 前端参数校验拦截 |
| 422 | (pydantic) | `询问` 为空 / 超长 | 前端输入校验拦截 |
| 404 | `script_not_found` | `code` 对应的剧本不存在 | 提示 code 错误 |
| 409 | `dm_dispatch_failed` | 消息队列不可用（罕见） | 稍后重试 |

---

## 3. 前置：先确认「可问答」

`ask` 只有在剧本手册**已完成向量索引**（`indexed=true`）时才有意义。前端有两种姿势：

1. **详情页 / 带本页常驻**：进入页面时调一次
   `GET /api/v1/scripts/{code}/dm-guide`，
   看 `indexed` 字段。为 `false` 时把问答框禁用并显示「手册解析中…」。
2. **刚上传完手册**：调
   `GET /api/v1/scripts/{code}/import-status`，
   按其 `overallStatus` 展示三阶段进度（`uploading` / `parsing` / `ready`），
   到达 `ready` 后再开放问答。建议轮询间隔 3~5 秒，
   **不要**用单一百分比展示（解析各阶段耗时差两个数量级）。

---

## 4. 前端调用示例

下面直接复用现有演示页（`frontend/index.html`）里的 `api()` 封装，
只需新增一个「问答」面板即可：

```js
// code 与 询问 都来自页面状态（如剧本详情页已持有 script.code）
async function askDmGuide(code, question) {
  const body = { code, 询问: question, mode: 'hybrid', topK: 6 };
  try {
    const res = await api('POST', '/dm-guide/ask', body);
    renderAnswer(res);            // 渲染 answer
    renderSources(res.sources);    // 渲染来源（章节 + 页码 + 相似度）
  } catch (err) {
    if (err.code === 'dm_not_indexed') {
      alert('该剧本手册还在解析中，暂不能问答');
    } else if (err.status === 401) {
      // 重新登录
    } else {
      alert('问答失败：' + err.message);
    }
  }
}

function renderSources(sources = []) {
  return sources.map(s => {
    const where = [s.sectionPath?.join(' > '), s.pageStart ? `P${s.pageStart}` : '']
      .filter(Boolean).join(' ');
    const text = s.type === 'qa' ? s.answer : s.content;
    return `<li><span class="tag">${s.type}</span> ${text}
            <span class="muted">${where} · 相似度 ${(s.similarity*100).toFixed(0)}%</span></li>`;
  }).join('');
}
```

---

## 5. 交互建议

- **默认直出、按需合成**：默认（`useLlm=false`）走向量直出，速度最快、零 LLM 额度；
  只有在确需 LLM 把多条来源润色成一句话时才传 `useLlm=true`。防抖逻辑可省，高频即时查规则也扛得住。
- **流式/加载态**：默认直出时 `tookMs` 通常几十毫秒级；`useLlm=true` 时可能 1~3 秒，
  按钮需有 loading 态，禁止重复提交。
- **来源展示**：把 `sources` 折叠在答案下方，点击可展开查看原文出处（章节路径 + 页码），
  增强主持人信任感。
- **降级**：若 `answer` 为空但 `sources` 非空，可直接把相似度最高的 QA 答案兜底展示，
  避免「无答案」的空体验。
- **多剧本隔离**：`code` 已经限定检索范围，前端无需额外处理；但要把 `code` 与当前
  展示的剧本强绑定，避免串台。

---

## 6. 可直接跑的演示

`frontend/dm-ask-demo.html` 是一个零依赖的单文件演示：登录后填入 `code` 与「询问」，
即可调用本接口并渲染答案与来源。把它和现有 `index.html` 放在同一静态目录下，
启动后端后访问 `/demo/dm-ask-demo.html` 即可试用。
