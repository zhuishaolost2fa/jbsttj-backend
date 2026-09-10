/**
 * Supabase Send Email Hook → 自建后端的中转函数。
 *
 * 为什么需要它：Supabase 的 Auth Hook 只接受 HTTPS 端点，而我们的服务器
 * （腾讯云轻量、域名未备案）在**国际链路**上会被 DNSPod 的未备案拦截页
 * 重定向（Let's Encrypt 境外验证实测被拦到 dnspod.qcloud.com/static/webblock.html），
 * 国内访问却正常。所以不能把 hook 直接指向我们自己的域名。
 *
 * 本函数由 Supabase 托管，自带 HTTPS，因此：
 *   GoTrue --HTTPS--> Edge Function(本文件) --HTTP--> http://<IP>/api/v1/hooks/supabase/send-email
 * 最后一段用纯 IP，绕开「未备案域名」拦截（拦截按 Host 头识别域名，IP 不受影响）。
 *
 * 签名校验仍在后端做：这里只原样透传 body 和 webhook-* 头，不做任何判断，
 * 避免在两处重复实现 Standard Webhooks 验签。
 */

const UPSTREAM =
  Deno.env.get("UPSTREAM_URL") ?? "http://101.34.58.31/api/v1/hooks/supabase/send-email";

const FORWARD_HEADERS = ["webhook-id", "webhook-timestamp", "webhook-signature"];

Deno.serve(async (req: Request): Promise<Response> => {
  if (req.method !== "POST") {
    return new Response("method not allowed", { status: 405 });
  }

  const headers = new Headers({ "content-type": "application/json" });
  for (const name of FORWARD_HEADERS) {
    const value = req.headers.get(name);
    if (value) headers.set(name, value);
  }

  const body = await req.text();

  try {
    const upstream = await fetch(UPSTREAM, { method: "POST", headers, body });
    const text = await upstream.text();
    console.log(`relay -> upstream ${upstream.status}`);
    return new Response(text, {
      status: upstream.status,
      headers: { "content-type": "application/json" },
    });
  } catch (err) {
    // 必须返回非 2xx：GoTrue 靠状态码决定是否重试，静默吞掉会导致邮件丢失
    console.error("relay failed:", err);
    return new Response(JSON.stringify({ error: "relay_failed" }), {
      status: 502,
      headers: { "content-type": "application/json" },
    });
  }
});
