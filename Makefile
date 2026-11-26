.DEFAULT_GOAL := vm-setup
export VM VCPU RAM_MB DISK_GB PROJECT RESUME REPO GIT_PROVIDER GIT_URL GIT_WRITE GIT_WRITE_BRANCH GIT_READ_BRANCHES GATEWAY_SNI_LOG
export LOG_TAIL LOG_SERVICE LOG_FOLLOW DISK_KEEP

.PHONY: atrice atrice-install
atrice:
	@GOTOOLCHAIN=local go build -o atrice ./cli

atrice-install:
	@bash cli/install.sh

.PHONY: vm-setup vm-proxy vm-git vm-git-status vm-git-audit vm-git-host-sync vm-git-host-writer vm-git-host-activate vm-git-host-disable vm-up vm-list vm-stop vm-logs vm-profile vm-remove vm-console vm-sessions vm-sessions-check vm-shell-check vm-session-open vm-session-claude vm-session-new vm-session-direct vm-session-direct-check init-claude claude claude-auth claude-logout

vm-setup vm-proxy vm-git vm-git-status vm-git-audit vm-git-host-sync vm-git-host-writer vm-git-host-activate vm-git-host-disable vm-up vm-list vm-stop vm-logs vm-profile vm-remove vm-console vm-sessions vm-sessions-check vm-shell-check vm-session-open vm-session-claude vm-session-new vm-session-direct vm-session-direct-check init-claude claude claude-auth claude-logout:
	@bash v2/manage.sh $@
