IMAGE ?= mcuboot-renode-spike:latest
HOST_ARTIFACTS := $(CURDIR)/artifacts
CONTAINER_RUN = docker run --rm --network none --cap-drop ALL --security-opt no-new-privileges \
	--volume $(HOST_ARTIFACTS):/workspace/app/artifacts

.DEFAULT_GOAL := proof
.PHONY: image build-firmware baseline matrix determinism negative-tests prove-unprivileged proof \
	prepare-artifacts \
	baseline-in-container matrix-in-container determinism-in-container \
	negative-tests-in-container prove-unprivileged-in-container proof-in-container

image:
	docker build --network host --tag $(IMAGE) .

ifeq ($(IN_CONTAINER),1)

build-firmware:
	./scripts/build_firmware.sh

baseline: baseline-in-container
matrix: matrix-in-container
determinism: determinism-in-container
negative-tests: negative-tests-in-container
prove-unprivileged: prove-unprivileged-in-container
proof: proof-in-container

baseline-in-container:
	@test -x tests/baseline.sh || { echo "error: tests/baseline.sh is required; no baseline result was fabricated" >&2; exit 2; }
	./tests/baseline.sh

matrix-in-container: baseline-in-container
	@test -x tests/run_matrix.sh || { echo "error: tests/run_matrix.sh is required; no matrix result was fabricated" >&2; exit 2; }
	./tests/run_matrix.sh

determinism-in-container: baseline-in-container
	@test -x tests/determinism.sh || { echo "error: tests/determinism.sh is required; no determinism result was fabricated" >&2; exit 2; }
	./tests/determinism.sh

negative-tests-in-container: baseline-in-container
	@test -x tests/negative_tests.sh || { echo "error: tests/negative_tests.sh is required; no negative-test result was fabricated" >&2; exit 2; }
	./tests/negative_tests.sh

prove-unprivileged-in-container:
	@test -x tests/test_unprivileged.sh || { echo "error: tests/test_unprivileged.sh is required; no privilege proof was fabricated" >&2; exit 2; }
	./tests/test_unprivileged.sh

proof-in-container: baseline-in-container matrix-in-container determinism-in-container negative-tests-in-container prove-unprivileged-in-container
	python3 tests/verify_state.py self-test
	python3 tests/finalize_proof.py
	@test -f artifacts/proof-summary.json || { echo "error: proof did not create artifacts/proof-summary.json" >&2; exit 2; }

else

prepare-artifacts:
	install -d -m 0777 $(HOST_ARTIFACTS)
	chmod 0777 $(HOST_ARTIFACTS)

build-firmware: image
	$(CONTAINER_RUN) $(IMAGE) make build-firmware

baseline: image prepare-artifacts
	$(CONTAINER_RUN) $(IMAGE) make baseline

matrix: image prepare-artifacts
	$(CONTAINER_RUN) $(IMAGE) make matrix

determinism: image prepare-artifacts
	$(CONTAINER_RUN) $(IMAGE) make determinism

negative-tests: image prepare-artifacts
	$(CONTAINER_RUN) $(IMAGE) make negative-tests

prove-unprivileged: image prepare-artifacts
	$(CONTAINER_RUN) $(IMAGE) make prove-unprivileged

proof: image prepare-artifacts
	$(CONTAINER_RUN) $(IMAGE) make proof

endif
