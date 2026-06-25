import pytest, pathlib, socket, tempfile, sys
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
from common import sha256_file, send_msg, recv_msg

def test_sha256_file():
    with tempfile.NamedTemporaryFile(delete=False, suffix=".bin") as f:
        f.write(b"hello world")
        p = pathlib.Path(f.name)
    import hashlib
    expected = hashlib.sha256(b"hello world").hexdigest()
    assert sha256_file(p) == expected
    p.unlink()

def test_send_recv_roundtrip():
    a, b = socket.socketpair()
    try:
        obj = {"action": "test", "value": 42, "nested": {"x": True}}
        send_msg(a, obj)
        result = recv_msg(b)
        assert result == obj
    finally:
        a.close(); b.close()

def test_recv_msg_closed_socket():
    a, b = socket.socketpair()
    a.close()
    with pytest.raises(Exception):
        recv_msg(b)
    b.close()
