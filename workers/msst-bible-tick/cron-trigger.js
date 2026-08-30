// msst-bible-tick
//
// A scheduled Cloudflare Worker that reliably triggers the GitHub Actions
// deploy workflow. GitHub's own `schedule` events are best-effort and can be
// delayed or dropped for hours; Cloudflare's cron triggers are dependable.
// This Worker just calls the GitHub Actions API (workflow_dispatch) on a
// Cloudflare cron, and the existing deploy.yml does the actual build+deploy.
//
// Requires two bindings:
//   DISPATCH_URL (var)      — the workflow_dispatch endpoint (see wrangler.toml)
//   GITHUB_TOKEN (secret)   — fine-grained PAT with "Actions: write" on the repo
//
export default {
  async scheduled(event, env, ctx) {
    const resp = await fetch(env.DISPATCH_URL, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.GITHUB_TOKEN}`,
        Accept: "application/vnd.github+json",
        "Content-Type": "application/json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "msst-bible-tick",
      },
      body: JSON.stringify({ ref: "main" }),
    });
    if (!resp.ok) {
      const text = await resp.text();
      console.error(`dispatch failed (${resp.status}): ${text}`);
      throw new Error(`dispatch failed: ${resp.status}`);
    }
    console.log(`dispatched deploy on ${env.DISPATCH_URL}`);
  },

  // Lets you fire a dispatch manually without waiting for the cron:
  //   curl https://<worker>.workers.dev/trigger
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (request.method === "GET" && url.pathname === "/trigger") {
      await this.scheduled(new Date(), env, ctx);
      return new Response("dispatching deploy…", { status: 202 });
    }
    return new Response(
      "msst-bible-tick worker. Cron triggers deploys; GET /trigger to dispatch now.",
      { status: 200 }
    );
  },
};