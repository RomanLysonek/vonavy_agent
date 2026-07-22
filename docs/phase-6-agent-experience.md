# Phase 6 agent experience hardening

This release makes conversational forecast planning reliable without changing model workers or forecast contracts.

## Asynchronous turn contract

Agent-session POST requests persist a bounded queued turn and asynchronously invoke the existing forecast-control-plane Lambda. The request returns before Bedrock work begins. The worker claims the queued turn conditionally in DynamoDB, performs the existing bounded tool workflow, and persists either a succeeded response or a bounded failure. Duplicate async deliveries cannot execute the same turn twice because only the queued-to-processing conditional transition may call Bedrock.

The browser polls the existing owner-scoped session GET route. Mutating POSTs remain single-attempt and are never retried automatically.

## Presentation and confirmation

Assistant messages are rendered by a small DOM-only Markdown renderer supporting headings, paragraphs, lists, code, blockquotes, links, and tables. It does not use `innerHTML`, `DOMParser`, remote scripts, or unsanitized HTML.

Every new turn clears the preceding draft plan. Confirmation remains disabled until the latest completed turn returns a valid `draftPlan`. Guard failures produce visible status text rather than silently returning.
