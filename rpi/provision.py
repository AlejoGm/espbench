#!/usr/bin/env python3
"""
WiFi provisioning AP para SfyBench Pi.
Levanta hotspot via nmcli, sirve portal web, conecta via nmcli al recibir credenciales.

Uso: sudo python3 provision.py [--ssid NAME] [--pass PASS]
"""

import subprocess
import json
import time
import threading
import sys
import os
import argparse
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

PORTAL_HTML = Path(__file__).parent / 'portal' / 'index.html'
CON_NAME = 'sfybench-hotspot'

_networks_cache = []


def sh(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True)


def scan_networks():
    sh('nmcli device wifi rescan ifname wlan0')
    time.sleep(4)
    r = sh("nmcli -t -f SSID,SECURITY device wifi list ifname wlan0")
    seen, nets = set(), []
    for line in r.stdout.strip().splitlines():
        parts = line.split(':')
        if len(parts) < 2:
            continue
        ssid = parts[0].strip()
        sec  = parts[1].strip()
        if not ssid or ssid in seen:
            continue
        seen.add(ssid)
        nets.append({'ssid': ssid, 'security': 'none' if sec == '--' else 'wpa'})
    return nets


def start_hotspot(ssid, passphrase):
    sh(f"nmcli connection delete '{CON_NAME}' 2>/dev/null")
    r = sh(
        f'nmcli device wifi hotspot ifname wlan0 con-name "{CON_NAME}" '
        f'ssid "{ssid}" password "{passphrase}"'
    )
    if r.returncode != 0:
        print(f'[ERROR] hotspot failed: {r.stderr.strip()}')
        sys.exit(1)
    print(f'[OK]   AP "{ssid}" levantado')


def _do_connect(ssid, passphrase):
    time.sleep(0.8)  # dejar que la respuesta HTTP llegue al cliente
    print(f'[INFO] Conectando a "{ssid}"...')
    sh(f"nmcli connection delete '{CON_NAME}' 2>/dev/null")
    sh('nmcli device disconnect wlan0 2>/dev/null')
    time.sleep(2)
    r = sh(f'nmcli device wifi connect "{ssid}" password "{passphrase}"')
    if r.returncode == 0:
        print(f'[OK]   Conectado a "{ssid}". Saliendo.')
        os._exit(0)
    else:
        print(f'[ERROR] Falló: {r.stderr.strip()}')
        print('[INFO] Relanzando hotspot...')
        start_hotspot(ssid, passphrase)


class PortalHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == '/networks':
            body = json.dumps(_networks_cache).encode()
            self._send(200, 'application/json', body)
        else:
            # Cualquier otra ruta → portal HTML (captive portal redirect)
            try:
                body = PORTAL_HTML.read_bytes()
                self._send(200, 'text/html; charset=utf-8', body)
            except Exception as e:
                self.send_error(500, str(e))

    def do_POST(self):
        if self.path == '/connect':
            n = int(self.headers.get('Content-Length', 0))
            try:
                data = json.loads(self.rfile.read(n))
                ssid       = data.get('ssid', '').strip()
                passphrase = data.get('passphrase', '').strip()
                if not ssid:
                    self.send_error(400, 'ssid requerido')
                    return
                self._send(200, 'application/json', b'{"ok":true}')
                threading.Thread(
                    target=_do_connect,
                    args=(ssid, passphrase),
                    daemon=True,
                ).start()
            except Exception as e:
                self.send_error(400, str(e))
        else:
            self.send_error(404)

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)


def main():
    global _networks_cache

    parser = argparse.ArgumentParser()
    parser.add_argument('--ssid', default=os.environ.get('AP_SSID', 'SfyBench'))
    parser.add_argument('--pass', dest='passphrase',
                        default=os.environ.get('AP_PASS', '1234567890'))
    args = parser.parse_args()

    print('[INFO] Escaneando redes WiFi...')
    _networks_cache = scan_networks()
    print(f'[INFO] {len(_networks_cache)} redes encontradas')

    start_hotspot(args.ssid, args.passphrase)
    time.sleep(3)  # esperar que dnsmasq levante

    print('[INFO] Portal en 0.0.0.0:80')
    try:
        HTTPServer(('0.0.0.0', 80), PortalHandler).serve_forever()
    except PermissionError:
        print('[ERROR] Puerto 80 requiere root (sudo)')
        sys.exit(1)


if __name__ == '__main__':
    main()
