# Profile / Projects verification — 2026-09-17

Runtime releases tested: backend `9835d8b`, UI `f7710ba`.
Backend hosted on Render; UI on Cloudflare at `rexy.baememory.com`.

## Automated gates

- Backend: 90 tests passed against a disposable local Postgres database.
- UI: 100 tests passed; TypeScript and Vite production build passed.
- Tests cover ownership, anonymous/device-token rejection, empty accounts,
  authentic counts, model-output citations and Unicode limits, durable results,
  generation failures, cancellation during a model call, quota enforcement and
  deletion invalidation.
- Isolated real-browser checks passed on desktop and mobile with no page errors.
- Two-tab IndexedDB checks rejected stale writes after deletion and logout.

## Production-data checks

For one existing account, independent source SQL matched the saved Profile:
124,779 tool events and 154 active days. Every one of its 63 project-label
groups matched source session counts, active days and first/last dates.
These checks verify imported records, not that every local transcript was
uploaded or that same-named folders are the same repository.

A real Grok 4.3 generation was saved, read through a fresh connection, and
rendered through the live UI. A separate live Regenerate click completed in
10,639 ms. The displayed, copied and downloaded SKILL.md matched the saved
API result exactly. Reloading with API calls blocked preserved both saved
pages and previously opened project files. Page loads did not trigger Grok.

## Timing measurements

Measured on one development Mac using isolated headless Chrome, not a mobile
CPU simulation or a Core Web Vitals audit. These are samples, not an SLA.

- Cached Profile/Projects navigation: 30 samples, range 16.9–41.6 ms, including
  DOM availability and the following animation frame.
- Live authenticated HTTP reads: Profile 126–679 ms, Projects 145–288 ms.
- IndexedDB 24 KB reads: median 0.2–0.3 ms, p95 0.3–0.4 ms.
- Stored-snapshot SQL examples: 2.5–10.1 ms, excluding network/auth/serialization.
- Initial background rebuild observed near 29.9 seconds for the large account;
  unchanged-source checks skip recomputation. This is not on the GET path.

Cached navigation met the 100 ms goal in these runs. First visits, expired
sessions, absent snapshots, cold Render instances and unavailable networks do
not have a sub-100-ms guarantee. The browser cache expires after 24 hours.
