/**
 * Baton — Main Application Entry, View Router, Shared Store & SSE Manager
 */

import { api } from './api.js';
import { showToast } from './components.js';
import {
  initSessionsView,
  renderSessionsSidebar,
  renderSessionChatView,
  openNewSessionModal,
  handleSessionSseEvent,
} from './sessions.js';
import {
  initPipelinesView,
  renderPipelineBuilder,
  handlePipelineSseEvent,
} from './pipelines.js';
import {
  initRunsView,
  renderActiveRunPanel,
  renderHistoryView,
  handleRunSseEvent,
} from './runs.js';
import {
  initTeamView,
  renderTeamView,
  updateTeamInboxBadge,
  handleTeamSseEvent,
} from './team.js';

/**
 * Shared application reactive state store (Clarification C7.3).
 */
export const store = {
  meta: {
    default_working_dir: '',
    providers: [],
    roles: [
      {
        id: 'designer',
        name: 'Designer',
        description: 'Produces system architecture and UI/UX design specifications. Read-only by default.',
        default_permission: 'readonly',
        system_prompt: '',
      },
      {
        id: 'planner',
        name: 'Planner',
        description: 'Breaks tasks and designs into step-by-step file-level implementation plans. Read-only by default.',
        default_permission: 'readonly',
        system_prompt: '',
      },
      {
        id: 'implementer',
        name: 'Implementer',
        description: 'Writes and edits code in the working directory according to the plan or review feedback.',
        default_permission: 'edit',
        system_prompt: '',
      },
      {
        id: 'reviewer',
        name: 'Code Reviewer',
        description: 'Reviews workspace changes against requirements and emits a machine-readable verdict.',
        default_permission: 'readonly',
        system_prompt: '',
      },
      {
        id: 'custom',
        name: 'Custom',
        description: 'Custom role with user-defined system instructions.',
        default_permission: 'readonly',
        system_prompt: '',
      },
    ],
    team_roles: [],
    default_team_members: [],
    permissions: [
      { id: 'readonly', label: 'Read-Only (Plan)', description: 'Inspect files only; no edits or shell commands.' },
      { id: 'edit', label: 'Edit Files', description: 'Auto-accept file edits in workspace.' },
      { id: 'full', label: 'Full Access', description: 'Auto-approve file edits and shell commands.' },
    ],
  },
  sessions: [],
  activeSessionId: null,
  activeSessionTurns: [],
  liveTurnEvents: {},
  pipelines: [],
  activePipelineId: null,
  pipelineRuns: [],
  activeRunId: null,
  activeRunDetail: null,
  teams: [],
  activeTeamId: null,
  teamRuns: [],
  activeTeamRunId: null,
  activeTeamRunDetail: null,
  teamRunTurns: {},
  allTurns: [],
  activeView: 'sessions',
  sseConnected: false,
};

let eventSourceInstance = null;

/**
 * Loads a specific session's details and turns from the backend and updates the chat view.
 * @param {string|null} sessionId
 */
async function loadAndSelectSession(sessionId) {
  store.activeSessionId = sessionId;
  renderSessionsSidebar();

  if (!sessionId) {
    store.activeSessionTurns = [];
    renderSessionChatView();
    return;
  }

  renderSessionChatView();

  try {
    const detail = await api.getSession(sessionId);
    if (detail && detail.session) {
      const idx = store.sessions.findIndex((s) => s.id === detail.session.id);
      if (idx >= 0) {
        store.sessions[idx] = detail.session;
      } else {
        store.sessions.push(detail.session);
      }
    }
    if (store.activeSessionId === sessionId) {
      store.activeSessionTurns = (detail && Array.isArray(detail.turns)) ? detail.turns : [];
      renderSessionsSidebar();
      renderSessionChatView();
    }
  } catch (err) {
    showToast(`Failed to load session: ${err.message || err}`, 'error');
  }
}

/**
 * Switches top-level workspace view ('sessions' | 'pipelines' | 'team' | 'history')
 * and optionally selects a sessionId, pipelineId, runId, or teamRunId.
 * @param {"sessions"|"pipelines"|"team"|"history"} viewName
 * @param {object} options
 */
export function switchView(viewName, options = {}) {
  const validViews = ['sessions', 'pipelines', 'team', 'history'];
  const targetView = validViews.includes(viewName) ? viewName : 'sessions';
  store.activeView = targetView;

  // Toggle top navigation tab buttons
  const tabMap = {
    sessions: document.getElementById('nav-tab-sessions'),
    pipelines: document.getElementById('nav-tab-pipelines'),
    team: document.getElementById('nav-tab-team'),
    history: document.getElementById('nav-tab-history'),
  };
  for (const [key, btn] of Object.entries(tabMap)) {
    if (btn) {
      btn.classList.toggle('active', key === targetView);
    }
  }

  // Toggle main view containers
  const viewPanels = {
    sessions: document.getElementById('view-sessions'),
    pipelines: document.getElementById('view-pipelines'),
    team: document.getElementById('view-team'),
    history: document.getElementById('view-history'),
  };
  for (const [key, panel] of Object.entries(viewPanels)) {
    if (panel) {
      panel.classList.toggle('hidden', key !== targetView);
    }
  }

  if (options.pipelineId !== undefined) {
    store.activePipelineId = options.pipelineId;
  }

  if (options.teamRunId !== undefined) {
    store.activeTeamRunId = options.teamRunId;
  }

  if (options.runId !== undefined) {
    store.activeRunId = options.runId;
    if (options.runId) {
      api.getPipelineRun(options.runId)
        .then((detail) => {
          if (store.activeRunId === options.runId) {
            store.activeRunDetail = detail;
            renderActiveRunPanel();
            if (store.activeView === 'history') {
              renderHistoryView();
            }
          }
        })
        .catch(() => {});
    }
  }

  if (targetView === 'sessions') {
    if (options.sessionId !== undefined) {
      loadAndSelectSession(options.sessionId);
    } else {
      renderSessionsSidebar();
      renderSessionChatView();
    }
  } else if (targetView === 'pipelines') {
    renderPipelineBuilder();
    renderActiveRunPanel();
  } else if (targetView === 'team') {
    renderTeamView();
  } else if (targetView === 'history') {
    refreshRuns();
    renderHistoryView();
  }
}

/**
 * Re-fetches all sessions from the API and triggers UI updates.
 * @returns {Promise<Array>}
 */
export async function refreshSessions() {
  try {
    const res = await api.listSessions();
    store.sessions = (res && Array.isArray(res.sessions)) ? res.sessions : [];

    if (store.activeSessionId) {
      const exists = store.sessions.some((s) => s.id === store.activeSessionId);
      if (!exists) {
        store.activeSessionId = store.sessions.length > 0 ? store.sessions[0].id : null;
        store.activeSessionTurns = [];
      }
    }

    renderSessionsSidebar();
    if (store.activeView === 'sessions') {
      renderSessionChatView();
    } else if (store.activeView === 'pipelines') {
      renderPipelineBuilder();
    }
    return store.sessions;
  } catch (err) {
    showToast(`Failed to refresh sessions: ${err.message || err}`, 'error');
    return store.sessions;
  }
}

/**
 * Re-fetches all pipelines from the API and triggers Pipeline Builder re-render.
 * @returns {Promise<Array>}
 */
export async function refreshPipelines() {
  try {
    const res = await api.listPipelines();
    store.pipelines = (res && Array.isArray(res.pipelines)) ? res.pipelines : [];
    renderPipelineBuilder();
    return store.pipelines;
  } catch (err) {
    showToast(`Failed to refresh pipelines: ${err.message || err}`, 'error');
    return store.pipelines;
  }
}

/**
 * Re-fetches pipeline runs and session turns from the API and updates Run views.
 * @returns {Promise<Array>}
 */
export async function refreshRuns() {
  try {
    const [runsRes, turnsRes] = await Promise.allSettled([
      api.listPipelineRuns(),
      api.listTurns(),
    ]);

    if (runsRes.status === 'fulfilled' && runsRes.value && Array.isArray(runsRes.value.runs)) {
      store.pipelineRuns = runsRes.value.runs;
    }
    if (turnsRes.status === 'fulfilled' && turnsRes.value && Array.isArray(turnsRes.value.turns)) {
      store.allTurns = turnsRes.value.turns;
    }

    if (store.activeRunId) {
      try {
        store.activeRunDetail = await api.getPipelineRun(store.activeRunId);
      } catch (_) {
        // Ignore if run was removed
      }
    } else if (store.pipelineRuns.length > 0 && !store.activeRunId) {
      store.activeRunId = store.pipelineRuns[0].id;
      try {
        store.activeRunDetail = await api.getPipelineRun(store.activeRunId);
      } catch (_) {
        // Ignore
      }
    }

    renderActiveRunPanel();
    if (store.activeView === 'history') {
      renderHistoryView();
    }
    return store.pipelineRuns;
  } catch (err) {
    showToast(`Failed to refresh runs: ${err.message || err}`, 'error');
    return store.pipelineRuns;
  }
}

/**
 * Re-fetches all team presets from the API.
 * @returns {Promise<Array>}
 */
export async function refreshTeams() {
  try {
    const res = await api.listTeams();
    store.teams = (res && Array.isArray(res.teams)) ? res.teams : [];
    if (!store.activeTeamId && store.teams.length > 0) {
      const def = store.teams.find((t) => t.is_default) || store.teams[0];
      store.activeTeamId = def.id;
    }
    if (store.activeView === 'team') {
      renderTeamView();
    }
    return store.teams;
  } catch (err) {
    showToast(`Failed to refresh teams: ${err.message || err}`, 'error');
    return store.teams;
  }
}

/**
 * Re-fetches all team runs from the API and updates the inbox badge.
 * @returns {Promise<Array>}
 */
export async function refreshTeamRuns() {
  try {
    const res = await api.listTeamRuns();
    store.teamRuns = (res && Array.isArray(res.runs)) ? res.runs : [];
    updateTeamInboxBadge();
    if (store.activeView === 'team') {
      renderTeamView();
    }
    return store.teamRuns;
  } catch (err) {
    showToast(`Failed to refresh team runs: ${err.message || err}`, 'error');
    return store.teamRuns;
  }
}

/**
 * Updates the #sse-status-indicator badge in the top navigation bar.
 * @param {boolean} connected
 */
function updateSseIndicator(connected) {
  store.sseConnected = connected;
  const indicator = document.getElementById('sse-status-indicator');
  if (!indicator) return;

  const dot = indicator.querySelector('.status-dot');
  const text = indicator.querySelector('.sse-status-text');

  if (connected) {
    indicator.classList.add('connected');
    indicator.classList.remove('reconnecting');
    if (dot) {
      dot.className = 'status-dot completed';
    }
    if (text) {
      text.textContent = 'SSE Connected';
    }
  } else {
    indicator.classList.add('reconnecting');
    indicator.classList.remove('connected');
    if (dot) {
      dot.className = 'status-dot waiting_approval';
    }
    if (text) {
      text.textContent = 'Reconnecting...';
    }
  }
}

/**
 * Connects to /api/events via browser EventSource and routes live SSE events.
 */
function connectEventStream() {
  if (eventSourceInstance) {
    try {
      eventSourceInstance.close();
    } catch (_) {
      // Ignore close errors
    }
  }

  const es = new EventSource('/api/events');
  eventSourceInstance = es;

  es.onopen = () => {
    updateSseIndicator(true);
  };

  es.onerror = () => {
    updateSseIndicator(false);
  };

  const sseEventNames = [
    'session_updated',
    'turn_started',
    'turn_event',
    'turn_completed',
    'pipeline_run_updated',
    'pipeline_stage_updated',
    'pipeline_waiting_approval',
    'team_run_updated',
    'team_message_created',
    'team_inbox_updated',
    'ping',
  ];

  for (const eventName of sseEventNames) {
    es.addEventListener(eventName, (evt) => {
      if (!store.sseConnected) {
        updateSseIndicator(true);
      }
      if (eventName === 'ping') {
        return;
      }

      let payload = {};
      if (evt.data) {
        try {
          payload = JSON.parse(evt.data);
        } catch (_) {
          return;
        }
      }

      // Cache live turn events globally so Session Chat, Active Pipeline Run, and Team views can read them
      if (eventName === 'turn_started' && payload.turn && payload.turn.id) {
        if (!store.liveTurnEvents[payload.turn.id]) {
          store.liveTurnEvents[payload.turn.id] = [];
        }
      } else if (eventName === 'turn_event' && payload.turn_id && payload.event) {
        if (!store.liveTurnEvents[payload.turn_id]) {
          store.liveTurnEvents[payload.turn_id] = [];
        }
        const list = store.liveTurnEvents[payload.turn_id];
        const alreadyExists = list.some((e) => e.seq === payload.event.seq);
        if (!alreadyExists) {
          list.push(payload.event);
        }
      }

      // Route to view handlers
      try {
        handleSessionSseEvent(eventName, payload);
      } catch (err) {
        console.error('Error in handleSessionSseEvent:', err);
      }

      try {
        handlePipelineSseEvent(eventName, payload);
      } catch (err) {
        console.error('Error in handlePipelineSseEvent:', err);
      }

      try {
        handleRunSseEvent(eventName, payload);
      } catch (err) {
        console.error('Error in handleRunSseEvent:', err);
      }

      try {
        handleTeamSseEvent(eventName, payload);
      } catch (err) {
        console.error('Error in handleTeamSseEvent:', err);
      }
    });
  }
}

/**
 * Binds top navigation bar buttons (#nav-tab-sessions, #nav-tab-pipelines, #nav-tab-team, #nav-tab-history).
 */
function bindTopNavigation() {
  const btnSessions = document.getElementById('nav-tab-sessions');
  const btnPipelines = document.getElementById('nav-tab-pipelines');
  const btnTeam = document.getElementById('nav-tab-team');
  const btnHistory = document.getElementById('nav-tab-history');

  if (btnSessions) {
    btnSessions.addEventListener('click', () => switchView('sessions'));
  }
  if (btnPipelines) {
    btnPipelines.addEventListener('click', () => switchView('pipelines'));
  }
  if (btnTeam) {
    btnTeam.addEventListener('click', () => switchView('team'));
  }
  if (btnHistory) {
    btnHistory.addEventListener('click', () => switchView('history'));
  }
}

/**
 * Initializes the application on DOMContentLoaded.
 */
async function bootstrapApp() {
  bindTopNavigation();

  const callbacks = {
    switchView,
    refreshSessions,
    refreshPipelines,
    refreshRuns,
    refreshTeams,
    refreshTeamRuns,
    selectSession: loadAndSelectSession,
    openNewSessionModal,
    renderSessionsSidebar,
    renderSessionChatView,
    renderPipelineBuilder,
    renderActiveRunPanel,
    renderHistoryView,
    renderTeamView,
  };

  initSessionsView(store, callbacks);
  initPipelinesView(store, callbacks);
  initRunsView(store, callbacks);
  initTeamView(store, callbacks);

  connectEventStream();

  // Fetch initial metadata, sessions, pipelines, pipeline runs, teams, and team runs in parallel
  const [metaRes, sessionsRes, pipelinesRes, runsRes, teamsRes, teamRunsRes] = await Promise.allSettled([
    api.getMeta(),
    api.listSessions(),
    api.listPipelines(),
    api.listPipelineRuns(),
    api.listTeams(),
    api.listTeamRuns(),
  ]);

  if (metaRes.status === 'fulfilled' && metaRes.value) {
    store.meta = {
      ...store.meta,
      ...metaRes.value,
    };
  }

  if (sessionsRes.status === 'fulfilled' && sessionsRes.value && Array.isArray(sessionsRes.value.sessions)) {
    store.sessions = sessionsRes.value.sessions;
  }

  if (pipelinesRes.status === 'fulfilled' && pipelinesRes.value && Array.isArray(pipelinesRes.value.pipelines)) {
    store.pipelines = pipelinesRes.value.pipelines;
    if (store.pipelines.length > 0 && !store.activePipelineId) {
      store.activePipelineId = store.pipelines[0].id;
    }
  }

  if (runsRes.status === 'fulfilled' && runsRes.value && Array.isArray(runsRes.value.runs)) {
    store.pipelineRuns = runsRes.value.runs;
    if (store.pipelineRuns.length > 0 && !store.activeRunId) {
      store.activeRunId = store.pipelineRuns[0].id;
      api.getPipelineRun(store.activeRunId)
        .then((detail) => {
          store.activeRunDetail = detail;
          renderActiveRunPanel();
        })
        .catch(() => {});
    }
  }

  if (teamsRes.status === 'fulfilled' && teamsRes.value && Array.isArray(teamsRes.value.teams)) {
    store.teams = teamsRes.value.teams;
    if (store.teams.length > 0 && !store.activeTeamId) {
      const defTeam = store.teams.find((t) => t.is_default) || store.teams[0];
      store.activeTeamId = defTeam.id;
    }
  }

  if (teamRunsRes.status === 'fulfilled' && teamRunsRes.value && Array.isArray(teamRunsRes.value.runs)) {
    store.teamRuns = teamRunsRes.value.runs;
  }

  updateTeamInboxBadge();
  renderSessionsSidebar();
  renderPipelineBuilder();
  renderActiveRunPanel();

  if (store.sessions.length > 0 && !store.activeSessionId) {
    const standalone = store.sessions.find((s) => !s.team_run_id);
    const firstSession = standalone || store.sessions[0];
    await loadAndSelectSession(firstSession.id);
  } else {
    renderSessionChatView();
  }
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', bootstrapApp);
} else {
  bootstrapApp();
}

export default {
  store,
  switchView,
  refreshSessions,
  refreshPipelines,
  refreshRuns,
  refreshTeams,
  refreshTeamRuns,
};
