.DEFAULT_GOAL := help
.PHONY: help up down logs test shell psql

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

up: ## Build and start the whole stack
	docker compose up --build

down: ## Stop the stack and delete its data volume
	docker compose down -v

logs: ## Tail the api logs
	docker compose logs -f api

test: ## Run the test suite (against a separate drd_test database)
	docker compose run --rm \
		-e DATABASE_URL=postgresql+psycopg://drd:drd@db:5432/drd_test \
		api pytest -q

shell: ## Open a shell in the api container
	docker compose run --rm api bash

psql: ## Open psql against the running database
	docker compose exec db psql -U drd -d drd
