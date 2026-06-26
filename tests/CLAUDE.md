# tests/

pytest test suite. Runs on dev machine (host), no Pi required.

## Files

| File | Tests |
|------|-------|
| `test_common.py` | SHA256, `send_msg`/`recv_msg` framing |
| `test_flash.py` | `build_esptool_cmd()` with various `flasher_args.json` formats |
| `test_artifact.py` | ZIP artifact creation + SHA256 verification |
| `test_device_registry.py` | Device discovery, port calculation (`5000+N`), status detection |
| `test_log_streamer.py` | Log tail, WebSocket subscribe/unsubscribe, broadcast, CHIPID parsing |

## Run

```bash
pytest tests/
```

## Notes

- `test_device_registry.py` patches `tmux has-session` and `/dev/ttyUSB*` — no actual devices needed
- `test_log_streamer.py` tests async WebSocket broadcast logic; mocks file I/O
- `test_flash.py` covers edge cases: dict offsets, list offsets, glob fallback
