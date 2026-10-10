APP_DIR := spring-petclinic-microservices
COMPOSE := docker compose --file $(APP_DIR)/docker-compose.yml
# Everything except the GenAI chatbot, which needs an OpenAI key.
MODULES := !spring-petclinic-genai-service

.PHONY: build up down status targets kill-switch

build: ## Build the PetClinic images from this repo's source for this machine's CPU, plus the chaos helper image
	cd $(APP_DIR) && ./mvnw -q clean install -P buildDocker -DskipTests \
		-pl '$(MODULES)' \
		-Dcontainer.platform=linux/$(shell uname -m | sed 's/x86_64/amd64/;s/aarch64/arm64/')
	docker build -q -t chaoslab/nettools tools/nettools

up: ## Start the whole sandbox and wait until it is healthy (add PROFILES=cadvisor for container metrics)
	$(COMPOSE) $(if $(PROFILES),--profile $(PROFILES)) up -d --build --wait

down: ## Stop and remove the sandbox
	$(COMPOSE) down

status: ## Show container health
	$(COMPOSE) ps --format 'table {{.Name}}\t{{.Status}}'

targets: ## List the containers the chaos controller is allowed to fault
	python3 -m chaoslab targets

kill-switch: ## Revert every active fault and restore the sandbox
	python3 -m chaoslab stop
