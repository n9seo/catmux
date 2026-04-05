#!/usr/bin/env python3
"""
catmux - CAT serial multiplexer for amateur radio transceivers

Usage:
    catmux [--config FILE] [--status] [--debug] [--version]

    catmux                              # catmux.toml in current dir
    catmux -c config/ft991a.toml        # specify config
    catmux -c config/kx3.toml --debug   # verbose frame logging
    catmux -c config/ft991a.toml -s     # print status every 5s

Supported rig families (set in config):
    yaesu     FT-991A, FT-dx101, FT-710, FT-450, FT-991 ...
    elecraft  KX3, KX2, K3, K3S, K4
    kenwood   TS-590, TS-2000, TS-890, TS-990 ...
    icom      IC-7100, IC-7300, IC-7600 ... (CI-V; limited testing)
"""

import argparse
import logging
import signal
import sys
import time
from pathlib import Path

try:
    import tomllib          # Python 3.11+
except ImportError:
    try:
        import tomli as tomllib
    except ImportError:
        print("ERROR: tomllib not available.")
        print("       Python 3.11+ includes it, or:  pip install tomli")
        sys.exit(1)

from catmux.broker import Broker


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config(path: Path) -> dict:
    if not path.exists():
        print(f"ERROR: config file not found: {path}")
        sys.exit(1)
    with open(path, "rb") as f:
        return tomllib.load(f)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logging(config: dict, debug: bool = False):
    log_cfg  = config.get("log", {})
    level    = logging.DEBUG if debug else getattr(
        logging, log_cfg.get("level", "INFO").upper(), logging.INFO)
    fmt      = "%(asctime)s %(levelname)-8s %(name)-22s %(message)s"
    datefmt  = "%H:%M:%S"

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    log_file = log_cfg.get("file", "").strip()
    if log_file:
        handlers.append(logging.FileHandler(log_file))

    logging.basicConfig(level=level, format=fmt, datefmt=datefmt, handlers=handlers)

    # Quieten pyserial's internal logging unless we're debugging
    if not debug:
        logging.getLogger("serial").setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# Status display
# ---------------------------------------------------------------------------

def print_status(broker: Broker):
    s = broker.status()
    W = 62
    print(f"\n{'─'*W}")
    print(f"  catmux  │  {s['rig_family'].upper()}  │  "
          f"{s['port']} @ {s['baud']}  │  "
          f"{'CONNECTED' if s['connected'] else 'DISCONNECTED'}")
    print(f"  queue depth: {s['queue_depth']}")
    print(f"{'─'*W}")

    print("  Virtual ports:")
    for vp in s["vports"]:
        rts = "RTS" if vp["rts"] else "   "
        dtr = "DTR" if vp["dtr"] else "   "
        print(f"    [{vp['name']:12s}]  {vp['symlink']:<26s}  "
              f"{rts} {dtr}  pri={vp['priority']}")

    print(f"{'─'*W}")
    print(f"  Mirror cache  ({len(s['mirror'])} keys):")
    for key, entry in sorted(s["mirror"].items()):
        stale = " !" if entry["stale"] else "  "
        val   = entry["value"]
        if isinstance(val, bytes):
            try:
                val = val.decode("ascii").strip()
            except Exception:
                val = val.hex(" ")
        print(f"  {stale} {key:<14s}  {str(val):<34s}  {entry['age_s']:5.1f}s")
    print(f"{'─'*W}\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="catmux — CAT serial multiplexer for amateur radio",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-c", "--config",
        default="catmux.toml",
        metavar="FILE",
        help="TOML config file (default: catmux.toml)",
    )
    parser.add_argument(
        "-s", "--status",
        action="store_true",
        help="Print mirror status every 5 seconds",
    )
    parser.add_argument(
        "-d", "--debug",
        action="store_true",
        help="Enable DEBUG logging (shows every CAT frame)",
    )
    parser.add_argument(
        "-v", "--version",
        action="store_true",
        help="Print version and exit",
    )
    args = parser.parse_args()

    if args.version:
        from catmux import __version__
        print(f"catmux {__version__}")
        return

    config = load_config(Path(args.config))
    setup_logging(config, debug=args.debug)
    log = logging.getLogger("catmux")

    log.info(
        f"catmux starting  rig={config['rig']['family']}  "
        f"port={config['rig']['port']}"
    )

    broker = Broker(config)

    # Graceful shutdown on Ctrl-C or SIGTERM
    def _shutdown(sig, frame):
        log.info(f"Signal {sig} received — shutting down")
        broker.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        broker.start()          # single call — no separate start_vport_readers()
    except PermissionError as e:
        port  = config['rig']['port']
        group = config['rig'].get('serial_group', 'dialout')
        log.error(
            f"Permission denied: {e}\n"
            f"  Fix: sudo usermod -aG {group} $USER  (then log out/in)\n"
            f"  Or:  sudo chmod a+rw {port}"
        )
        sys.exit(1)
    except Exception as e:
        log.error(f"Startup failed: {e}", exc_info=args.debug)
        sys.exit(1)

    log.info("catmux running — Ctrl-C or SIGTERM to stop")

    # Main loop
    status_interval = 5.0
    last_status     = 0.0

    while True:
        time.sleep(0.5)
        if args.status:
            now = time.monotonic()
            if now - last_status >= status_interval:
                print_status(broker)
                last_status = now


if __name__ == "__main__":
    main()
