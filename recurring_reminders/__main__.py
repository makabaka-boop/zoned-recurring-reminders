"""Run the HTTP API:  python -m recurring_reminders --db reminders.db --port 8080

Options:
  --db PATH        SQLite database file (default: reminders.db)
  --port N         listen port (default: 8080)
  --manual-clock ISO
                   use a controllable clock starting at the given UTC time
                   instead of the system clock; advance it via POST /tick
                   {"now": "..."}
"""
from __future__ import annotations

import argparse

from .api import make_server
from .clock import ManualClock
from .service import Service, parse_utc


def main() -> None:
    parser = argparse.ArgumentParser(prog="recurring_reminders")
    parser.add_argument("--db", default="reminders.db")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--manual-clock", metavar="ISO", default=None)
    args = parser.parse_args()

    clock = ManualClock(parse_utc(args.manual_clock)) if args.manual_clock else None
    service = Service(args.db, clock=clock)
    server = make_server(service, port=args.port)
    print(f"serving on http://127.0.0.1:{args.port} (db={args.db})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        service.close()


if __name__ == "__main__":
    main()
