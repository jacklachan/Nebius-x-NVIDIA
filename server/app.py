"""Alternative server entry point that reuses the root FastAPI app."""

try:
    from app import app
except ModuleNotFoundError:
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from app import app


def main(host: str = "0.0.0.0", port: int = 7860):
    """Run the Hindsight server directly."""
    import uvicorn

    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
