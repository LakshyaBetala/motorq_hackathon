# ReadyLine. `make up && make seed && make experiment && make api` gives a working demo.
PY := PYTHONPATH=services/api;services/simulator
COMPOSE := docker compose -f infra/compose/docker-compose.yml

.PHONY: up down ps seed stream ingest experiment api web test test-cov bench clean

up:                      ## boot the stack
	$(COMPOSE) up -d
	@echo "waiting for health..."
	@$(COMPOSE) ps

down:
	$(COMPOSE) down

ps:
	@$(COMPOSE) ps

seed:                    ## 100k vehicles across 60 tenants into PostgreSQL
	@PYTHONPATH="services/api;services/simulator" python -m readyline_sim.seed

stream:                  ## produce telemetry into Redpanda
	@PYTHONPATH="services/api;services/simulator" python -m readyline_sim.stream

ingest:                  ## consume Redpanda -> normalise -> ClickHouse
	@PYTHONPATH="services/api;services/simulator" python services/ingest/consumer.py

experiment:              ## run both arms for 30 nights into ClickHouse
	@PYTHONPATH="services/api;services/simulator" python -m readyline_sim.charging

api:                     ## serve the API on :8000
	@PYTHONPATH="services/api" python -m uvicorn readyline.api.main:app --host 0.0.0.0 --port 8000

web:                     ## serve the UI on :5173 (proxies /api to :8000)
	cd web && npm run dev

test:
	python -m pytest

test-cov:
	python -m pytest --cov=readyline --cov=readyline_sim --cov-report=term-missing --cov-report=html

bench:                   ## throughput measurement, see docs/BENCHMARKS.md
	@echo "4 producers..."
	@for i in 1 2 3 4; do (PYTHONPATH="services/api;services/simulator" SIM_SEED=$$((20261008+i)) \
	  SIM_LIVE_VEHICLES=1500 SIM_DURATION_S=40 python -m readyline_sim.stream &); done; sleep 60
	@echo "4 consumers..."
	@for i in 1 2 3 4; do (PYTHONPATH="services/api;services/simulator" INGEST_IDLE_EXIT_S=10 \
	  INGEST_BATCH_ROWS=50000 python services/ingest/consumer.py &); done; sleep 95

clean:
	$(COMPOSE) down -v
