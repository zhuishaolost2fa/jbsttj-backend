# 邮箱验证码链路：前端对接说明

> 适用范围：H5 / 小程序任意端。后端已完成，本文只讲「你该怎么接」。
> 最后更新：2026-09-10（链路已端到端验证通过）

## 1. 现在的链路长什么样

```
用户注册
  │ POST /api/v1/auth/register
  ▼
GoTrue 生成 8 位验证码
  │ Postgres Hook → auth_email_outbox 表
  ▼
后端轮询（3s）→ 腾讯云 SES → 用户邮箱        ← 邮件里只有验证码，没有链接
  ▼
用户在页面输入验证码
  │ POST /api/v1/auth/verify-email
  ▼
返回 access_token / refresh_token → 登录态就绪
```

要点：**邮件里没有可点链接**，所以注册流程必须多一个「输入验证码」步骤。
这是腾讯云模板审核规则导致的（链接域名不能做成变量），不是设计选择。

## 2. 接口

### `POST /api/v1/auth/register`

```jsonc
// request
{ "email": "user@example.com", "password": "至少6位" }

// response 200 —— 注意 token 是空串
{ "access_token": "", "refresh_token": null, "user": null }
```

**`access_token` 为空 = 正常**，代表「邮件已发，等验证」。前端据此跳验证码页，
不要用 `!response.access_token` 判失败。

可能报错：

| HTTP | code | 处理 |
|---|---|---|
| 422 | `email_exists` 之类 | GoTrue 提示邮箱已注册，走登录或找回密码 |
| 429 | `auth_email_rate_limited` | 见下方 `details.hint` 分支 |

429 的 body：

```jsonc
{ "error": {
    "code": "auth_email_rate_limited",
    "message": "邮件发送额度已用尽，请稍后再试",
    "details": { "hint": "smtp_quota_exhausted", "retry_after": 1800 } } }
```

`hint` 两种取值：

- `cooldown_60s` —— 同一邮箱 60 秒才能发一次，倒计时提示即可
- `smtp_quota_exhausted` —— 配额耗尽，按 `retry_after` 显示分钟数

### `POST /api/v1/auth/verify-email`

```jsonc
// request
{ "email": "user@example.com", "code": "12345678", "type": "signup" }

// response 200
{ "access_token": "eyJhbGciOi...", "refresh_token": "xxx",
  "token_type": "bearer", "expires_in": 3600,
  "user": { "id": "...", "email": "...", "email_verified": true } }
```

- `code`：**8 位数字**（本项目 `mailer_otp_length=8`，不是常见的 6 位）。
  输入框按 8 位做，别硬编码 6。
- `type` 取值固定为：`signup` / `recovery` / `magiclink` / `email_change` / `email`。
  传别的会 422。
- **验证码一次有效**。重复提交返回 422（前端不要再自动重试，让用户重新获取）。
- 成功后直接把 `access_token` 落存储，和登录返回的 token 用法完全一致。

## 3. 页面建议

注册页 → 验证码页（承载邮箱地址）→ 首页

验证码页要素：

- 8 位数字输入（可拆成 8 个格子提升观感，但提交时拼成一个字符串）
- 「重新获取验证码」按钮：**60 秒倒计时**后才可点，点了再次调 `/register`
  （对已注册未验证的用户，GoTrue 会重发邮件，这是预期行为）
- 错误提示统一走「验证码错误或已过期」文案，不区分具体原因

## 4. 已知边界

| 项 | 说明 |
|---|---|
| 邮件延迟 | 约 3 秒（轮询间隔），短时可接受 |
| 发送配额 | 已调到 100 封/小时（`rate_limit_email_sent`），腾讯侧还有自有额度 |
| 验证码有效期 | GoTrue 默认控制，过期走重新获取 |
| Access token 遗失 | 重新走注册即可，GoTrue 对未验证用户会重发并覆盖旧码 |

## 5. 排查入口

- 服务端日志：`docker logs jbs-api --tail 100 | grep -i outbox`
- 发件箱表：`select id, created_at, sent_at, error from public.auth_email_outbox order by id desc limit 10`
  — `sent_at` 有值就是发出去了；`error` 有值是投递失败
- 已投递的记录里 `token` 会被抹成 `***redacted***`（安全设计），查不到验证码是正常的
