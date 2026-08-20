# Northwind Platform — FinOps guardrails
#
# Cost is a policy input, not a monthly surprise. The agent renders a cost
# estimate for the infrastructure it is about to request and this bundle
# decides whether that estimate is allowed to proceed.
#
# Input shape (produced by src/costing.py, Infracost-compatible schema subset):
# {
#   "service": "payments-ledger",
#   "environment": "prod",
#   "costCenter": "CC-4471",
#   "monthlyBudgetUsd": 400,
#   "totalMonthlyCostUsd": 612.40,
#   "resources": [ {"name": ..., "type": ..., "monthlyCostUsd": ..., "params": {...}} ]
# }
#
# Engine: Open Policy Agent (Apache-2.0) via Conftest (Apache-2.0)

package main

import rego.v1

##############################################################################
# NW-FIN-001 — respect the team's monthly budget envelope
##############################################################################

# OPA normalises a whole float (400.0) to an int64, and sprintf("%.2f") then
# refuses it -- you get a literal "%!f(int64=400)" in the message. Formatting
# the cents by hand is the portable fix.
usd(x) := s if {
	cents := round(x * 100)
	whole := floor(cents / 100)
	s := sprintf("%d.%02d", [whole, cents - (whole * 100)])
}

pct(x) := sprintf("%d", [round(x)])

deny contains msg if {
	input.totalMonthlyCostUsd > input.monthlyBudgetUsd
	over := input.totalMonthlyCostUsd - input.monthlyBudgetUsd
	msg := sprintf(
		"[NW-FIN-001] %s/%s: estimated $%s/mo exceeds the %s budget of $%s/mo by $%s -> right-size the largest line item or request a budget increase from the cost-center owner",
		[input.service, input.environment, usd(input.totalMonthlyCostUsd), input.costCenter, usd(input.monthlyBudgetUsd), usd(over)],
	)
}

##############################################################################
# NW-FIN-002 — non-production must not provision production-grade instances
##############################################################################

prod_grade := {"db.r6g.xlarge", "db.r6g.2xlarge", "db.m6g.2xlarge", "db.r6g.4xlarge"}

deny contains msg if {
	input.environment != "prod"
	some r in input.resources
	r.params.instanceClass in prod_grade
	msg := sprintf(
		"[NW-FIN-002] %s/%s: resource %q uses production-grade instance class %q in a non-production environment -> the golden path defaults staging to db.t4g.medium",
		[input.service, input.environment, r.name, r.params.instanceClass],
	)
}

##############################################################################
# NW-FIN-003 — no single resource may dominate the envelope unreviewed
##############################################################################

deny contains msg if {
	some r in input.resources
	input.totalMonthlyCostUsd > 0
	share := (r.monthlyCostUsd / input.totalMonthlyCostUsd) * 100
	share > 70
	r.monthlyCostUsd > 250
	msg := sprintf(
		"[NW-FIN-003] %s/%s: resource %q is $%s/mo — %s%% of the whole service budget -> single line items over 70%% need an explicit exception annotation from the cost-center owner",
		[input.service, input.environment, r.name, usd(r.monthlyCostUsd), pct(share)],
	)
}

##############################################################################
# NW-FIN-004 — every provisioned resource carries a cost-allocation tag
##############################################################################

deny contains msg if {
	some r in input.resources
	not r.params.costCenter
	msg := sprintf(
		"[NW-FIN-004] %s/%s: resource %q has no costCenter tag -> untagged spend cannot be charged back; the scaffolder injects this from the owning Group",
		[input.service, input.environment, r.name],
	)
}

##############################################################################
# NW-FIN-005 — non-production should not run multi-AZ
##############################################################################

deny contains msg if {
	input.environment != "prod"
	some r in input.resources
	r.params.multiAz
	msg := sprintf(
		"[NW-FIN-005] %s/%s: resource %q runs multi-AZ outside production -> roughly doubles cost for no availability benefit in %s; set multiAz=false",
		[input.service, input.environment, r.name, input.environment],
	)
}

##############################################################################
# NW-FIN-006 — warn (do not block) when spend jumps sharply
##############################################################################

warn contains msg if {
	input.previousMonthlyCostUsd > 0
	delta := input.totalMonthlyCostUsd - input.previousMonthlyCostUsd
	change_pct := (delta / input.previousMonthlyCostUsd) * 100
	change_pct > 25
	msg := sprintf(
		"[NW-FIN-006] %s/%s: this change increases spend by %s%% ($%s/mo) -> allowed, but the diff is posted to the cost-center owner for review",
		[input.service, input.environment, pct(change_pct), usd(delta)],
	)
}
