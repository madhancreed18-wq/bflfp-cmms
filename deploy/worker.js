// Cloudflare Worker — fixed front door for BFLFP CMMS
// Paste this into your Worker (Edit code) and deploy.
// It forwards every request to the current tunnel URL stored in KV key "target".
// Bind a KV namespace named TUNNEL (Settings → Bindings → KV) first.

export default {
  async fetch(req, env) {
    const target = await env.TUNNEL.get("target");
    if (!target) {
      return new Response("BFLFP CMMS: tunnel not running (no target set)", { status: 503 });
    }
    const url = new URL(req.url);
    const t = new URL(target);
    url.protocol = "https:";
    url.hostname = t.hostname;
    url.port = "";
    return fetch(url.toString(), {
      method: req.method,
      headers: req.headers,
      body: ["GET", "HEAD"].includes(req.method) ? undefined : req.body,
      redirect: "manual",
    });
  },
};
