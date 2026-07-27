# Financial Agent Demo UI Design

## Goal

Provide a lightweight visual demo for the existing financial investigation Agent:

1. Double-click `启动记账系统.cmd`.
2. Click “启动并打开财务 Agent”.
3. Ask a question on `/agent`.
4. See the answer, tool steps, statuses, and `trace_id` on the same page.

The UI must remain easy for interview demonstrations and open-source users. It must not add a
frontend toolchain.

The project remains a backend + Agent project. The page is only a thin demonstration shell:
Agent planning, tool execution, validation, money calculations, limits, and auditing all stay in
the Python backend.

## Scope

### Included

- A standalone `/agent` page.
- Native HTML, CSS, and JavaScript served by FastAPI.
- One-question-at-a-time submission to the existing `POST /api/agent/query`.
- A conversation-style question and answer area.
- A visible investigation-step panel using the API response.
- Loading, validation, authentication, and backend-error states.
- A launcher button that starts services and opens `/agent`.
- Existing `/review` and Firefly III launcher buttons remain available.

### Excluded

- React, Vue, npm, or a separate frontend service.
- Streaming output.
- Persistent chat history or a new database table.
- Markdown rendering.
- Accounts, multi-user sessions, RAG, or additional Agent tools.

## Architecture

Keep the UI with the existing Agent route module:

- `app/api/routes_agent.py`
  - Continue exposing the JSON API router.
  - Add a page router for `GET /agent`.
  - Serve one self-contained HTML document.
  - Accept the existing console token through either `X-Console-Token` or the HttpOnly
    `console_token` cookie.
  - When `/agent?token=...` is valid, set the cookie and redirect to the clean `/agent` URL.
- `app/main.py`
  - Register the Agent page router without the `/api` prefix.
- `scripts/launcher.ps1`
  - Generate the authenticated `/agent` URL.
  - Change the primary action to “启动并打开财务 Agent”.
  - Keep a separate “打开记账复核台” action.

No new runtime dependency or migration is required.

## UI and Data Flow

The page contains:

- A header showing “只读” and “最多 3 步”.
- A question input and “开始调查” button.
- A conversation area where the submitted question appears above the answer.
- A step panel showing each tool, status, and observation summary.
- The final `trace_id` and stopped reason.

On submit, JavaScript:

1. Trims and validates the question.
2. Disables the form and shows a loading state.
3. Calls `/api/agent/query` with same-origin cookies.
4. Renders API fields with `textContent` only.
5. Restores the form and displays a short Chinese error when the request fails.

The page keeps results only in the current browser DOM. Refreshing clears them.
It performs no financial calculation and contains no Agent decision logic.

## Security and Failure Handling

- The token stays in an HttpOnly cookie and is never read by JavaScript.
- All question, answer, tool, and observation content is rendered with `textContent`, not
  `innerHTML`.
- The existing 500-character question limit remains the server-side trust boundary.
- API failures show a generic message; detailed backend errors are not exposed.
- The Agent remains read-only and retains its three-tool-call limit.

## Testing

Use TDD for:

- `/agent` renders the expected UI and API target.
- A valid query token sets an HttpOnly cookie and redirects to `/agent`.
- The JSON API accepts the valid cookie and rejects invalid authentication.
- Launcher self-test confirms the Agent button and generated URL.

Then run:

- `python -m ruff check .`
- `python -m pytest -q`
- `scripts/launcher.ps1 -Action SelfTest`

## Workload Boundary

This is a small presentation layer over the completed Agent. The implementation should touch the
Agent route, application router registration, launcher, and focused tests only. If a future version
needs streaming or persistent sessions, add them as separate features rather than expanding this
demo UI.
