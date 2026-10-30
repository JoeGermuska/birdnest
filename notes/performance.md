# Performance and crawlers

## What happened (2026-10-28)

Pages took 2-9 s, even `/robots.txt`. The Fly machine is one shared CPU, 512 MB, gunicorn with 1 worker and 8 threads, so requests were queueing behind crawler traffic (~7-15 requests a second):

- Something walked every combination of `/shows?sort=&dj=&dj=&family=`, with rotating browser user agents and a home-page Referer. Who it is isn't known: the access log's client address is Fly's own proxy (the app's dedicated IPs, 188.93.144.145 and 2a09:8280:1::a751), not the visitor. It was first misread as two crawler addresses.
- `meta-externalagent` fetched `/radio?seed=...` and `/spotify/login?next=...` (both disallowed in robots.txt; it ignores it). Amazon's `Amzn-SearchBot` did some of the same.

After the fixes below, `/shows` takes about 3 ms and a refused bot about 0.6 ms; the site is back to ~0.2-0.3 s.

## In the app

- `block_bots` (app.py): 403 for `BLOCKED_BOTS` user agents (not `/robots.txt`, so they can still read it). Not `facebookexternalhit`: that's Facebook's link-preview fetcher, and blocking it breaks the preview card when someone shares a link (it was blocked by mistake for a day; a test pins it open).
- `/shows` has one URL per view: junk parameters dropped, DJs sorted, anything else 301s to the canonical form. Rendered pages are cached in memory (`_browse_cache`, 500 entries, per "listener logged in" state), emptied by a restart. Shuffle without a seed is never cached.
- `gunicorn.conf.py`: the access log ends with the request time in seconds. `fly logs` shows what a page costs.

- `origin_lock` (app.py): Fly's IPs are public, and the log (`cf=-` lines) showed most of the `/shows` crawl, plus Meta's crawler, connecting to them directly and skipping Cloudflare. With the Fly secret `ORIGIN_SECRET` set, only requests carrying it as `X-Origin-Secret` get through (not `/robots.txt`). Unset (locally, in tests) it does nothing. **Live since 2026-10-28.** Don't unset the secret or delete the Cloudflare rule on its own: with the secret set and no header being added, the whole site 403s; with the header added and no secret, the lock is just off. Setting it up (or rotating it), in this order, or the site 403s for everyone:
  1. Cloudflare: a request header Transform Rule setting `X-Origin-Secret` to a long random value, for all requests (rule ref `origin_secret`). The dashboard didn't offer it, so it was made with the API (https://developers.cloudflare.com/rules/transform/request-header-modification/create-api/): a `PUT` to `/zones/$ZONE_ID/rulesets/phases/http_request_late_transform/entrypoint` with an API token that has Zone > Transform Rules > Edit. That `PUT` replaces every rule in the phase, so read it first.
  2. `fly secrets set ORIGIN_SECRET=<the same value>` (restarts the app). To turn it off: `fly secrets unset ORIGIN_SECRET`. To check: `curl --resolve birdsconferring.com:443:<Fly's IPv4, from fly ips list> https://birdsconferring.com/` is a 403, and the site through Cloudflare is a 200.
- The crawl itself: many hosting-provider addresses (several look like Contabo and Scaleway), each sending a request or two, so a per-IP rate limit never trips. Meta's `meta-externalagent` is genuine (`2a03:2880::`), a separate thing. About 10 pages/s of ~20 KB is roughly 500 GB a month of egress.

## At Cloudflare (the domain is proxied; SSL mode is Automatic)

- Custom rule: Block the same user agents as `BLOCKED_BOTS` (`lower(http.user_agent) contains "..."`).
- Rate limiting rule (free plan: path only, count by IP): `http.request.uri.path in {"/shows" "/radio" "/search"}`, 20 requests per 10 s, Block. `http.user_agent` isn't allowed in rate limiting rules on the free plan.
- Block by network (live since 2026-10-29): a custom rule, Block on `ip.src.asnum eq 51167`. After it went in, no `/shows` request from Contabo got a 200 and the log went from ~10 requests a second to a few a minute. That's Contabo, a cheap server host and the source of every disguised `/shows` request that got a 200 through Cloudflare (40 of 40 sampled, from 15 addresses; found by looking up the `cf=` addresses from `fly logs` with Team Cymru's DNS service). Security > Events only lists blocked requests, so it can't show these; Security > Analytics or the log can.
- What the "declared crawlers" rule caught in its first day (109 events): Amzn-SearchBot from 60 different Amazon addresses (why a per-address rate limit can't catch it), PetalBot (Huawei's search crawler) from Huawei Cloud, Singapore, and ClaudeBot (Anthropic) from Amazon. A `wp-config.php` scanner was stopped by Cloudflare's managed WordPress rule. ClaudeBot is in the block list by choice, not need: remove it from `BLOCKED_BOTS` and the Cloudflare rule to allow it. `PetalBot` and `Bytespider` are blocked the same way.
- Not used: a Managed Challenge on `/shows?dj=` (Turbo loads pages with fetch, so a challenge page would land inside the layout), and an app-side per-IP throttle (Cloudflare does this better).

## If it gets slow again

1. `fly logs`: the last field is seconds per request. Slow with an idle queue = the page; slow everywhere = a crowd.
2. Count by client and path (`awk`/`sort | uniq -c`) to see who it is; Security > Events at Cloudflare shows what its rules caught.
3. The access log's first address is always Fly's proxy. The real client is in the `cf=` field (`CF-Connecting-IP`), or `fly=` if a request bypassed Cloudflare (Fly itself is still reachable directly at its IPs). Security > Events at Cloudflare also shows who is who. An IP rate limit only works if the crawl comes from few addresses; a crawler rotating through many (as this one seems to) needs a different rule.
4. Then: tighten the rate limit, add the user agent to `BLOCKED_BOTS` and the Cloudflare rule, or give the machine more CPU/memory (watch the old OOM at 256 MB; see `fly.toml`).
