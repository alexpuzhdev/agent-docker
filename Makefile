.DEFAULT_GOAL := vm-setup
export VM VCPU RAM_MB DISK_GB PROJECT RESUME REPO GIT_WRITE GIT_WRITE_BRANCH GATEWAY_SNI_LOG

.PHONY: vm-setup vm-proxy vm-git vm-git-status vm-up vm-list vm-stop vm-console init-claude claude claude-auth claude-logout

vm-setup vm-proxy vm-git vm-git-status vm-up vm-list vm-stop vm-console init-claude claude claude-auth claude-logout:
	@bash v2/manage.sh $@
