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

def mac_to_sn_sfy(mac_str: str) -> str:
    """MAC address → Sensify SN (reversed bytes as decimal integer string)."""
    mac_str = mac_str.replace(":", "").replace("-", "").upper()
    if len(mac_str) != 12:
        raise ValueError("MAC debe tener 12 caracteres hex.")
    bytes_reversed = [mac_str[i:i+2] for i in range(0, 12, 2)][::-1]
    return str(int("".join(bytes_reversed), 16))

def sn_sfy_to_mac(sn_str: str, with_colons: bool = True) -> str:
    """Sensify SN → MAC address."""
    hex_str = f"{int(sn_str):012X}"
    bytes_original = [hex_str[i:i+2] for i in range(0, 12, 2)][::-1]
    return ":".join(bytes_original) if with_colons else "".join(bytes_original)

def hw_model_from_project_name(project_name: str) -> str:
    """'NVC3-55_0' → 'NVC3'  (split on last '-', take model part)."""
    idx = project_name.rfind("-")
    return project_name[:idx] if idx >= 0 else project_name
