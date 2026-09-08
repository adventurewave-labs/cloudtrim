# CloudTrim — productized cloud cost audit demo
#
# make up        build + start the stack (LocalStack + engine + dashboard)
# make demo      run the full pipeline: seed -> audit -> remediate -> verify
# make audit     re-run the audit only
# make remediate apply Tier 0/1 findings and re-audit
# make report    regenerate the client PDF report
# make cli       shell into the engine container
# make logs      tail engine logs
# make down      stop the stack
# make clean     stop the stack and remove engine state

COMPOSE := docker compose
ENGINE := $(COMPOSE) exec engine cloudtrim

.PHONY: up demo audit remediate report cli logs down clean

up:
	$(COMPOSE) up -d --build

demo:
	$(ENGINE) demo

audit:
	$(ENGINE) audit

remediate:
	$(ENGINE) remediate

report:
	$(ENGINE) report

cli:
	$(COMPOSE) exec engine bash

logs:
	$(COMPOSE) logs -f engine

down:
	$(COMPOSE) down

clean:
	$(COMPOSE) down -v
	rm -rf engine/data engine/output
	mkdir -p engine/data/cur engine/output/terraform
