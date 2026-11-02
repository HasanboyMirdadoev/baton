### Code Review Findings

1. **[Critical] Subprocess stdout/stderr `StreamReader` 64 KiB line limit causes unhandled `ValueError`, orphaned CLI processes, and permanently hung turns/pipelines**
   - **Location**: [`backend/session_manager.py:183-278`](file:///Users/mac/MY/Benevia/agent-relay/backend/session_manager.py#L183-L278) ([`SessionManager._execute_turn`](file:///Users/mac/MY/Benevia/agent-relay/backend/session_manager.py#L136-L365))
   - **What's wrong**: `asyncio.create_subprocess_exec` is called without a `limit` argument (defaulting to `asyncio.streams._DEFAULT_LIMIT = 65536` bytes / 64 KiB), while `_read_stdout` (line 205) and `_read_stderr` (line 256) use `await proc.stdout.readline()`. Furthermore, the `try` block around `_consume_and_wait()` (lines 268–278) only catches `asyncio.TimeoutError` and `asyncio.CancelledError`, and the DB/SSE finalization logic (lines 340–359) is outside any `finally` or `except Exception` handler.
   - **Concrete failure scenario**: During a real `claude` or `agy` turn, the agent reads or writes a file > 64 KiB or emits a large `assistant` / `tool_result` / `init` NDJSON line exceeding 65,536 bytes. `proc.stdout.readline()` raises `ValueError: Separator is not found, and chunk exceed the limit` (wrapping `asyncio.LimitOverrunError`). The exception escapes `_consume_and_wait()` and `_execute_turn()` without calling `self._terminate_proc(proc)` (leaving the CLI process group running as an orphan) and without calling `self.db.update_turn()` or `self.db.update_session()`. Both the turn and session remain stuck in `status = "running"` in SQLite and in the UI forever (and any parent pipeline run hangs indefinitely).
   - **Exact fix**:
     1. In [`backend/session_manager.py:183-190`](file:///Users/mac/MY/Benevia/agent-relay/backend/session_manager.py#L183-L190), pass `limit=32 * 1024 * 1024` (32 MiB) to `asyncio.create_subprocess_exec`.
     2. Create a helper `_safe_readline(stream: asyncio.StreamReader) -> bytes` that catches `(ValueError, asyncio.LimitOverrunError)` and drains oversized lines in chunks until `\n` or EOF instead of raising.
     3. In [`backend/session_manager.py:268-278`](file:///Users/mac/MY/Benevia/agent-relay/backend/session_manager.py#L268-L278), add an `except Exception as exc:` handler that sets `spawn_error = f"Stream reader error: {exc}"` and calls `await self._terminate_proc(proc)` so the turn always reaches the DB update and SSE completion block (lines 280–360).
     4. In the `finally:` block at [`backend/session_manager.py:362-365`](file:///Users/mac/MY/Benevia/agent-relay/backend/session_manager.py#L362-L365), also clean up `self._active_tasks.pop(turn_id, None)` and `self._stopped_turns.discard(turn_id)`.

2. **[Critical] Real `claude` CLI error output (`is_api_error_message: true` and `subtype: "success"` with `is_error: true`) is rendered as normal assistant reply text, and `fake_claude.py` diverges from real output**
   - **Location**: [`backend/providers/claude.py:177-260`](file:///Users/mac/MY/Benevia/agent-relay/backend/providers/claude.py#L177-L260) ([`ClaudeAdapter.parse_stream_line`](file:///Users/mac/MY/Benevia/agent-relay/backend/providers/claude.py#L106-L292)) and [`tests/fixtures/fake_claude.py:74-85`](file:///Users/mac/MY/Benevia/agent-relay/tests/fixtures/fake_claude.py#L74-L85)
   - **What's wrong**:
     1. In real `claude` CLI output ([`docs/reference/samples/claude-turn1.jsonl:3-4`](file:///Users/mac/MY/Benevia/agent-relay/docs/reference/samples/claude-turn1.jsonl#L3-L4) and [`claude-turn2.jsonl:2-3`](file:///Users/mac/MY/Benevia/agent-relay/docs/reference/samples/claude-turn2.jsonl#L2-L3)), an unauthenticated or failed API turn emits:
        - Line 3: `{"type": "assistant", "message": {"model": "<synthetic>", "content": [{"type": "text", "text": "Not logged in · Please run /login"}]}, "error": "authentication_failed", "is_api_error_message": true}`
        - Line 4: `{"type": "result", "subtype": "success", "is_error": true, "result": "Not logged in · Please run /login"}` (no `"error"` key, and `"subtype"` is `"success"` even when `"is_error"` is `true`).
     2. In `ClaudeAdapter.parse_stream_line`, the `msg_type == "assistant"` branch (lines 177–196) does not check `obj.get("is_api_error_message")` or `obj.get("error")`. Because no `stream_event` deltas preceded the synthetic message (`state.saw_text_delta` is `False`), it emits `"Not logged in · Please run /login"` as a normal `TEXT_DELTA` and appends it to `state.accumulated_text`.
     3. In the `msg_type == "result"` branch (lines 251–260), `result_text = str(obj.get("result") or "".join(state.accumulated_text))` sets `state.final_response = "Not logged in · Please run /login"` even when `is_err` is `True`. `SessionManager._execute_turn` then writes `"Not logged in · Please run /login"` into **both** `turns.response_text` and `turns.error_message`, displaying the error twice in the UI (once as the assistant's markdown reply and once in `.turn-error-box`).
     4. [`tests/fixtures/fake_claude.py:74-84`](file:///Users/mac/MY/Benevia/agent-relay/tests/fixtures/fake_claude.py#L74-L84) diverges from real `claude` by omitting the synthetic `assistant` line and emitting `{"type": "result", "subtype": "error", "is_error": True, "error": "Fatal CLI error occurred"}`.
   - **Concrete failure scenario**: A user runs a turn on a Claude session when `claude` is not logged in (or hits an API error). The UI streams `"Not logged in · Please run /login"` as an assistant markdown message and then renders the exact same string again in the red error banner below it.
   - **Exact fix**:
     1. In [`backend/providers/claude.py:177-180`](file:///Users/mac/MY/Benevia/agent-relay/backend/providers/claude.py#L177-L180), if `obj.get("is_api_error_message") or obj.get("error")`, record `state.error_message = str(obj.get("error") or "")` (or extract the text block into `state.error_message`) and `return events` without emitting `TEXT_DELTA` or appending to `state.accumulated_text`.
     2. In [`backend/providers/claude.py:251-260`](file:///Users/mac/MY/Benevia/agent-relay/backend/providers/claude.py#L251-L260), when `is_err` is `True`:
        ```python
        if is_err:
            state.error_message = str(
                obj.get("error") or obj.get("result") or state.error_message or "Claude CLI reported an error"
            )
            result_text = "".join(state.accumulated_text)
        else:
            result_text = str(obj.get("result") or "".join(state.accumulated_text))
        state.final_response = result_text
        ```
     3. Update [`tests/fixtures/fake_claude.py:74-85`](file:///Users/mac/MY/Benevia/agent-relay/tests/fixtures/fake_claude.py#L74-L85) to emit the real two-line error sequence (`assistant` with `"is_api_error_message": True, "error": "authentication_failed"` followed by `result` with `"subtype": "success", "is_error": True, "result": "Fatal CLI error occurred"`).

3. **[Critical] Saving `provider_session_id` on `init` before Turn 1 succeeds causes subsequent turns to drop the role `system_prompt` and resume a failed/uninitialized conversation**
   - **Location**: [`backend/session_manager.py:153-229`](file:///Users/mac/MY/Benevia/agent-relay/backend/session_manager.py#L153-L229) ([`SessionManager._execute_turn`](file:///Users/mac/MY/Benevia/agent-relay/backend/session_manager.py#L136-L365))
   - **What's wrong**: Both `claude` ([`claude-turn1.jsonl:1`](file:///Users/mac/MY/Benevia/agent-relay/docs/reference/samples/claude-turn1.jsonl#L1)) and `agy` ([`agy-turn1.jsonl:1`](file:///Users/mac/MY/Benevia/agent-relay/docs/reference/samples/agy-turn1.jsonl#L1)) emit their `init` line with `session_id` / `conversation_id` at the start of stdout, before authentication or turn execution. `_read_stdout` (lines 215–228) immediately persists `state.provider_conversation_id` to `sessions.provider_session_id` in SQLite. If Turn 1 fails, times out, or is stopped before completing, `provider_session_id` remains non-null on the session. On Turn 2, `is_first_turn = session.get("provider_session_id") is None` (line 153) is `False`, so both [`ClaudeAdapter.build_command`](file:///Users/mac/MY/Benevia/agent-relay/backend/providers/claude.py#L86-L102) and [`AntigravityAdapter.build_command`](file:///Users/mac/MY/Benevia/agent-relay/backend/providers/antigravity.py#L72-L77) omit `system_prompt` and pass `--resume` / `--conversation`.
   - **Concrete failure scenario**: A user creates a Code Reviewer or Planner session and sends Turn 1 while not logged in (`claude-turn1.jsonl`) or stops Turn 1 immediately. After logging in and sending Turn 2, `is_first_turn` is `False`: the role's `system_prompt` (including the Code Reviewer's mandatory `VERDICT: APPROVED` / `VERDICT: CHANGES_REQUESTED` instructions) is never sent on Turn 2, and `--resume` / `--conversation` attempts to resume the failed Turn 1 ID.
   - **Exact fix**: In [`backend/session_manager.py:215-228`](file:///Users/mac/MY/Benevia/agent-relay/backend/session_manager.py#L215-L228), do not persist `provider_session_id` to SQLite inside `_read_stdout` on an unverified turn; instead, persist `provider_session_id = state.provider_conversation_id` in [`backend/session_manager.py:351-354`](file:///Users/mac/MY/Benevia/agent-relay/backend/session_manager.py#L351-L354) only when `final_turn_status == TurnStatus.COMPLETED` and `session.get("provider_session_id") is None` (or if updated early for live UI display, reset `provider_session_id = None` in SQLite when `is_first_turn` was `True` and `final_turn_status != TurnStatus.COMPLETED`).

4. **[Major] `pipeline_stage_runs.turn_id` remains `NULL` throughout stage execution, breaking live stage re-renders and causing `turn_completed` to blank out streamed stage output**
   - **Location**: [`backend/db.py:320-372`](file:///Users/mac/MY/Benevia/agent-relay/backend/db.py#L320-L372) ([`Database.create_turn`](file:///Users/mac/MY/Benevia/agent-relay/backend/db.py#L320-L372)), [`backend/pipeline_engine.py:315-343`](file:///Users/mac/MY/Benevia/agent-relay/backend/pipeline_engine.py#L315-L343) ([`PipelineEngine._execute_stage_turn`](file:///Users/mac/MY/Benevia/agent-relay/backend/pipeline_engine.py#L297-L414)), and [`frontend/js/runs.js:150-154`](file:///Users/mac/MY/Benevia/agent-relay/frontend/js/runs.js#L150-L154) & [`1065-1073`](file:///Users/mac/MY/Benevia/agent-relay/frontend/js/runs.js#L1065-L1073)
   - **What's wrong**:
     1. `_execute_stage_turn` creates the `pipeline_stage_runs` row with `turn_id = None` (line 315) and only updates `pipeline_stage_runs.turn_id` **after** `run_turn_and_wait` finishes.
     2. `renderTimelineStages` in [`frontend/js/runs.js:151-153`](file:///Users/mac/MY/Benevia/agent-relay/frontend/js/runs.js#L151-L153) reads cached live events via `appStore.liveTurnEvents[stageRun.turn_id]`. While the stage turn is running, `stageRun.turn_id` is `null`.
     3. When the stage turn finishes, `SessionManager._execute_turn` publishes `turn_completed` ([`backend/session_manager.py:357`](file:///Users/mac/MY/Benevia/agent-relay/backend/session_manager.py#L357)) **before** `PipelineEngine` calls `db.update_stage_run` and publishes `pipeline_stage_updated`. When `turn_completed` arrives at [`frontend/js/runs.js:1069-1072`](file:///Users/mac/MY/Benevia/agent-relay/frontend/js/runs.js#L1069-L1072), it calls `renderActiveRunPanel()` while `stageRun.turn_id` is still `null` and `stageRun.output_text` is still `""`.
     4. Similarly, in [`frontend/js/pipelines.js:410-428`](file:///Users/mac/MY/Benevia/agent-relay/frontend/js/pipelines.js#L410-L428) (`handleRunPipeline`) and [`frontend/js/runs.js:407-420`](file:///Users/mac/MY/Benevia/agent-relay/frontend/js/runs.js#L407-L420) (`approveActivePipelineRun`), `appStore.activeRunDetail = res` overwrites `stage_runs` with the initial HTTP response (`stage_runs: []` on start), discarding any `pipeline_stage_updated` SSE events that arrived while the HTTP response was in flight.
   - **Concrete failure scenario**: While a pipeline stage is streaming tool calls and markdown text in the Active Run view, the user switches tabs (or `refreshRuns()` / `turn_completed` fires). `renderActiveRunPanel()` rebuilds `#stage-runs-timeline`, sees `stageRun.turn_id === null` and `stageRun.output_text === ""`, and replaces all streamed tool cards and text with `"Stage turn is currently executing..."`.
   - **Exact fix**:
     1. In [`backend/db.py:369-370`](file:///Users/mac/MY/Benevia/agent-relay/backend/db.py#L369-L370) inside `Database.create_turn`, if `stage_run_id is not None`, execute:
        ```python
        if stage_run_id is not None:
            conn.execute(
                "UPDATE pipeline_stage_runs SET turn_id = ? WHERE id = ?",
                (turn_id, stage_run_id),
            )
        ```
     2. In [`frontend/js/runs.js:1065-1073`](file:///Users/mac/MY/Benevia/agent-relay/frontend/js/runs.js#L1065-L1073), also handle `turn_started` (and in `turn_event`) to set `stageRun.turn_id = turnId` on the matching `stage_run` in `appStore.activeRunDetail.stage_runs`, and in `pipeline_stage_updated` (line 1024), if `appStore.activeRunId === stageRun.pipeline_run_id` and `!appStore.activeRunDetail`, initialize `appStore.activeRunDetail = { run: { id: stageRun.pipeline_run_id }, stage_runs: [stageRun] }`.
     3. In [`frontend/js/pipelines.js:413-416`](file:///Users/mac/MY/Benevia/agent-relay/frontend/js/pipelines.js#L413-L416) and [`frontend/js/runs.js:411-415`](file:///Users/mac/MY/Benevia/agent-relay/frontend/js/runs.js#L411-L415), preserve any `stage_runs` already received via SSE if `res.stage_runs` is empty or older.

5. **[Major] `Database.get_turn` does not join `sessions`, causing Run History's Turn Inspector and live turn updates to show `"Session"` and `"Unknown"` badges**
   - **Location**: [`backend/db.py:417-423`](file:///Users/mac/MY/Benevia/agent-relay/backend/db.py#L417-L423) ([`Database.get_turn`](file:///Users/mac/MY/Benevia/agent-relay/backend/db.py#L417-L423)) and [`frontend/js/runs.js:619-674`](file:///Users/mac/MY/Benevia/agent-relay/frontend/js/runs.js#L619-L674) & [`1074-1085`](file:///Users/mac/MY/Benevia/agent-relay/frontend/js/runs.js#L1074-L1085)
   - **What's wrong**: [`Database.list_all_turns`](file:///Users/mac/MY/Benevia/agent-relay/backend/db.py#L433-L454) joins `sessions` (`SELECT t.*, s.name AS session_name, s.provider, s.model, s.role FROM turns t JOIN sessions s ON t.session_id = s.id`), whereas [`Database.get_turn`](file:///Users/mac/MY/Benevia/agent-relay/backend/db.py#L417-L423) runs `SELECT * FROM turns WHERE id = ?` without joining `sessions`. Both `GET /api/turns/{turn_id}` ([`backend/main.py:284`](file:///Users/mac/MY/Benevia/agent-relay/backend/main.py#L284)) and the `turn_started` / `turn_completed` SSE events use `db.get_turn()`.
   - **Concrete failure scenario**: The user opens **Run History → All Session Turns** and clicks any turn card. `renderHistoryDetail()` fetches `GET /api/turns/{turn_id}` and renders `turn.session_name || 'Session'`, `renderProviderBadge(turn.provider)`, `renderRoleBadge(turn.role)`, and `turn.model || ''` (lines 667–672). Because those four fields are missing from `db.get_turn()`, every turn detail header displays `"Session · Turn #1"`, `"Unknown"` provider pill, `"Unknown"` role pill, and a blank model badge. Likewise, when any turn finishes, `handleRunSseEvent` (line 1079) unshifts `data.turn` into `appStore.allTurns` with those fields missing.
   - **Exact fix**: Update [`Database.get_turn`](file:///Users/mac/MY/Benevia/agent-relay/backend/db.py#L417-L423) to join `sessions`:
     ```python
     def get_turn(self, turn_id: str) -> Optional[Dict[str, Any]]:
         with self._get_connection() as conn:
             cursor = conn.execute(
                 """
                 SELECT
                     t.*,
                     s.name AS session_name,
                     s.provider,
                     s.model,
                     s.role
                 FROM turns t
                 LEFT JOIN sessions s ON t.session_id = s.id
                 WHERE t.id = ?
                 """,
                 (turn_id,),
             )
             row = cursor.fetchone()
             if row is None:
                 return None
             return _turn_row_to_dict(row)
     ```

6. **[Major] `AntigravityAdapter.list_models()` regex `^[a-z0-9][a-z0-9._-]+$` matches ordinary English words in `agy models` output**
   - **Location**: [`backend/providers/antigravity.py:40-52`](file:///Users/mac/MY/Benevia/agent-relay/backend/providers/antigravity.py#L40-L52) and [`270-281`](file:///Users/mac/MY/Benevia/agent-relay/backend/providers/antigravity.py#L270-L281) ([`AntigravityAdapter.list_models`](file:///Users/mac/MY/Benevia/agent-relay/backend/providers/antigravity.py#L253-L303))
   - **What's wrong**: `list_models()` splits every line of `agy models` stdout by whitespace (`for raw_token in stripped_line.split()`) and accepts any token matching `_MODEL_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]+$")` not in the 10-word `_IGNORED_MODEL_WORDS` set. Because `[a-z0-9._-]+` does not require a hyphen (`-`), any lowercase word of 2+ characters in `agy models` output (such as display labels, column headers, or status text) matches and is added as a model ID. Additionally, in the `except Exception:` block at lines 282–287, `proc.kill()` is called without `await proc.wait()`, leaving a zombie process if `agy models` times out.
   - **Concrete failure scenario**: When `GET /api/meta` runs `agy models`, words from human-readable labels or messages on stdout (e.g., `"high"`, `"medium"`, `"low"`, `"flash"`, `"argon"`, `"current"`, `"session"`, `"configured"`) are added as separate entries in the Antigravity Model dropdown in the New Session modal.
   - **Exact fix**:
     1. In [`backend/providers/antigravity.py:40`](file:///Users/mac/MY/Benevia/agent-relay/backend/providers/antigravity.py#L40), require at least one hyphen in model IDs:
        ```python
        _MODEL_ID_RE = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)+$")
        ```
        and verify `"-"` is in `token` before appending to `discovered_ids`.
     2. In [`backend/providers/antigravity.py:283-287`](file:///Users/mac/MY/Benevia/agent-relay/backend/providers/antigravity.py#L283-L287), `await proc.wait()` after `proc.kill()`.

7. **[Major] `ClaudeAdapter.parse_stream_line` checks `not state.saw_text_delta` instead of `not state.saw_thinking_delta` on `assistant` thinking blocks, duplicating reasoning text**
   - **Location**: [`backend/providers/base.py:12-22`](file:///Users/mac/MY/Benevia/agent-relay/backend/providers/base.py#L12-L22) ([`StreamParseState`](file:///Users/mac/MY/Benevia/agent-relay/backend/providers/base.py#L9-L26)) and [`backend/providers/claude.py:163-207`](file:///Users/mac/MY/Benevia/agent-relay/backend/providers/claude.py#L163-L207) ([`ClaudeAdapter.parse_stream_line`](file:///Users/mac/MY/Benevia/agent-relay/backend/providers/claude.py#L106-L292))
   - **What's wrong**: When `claude` runs with `--include-partial-messages`, `thinking_delta` stream events are emitted before any `text_delta` event. When an `"type": "assistant"` message arrives containing a `"type": "thinking"` content block (e.g. an assistant message that thinks and invokes a `tool_use` before any final text response is streamed, or when thinking finishes), `state.saw_text_delta` is still `False`. Line 197 checks `elif btype == "thinking" and block.get("thinking") and not state.saw_text_delta:`, emitting the full thinking block a second time.
   - **Concrete failure scenario**: On any Claude turn where extended thinking occurs before a tool call, the Reasoning / Thinking collapsible block in the UI and `<turn_id>.jsonl` contain the entire thinking text twice back-to-back.
   - **Exact fix**:
     1. Add `self.saw_thinking_delta: bool = False` to [`StreamParseState.__init__`](file:///Users/mac/MY/Benevia/agent-relay/backend/providers/base.py#L12-L22).
     2. In [`backend/providers/claude.py:163-165`](file:///Users/mac/MY/Benevia/agent-relay/backend/providers/claude.py#L163-L165), set `state.saw_thinking_delta = True` when a `thinking_delta` stream event is parsed.
     3. In [`backend/providers/claude.py:197`](file:///Users/mac/MY/Benevia/agent-relay/backend/providers/claude.py#L197), change `and not state.saw_text_delta:` to `and not state.saw_thinking_delta:`.

8. **[Major] `AntigravityAdapter` and `fake_agy.py` diverge from real `agy` `stream-json` output (`step_update.conversation_id`, `init` event, `ACTIVE` state, and error dict extraction)**
   - **Location**: [`backend/providers/antigravity.py:110-126`](file:///Users/mac/MY/Benevia/agent-relay/backend/providers/antigravity.py#L110-L126), [`233-237`](file:///Users/mac/MY/Benevia/agent-relay/backend/providers/antigravity.py#L233-L237), and [`tests/fixtures/fake_agy.py:55-190`](file:///Users/mac/MY/Benevia/agent-relay/tests/fixtures/fake_agy.py#L55-L190)
   - **What's wrong**:
     1. In [`docs/reference/samples/agy-turn1.jsonl`](file:///Users/mac/MY/Benevia/agent-relay/docs/reference/samples/agy-turn1.jsonl) and [`agy-turn2.jsonl`](file:///Users/mac/MY/Benevia/agent-relay/docs/reference/samples/agy-turn2.jsonl), `conversation_id` is at the top level only on `"event": "init"` (line 1), inside `obj["step_update"]["conversation_id"]` on all `"event": "step_update"` lines (lines 2–8), and inside `obj["result"]["conversation_id"]` on `"event": "result"` (line 9). [`AntigravityAdapter.parse_stream_line`](file:///Users/mac/MY/Benevia/agent-relay/backend/providers/antigravity.py#L110-L112) only checks `obj.get("conversation_id") or res_dict.get("conversation_id")`, ignoring `step_update.get("conversation_id")`.
     2. In [`backend/providers/antigravity.py:233-236`](file:///Users/mac/MY/Benevia/agent-relay/backend/providers/antigravity.py#L233-L236), if `res_dict.get("error")` on a failed result is a dict `{"message": "..."}` (matching `tool_info.get("error")` at lines 185–190), `str(res_dict.get("error"))` formats a raw Python dict repr instead of extracting `message`.
     3. [`tests/fixtures/fake_agy.py`](file:///Users/mac/MY/Benevia/agent-relay/tests/fixtures/fake_agy.py#L55-L190) never emits `"event": "init"`, puts `"conversation_id"` at the top level of every line instead of inside `step_update`, omits `"event": "step_update"`, uses `"state": "RUNNING"` instead of real `agy`'s `"state": "ACTIVE"`, and uses lowercase `{"path": "README.md"}` instead of real `agy`'s PascalCase `{"Path": "...", "StartLine": 1, "EndLine": 5}`.
   - **Concrete failure scenario**: If `agy`'s initial `init` line is missed or a stream starts with `step_update`, `AntigravityAdapter` fails to capture `conversation_id` until the end of the turn; and unit tests against `fake_agy.py` do not exercise the real `init` / `step_update` / `ACTIVE` event structure.
   - **Exact fix**:
     1. In [`backend/providers/antigravity.py:110-113`](file:///Users/mac/MY/Benevia/agent-relay/backend/providers/antigravity.py#L110-L113), extract `step_update = obj.get("step_update")` first (`step_dict = step_update if isinstance(step_update, dict) else {}`) and check:
        ```python
        conv_id = (
            obj.get("conversation_id")
            or step_dict.get("conversation_id")
            or res_dict.get("conversation_id")
        )
        ```
     2. In [`backend/providers/antigravity.py:233-236`](file:///Users/mac/MY/Benevia/agent-relay/backend/providers/antigravity.py#L233-L236), handle dict errors on `result`:
        ```python
        raw_res_err = res_dict.get("error")
        if isinstance(raw_res_err, dict) and raw_res_err.get("message"):
            state.error_message = str(raw_res_err["message"])
        else:
            state.error_message = str(raw_res_err or "Turn ended without SUCCESS status")
        ```
     3. Update [`tests/fixtures/fake_agy.py`](file:///Users/mac/MY/Benevia/agent-relay/tests/fixtures/fake_agy.py#L55-L190) to emit the real `{"event": "init", "conversation_id": conv_id, "init": {...}}` line and nest `"conversation_id"` and `"state": "ACTIVE"` inside `{"event": "step_update", "step_update": {...}}`.

9. **[Major] Pipeline Builder overwrites unsaved edits on re-render and ignores unsaved changes when clicking `▶ Run Pipeline` on an existing pipeline**
   - **Location**: [`frontend/js/pipelines.js:216-235`](file:///Users/mac/MY/Benevia/agent-relay/frontend/js/pipelines.js#L216-L235) ([`renderPipelineBuilder`](file:///Users/mac/MY/Benevia/agent-relay/frontend/js/pipelines.js#L185-L236)) and [`383-402`](file:///Users/mac/MY/Benevia/agent-relay/frontend/js/pipelines.js#L383-L402) ([`handleRunPipeline`](file:///Users/mac/MY/Benevia/agent-relay/frontend/js/pipelines.js#L383-L437))
   - **What's wrong**:
     1. At startup ([`frontend/js/app.js:479-481`](file:///Users/mac/MY/Benevia/agent-relay/frontend/js/app.js#L479-L481)), `store.activePipelineId` is initialized to `store.pipelines[0].id`. Whenever `renderPipelineBuilder()` is called (including when switching views in `app.js:192` or when `refreshSessions()` runs in `app.js:221`), lines 216–227 unconditionally reset `nameInput`, `descInput`, `maxLoopsInput`, and `editingStages` from the saved pipeline in `appStore.pipelines`.
     2. In `handleRunPipeline()` (lines 396–401), `handleSavePipeline()` is only called `if (!pipelineId)`. When an existing pipeline is selected (`appStore.activePipelineId` is truthy), clicking `▶ Run Pipeline` never saves edits made to stages, bound sessions, approval checkboxes, or `Max Review Loops`.
   - **Concrete failure scenario**: A user selects a saved pipeline (or uses the one auto-selected at startup), toggles `"Pause for my approval before passing on"` or clicks `"Auto-Fill 4-Stage Pipeline"`, and clicks `"▶ Run Pipeline"`. The backend runs the old unmodified pipeline from SQLite without the approval gate or stage changes. Or, while building a pipeline with `activePipelineId` set, the user creates a new session from the sidebar (`refreshSessions()`), which triggers `renderPipelineBuilder()` and wipes out all unsaved stage edits.
   - **Exact fix**:
     1. Track `let loadedPipelineId = null;` and `let isDirty = false;` in [`frontend/js/pipelines.js`](file:///Users/mac/MY/Benevia/agent-relay/frontend/js/pipelines.js#L13-L21). Set `isDirty = true` whenever the user modifies name, description, max loops, or stages (`editingStages`).
     2. In [`renderPipelineBuilder`](file:///Users/mac/MY/Benevia/agent-relay/frontend/js/pipelines.js#L216-L227), only reload `nameInput`, `descInput`, `maxLoopsInput`, and `editingStages` from `pipe` when `loadedPipelineId !== appStore.activePipelineId || !isDirty`.
     3. In [`handleRunPipeline`](file:///Users/mac/MY/Benevia/agent-relay/frontend/js/pipelines.js#L396-L401), call `handleSavePipeline()` when `!pipelineId || isDirty`.

10. **[Major] `PipelineEngine` allows concurrent runs when a pipeline is paused in `waiting_approval` and can spawn a stage turn after `stop_pipeline_run` is called**
    - **Location**: [`backend/pipeline_engine.py:203-215`](file:///Users/mac/MY/Benevia/agent-relay/backend/pipeline_engine.py#L203-L215) ([`start_pipeline_run`](file:///Users/mac/MY/Benevia/agent-relay/backend/pipeline_engine.py#L192-L230)), [`332-343`](file:///Users/mac/MY/Benevia/agent-relay/backend/pipeline_engine.py#L332-L343) ([`_execute_stage_turn`](file:///Users/mac/MY/Benevia/agent-relay/backend/pipeline_engine.py#L297-L414)), and [`800-834`](file:///Users/mac/MY/Benevia/agent-relay/backend/pipeline_engine.py#L800-L834) ([`stop_pipeline_run`](file:///Users/mac/MY/Benevia/agent-relay/backend/pipeline_engine.py#L781-L852))
    - **What's wrong**:
      1. `start_pipeline_run` (lines 203–215) only checks `self.session_manager.is_session_running(sess_id)` and `sess.get("status") == "running"`. It does not check `self.db.has_active_pipeline_runs(pipeline_id)` or whether any stage session belongs to another active/waiting pipeline run. When Run 1 is paused in `waiting_approval`, all its sessions have `status == "idle"`, so starting Run 2 on the same pipeline succeeds and corrupts the shared sessions' conversation state.
      2. In `_execute_stage_turn` (lines 332–343), `self._active_stage_sessions[run_id] = session["id"]` is set at line 336 before `run_turn_and_wait` registers the turn in `SessionManager`, and `_execute_stage_turn` does not check `if run_id in self._stopped_runs:` before calling `run_turn_and_wait` (nor before rework/re-review stages at lines 620 and 669).
    - **Concrete failure scenario**:
      1. Run 1 pauses for approval after Stage 1. The user clicks `▶ Run Pipeline` again; Run 2 starts on the same sessions while Run 1 is still waiting for approval.
      2. `stop_pipeline_run` is called just as `_execute_stage_turn` sets `self._active_stage_sessions[run_id]` (line 336) before `start_turn` registers the subprocess; `stop_pipeline_run`'s wait loop (line 804) breaks immediately, `stop_session` finds no process yet and returns `False`, and `_execute_stage_turn` proceeds to spawn the CLI subprocess.
    - **Exact fix**:
      1. In [`backend/pipeline_engine.py:202`](file:///Users/mac/MY/Benevia/agent-relay/backend/pipeline_engine.py#L202), check `if self.db.has_active_pipeline_runs(pipeline_id): raise ConflictError("Pipeline already has an active run")` (and check that none of `stages`'s `session_id`s are in `self._active_stage_sessions.values()`).
      2. In [`backend/pipeline_engine.py:336`](file:///Users/mac/MY/Benevia/agent-relay/backend/pipeline_engine.py#L336), check `if run_id in self._stopped_runs:` immediately before calling `run_turn_and_wait` (and if stopped, mark the stage run and pipeline run `STOPPED` and return `(updated_sr, None, False)`).

11. **[Minor] `renderMarkdown()` runs bold/italic/heading regexes before extracting inline code spans, corrupting inline code containing `*`**
    - **Location**: [`frontend/js/components.js:56-65`](file:///Users/mac/MY/Benevia/agent-relay/frontend/js/components.js#L56-L65) ([`renderMarkdown`](file:///Users/mac/MY/Benevia/agent-relay/frontend/js/components.js#L26-L101))
    - **What's wrong**: `renderMarkdown()` extracts fenced code blocks into placeholders (`__CODE_BLOCK_N__`) before inline formatting, but leaves inline code spans (`` `...` ``) in `safe` while executing bold (`\*\*([^*]+)\*\*`) and italic (`(^|[^*])\*([^*]+)\*`) replacements at lines 62–63, only converting `` `([^`]+)` `` at line 64.
    - **Concrete failure scenario**: An agent outputs inline code containing asterisks such as `` `SELECT * FROM turns WHERE status = *p` `` or `` `def foo(*args, **kwargs):` ``. `renderMarkdown()` injects `<em>` or `<strong>` tags inside the `<code class="inline-code">` element, breaking the code text.
    - **Exact fix**: In [`frontend/js/components.js:42`](file:///Users/mac/MY/Benevia/agent-relay/frontend/js/components.js#L42), extract inline code spans `` `([^`\r\n]+)` `` into an `inlineCodeBlocks` placeholder array (`__INLINE_CODE_${i}__`) right after fenced code blocks, and restore them in step 8 alongside `codeBlocks`.

---

**Overall Verdict**: The architecture and module boundaries are well-structured, but **3 critical runtime defects** (`StreamReader` 64 KiB line limit crash/orphan, real `claude` error event misparsing, and premature `provider_session_id` persistence on failed Turn 1) and **7 major integration/state defects** must be fixed before running against real `claude` and `agy` CLIs.

---

### Short Report

1. **Files changed**:
   - None (read-only code review).
2. **Anything unsure about, skipped, or could not do**:
   - None. All priority areas (`docs/reference/samples/`, CLI help flags, fake CLI fixtures, backend/frontend integration, pipeline engine state machine, subprocess safety, and security) were inspected directly.
tool failed: read_file: file does not exist
tool failed: read_file: file does not exist
tool failed: read_file: file does not exist

[tools: 48 calls, 3 failed]
