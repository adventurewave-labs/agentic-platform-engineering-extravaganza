# Northwind Platform — Kubernetes workload guardrails
#
# These are the rules the platform team encodes ONCE so that no human has to
# review them again. The agent reads the deny messages and remediates.
#
# Every message carries: [POLICY-ID] subject: what's wrong -> how to fix it
# The "-> how to fix it" half is what makes the loop agentic rather than
# merely a linter. The agent parses it.
#
# Evaluated by: conftest test --policy policy/ <manifests>
# Engine:       Open Policy Agent (Apache-2.0) / Conftest (Apache-2.0)

package main

import rego.v1

##############################################################################
# helpers
##############################################################################

workload_kinds := {"Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob"}

is_workload if input.kind in workload_kinds

pod_spec := input.spec.template.spec if {
	input.kind in {"Deployment", "StatefulSet", "DaemonSet"}
}

pod_spec := input.spec.template.spec if input.kind == "Job"

pod_spec := input.spec.jobTemplate.spec.template.spec if input.kind == "CronJob"

containers contains c if {
	some c in pod_spec.containers
}

containers contains c if {
	some c in pod_spec.initContainers
}

name := sprintf("%s/%s", [input.kind, object.get(input, ["metadata", "name"], "<unnamed>")])

labels := object.get(input, ["metadata", "labels"], {})

pod_labels := object.get(input, ["spec", "template", "metadata", "labels"], {})

is_pci if labels["northwind.io/data-classification"] == "pci"

is_prod if labels["northwind.io/environment"] == "prod"

# A tag is acceptable if it is a digest pin or a semver-ish release tag.
acceptable_image_ref(ref) if contains(ref, "@sha256:")

acceptable_image_ref(ref) if {
	not contains(ref, "@sha256:")
	parts := split(ref, ":")
	count(parts) > 1
	tag := parts[count(parts) - 1]
	regex.match(`^v?[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$`, tag)
}

##############################################################################
# NW-K8S-001 — image tags must be immutable
##############################################################################

deny contains msg if {
	is_workload
	some c in containers
	not acceptable_image_ref(c.image)
	msg := sprintf(
		"[NW-K8S-001] %s container %q: image %q is not immutably pinned -> pin to a semver tag or @sha256 digest in score.yaml containers.%s.image",
		[name, c.name, c.image, c.name],
	)
}

##############################################################################
# NW-K8S-002 — every container declares requests AND limits
##############################################################################

deny contains msg if {
	is_workload
	some c in containers
	not c.resources.limits.memory
	msg := sprintf(
		"[NW-K8S-002] %s container %q: no memory limit -> set containers.%s.resources.limits.memory in score.yaml",
		[name, c.name, c.name],
	)
}

deny contains msg if {
	is_workload
	some c in containers
	not c.resources.limits.cpu
	msg := sprintf(
		"[NW-K8S-002] %s container %q: no CPU limit -> set containers.%s.resources.limits.cpu in score.yaml",
		[name, c.name, c.name],
	)
}

deny contains msg if {
	is_workload
	some c in containers
	not c.resources.requests.memory
	msg := sprintf(
		"[NW-K8S-002] %s container %q: no memory request -> set containers.%s.resources.requests.memory in score.yaml",
		[name, c.name, c.name],
	)
}

##############################################################################
# NW-K8S-003 — ownership and cost attribution labels
##############################################################################

required_labels := {
	"app.kubernetes.io/name",
	"backstage.io/component",
	"northwind.io/owner",
	"northwind.io/cost-center",
	"northwind.io/data-classification",
}

deny contains msg if {
	is_workload
	some required in required_labels
	not labels[required]
	msg := sprintf(
		"[NW-K8S-003] %s: missing required label %q -> the golden-path scaffolder injects this from catalog-info.yaml; re-run scaffolder.execute with the owning Group set",
		[name, required],
	)
}

##############################################################################
# NW-K8S-004 — hardened pod security (restricted PSS baseline)
##############################################################################

deny contains msg if {
	is_workload
	not pod_spec.securityContext.runAsNonRoot
	msg := sprintf(
		"[NW-K8S-004] %s: pod may run as root -> set securityContext.runAsNonRoot=true (golden-path default; someone overrode it)",
		[name],
	)
}

deny contains msg if {
	is_workload
	some c in containers
	c.securityContext.allowPrivilegeEscalation
	msg := sprintf(
		"[NW-K8S-004] %s container %q: allowPrivilegeEscalation is true -> set it to false",
		[name, c.name],
	)
}

deny contains msg if {
	is_workload
	some c in containers
	not c.securityContext.readOnlyRootFilesystem
	msg := sprintf(
		"[NW-K8S-004] %s container %q: writable root filesystem -> set readOnlyRootFilesystem=true and mount an emptyDir for scratch",
		[name, c.name],
	)
}

deny contains msg if {
	is_workload
	some c in containers
	dropped := object.get(c, ["securityContext", "capabilities", "drop"], [])
	not "ALL" in dropped
	msg := sprintf(
		"[NW-K8S-004] %s container %q: does not drop ALL capabilities -> set securityContext.capabilities.drop=[\"ALL\"]",
		[name, c.name],
	)
}

##############################################################################
# NW-K8S-005 — health probes are mandatory (they gate Argo Rollouts analysis)
##############################################################################

deny contains msg if {
	input.kind in {"Deployment", "StatefulSet"}
	some c in pod_spec.containers
	not c.readinessProbe
	msg := sprintf(
		"[NW-K8S-005] %s container %q: no readinessProbe -> Argo Rollouts cannot verify a canary without one; add a probe on the service port",
		[name, c.name],
	)
}

deny contains msg if {
	input.kind in {"Deployment", "StatefulSet"}
	some c in pod_spec.containers
	not c.livenessProbe
	msg := sprintf(
		"[NW-K8S-005] %s container %q: no livenessProbe -> add a probe on the service port",
		[name, c.name],
	)
}

##############################################################################
# NW-K8S-006 — no plaintext credentials in the pod spec
##############################################################################

secretish := ["password", "passwd", "secret", "token", "apikey", "api_key", "private_key", "credential"]

deny contains msg if {
	is_workload
	some c in containers
	some e in object.get(c, "env", [])
	e.value
	some pattern in secretish
	contains(lower(e.name), pattern)
	msg := sprintf(
		"[NW-K8S-006] %s container %q: env %q holds a literal value that looks like a credential -> reference it via resources.<name> in score.yaml so the platform injects a secretKeyRef",
		[name, c.name, e.name],
	)
}

##############################################################################
# NW-K8S-007 — production availability floor
##############################################################################

deny contains msg if {
	input.kind == "Deployment"
	is_prod
	object.get(input, ["spec", "replicas"], 1) < 2
	msg := sprintf(
		"[NW-K8S-007] %s: prod workload has fewer than 2 replicas -> a single replica cannot survive a node drain; set replicas>=2",
		[name],
	)
}

##############################################################################
# NW-K8S-008 — approved registries only
##############################################################################

approved_registries := {"ghcr.io/northwind-retail", "registry.northwind.internal"}

deny contains msg if {
	is_workload
	some c in containers
	not startswith_any(c.image, approved_registries)
	msg := sprintf(
		"[NW-K8S-008] %s container %q: image %q is not from an approved registry -> build via the golden-path CI workflow, which publishes to ghcr.io/northwind-retail",
		[name, c.name, c.image],
	)
}

startswith_any(s, prefixes) if {
	some p in prefixes
	startswith(s, p)
}
