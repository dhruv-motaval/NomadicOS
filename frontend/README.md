# NomadicOS owner console

A React + Vite web console for the NomadicOS owner API. It launches goals, follows runs, answers owner conflicts, manages authority and lists models. It talks only to the API, which goes through the same authority and verification path as the CLI.

## Run it

Start the owner API from the repository root:

```bash
python -m nomadicos.api            # http://127.0.0.1:8090, loopback only
```

It prints an owner token and a link like `http://localhost:5173/#token=…`. Then start the console:

```bash
cd frontend
npm install
npm run dev                        # http://localhost:5173
```

Open the link the API printed. The console reads the token from the URL fragment, keeps it in `sessionStorage` for that tab only, and removes it from the address bar. A new API process mints a new token, so open the new link after each restart.

## How it connects

- In dev, Vite proxies `/api/*` to `http://127.0.0.1:8090` (see `vite.config.ts`).
- For a static build, set `VITE_API_BASE` to the API root. The API only allows CORS from the local dev origin, so a different origin also needs a change in `src/nomadicos/api/app.py`.
- Every request sends `Authorization: Bearer <owner token>`. All endpoints except `/api/health` reject requests without it.
- The typed API client is in `src/lib/api.ts`; the UI is in `src/App.tsx`.

## Tabs

| Tab | What it does |
| --- | --- |
| Goal | Launch a goal with constraints |
| Runs | In-flight and durable runs, summaries, resume |
| Conflicts | Pending owner conflicts, answered `ALLOW` / `DENY` |
| Authority | Grant `FULL_PC_AUTONOMY`, revoke all, edit owner instructions |
| Models | Discovered models and engine states |

## Scripts

| Command | What it does |
| --- | --- |
| `npm run dev` | Dev server with hot reload |
| `npm run build` | Type-check (`tsc -b`) and build to `dist/` |
| `npm run lint` | Lint with oxlint |
| `npm run preview` | Serve the built `dist/` locally |

UI changes follow the `ui-ux-pro-max` skill rules described in [`AGENTS.md`](../AGENTS.md).
