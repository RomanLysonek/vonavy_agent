# Phase 6 custom-domain cutover

The portfolio application remains hosted by the existing private-S3 and CloudFront
architecture. WEDOS remains the registrar. Amazon Route 53 is authoritative DNS for
`vonava-predikce.fun`, and the CloudFront viewer certificate is an ACM certificate in
`us-east-1` covering both:

- `vonava-predikce.fun`
- `www.vonava-predikce.fun`

The control-plane stack imports the pre-provisioned Route 53 hosted zone and ACM
certificate. It manages only the CloudFront aliases and Route 53 `A`/`AAAA` alias
records. This avoids cross-region CloudFormation certificate references and keeps the
existing application stack in `eu-central-1`.

## Deployment inputs

A domain-enabled deployment must set all three values together:

```bash
VONAVY_PUBLIC_DOMAIN_NAME=vonava-predikce.fun
VONAVY_PUBLIC_HOSTED_ZONE_ID=Z...
VONAVY_PUBLIC_CERTIFICATE_ARN=arn:aws:acm:us-east-1:...:certificate/...
```

The certificate must already be `ISSUED`, and WEDOS must delegate the domain to the
four Route 53 nameservers for the imported hosted zone. The application continues to
publish the generated CloudFront URL as `LegacyWebUrl` for rollback.

## Security and compatibility

- Public Cognito self-registration remains disabled.
- All 14 API routes retain JWT authorization and the `vonavy-agent/api` scope.
- API and upload CORS permit the apex domain, `www`, the legacy CloudFront URL, and the
  explicit localhost development callback only.
- OAuth callback/logout URLs permit the same bounded set.
- The generated browser configuration uses the apex HTTPS URL as its canonical
  `redirectUri`.
- No EC2, load balancer, public S3 bucket, new worker, new queue, or anonymous API is
  introduced.
