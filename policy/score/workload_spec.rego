# Northwind Platform — Score workload-spec guardrails
#
# Score (CNCF Sandbox, score-spec/spec) is the developer-facing contract: a
# small, strictly-schema'd YAML that describes WHAT a workload needs, never
# HOW it is provisioned. Because it is small and schema'd, it is also the
# artifact an LLM generates most reliably — which is exactly why it is worth
# gating at this layer, before anything is rendered.
#
# Catching a violation here is cheaper than catching it in the rendered
# Kubernetes manifests, and the remediation is a one-line edit the agent can
# make confidently.
#
# Engine: Open Policy Agent (Apache-2.0) via Conftest (Apache-2.0)

package main

import rego.v1

is_score if input.apiVersion == "score.dev/v1b1"

wl := object.get(input, ["metadata", "name"], "<unnamed>")

##############################################################################
# NW-SCORE-001 — declare the platform contract fields the catalog needs
##############################################################################

required_annotations := {
	"northwind.io/owner",
	"northwind.io/cost-center",
	"northwind.io/data-classification",
}

deny contains msg if {
	is_score
	some a in required_annotations
	not object.get(input, ["metadata", "annotations", a], false)
	msg := sprintf(
		"[NW-SCORE-001] score.yaml %q: missing metadata.annotations[%q] -> the platform cannot attribute ownership or cost without it",
		[wl, a],
	)
}

##############################################################################
# NW-SCORE-002 — resources must be requested by type, never hand-wired
##############################################################################

known_resource_types := {"postgres", "mysql", "redis", "s3", "dns", "route", "volume", "amqp", "service-port"}

deny contains msg if {
	is_score
	some rname, r in object.get(input, "resources", {})
	not r.type in known_resource_types
	msg := sprintf(
		"[NW-SCORE-002] score.yaml %q: resource %q has unsupported type %q -> the Northwind provisioner set supports %v; request a new provisioner from the platform team",
		[wl, rname, r.type, known_resource_types],
	)
}

##############################################################################
# NW-SCORE-003 — credentials come from resource outputs, never literals
##############################################################################

deny contains msg if {
	is_score
	some cname, c in object.get(input, "containers", {})
	some ename, ev in object.get(c, ["variables"], {})
	is_string(ev)
	not contains(ev, "${resources.")
	regex.match(`(?i)(password|secret|token|apikey|api_key|credential|private_key)`, ename)
	msg := sprintf(
		"[NW-SCORE-003] score.yaml %q: containers.%s.variables.%s is a literal credential -> use ${resources.<name>.<output>} so the platform injects it from a Secret",
		[wl, cname, ename],
	)
}

##############################################################################
# NW-SCORE-004 — every container declares a resource envelope
##############################################################################

deny contains msg if {
	is_score
	some cname, c in object.get(input, "containers", {})
	not object.get(c, ["resources", "limits", "memory"], false)
	msg := sprintf(
		"[NW-SCORE-004] score.yaml %q: containers.%s has no memory limit -> unbounded workloads get evicted first and make capacity planning impossible",
		[wl, cname],
	)
}

##############################################################################
# NW-SCORE-005 — PCI workloads must declare residency
##############################################################################

deny contains msg if {
	is_score
	object.get(input, ["metadata", "annotations", "northwind.io/data-classification"], "") == "pci"
	not object.get(input, ["metadata", "annotations", "northwind.io/data-residency"], false)
	msg := sprintf(
		"[NW-SCORE-005] score.yaml %q: PCI workload does not declare data residency -> set metadata.annotations[\"northwind.io/data-residency\"] (eu | us | apac)",
		[wl],
	)
}
