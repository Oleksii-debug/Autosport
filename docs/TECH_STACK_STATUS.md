# Technology stack status

Language/runtime: Python 3.12 baseline. Windows/web shell candidate: pywebview/WebView2. Front-end: semantic plain HTML/JavaScript first, framework only if measured complexity justifies it. Persistent event/history store: not locked; DuckDB/Parquet is a researched candidate pending benchmarks. Analytical pipeline: Polars candidate pending benchmarks. Portfolio solver: deliberately not locked pending clean interface/exact oracle. CI: GitHub Actions Windows + Ubuntu. Release packager: not yet selected/proven.
