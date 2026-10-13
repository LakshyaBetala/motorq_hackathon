# Terraform

```bash
terraform init
terraform plan -out tf.plan
terraform apply tf.plan

aws eks update-kubeconfig --name readyline --region ap-south-1
kubectl create secret generic readyline-secrets \
  --from-literal=READYLINE_PG_APP_DSN="postgresql://..." \
  --from-literal=CLICKHOUSE_PASSWORD="..." \
  --from-literal=GROQ_API_KEY="..." \
  --from-literal=OIDC_ISSUER="https://keycloak.example/realms/readyline"
helm upgrade --install readyline ../helm/readyline --set profile=scale
```

## What has and has not been run

**Neither `terraform validate` nor `terraform apply` has been run.** The Terraform CLI is not
installed on the development machine, and standing up an EKS cluster costs real money that was
out of scope for the hackathon.

Treat this as reviewed infrastructure code, not as a deployment that has been proven. It is the
weakest-evidence artefact in the repository and we would rather say so than imply a `plan` we
never produced. The same honesty applies to the Helm chart (written, not `helm lint`-ed) and to
the 100k events/sec figure (measured per-consumer and extrapolated — see `docs/BENCHMARKS.md`).

Running `terraform validate` is the first thing to do on a machine that has the CLI.

## Porting to a second cloud

Nothing in the application talks to an AWS API, so a port is a sibling directory producing the
same four outputs (`cluster_endpoint`, `cluster_name`, `cold_bucket`, `region`):

| AWS | GCP | Azure |
| --- | --- | --- |
| EKS | GKE | AKS |
| S3 + lifecycle | GCS + lifecycle | Blob + lifecycle |
| VPC module | VPC module | VNet module |

The Helm chart, the container images and the application code are unchanged.
