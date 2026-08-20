# ${{ values.name }}

${{ values.description }}

Scaffolded from the Northwind golden path `golden-path-service-postgres`.

| | |
|---|---|
| Owner | `${{ values.owner }}` |
| Cost centre | `${{ values.costCenter }}` |
| Data classification | `${{ values.dataClassification }}` |
| Data residency | `${{ values.dataResidency }}` |

## The only file you need to care about

`score.yaml`. It says what this service needs; the platform decides how that
gets provisioned. If you need something it cannot express, that is a
conversation with `@platform-team` about a new provisioner, not a reason to
hand-write Kubernetes YAML.

## What the platform added on your behalf

Hardened security context, dropped capabilities, read-only root filesystem,
liveness and readiness probes, topology spread, a PodDisruptionBudget, a
default-deny NetworkPolicy, ownership and cost-centre labels, and — because
this service is `${{ values.dataClassification }}` — an encrypted database with
a 30-day backup window and no automounted service-account token.

You did not have to remember any of it, and you cannot accidentally remove it.

## Local

```bash
score-compose init && score-compose generate score.yaml && docker compose up
```

## Promotion

`main` → staging automatically. Staging → production is a Kargo promotion that
requires an approval from `${{ values.owner }}`.
