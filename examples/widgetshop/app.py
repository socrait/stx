"""The trivial app in stx's synthetic example repo.

Fictional end to end: no real widgets, no real shop. Exists only so the
repo's `sandbox.yaml`/`compose.yaml`/Dockerfile have something plausible to
build and run, for stx's own docs and tests — never installed or started by
the test suite itself.
"""

from flask import Flask

app = Flask(__name__)


@app.get("/")
def index() -> str:
    return "widgetshop: nothing for sale here, this is a fixture"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
