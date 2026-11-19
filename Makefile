.DEFAULT_GOAL := vm-setup
export VM VCPU RAM_MB DISK_GB PROJECT RESUME REPO GIT_PROVIDER GIT_URL GIT_WRITE GIT_WRITE_BRANCH GATEWAY_SNI_LOG
export LOG_TAIL LOG_SERVICE LOG_FOLLOW DISK_KEEP

.PHONY: vm-setup vm-proxy vm-git vm-git-status vm-up vm-list vm-stop vm-logs vm-profile vm-remove vm-console init-claude claude claude-auth claude-logout

vm-setup vm-proxy vm-git vm-git-status vm-up vm-list vm-stop vm-logs vm-profile vm-remove vm-console init-claude claude claude-auth claude-logout:
	@bash v2/manage.sh $@
