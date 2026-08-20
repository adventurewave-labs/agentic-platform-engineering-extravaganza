# Northwind Platform — PCI-scope guardrails
#
# Northwind Retail runs a card-present business. Anything labelled
#   northwind.io/data-classification: pci
# inherits a stricter set of controls. These are the rules that used to live in
# a Confluence page and a QSA's spreadsheet.
#
# Engine: Open Policy Agent (Apache-2.0) via Conftest (Apache-2.0)

package main

import rego.v1

##############################################################################
# NW-PCI-001 — PCI workloads must be default-deny at the network layer
#
# An honest limitation, stated rather than hidden: conftest evaluates each
# document independently, so this rule cannot see whether the paired
# NetworkPolicy is actually present. It checks the annotation the renderer
# writes when it emits one, which catches the case that matters here (a PCI
# workload rendered outside the golden path) but would not catch someone
# setting the annotation by hand.
#
# Closing that gap properly means evaluating the combined document set, which
# is what an admission controller does. Kyverno or Gatekeeper is the right
# enforcement point for the cross-object half; this bundle is the pre-merge
# half. Use both -- they fail at different times, which is the point.
##############################################################################

deny contains msg if {
	input.kind in {"Deployment", "StatefulSet"}
	input.metadata.labels["northwind.io/data-classification"] == "pci"
	not input.metadata.annotations["northwind.io/network-policy"]
	msg := sprintf(
		"[NW-PCI-001] %s/%s: PCI workload has no paired NetworkPolicy -> emit a default-deny NetworkPolicy plus explicit egress to the database and set annotation northwind.io/network-policy",
		[input.kind, input.metadata.name],
	)
}

##############################################################################
# NW-PCI-002 — PCI NetworkPolicies must actually be default-deny
##############################################################################

deny contains msg if {
	input.kind == "NetworkPolicy"
	input.metadata.labels["northwind.io/data-classification"] == "pci"
	not "Ingress" in object.get(input, ["spec", "policyTypes"], [])
	msg := sprintf(
		"[NW-PCI-002] NetworkPolicy/%s: does not govern Ingress -> add \"Ingress\" to spec.policyTypes; PCI requires default-deny in both directions",
		[input.metadata.name],
	)
}

deny contains msg if {
	input.kind == "NetworkPolicy"
	input.metadata.labels["northwind.io/data-classification"] == "pci"
	not "Egress" in object.get(input, ["spec", "policyTypes"], [])
	msg := sprintf(
		"[NW-PCI-002] NetworkPolicy/%s: does not govern Egress -> add \"Egress\" to spec.policyTypes; unrestricted egress from PCI scope is a finding",
		[input.metadata.name],
	)
}

##############################################################################
# NW-PCI-003 — PCI data stores: encryption, backups, no public exposure
#
# Applies to the Crossplane composite resource (XR) the platform API renders.
##############################################################################

is_sql_claim if input.kind == "SQLInstance"

deny contains msg if {
	is_sql_claim
	input.metadata.labels["northwind.io/data-classification"] == "pci"
	not input.spec.parameters.storageEncrypted
	msg := sprintf(
		"[NW-PCI-003] SQLInstance/%s: storage encryption disabled -> set spec.parameters.storageEncrypted=true (this maps to the encrypted=true resource param in score.yaml)",
		[input.metadata.name],
	)
}

deny contains msg if {
	is_sql_claim
	input.metadata.labels["northwind.io/data-classification"] == "pci"
	object.get(input, ["spec", "parameters", "backupRetentionDays"], 0) < 30
	msg := sprintf(
		"[NW-PCI-003] SQLInstance/%s: backupRetentionDays=%v is below the 30-day PCI floor -> set spec.parameters.backupRetentionDays>=30",
		[input.metadata.name, object.get(input, ["spec", "parameters", "backupRetentionDays"], 0)],
	)
}

deny contains msg if {
	is_sql_claim
	input.spec.parameters.publiclyAccessible
	msg := sprintf(
		"[NW-PCI-003] SQLInstance/%s: publiclyAccessible=true -> never; the platform provisions a private endpoint and a NetworkPolicy egress rule instead",
		[input.metadata.name],
	)
}

##############################################################################
# NW-PCI-004 — data residency
#
# Northwind's EU entity is the data controller for EU cardholder data. The
# region must be inside the EU when residency is declared.
##############################################################################

eu_regions := {"eu-west-1", "eu-west-2", "eu-central-1", "eu-north-1", "eu-south-1"}

deny contains msg if {
	is_sql_claim
	input.metadata.labels["northwind.io/data-residency"] == "eu"
	not input.spec.parameters.region in eu_regions
	msg := sprintf(
		"[NW-PCI-004] SQLInstance/%s: region %q violates declared EU data residency -> choose one of %v",
		[input.metadata.name, object.get(input, ["spec", "parameters", "region"], "<unset>"), eu_regions],
	)
}

##############################################################################
# NW-PCI-005 — PCI workloads must ship audit logs off-host
##############################################################################

deny contains msg if {
	input.kind in {"Deployment", "StatefulSet"}
	input.metadata.labels["northwind.io/data-classification"] == "pci"
	not input.spec.template.metadata.annotations["northwind.io/audit-sink"]
	msg := sprintf(
		"[NW-PCI-005] %s/%s: no audit sink configured -> set pod annotation northwind.io/audit-sink (the golden path wires this to the platform's Loki tenant)",
		[input.kind, input.metadata.name],
	)
}

##############################################################################
# NW-PCI-006 — service accounts must not be automounted by default
##############################################################################

deny contains msg if {
	input.kind in {"Deployment", "StatefulSet"}
	input.metadata.labels["northwind.io/data-classification"] == "pci"
	object.get(input, ["spec", "template", "spec", "automountServiceAccountToken"], true)
	msg := sprintf(
		"[NW-PCI-006] %s/%s: service-account token is automounted into a PCI pod -> set spec.template.spec.automountServiceAccountToken=false",
		[input.kind, input.metadata.name],
	)
}
