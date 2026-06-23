import hashlib, json, pathlib, struct

SHA256_READ_CHUNK = 1024 * 1024

def sha256_file(p: pathlib.Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(SHA256_READ_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()

def send_msg(sock, obj: dict):
    payload = json.dumps(obj).encode("utf-8")
    sock.sendall(struct.pack(">I", len(payload)) + payload)

def recv_msg(sock) -> dict:
    raw = _recv_exact(sock, 4)
    n = struct.unpack(">I", raw)[0]
    return json.loads(_recv_exact(sock, n).decode("utf-8"))

def _recv_exact(sock, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("socket closed prematurely")
        buf += chunk
    return buf
