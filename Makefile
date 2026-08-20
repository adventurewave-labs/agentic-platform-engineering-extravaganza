# Agentic Platform Engineering Extravaganza
.DEFAULT_GOAL := help
SHELL := /bin/bash

.PHONY: help setup demo verify site mcp tools drift gate record site-build clean versions docker

help: ## show this help
	@grep -hE '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-13s\033[0m %s\n", $$1, $$2}'

setup: ## fetch the pinned upstream binaries (conftest, score-k8s, kube-linter, ...)
	@./run.sh setup --all

demo: ## run the eight-act demo
	@./run.sh demo

verify: ## run the 14 acceptance checks against real tool output
	@./run.sh verify

versions: ## print the exact upstream tool versions in play
	@./run.sh versions

site: ## serve the showcase page on :8080
	@./run.sh site

mcp: ## start the platform MCP server on :8099
	@./run.sh mcp

tools: ## show the MCP tool list each identity receives
	@for id in platform-agent drift-agent cost-reviewer release-manager; do \
	  ./run.sh tools $$id; echo; done

drift: ## run the day-2 drift agent
	@./run.sh drift

gate: ## evaluate the committed manifests against the policy bundle
	@./run.sh gate outputs/final-manifests.yaml

record: ## rebuild the asciinema casts and GIFs
	@./run.sh record

site-build: ## regenerate the report + playground data and re-render index.html
	@python3 src/build_report.py && python3 src/build_playground.py && python3 src/build_site.py

docker: ## build the container image
	@docker build -t agentic-platform-engineering:latest .

clean: ## remove scratch state (keeps outputs/, gifs/, recordings/)
	@rm -rf workspace/* && find . -name '__pycache__' -type d -prune -exec rm -rf {} + \
	  && echo "  cleaned"
