.PHONY: test lint typecheck run

test:
\tpytest -q

lint:
\truff check trader tests

typecheck:
\tmypy trader

run:
\tpython -m trader run --config config/paper.example.yaml
