# Test entry points (docs/f001_buildout/decisions.md D16).
#   make test                            unit tests, no Krita needed
#   make test-krita                      headless suite inside kritarunner
#   make test-krita KRITA_TESTS=pixel    only tests whose name contains "pixel"
#   make test-live                       against the running GUI Krita
#   make lint-fixture                    rebuild tests/fixtures/lint_four_findings.kra

PYTEST ?= .venv/bin/pytest
KRITARUNNER ?= /Applications/krita.app/Contents/MacOS/kritarunner
TEST_KRITA_DIR ?= $(HOME)/krita-mcp-output/test-krita
KRITA_TESTS ?=
LINT_FIXTURE ?= $(CURDIR)/tests/fixtures/lint_four_findings.kra

.PHONY: test test-krita test-live install-test-runner lint-fixture

test:
	$(PYTEST) tests/unit

test-live:
	KRITA_MCP_LIVE=1 $(PYTEST) tests/live

install-test-runner:
	tools/install_test_runner.sh

# kritarunner's exit code doesn't reflect test results, so this reads the
# KMCP_TEST_RESULT line the shim prints. The full (noisy) Krita log goes to
# $(TEST_KRITA_DIR)/run.log; only the unittest output is shown.
test-krita:
	@mkdir -p "$(TEST_KRITA_DIR)"
	@echo "$(KRITARUNNER) -s kmcp_test_runner -f run $(CURDIR) $(KRITA_TESTS)"
	@KMCP_TEST_KRITA_DIR="$(TEST_KRITA_DIR)" "$(KRITARUNNER)" -s kmcp_test_runner -f run "$(CURDIR)" $(KRITA_TESTS) \
	    >"$(TEST_KRITA_DIR)/run.log" 2>&1; rc=$$?; \
	marker="$$(grep -E 'KMCP_TEST_RESULT: (PASS|FAIL)' "$(TEST_KRITA_DIR)/run.log" | tail -n 1)"; \
	if [ -n "$$marker" ] && [ -f "$(TEST_KRITA_DIR)/output.txt" ]; then \
	    cat "$(TEST_KRITA_DIR)/output.txt"; \
	else \
	    echo "--- last 40 lines of $(TEST_KRITA_DIR)/run.log ---"; \
	    tail -n 40 "$(TEST_KRITA_DIR)/run.log"; \
	fi; \
	echo "(kritarunner exit code $$rc; full log: $(TEST_KRITA_DIR)/run.log)"; \
	if [ -z "$$marker" ]; then \
	    echo "test-krita: FAIL, no KMCP_TEST_RESULT line (Krita crashed, or the shim is not installed: make install-test-runner)"; \
	    exit 1; \
	fi; \
	echo "$$marker"; \
	case "$$marker" in *"KMCP_TEST_RESULT: PASS"*) exit 0 ;; *) exit 1 ;; esac

# Rebuilds the committed lint fixture (acceptance.md § Fixtures, Lint
# fixture) headless, through the shim's build_lint_fixture entry point.
# Only needed when the fixture's definition changes; tests never rebuild it.
# Reads the KMCP_FIXTURE line, since kritarunner's exit code means nothing.
lint-fixture:
	@mkdir -p "$(TEST_KRITA_DIR)"
	@echo "$(KRITARUNNER) -s kmcp_test_runner -f build_lint_fixture $(CURDIR) $(LINT_FIXTURE)"
	@"$(KRITARUNNER)" -s kmcp_test_runner -f build_lint_fixture "$(CURDIR)" "$(LINT_FIXTURE)" \
	    >"$(TEST_KRITA_DIR)/fixture.log" 2>&1; \
	marker="$$(grep -E 'KMCP_FIXTURE: (OK|FAIL)' "$(TEST_KRITA_DIR)/fixture.log" | tail -n 1)"; \
	case "$$marker" in \
	    *"KMCP_FIXTURE: OK"*) echo "$$marker" ;; \
	    *) echo "--- last 40 lines of $(TEST_KRITA_DIR)/fixture.log ---"; \
	       tail -n 40 "$(TEST_KRITA_DIR)/fixture.log"; \
	       echo "lint-fixture: FAIL $$marker"; exit 1 ;; \
	esac
