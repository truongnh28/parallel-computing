# zz_pack.mk -- packaging and helper targets (included last by the Makefile: mk/*.mk in
# alphabetical order).
#
#   make zip               pack the submission into assignment1.zip
#   make quick             tiny benchmark run that tests the whole pipeline (not for the report)
#   make bench-dry         print the grid and the estimated duration of `make bench`
#   make bench-relax       optional extra sweep: ISB vs YIELD as the spin-wait hint (AArch64),
#                          added to the results of an earlier `make bench`
#   make machine-info      print the description of this machine
#   make pc-print-config   compiler and flags (read by scripts/machine_info.sh)

ZIP_NAME  ?= assignment1
PACK_DIR  := $(BUILD)/pack
# What goes into the zip: sources, scripts, raw results, report sources with the generated
# figures, and the report PDF. Add docs/ or PLAN.md here if the group wants to submit them.
PACK_ITEMS ?= Makefile README.md mk include tests bench scripts results report

zip:
	@test -f report/report.pdf || { \
	  echo "*** WARNING: report/report.pdf does not exist. The submission needs it: run 'make figures report' first."; }
	@if ls -d results/FAKE* >/dev/null 2>&1; then \
	  echo "*** ERROR: results/ contains synthetic data (results/FAKE*). Remove it before packing."; exit 1; fi
	rm -rf $(PACK_DIR) $(ZIP_NAME).zip
	mkdir -p $(PACK_DIR)/$(ZIP_NAME)
	for item in $(PACK_ITEMS); do \
	  if [ -e $$item ]; then cp -R $$item $(PACK_DIR)/$(ZIP_NAME)/; fi; done
	cd $(PACK_DIR) && zip -qr ../../$(ZIP_NAME).zip $(ZIP_NAME) \
	  -x '*/__pycache__/*' '*.pyc' '*/.DS_Store' '*.dSYM/*' '*/old-*/*' '*.zip' \
	     $(foreach e,aux log out toc lof lot fls fdb_latexmk xdv bbl blg bcf run.xml synctex.gz,'$(ZIP_NAME)/report/*.$(e)')
	rm -rf $(PACK_DIR)
	@echo "packed $(ZIP_NAME).zip:"; unzip -l $(ZIP_NAME).zip | tail -1

quick: all
	$(PYTHON) scripts/run_bench.py --quick

bench-dry: all
	$(PYTHON) scripts/run_bench.py --dry-run

bench-relax: all $(BUILD)/bench_lock_yield
	$(PYTHON) scripts/run_bench.py --resume --relax-yield --part lock_yield

machine-info:
	@sh scripts/machine_info.sh

pc-print-config:
	@echo "cxx: $(CXX)"
	@echo "cxxflags: $(CXXFLAGS)"
	@echo "openmp: $(if $(filter yes,$(OPENMP_OK)),yes,no)"

.PHONY: quick bench-dry bench-relax machine-info pc-print-config
