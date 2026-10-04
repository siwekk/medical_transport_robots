Hospital delivery robot dispatch: reproduction package

This compact repository reproduces the numerical comparisons in 

    **A State Based Dispatch Controller for Hospital Delivery Robots with Shared Human and Infrastructure Resources**
	by Krzysztof Siwek and Aleksandra &Sacute;wietlicka

It retains pickup travel, human support, and the shared elevator, charging, and cleaning model. It contains only the required simulator modules, experiment designs, analysis programs, prepared input, saved confirmation outputs, and two focused test files. It excludes the article sources, earlier experiments, raw source downloads, and temporary files.

Use Python 3.12 or later. This package was checked with Python 3.12.14. Run commands from this directory. Create and activate a virtual environment, then install the package with:

    python -m pip install -r requirements-tested.txt
    python -m pip install --no-deps -e .

The requirements file records the versions used for the package check. The project metadata also allows compatible dependency versions. Exact floating point serialization or PDF metadata can differ across environments. The required result is agreement of numerical comparisons, rather than identical PDF bytes.

To check the package, regenerate the published analyses from the saved simulations, and run the focused tests:

    python scripts/reproduce.py verify
    python scripts/reproduce.py analysis
    python -m pytest

Analysis does not rerun the simulator. It regenerates all reported numerical comparison tables, paired intervals, coverage and support summaries, and three figures. CSV tables are written to results/tables. LaTeX table fragments and PDF figures are written to manuscript. The manuscript prose and article PDF are intentionally excluded. The six saved run files contain 2,460 replications: 2,160 in the two full grids, 180 in the pickup and elevator sensitivity cases, and 120 in the exploratory deadline benchmark. Screening runs are excluded because they are not used for the reported inference.

For a complete simulation rerun:

    python scripts/reproduce.py simulate --workers 4

This executes all 2,460 replications and then regenerates the analyses. It is substantially slower than analysis. Change the worker count to suit available memory and CPU capacity. No runtime estimate is claimed. Existing raw outputs and manifests are copied to results/archive before replacement. The deadline checkpoint is then cleared so all 120 benchmark runs are executed. A small installation check is available with:

    python scripts/reproduce.py smoke --workers 1

The smoke command executes one full confirmation replication, not the entire grid. It writes separate smoke files. The simulator uses the original seeds 20261120 through 20261149, seven warmup days, thirty measurement days, and one clearance day. Support travel and service occupy the worker schedule. All statistical comparisons are conditional on the assumed staffing, deadlines, task mix, and facility capacities. They are not validated estimates of benefit at the source hospital.

The prepared file data/processed/combined_parameters.json is sufficient to rerun the simulations without external downloads. It contains the observed request profiles, route estimates, pathway network, and synthetic task mix. No MIMIC patient records are included. It is a prepared simulation input, not the original data release. Data provenance and licensing boundaries are documented in DATA_SOURCES.txt. The software license does not replace the source data terms.

The five original confirmation manifests and the deadline benchmark manifest retain historical hashes. PACKAGE_MANIFEST.json records every file delivered in this package. Git attributes preserve file bytes so those hashes remain valid on Windows and Unix. The verify command checks the delivered references, so it will report differences after a simulation rerun changes raw outputs or manifests. Use a fresh extraction when checking the original references again. Analysis preserves the delivered raw files and historical manifests, so verification remains available after generating figures and tables.

Programs can also be run individually. scripts/run_operational_study.py runs or summarizes a selected confirmation design. scripts/run_reconciled_benchmark.py runs the simpler deadline comparator after both main grids exist. scripts/analyze_reconciled_benchmarks.py performs direct paired contrasts. scripts/derive_structural_comparisons.py computes pickup and elevator comparisons. The two figure programs generate the main grid tables and plots and the elevator sensitivity plot. scripts/reproduce.py runs these in the required order.

Cite the data DOI addresses in DATA_SOURCES.txt. No journal publication DOI or public software DOI has been assigned to this revision. Add the eventual repository URL and software archive DOI after publication if available.
