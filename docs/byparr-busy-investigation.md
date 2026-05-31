# Investigation prompt: "Browser is busy processing another request"

Paste this into an agent running **inside the byparr repository** (image
`ghcr.io/xplux/byparr:main`, the Camoufox/Firefox-based FlareSolverr-compatible
solver). It does NOT have access to our caller code, so everything it needs to
know is described below.

---

## Your task

Find out **why a byparr container keeps answering
`{"detail":"Browser is busy processing another request"}`** for an extended
period, and whether `maxTimeout` is supposed to abort the in-flight navigation
and free the browser. Then propose/implement a fix so a stuck or still-running
request cannot block all subsequent requests indefinitely.

Write your findings and any code changes back into the byparr repo. Do **not**
assume — verify against the actual source.

## How byparr is being called (from our side)

- One byparr **container per browser**, each on its own port (`-p 82xx:8191`),
  started with `docker run -d --restart unless-stopped --init ... ghcr.io/xplux/byparr:main`.
- Endpoint: `POST http://<host>:<port>/v1`
- Body:
  ```json
  {
    "cmd": "request.get",
    "url": "<target>",
    "maxTimeout": 25000,            // = our per-browser timeout (s) * 1000
    "cookies": [ ... ]              // optional
  }
  ```
- Our HTTP client (curl) waits `timeout + 30` seconds (e.g. 55s) before it
  gives up on its side. So byparr has ~25s (`maxTimeout`) of intended budget but
  the socket stays open up to 55s.
- We send **at most one request at a time per container**. We never deliberately
  send a second concurrent request. The next request to the same container is
  only sent **after** the previous one returned (success or failure) **plus a
  cooldown** (a few seconds).

## Observed symptom (the thing to explain)

1. A request for a hard target (Cloudflare-protected page) is sent.
2. It sometimes exceeds `maxTimeout`; our client eventually times out / the
   response comes back as an error.
3. **Every subsequent request to that same container then returns
   `"Browser is busy processing another request"`** — repeatedly, for a long
   time (observed in bursts spaced by our retry cadence), until the container is
   restarted or eventually unwedges on its own.
4. A cold `request.get` to `https://example.com` (trivial site) takes ~7s, which
   suggests the browser is cold-started per request.

## Questions to answer from the byparr source

1. **Concurrency model:** Does byparr serialize requests behind a single shared
   browser/page and reject overlapping ones with the "busy" message? Where is
   that lock/guard implemented? (Search for the "busy processing another
   request" string and trace what sets/clears the busy flag.)

2. **Does `maxTimeout` actually abort the navigation?** When the timeout fires,
   does byparr:
   - cancel the running Playwright/Camoufox task and release the lock, or
   - just return a timeout response to the client while the navigation keeps
     running in the background (leaving the browser "busy")?
   This is the crux. If the lock is held by a background task that outlives the
   HTTP response, that explains the persistent "busy".

3. **Client disconnect handling:** If the HTTP client disconnects (our curl
   timeout at 55s) before byparr finishes, is the in-flight task cancelled and
   the lock released, or does it leak?

4. **Recovery:** Is there any watchdog that force-closes a hung page/context and
   resets the busy flag? If not, the browser can stay wedged forever.

5. **Is there an internal request queue** or is it immediate-reject-on-busy?
   What would it take to make byparr either (a) queue the next request, or
   (b) reliably free the browser on timeout/disconnect?

## Deliverables

- A clear explanation of whether `maxTimeout` releases the browser (yes/no, with
  the exact code path).
- The root cause of the persistent "busy" state.
- A concrete fix in the byparr codebase, e.g. one or more of:
  - wrap the solve in a hard timeout that **always** cancels the task and
    releases the lock in a `finally`,
  - add a watchdog that recreates the browser context if a request runs longer
    than `maxTimeout + margin`,
  - release the lock on client disconnect,
  - optionally expose a `/health` (or similar) that reports real busy/idle state
    so callers can detect a wedged container (the Docker `healthcheck` currently
    reports "healthy" even when wedged).

## Notes / constraints

- Keep the existing `/v1` request/response contract intact (we depend on
  `status`, `solution.response`).
- The container runs with `--init` (tini reaps zombie Firefox subprocesses), so
  process-reaping is already handled at the container level — focus on the
  application-level lock/timeout logic, not zombies.
