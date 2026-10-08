.PHONY: up down seed simulate test test-cov lint load clean

COMPOSE := docker compose -f infra/compose/docker-compose.yml

up:
	$(COMPOSE) up -d
	@echo "waiting for health..." && $(COMPOSE) ps

down:
	$(COMPOSE) down

seed:
	python -m readyline_sim.seed

simulate:
	python -m readyline_sim.stream

test:
	python -m pytest

test-cov:
	python -m pytest --cov=readyline --cov=readyline_sim --cov-report=term-missing --cov-report=html

load:
	k6 run tests/load/ingest.js

clean:
	$(COMPOSE) down -v
