# Optional Grok enrichment

The portable skill works without Rexy, a provider key, or a model API call. Give
the coding assistant the conversation and feedback, then invoke
`$rexy-session-coach`. Grok is an optional second review pass, not an authority.

## Backend capability and current boundary

Rexy's `backend.session_coach` module accepts a bounded evidence packet and calls
Grok using the backend's existing `XAI_API_KEY` and `XAI_MODEL` (default
`grok-4.3`). The key never belongs in the skill, Linus, browser, packet, or export.
Calling it sends selected text to xAI; it is not local inference.

The module is an operator/library entry point, **not yet a dashboard route or
button**. Do not invent an HTTP endpoint or use an ingestion-only device token
to read session content. Dashboard integration must check session ownership
before loading evidence, then add server-side quotas and revision-scoped caching.
Cache keys must include owner, session, revision, feedback, evidence digest,
model, and prompt version; source deletions must invalidate stored reviews.

The backend operator can run:

```sh
python -m backend.session_coach --input packet.json --output review.json
```

This makes one bounded request, with no automatic retries or background backfill.
The command refuses to overwrite an existing output. Do not run it on the end
user's CLI with a copied backend key. To create a human handoff from a validated
result, backend code can call `render_handoff(review)`.

## Evidence packet

```json
{
  "schema_version": 1,
  "session_id": "example-session",
  "input_revision": "opaque-revision",
  "goal": "Make Google sign-in work end to end",
  "feedback": "Please check the actual flow, not only whether it builds.",
  "coverage": "selected_excerpt",
  "events": [
    {"id":"e1","kind":"tool","text":"npm run build","tool_name":"Bash","status":"succeeded","truncated":false},
    {"id":"e2","kind":"agent","text":"Login is fixed; the build passes.","tool_name":null,"status":"unknown","truncated":false},
    {"id":"e3","kind":"user","text":"I still get an error after clicking Google sign-in.","tool_name":null,"status":"unknown","truncated":false}
  ]
}
```

Use stable event IDs, chronological order, and one session per packet. Include
the request, claimed outcome, relevant tool results, and any correction—not
just the correction. `complete` is only appropriate when all relevant session
evidence is available without omissions. Otherwise use `selected_excerpt` or
`partial_import`; mark individual truncated previews too.

Limits: 60 events, 1,200 characters per event, and 24 KB for the serialized packet.
Oversized packets are rejected rather than silently chopped. Select a smaller
coherent exchange and label its coverage. Exclude thinking blocks, credentials,
irrelevant stdout, and full files/diffs. Pattern-based redaction is defense in
depth, not a guarantee that arbitrary text contains no secrets.

## Output and validation

The returned review has the original session/revision, an evidence digest,
model/prompt version, the selected redacted evidence, and at most three findings.
Each finding has `kind`, `basis` (observed/inferred), `confidence`, `observation`,
`suggestion`, `verify`, and `evidence` citations with an event ID and exact quote.
No supported problem is a valid result: `findings: []`.

The backend rejects unknown event IDs, invented quotes, suspicious secrets,
overlong output, and malformed responses. Valid citations do **not** prove a
model's interpretation: the assistant/user must review the reasoning. Suggestions
are proposals, not executable commands or permission to edit the repository.

Generated handoffs are not installed skills. Keep the reusable instructions in
`SKILL.md` stable; use the review as evidence when proposing an approved rule
update. No automatic self-modification or transfer of lessons between accounts.
