COMPOSE = docker compose

.PHONY: init init-claude init-codex up

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

up:
	$(COMPOSE) up -d --build --wait
