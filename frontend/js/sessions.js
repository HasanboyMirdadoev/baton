/**
 * Baton — Sessions Sidebar, New Session Modal & Live Session Chat View
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
let turnTimerInterval = null;
let renderedSessionId = null;

const FALLBACK_MODELS = {
  claude: [
    { id: 'sonnet', label: 'Claude Sonnet (latest alias)' },
    { id: 'opus', label: 'Claude Opus (latest alias)' },
    { id: 'haiku', label: 'Claude Haiku (latest alias)' },
    { id: 'claude-sonnet-4-5', label: 'Claude Sonnet 4.5' },
    { id: 'claude-opus-4-5', label: 'Claude Opus 4.5' },
  ],
  antigravity: [
    { id: 'gemini-4-argon-high', label: 'Gemini 4 Argon (High)' },
    { id: 'gemini-3.8-flash-medium', label: 'Gemini 3.8 Flash (Medium)' },
    { id: 'claude-sonnet-5-5-medium', label: 'Claude Sonnet 5.5 (Medium via AGY)' },
  ],
};

/**
 * Extracts the basename of a filesystem path.
 * @param {string} pathStr
 * @returns {string}
 */
function getBasename(pathStr) {
  if (!pathStr) return '';
  const trimmed = String(pathStr).replace(/\/+$/, '');
  if (!trimmed) return '/';
  const parts = trimmed.split('/');
  return parts[parts.length - 1] || trimmed;
}

/**
 * Shortens a conversation ID for compact badge display.
 * @param {string|null} convId
 * @returns {string}
 */
function formatShortConvId(convId) {
  if (!convId) return 'not started';
  const s = String(convId);
  if (s.length <= 14) return `conv: ${s}`;
  return `conv: ${s.slice(0, 12)}…`;
}

/**
 * Checks whether a scrollable container is currently scrolled near the bottom.
 * @param {HTMLElement|null} el
 * @returns {boolean}
 */
function isNearBottom(el) {
  if (!el) return true;
  const threshold = 90;
  return el.scrollHeight - el.scrollTop - el.clientHeight <= threshold;
}

/**
 * Scrolls the chat transcript to the bottom.
 * @param {boolean} force
 */
function scrollTranscriptToBottom(force = false) {
  const transcript = document.getElementById('chat-transcript');
  if (!transcript) return;
  if (force || isNearBottom(transcript)) {
    transcript.scrollTop = transcript.scrollHeight;
  }
}

/**
 * Finds the currently active session object from store.sessions.
 * @returns {object|null}
 */
function getActiveSession() {
  if (!appStore || !appStore.activeSessionId) return null;
  return appStore.sessions.find((s) => s.id === appStore.activeSessionId) || null;
}

/**
 * Selects a session by ID, fetches its turns, and opens the Sessions view.
 * @param {string} sessionId
 */
async function selectSession(sessionId) {
  if (!appStore) return;
  appStore.activeSessionId = sessionId;

  if (appStore.activeView !== 'sessions' && appCallbacks.switchView) {
    appCallbacks.switchView('sessions', { sessionId });
    return;
  }

  renderSessionsSidebar();

  if (!sessionId) {
    appStore.activeSessionTurns = [];
    renderSessionChatView();
    return;
  }

  const session = getActiveSession();
  if (session) {
    renderSessionHeaderAndComposer(session);
  }

  try {
    const data = await api.getSession(sessionId);
    if (data && data.session) {
      const idx = appStore.sessions.findIndex((s) => s.id === data.session.id);
      if (idx >= 0) {
        appStore.sessions[idx] = data.session;
      } else {
        appStore.sessions.push(data.session);
      }
    }
    if (appStore.activeSessionId === sessionId) {
      appStore.activeSessionTurns = (data && Array.isArray(data.turns)) ? data.turns : [];
      renderSessionsSidebar();
      renderSessionChatView();
      scrollTranscriptToBottom(true);
    }
  } catch (err) {
    showToast(`Failed to load session: ${err.message || err}`, 'error');
  }
}

/**
 * Builds the HTML for a single session card in the sidebar.
 * @param {object} session
 * @returns {string}
 */
function buildSessionCardHtml(session) {
  const isSelected = session.id === appStore.activeSessionId;
  const isRunning = session.status === 'running';
  const turnCount = Number(session.turn_count || 0);
  const turnLabel = `${turnCount} ${turnCount === 1 ? 'turn' : 'turns'}`;
  const cwdBase = getBasename(session.working_dir || '');

  return `
    <div
      class="session-card${isSelected ? ' active selected' : ''}"
      data-session-id="${escapeHtml(session.id)}"
      role="listitem"
    >
      <div class="session-card-row1">
        <div class="session-card-title-group">
          ${renderStatusDot(session.status)}
          <span class="session-card-name" title="${escapeHtml(session.name)}">${escapeHtml(session.name)}</span>
        </div>
        ${
          isRunning
            ? `<button type="button" class="btn-stop-inline" data-stop-session-id="${escapeHtml(session.id)}" title="Stop running turn">■</button>`
            : ''
        }
      </div>
      <div class="session-card-row2">
        ${renderProviderBadge(session.provider)}
        ${renderRoleBadge(session.role)}
        <span class="badge-mono" title="${escapeHtml(session.model)}">${escapeHtml(session.model)}</span>
      </div>
      <div class="session-card-row3">
        <span>${escapeHtml(turnLabel)}</span>
        <span class="session-card-cwd" title="${escapeHtml(session.working_dir || '')}">${escapeHtml(cwdBase)}</span>
      </div>
    </div>
  `;
}

/**
 * Renders the persistent left sidebar sessions list (#sessions-list).
 */
export function renderSessionsSidebar() {
  const listEl = document.getElementById('sessions-list');
  if (!listEl || !appStore) return;

  const prevDetails = document.getElementById('sidebar-team-sessions-group');
  const wasGroupOpen = prevDetails ? prevDetails.open : false;

  const sessions = appStore.sessions || [];
  if (sessions.length === 0) {
    listEl.innerHTML = `
      <div class="sessions-empty-state">
        <p>No sessions yet. Create a Claude or Antigravity session to start chatting or building pipelines.</p>
        <button type="button" class="btn-secondary" id="btn-create-first-session">+ Create First Session</button>
      </div>
    `;
    const firstBtn = document.getElementById('btn-create-first-session');
    if (firstBtn) {
      firstBtn.addEventListener('click', () => openNewSessionModal());
    }
    return;
  }

  const standaloneSessions = sessions.filter((s) => !s.team_run_id);
  const teamSessions = sessions.filter((s) => Boolean(s.team_run_id));

  let standaloneHtml = standaloneSessions.map(buildSessionCardHtml).join('');
  if (!standaloneHtml && teamSessions.length > 0) {
    standaloneHtml = `
      <div class="sessions-empty-state" style="padding: 12px 8px;">
        <p>No standalone sessions yet.</p>
      </div>
    `;
  }

  let teamGroupHtml = '';
  if (teamSessions.length > 0) {
    const hasSelectedTeamSession = teamSessions.some((s) => s.id === appStore.activeSessionId);
    const openAttr = (wasGroupOpen || hasSelectedTeamSession) ? ' open' : '';
    const teamCardsHtml = teamSessions.map(buildSessionCardHtml).join('');
    teamGroupHtml = `
      <details id="sidebar-team-sessions-group" class="sidebar-team-group"${openAttr}>
        <summary class="sidebar-team-summary">👥 Team Run Sessions (${teamSessions.length})</summary>
        <div class="sidebar-team-list">
          ${teamCardsHtml}
        </div>
      </details>
    `;
  }

  listEl.innerHTML = `${standaloneHtml}${teamGroupHtml}`;

  // Bind click events on session cards and inline stop buttons
  const cardEls = listEl.querySelectorAll('.session-card');
  cardEls.forEach((card) => {
    card.addEventListener('click', (e) => {
      const stopBtn = e.target.closest('[data-stop-session-id]');
      if (stopBtn) return;
      const sid = card.getAttribute('data-session-id');
      if (sid) {
        selectSession(sid);
      }
    });
  });

  const stopBtns = listEl.querySelectorAll('[data-stop-session-id]');
  stopBtns.forEach((btn) => {
    btn.addEventListener('click', async (e) => {
      e.stopPropagation();
      const sid = btn.getAttribute('data-stop-session-id');
      if (!sid) return;
      btn.disabled = true;
      try {
        await api.stopSession(sid);
        showToast('Stopping running turn...', 'info');
      } catch (err) {
        showToast(`Failed to stop session: ${err.message || err}`, 'error');
        btn.disabled = false;
      }
    });
  });
}

/**
 * Reconstructs aggregated state from an array of NormalizedEvent items for a turn.
 * @param {Array<object>} events
 * @returns {{ thinkingText: string, textDeltaAccum: string, sawTextDelta: boolean, tools: Array<object>, deniedActions: Array<string> }}
 */
function aggregateTurnEvents(events) {
  const thinkingParts = [];
  const textParts = [];
  let sawTextDelta = false;
  const toolMap = new Map();
  const toolOrder = [];
  const deniedSet = new Set();

  for (const ev of events || []) {
    if (!ev || !ev.type) continue;
    if (ev.type === 'thinking_delta' && ev.text) {
      thinkingParts.push(ev.text);
    } else if (ev.type === 'text_delta' && ev.text) {
      sawTextDelta = true;
      textParts.push(ev.text);
    } else if (ev.type === 'tool_use') {
      const key = String(ev.tool_call_id || ev.seq || toolOrder.length + 1);
      if (!toolMap.has(key)) {
        const toolObj = {
          tool_call_id: key,
          tool_name: ev.tool_name || 'tool',
          tool_input: ev.tool_input !== undefined ? ev.tool_input : null,
          tool_output: null,
          is_error: false,
          status: 'running',
        };
        toolMap.set(key, toolObj);
        toolOrder.push(toolObj);
      } else {
        const existing = toolMap.get(key);
        if (ev.tool_name) existing.tool_name = ev.tool_name;
        if (ev.tool_input !== undefined) existing.tool_input = ev.tool_input;
      }
    } else if (ev.type === 'tool_result') {
      const key = String(ev.tool_call_id || '');
      let target = key ? toolMap.get(key) : null;
      if (!target && toolOrder.length > 0) {
        // Fallback to most recent running tool if tool_call_id was empty
        for (let i = toolOrder.length - 1; i >= 0; i--) {
          if (toolOrder[i].status === 'running') {
            target = toolOrder[i];
            break;
          }
        }
      }
      if (!target) {
        target = {
          tool_call_id: key || String(ev.seq || toolOrder.length + 1),
          tool_name: ev.tool_name || 'tool',
          tool_input: ev.tool_input !== undefined ? ev.tool_input : null,
          tool_output: ev.tool_output !== undefined ? ev.tool_output : '',
          is_error: Boolean(ev.is_error),
          status: ev.is_error ? 'error' : 'completed',
        };
        toolMap.set(target.tool_call_id, target);
        toolOrder.push(target);
      } else {
        if (ev.tool_name && target.tool_name === 'tool') {
          target.tool_name = ev.tool_name;
        }
        if (ev.tool_input && !target.tool_input) {
          target.tool_input = ev.tool_input;
        }
        target.tool_output = ev.tool_output !== undefined ? ev.tool_output : 'OK';
        target.is_error = Boolean(ev.is_error);
        target.status = ev.is_error ? 'error' : 'completed';
      }
    } else if (ev.type === 'turn_result') {
      if (Array.isArray(ev.denied_actions)) {
        ev.denied_actions.forEach((a) => deniedSet.add(a));
      }
    }
  }

  return {
    thinkingText: thinkingParts.join(''),
    textDeltaAccum: textParts.join(''),
    sawTextDelta,
    tools: toolOrder,
    deniedActions: Array.from(deniedSet),
  };
}

/**
 * Looks up pipeline name if a turn was triggered by a pipeline run.
 * @param {string|null} pipelineRunId
 * @returns {string|null}
 */
function getPipelineNameForTurn(pipelineRunId) {
  if (!pipelineRunId || !appStore) return null;
  const run = (appStore.pipelineRuns || []).find((r) => r.id === pipelineRunId);
  if (run && run.pipeline_name) return run.pipeline_name;
  return 'Pipeline Run';
}

/**
 * Builds the inner HTML of a .turn-agent-card for a turn.
 * @param {object} turn
 * @returns {string}
 */
function buildTurnAgentCardInnerHtml(turn) {
  const events = (appStore && appStore.liveTurnEvents && appStore.liveTurnEvents[turn.id]) || null;
  const agg = events ? aggregateTurnEvents(events) : {
    thinkingText: '',
    textDeltaAccum: '',
    sawTextDelta: false,
    tools: [],
    deniedActions: [],
  };

  const isRunning = turn.status === 'running';
  const responseText = turn.response_text || agg.textDeltaAccum || '';
  const hasThinking = Boolean(agg.thinkingText);
  const thinkingOpenAttr = (isRunning && !agg.sawTextDelta && !turn.response_text) ? ' open' : '';

  // Thinking block
  const thinkingHtml = `
    <details class="thinking-block${hasThinking ? '' : ' hidden'}" id="turn-thinking-${escapeHtml(turn.id)}"${thinkingOpenAttr}>
      <summary>💭 Reasoning / Thinking</summary>
      <div class="thinking-body" id="turn-thinking-body-${escapeHtml(turn.id)}">${escapeHtml(agg.thinkingText)}</div>
    </details>
  `;

  // Tool Call cards or On-Demand Load button for historical turns
  const toolCount = Number(turn.tool_call_count || agg.tools.length || 0);
  const failedToolCount = Number(turn.failed_tool_count || 0);
  let toolsInner = '';
  if (agg.tools.length > 0) {
    toolsInner = agg.tools
      .map((tool) => `<div class="turn-tool-item" data-tool-call-id="${escapeHtml(tool.tool_call_id)}">${renderToolCard(tool)}</div>`)
      .join('');
  } else if (!isRunning && toolCount > 0 && !events) {
    const failNote = failedToolCount > 0 ? ` (${failedToolCount} failed)` : '';
    toolsInner = `
      <button type="button" class="btn-secondary btn-load-turn-tools" data-load-turn-id="${escapeHtml(turn.id)}">
        🔧 Inspect ${toolCount} Tool ${toolCount === 1 ? 'Call' : 'Calls'}${escapeHtml(failNote)}
      </button>
    `;
  }

  const toolsContainerHtml = `
    <div class="turn-tools-container" id="turn-tools-${escapeHtml(turn.id)}" style="display: flex; flex-direction: column; gap: 6px;">
      ${toolsInner}
    </div>
  `;

  // Assistant Markdown Response
  const waitingHtml = (isRunning && !responseText && !hasThinking && agg.tools.length === 0)
    ? `<div class="form-hint" id="turn-waiting-${escapeHtml(turn.id)}">⏳ Waiting for agent response...</div>`
    : '';

  const markdownHtml = `
    <div class="assistant-markdown" id="turn-response-${escapeHtml(turn.id)}">
      ${responseText ? renderMarkdown(responseText) : waitingHtml}
    </div>
  `;

  // Denied actions warning banner
  const mergedDenied = Array.from(new Set([
    ...(Array.isArray(turn.denied_actions) ? turn.denied_actions : []),
    ...agg.deniedActions,
  ]));
  const deniedHtml = mergedDenied.length > 0
    ? `<div class="denied-actions-banner" id="turn-denied-${escapeHtml(turn.id)}">⚠️ Denied in Read-Only mode: ${escapeHtml(mergedDenied.join(', '))}</div>`
    : `<div class="denied-actions-banner hidden" id="turn-denied-${escapeHtml(turn.id)}"></div>`;

  // Error or Stopped status alert
  let statusAlertHtml = '';
  if (turn.status === 'error' || turn.status === 'timeout') {
    const errMsg = turn.error_message || (turn.status === 'timeout' ? 'Turn timed out.' : 'Turn failed with an error.');
    statusAlertHtml = `<div class="turn-error-box" id="turn-error-${escapeHtml(turn.id)}">${escapeHtml(errMsg)}</div>`;
  } else if (turn.status === 'stopped') {
    statusAlertHtml = `<div id="turn-stopped-${escapeHtml(turn.id)}"><span class="status-badge status-stopped">⏹ Turn stopped by user</span></div>`;
  }

  // Turn Footer
  const displayToolCount = Math.max(toolCount, agg.tools.length);
  const durationLabel = isRunning ? 'Running...' : formatDuration(turn.duration_ms);
  const footerHtml = `
    <div class="turn-footer" id="turn-footer-${escapeHtml(turn.id)}">
      <div style="display: flex; align-items: center; gap: 8px;">
        ${renderStatusBadge(turn.status)}
        <span id="turn-tool-count-${escapeHtml(turn.id)}">${displayToolCount} tool ${displayToolCount === 1 ? 'call' : 'calls'}</span>
      </div>
      <span id="turn-duration-${escapeHtml(turn.id)}">${escapeHtml(durationLabel)}</span>
    </div>
  `;

  // Handoff Actions ("Send to..." and "Continue in pipeline...")
  let actionsHtml = '';
  if (turn.status === 'completed' && Boolean((responseText || '').trim())) {
    actionsHtml = `
      <div class="turn-handoff-actions">
        <button type="button" class="btn-handoff btn-forward-turn" data-forward-turn-id="${escapeHtml(turn.id)}" title="Send this response to another session">
          <span>↗</span> Send to…
        </button>
        <button type="button" class="btn-handoff btn-pipeline-turn" data-pipeline-turn-id="${escapeHtml(turn.id)}" title="Continue this response in a pipeline">
          <span>▶</span> Continue in pipeline…
        </button>
      </div>
    `;
  }

  return `
    ${thinkingHtml}
    ${toolsContainerHtml}
    ${markdownHtml}
    ${deniedHtml}
    ${statusAlertHtml}
    ${actionsHtml}
    ${footerHtml}
  `;
}

/**
 * Builds the full HTML for a single turn (User Prompt Card + Agent Response Card).
 * @param {object} turn
 * @returns {string}
 */
function buildTurnPairHtml(turn) {
  const pipelineName = getPipelineNameForTurn(turn.pipeline_run_id);
  const pipelineBadge = pipelineName
    ? `<span class="badge-permission" style="margin-left: 6px;">🔗 Pipeline: ${escapeHtml(pipelineName)}</span>`
    : '';

  return `
    <div class="turn-pair" id="turn-pair-${escapeHtml(turn.id)}" data-turn-id="${escapeHtml(turn.id)}">
      <div class="turn-user-card" style="margin-bottom: 8px;">
        <div class="turn-user-header">
          <div>
            <strong>Turn #${escapeHtml(turn.turn_number)} · User</strong>
            ${pipelineBadge}
          </div>
          <span>${escapeHtml(formatTimestamp(turn.started_at))}</span>
        </div>
        <div class="turn-user-prompt">${escapeHtml(turn.prompt)}</div>
      </div>
      <div class="turn-agent-card" id="turn-agent-${escapeHtml(turn.id)}" data-turn-id="${escapeHtml(turn.id)}">
        ${buildTurnAgentCardInnerHtml(turn)}
      </div>
    </div>
  `;
}

/**
 * Binds on-demand "Inspect Tool Calls" buttons inside a container.
 * @param {HTMLElement} container
 */
function bindLoadTurnToolsButtons(container) {
  if (!container) return;
  const btns = container.querySelectorAll('[data-load-turn-id]');
  btns.forEach((btn) => {
    btn.addEventListener('click', async () => {
      const turnId = btn.getAttribute('data-load-turn-id');
      if (!turnId) return;
      btn.disabled = true;
      btn.textContent = '⏳ Loading tool events...';
      try {
        const res = await api.getTurn(turnId);
        if (res && Array.isArray(res.events)) {
          appStore.liveTurnEvents[turnId] = res.events;
        } else {
          appStore.liveTurnEvents[turnId] = [];
        }
        const turn = (appStore.activeSessionTurns || []).find((t) => t.id === turnId) || (res && res.turn);
        const agentCard = document.getElementById(`turn-agent-${turnId}`);
        if (agentCard && turn) {
          agentCard.innerHTML = buildTurnAgentCardInnerHtml(turn);
          // Open tool cards or first tool card so user immediately sees the loaded tool details
          const detailsList = agentCard.querySelectorAll('details.tool-card');
          if (detailsList.length === 1) {
            detailsList[0].open = true;
          }
        }
      } catch (err) {
        showToast(`Failed to load turn events: ${err.message || err}`, 'error');
        btn.disabled = false;
        btn.textContent = '🔧 Retry Loading Tool Calls';
      }
    });
  });
}

/**
 * Updates an existing turn's agent card in-place during live SSE streaming
 * without closing user-expanded tool call cards.
 * @param {string} turnId
 */
function updateLiveTurnCardDom(turnId) {
  if (!appStore) return;
  const transcript = document.getElementById('chat-transcript');
  if (!transcript) return;

  const shouldScroll = isNearBottom(transcript);
  let turn = (appStore.activeSessionTurns || []).find((t) => t.id === turnId);
  if (!turn && appStore.allTurns) {
    turn = appStore.allTurns.find((t) => t.id === turnId);
    if (turn && turn.session_id === appStore.activeSessionId) {
      appStore.activeSessionTurns.push(turn);
    }
  }
  if (!turn) return;

  let agentCard = document.getElementById(`turn-agent-${turnId}`);
  if (!agentCard) {
    transcript.insertAdjacentHTML('beforeend', buildTurnPairHtml(turn));
    agentCard = document.getElementById(`turn-agent-${turnId}`);
    bindLoadTurnToolsButtons(agentCard);
    if (shouldScroll) scrollTranscriptToBottom(true);
    return;
  }

  const events = appStore.liveTurnEvents[turnId] || [];
  const agg = aggregateTurnEvents(events);

  // 1. Update Thinking Block
  const thinkingDetails = document.getElementById(`turn-thinking-${turnId}`);
  const thinkingBody = document.getElementById(`turn-thinking-body-${turnId}`);
  if (thinkingDetails && thinkingBody) {
    if (agg.thinkingText) {
      const wasHidden = thinkingDetails.classList.contains('hidden');
      thinkingDetails.classList.remove('hidden');
      thinkingBody.textContent = agg.thinkingText;
      if (wasHidden && !agg.sawTextDelta && !turn.response_text) {
        thinkingDetails.open = true;
      }
      if (agg.sawTextDelta && thinkingDetails.open) {
        thinkingDetails.open = false;
      }
    }
  }

  // 2. Update Tool Cards while preserving open states
  const toolsContainer = document.getElementById(`turn-tools-${turnId}`);
  if (toolsContainer && agg.tools.length > 0) {
    const openToolIds = new Set();
    toolsContainer.querySelectorAll('.turn-tool-item').forEach((item) => {
      const tid = item.getAttribute('data-tool-call-id');
      const det = item.querySelector('details.tool-card');
      if (tid && det && det.open) {
        openToolIds.add(tid);
      }
    });

    toolsContainer.innerHTML = agg.tools
      .map((tool) => `<div class="turn-tool-item" data-tool-call-id="${escapeHtml(tool.tool_call_id)}">${renderToolCard(tool)}</div>`)
      .join('');

    toolsContainer.querySelectorAll('.turn-tool-item').forEach((item) => {
      const tid = item.getAttribute('data-tool-call-id');
      const det = item.querySelector('details.tool-card');
      if (tid && det && openToolIds.has(tid)) {
        det.open = true;
      }
    });
  }

  // 3. Update Assistant Markdown Response
  const responseEl = document.getElementById(`turn-response-${turnId}`);
  const effectiveText = turn.response_text || agg.textDeltaAccum || '';
  if (responseEl) {
    if (effectiveText) {
      responseEl.innerHTML = renderMarkdown(effectiveText);
    } else if (turn.status === 'running' && !agg.thinkingText && agg.tools.length === 0) {
      responseEl.innerHTML = `<div class="form-hint" id="turn-waiting-${escapeHtml(turnId)}">⏳ Waiting for agent response...</div>`;
    } else {
      responseEl.innerHTML = '';
    }
  }

  // 4. Update Denied Actions Banner
  const deniedEl = document.getElementById(`turn-denied-${turnId}`);
  const mergedDenied = Array.from(new Set([
    ...(Array.isArray(turn.denied_actions) ? turn.denied_actions : []),
    ...agg.deniedActions,
  ]));
  if (deniedEl && mergedDenied.length > 0) {
    deniedEl.classList.remove('hidden');
    deniedEl.textContent = `⚠️ Denied in Read-Only mode: ${mergedDenied.join(', ')}`;
  }

  // 5. Update Footer Tool Count
  const toolCountEl = document.getElementById(`turn-tool-count-${turnId}`);
  if (toolCountEl) {
    const count = Math.max(Number(turn.tool_call_count || 0), agg.tools.length);
    toolCountEl.textContent = `${count} tool ${count === 1 ? 'call' : 'calls'}`;
  }

  if (shouldScroll) {
    scrollTranscriptToBottom(true);
  }
}

/**
 * Starts or stops the live elapsed timer in the composer bar (#turn-timer).
 * @param {object|null} session
 */
function syncComposerTimer(session) {
  const timerEl = document.getElementById('turn-timer');
  if (turnTimerInterval) {
    clearInterval(turnTimerInterval);
    turnTimerInterval = null;
  }

  if (!timerEl) return;

  if (!session || session.status !== 'running') {
    timerEl.classList.add('hidden');
    return;
  }

  timerEl.classList.remove('hidden');

  // Find active running turn start timestamp
  const runningTurn = (appStore && appStore.activeSessionTurns || [])
    .slice()
    .reverse()
    .find((t) => t.status === 'running');

  const startMs = (runningTurn && runningTurn.started_at)
    ? new Date(runningTurn.started_at).getTime()
    : Date.now();

  const updateTick = () => {
    const elapsedSec = Math.max(0, Math.floor((Date.now() - startMs) / 1000));
    const mins = Math.floor(elapsedSec / 60);
    const secs = elapsedSec % 60;
    const mStr = mins < 10 ? `0${mins}` : `${mins}`;
    const sStr = secs < 10 ? `0${secs}` : `${secs}`;
    timerEl.textContent = `Running... ${mStr}:${sStr}`;
  };

  updateTick();
  turnTimerInterval = setInterval(updateTick, 1000);
}

/**
 * Updates the Session Header Bar and Composer controls without rebuilding the transcript.
 * @param {object} session
 */
function renderSessionHeaderAndComposer(session) {
  const nameEl = document.getElementById('session-header-name');
  const statusEl = document.getElementById('session-header-status');
  const providerEl = document.getElementById('session-header-provider');
  const modelEl = document.getElementById('session-header-model');
  const roleEl = document.getElementById('session-header-role');
  const cwdEl = document.getElementById('session-header-cwd');
  const convIdEl = document.getElementById('session-header-conv-id');
  const permSelect = document.getElementById('session-permission-override');
  const deleteBtn = document.getElementById('btn-delete-session');

  const isRunning = session.status === 'running';

  if (nameEl) nameEl.textContent = session.name || 'Session';
  if (statusEl) statusEl.innerHTML = renderStatusBadge(session.status);
  if (providerEl) providerEl.innerHTML = renderProviderBadge(session.provider);
  if (modelEl) modelEl.textContent = session.model || '';
  if (roleEl) roleEl.innerHTML = renderRoleBadge(session.role);
  if (cwdEl) {
    cwdEl.textContent = session.working_dir || '';
    cwdEl.title = session.working_dir || '';
  }
  if (convIdEl) {
    convIdEl.textContent = formatShortConvId(session.provider_session_id);
    convIdEl.title = session.provider_session_id
      ? `Provider Conversation ID: ${session.provider_session_id}`
      : 'Conversation ID will be captured on Turn 1';
  }
  if (permSelect) {
    permSelect.value = session.permission_level || 'readonly';
    permSelect.disabled = isRunning;
  }
  if (deleteBtn) {
    deleteBtn.disabled = isRunning;
  }

  // Composer controls
  const promptInput = document.getElementById('session-prompt-input');
  const sendBtn = document.getElementById('btn-send-turn');
  const stopBtn = document.getElementById('btn-stop-turn');

  if (promptInput) {
    promptInput.disabled = isRunning;
  }
  if (sendBtn) {
    sendBtn.classList.toggle('hidden', isRunning);
  }
  if (stopBtn) {
    stopBtn.classList.toggle('hidden', !isRunning);
  }

  syncComposerTimer(session);
}

/**
 * Renders the Session Chat View (#view-sessions: header, transcript, and composer).
 */
export function renderSessionChatView() {
  const emptyEl = document.getElementById('session-chat-empty');
  const activeEl = document.getElementById('session-chat-active');
  if (!emptyEl || !activeEl || !appStore) return;

  const session = getActiveSession();
  if (!session) {
    emptyEl.classList.remove('hidden');
    activeEl.classList.add('hidden');
    renderedSessionId = null;
    syncComposerTimer(null);
    return;
  }

  emptyEl.classList.add('hidden');
  activeEl.classList.remove('hidden');

  renderSessionHeaderAndComposer(session);

  const transcript = document.getElementById('chat-transcript');
  if (!transcript) return;

  const turns = appStore.activeSessionTurns || [];
  if (turns.length === 0) {
    transcript.innerHTML = `
      <div class="sessions-empty-state" style="margin: auto;">
        <p>No turns yet in <strong>${escapeHtml(session.name)}</strong>. Enter a prompt below to start Turn #1.</p>
      </div>
    `;
    renderedSessionId = session.id;
    return;
  }

  const switchingSession = renderedSessionId !== session.id;
  const wasNearBottom = isNearBottom(transcript);

  transcript.innerHTML = turns.map((turn) => buildTurnPairHtml(turn)).join('');
  bindLoadTurnToolsButtons(transcript);

  renderedSessionId = session.id;
  if (switchingSession || wasNearBottom) {
    scrollTranscriptToBottom(true);
  }
}

/**
 * Populates the Model <select> in the New Session Modal for a given provider ID.
 * @param {string} providerId
 */
function populateModalModelsForProvider(providerId) {
  const modelSelect = document.getElementById('new-session-model');
  const customGroup = document.getElementById('new-session-custom-model-group');
  const customInput = document.getElementById('new-session-custom-model');
  if (!modelSelect) return;

  const providers = (appStore && appStore.meta && appStore.meta.providers) || [];
  const providerMeta = providers.find((p) => p.id === providerId);
  const models = (providerMeta && Array.isArray(providerMeta.models) && providerMeta.models.length > 0)
    ? providerMeta.models
    : (FALLBACK_MODELS[providerId] || FALLBACK_MODELS.claude);

  const optionsHtml = models
    .map((m) => `<option value="${escapeHtml(m.id)}">${escapeHtml(m.label || m.id)}</option>`)
    .concat(['<option value="__custom__">Other / Custom model ID...</option>'])
    .join('');

  modelSelect.innerHTML = optionsHtml;
  if (models.length > 0) {
    modelSelect.value = models[0].id;
  }

  if (customGroup) customGroup.classList.add('hidden');
  if (customInput) {
    customInput.required = false;
    customInput.value = '';
  }
}

/**
 * Updates the permission override warning banner in the New Session Modal.
 */
function updateModalPermissionWarning() {
  const roleSelect = document.getElementById('new-session-role');
  const permSelect = document.getElementById('new-session-permission');
  const warningEl = document.getElementById('new-session-permission-warning');
  if (!roleSelect || !permSelect || !warningEl) return;

  const roleId = roleSelect.value;
  const roles = (appStore && appStore.meta && appStore.meta.roles) || [];
  const roleObj = roles.find((r) => r.id === roleId);
  const defaultPerm = (roleObj && roleObj.default_permission) || (roleId === 'implementer' ? 'edit' : 'readonly');
  const currentPerm = permSelect.value;

  const isOverriddenReadonly = defaultPerm === 'readonly' && (currentPerm === 'edit' || currentPerm === 'full');
  warningEl.classList.toggle('hidden', !isOverriddenReadonly);
}

/**
 * Updates role description, system prompt preview, and default permission when Role changes in Modal.
 */
function handleModalRoleChange() {
  const roleSelect = document.getElementById('new-session-role');
  const descEl = document.getElementById('new-session-role-desc');
  const previewEl = document.getElementById('new-session-system-prompt-preview');
  const permSelect = document.getElementById('new-session-permission');
  const detailsEl = document.getElementById('new-session-prompt-details');
  if (!roleSelect) return;

  const roleId = roleSelect.value;
  const roles = (appStore && appStore.meta && appStore.meta.roles) || [];
  const roleObj = roles.find((r) => r.id === roleId);

  if (descEl) {
    descEl.textContent = (roleObj && roleObj.description) || '';
  }
  if (previewEl) {
    previewEl.textContent = (roleObj && roleObj.system_prompt)
      ? roleObj.system_prompt
      : '(Custom role: uses your Additional Instructions below as the system prompt)';
  }
  if (permSelect) {
    permSelect.value = (roleObj && roleObj.default_permission) || (roleId === 'implementer' ? 'edit' : 'readonly');
  }
  if (detailsEl && roleId === 'custom') {
    detailsEl.open = true;
  }

  updateModalPermissionWarning();
}

/**
 * Opens and initializes the New Session Modal (#new-session-modal).
 */
export function openNewSessionModal() {
  const modal = document.getElementById('new-session-modal');
  if (!modal) return;

  const errorBox = document.getElementById('new-session-error');
  const nameInput = document.getElementById('new-session-name');
  const providerInput = document.getElementById('new-session-provider');
  const btnClaude = document.getElementById('provider-toggle-claude');
  const btnAgy = document.getElementById('provider-toggle-agy');
  const roleSelect = document.getElementById('new-session-role');
  const customPromptInput = document.getElementById('new-session-custom-prompt');
  const cwdInput = document.getElementById('new-session-working-dir');
  const detailsEl = document.getElementById('new-session-prompt-details');

  if (errorBox) {
    errorBox.textContent = '';
    errorBox.classList.add('hidden');
  }
  if (nameInput) nameInput.value = '';
  if (customPromptInput) customPromptInput.value = '';
  if (detailsEl) detailsEl.open = false;

  // Set default provider to 'claude'
  const initialProvider = 'claude';
  if (providerInput) providerInput.value = initialProvider;
  if (btnClaude) btnClaude.classList.add('active');
  if (btnAgy) btnAgy.classList.remove('active');

  populateModalModelsForProvider(initialProvider);

  // Populate Roles dropdown from store.meta.roles
  const roles = (appStore && appStore.meta && Array.isArray(appStore.meta.roles) && appStore.meta.roles.length > 0)
    ? appStore.meta.roles
    : [
        { id: 'designer', name: 'Designer', default_permission: 'readonly' },
        { id: 'planner', name: 'Planner', default_permission: 'readonly' },
        { id: 'implementer', name: 'Implementer', default_permission: 'edit' },
        { id: 'reviewer', name: 'Code Reviewer', default_permission: 'readonly' },
        { id: 'custom', name: 'Custom', default_permission: 'readonly' },
      ];

  if (roleSelect) {
    roleSelect.innerHTML = roles
      .map((r) => `<option value="${escapeHtml(r.id)}">${escapeHtml(r.name)}</option>`)
      .join('');
    roleSelect.value = roles[0].id;
  }

  handleModalRoleChange();

  if (cwdInput) {
    cwdInput.value = (appStore && appStore.meta && appStore.meta.default_working_dir) || '';
  }

  modal.classList.remove('hidden');
  if (nameInput) {
    setTimeout(() => nameInput.focus(), 20);
  }
}

/**
 * Closes the New Session Modal (#new-session-modal).
 */
function closeNewSessionModal() {
  const modal = document.getElementById('new-session-modal');
  if (modal) {
    modal.classList.add('hidden');
  }
}

/**
 * Formats a role ID into a display label.
 * @param {string} roleId
 * @returns {string}
 */
function formatRoleName(roleId) {
  const map = {
    designer: 'Designer',
    planner: 'Planner',
    implementer: 'Implementer',
    reviewer: 'Code Reviewer',
    custom: 'Custom',
    pm: 'PM',
    ui_designer: 'UI Designer',
    architect: 'Code Architect',
    ui_checker: 'UI Checker',
  };
  return map[(roleId || '').toLowerCase()] || roleId || 'Role';
}

/**
 * Updates provider and role badges below target session select in Forward modal.
 */
function updateForwardTargetBadges() {
  const targetSelect = document.getElementById('forward-target-session');
  const targetBadges = document.getElementById('forward-target-badges');
  const submitBtn = document.getElementById('btn-submit-forward');
  if (!targetSelect || !targetBadges) return;

  const sid = targetSelect.value;
  const sess = (appStore.sessions || []).find((s) => s.id === sid);
  if (!sess) {
    targetBadges.innerHTML = '';
    if (submitBtn) submitBtn.disabled = true;
    return;
  }

  const isBusy = sess.status === 'running';
  targetBadges.innerHTML = `
    ${renderProviderBadge(sess.provider)}
    ${renderRoleBadge(sess.role)}
    <span class="badge-mono">${escapeHtml(sess.model || '')}</span>
    ${isBusy ? '<span class="status-badge status-running"><span class="status-dot running"></span> Busy</span>' : ''}
  `;

  if (submitBtn) {
    submitBtn.disabled = isBusy;
  }
}

/**
 * Opens and initializes the Forward Turn ("Send to...") Modal.
 * @param {string} turnId
 */
export function openForwardTurnModal(turnId) {
  const modal = document.getElementById('forward-turn-modal');
  if (!modal || !appStore) return;

  const turn = (appStore.activeSessionTurns || []).find((t) => t.id === turnId) ||
    (appStore.allTurns || []).find((t) => t.id === turnId);
  if (!turn) return;

  const sourceSessionId = turn.session_id || appStore.activeSessionId;

  const turnIdInput = document.getElementById('forward-source-turn-id');
  const errorBox = document.getElementById('forward-turn-error');
  const targetSelect = document.getElementById('forward-target-session');
  const instructionsInput = document.getElementById('forward-instructions');
  const submitBtn = document.getElementById('btn-submit-forward');

  if (turnIdInput) turnIdInput.value = turn.id;
  if (errorBox) {
    errorBox.textContent = '';
    errorBox.classList.add('hidden');
  }
  if (instructionsInput) instructionsInput.value = '';

  const sessions = (appStore.sessions || []).filter((s) => s.id !== sourceSessionId);

  if (targetSelect) {
    if (sessions.length === 0) {
      targetSelect.innerHTML = '<option value="" disabled selected>No other sessions available</option>';
      if (submitBtn) submitBtn.disabled = true;
    } else {
      targetSelect.innerHTML = sessions.map((s) => {
        const isBusy = s.status === 'running';
        const provUpper = (s.provider || '').toUpperCase();
        const roleLabel = formatRoleName(s.role);
        const busyLabel = isBusy ? ' — (Busy: running turn)' : '';
        return `<option value="${escapeHtml(s.id)}"${isBusy ? ' disabled' : ''}>${escapeHtml(s.name)} [${escapeHtml(provUpper)} · ${escapeHtml(roleLabel)}]${busyLabel}</option>`;
      }).join('');

      // Select first non-running session if available
      const firstAvailable = sessions.find((s) => s.status !== 'running') || sessions[0];
      targetSelect.value = firstAvailable.id;
      if (submitBtn) submitBtn.disabled = firstAvailable.status === 'running';
    }
  }

  updateForwardTargetBadges();
  modal.classList.remove('hidden');
  if (instructionsInput) {
    setTimeout(() => instructionsInput.focus(), 20);
  }
}

/**
 * Closes the Forward Turn Modal.
 */
function closeForwardTurnModal() {
  const modal = document.getElementById('forward-turn-modal');
  if (modal) {
    modal.classList.add('hidden');
  }
}

/**
 * Populates start stages dropdown in Continue Pipeline Modal.
 * @param {string} pipelineId
 */
function populatePipelineStagesForModal(pipelineId) {
  const stageSelect = document.getElementById('continue-pipeline-stage-select');
  const stageHint = document.getElementById('continue-pipeline-stage-hint');
  const submitBtn = document.getElementById('btn-submit-pipeline-start');
  if (!stageSelect) return;

  const pipe = (appStore.pipelines || []).find((p) => p.id === pipelineId);
  const stages = (pipe && Array.isArray(pipe.stages)) ? pipe.stages : [];

  if (stages.length === 0) {
    stageSelect.innerHTML = '<option value="" disabled selected>Pipeline has no stages</option>';
    if (stageHint) stageHint.textContent = 'This pipeline has no stages configured.';
    if (submitBtn) submitBtn.disabled = true;
    return;
  }

  // Contract B: start_stage_index must be > 0 when initial_input_turn_id is provided.
  // Stage 1 (index 0) cannot receive handoff input and is disabled.
  const optionsHtml = stages.map((st, idx) => {
    const sess = (appStore.sessions || []).find((s) => s.id === st.session_id);
    const sessName = sess ? sess.name : 'Unassigned';
    const roleLabel = sess ? formatRoleName(sess.role) : 'Role';
    const label = `Stage ${idx + 1}: ${st.name} — ${sessName} (${roleLabel})`;
    const isDisabled = idx === 0;
    const note = isDisabled ? ' [Stage 1 cannot receive handoff input]' : '';
    return `<option value="${idx}"${isDisabled ? ' disabled' : ''}>${escapeHtml(label)}${note}</option>`;
  }).join('');

  stageSelect.innerHTML = optionsHtml;

  if (stages.length > 1) {
    stageSelect.value = '1';
    if (stageHint) {
      stageHint.textContent = 'Earlier stages will be skipped; this turn’s response will be fed as initial input to the selected stage.';
    }
    if (submitBtn) submitBtn.disabled = false;
  } else {
    stageSelect.value = '0';
    if (stageHint) {
      stageHint.textContent = 'This pipeline only has 1 stage. Pipelines must have at least 2 stages to continue from a later stage.';
    }
    if (submitBtn) submitBtn.disabled = true;
  }
}

/**
 * Opens and initializes the Continue in Pipeline Modal.
 * @param {string} turnId
 */
export async function openContinuePipelineModal(turnId) {
  const modal = document.getElementById('continue-pipeline-modal');
  if (!modal || !appStore) return;

  const turn = (appStore.activeSessionTurns || []).find((t) => t.id === turnId) ||
    (appStore.allTurns || []).find((t) => t.id === turnId);
  if (!turn) return;

  if (!appStore.pipelines || appStore.pipelines.length === 0) {
    try {
      const res = await api.listPipelines();
      if (res && Array.isArray(res.pipelines)) {
        appStore.pipelines = res.pipelines;
      }
    } catch (_) {}
  }

  const turnIdInput = document.getElementById('pipeline-source-turn-id');
  const errorBox = document.getElementById('continue-pipeline-error');
  const pipelineSelect = document.getElementById('continue-pipeline-select');
  const taskPromptInput = document.getElementById('continue-pipeline-task-prompt');
  const submitBtn = document.getElementById('btn-submit-pipeline-start');

  if (turnIdInput) turnIdInput.value = turn.id;
  if (errorBox) {
    errorBox.textContent = '';
    errorBox.classList.add('hidden');
  }
  if (taskPromptInput) {
    taskPromptInput.value = turn.prompt || '';
  }

  const pipelines = appStore.pipelines || [];
  if (pipelineSelect) {
    if (pipelines.length === 0) {
      pipelineSelect.innerHTML = '<option value="" disabled selected>No saved pipelines available</option>';
      const stageSelect = document.getElementById('continue-pipeline-stage-select');
      if (stageSelect) stageSelect.innerHTML = '<option value="" disabled selected>No stages</option>';
      if (submitBtn) submitBtn.disabled = true;
    } else {
      pipelineSelect.innerHTML = pipelines.map((p) => `<option value="${escapeHtml(p.id)}">${escapeHtml(p.name)}</option>`).join('');

      if (appStore.activePipelineId && pipelines.some((p) => p.id === appStore.activePipelineId)) {
        pipelineSelect.value = appStore.activePipelineId;
      } else {
        pipelineSelect.value = pipelines[0].id;
      }

      populatePipelineStagesForModal(pipelineSelect.value);
    }
  }

  modal.classList.remove('hidden');
}

/**
 * Closes the Continue in Pipeline Modal.
 */
function closeContinuePipelineModal() {
  const modal = document.getElementById('continue-pipeline-modal');
  if (modal) {
    modal.classList.add('hidden');
  }
}

/**
 * Handles live SSE events relevant to sessions and turns.
 * @param {string} eventName
 * @param {object} data
 */
export function handleSessionSseEvent(eventName, data) {
  if (!appStore || !data) return;

  if (eventName === 'session_updated' && data.session) {
    const updated = data.session;
    const idx = appStore.sessions.findIndex((s) => s.id === updated.id);
    if (idx >= 0) {
      appStore.sessions[idx] = updated;
    } else {
      appStore.sessions.push(updated);
    }

    renderSessionsSidebar();
    if (appStore.activeSessionId === updated.id) {
      renderSessionHeaderAndComposer(updated);
    }
  } else if (eventName === 'turn_started' && data.turn) {
    const turn = data.turn;
    if (!appStore.liveTurnEvents[turn.id]) {
      appStore.liveTurnEvents[turn.id] = [];
    }

    // Update session status & turn_count in store
    const sess = appStore.sessions.find((s) => s.id === turn.session_id);
    if (sess) {
      sess.status = 'running';
      sess.turn_count = Math.max(Number(sess.turn_count || 0), Number(turn.turn_number || 1));
    }
    renderSessionsSidebar();

    if (turn.session_id === appStore.activeSessionId) {
      const existingIdx = appStore.activeSessionTurns.findIndex((t) => t.id === turn.id);
      if (existingIdx >= 0) {
        appStore.activeSessionTurns[existingIdx] = turn;
      } else {
        appStore.activeSessionTurns.push(turn);
      }

      if (sess) {
        renderSessionHeaderAndComposer(sess);
      }

      const transcript = document.getElementById('chat-transcript');
      if (transcript) {
        // Remove empty state placeholder if present
        const emptyPlaceholder = transcript.querySelector('.sessions-empty-state');
        if (emptyPlaceholder) {
          transcript.innerHTML = '';
        }
        const existingPair = document.getElementById(`turn-pair-${turn.id}`);
        if (!existingPair) {
          transcript.insertAdjacentHTML('beforeend', buildTurnPairHtml(turn));
        } else {
          const agentCard = document.getElementById(`turn-agent-${turn.id}`);
          if (agentCard) {
            agentCard.innerHTML = buildTurnAgentCardInnerHtml(turn);
          }
        }
        scrollTranscriptToBottom(true);
      }
    }
  } else if (eventName === 'turn_event' && data.turn_id && data.event) {
    const { session_id: sessionId, turn_id: turnId, event: ev } = data;

    // Update provider_session_id if session_init event arrives
    if (ev.type === 'session_init' && ev.provider_conversation_id) {
      const sess = appStore.sessions.find((s) => s.id === sessionId);
      if (sess && !sess.provider_session_id) {
        sess.provider_session_id = ev.provider_conversation_id;
        if (sessionId === appStore.activeSessionId) {
          renderSessionHeaderAndComposer(sess);
        }
      }
    }

    if (sessionId === appStore.activeSessionId) {
      updateLiveTurnCardDom(turnId);
    }
  } else if (eventName === 'turn_completed' && data.turn) {
    const turn = data.turn;

    if (turn.session_id === appStore.activeSessionId) {
      const idx = appStore.activeSessionTurns.findIndex((t) => t.id === turn.id);
      if (idx >= 0) {
        appStore.activeSessionTurns[idx] = turn;
      } else {
        appStore.activeSessionTurns.push(turn);
      }

      const transcript = document.getElementById('chat-transcript');
      const shouldScroll = isNearBottom(transcript);
      const agentCard = document.getElementById(`turn-agent-${turn.id}`);
      if (agentCard) {
        agentCard.innerHTML = buildTurnAgentCardInnerHtml(turn);
        bindLoadTurnToolsButtons(agentCard);
      } else {
        renderSessionChatView();
      }
      if (shouldScroll) {
        scrollTranscriptToBottom(true);
      }

      const sess = getActiveSession();
      if (sess) {
        renderSessionHeaderAndComposer(sess);
      }
    }
  }
}

/**
 * Initializes all event listeners for the Sessions sidebar, New Session Modal,
 * Session Header actions, and Chat Composer.
 * @param {object} store
 * @param {object} callbacks
 */
export function initSessionsView(store, callbacks = {}) {
  appStore = store;
  appCallbacks = callbacks;

  // Sidebar "+ New Session" button
  const btnNewSession = document.getElementById('btn-new-session');
  if (btnNewSession) {
    btnNewSession.addEventListener('click', () => openNewSessionModal());
  }

  // Modal close / cancel buttons & backdrop click
  const modal = document.getElementById('new-session-modal');
  const btnCloseModal = document.getElementById('btn-close-modal');
  const btnCancelModal = document.getElementById('btn-cancel-new-session');

  if (btnCloseModal) {
    btnCloseModal.addEventListener('click', closeNewSessionModal);
  }
  if (btnCancelModal) {
    btnCancelModal.addEventListener('click', closeNewSessionModal);
  }
  if (modal) {
    modal.addEventListener('click', (e) => {
      if (e.target === modal) {
        closeNewSessionModal();
      }
    });
  }

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      if (modal && !modal.classList.contains('hidden')) {
        closeNewSessionModal();
      }
      const fwdModal = document.getElementById('forward-turn-modal');
      if (fwdModal && !fwdModal.classList.contains('hidden')) {
        closeForwardTurnModal();
      }
      const pipeModal = document.getElementById('continue-pipeline-modal');
      if (pipeModal && !pipeModal.classList.contains('hidden')) {
        closeContinuePipelineModal();
      }
    }
  });

  // Modal Provider segmented toggle
  const btnClaude = document.getElementById('provider-toggle-claude');
  const btnAgy = document.getElementById('provider-toggle-agy');
  const providerInput = document.getElementById('new-session-provider');

  const selectProvider = (providerId) => {
    if (providerInput) providerInput.value = providerId;
    if (btnClaude) btnClaude.classList.toggle('active', providerId === 'claude');
    if (btnAgy) btnAgy.classList.toggle('active', providerId === 'antigravity');
    populateModalModelsForProvider(providerId);
  };

  if (btnClaude) {
    btnClaude.addEventListener('click', () => selectProvider('claude'));
  }
  if (btnAgy) {
    btnAgy.addEventListener('click', () => selectProvider('antigravity'));
  }

  // Modal Model dropdown ("Other / Custom model ID...")
  const modelSelect = document.getElementById('new-session-model');
  const customModelGroup = document.getElementById('new-session-custom-model-group');
  const customModelInput = document.getElementById('new-session-custom-model');

  if (modelSelect) {
    modelSelect.addEventListener('change', () => {
      const isCustom = modelSelect.value === '__custom__';
      if (customModelGroup) {
        customModelGroup.classList.toggle('hidden', !isCustom);
      }
      if (customModelInput) {
        customModelInput.required = isCustom;
        if (isCustom) customModelInput.focus();
      }
    });
  }

  // Modal Role & Permission dropdowns
  const roleSelect = document.getElementById('new-session-role');
  const permSelect = document.getElementById('new-session-permission');

  if (roleSelect) {
    roleSelect.addEventListener('change', handleModalRoleChange);
  }
  if (permSelect) {
    permSelect.addEventListener('change', updateModalPermissionWarning);
  }

  // Modal Form submission
  const newSessionForm = document.getElementById('new-session-form');
  if (newSessionForm) {
    newSessionForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      const errorBox = document.getElementById('new-session-error');
      const submitBtn = document.getElementById('btn-submit-new-session');

      if (errorBox) {
        errorBox.textContent = '';
        errorBox.classList.add('hidden');
      }

      const name = (document.getElementById('new-session-name')?.value || '').trim();
      const provider = document.getElementById('new-session-provider')?.value || 'claude';
      const selectedModel = document.getElementById('new-session-model')?.value || '';
      const customModel = (document.getElementById('new-session-custom-model')?.value || '').trim();
      const model = selectedModel === '__custom__' ? customModel : selectedModel;
      const role = document.getElementById('new-session-role')?.value || 'designer';
      const customPrompt = document.getElementById('new-session-custom-prompt')?.value || '';
      const permissionLevel = document.getElementById('new-session-permission')?.value || 'readonly';
      const workingDir = (document.getElementById('new-session-working-dir')?.value || '').trim();

      if (!name || !model || !workingDir) {
        if (errorBox) {
          errorBox.textContent = 'Please provide a session name, model, and working directory.';
          errorBox.classList.remove('hidden');
        }
        return;
      }

      if (submitBtn) submitBtn.disabled = true;

      try {
        const created = await api.createSession({
          name,
          provider,
          model,
          role,
          custom_prompt: customPrompt,
          permission_level: permissionLevel,
          working_dir: workingDir,
        });

        closeNewSessionModal();
        showToast(`Created session "${created.name}"`, 'success');

        if (appCallbacks.refreshSessions) {
          await appCallbacks.refreshSessions();
        }
        await selectSession(created.id);
      } catch (err) {
        if (errorBox) {
          errorBox.textContent = err.message || 'Failed to create session';
          errorBox.classList.remove('hidden');
        } else {
          showToast(err.message || 'Failed to create session', 'error');
        }
      } finally {
        if (submitBtn) submitBtn.disabled = false;
      }
    });
  }

  // Session Header: Quick Permission Override
  const headerPermSelect = document.getElementById('session-permission-override');
  if (headerPermSelect) {
    headerPermSelect.addEventListener('change', async () => {
      const session = getActiveSession();
      if (!session) return;
      const newPerm = headerPermSelect.value;
      const prevPerm = session.permission_level;
      try {
        const updated = await api.updateSession(session.id, { permission_level: newPerm });
        const idx = appStore.sessions.findIndex((s) => s.id === session.id);
        if (idx >= 0 && updated) {
          appStore.sessions[idx] = updated;
        }
        renderSessionHeaderAndComposer(updated || session);
        renderSessionsSidebar();
        showToast(`Permission updated to ${newPerm}`, 'info');
      } catch (err) {
        headerPermSelect.value = prevPerm;
        showToast(`Failed to update permission: ${err.message || err}`, 'error');
      }
    });
  }

  // Session Header: Delete Session button
  const btnDeleteSession = document.getElementById('btn-delete-session');
  if (btnDeleteSession) {
    btnDeleteSession.addEventListener('click', async () => {
      const session = getActiveSession();
      if (!session) return;
      const confirmed = window.confirm(`Delete session "${session.name}" and all its turns?`);
      if (!confirmed) return;

      btnDeleteSession.disabled = true;
      try {
        await api.deleteSession(session.id);
        showToast(`Deleted session "${session.name}"`, 'info');
        appStore.sessions = appStore.sessions.filter((s) => s.id !== session.id);
        const nextId = appStore.sessions.length > 0 ? appStore.sessions[0].id : null;
        await selectSession(nextId);
        if (appCallbacks.refreshSessions) {
          await appCallbacks.refreshSessions();
        }
      } catch (err) {
        showToast(err.message || 'Failed to delete session', 'error');
        btnDeleteSession.disabled = false;
      }
    });
  }

  // Composer: Prompt form submission & Cmd+Enter / Ctrl+Enter shortcut
  const promptForm = document.getElementById('session-prompt-form');
  const promptInput = document.getElementById('session-prompt-input');
  const btnStopTurn = document.getElementById('btn-stop-turn');

  const submitPromptTurn = async () => {
    const session = getActiveSession();
    if (!session || !promptInput) return;
    const prompt = promptInput.value.trim();
    if (!prompt) return;

    const savedText = promptInput.value;
    promptInput.value = '';
    promptInput.disabled = true;

    try {
      await api.startTurn(session.id, { prompt });
      session.status = 'running';
      renderSessionHeaderAndComposer(session);
      renderSessionsSidebar();
    } catch (err) {
      promptInput.value = savedText;
      promptInput.disabled = false;
      showToast(err.message || 'Failed to start turn', 'error');
    }
  };

  if (promptForm) {
    promptForm.addEventListener('submit', (e) => {
      e.preventDefault();
      submitPromptTurn();
    });
  }

  if (promptInput) {
    promptInput.addEventListener('keydown', (e) => {
      if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
        e.preventDefault();
        submitPromptTurn();
      }
    });
  }

  if (btnStopTurn) {
    btnStopTurn.addEventListener('click', async () => {
      const session = getActiveSession();
      if (!session) return;
      btnStopTurn.disabled = true;
      try {
        await api.stopSession(session.id);
        showToast('Stopping turn...', 'info');
      } catch (err) {
        showToast(`Failed to stop turn: ${err.message || err}`, 'error');
      } finally {
        btnStopTurn.disabled = false;
      }
    });
  }

  // Delegated click handler on transcript for "Send to..." and "Continue in pipeline..."
  const transcript = document.getElementById('chat-transcript');
  if (transcript) {
    transcript.addEventListener('click', (e) => {
      const fwdBtn = e.target.closest('[data-forward-turn-id]');
      if (fwdBtn) {
        const turnId = fwdBtn.getAttribute('data-forward-turn-id');
        if (turnId) openForwardTurnModal(turnId);
        return;
      }

      const pipeBtn = e.target.closest('[data-pipeline-turn-id]');
      if (pipeBtn) {
        const turnId = pipeBtn.getAttribute('data-pipeline-turn-id');
        if (turnId) openContinuePipelineModal(turnId);
        return;
      }
    });
  }

  // Forward Turn Modal event handlers
  const fwdModal = document.getElementById('forward-turn-modal');
  const btnCloseFwd = document.getElementById('btn-close-forward-modal');
  const btnCancelFwd = document.getElementById('btn-cancel-forward-modal');
  const fwdTargetSelect = document.getElementById('forward-target-session');
  const fwdForm = document.getElementById('forward-turn-form');

  if (btnCloseFwd) {
    btnCloseFwd.addEventListener('click', closeForwardTurnModal);
  }
  if (btnCancelFwd) {
    btnCancelFwd.addEventListener('click', closeForwardTurnModal);
  }
  if (fwdModal) {
    fwdModal.addEventListener('click', (e) => {
      if (e.target === fwdModal) {
        closeForwardTurnModal();
      }
    });
  }
  if (fwdTargetSelect) {
    fwdTargetSelect.addEventListener('change', updateForwardTargetBadges);
  }

  if (fwdForm) {
    fwdForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      const turnId = document.getElementById('forward-source-turn-id')?.value;
      const targetSessionId = document.getElementById('forward-target-session')?.value;
      const instructions = (document.getElementById('forward-instructions')?.value || '').trim();
      const errorBox = document.getElementById('forward-turn-error');
      const submitBtn = document.getElementById('btn-submit-forward');

      if (!turnId || !targetSessionId) return;

      if (errorBox) {
        errorBox.textContent = '';
        errorBox.classList.add('hidden');
      }

      if (submitBtn) {
        submitBtn.disabled = true;
        submitBtn.textContent = 'Sending...';
      }

      try {
        const payload = { target_session_id: targetSessionId };
        if (instructions) {
          payload.instructions = instructions;
        }

        // Switch to the target session and load its turns first so that:
        // 1. Target session's history is fetched and rendered in the chat view.
        // 2. targetSessionId is activeSessionId when the backend starts the turn and emits SSE turn_started.
        closeForwardTurnModal();
        await selectSession(targetSessionId);

        const targetSess = (appStore.sessions || []).find((s) => s.id === targetSessionId);
        const sessName = targetSess ? targetSess.name : 'target session';
        showToast(`Forwarded to "${sessName}"`, 'success');

        const forwardRes = await api.forwardTurn(turnId, payload);

        // Ensure newly created running turn is in activeSessionTurns if SSE turn_started was delayed
        if (forwardRes && forwardRes.turn_id && appStore.activeSessionId === targetSessionId) {
          const alreadyRegistered = (appStore.activeSessionTurns || []).some((t) => t.id === forwardRes.turn_id);
          if (!alreadyRegistered) {
            try {
              const turnDetail = await api.getTurn(forwardRes.turn_id);
              if (turnDetail && turnDetail.turn && appStore.activeSessionId === targetSessionId) {
                if (!appStore.activeSessionTurns.some((t) => t.id === forwardRes.turn_id)) {
                  appStore.activeSessionTurns.push(turnDetail.turn);
                  const transcript = document.getElementById('chat-transcript');
                  if (transcript) {
                    const emptyPlaceholder = transcript.querySelector('.sessions-empty-state');
                    if (emptyPlaceholder) {
                      transcript.innerHTML = '';
                    }
                    const existingPair = document.getElementById(`turn-pair-${turnDetail.turn.id}`);
                    if (!existingPair) {
                      transcript.insertAdjacentHTML('beforeend', buildTurnPairHtml(turnDetail.turn));
                    }
                    scrollTranscriptToBottom(true);
                  }
                }
              }
            } catch (_) {}
          }
        }
      } catch (err) {
        showToast(err.detail || err.message || 'Failed to forward turn', 'error');
      } finally {
        if (submitBtn) {
          submitBtn.disabled = false;
          submitBtn.textContent = 'Send';
        }
      }
    });
  }

  // Continue in Pipeline Modal event handlers
  const pipeModal = document.getElementById('continue-pipeline-modal');
  const btnClosePipe = document.getElementById('btn-close-pipeline-modal');
  const btnCancelPipe = document.getElementById('btn-cancel-pipeline-modal');
  const pipeSelect = document.getElementById('continue-pipeline-select');
  const pipeForm = document.getElementById('continue-pipeline-form');

  if (btnClosePipe) {
    btnClosePipe.addEventListener('click', closeContinuePipelineModal);
  }
  if (btnCancelPipe) {
    btnCancelPipe.addEventListener('click', closeContinuePipelineModal);
  }
  if (pipeModal) {
    pipeModal.addEventListener('click', (e) => {
      if (e.target === pipeModal) {
        closeContinuePipelineModal();
      }
    });
  }
  if (pipeSelect) {
    pipeSelect.addEventListener('change', () => {
      populatePipelineStagesForModal(pipeSelect.value);
    });
  }

  if (pipeForm) {
    pipeForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      const turnId = document.getElementById('pipeline-source-turn-id')?.value;
      const pipelineId = document.getElementById('continue-pipeline-select')?.value;
      const stageSelect = document.getElementById('continue-pipeline-stage-select');
      const startStageIndex = stageSelect ? parseInt(stageSelect.value, 10) : 0;
      const taskPrompt = (document.getElementById('continue-pipeline-task-prompt')?.value || '').trim();
      const errorBox = document.getElementById('continue-pipeline-error');
      const submitBtn = document.getElementById('btn-submit-pipeline-start');

      if (!turnId || !pipelineId) return;

      if (startStageIndex === 0) {
        if (errorBox) {
          errorBox.textContent = 'Please choose Stage 2 or later. Stage 1 cannot receive handoff input.';
          errorBox.classList.remove('hidden');
        }
        return;
      }

      if (errorBox) {
        errorBox.textContent = '';
        errorBox.classList.add('hidden');
      }

      if (submitBtn) {
        submitBtn.disabled = true;
        submitBtn.textContent = 'Starting...';
      }

      try {
        const payload = {
          task_prompt: taskPrompt,
          start_stage_index: startStageIndex,
          initial_input_turn_id: turnId,
        };

        const res = await api.startPipelineRun(pipelineId, payload);
        closeContinuePipelineModal();
        showToast('Pipeline run started', 'success');

        if (res && res.run) {
          appStore.activeRunId = res.run.id;
          appStore.activeRunDetail = res;
        }

        if (appCallbacks.switchView) {
          appCallbacks.switchView('pipelines', {
            runId: res && res.run ? res.run.id : undefined,
            pipelineId,
          });
        }

        const pipeSubtabRun = document.getElementById('pipe-subtab-run');
        if (pipeSubtabRun) {
          pipeSubtabRun.click();
        }
      } catch (err) {
        if (errorBox) {
          errorBox.textContent = err.detail || err.message || 'Failed to start pipeline run';
          errorBox.classList.remove('hidden');
        } else {
          showToast(err.detail || err.message || 'Failed to start pipeline run', 'error');
        }
      } finally {
        if (submitBtn) {
          submitBtn.disabled = false;
          submitBtn.textContent = 'Start';
        }
      }
    });
  }
}

export default {
  initSessionsView,
  renderSessionsSidebar,
  renderSessionChatView,
  openNewSessionModal,
  openForwardTurnModal,
  openContinuePipelineModal,
  handleSessionSseEvent,
};
