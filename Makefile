COMPOSE = docker compose
NPM_REGISTRY_HOST = registry.npmjs.org
export PROJECT RESUME

.PHONY: init init-claude init-codex claude claude-auth claude-logout up prepare-proxy check-install-proxy

init: init-claude init-codex

init-claude: check-install-proxy
	$(COMPOSE) exec -T -w /home/node agents sh -ec '\
		flock "$$HOME/.agent-cli-install.lock" npm install --global --prefix "$$HOME/.local" \
			--registry "https://$(NPM_REGISTRY_HOST)/" --https-proxy "$$HTTPS_PROXY" \
			--include=optional --no-audit --no-fund "@anthropic-ai/claude-code@$$AGENT_CLAUDE_CODE_VERSION"; \
		claude --version'

init-codex: check-install-proxy
	$(COMPOSE) exec -T -w /home/node agents sh -ec '\
		flock "$$HOME/.agent-cli-install.lock" npm install --global --prefix "$$HOME/.local" \
			--registry "https://$(NPM_REGISTRY_HOST)/" --https-proxy "$$HTTPS_PROXY" \
			--include=optional --no-audit --no-fund "@openai/codex@$$AGENT_CODEX_VERSION"; \
		codex --version'

claude: up
	@set --; \
		if [ -n "$$RESUME" ]; then set -- --resume "$$RESUME"; fi; \
		$(COMPOSE) exec -w "/workspace$${PROJECT:+/$$PROJECT}" agents claude "$$@"

claude-auth: up
	$(COMPOSE) exec agents claude auth login

claude-logout: up
	$(COMPOSE) exec agents claude auth logout

up: prepare-proxy
	$(COMPOSE) up -d --no-build --pull never --wait --wait-timeout 60

check-install-proxy: up
	$(COMPOSE) exec -T agents sh -ec '\
		test -n "$${HTTPS_PROXY:-}"; \
		for attempt in 1 2 3; do \
			if ! curl -q --proxy "$$HTTPS_PROXY" --noproxy "" --fail --silent --show-error \
					--connect-timeout 3 --max-time 10 --output /dev/null "https://$(NPM_REGISTRY_HOST)/-/ping"; then \
				printf "Проверка npm через Tinyproxy не прошла (%s из 3). Установка отменена.\n" "$$attempt" >&2; \
				exit 1; \
			fi; \
			if [ "$$attempt" -lt 3 ]; then sleep 1; fi; \
		done'

prepare-proxy:
	@set -eu; umask 022; \
		compose_env="$$( $(COMPOSE) config --environment )"; \
		projects_dir="$$(printf '%s\n' "$$compose_env" | sed -n 's/^PROJECTS_DIR=//p')"; \
		proxy_dir="$$(printf '%s\n' "$$compose_env" | sed -n 's/^PROXY_CONFIG_DIR=//p')"; \
		projects_dir="$$(cd -- "$$projects_dir" && pwd -P)"; \
		mkdir -p -- "$$proxy_dir"; \
		proxy_dir="$$(cd -- "$$proxy_dir" && pwd -P)"; \
		case "$$proxy_dir/" in \
			"$${projects_dir%/}/"*) \
				printf '%s\n' 'PROXY_CONFIG_DIR должен находиться вне PROJECTS_DIR.' >&2; exit 1 ;; \
		esac; \
		cp -n -- allowed-domains.example "$$proxy_dir/allowed-domains"
