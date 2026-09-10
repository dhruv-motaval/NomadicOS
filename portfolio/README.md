# NomadicOS Architecture Explorer

An interactive, pannable/zoomable diagram of the NomadicOS architecture,
built with React, D3 (for pan/zoom), Tailwind CSS, and Vite.

## Run it locally

You need Node.js 18 or newer.

```bash
npm install
npm run dev
```

Open the URL it prints (usually `http://localhost:5173`).

To produce an optimized production build (this is also what Vercel runs
automatically):

```bash
npm run build
npm run preview   # serve the built version locally to double check it
```

## Deploy to Vercel

You don't need Git for either of these.

### Option A — drag and drop the folder (simplest)

1. Go to **vercel.com/drop**
2. Drag this whole project folder onto the page (or a zip of it)
3. Pick a team + project name, then hit **Deploy**

Vercel detects it's a Vite project and builds it automatically.

### Option B — Vercel CLI

```bash
npm install -g vercel
cd nomadicos-explorer
vercel
```

Answer the prompts (link or create a project), and it deploys. Run
`vercel --prod` afterwards to push it to your production URL.

### Option C — GitHub (best if you'll keep editing it)

Push this folder to a GitHub repo, then import it at **vercel.com/new**.
Every push to `main` auto-deploys after that. Vercel auto-detects Vite,
so no extra config is needed.

## Editing the data

Everything the explorer shows (nodes, edges, layers, the task-lifecycle
steps) comes from one object near the top of `src/App.jsx`:

```js
const ARCHITECTURE_DATA = { ... };
```

Edit that object (or paste a regenerated version over it) and every view
updates automatically — layout, filters, search, and the detail panel all
derive from this data, including reasonable placement for any new node
you add without giving it x/y coordinates.

## Project structure

```
├── index.html          entry HTML
├── src/
│   ├── main.jsx         mounts the app
│   ├── App.jsx           the explorer component + embedded data
│   └── index.css         Tailwind entry point
├── package.json
├── vite.config.js
├── tailwind.config.js
└── postcss.config.js
```
