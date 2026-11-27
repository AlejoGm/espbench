"""python -m master  (o master/bench-master, que además arma el venv)."""
import argparse
import threading
import webbrowser

import uvicorn

from master.app import LOCAL_HOSTS, create_app


def allowed_hosts(host: str, extra=()) -> "set | None":
    """Hosts que acepta el guard (app._OnlyFromThisPage): localhost, el --host y los --allow-host (el nombre
    del bench que lo corre: `sensibox`, `sensibox.<tailnet>.ts.net`). Con --host 0.0.0.0 / :: no hay chequeo
    de Host (queda el de Origin): ahí cualquiera de la red llega a las consolas."""
    if host in ("0.0.0.0", "::"):
        return None
    names = {h.strip().lower() for item in extra for h in item.split(",") if h.strip()}
    return LOCAL_HOSTS | {host} | names


def main():
    ap = argparse.ArgumentParser(prog="bench-master", description="Todos los benches de espbench en un solo lugar")
    ap.add_argument("--host", default="127.0.0.1",
                    help="default 127.0.0.1: el proxy expone la consola serie de todos los benches, sin auth")
    ap.add_argument("--port", type=int, default=8090)
    ap.add_argument("--poll", type=float, default=5.0, help="segundos entre scans de benches")
    ap.add_argument("--open", action="store_true", help="abrir el navegador")
    ap.add_argument("--allow-host", action="append", default=[], metavar="NOMBRE[,NOMBRE]",
                    help="otros nombres con los que se entra (corriendo en un bench: su nombre de Tailscale)")
    args = ap.parse_args()
    if args.open:
        url = f"http://{'localhost' if args.host in ('127.0.0.1', '0.0.0.0') else args.host}:{args.port}/"
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    allowed = allowed_hosts(args.host, args.allow_host)
    uvicorn.run(create_app(poll_s=args.poll, allowed_hosts=allowed), host=args.host, port=args.port,
                log_level="warning")


if __name__ == "__main__":
    main()
