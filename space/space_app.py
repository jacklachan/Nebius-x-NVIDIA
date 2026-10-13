"""Entry point for the Hugging Face Space.

Free Hugging Face accounts can only run a Python Space on ZeroGPU hardware,
and ZeroGPU will not start without at least one ``@spaces.GPU`` function.
Hindsight does no GPU work (the models run on Nebius Token Factory), so that
function is never called and the FastAPI app is served unchanged.
"""

import subprocess
import sys

import spaces

# ponytail: the Space image installs gradio[mcp], which pins mcp<2, while
# Hindsight's MCP server needs mcp 2, so the build cannot ask for it. Upgrade
# at boot (a few seconds per cold start); Gradio's own MCP support is unused.
# Pinned to the version the tests run on, so a new release cannot break a
# restart mid-judging. Drop this when gradio allows mcp 2 or the Space moves
# to the Docker SDK.
MCP_VERSION = "2.3.0"
subprocess.run([sys.executable, "-m", "pip", "install", "-q", "--root-user-action=ignore",
                f"mcp=={MCP_VERSION}"], check=True)
# `import spaces` already loaded mcp 1 through gradio; forget it so the app
# imports mcp 2 from disk instead of a mix of both.
for name in [m for m in sys.modules if m == "mcp" or m.startswith("mcp.")]:
    del sys.modules[name]

import uvicorn  # noqa: E402

from app import app  # noqa: E402


@spaces.GPU(duration=1)
def _noop() -> None:  # required by ZeroGPU; never called
    pass


if __name__ == "__main__":
    # ponytail: ZeroGPU reports its decorated functions from inside
    # gr.Blocks.launch, which this app never calls, so report directly. Relies
    # on a spaces internal; if a spaces upgrade breaks it, serve through
    # gr.Server instead.
    from spaces.config import Config

    if Config.zero_gpu:
        from spaces.zero import client

        client.startup_report()
    uvicorn.run(app, host="0.0.0.0", port=7860)
