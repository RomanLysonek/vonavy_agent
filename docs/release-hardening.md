# Release hardening contract

Phase 5 adds bounded client resilience and response provenance without changing
application authority, authentication, routes, storage, IAM, or compute.

## Browser request policy

The authenticated API client applies these fixed rules:

- each API attempt has a 15-second timeout enforced by `AbortController`;
- only `GET` requests may be retried;
- a `GET` receives at most three total attempts;
- delays are deterministic: 250 ms before attempt two and 750 ms before attempt three;
- HTTP retries are limited to `429`, `502`, `503`, and `504`;
- transport failures and timeouts are retryable only for `GET`;
- `POST` requests are attempted exactly once and are never retried automatically;
- the browser does not create forecast, validation, upload, or agent mutations after an
  ambiguous response.

This policy protects read-only polling from transient failures while preserving
server-side idempotency and explicit-confirmation boundaries for mutations.

## Response provenance

Both API Lambdas add these response headers to every JSON response:

- `x-vonavy-request-id`: a version-4 UUID generated from an independent
  operating-system random source and written to the corresponding structured
  CloudWatch log entry; response correlation never consumes the domain UUID
  generator used for dataset, upload, validation, session, or forecast identities;
- `x-vonavy-source-revision`: the exact deployed source revision supplied by CDK.

API Gateway CORS exposes both headers to the browser. User-visible API errors include
the response reference and abbreviated source revision when those values are available.
The headers contain no owner identifier, token, dataset value, row value, or presigned URL.

## Operational boundaries

Phase 5 deliberately adds no:

- API route;
- IAM action or resource widening;
- DynamoDB, S3, Cognito, CloudFront, Batch, ECS, VPC, queue, or compute resource;
- worker behavior change;
- model, preprocessing, evaluation, or agent policy change;
- automatic mutation retry, training run, validation run, forecast run, or agent turn.

A release is supportable only when a failing browser request can be tied to one bounded
response log entry and one deployed source revision without exposing private data.
