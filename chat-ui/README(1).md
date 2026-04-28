# Data Analysis Chat UI — React

Drop-in replacement for the Streamlit frontend. Connects to the **same backend API** (FastAPI + Swagger).

## Quick Start

### Option A: Add to existing React project
```bash
# Copy DataAnalysisChat.jsx into your src/ folder
cp DataAnalysisChat.jsx src/components/DataAnalysisChat.jsx
```

In your `App.jsx`:
```jsx
import DataAnalysisChat from "./components/DataAnalysisChat";
export default function App() {
  return <DataAnalysisChat />;
}
```

### Option B: New project with Vite
```bash
npm create vite@latest chat-ui -- --template react
cd chat-ui
cp ../DataAnalysisChat.jsx src/DataAnalysisChat.jsx
```

Edit `src/App.jsx`:
```jsx
import DataAnalysisChat from "./DataAnalysisChat";
export default function App() {
  return <DataAnalysisChat />;
}
```

```bash
npm install
npm run dev
```

## API Endpoints Used

The component calls your existing backend at `http://localhost:8000` (configurable in the sidebar):

| Method | Path                  | Purpose              |
|--------|-----------------------|----------------------|
| GET    | `/health`             | Backend status check |
| GET    | `/sources`            | List data sources    |
| GET    | `/feedback/learnings` | AI learning rules    |
| POST   | `/chat`               | Send query + history |
| POST   | `/feedback`           | Submit rating        |
| POST   | `/cache/clear`        | Clear query cache    |

### POST `/chat` — Request
```json
{
  "query": "how many deals?",
  "history": [
    { "role": "user", "content": "..." },
    { "role": "assistant", "content": "..." }
  ]
}
```

### POST `/chat` — Response
```json
{
  "answer": "There are 42 deals.",
  "data": { ... },
  "sources_used": ["deals"],
  "confidence": 0.95,
  "processing_time_ms": 230,
  "agent_time_ms": 180,
  "query_used": { ... },
  "query_plan": {
    "intent": "count",
    "collection": "deals",
    "filters": {},
    "output_format": "text"
  }
}
```

## CORS

Add CORS to your FastAPI backend if not already present:

```python
from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # or your frontend URL
    allow_methods=["*"],
    allow_headers=["*"],
)
```

## Customization

Edit CSS variables at the top of the root `<div>` in `DataAnalysisChat.jsx`:

```css
--accent: #2563eb;        /* Primary brand color */
--surface: #fafbfc;       /* Page background */
--surface-raised: #f1f3f5; /* Cards / bubbles */
--border: #e2e5ea;        /* Borders */
--text-primary: #1a1d23;  /* Main text */
```

## No Dependencies

The component uses only React (useState, useEffect, useRef, useCallback). No extra packages needed — just drop it in.
