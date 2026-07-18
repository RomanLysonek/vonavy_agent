# GPU quota bootstrap

Region: `eu-central-1`.

Inspect adjustable EC2 quotas:

```bash
aws service-quotas list-service-quotas \
  --service-code ec2 \
  --region eu-central-1 \
  --profile vonavy-admin \
  --query "Quotas[?QuotaName=='All G and VT Spot Instance Requests' || QuotaName=='Running On-Demand G and VT instances'].{Name:QuotaName,Code:QuotaCode,Value:Value,Adjustable:Adjustable}" \
  --output table
```

Target bootstrap quota:

- All G and VT Spot Instance Requests: 8 vCPUs;
- Running On-Demand G and VT instances: 8 vCPUs.

Do not guess quota codes. Read the exact codes from the account and region, show the proposed requests, and obtain explicit approval before calling `request-service-quota-increase`.

The first deployment must still cap AWS Batch at one small single-GPU worker regardless of the approved quota.
