# remote/dashboard/

Web UI static files. Served by `server/dashboard.py` (FastAPI static mount).

## Files

| File | Purpose |
|------|---------|
| `index.html` | Home page: device cards grid |
| `device.html` | Device detail: serial log viewer with WebSocket |
| `style.css` | Responsive grid, dark theme, card styling |

No build step. Vanilla HTML/CSS/JS — no framework, no bundler.

## index.html

- Polls `/api/devices` every 5 seconds
- Card per device: key (editable), hw_model, fw version, IDF, deployer, SN, TTY, last flash, TCP port, status badge, lock indicator
- Groups: known devices (have MAC) vs unknown
- "Ver monitor" button → `device.html?tty=...`
- Inline rename: PATCH `/api/devices/{mac}`

## device.html

- Query param: `?tty=ttyUSB0`
- WebSocket to `/ws/device/{tty}`
- Shows full day log + live stream
- Auto-scroll on new content
- Back link to home

## style.css

- CSS Grid for card layout (responsive)
- Dark color scheme (no Bootstrap/Tailwind dependency)
- Animations + hover effects on cards

## Notes

- All API calls target same host/port as page origin (no hardcoded URLs)
- No auth on dashboard — internal network use only
