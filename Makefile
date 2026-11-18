.DEFAULT_GOAL := vm-setup
export VM VCPU RAM_MB DISK_GB PROJECT RESUME

.PHONY: vm-setup vm-proxy vm-up vm-list vm-stop vm-console init-claude claude claude-auth claude-logout

vm-setup vm-proxy vm-up vm-list vm-stop vm-console init-claude claude claude-auth claude-logout:
	@bash v2/manage.sh $@
