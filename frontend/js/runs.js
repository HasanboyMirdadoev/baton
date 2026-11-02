/**
 * Agent Relay — Active Pipeline Run Timeline & Run History Views
 */

import { api } from './api.js';
import {
  escapeHtml,
  renderMarkdown,
  renderProviderBadge,
  renderRoleBadge,
  renderStatusDot,
  renderStatusBadge,
  renderToolCard,
  formatDuration,
  formatTimestamp,
  showToast,
} from './components.js';

let appStore = null;
let appCallbacks = {};
let isInitialized = false;
let runTimerInterval = null;

// History View State
let historySubTab = 'runs'; // 'runs' | 'turns'
let selectedHistoryId = null;
let selectedHistoryType = null; // 'run' | 'turn'

/**
 * Starts the live elapsed timer for an active pipeline run.
 * @param {string|null} startedAt
 */
function startRunTimer(startedAt) {
  stopRunTimer();
  const timerEl = document.getElementById('run-header-timer');
  if (!timerEl || !startedAt) return;

  const update = () => {
    const startMs = new Date(startedAt).getTime();
    if (isNaN(startMs)) return;
    const elapsedMs = Math.max(0, Date.now() - startMs);
    const totalSec = Math.floor(elapsedMs / 1000);
    const m = Math.floor(totalSec / 60);
    const s = totalSec % 60;
    timerEl.textContent = `${m < 10 ? '0' : ''}${m}:${s < 10 ? '0' : ''}${s}`;
  };

  update();
  runTimerInterval = setInterval(update, 1000);
}

/**
 * Stops the live elapsed timer for pipeline runs.
 */
function stopRunTimer() {
  if (runTimerInterval) {
    clearInterval(runTimerInterval);
    runTimerInterval = null;
  }
}

/**
 * Reconstructs tool call objects from a list of NormalizedEvents.
 * @param {Array} events
 * @returns {Array<object>}
 */
function buildToolCardsFromEvents(events) {
  if (!Array.isArray(events)) return [];
  const toolsMap = new Map();
  const toolList = [];

  for (const evt of events) {
    if (evt.type === 'tool_use') {
      const id = evt.tool_call_id || `tool_${evt.seq}`;
      const item = {
        id,
        name: evt.tool_name,
        input: evt.tool_input,
        output: null,
        is_error: false,
        status: 'running',
      };
      toolsMap.set(id, item);
      toolList.push(item);
    } else if (evt.type === 'tool_result') {
      const id = evt.tool_call_id;
      const existing = toolsMap.get(id);
      if (existing) {
        existing.output = evt.tool_output;
        existing.is_error = Boolean(evt.is_error);
        existing.status = evt.is_error ? 'error' : 'completed';
      } else {
        const item = {
          id: id || `res_${evt.seq}`,
          name: 'tool_result',
          input: null,
          output: evt.tool_output,
          is_error: Boolean(evt.is_error),
          status: evt.is_error ? 'error' : 'completed',
        };
        toolList.push(item);
      }
    }
  }

  return toolList;
}

/**
 * Generates HTML for the ordered stage run cards.
 * @param {Array} stageRuns
 * @returns {string}
 */
function renderTimelineStages(stageRuns) {
  if (!stageRuns || stageRuns.length === 0) {
    return `
      <div class="sessions-empty-state" style="padding: 24px;">
        <p>No stage executions recorded yet.</p>
      </div>
    `;
  }

  return stageRuns
    .map((stageRun) => {
      const isRunning = stageRun.status === 'running';

      // Iteration badge
      let iterationBadgeHtml = '';
      if (stageRun.iteration > 0) {
        let label = `Iteration #${stageRun.iteration}`;
        if (stageRun.role === 'reviewer') {
          label = `Re-Review #${stageRun.iteration} · Code Reviewer`;
        } else if (stageRun.role === 'implementer') {
          label = `Rework Loop #${stageRun.iteration} · Implementer`;
        }
        iterationBadgeHtml = `<span class="stage-iteration-pill">${escapeHtml(label)}</span>`;
      }

      // Verdict badge
      let verdictBadgeHtml = '';
      if (stageRun.verdict === 'APPROVED') {
        verdictBadgeHtml = '<span class="status-badge status-completed"><span class="verdict-icon">✓</span> APPROVED</span>';
      } else if (stageRun.verdict === 'CHANGES_REQUESTED') {
        verdictBadgeHtml = '<span class="status-badge status-changes_requested"><span class="verdict-icon">⚠</span> CHANGES_REQUESTED</span>';
      }

      // Duration
      const durationText = isRunning ? 'Running...' : formatDuration(stageRun.duration_ms);

      // Check cached events in liveTurnEvents
      const turnEvents = (appStore && appStore.liveTurnEvents && stageRun.turn_id)
        ? appStore.liveTurnEvents[stageRun.turn_id] || []
        : [];

      // Build tool cards
      const toolCards = buildToolCardsFromEvents(turnEvents);
      const toolCardsHtml = toolCards.map((t) => renderToolCard(t)).join('');

      // Build thinking text
      const thinkingText = turnEvents
        .filter((e) => e.type === 'thinking_delta' && e.text)
        .map((e) => e.text)
        .join('');

      // Build live streaming text if output_text not yet populated
      const textDeltas = turnEvents
        .filter((e) => e.type === 'text_delta' && e.text)
        .map((e) => e.text)
        .join('');

      const hasOutput = Boolean(stageRun.output_text || textDeltas);
      const contentText = stageRun.output_text || textDeltas;

      let thinkingBlockHtml = '';
      if (thinkingText) {
        thinkingBlockHtml = `
          <details class="thinking-block"${hasOutput ? '' : ' open'}>
            <summary>💭 Reasoning / Thinking</summary>
            <div class="thinking-body">${escapeHtml(thinkingText)}</div>
          </details>
        `;
      }

      // On-demand tool loader button if turn_id exists and toolCardsHtml is empty
      let loadToolsBtnHtml = '';
      if (stageRun.turn_id && !toolCards.length && !isRunning) {
        loadToolsBtnHtml = `
          <div style="margin-bottom: 8px;">
            <button
              type="button"
              class="btn-stage-ctrl btn-load-stage-tools"
              data-turn-id="${escapeHtml(stageRun.turn_id)}"
              data-stage-id="${escapeHtml(stageRun.id)}"
            >🔍 Load Tool Call Events</button>
          </div>
        `;
      }

      let findingsHtml = '';
      if (stageRun.verdict === 'CHANGES_REQUESTED' && stageRun.findings) {
        findingsHtml = `
          <div class="findings-box" style="margin-top: 12px; padding: 12px; background-color: var(--status-rework-bg); border: 1px solid var(--status-rework-fg); border-radius: var(--radius-sm);">
            <div class="findings-header">⚠ Findings / Requested Changes:</div>
            <div style="margin-top: 6px; white-space: pre-wrap; font-size: 12.5px;">${escapeHtml(stageRun.findings)}</div>
          </div>
        `;
      }

      let approvalNoteHtml = '';
      if (stageRun.approval_note) {
        approvalNoteHtml = `
          <div class="approval-note-box" style="margin-top: 8px; padding: 6px 10px; background-color: var(--bg-elevated); border-radius: var(--radius-sm); font-size: 11.5px; color: var(--text-secondary);">
            <strong>Approval Note:</strong> ${escapeHtml(stageRun.approval_note)}
          </div>
        `;
      }

      let errorHtml = '';
      if (stageRun.error_message) {
        errorHtml = `<div class="turn-error-box" style="margin-top: 8px;">${escapeHtml(stageRun.error_message)}</div>`;
      }

      let outputBodyHtml = '';
      if (hasOutput) {
        outputBodyHtml = `<div class="assistant-markdown" data-raw-text="${escapeHtml(contentText)}">${renderMarkdown(contentText)}</div>`;
      } else if (isRunning) {
        outputBodyHtml = '<div class="form-hint" style="font-style: italic;">Stage turn is currently executing...</div>';
      } else {
        outputBodyHtml = '<div class="form-hint" style="font-style: italic;">No output produced</div>';
      }

      return `
        <div class="stage-run-card" data-stage-run-id="${escapeHtml(stageRun.id)}">
          <div class="stage-run-header">
            <div class="stage-run-title-group">
              ${renderStatusDot(stageRun.status)}
              <span style="font-weight: 700; font-size: 13px;">Stage #${stageRun.stage_order + 1}: ${escapeHtml(stageRun.stage_name)}</span>
              <span style="color: var(--text-muted); font-size: 12px;">(${escapeHtml(stageRun.session_name)})</span>
              ${renderProviderBadge(stageRun.provider)}
              ${renderRoleBadge(stageRun.role)}
              <span class="badge-mono" style="font-size: 11px;">${escapeHtml(stageRun.model)}</span>
              ${iterationBadgeHtml}
            </div>
            <div style="display: flex; align-items: center; gap: 8px;">
              ${verdictBadgeHtml}
              <span style="font-family: var(--font-mono); font-size: 11px; color: var(--text-muted);">${durationText}</span>
            </div>
          </div>

          <div class="stage-tabs-bar">
            <button type="button" class="stage-tab-btn active" data-stage-id="${escapeHtml(stageRun.id)}" data-tab="live">Live Output / Reply</button>
            <button type="button" class="stage-tab-btn" data-stage-id="${escapeHtml(stageRun.id)}" data-tab="prompt">Handoff Input Prompt</button>
          </div>

          <div class="stage-tab-content stage-tab-live" id="tab-live-${escapeHtml(stageRun.id)}">
            ${thinkingBlockHtml}
            <div class="stage-tool-stream" id="tools-${escapeHtml(stageRun.id)}">${toolCardsHtml}</div>
            ${loadToolsBtnHtml}
            ${outputBodyHtml}
            ${findingsHtml}
            ${approvalNoteHtml}
            ${errorHtml}
          </div>

          <div class="stage-tab-content stage-tab-prompt hidden" id="tab-prompt-${escapeHtml(stageRun.id)}">
            <pre class="stage-handoff-pre"><code>${escapeHtml(stageRun.input_prompt || '')}</code></pre>
          </div>
        </div>
      `;
    })
    .join('');
}

/**
 * Renders Panel 2 (Active Pipeline Run Timeline & Approval Banner).
 */
export function renderActiveRunPanel() {
  if (!appStore) return;

  const emptyEl = document.getElementById('pipeline-empty-run');
  const runContainer = document.getElementById('pipeline-run-container');

  const detail = appStore.activeRunDetail;
  const run = detail ? detail.run : null;

  if (!run) {
    if (emptyEl) emptyEl.classList.remove('hidden');
    if (runContainer) runContainer.classList.add('hidden');
    stopRunTimer();
    return;
  }

  if (emptyEl) emptyEl.classList.add('hidden');
  if (runContainer) runContainer.classList.remove('hidden');

  const stageRuns = (detail && Array.isArray(detail.stage_runs)) ? detail.stage_runs : [];

  // 1. Run Summary Header
  const titleEl = document.getElementById('run-header-title');
  if (titleEl) {
    titleEl.textContent = run.pipeline_name || 'Pipeline Run';
  }

  const statusEl = document.getElementById('run-header-status');
  if (statusEl) {
    statusEl.innerHTML = renderStatusBadge(run.status);
  }

  const loopEl = document.getElementById('run-header-loop');
  if (loopEl) {
    if (run.max_review_loops > 0) {
      loopEl.textContent = `Review Loop: ${run.review_loop_count || 0} / ${run.max_review_loops}`;
      loopEl.classList.remove('hidden');
    } else {
      loopEl.classList.add('hidden');
    }
  }

  const timerEl = document.getElementById('run-header-timer');
  const stopBtn = document.getElementById('btn-stop-pipeline');

  const isActive = run.status === 'running' || run.status === 'waiting_approval';

  if (isActive) {
    startRunTimer(run.started_at);
    if (stopBtn) stopBtn.classList.remove('hidden');
  } else {
    stopRunTimer();
    if (timerEl) {
      timerEl.textContent = formatDuration(run.duration_ms);
    }
    if (stopBtn) stopBtn.classList.add('hidden');
  }

  const promptTextEl = document.getElementById('run-task-prompt-text');
  if (promptTextEl) {
    promptTextEl.innerHTML = renderMarkdown(run.task_prompt || '');
  }

  // 2. Sticky Approval Banner
  const banner = document.getElementById('pipeline-approval-banner');
  const bannerMsg = document.getElementById('approval-banner-message');

  if (run.status === 'waiting_approval') {
    if (banner) banner.classList.remove('hidden');
    const paused = stageRuns
      .slice()
      .reverse()
      .find((s) => s.status === 'waiting_approval' || (s.status === 'completed' && s.require_approval)) || stageRuns[stageRuns.length - 1];

    const stageName = paused ? paused.stage_name : 'Current Stage';
    const sessName = paused && paused.session_name ? ` (${paused.session_name})` : '';
    if (bannerMsg) {
      bannerMsg.textContent = `Paused for Approval after Stage: ${stageName}${sessName}`;
    }
  } else if (banner) {
    banner.classList.add('hidden');
  }

  // 3. Live Per-Stage Timeline
  const timelineEl = document.getElementById('stage-runs-timeline');
  if (timelineEl) {
    timelineEl.innerHTML = renderTimelineStages(stageRuns);
  }
}

/**
 * Stops the active pipeline run immediately.
 */
async function stopActivePipelineRun() {
  if (!appStore || !appStore.activeRunDetail || !appStore.activeRunDetail.run) return;
  const runId = appStore.activeRunDetail.run.id;

  try {
    const res = await api.stopPipelineRun(runId);
    showToast('Pipeline run stopped', 'info');
    if (res && res.run) {
      appStore.activeRunDetail = res;
    } else {
      appStore.activeRunDetail.run.status = 'stopped';
    }
    renderActiveRunPanel();
    if (appCallbacks.refreshRuns) {
      appCallbacks.refreshRuns();
    }
  } catch (err) {
    showToast(`Failed to stop pipeline: ${err.message || err}`, 'error');
  }
}

/**
 * Approves a paused pipeline stage and resumes execution.
 */
async function approveActivePipelineRun() {
  if (!appStore || !appStore.activeRunDetail || !appStore.activeRunDetail.run) return;
  const runId = appStore.activeRunDetail.run.id;
  const noteInput = document.getElementById('pipeline-approval-note');
  const note = noteInput ? noteInput.value.trim() : '';

  const approveBtn = document.getElementById('btn-approve-pipeline');
  if (approveBtn) {
    approveBtn.disabled = true;
    approveBtn.textContent = 'Approving...';
  }

  try {
    const res = await api.approvePipelineRun(runId, { note: note || null });
    showToast('Stage approved, continuing pipeline...', 'success');
    if (noteInput) noteInput.value = '';

    if (res && res.run) {
      const existingRuns = (
        appStore.activeRunDetail &&
        appStore.activeRunDetail.run &&
        appStore.activeRunDetail.run.id === res.run.id &&
        Array.isArray(appStore.activeRunDetail.stage_runs)
      ) ? appStore.activeRunDetail.stage_runs : [];

      if (existingRuns.length > 0 && (!res.stage_runs || res.stage_runs.length === 0)) {
        appStore.activeRunDetail = { ...res, stage_runs: existingRuns };
      } else if (existingRuns.length > 0 && Array.isArray(res.stage_runs)) {
        const merged = res.stage_runs.map((rStage) => {
          const prev = existingRuns.find((e) => e.id === rStage.id);
          if (!prev) return rStage;
          return {
            ...rStage,
            turn_id: rStage.turn_id || prev.turn_id || null,
            output_text: rStage.output_text || prev.output_text || '',
            findings: rStage.findings || prev.findings || null,
          };
        });
        for (const prev of existingRuns) {
          if (!merged.some((m) => m.id === prev.id)) {
            merged.push(prev);
          }
        }
        appStore.activeRunDetail = { ...res, stage_runs: merged };
      } else {
        appStore.activeRunDetail = res;
      }
    } else if (appStore.activeRunDetail && appStore.activeRunDetail.run) {
      appStore.activeRunDetail.run.status = 'running';
    }

    renderActiveRunPanel();
    if (appCallbacks.refreshRuns) {
      appCallbacks.refreshRuns();
    }
  } catch (err) {
    showToast(`Failed to approve pipeline: ${err.message || err}`, 'error');
  } finally {
    if (approveBtn) {
      approveBtn.disabled = false;
      approveBtn.textContent = '✓ Approve & Continue';
    }
  }
}

/**
 * Renders the filterable history list in #history-items-list.
 */
function renderHistoryList() {
  const tabRuns = document.getElementById('history-tab-runs');
  const tabTurns = document.getElementById('history-tab-turns');
  const listEl = document.getElementById('history-items-list');

  if (tabRuns) tabRuns.classList.toggle('active', historySubTab === 'runs');
  if (tabTurns) tabTurns.classList.toggle('active', historySubTab === 'turns');

  if (!listEl || !appStore) return;

  if (historySubTab === 'runs') {
    const runs = appStore.pipelineRuns || [];
    if (runs.length === 0) {
      listEl.innerHTML = `
        <div class="sessions-empty-state" style="padding: 16px;">
          <p>No pipeline runs recorded yet.</p>
        </div>
      `;
      return;
    }

    const html = runs
      .map((r) => {
        const isSelected = selectedHistoryId === r.id && selectedHistoryType === 'run';
        const loopCount = Number(r.review_loop_count || 0);
        const loopText = loopCount > 0 ? `${loopCount} rework ${loopCount === 1 ? 'loop' : 'loops'}` : '0 rework loops';
        const snippet = r.task_prompt ? r.task_prompt.slice(0, 80) : '';

        return `
          <div
            class="history-card${isSelected ? ' active selected' : ''}"
            data-run-id="${escapeHtml(r.id)}"
            role="listitem"
          >
            <div class="history-card-header">
              <span style="font-weight: 600; font-size: 13px;" title="${escapeHtml(r.pipeline_name || 'Pipeline')}">
                ${escapeHtml(r.pipeline_name || 'Pipeline')}
              </span>
              ${renderStatusBadge(r.status)}
            </div>
            <div class="history-card-snippet" title="${escapeHtml(r.task_prompt || '')}">
              ${escapeHtml(snippet)}
            </div>
            <div class="history-card-meta">
              <span>${escapeHtml(loopText)}</span>
              <span>${formatDuration(r.duration_ms)}</span>
              <span>${formatTimestamp(r.started_at)}</span>
            </div>
          </div>
        `;
      })
      .join('');

    listEl.innerHTML = html;
  } else {
    const turns = appStore.allTurns || [];
    if (turns.length === 0) {
      listEl.innerHTML = `
        <div class="sessions-empty-state" style="padding: 16px;">
          <p>No session turns recorded yet.</p>
        </div>
      `;
      return;
    }

    const html = turns
      .map((t) => {
        const isSelected = selectedHistoryId === t.id && selectedHistoryType === 'turn';
        const snippet = t.prompt ? t.prompt.slice(0, 80) : '';

        return `
          <div
            class="history-card${isSelected ? ' active selected' : ''}"
            data-turn-id="${escapeHtml(t.id)}"
            role="listitem"
          >
            <div class="history-card-header">
              <div style="display: flex; align-items: center; gap: 6px; overflow: hidden;">
                <span style="font-weight: 600; font-size: 12.5px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">
                  ${escapeHtml(t.session_name || 'Session')}
                </span>
                ${renderProviderBadge(t.provider)}
                ${renderRoleBadge(t.role)}
              </div>
              ${renderStatusBadge(t.status)}
            </div>
            <div class="history-card-snippet" title="${escapeHtml(t.prompt || '')}">
              <strong>Turn #${t.turn_number}:</strong> ${escapeHtml(snippet)}
            </div>
            <div class="history-card-meta">
              <span class="badge-mono" style="font-size: 10px;">${escapeHtml(t.model || '')}</span>
              <span>${formatDuration(t.duration_ms)}</span>
              <span>${formatTimestamp(t.started_at)}</span>
            </div>
          </div>
        `;
      })
      .join('');

    listEl.innerHTML = html;
  }
}

/**
 * Renders the detail inspector panel for historical runs and turns.
 */
async function renderHistoryDetail() {
  const emptyEl = document.getElementById('history-empty-detail');
  const contentEl = document.getElementById('history-content-detail');

  if (!selectedHistoryId || !selectedHistoryType) {
    if (emptyEl) emptyEl.classList.remove('hidden');
    if (contentEl) contentEl.classList.add('hidden');
    return;
  }

  if (emptyEl) emptyEl.classList.add('hidden');
  if (contentEl) contentEl.classList.remove('hidden');

  if (selectedHistoryType === 'run') {
    contentEl.innerHTML = `
      <div style="padding: 24px; text-align: center; color: var(--text-muted);">
        Loading pipeline run details...
      </div>
    `;

    try {
      const detail = await api.getPipelineRun(selectedHistoryId);
      if (!detail || !detail.run) {
        contentEl.innerHTML = '<div class="turn-error-box">Pipeline run not found.</div>';
        return;
      }

      const run = detail.run;
      const stageRuns = detail.stage_runs || [];

      let errorAlert = '';
      if (run.error_message) {
        errorAlert = `<div class="turn-error-box" style="margin-top: 8px;">${escapeHtml(run.error_message)}</div>`;
      }

      contentEl.innerHTML = `
        <div class="run-summary-header" style="margin-bottom: 16px;">
          <div class="run-summary-top">
            <div class="run-pipeline-title">${escapeHtml(run.pipeline_name || 'Pipeline Run')}</div>
            <div class="run-badges-group">
              ${renderStatusBadge(run.status)}
              ${
                run.max_review_loops > 0
                  ? `<span class="stage-iteration-pill">Review Loop: ${run.review_loop_count || 0} / ${run.max_review_loops}</span>`
                  : ''
              }
              <span class="turn-elapsed-timer">${formatDuration(run.duration_ms)}</span>
            </div>
          </div>
          <div style="display: flex; align-items: center; gap: 12px; font-size: 11px; color: var(--text-muted); margin-top: 4px;">
            <span>Started: ${formatTimestamp(run.started_at)}</span>
            ${run.finished_at ? `<span>Finished: ${formatTimestamp(run.finished_at)}</span>` : ''}
          </div>
          <details class="thinking-block" style="margin-top: 8px;">
            <summary>Original Task Prompt</summary>
            <div class="thinking-body">${renderMarkdown(run.task_prompt || '')}</div>
          </details>
          ${errorAlert}
        </div>

        <div style="font-weight: 700; font-size: 13.5px; margin-bottom: 12px; color: var(--text-primary);">
          Per-Stage Execution Timeline
        </div>

        <div id="history-stage-timeline">
          ${renderTimelineStages(stageRuns)}
        </div>
      `;
    } catch (err) {
      contentEl.innerHTML = `<div class="turn-error-box">Failed to load run details: ${escapeHtml(err.message || err)}</div>`;
    }
  } else if (selectedHistoryType === 'turn') {
    contentEl.innerHTML = `
      <div style="padding: 24px; text-align: center; color: var(--text-muted);">
        Loading turn details...
      </div>
    `;

    try {
      const data = await api.getTurn(selectedHistoryId);
      if (!data || !data.turn) {
        contentEl.innerHTML = '<div class="turn-error-box">Turn not found.</div>';
        return;
      }

      const turn = data.turn;
      const events = data.events || [];

      // Build tool cards
      const toolCards = buildToolCardsFromEvents(events);
      const toolCardsHtml = toolCards.map((t) => renderToolCard(t)).join('');

      // Build thinking text
      const thinkingText = events
        .filter((e) => e.type === 'thinking_delta' && e.text)
        .map((e) => e.text)
        .join('');

      let thinkingBlockHtml = '';
      if (thinkingText) {
        thinkingBlockHtml = `
          <details class="thinking-block" style="margin-bottom: 12px;">
            <summary>💭 Reasoning / Thinking</summary>
            <div class="thinking-body">${escapeHtml(thinkingText)}</div>
          </details>
        `;
      }

      let deniedActionsHtml = '';
      if (turn.denied_actions && Array.isArray(turn.denied_actions) && turn.denied_actions.length > 0) {
        deniedActionsHtml = `
          <div class="denied-actions-banner" style="margin-top: 8px;">
            ⚠️ Denied actions: ${escapeHtml(turn.denied_actions.join(', '))}
          </div>
        `;
      }

      let errorBoxHtml = '';
      if (turn.error_message) {
        errorBoxHtml = `<div class="turn-error-box" style="margin-top: 8px;">${escapeHtml(turn.error_message)}</div>`;
      }

      contentEl.innerHTML = `
        <div class="run-summary-header" style="margin-bottom: 16px;">
          <div class="run-summary-top">
            <div style="display: flex; align-items: center; gap: 8px; flex-wrap: wrap;">
              <span style="font-size: 16px; font-weight: 700;">
                ${escapeHtml(turn.session_name || 'Session')} · Turn #${turn.turn_number}
              </span>
              ${renderProviderBadge(turn.provider)}
              ${renderRoleBadge(turn.role)}
              <span class="badge-mono">${escapeHtml(turn.model || '')}</span>
            </div>
            <div style="display: flex; align-items: center; gap: 8px;">
              ${renderStatusBadge(turn.status)}
              <button
                type="button"
                class="btn-primary"
                id="btn-history-open-chat"
                data-session-id="${escapeHtml(turn.session_id)}"
                style="font-size: 12px; padding: 4px 10px;"
              >
                Open Session Chat →
              </button>
            </div>
          </div>
          <div style="display: flex; align-items: center; gap: 12px; font-size: 11px; color: var(--text-muted); margin-top: 4px;">
            <span>Started: ${formatTimestamp(turn.started_at)}</span>
            <span>Duration: ${formatDuration(turn.duration_ms)}</span>
            <span>Tools: ${turn.tool_call_count || 0} calls</span>
          </div>
        </div>

        <div class="turn-user-card" style="margin-bottom: 16px;">
          <div class="turn-user-header">
            <span>Turn #${turn.turn_number} · User Prompt</span>
            <span>${formatTimestamp(turn.started_at)}</span>
          </div>
          <div class="turn-user-prompt">${escapeHtml(turn.prompt)}</div>
        </div>

        <div class="turn-agent-card">
          ${thinkingBlockHtml}
          ${toolCardsHtml ? `<div style="display: flex; flex-direction: column; gap: 8px; margin-bottom: 12px;">${toolCardsHtml}</div>` : ''}
          <div class="assistant-markdown">${renderMarkdown(turn.response_text || '')}</div>
          ${deniedActionsHtml}
          ${errorBoxHtml}
        </div>
      `;

      const openChatBtn = document.getElementById('btn-history-open-chat');
      if (openChatBtn) {
        openChatBtn.addEventListener('click', () => {
          if (appCallbacks.switchView) {
            appCallbacks.switchView('sessions', { sessionId: turn.session_id });
          }
        });
      }
    } catch (err) {
      contentEl.innerHTML = `<div class="turn-error-box">Failed to load turn details: ${escapeHtml(err.message || err)}</div>`;
    }
  }
}

/**
 * Loads historical turn events on demand when requested from timeline stage cards.
 * @param {HTMLElement} btn
 */
async function handleLoadStageTools(btn) {
  const turnId = btn.dataset.turnId;
  const stageId = btn.dataset.stageId;
  if (!turnId) return;

  btn.disabled = true;
  btn.textContent = 'Loading tool events...';

  try {
    const res = await api.getTurn(turnId);
    if (res && Array.isArray(res.events)) {
      if (!appStore.liveTurnEvents) {
        appStore.liveTurnEvents = {};
      }
      appStore.liveTurnEvents[turnId] = res.events;

      const container = document.getElementById(`tools-${stageId}`);
      if (container) {
        const tools = buildToolCardsFromEvents(res.events);
        container.innerHTML = tools.map((t) => renderToolCard(t)).join('');
      }

      btn.remove();
      showToast('Loaded tool call events', 'info');
    }
  } catch (err) {
    btn.disabled = false;
    btn.textContent = 'Retry Loading Tool Events';
    showToast(`Failed to load tool events: ${err.message || err}`, 'error');
  }
}

/**
 * Streams live SSE turn events directly into an active stage card's DOM elements.
 * @param {object} data
 */
function handleLiveStageStream(data) {
  const stageRunId = data.stage_run_id;
  const event = data.event;
  if (!stageRunId || !event) return;

  const card = document.querySelector(`.stage-run-card[data-stage-run-id="${stageRunId}"]`);
  if (!card) return;

  const liveTab = card.querySelector('.stage-tab-live');
  if (!liveTab) return;

  const hint = liveTab.querySelector('.form-hint');
  if (hint) {
    hint.remove();
  }

  if (event.type === 'thinking_delta' && event.text) {
    let thinkingDetails = liveTab.querySelector('.thinking-block');
    if (!thinkingDetails) {
      thinkingDetails = document.createElement('details');
      thinkingDetails.className = 'thinking-block';
      thinkingDetails.open = true;
      thinkingDetails.innerHTML = '<summary>💭 Reasoning / Thinking</summary><div class="thinking-body"></div>';
      liveTab.prepend(thinkingDetails);
    }
    const body = thinkingDetails.querySelector('.thinking-body');
    if (body) {
      body.textContent += event.text;
    }
  } else if (event.type === 'text_delta' && event.text) {
    const thinkingDetails = liveTab.querySelector('.thinking-block');
    if (thinkingDetails && thinkingDetails.open) {
      thinkingDetails.open = false;
    }

    let markdownEl = liveTab.querySelector('.assistant-markdown');
    if (!markdownEl) {
      markdownEl = document.createElement('div');
      markdownEl.className = 'assistant-markdown';
      liveTab.appendChild(markdownEl);
    }

    const curRaw = (markdownEl.dataset.rawText || '') + event.text;
    markdownEl.dataset.rawText = curRaw;
    markdownEl.innerHTML = renderMarkdown(curRaw);
  } else if (event.type === 'tool_use' || event.type === 'tool_result') {
    let toolsStream = liveTab.querySelector('.stage-tool-stream');
    if (!toolsStream) {
      toolsStream = document.createElement('div');
      toolsStream.className = 'stage-tool-stream';
      const thinkingDetails = liveTab.querySelector('.thinking-block');
      if (thinkingDetails && thinkingDetails.nextSibling) {
        liveTab.insertBefore(toolsStream, thinkingDetails.nextSibling);
      } else {
        liveTab.prepend(toolsStream);
      }
    }

    const events = (appStore.liveTurnEvents && appStore.liveTurnEvents[data.turn_id]) || [];
    const tools = buildToolCardsFromEvents(events);
    toolsStream.innerHTML = tools.map((t) => renderToolCard(t)).join('');
  }
}

/**
 * Initializes the Run Timeline & History views and registers DOM event listeners.
 * @param {object} store
 * @param {object} callbacks
 */
export function initRunsView(store, callbacks) {
  appStore = store;
  appCallbacks = callbacks || {};

  if (isInitialized) {
    return;
  }
  isInitialized = true;

  // Stop button on active run summary header
  const btnStop = document.getElementById('btn-stop-pipeline');
  if (btnStop) {
    btnStop.addEventListener('click', stopActivePipelineRun);
  }

  // Approval banner buttons
  const btnApprove = document.getElementById('btn-approve-pipeline');
  if (btnApprove) {
    btnApprove.addEventListener('click', approveActivePipelineRun);
  }

  const btnStopApproval = document.getElementById('btn-stop-approval-pipeline');
  if (btnStopApproval) {
    btnStopApproval.addEventListener('click', stopActivePipelineRun);
  }

  const noteInput = document.getElementById('pipeline-approval-note');
  if (noteInput) {
    noteInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        approveActivePipelineRun();
      }
    });
  }

  // History sub-tabs
  const tabRuns = document.getElementById('history-tab-runs');
  const tabTurns = document.getElementById('history-tab-turns');
  if (tabRuns) {
    tabRuns.addEventListener('click', () => {
      historySubTab = 'runs';
      selectedHistoryId = null;
      selectedHistoryType = null;
      renderHistoryList();
      renderHistoryDetail();
    });
  }
  if (tabTurns) {
    tabTurns.addEventListener('click', async () => {
      historySubTab = 'turns';
      selectedHistoryId = null;
      selectedHistoryType = null;
      if (!appStore.allTurns || appStore.allTurns.length === 0) {
        try {
          const res = await api.listTurns();
          if (res && Array.isArray(res.turns)) {
            appStore.allTurns = res.turns;
          }
        } catch (_) {}
      }
      renderHistoryList();
      renderHistoryDetail();
    });
  }

  // History list card selection
  const historyList = document.getElementById('history-items-list');
  if (historyList) {
    historyList.addEventListener('click', (e) => {
      const card = e.target.closest('.history-card');
      if (!card) return;

      if (historySubTab === 'runs') {
        selectedHistoryId = card.dataset.runId;
        selectedHistoryType = 'run';
      } else {
        selectedHistoryId = card.dataset.turnId;
        selectedHistoryType = 'turn';
      }

      renderHistoryList();
      renderHistoryDetail();
    });
  }

  // Delegated tab switching and tool loaders on active timeline
  const activeTimeline = document.getElementById('stage-runs-timeline');
  if (activeTimeline) {
    activeTimeline.addEventListener('click', (e) => {
      const tabBtn = e.target.closest('.stage-tab-btn');
      if (tabBtn) {
        const card = tabBtn.closest('.stage-run-card');
        if (!card) return;
        const stageId = tabBtn.dataset.stageId;
        const tabName = tabBtn.dataset.tab;

        const allTabs = card.querySelectorAll('.stage-tab-btn');
        allTabs.forEach((b) => b.classList.toggle('active', b === tabBtn));

        const liveContent = card.querySelector(`#tab-live-${stageId}`);
        const promptContent = card.querySelector(`#tab-prompt-${stageId}`);

        if (liveContent) liveContent.classList.toggle('hidden', tabName !== 'live');
        if (promptContent) promptContent.classList.toggle('hidden', tabName !== 'prompt');
        return;
      }

      const loadBtn = e.target.closest('.btn-load-stage-tools');
      if (loadBtn) {
        handleLoadStageTools(loadBtn);
      }
    });
  }

  // Delegated tab switching and tool loaders on history detail inspector
  const historyDetail = document.getElementById('history-detail-panel');
  if (historyDetail) {
    historyDetail.addEventListener('click', (e) => {
      const tabBtn = e.target.closest('.stage-tab-btn');
      if (tabBtn) {
        const card = tabBtn.closest('.stage-run-card');
        if (!card) return;
        const stageId = tabBtn.dataset.stageId;
        const tabName = tabBtn.dataset.tab;

        const allTabs = card.querySelectorAll('.stage-tab-btn');
        allTabs.forEach((b) => b.classList.toggle('active', b === tabBtn));

        const liveContent = card.querySelector(`#tab-live-${stageId}`);
        const promptContent = card.querySelector(`#tab-prompt-${stageId}`);

        if (liveContent) liveContent.classList.toggle('hidden', tabName !== 'live');
        if (promptContent) promptContent.classList.toggle('hidden', tabName !== 'prompt');
        return;
      }

      const loadBtn = e.target.closest('.btn-load-stage-tools');
      if (loadBtn) {
        handleLoadStageTools(loadBtn);
      }
    });
  }
}

/**
 * Renders the top-level View 3 (Run History Browser).
 */
export function renderHistoryView() {
  if (!appStore) return;
  renderHistoryList();
  renderHistoryDetail();
}

/**
 * Handles incoming SSE events for Active Run and Run History views.
 * @param {string} eventName
 * @param {object} data
 */
export function handleRunSseEvent(eventName, data) {
  if (!appStore) return;

  if (eventName === 'pipeline_run_updated' && data && data.run) {
    const updatedRun = data.run;
    const runs = appStore.pipelineRuns || [];
    const idx = runs.findIndex((r) => r.id === updatedRun.id);
    if (idx >= 0) {
      runs[idx] = { ...runs[idx], ...updatedRun };
    } else {
      runs.unshift(updatedRun);
    }
    appStore.pipelineRuns = runs;

    if (appStore.activeRunId === updatedRun.id) {
      if (appStore.activeRunDetail) {
        appStore.activeRunDetail.run = { ...appStore.activeRunDetail.run, ...updatedRun };
      } else {
        appStore.activeRunDetail = { run: updatedRun, stage_runs: [] };
      }
      renderActiveRunPanel();
    }

    if (appStore.activeView === 'history') {
      renderHistoryList();
      if (selectedHistoryId === updatedRun.id && selectedHistoryType === 'run') {
        renderHistoryDetail();
      }
    }
  } else if (eventName === 'pipeline_stage_updated' && data && data.stage_run) {
    const stageRun = data.stage_run;

    if (appStore.activeRunId === stageRun.pipeline_run_id && !appStore.activeRunDetail) {
      appStore.activeRunDetail = { run: { id: stageRun.pipeline_run_id }, stage_runs: [stageRun] };
      renderActiveRunPanel();
    } else if (
      appStore.activeRunDetail &&
      appStore.activeRunDetail.run &&
      appStore.activeRunDetail.run.id === stageRun.pipeline_run_id
    ) {
      if (!Array.isArray(appStore.activeRunDetail.stage_runs)) {
        appStore.activeRunDetail.stage_runs = [];
      }
      const sIdx = appStore.activeRunDetail.stage_runs.findIndex((s) => s.id === stageRun.id);
      if (sIdx >= 0) {
        const existingTurnId = appStore.activeRunDetail.stage_runs[sIdx].turn_id;
        appStore.activeRunDetail.stage_runs[sIdx] = {
          ...appStore.activeRunDetail.stage_runs[sIdx],
          ...stageRun,
          turn_id: stageRun.turn_id || existingTurnId || null,
        };
      } else {
        appStore.activeRunDetail.stage_runs.push(stageRun);
      }
      renderActiveRunPanel();
    }

    if (appStore.activeView === 'history' && selectedHistoryId === stageRun.pipeline_run_id && selectedHistoryType === 'run') {
      renderHistoryDetail();
    }
  } else if (eventName === 'pipeline_waiting_approval' && data) {
    const runId = data.run_id;
    if (
      appStore.activeRunDetail &&
      appStore.activeRunDetail.run &&
      appStore.activeRunDetail.run.id === runId
    ) {
      appStore.activeRunDetail.run.status = 'waiting_approval';
      if (data.stage_run) {
        if (!Array.isArray(appStore.activeRunDetail.stage_runs)) {
          appStore.activeRunDetail.stage_runs = [];
        }
        const sIdx = appStore.activeRunDetail.stage_runs.findIndex((s) => s.id === data.stage_run.id);
        if (sIdx >= 0) {
          const existingTurnId = appStore.activeRunDetail.stage_runs[sIdx].turn_id;
          appStore.activeRunDetail.stage_runs[sIdx] = {
            ...appStore.activeRunDetail.stage_runs[sIdx],
            ...data.stage_run,
            turn_id: data.stage_run.turn_id || existingTurnId || null,
          };
        } else {
          appStore.activeRunDetail.stage_runs.push(data.stage_run);
        }
      }
      renderActiveRunPanel();
    }
  } else if (eventName === 'turn_started' && data && data.turn) {
    const turn = data.turn;
    const turnId = turn.id;
    const pipelineRunId = turn.pipeline_run_id;
    const stageRunId = turn.stage_run_id;

    if (
      pipelineRunId &&
      pipelineRunId === appStore.activeRunId &&
      stageRunId &&
      appStore.activeRunDetail &&
      Array.isArray(appStore.activeRunDetail.stage_runs)
    ) {
      const match = appStore.activeRunDetail.stage_runs.find((s) => s.id === stageRunId);
      if (match) {
        match.turn_id = turnId;
      }
    }
  } else if (eventName === 'turn_event' && data) {
    const pipelineRunId = data.pipeline_run_id;
    const stageRunId = data.stage_run_id;
    const turnId = data.turn_id;

    if (
      pipelineRunId &&
      pipelineRunId === appStore.activeRunId &&
      stageRunId &&
      turnId &&
      appStore.activeRunDetail &&
      Array.isArray(appStore.activeRunDetail.stage_runs)
    ) {
      const match = appStore.activeRunDetail.stage_runs.find((s) => s.id === stageRunId);
      if (match && !match.turn_id) {
        match.turn_id = turnId;
      }
    }

    if (pipelineRunId && pipelineRunId === appStore.activeRunId) {
      handleLiveStageStream(data);
    }
  } else if (eventName === 'turn_completed' && data && data.turn) {
    const turn = data.turn;
    const turnId = turn.id;
    const pipelineRunId = turn.pipeline_run_id;
    const stageRunId = turn.stage_run_id;

    if (
      pipelineRunId &&
      pipelineRunId === appStore.activeRunId &&
      stageRunId &&
      appStore.activeRunDetail &&
      Array.isArray(appStore.activeRunDetail.stage_runs)
    ) {
      const match = appStore.activeRunDetail.stage_runs.find((s) => s.id === stageRunId);
      if (match) {
        match.turn_id = turnId;
        if (!match.output_text && turn.response_text) {
          match.output_text = turn.response_text;
        }
      }
    }

    if (pipelineRunId && pipelineRunId === appStore.activeRunId) {
      renderActiveRunPanel();
    }
    // Also if history view is showing turns, refresh allTurns list if turn matches
    if (appStore.allTurns && Array.isArray(appStore.allTurns)) {
      const tIdx = appStore.allTurns.findIndex((t) => t.id === data.turn.id);
      if (tIdx >= 0) {
        appStore.allTurns[tIdx] = { ...appStore.allTurns[tIdx], ...data.turn };
      } else {
        appStore.allTurns.unshift(data.turn);
      }
      if (appStore.activeView === 'history' && historySubTab === 'turns') {
        renderHistoryList();
      }
    }
  }
}

export default {
  initRunsView,
  renderActiveRunPanel,
  renderHistoryView,
  handleRunSseEvent,
};
