COMPOSE = docker compose

.PHONY: init init-claude init-codex up prepare-proxy

init: init-claude init-codex

init-claude: up
	$(COMPOSE) exec -T -w /home/node agents sh -ec '\
		flock "$$HOME/.agent-cli-install.lock" npm install --global --prefix "$$HOME/.local" \
			--include=optional --no-audit --no-fund "@anthropic-ai/claude-code@$$AGENT_CLAUDE_CODE_VERSION"; \
		claude --version'

init-codex: up
	$(COMPOSE) exec -T -w /home/node agents sh -ec '\
		flock "$$HOME/.agent-cli-install.lock" npm install --global --prefix "$$HOME/.local" \
			--include=optional --no-audit --no-fund "@openai/codex@$$AGENT_CODEX_VERSION"; \
		codex --version'

up: prepare-proxy
	$(COMPOSE) up -d --build --wait

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
