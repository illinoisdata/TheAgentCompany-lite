.PHONY: setup setup-full mock dry-run single build-base clean

setup:
	./scripts/macos_eval.sh install

setup-full:
	./scripts/macos_eval.sh install

build-base:
	@echo "Building base image locally (first time: ~5 min)..."
	sed 's|^FROM python:3.12$$|FROM python:3.12-slim-bookworm|' \
		TheAgentCompany/workspaces/base_image/Dockerfile > /tmp/tac-base.Dockerfile
	perl -pi -e 'print "RUN pip install \"setuptools<70\"\n" if /RUN pip install litellm==1.23.16/' /tmp/tac-base.Dockerfile
	docker build -t ghcr.io/haochengxia/theagentcompany-lite-base:latest \
		-f /tmp/tac-base.Dockerfile TheAgentCompany/workspaces/base_image/
	rm -f /tmp/tac-base.Dockerfile

mock:
	uv run python evaluation_lite/scheduler.py \
		--agent-llm-config agent --env-llm-config env \
		--mock --mock-duration 5,8 \
		--outputs-path ./outputs_mock

dry-run:
	uv run python evaluation_lite/scheduler.py \
		--agent-llm-config agent --env-llm-config env \
		--dry-run

single:
	@echo "Usage: make single TASK=gitlab-create-repo-1"
	uv run python evaluation_lite/run_eval.py \
		--task $(TASK) \
		--agent-llm-config agent --env-llm-config env \
		--server-hostname localhost \
		--verbose \
		--outputs-path ./outputs

clean:
	rm -rf outputs outputs_mock .venv
