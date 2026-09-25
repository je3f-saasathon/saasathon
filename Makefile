.PHONY: setup dev dev-docker stop restart restart-docker status logs doctor test contracts reset-db dev-token sync-agents deploy prod-local tunnel infra-up infra-down sandbox-image sandbox-image-prod sre-worker

setup:
	scripts/setup.sh $(if $(DOCKER_ONLY),--docker-only,) $(if $(NATIVE_ONLY),--native-only,) $(if $(YES),--yes,)

dev:
	scripts/dev.sh $(if $(FORCE),--force,)

dev-docker:
	scripts/dev-docker.sh $(if $(DETACH),--detach,) $(if $(FORCE),--force,)

stop:
	scripts/stop.sh $(if $(ALL),--all,)

restart:
	scripts/restart.sh

restart-docker:
	scripts/restart.sh --docker $(if $(DETACH),--detach,)

status:
	scripts/status.sh

logs:
	scripts/logs.sh $(SERVICE)

doctor:
	scripts/doctor.sh

test:
	scripts/test.sh

contracts:
	scripts/contracts.sh

reset-db:
	scripts/reset-db.sh

dev-token:
	scripts/dev-token.sh

sync-agents:
	scripts/sync-agent-docs.sh

deploy:
	scripts/deploy.sh $(if $(DRY_RUN),--dry-run,)

prod-local:
	scripts/deploy.sh

tunnel:
	scripts/dev-tunnel.sh $(if $(CMD),$(CMD),up) $(SVC) $(if $(DB),--db,) $(if $(FORCE),--force,)

infra-up:
	scripts/infra.sh up

infra-down:
	scripts/infra.sh down

sandbox-image:
	docker build -t $${SRE_SANDBOX_IMAGE:-saasathon-sre-sandbox:latest} backend/sre/sandbox

sandbox-image-prod:
	docker compose -f docker-compose.prod.yml exec sre-docker docker build -t $${SRE_SANDBOX_IMAGE:-saasathon-sre-sandbox:latest} /sandbox

sre-worker:
	cd backend && uv run python -m sre.worker
