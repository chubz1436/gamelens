"""Command line entry point.

    python -m gamelens --list
    python -m gamelens --target "Game Window Title"
    python -m gamelens --target 132638 --backend printwindow
"""

from __future__ import annotations

import argparse
import logging
import sys

import uvicorn

from gamelens import DPI
from gamelens.agent import Agent, VisionTier
from gamelens.app import GameLens
from gamelens.capture import Backend
from gamelens.server import create_app
from gamelens.session import agent_token_path, clear_agent_token, publish_agent_token
from gamelens.windows import AmbiguousTarget, find_window, list_windows


def _print_windows() -> int:
    windows = list_windows()
    if not windows:
        print("no capturable windows found")
        return 1
    print(f"{'hwnd':>10}  {'size':>12}  {'scale':>5}  {'fg':>3}  title")
    print("-" * 78)
    for w in windows:
        print(
            f"{w.hwnd:>10}  {w.width:>5}x{w.height:<6}  "
            f"{w.dpi_scale:>5.2f}  {'yes' if w.foreground else '':>3}  "
            f"{w.title[:38]}  [{w.exe}]"
        )
    return 0


def _port_is_free(port: int, host: str = "127.0.0.1") -> bool:
    """Can we actually bind? Asked before anything expensive starts.

    No SO_REUSEADDR: the question is whether uvicorn will succeed in a moment,
    and reusing the address here would answer a different, more optimistic
    question than the one that matters.
    """
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((host, port))
        except OSError:
            return False
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="gamelens",
        description="Low-latency window capture and input harness for AI agents.",
    )
    parser.add_argument("--list", action="store_true", help="list capturable windows and exit")
    parser.add_argument("--target", help="window title, substring, or hwnd")
    parser.add_argument("--port", type=int, default=8777)
    parser.add_argument("--rate", type=float, default=10.0, help="max actions per second")
    parser.add_argument("--pool-depth", type=int, default=4)
    parser.add_argument(
        "--backend", choices=[b.value for b in Backend],
        help="force one capture backend; disables automatic failover (testing only)",
    )
    parser.add_argument("--goal", default="play the game", help="what the vision tier should pursue")
    parser.add_argument("--vision-fps", type=float, default=2.0)
    parser.add_argument("--no-agent", action="store_true", help="capture and serve only")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)-18s %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.list:
        return _print_windows()

    if not args.target:
        parser.error("--target is required (or use --list to see what is open)")

    if not DPI.trustworthy:
        print(f"refusing to start: {DPI.reason}", file=sys.stderr)
        return 2

    try:
        target = find_window(args.target)
    except AmbiguousTarget as exc:
        print(exc, file=sys.stderr)
        return 2
    except LookupError as exc:
        print(exc, file=sys.stderr)
        return 2

    if not _port_is_free(args.port):
        # Checked here rather than left to uvicorn. Binding is the *last* thing
        # that happens, so a busy port meant capture had already started, a
        # worker process had been spawned and a fresh pair of one-time tokens
        # had been printed -- for a server that then exited without ever
        # listening. The tokens are the part that matters: a console full of
        # credentials that belong to nothing is how the wrong one gets pasted.
        print(
            f"refusing to start: port {args.port} is already in use.\n"
            f"Another GameLens is probably still running -- note that the kill "
            f"switch latches the process rather than stopping it, so a killed "
            f"session still holds the port. Stop it, or pass --port.",
            file=sys.stderr,
        )
        return 2

    lens = GameLens(
        target,
        port=args.port,
        rate=args.rate,
        pool_depth=args.pool_depth,
        backend=Backend(args.backend) if args.backend else None,
    )
    lens.start()

    if not args.no_agent:
        agent = Agent(
            lens,
            goal=args.goal,
            reflexes=[],                 # add project-specific reflexes here
            vision=VisionTier(),
            vision_fps=args.vision_fps,
        )
        lens.attach_agent(agent)
        agent.start()

    app = create_app(lens)
    token_path = agent_token_path(args.port)
    try:
        publish_agent_token(token_path, lens.tokens.agent)
    except Exception:
        lens.stop()
        raise
    # flush: this is the only time the tokens are ever shown, and stdout is
    # block-buffered when it is not a terminal, so a redirected log would
    # otherwise withhold them until the process exits.
    print(lens.tokens.banner("127.0.0.1", args.port), flush=True)
    print(f"  Agent token handoff (Windows-encrypted): {token_path}", flush=True)
    print("  Kill switch: F12 or Pause. It latches -- restart to clear it.\n", flush=True)

    try:
        uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    except KeyboardInterrupt:
        pass
    finally:
        lens.stop()
        clear_agent_token(token_path, lens.tokens.agent)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
