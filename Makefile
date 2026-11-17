VENV ?= .venv
BIN := $(VENV)/bin
export ANSIBLE_CONFIG := $(CURDIR)/ansible.cfg

.PHONY: venv collections lint test render smoke smoke-debian e2e e2e-replica e2e-search-authz clean

venv:
	python3 -m venv $(VENV)
	$(BIN)/pip install --quiet --upgrade pip
	$(BIN)/pip install --quiet 'ansible-core>=2.14' ansible-lint yamllint shellcheck-py

collections:
	$(BIN)/ansible-galaxy collection install -r requirements.yml -p collections

lint: collections
	$(BIN)/yamllint .
	$(BIN)/ansible-lint
	$(BIN)/shellcheck -x install.sh tools/build-offline-bundle.sh tests/smoke/run.sh tests/render/run.sh tests/e2e/run.sh tests/e2e/functional.sh tests/e2e/replica.sh tests/e2e/opensearch_authz.sh
	for pb in playbooks/*.yml; do $(BIN)/ansible-playbook --syntax-check $$pb || exit 1; done

test:
	$(BIN)/python -m unittest discover -s tests -v

render: collections
	ANSIBLE_PLAYBOOK=$(BIN)/ansible-playbook tests/render/run.sh

smoke:
	tests/smoke/run.sh

# Debian 12 and Ubuntu 24.04. The images are built here because the usual ones are amd64 only.
smoke-debian:
	docker build -q -f tests/smoke/Dockerfile.debian --build-arg BASE=debian:12 -t mmd-systemd-debian:12 tests/smoke
	docker build -q -f tests/smoke/Dockerfile.debian --build-arg BASE=ubuntu:24.04 -t mmd-systemd-ubuntu:24.04 tests/smoke
	ENGINE=docker NAME=mmd-smoke-debian IMAGE=mmd-systemd-debian:12 tests/smoke/run.sh
	ENGINE=docker NAME=mmd-smoke-ubuntu IMAGE=mmd-systemd-ubuntu:24.04 tests/smoke/run.sh

e2e:
	tests/e2e/run.sh

e2e-replica:
	tests/e2e/replica.sh

# What Mattermost's OpenSearch account can and cannot do, against a real node.
e2e-search-authz:
	tests/e2e/opensearch_authz.sh

clean:
	rm -rf collections tests/smoke/out tests/render/out tests/e2e/out
