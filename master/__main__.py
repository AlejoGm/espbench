"""python -m master  (o master/bench-master, que además arma el venv)."""
import argparse
import threading
import webbrowser

import uvicorn

from master.app import LOCAL_HOSTS, create_app


def main():
    ap = argparse.ArgumentParser(prog="bench-master", description="Todos los benches de espbench en un solo lugar")
    ap.add_argument("--host", default="127.0.0.1",
                    help="default 127.0.0.1: el proxy expone la consola serie de todos los benches, sin auth")
    ap.add_argument("--port", type=int, default=8090)
    ap.add_argument("--poll", type=float, default=5.0, help="segundos entre scans de benches")
    ap.add_argument("--open", action="store_true", help="abrir el navegador")
    args = ap.parse_args()
    if args.open:
        url = f"http://{'localhost' if args.host in ('127.0.0.1', '0.0.0.0') else args.host}:{args.port}/"
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    # Con --host 0.0.0.0 el Host puede ser cualquier nombre de la máquina: sin chequeo
    # de Host (queda el de Origin). Ojo: ahí cualquiera de la red llega a las consolas.
    allowed = None if args.host in ("0.0.0.0", "::") else LOCAL_HOSTS | {args.host}
    uvicorn.run(create_app(poll_s=args.poll, allowed_hosts=allowed), host=args.host, port=args.port,
                log_level="warning")


if __name__ == "__main__":
    main()
