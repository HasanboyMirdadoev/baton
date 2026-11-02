/**
 * Baton — Team Mode UI (docs/TEAM_MODE.md §8)
 *
 * - Launch panel: team preset, target repo, task prompt, auto-approve-mockup, Start
 * - Team runs list (left column) + top-bar inbox badge
 * - Run board: header/counters, stage stepper, user inbox (PM questions, mockup approval,
 *   limit/restart resume, final sign-off with diff), 7-agent roster, inter-agent message log,
 *   live stream inspector
 * - Team setup modal (#team-setup-modal): name, limits, 7-role provider/model/permission mapping
 *
 * All DOM ids used here are defined in frontend/index.html; payload shapes follow
 * backend/main.py + backend/team_engine.py.
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
let teamRunTimerInterval = null;

let streamRoleTab = null; // role picked by the user in the live-stream tabs
let mockupViewportWidth = '100%';
const cachedRunDiffs = {}; // { [runId]: diff payload }
const diffLoading = new Set();
const openMessageIds = new Set();
const completedTurnIds = new Set();
let lastInboxKey = null;
let launchAutoApproveSynced = false;
let renderFrame = null;
const pendingRenderParts = new Set();

const LAST_REPO_KEY = 'baton.team.lastTargetRepo';

export const TEAM_ROLE_ORDER = [
  'pm',
  'ui_designer',
  'architect',
  'planner',
  'implementer',
  'ui_checker',
  'reviewer',
];

const ROLE_LABELS = {
  pm: 'PM',
  ui_designer: 'UI Designer',
  architect: 'Code Architect',
  planner: 'Planner',
  implementer: 'Implementer',
  ui_checker: 'UI Checker',
  reviewer: 'Code Reviewer',
  user: 'User',
};

// Fallback only; the backend's /api/meta default_team_members is the source of truth.
export const RECOMMENDED_TEAM_MEMBERS = [
  { role: 'pm', agent_name: 'PM', provider: 'claude', model: 'sonnet', permission_level: 'readonly', custom_prompt: '' },
  { role: 'ui_designer', agent_name: 'UI Designer', provider: 'antigravity', model: 'gemini-4-argon-high', permission_level: 'edit', custom_prompt: '' },
  { role: 'architect', agent_name: 'Code Architect', provider: 'claude', model: 'opus', permission_level: 'readonly', custom_prompt: '' },
  { role: 'planner', agent_name: 'Planner', provider: 'claude', model: 'sonnet', permission_level: 'readonly', custom_prompt: '' },
  { role: 'implementer', agent_name: 'Implementer', provider: 'antigravity', model: 'gemini-4-argon-high', permission_level: 'full', custom_prompt: '' },
  { role: 'ui_checker', agent_name: 'UI Checker', provider: 'antigravity', model: 'gemini-3.8-flash-medium', permission_level: 'full', custom_prompt: '' },
  { role: 'reviewer', agent_name: 'Code Reviewer', provider: 'claude', model: 'sonnet', permission_level: 'readonly', custom_prompt: '' },
];

const PROVIDER_OPTIONS = [
  { id: 'claude', label: 'Claude' },
  { id: 'antigravity', label: 'AGY' },
];

const PERMISSION_OPTIONS = [
  { id: 'readonly', label: 'Read-Only' },
  { id: 'edit', label: 'Edit Files' },
  { id: 'full', label: 'Full Access' },
];

const LIMIT_FIELDS = [
  { id: 'team-limit-rework', key: 'max_rework_loops', label: 'Max Rework Loops', min: 0, max: 10, def: 3 },
  { id: 'team-limit-questions', key: 'max_question_rounds', label: 'Max Question Rounds', min: 1, max: 10, def: 4 },
  { id: 'team-limit-turns', key: 'max_total_turns', label: 'Max Total Turns', min: 5, max: 100, def: 30 },
  { id: 'team-limit-turn-timeout', key: 'turn_timeout_sec', label: 'Turn Timeout', min: 30, max: 7200, def: 600 },
  { id: 'team-limit-run-timeout', key: 'run_timeout_sec', label: 'Run Timeout', min: 60, max: 28800, def: 3600 },
];

const STAGE_LABELS = {
  pm_triage: 'PM Triage',
  waiting_user_answers: 'Waiting on Your Answers',
  ui_design: 'UI Mockup Design',
  pm_mockup_review: 'PM Mockup Review',
  waiting_mockup_approval: 'Waiting on Mockup Approval',
  architecture: 'Code Architecture',
  planning: 'Implementation Plan',
  implementing: 'Implementation',
  pm_answer_relay: 'PM Answering Specialist',
  checking: 'Verification (UI + Code)',
  pm_final_summary: 'PM Final Summary',
  waiting_final_signoff: 'Waiting on Final Sign-off',
  paused_limit: 'Paused at Limit',
  interrupted: 'Interrupted (Server Restarted)',
  completed: 'Completed',
  stopped: 'Stopped',
  failed: 'Failed',
};

const STAGE_ROLES = {
  pm_triage: ['pm'],
  ui_design: ['ui_designer'],
  pm_mockup_review: ['pm'],
  architecture: ['architect'],
  planning: ['planner'],
  implementing: ['implementer'],
  pm_answer_relay: ['pm'],
  checking: ['ui_checker', 'reviewer'],
  pm_final_summary: ['pm'],
};

const STEPS = [
  { id: 'triage', label: '1. PM', states: ['pm_triage', 'waiting_user_answers'] },
  { id: 'mockup', label: '2. Mockup', states: ['ui_design', 'pm_mockup_review', 'waiting_mockup_approval'] },
  { id: 'arch', label: '3. Arch', states: ['architecture'] },
  { id: 'plan', label: '4. Plan', states: ['planning'] },
  { id: 'impl', label: '5. Implement', states: ['implementing', 'pm_answer_relay'] },
  { id: 'check', label: '6. Verify', states: ['checking'] },
  { id: 'signoff', label: '7. Sign-off', states: ['pm_final_summary', 'waiting_final_signoff', 'completed'] },
];

const ACTIVE_STATUSES = ['pending', 'running', 'waiting_user'];

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function roleLabel(role) {
  return ROLE_LABELS[role] || role || 'Unknown';
}

function formatStageLabel(stageState) {
  return STAGE_LABELS[stageState] || stageState || '';
}

function getModelsForProvider(providerId) {
  const providers = (appStore && appStore.meta && appStore.meta.providers) || [];
  const found = providers.find((p) => p.id === providerId);
  if (found && Array.isArray(found.models) && found.models.length > 0) {
    return found.models;
  }
  const fallback = RECOMMENDED_TEAM_MEMBERS.filter((m) => m.provider === providerId).map((m) => ({ id: m.model, label: m.model }));
  return fallback.filter((m, i, arr) => arr.findIndex((x) => x.id === m.id) === i);
}

function getPermissionLabel(id) {
  const fromMeta = ((appStore && appStore.meta && appStore.meta.permissions) || []).find((p) => p.id === id);
  const local = PERMISSION_OPTIONS.find((p) => p.id === id);
  return (local && local.label) || (fromMeta && fromMeta.label) || id;
}

function getActiveDetail() {
  if (!appStore || !appStore.activeTeamRunDetail || !appStore.activeTeamRunDetail.run) return null;
  if (appStore.activeTeamRunDetail.run.id !== appStore.activeTeamRunId) return null;
  return appStore.activeTeamRunDetail;
}

function upsertRunInList(run) {
  if (!appStore || !run || !run.id) return;
  const runs = Array.isArray(appStore.teamRuns) ? appStore.teamRuns : [];
  const idx = runs.findIndex((r) => r.id === run.id);
  if (idx >= 0) {
    runs[idx] = run;
    appStore.teamRuns = runs;
  } else {
    appStore.teamRuns = [run, ...runs];
  }
}

/** Returns { [role]: sessionObject } from a run detail. */
function sessionsByRole(detail) {
  const map = {};
  ((detail && detail.sessions) || []).forEach((entry) => {
    if (!entry) return;
    const sess = entry.session || entry;
    const role = entry.role || sess.role;
    if (role) map[role] = { ...sess, id: entry.session_id || sess.id };
  });
  return map;
}

function runTitle(run) {
  const text = (run.task_prompt || '').replace(/\s+/g, ' ').trim();
  return text ? (text.length > 80 ? `${text.slice(0, 77)}...` : text) : 'Team Run';
}

/** The stage a paused/interrupted/stopped run was in, for the stepper and resume label. */
function effectiveStage(run) {
  if (['paused_limit', 'interrupted', 'stopped', 'failed'].includes(run.stage_state) && run.interrupted_stage_state) {
    return run.interrupted_stage_state;
  }
  return run.stage_state;
}

function readLocal(key) {
  try {
    return window.localStorage.getItem(key);
  } catch (_) {
    return null;
  }
}

function writeLocal(key, value) {
  try {
    window.localStorage.setItem(key, value);
  } catch (_) {
    // Storage unavailable; ignore
  }
}

/**
 * Coalesces board re-renders (live token streams can arrive many times per frame).
 * @param {...("header"|"inbox"|"roster"|"messages"|"stream"|"list")} parts
 */
function scheduleRender(...parts) {
  parts.forEach((p) => pendingRenderParts.add(p));
  if (renderFrame !== null) return;
  renderFrame = requestAnimationFrame(() => {
    renderFrame = null;
    const todo = new Set(pendingRenderParts);
    pendingRenderParts.clear();
    if (!appStore || appStore.activeView !== 'team') return;
    if (todo.has('list')) renderTeamRunsList();
    const detail = getActiveDetail();
    if (!detail) return;
    if (todo.has('header')) renderTeamRunHeader(detail.run);
    if (todo.has('inbox')) renderTeamUserInbox(detail);
    if (todo.has('roster')) renderTeamRoster(detail);
    if (todo.has('messages')) renderTeamMessagesLog(detail);
    if (todo.has('stream')) renderTeamLiveStream(detail);
  });
}

// ---------------------------------------------------------------------------
// Inbox badge, subtabs, timer
// ---------------------------------------------------------------------------

/**
 * Updates #team-inbox-badge: number of team runs with status === 'waiting_user'.
 */
export function updateTeamInboxBadge() {
  const badge = document.getElementById('team-inbox-badge');
  if (!badge || !appStore) return;
  const waitingCount = (appStore.teamRuns || []).filter((r) => r.status === 'waiting_user').length;
  badge.textContent = String(waitingCount);
  badge.classList.toggle('hidden', waitingCount === 0);
  badge.title = waitingCount > 0 ? `${waitingCount} team run(s) waiting on you` : '';
}

/**
 * Switches compact subtabs on <= 1024px screens (CSS hides the other panel via data-subtab).
 * @param {"launch"|"board"} tab
 */
export function showTeamSubtab(tab) {
  const view = document.getElementById('view-team');
  const btnLaunch = document.getElementById('team-subtab-launch');
  const btnBoard = document.getElementById('team-subtab-board');
  const target = tab === 'board' ? 'board' : 'launch';
  if (view) view.setAttribute('data-subtab', target);
  if (btnLaunch) btnLaunch.classList.toggle('active', target === 'launch');
  if (btnBoard) btnBoard.classList.toggle('active', target === 'board');
}

function startTeamRunTimer(startedAtIso) {
  stopTeamRunTimer();
  const startMs = new Date(startedAtIso).getTime();
  if (!startedAtIso || Number.isNaN(startMs)) return;
  const tick = () => {
    const el = document.getElementById('team-run-timer');
    if (el) el.textContent = formatDuration(Math.max(0, Date.now() - startMs));
  };
  tick();
  teamRunTimerInterval = setInterval(tick, 1000);
}

function stopTeamRunTimer() {
  if (teamRunTimerInterval) {
    clearInterval(teamRunTimerInterval);
    teamRunTimerInterval = null;
  }
}

// ---------------------------------------------------------------------------
// Launch panel & runs list
// ---------------------------------------------------------------------------

function renderTeamLaunchPanel() {
  if (!appStore) return;
  const teams = appStore.teams || [];
  if ((!appStore.activeTeamId || !teams.some((t) => t.id === appStore.activeTeamId)) && teams.length > 0) {
    const def = teams.find((t) => t.is_default) || teams[0];
    appStore.activeTeamId = def.id;
    launchAutoApproveSynced = false;
  }
  if (!launchAutoApproveSynced && teams.length > 0) {
    // Per-run checkbox starts from the selected team's default (spec §12.4: off by default).
    const team = teams.find((t) => t.id === appStore.activeTeamId);
    const autoCheck = document.getElementById('team-auto-approve-mockup');
    if (autoCheck && team) autoCheck.checked = Boolean(team.auto_approve_mockup);
    launchAutoApproveSynced = true;
  }

  const selectEl = document.getElementById('team-preset-select');
  if (selectEl) {
    selectEl.innerHTML = teams.length === 0
      ? '<option value="">Default Engineering Team</option>'
      : teams
          .map((t) => `<option value="${escapeHtml(t.id)}"${t.id === appStore.activeTeamId ? ' selected' : ''}>${escapeHtml(t.name)}${t.is_default ? ' ★' : ''}</option>`)
          .join('');
  }

  const repoInput = document.getElementById('team-target-repo-input');
  if (repoInput && !repoInput.value) {
    repoInput.value = readLocal(LAST_REPO_KEY) || (appStore.meta && appStore.meta.default_working_dir) || '';
  }
}

/**
 * Renders the team runs list (#team-runs-list) in the left column.
 */
export function renderTeamRunsList() {
  if (!appStore) return;
  const countBadge = document.getElementById('team-runs-count-badge');
  const listEl = document.getElementById('team-runs-list');
  const runs = appStore.teamRuns || [];
  if (countBadge) countBadge.textContent = String(runs.length);
  if (!listEl) return;

  if (runs.length === 0) {
    listEl.innerHTML = `
      <div class="sessions-empty-state" style="padding: 16px;">
        <p>No team runs yet. Enter a task prompt above to launch your first 7-agent team run.</p>
      </div>`;
    return;
  }

  listEl.innerHTML = runs
    .map((run) => {
      const isActive = run.id === appStore.activeTeamRunId;
      const isWaiting = run.status === 'waiting_user';
      const resolution = run.resolution ? ` · ${run.resolution.replace(/_/g, ' ')}` : '';
      return `
        <div class="team-run-card${isActive ? ' active' : ''}${isWaiting ? ' waiting-user' : ''}" role="listitem" tabindex="0" data-team-run-id="${escapeHtml(run.id)}">
          <div class="team-run-card-top">
            <span class="team-run-card-prompt" title="${escapeHtml(run.task_prompt || '')}">${isWaiting ? '📬 ' : ''}${escapeHtml(runTitle(run))}</span>
            ${renderStatusBadge(run.status)}
          </div>
          <div class="team-run-card-meta">
            <span>${escapeHtml(formatStageLabel(run.stage_state))}${escapeHtml(resolution)}</span>
            <span>${Number(run.turn_count || 0)}/${Number(run.max_total_turns || 0)} turns · ${escapeHtml(formatTimestamp(run.started_at))}</span>
          </div>
        </div>`;
    })
    .join('');

  listEl.querySelectorAll('.team-run-card').forEach((item) => {
    const open = () => {
      const runId = item.getAttribute('data-team-run-id');
      if (runId) {
        selectTeamRun(runId);
        showTeamSubtab('board');
      }
    };
    item.addEventListener('click', open);
    item.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        open();
      }
    });
  });
}

/**
 * Selects a team run, fetches GET /api/team-runs/{id} ({run, sessions, messages}) and renders the board.
 * @param {string} runId
 */
export async function selectTeamRun(runId) {
  if (!appStore || !runId) return;
  const switching = appStore.activeTeamRunId !== runId;
  appStore.activeTeamRunId = runId;
  if (switching) {
    streamRoleTab = null;
    openMessageIds.clear();
    lastInboxKey = null;
  }
  renderTeamRunsList();

  try {
    const detail = await api.getTeamRun(runId);
    if (appStore.activeTeamRunId !== runId) return;
    appStore.activeTeamRunDetail = detail;
    if (detail && detail.run) upsertRunInList(detail.run);
    updateTeamInboxBadge();
    renderTeamRunsList();
    renderTeamBoard();
  } catch (err) {
    showToast(`Failed to load team run: ${err.message}`, 'error');
  }
}

// ---------------------------------------------------------------------------
// Board: header & stepper
// ---------------------------------------------------------------------------

function renderStageStepper(run) {
  const stepperEl = document.getElementById('team-stage-stepper');
  if (!stepperEl) return;

  const stage = effectiveStage(run);
  const order = STEPS.flatMap((s) => s.states);
  const currentIdx = order.indexOf(stage);
  const reworkCount = Number(run.rework_loop_count || 0);
  const isWaiting = run.status === 'waiting_user';
  const isHalted = ['paused_limit', 'interrupted', 'stopped', 'failed'].includes(run.stage_state);

  stepperEl.innerHTML = STEPS.map((step, i) => {
    const lastIdx = Math.max(...step.states.map((s) => order.indexOf(s)));
    let cls = '';
    let suffix = '';
    if (run.stage_state === 'completed') {
      cls = 'done';
    } else if (step.states.includes(stage)) {
      cls = isWaiting || isHalted ? 'waiting' : 'active';
    } else if (currentIdx > lastIdx) {
      cls = 'done';
    }
    if (step.id === 'mockup' && run.needs_ui_mockup === false) {
      suffix = ' (skipped)';
    }
    if (step.id === 'impl' && reworkCount > 0) {
      suffix = ` ↺${reworkCount}`;
    }
    const arrow = i < STEPS.length - 1 ? '<span class="team-step-arrow">→</span>' : '';
    return `<span class="team-step-pill ${cls}">${escapeHtml(step.label + suffix)}</span>${arrow}`;
  }).join('');
}

function renderTeamRunHeader(run) {
  const setText = (id, text) => {
    const el = document.getElementById(id);
    if (el) el.textContent = text;
  };

  const titleEl = document.getElementById('team-run-title');
  if (titleEl) {
    titleEl.textContent = runTitle(run);
    titleEl.title = run.task_prompt || '';
  }
  const statusEl = document.getElementById('team-run-status-badge');
  if (statusEl) statusEl.innerHTML = renderStatusBadge(run.status);
  setText('team-run-stage-badge', formatStageLabel(run.stage_state));

  const branchEl = document.getElementById('team-run-branch-badge');
  if (branchEl) {
    branchEl.textContent = run.branch_name ? `🌿 ${run.branch_name}` : '';
    branchEl.title = run.worktree_path || 'Isolated Git Worktree Branch';
    branchEl.classList.toggle('hidden', !run.branch_name);
  }
  const portEl = document.getElementById('team-run-port-badge');
  if (portEl) {
    portEl.textContent = run.allocated_port ? `:${run.allocated_port}` : '';
    portEl.classList.toggle('hidden', !run.allocated_port);
  }
  setText('team-run-rework-badge', `Rework: ${Number(run.rework_loop_count || 0)} / ${Number(run.max_rework_loops || 0)}`);
  setText('team-run-turns-badge', `${Number(run.turn_count || 0)} / ${Number(run.max_total_turns || 0)} turns`);

  const stopBtn = document.getElementById('btn-stop-team-run');
  if (stopBtn) stopBtn.classList.toggle('hidden', !ACTIVE_STATUSES.includes(run.status));

  if (ACTIVE_STATUSES.includes(run.status)) {
    startTeamRunTimer(run.started_at);
  } else {
    stopTeamRunTimer();
    const timerEl = document.getElementById('team-run-timer');
    if (timerEl) {
      if (run.duration_ms != null) {
        timerEl.textContent = formatDuration(Math.max(0, Number(run.duration_ms)));
      } else if (run.started_at) {
        const end = run.finished_at ? new Date(run.finished_at).getTime() : Date.now();
        timerEl.textContent = formatDuration(Math.max(0, end - new Date(run.started_at).getTime()));
      } else {
        timerEl.textContent = '';
      }
    }
  }

  const promptBody = document.getElementById('team-task-prompt-body');
  if (promptBody) {
    const shortCommit = (run.base_commit || '').slice(0, 10);
    const infoRows = [
      ['Team', run.team_name],
      ['Target repo', run.target_repo],
      ['Worktree', run.worktree_path],
      ['Base', run.base_branch ? `${run.base_branch}${shortCommit ? ` @ ${shortCommit}` : ''}` : shortCommit],
      ['Mockup', run.needs_ui_mockup === false ? 'not needed' : run.auto_approve_mockup ? 'auto-approved' : 'user approval'],
    ].filter(([, v]) => v);
    promptBody.innerHTML = `
      <div class="assistant-markdown">${renderMarkdown(run.task_prompt || '')}</div>
      <div class="team-workspace-info">
        ${infoRows.map(([k, v]) => `<div><span class="team-info-key">${escapeHtml(k)}:</span> <code>${escapeHtml(String(v))}</code></div>`).join('')}
      </div>`;
  }

  renderStageStepper(run);
}

// ---------------------------------------------------------------------------
// Board: roster
// ---------------------------------------------------------------------------

function renderTeamRoster(detail) {
  const rosterEl = document.getElementById('team-roster-strip');
  if (!rosterEl || !detail || !detail.run) return;

  const run = detail.run;
  const byRole = sessionsByRole(detail);
  const team = (appStore.teams || []).find((t) => t.id === run.team_id);
  const teamMembers = {};
  ((team && team.members) || []).forEach((m) => {
    teamMembers[m.role] = m;
  });
  const stageRoles = run.status === 'running' ? STAGE_ROLES[run.stage_state] || [] : [];

  rosterEl.innerHTML = TEAM_ROLE_ORDER.map((role) => {
    const sess = byRole[role];
    const member = sess || teamMembers[role] || RECOMMENDED_TEAM_MEMBERS.find((m) => m.role === role) || {};
    const sessStatus = sess ? sess.status || 'idle' : 'idle';
    const isWorking = sessStatus === 'running' || (stageRoles.includes(role) && sessStatus !== 'error');
    const dotStatus = isWorking ? 'running' : sessStatus;
    const turns = sess ? Number(sess.turn_count || 0) : 0;
    const titleBits = [sess ? sess.name : roleLabel(role), getPermissionLabel(member.permission_level)].filter(Boolean);

    return `
      <div class="team-roster-card${isWorking ? ' active-turn' : ''}${sess ? '' : ' disabled'}" data-roster-role="${escapeHtml(role)}" ${sess ? `data-open-session-id="${escapeHtml(sess.id)}" role="button" tabindex="0"` : ''} title="${escapeHtml(titleBits.join(' · '))}">
        <div class="team-roster-top">
          <span class="team-roster-role-wrap">${renderStatusDot(dotStatus)}${renderRoleBadge(role)}</span>
          ${renderProviderBadge(member.provider || 'claude')}
        </div>
        <div class="team-roster-model">${escapeHtml(member.model || '')}</div>
        <div class="team-roster-bottom">
          <span>${turns} ${turns === 1 ? 'turn' : 'turns'} · ${escapeHtml(isWorking ? 'working' : sessStatus)}</span>
          ${sess ? `<button type="button" class="btn-roster-chat" data-open-session-id="${escapeHtml(sess.id)}">Chat ↗</button>` : ''}
        </div>
      </div>`;
  }).join('');

  rosterEl.querySelectorAll('[data-open-session-id]').forEach((el) => {
    const open = (e) => {
      e.stopPropagation();
      const sid = el.getAttribute('data-open-session-id');
      if (sid && appCallbacks.switchView) appCallbacks.switchView('sessions', { sessionId: sid });
    };
    el.addEventListener('click', open);
    if (el.classList.contains('team-roster-card')) {
      el.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') open(e);
      });
    }
  });
}

// ---------------------------------------------------------------------------
// Board: user inbox
// ---------------------------------------------------------------------------

function inboxKeyFor(run) {
  return [run.id, run.status, run.stage_state, run.mockup_version, run.resolution, run.error_message, (run.pending_questions || []).length, run.final_summary ? run.final_summary.length : 0].join('|');
}

function bindAsyncButton(btn, action) {
  if (!btn) return;
  btn.addEventListener('click', async () => {
    const original = btn.textContent;
    btn.disabled = true;
    try {
      await action();
    } catch (err) {
      showToast(err.message || String(err), 'error');
    } finally {
      if (document.body.contains(btn)) {
        btn.disabled = false;
        btn.textContent = original;
      }
    }
  });
}

async function applyDetailResult(runId, detail) {
  if (detail && detail.run) {
    upsertRunInList(detail.run);
    updateTeamInboxBadge();
    if (appStore.activeTeamRunId === runId) {
      appStore.activeTeamRunDetail = detail;
      lastInboxKey = null;
      renderTeamBoard();
    }
    renderTeamRunsList();
  } else {
    await selectTeamRun(runId);
  }
}

/**
 * Renders the User Inbox (#team-user-inbox): PM questions, mockup approval,
 * paused/interrupted resume, final sign-off, and stopped/failed worktree cleanup.
 * Skips re-render when nothing relevant changed so typed answers/feedback are preserved.
 */
function renderTeamUserInbox(detail) {
  const inboxEl = document.getElementById('team-user-inbox');
  if (!inboxEl || !detail || !detail.run) return;
  const run = detail.run;
  const key = inboxKeyFor(run);
  if (key === lastInboxKey) return;
  lastInboxKey = key;

  inboxEl.classList.remove('inbox-done', 'inbox-neutral');
  const stage = run.stage_state;

  if (stage === 'waiting_user_answers') {
    renderQuestionsInbox(inboxEl, run, detail.messages || []);
  } else if (stage === 'waiting_mockup_approval') {
    renderMockupInbox(inboxEl, run, detail.messages || []);
  } else if (stage === 'paused_limit' || stage === 'interrupted') {
    renderPausedInbox(inboxEl, run, detail.messages || []);
  } else if (stage === 'waiting_final_signoff') {
    renderSignoffInbox(inboxEl, run, 'signoff');
  } else if ((run.status === 'stopped' || run.status === 'failed') && run.worktree_path && !run.resolution) {
    renderSignoffInbox(inboxEl, run, 'cleanup');
  } else if (run.status === 'completed' || run.resolution || run.status === 'failed') {
    renderOutcomeInbox(inboxEl, run);
  } else {
    inboxEl.classList.add('hidden');
    inboxEl.innerHTML = '';
  }
}

function latestMessage(messages, predicate) {
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    if (predicate(messages[i])) return messages[i];
  }
  return null;
}

function renderQuestionsInbox(inboxEl, run, messages) {
  const questions = (Array.isArray(run.pending_questions) ? run.pending_questions : []).map((q) => String(q));
  const asking = run.asking_role && run.asking_role !== 'pm' ? run.asking_role : null;
  const pmMsg = latestMessage(messages, (m) => m.from_role === 'pm' && (m.kind === 'question' || m.to_role === 'user'));
  const context = pmMsg ? pmMsg.content_text || pmMsg.summary || '' : '';
  const round = Number(run.question_round_count || 0);

  const fields = questions.length > 0
    ? questions.map((q, idx) => `
        <div class="team-question-item">
          <label class="team-question-label" for="team-question-${idx}">Q${idx + 1}. ${escapeHtml(q)}</label>
          <textarea id="team-question-${idx}" class="team-question-input" rows="2" placeholder="Your answer..."></textarea>
        </div>`).join('')
    : `
        <div class="team-question-item">
          <label class="team-question-label" for="team-question-0">The PM is waiting for your input.</label>
          <textarea id="team-question-0" class="team-question-input" rows="3" placeholder="Your answer..."></textarea>
        </div>`;

  inboxEl.innerHTML = `
    <div class="team-inbox-header">
      <span class="team-inbox-title">💬 PM has ${questions.length > 1 ? `${questions.length} questions` : 'a question'} for you${asking ? ` (from ${escapeHtml(roleLabel(asking))})` : ''}</span>
      <span class="badge-mono">Round ${round} / ${Number(run.max_question_rounds || 0)}</span>
    </div>
    ${context ? `<details class="thinking-block"><summary>PM context</summary><div class="assistant-markdown">${renderMarkdown(context)}</div></details>` : ''}
    ${fields}
    <div class="team-inbox-actions">
      <button type="button" id="btn-send-team-answers" class="btn-primary">Send Answers to PM</button>
    </div>`;
  inboxEl.classList.remove('hidden');

  bindAsyncButton(document.getElementById('btn-send-team-answers'), async () => {
    const values = Array.from(inboxEl.querySelectorAll('.team-question-input')).map((el) => el.value.trim());
    if (!values.some(Boolean)) {
      showToast('Please answer at least one question.', 'error');
      return;
    }
    // Keep answers positional: blank ones tell the PM to use its judgment.
    const answers = values.map((v) => v || '(No preference — use your best judgment.)');
    const detail = await api.answerTeamQuestions(run.id, { answers });
    showToast('Answers sent to PM', 'success');
    await applyDetailResult(run.id, detail);
  });
}

function openMockupInNewTab(url) {
  // Open an isolated wrapper page that frames the mockup with sandbox="allow-scripts"
  // (no allow-same-origin), so the mockup never runs with Baton's origin.
  const win = window.open('', '_blank');
  if (!win) {
    showToast('Popup blocked — allow popups to open the preview in a new tab.', 'error');
    return;
  }
  const absUrl = new URL(url, window.location.href).href;
  win.document.open();
  win.document.write(`<!doctype html><html><head><meta charset="utf-8"><title>Mockup Preview</title>
<style>html,body{margin:0;height:100%;background:#fff}iframe{border:0;width:100%;height:100%;display:block}</style></head>
<body><iframe sandbox="allow-scripts" referrerpolicy="no-referrer" src="${escapeHtml(absUrl)}" title="UI Mockup Preview"></iframe></body></html>`);
  win.document.close();
  try {
    win.opener = null;
  } catch (_) {
    // ignore
  }
}

function renderMockupInbox(inboxEl, run, messages) {
  const v = Math.max(1, Number(run.mockup_version || 1));
  const mockupUrl = `/api/team-runs/${encodeURIComponent(run.id)}/mockup?v=${encodeURIComponent(v)}`;
  const pmMsg = latestMessage(messages, (m) => m.from_role === 'pm' && m.stage_state === 'pm_mockup_review');
  const designerMsg = latestMessage(messages, (m) => m.from_role === 'ui_designer');
  const notes = (pmMsg && pmMsg.summary) || (designerMsg && designerMsg.summary) || '';
  const vpButtons = [
    ['100%', 'Desktop 100%'],
    ['768px', 'Tablet 768px'],
    ['390px', 'Mobile 390px'],
  ];

  inboxEl.innerHTML = `
    <div class="team-inbox-header">
      <span class="team-inbox-title">🎨 UI Mockup Ready for Review (v${v})</span>
      ${renderStatusBadge('waiting_user')}
    </div>
    ${notes ? `<div class="form-hint">${escapeHtml(notes)}</div>` : ''}
    <div class="team-mockup-toolbar">
      <div class="team-viewport-toggles">
        ${vpButtons.map(([w, label]) => `<button type="button" class="pipe-subtab-btn team-vp-btn${mockupViewportWidth === w ? ' active' : ''}" data-vp="${w}">${label}</button>`).join('')}
      </div>
      <button type="button" class="btn-secondary" id="btn-open-mockup-tab">Open Preview in New Tab ↗</button>
    </div>
    <div class="team-mockup-frame-wrap">
      <iframe
        id="team-mockup-preview-iframe"
        class="team-mockup-iframe"
        style="width: ${escapeHtml(mockupViewportWidth)};"
        src="${escapeHtml(mockupUrl)}"
        sandbox="allow-scripts"
        referrerpolicy="no-referrer"
        title="UI Mockup Preview"
      ></iframe>
    </div>
    <div class="form-group">
      <label class="form-label" for="team-mockup-feedback">Feedback (required when requesting changes)</label>
      <textarea id="team-mockup-feedback" class="input-text" rows="3" placeholder="Optional notes, or describe changes needed if requesting revisions..."></textarea>
    </div>
    <div class="team-inbox-actions">
      <button type="button" id="btn-approve-mockup" class="btn-primary">✓ Approve Mockup</button>
      <button type="button" id="btn-reject-mockup" class="btn-secondary">↺ Request Changes</button>
    </div>`;
  inboxEl.classList.remove('hidden');

  inboxEl.querySelectorAll('.team-vp-btn').forEach((btn) => {
    btn.addEventListener('click', () => {
      mockupViewportWidth = btn.getAttribute('data-vp') || '100%';
      const iframe = document.getElementById('team-mockup-preview-iframe');
      if (iframe) iframe.style.width = mockupViewportWidth;
      inboxEl.querySelectorAll('.team-vp-btn').forEach((b) => b.classList.toggle('active', b === btn));
    });
  });

  const openTabBtn = document.getElementById('btn-open-mockup-tab');
  if (openTabBtn) openTabBtn.addEventListener('click', () => openMockupInNewTab(mockupUrl));

  const feedbackEl = document.getElementById('team-mockup-feedback');
  bindAsyncButton(document.getElementById('btn-approve-mockup'), async () => {
    const feedback = feedbackEl ? feedbackEl.value.trim() : '';
    const detail = await api.reviewTeamMockup(run.id, { approved: true, feedback });
    showToast('Mockup approved — the team continues with architecture', 'success');
    await applyDetailResult(run.id, detail);
  });
  bindAsyncButton(document.getElementById('btn-reject-mockup'), async () => {
    const feedback = feedbackEl ? feedbackEl.value.trim() : '';
    if (!feedback) {
      showToast('Describe the changes you want before requesting a revision.', 'error');
      if (feedbackEl) feedbackEl.focus();
      return;
    }
    const detail = await api.reviewTeamMockup(run.id, { approved: false, feedback });
    showToast('Revision requested from the UI Designer', 'info');
    await applyDetailResult(run.id, detail);
  });
}

function renderPausedInbox(inboxEl, run, messages) {
  const isInterrupted = run.stage_state === 'interrupted';
  const resumeStage = run.interrupted_stage_state ? formatStageLabel(run.interrupted_stage_state) : '';
  const reason = run.error_message || (isInterrupted
    ? 'The server restarted while this run was active.'
    : 'The run paused after reaching a safety limit.');
  const pmMsg = latestMessage(messages, (m) => m.from_role === 'pm');
  const findings = Array.isArray(run.last_findings) ? run.last_findings : [];

  inboxEl.innerHTML = `
    <div class="team-inbox-header">
      <span class="team-inbox-title">⏸ ${isInterrupted ? 'Run Interrupted' : 'Limit Reached'}${resumeStage ? ` during ${escapeHtml(resumeStage)}` : ''}</span>
      <span class="badge-mono">Turns ${Number(run.turn_count || 0)}/${Number(run.max_total_turns || 0)} · Rework ${Number(run.rework_loop_count || 0)}/${Number(run.max_rework_loops || 0)} · Q&amp;A ${Number(run.question_round_count || 0)}/${Number(run.max_question_rounds || 0)}</span>
    </div>
    <div class="assistant-markdown">${renderMarkdown(reason)}</div>
    ${pmMsg && pmMsg.summary ? `<div class="form-hint">Last PM status: ${escapeHtml(pmMsg.summary)}</div>` : ''}
    ${findings.length > 0 ? `<div class="team-msg-pills">${findings.map((f) => `<span class="team-pill team-pill-finding">${escapeHtml(String(f))}</span>`).join('')}</div>` : ''}
    <div class="form-group">
      <label class="form-label" for="team-resume-note">Optional guidance for the team on resume</label>
      <input type="text" id="team-resume-note" class="input-text" placeholder="e.g. Focus on the failing test first">
    </div>
    <div class="team-inbox-actions">
      <button type="button" id="btn-resume-team-run" class="btn-primary">▶ Continue Run (+2 Rework Loops, +15 Turns)</button>
      <button type="button" id="btn-force-signoff-run" class="btn-secondary">⏭ Proceed to Final Sign-off Anyway</button>
      <button type="button" id="btn-inbox-stop-run" class="btn-stop-pipeline">■ Stop Run</button>
    </div>`;
  inboxEl.classList.remove('hidden');

  const noteValue = () => {
    const el = document.getElementById('team-resume-note');
    return el ? el.value.trim() : '';
  };
  bindAsyncButton(document.getElementById('btn-resume-team-run'), async () => {
    const detail = await api.resumeTeamRun(run.id, {
      add_rework_loops: 2,
      add_question_rounds: 2,
      add_turns: 15,
      add_timeout_sec: 1800,
      force_signoff: false,
      note: noteValue(),
    });
    showToast('Team run resumed', 'success');
    await applyDetailResult(run.id, detail);
  });
  bindAsyncButton(document.getElementById('btn-force-signoff-run'), async () => {
    const detail = await api.resumeTeamRun(run.id, {
      add_rework_loops: 0,
      add_question_rounds: 0,
      add_turns: 2,
      add_timeout_sec: 600,
      force_signoff: true,
      note: noteValue(),
    });
    showToast('Moving to final sign-off', 'info');
    await applyDetailResult(run.id, detail);
  });
  bindAsyncButton(document.getElementById('btn-inbox-stop-run'), async () => {
    if (!window.confirm('Stop this team run? The worktree is kept so you can still keep or discard the branch.')) return;
    const detail = await api.stopTeamRun(run.id);
    showToast('Team run stopped', 'info');
    await applyDetailResult(run.id, detail);
  });
}

function verdictBadge(label, verdict) {
  const v = verdict ? String(verdict).toUpperCase() : 'NOT RUN';
  const cls = v === 'APPROVED' ? 'status-completed' : v === 'NOT RUN' ? 'status-idle' : 'status-changes_requested';
  return `<span class="status-badge ${cls}"><span class="status-text">${escapeHtml(label)}: ${escapeHtml(v.replace(/_/g, ' '))}</span></span>`;
}

/**
 * Final sign-off (mode 'signoff') or stopped/failed worktree cleanup (mode 'cleanup').
 */
function renderSignoffInbox(inboxEl, run, mode) {
  const isSignoff = mode === 'signoff';
  const baseBranch = run.base_branch || 'base branch';
  const branch = run.branch_name || `baton/${(run.id || '').slice(0, 8)}`;
  const defaultCommitMsg = `Baton team run: ${(run.task_prompt || '').replace(/\s+/g, ' ').trim().slice(0, 60)}`;
  const uiVerdict = run.needs_ui_mockup === false && !run.ui_check_verdict ? 'SKIPPED' : run.ui_check_verdict;

  inboxEl.innerHTML = `
    <div class="team-inbox-header">
      <span class="team-inbox-title">${isSignoff ? '🏁 Final Sign-off — Review the Changes' : `⚠ Run ${escapeHtml(run.status)} — Keep or Discard the Worktree`}</span>
      <div class="team-verification-badges">
        ${verdictBadge('UI Checker', uiVerdict)}
        ${verdictBadge('Code Reviewer', run.code_review_verdict)}
      </div>
    </div>
    ${run.final_summary ? `<div class="assistant-markdown">${renderMarkdown(run.final_summary)}</div>` : ''}
    ${!isSignoff && run.error_message ? `<div class="assistant-markdown">${renderMarkdown(run.error_message)}</div>` : ''}
    <div id="team-diff-container">${cachedRunDiffs[run.id] ? renderDiffViewerHtml(cachedRunDiffs[run.id]) : '<div class="form-hint">Loading workspace diff...</div>'}</div>
    ${isSignoff ? `
    <div class="form-group">
      <label class="form-label" for="team-signoff-commit-msg">Commit message</label>
      <input type="text" id="team-signoff-commit-msg" class="input-text" value="${escapeHtml(defaultCommitMsg)}">
    </div>` : ''}
    <div class="team-inbox-actions">
      ${isSignoff ? `<button type="button" class="btn-primary team-signoff-btn" data-signoff-action="merge">✓ Merge into ${escapeHtml(baseBranch)}</button>` : ''}
      <button type="button" class="btn-secondary team-signoff-btn" data-signoff-action="keep_branch">🌿 Keep Branch (${escapeHtml(branch)})</button>
      <button type="button" class="btn-stop-pipeline team-signoff-btn" data-signoff-action="discard">🗑 Discard Changes</button>
    </div>`;
  inboxEl.classList.remove('hidden');

  loadAndRenderRunDiff(run.id, true);

  inboxEl.querySelectorAll('.team-signoff-btn').forEach((btn) => {
    btn.addEventListener('click', async () => {
      const action = btn.getAttribute('data-signoff-action');
      if (!action) return;
      if (action === 'discard' && !window.confirm(`Discard all changes on "${branch}" and remove its worktree? This cannot be undone.`)) {
        return;
      }
      if (action === 'merge' && !window.confirm(`Merge "${branch}" into ${baseBranch} in ${run.target_repo}?`)) {
        return;
      }
      const commitInput = document.getElementById('team-signoff-commit-msg');
      const payload = { action };
      if (commitInput && commitInput.value.trim()) payload.commit_message = commitInput.value.trim();

      const buttons = inboxEl.querySelectorAll('.team-signoff-btn');
      buttons.forEach((b) => { b.disabled = true; });
      try {
        const detail = await api.signoffTeamRun(run.id, payload);
        delete cachedRunDiffs[run.id];
        showToast(action === 'merge' ? `Merged into ${baseBranch}` : action === 'keep_branch' ? `Branch ${branch} kept` : 'Changes discarded', 'success');
        await applyDetailResult(run.id, detail);
      } catch (err) {
        showToast(`Sign-off failed: ${err.message}`, 'error');
        buttons.forEach((b) => { b.disabled = false; });
      }
    });
  });
}

function renderOutcomeInbox(inboxEl, run) {
  const resolutionText = {
    merged: `Merged into ${run.base_branch || 'the base branch'}`,
    kept_branch: `Branch ${run.branch_name || ''} kept`,
    discarded: 'Changes discarded',
  }[run.resolution] || (run.resolution ? run.resolution.replace(/_/g, ' ') : '');
  const title = run.status === 'completed'
    ? `✓ Team run completed${resolutionText ? ` — ${resolutionText}` : ''}`
    : `${run.status === 'failed' ? '✗ Team run failed' : '⏹ Team run stopped'}${resolutionText ? ` — ${resolutionText}` : ''}`;

  inboxEl.classList.add(run.status === 'completed' ? 'inbox-done' : 'inbox-neutral');
  inboxEl.innerHTML = `
    <div class="team-inbox-header">
      <span class="team-inbox-title">${escapeHtml(title)}</span>
      ${renderStatusBadge(run.status)}
    </div>
    ${run.final_summary ? `<details class="thinking-block"${run.status === 'completed' ? ' open' : ''}><summary>PM final summary</summary><div class="assistant-markdown">${renderMarkdown(run.final_summary)}</div></details>` : ''}
    ${run.error_message && run.status !== 'completed' ? `<div class="assistant-markdown">${renderMarkdown(run.error_message)}</div>` : ''}`;
  inboxEl.classList.remove('hidden');
}

function renderDiffViewerHtml(diffData) {
  const files = Array.isArray(diffData.files) ? diffData.files : [];
  const rawDiff = diffData.diff_text || '';
  const filesHtml = files.length === 0
    ? '<span class="form-hint">No changed files in the worktree.</span>'
    : files.map((f) => `
        <span class="team-diff-file-pill" title="${escapeHtml(f.status || '')}">
          <span>${escapeHtml(f.status ? `${f.status} ` : '')}${escapeHtml(f.path || '')}</span>
          <span class="diff-add-badge">+${Number(f.additions || 0)}</span>
          <span class="diff-del-badge">-${Number(f.deletions || 0)}</span>
        </span>`).join('');

  const linesHtml = rawDiff
    ? rawDiff.split('\n').map((line) => {
        let cls = '';
        if (line.startsWith('diff --git') || line.startsWith('+++') || line.startsWith('---') || line.startsWith('@@') || line.startsWith('index ')) {
          cls = 'diff-line-hunk';
        } else if (line.startsWith('+')) {
          cls = 'diff-line-add';
        } else if (line.startsWith('-')) {
          cls = 'diff-line-del';
        }
        return cls ? `<span class="${cls}">${escapeHtml(line)}</span>` : `${escapeHtml(line)}\n`;
      }).join('')
    : 'No diff lines.';

  return `
    <div class="team-diff-viewer">
      <div class="team-diff-files-list">${filesHtml}</div>
      <pre class="team-diff-pre">${linesHtml}</pre>
    </div>`;
}

async function loadAndRenderRunDiff(runId, refresh = false) {
  if (!runId || diffLoading.has(runId)) return;
  if (cachedRunDiffs[runId] && !refresh) return;
  diffLoading.add(runId);
  try {
    const diffData = await api.getTeamRunDiff(runId);
    cachedRunDiffs[runId] = diffData;
    const container = document.getElementById('team-diff-container');
    if (container && appStore.activeTeamRunId === runId) container.innerHTML = renderDiffViewerHtml(diffData);
  } catch (err) {
    const container = document.getElementById('team-diff-container');
    if (container && appStore.activeTeamRunId === runId) {
      container.innerHTML = `<div class="form-hint">Could not load the workspace diff: ${escapeHtml(err.message)}</div>`;
    }
  } finally {
    diffLoading.delete(runId);
  }
}

// ---------------------------------------------------------------------------
// Board: inter-agent message log
// ---------------------------------------------------------------------------

function renderTeamMessagesLog(detail) {
  const countEl = document.getElementById('team-messages-count');
  const logEl = document.getElementById('team-messages-log');
  if (!logEl) return;
  const messages = (detail && detail.messages) || [];
  if (countEl) countEl.textContent = `${messages.length} ${messages.length === 1 ? 'message' : 'messages'}`;

  if (messages.length === 0) {
    logEl.innerHTML = '<div class="form-hint">No inter-agent messages yet.</div>';
    return;
  }

  const nearBottom = logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight < 40;

  logEl.innerHTML = messages.map((msg) => {
    const payload = msg.payload && typeof msg.payload === 'object' ? msg.payload : {};
    const questions = Array.isArray(payload.questions) ? payload.questions : [];
    const findings = Array.isArray(payload.findings) ? payload.findings : [];
    const summary = msg.summary || `${roleLabel(msg.from_role)} → ${roleLabel(msg.to_role)}`;
    const body = msg.content_text || '';
    const time = formatTimestamp(msg.created_at);
    const open = openMessageIds.has(msg.id) ? ' open' : '';
    return `
      <details class="team-msg-item" data-msg-id="${escapeHtml(msg.id)}"${open}>
        <summary class="team-msg-summary">
          <div class="team-msg-summary-row">
            <span class="team-msg-route">${renderRoleBadge(msg.from_role)}<span class="team-step-arrow">→</span>${renderRoleBadge(msg.to_role)}<span class="badge-mono">${escapeHtml((msg.kind || '').replace(/_/g, ' '))}</span></span>
            <span class="team-msg-time">${escapeHtml(time)}${body ? ' · ▸' : ''}</span>
          </div>
          <div class="team-msg-headline">${escapeHtml(summary)}</div>
          ${questions.length || findings.length ? `
            <div class="team-msg-pills">
              ${questions.map((q) => `<span class="team-pill team-pill-question">? ${escapeHtml(String(q))}</span>`).join('')}
              ${findings.map((f) => `<span class="team-pill team-pill-finding">! ${escapeHtml(String(f))}</span>`).join('')}
            </div>` : ''}
        </summary>
        ${body ? `<div class="team-msg-body assistant-markdown">${renderMarkdown(body)}</div>` : ''}
      </details>`;
  }).join('');

  logEl.querySelectorAll('details.team-msg-item').forEach((el) => {
    el.addEventListener('toggle', () => {
      const id = el.getAttribute('data-msg-id');
      if (!id) return;
      if (el.open) openMessageIds.add(id);
      else openMessageIds.delete(id);
    });
  });

  if (nearBottom) logEl.scrollTop = logEl.scrollHeight;
}

// ---------------------------------------------------------------------------
// Board: live stream inspector
// ---------------------------------------------------------------------------

function liveRolesFor(run, byRole) {
  const running = TEAM_ROLE_ORDER.filter((r) => byRole[r] && byRole[r].status === 'running');
  const stageRoles = STAGE_ROLES[run.stage_state] || [];
  const roles = [...stageRoles, ...running].filter((r, i, arr) => arr.indexOf(r) === i);
  return roles;
}

function renderTeamLiveStream(detail) {
  const tabsEl = document.getElementById('team-stream-role-tabs');
  const streamEl = document.getElementById('team-live-stream');
  if (!streamEl || !detail || !detail.run) return;

  const run = detail.run;
  const messages = detail.messages || [];
  const byRole = sessionsByRole(detail);
  const liveRoles = run.status === 'running' ? liveRolesFor(run, byRole) : [];
  const turnsByRole = (appStore.teamRunTurns && appStore.teamRunTurns[run.id]) || {};

  // Tabs: the roles working now, plus any role that streamed in this run; the user's pick sticks.
  const tabRoles = [...liveRoles];
  TEAM_ROLE_ORDER.forEach((r) => {
    if (turnsByRole[r] && !tabRoles.includes(r)) tabRoles.push(r);
  });
  if (streamRoleTab && !tabRoles.includes(streamRoleTab)) tabRoles.push(streamRoleTab);

  let targetRole = streamRoleTab;
  if (!targetRole || (!tabRoles.includes(targetRole) && liveRoles.length > 0)) {
    targetRole = liveRoles[0] || (messages.length ? messages[messages.length - 1].from_role : null) || 'pm';
    if (targetRole === 'user') targetRole = 'pm';
  }
  if (!tabRoles.includes(targetRole)) tabRoles.unshift(targetRole);

  if (tabsEl) {
    tabsEl.innerHTML = tabRoles.map((role) => {
      const sess = byRole[role];
      const running = sess && sess.status === 'running';
      const icon = role === 'ui_checker' ? '🌐 ' : role === 'reviewer' ? '🔍 ' : '';
      return `<button type="button" class="pipe-subtab-btn team-stream-tab${role === targetRole ? ' active' : ''}" data-stream-role="${escapeHtml(role)}">${icon}${escapeHtml(roleLabel(role))}${running ? ' (Running)' : ''}</button>`;
    }).join('');
    tabsEl.querySelectorAll('.team-stream-tab').forEach((btn) => {
      btn.addEventListener('click', () => {
        streamRoleTab = btn.getAttribute('data-stream-role');
        renderTeamLiveStream(getActiveDetail() || detail);
      });
    });
  }

  const sess = byRole[targetRole];
  const sessionId = sess ? sess.id : null;
  const liveTurnId = turnsByRole[targetRole];
  const events = liveTurnId && appStore.liveTurnEvents ? appStore.liveTurnEvents[liveTurnId] : null;
  const chatBtn = sessionId ? `<button type="button" class="btn-roster-chat" data-stream-open-session="${escapeHtml(sessionId)}">Open Chat ↗</button>` : '';
  const wasNearBottom = streamEl.scrollHeight - streamEl.scrollTop - streamEl.clientHeight < 40;

  if (Array.isArray(events) && events.length > 0) {
    let thinking = '';
    let text = '';
    const tools = [];
    const toolsById = new Map();
    events.forEach((evt) => {
      if (evt.type === 'thinking_delta' && evt.text) {
        thinking += evt.text;
      } else if (evt.type === 'text_delta' && evt.text) {
        text += evt.text;
      } else if (evt.type === 'tool_use') {
        const item = { ...evt };
        toolsById.set(evt.tool_call_id || `seq-${evt.seq}`, item);
        tools.push(item);
      } else if (evt.type === 'tool_result') {
        const t = toolsById.get(evt.tool_call_id);
        if (t) {
          t.tool_output = evt.tool_output != null ? evt.tool_output : '';
          t.is_error = Boolean(evt.is_error);
        }
      } else if (evt.type === 'turn_result' && evt.text && !text) {
        text = evt.text;
      } else if (evt.type === 'error' && evt.text) {
        text += `\n\n**Error:** ${evt.text}`;
      }
    });
    const done = completedTurnIds.has(liveTurnId) || !(sess && sess.status === 'running');
    streamEl.innerHTML = `
      <div class="team-live-turn-box">
        <div class="team-msg-summary-row">
          <span class="team-msg-route">${renderStatusDot(done ? 'completed' : 'running')}${renderRoleBadge(targetRole)}<span class="form-hint">${done ? 'Last turn' : 'Live turn'}</span></span>
          ${chatBtn}
        </div>
        ${thinking ? `<details class="thinking-block"${text ? '' : ' open'}><summary>💭 Reasoning</summary><div class="thinking-body">${escapeHtml(thinking)}</div></details>` : ''}
        ${tools.length ? `<div class="team-stream-tools">${tools.map((t) => renderToolCard(t)).join('')}</div>` : ''}
        ${text ? `<div class="assistant-markdown">${renderMarkdown(text)}</div>` : done ? '' : '<div class="form-hint">Waiting for output...</div>'}
      </div>`;
  } else {
    const latest = latestMessage(messages, (m) => m.from_role === targetRole);
    if (sess && sess.status === 'running') {
      streamEl.innerHTML = `
        <div class="team-live-turn-box">
          <div class="team-msg-summary-row">
            <span class="team-msg-route">${renderStatusDot('running')}${renderRoleBadge(targetRole)}<span class="form-hint">Working — waiting for stream output...</span></span>
            ${chatBtn}
          </div>
        </div>`;
    } else if (latest && (latest.content_text || latest.summary)) {
      streamEl.innerHTML = `
        <div class="team-live-turn-box">
          <div class="team-msg-summary-row">
            <span class="team-msg-route">${renderRoleBadge(targetRole)}<span class="form-hint">Latest output · ${escapeHtml(formatTimestamp(latest.created_at))}</span></span>
            ${chatBtn}
          </div>
          <div class="assistant-markdown">${renderMarkdown(latest.content_text || latest.summary)}</div>
        </div>`;
    } else {
      streamEl.innerHTML = `<div class="form-hint">No output from ${escapeHtml(roleLabel(targetRole))} yet.</div>${chatBtn ? `<div>${chatBtn}</div>` : ''}`;
    }
  }

  streamEl.querySelectorAll('[data-stream-open-session]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const sid = btn.getAttribute('data-stream-open-session');
      if (sid && appCallbacks.switchView) appCallbacks.switchView('sessions', { sessionId: sid });
    });
  });

  if (wasNearBottom) streamEl.scrollTop = streamEl.scrollHeight;
}

/**
 * Renders the right-hand Team Board for the active team run.
 */
export function renderTeamBoard() {
  const emptyEl = document.getElementById('team-empty-board');
  const activeEl = document.getElementById('team-active-board');
  if (!emptyEl || !activeEl || !appStore) return;

  const detail = getActiveDetail();
  if (!detail) {
    emptyEl.classList.remove('hidden');
    activeEl.classList.add('hidden');
    stopTeamRunTimer();
    return;
  }

  emptyEl.classList.add('hidden');
  activeEl.classList.remove('hidden');
  renderTeamRunHeader(detail.run);
  renderTeamUserInbox(detail);
  renderTeamRoster(detail);
  renderTeamMessagesLog(detail);
  renderTeamLiveStream(detail);
}

// ---------------------------------------------------------------------------
// Team setup modal
// ---------------------------------------------------------------------------

let modalTeamId = null; // null = creating a new team
let modalMembers = [];

function getDefaultMembersClone() {
  const fromMeta = appStore && appStore.meta && Array.isArray(appStore.meta.default_team_members)
    ? appStore.meta.default_team_members
    : [];
  const source = fromMeta.length >= 7 ? fromMeta : RECOMMENDED_TEAM_MEMBERS;
  return TEAM_ROLE_ORDER.map((role) => {
    const m = source.find((x) => x.role === role) || RECOMMENDED_TEAM_MEMBERS.find((x) => x.role === role);
    return {
      role,
      agent_name: m.agent_name || roleLabel(role),
      provider: m.provider || 'claude',
      model: m.model || 'sonnet',
      permission_level: m.permission_level || 'readonly',
      custom_prompt: m.custom_prompt || '',
    };
  });
}

function membersFromTeam(team) {
  const defaults = getDefaultMembersClone();
  const members = (team && Array.isArray(team.members)) ? team.members : [];
  return defaults.map((def) => {
    const m = members.find((x) => x.role === def.role);
    return m
      ? {
          role: def.role,
          agent_name: m.agent_name || def.agent_name,
          provider: m.provider || def.provider,
          model: m.model || def.model,
          permission_level: m.permission_level || def.permission_level,
          custom_prompt: m.custom_prompt || '',
        }
      : def;
  });
}

function setModalError(message) {
  const errEl = document.getElementById('team-setup-error');
  if (!errEl) return;
  errEl.textContent = message || '';
  errEl.classList.toggle('hidden', !message);
}

function renderModalMembers() {
  const listEl = document.getElementById('team-setup-members-list');
  if (!listEl) return;
  const teamRoles = (appStore && appStore.meta && appStore.meta.team_roles) || [];

  listEl.innerHTML = modalMembers.map((m, idx) => {
    const roleMeta = teamRoles.find((r) => r.id === m.role);
    const models = getModelsForProvider(m.provider);
    const modelOptions = models.some((mod) => mod.id === m.model) || !m.model ? models : [...models, { id: m.model, label: `${m.model} (custom)` }];
    return `
      <div class="team-member-row" data-member-idx="${idx}">
        <div class="team-member-head">
          ${renderRoleBadge(m.role)}
          <span class="form-hint">${escapeHtml(roleMeta ? roleMeta.description : '')}</span>
        </div>
        <div class="team-member-controls">
          <input type="text" class="input-text team-member-name" aria-label="${escapeHtml(roleLabel(m.role))} agent name" value="${escapeHtml(m.agent_name)}" maxlength="200">
          <select class="select-input team-member-provider" aria-label="${escapeHtml(roleLabel(m.role))} provider">
            ${PROVIDER_OPTIONS.map((p) => `<option value="${p.id}"${p.id === m.provider ? ' selected' : ''}>${p.label}</option>`).join('')}
          </select>
          <select class="select-input team-member-model" aria-label="${escapeHtml(roleLabel(m.role))} model">
            ${modelOptions.map((mod) => `<option value="${escapeHtml(mod.id)}"${mod.id === m.model ? ' selected' : ''}>${escapeHtml(mod.label || mod.id)}</option>`).join('')}
          </select>
          <select class="select-input team-member-perm" aria-label="${escapeHtml(roleLabel(m.role))} permission">
            ${PERMISSION_OPTIONS.map((p) => `<option value="${p.id}"${p.id === m.permission_level ? ' selected' : ''}>${p.label}</option>`).join('')}
          </select>
        </div>
        <details class="team-prompt-details"${m.custom_prompt ? ' open' : ''}>
          <summary>Custom prompt addendum${m.custom_prompt ? ' ★' : ''}</summary>
          <textarea class="input-text team-member-prompt" rows="2" placeholder="Extra instructions appended to this role's system prompt...">${escapeHtml(m.custom_prompt)}</textarea>
        </details>
      </div>`;
  }).join('');

  listEl.querySelectorAll('.team-member-provider').forEach((sel) => {
    sel.addEventListener('change', () => {
      syncModalMembersFromDom();
      const row = sel.closest('[data-member-idx]');
      const idx = Number(row && row.getAttribute('data-member-idx'));
      if (!modalMembers[idx]) return;
      const models = getModelsForProvider(sel.value);
      modalMembers[idx].provider = sel.value;
      modalMembers[idx].model = models[0] ? models[0].id : '';
      renderModalMembers();
    });
  });
}

function syncModalMembersFromDom() {
  const listEl = document.getElementById('team-setup-members-list');
  if (!listEl) return;
  listEl.querySelectorAll('[data-member-idx]').forEach((row) => {
    const m = modalMembers[Number(row.getAttribute('data-member-idx'))];
    if (!m) return;
    const q = (sel) => row.querySelector(sel);
    if (q('.team-member-name')) m.agent_name = q('.team-member-name').value.trim() || roleLabel(m.role);
    if (q('.team-member-provider')) m.provider = q('.team-member-provider').value;
    if (q('.team-member-model')) m.model = q('.team-member-model').value;
    if (q('.team-member-perm')) m.permission_level = q('.team-member-perm').value;
    if (q('.team-member-prompt')) m.custom_prompt = q('.team-member-prompt').value;
  });
}

function populateTeamSetupForm(team) {
  modalTeamId = team ? team.id : null;
  setModalError('');

  const selectEl = document.getElementById('team-setup-select');
  if (selectEl) {
    const teams = appStore.teams || [];
    selectEl.innerHTML = `<option value=""${modalTeamId ? '' : ' selected'}>— New Team Preset —</option>${teams
      .map((t) => `<option value="${escapeHtml(t.id)}"${t.id === modalTeamId ? ' selected' : ''}>${escapeHtml(t.name)}${t.is_default ? ' ★' : ''}</option>`)
      .join('')}`;
  }

  const nameEl = document.getElementById('team-setup-name');
  const descEl = document.getElementById('team-setup-description');
  const autoEl = document.getElementById('team-setup-auto-approve');
  const delBtn = document.getElementById('btn-delete-team-preset');
  if (nameEl) nameEl.value = team ? team.name || '' : 'New Engineering Team';
  if (descEl) descEl.value = team ? team.description || '' : '';
  if (autoEl) autoEl.checked = team ? Boolean(team.auto_approve_mockup) : false;
  if (delBtn) delBtn.classList.toggle('hidden', !team || (appStore.teams || []).length <= 1);

  LIMIT_FIELDS.forEach((f) => {
    const el = document.getElementById(f.id);
    if (el) el.value = String(team && team[f.key] != null ? team[f.key] : f.def);
  });

  modalMembers = team ? membersFromTeam(team) : getDefaultMembersClone();
  renderModalMembers();
}

/**
 * Opens the Team Setup Modal for the team currently selected in the launch panel.
 */
export function openTeamSetupModal() {
  const modal = document.getElementById('team-setup-modal');
  if (!modal || !appStore) return;
  const teams = appStore.teams || [];
  const team = teams.find((t) => t.id === appStore.activeTeamId) || teams.find((t) => t.is_default) || teams[0] || null;
  populateTeamSetupForm(team);
  modal.classList.remove('hidden');
  const nameEl = document.getElementById('team-setup-name');
  if (nameEl) nameEl.focus();
}

export function closeTeamSetupModal() {
  const modal = document.getElementById('team-setup-modal');
  if (modal) modal.classList.add('hidden');
  setModalError('');
}

async function saveTeamFromModal() {
  syncModalMembersFromDom();
  const nameEl = document.getElementById('team-setup-name');
  const descEl = document.getElementById('team-setup-description');
  const autoEl = document.getElementById('team-setup-auto-approve');
  const saveBtn = document.getElementById('btn-save-team-modal');

  const name = nameEl ? nameEl.value.trim() : '';
  if (!name) {
    setModalError('Team name is required.');
    return;
  }
  const payload = {
    name,
    description: descEl ? descEl.value.trim() : '',
    auto_approve_mockup: Boolean(autoEl && autoEl.checked),
    members: modalMembers.map((m) => ({
      role: m.role,
      agent_name: m.agent_name || roleLabel(m.role),
      provider: m.provider,
      model: m.model,
      permission_level: m.permission_level,
      custom_prompt: m.custom_prompt || '',
    })),
  };
  for (const f of LIMIT_FIELDS) {
    const el = document.getElementById(f.id);
    const value = Number(el ? el.value : f.def);
    if (!Number.isInteger(value) || value < f.min || value > f.max) {
      setModalError(`${f.label} must be a whole number between ${f.min} and ${f.max}.`);
      if (el) el.focus();
      return;
    }
    payload[f.key] = value;
  }
  const missingModel = payload.members.find((m) => !m.model);
  if (missingModel) {
    setModalError(`Pick a model for ${roleLabel(missingModel.role)}.`);
    return;
  }

  setModalError('');
  if (saveBtn) saveBtn.disabled = true;
  try {
    const saved = modalTeamId ? await api.updateTeam(modalTeamId, payload) : await api.createTeam(payload);
    showToast(`${modalTeamId ? 'Saved' : 'Created'} team "${saved.name}"`, 'success');
    if (saved && saved.id) appStore.activeTeamId = saved.id;
    const autoCheck = document.getElementById('team-auto-approve-mockup');
    if (autoCheck) autoCheck.checked = Boolean(saved && saved.auto_approve_mockup);
    if (appCallbacks.refreshTeams) await appCallbacks.refreshTeams();
    renderTeamLaunchPanel();
    closeTeamSetupModal();
  } catch (err) {
    setModalError(`Failed to save team: ${err.message}`);
  } finally {
    if (saveBtn) saveBtn.disabled = false;
  }
}

async function deleteTeamFromModal() {
  if (!modalTeamId) return;
  const team = (appStore.teams || []).find((t) => t.id === modalTeamId);
  if (!window.confirm(`Delete team preset "${team ? team.name : modalTeamId}"?`)) return;
  try {
    await api.deleteTeam(modalTeamId);
    showToast('Team preset deleted', 'info');
    if (appStore.activeTeamId === modalTeamId) appStore.activeTeamId = null;
    if (appCallbacks.refreshTeams) await appCallbacks.refreshTeams();
    renderTeamLaunchPanel();
    closeTeamSetupModal();
  } catch (err) {
    setModalError(`Failed to delete team: ${err.message}`);
  }
}

// ---------------------------------------------------------------------------
// View entry & SSE
// ---------------------------------------------------------------------------

/**
 * Renders the full Team View (#view-team).
 */
export function renderTeamView() {
  if (!appStore) return;
  renderTeamLaunchPanel();
  renderTeamRunsList();
  updateTeamInboxBadge();

  const runs = appStore.teamRuns || [];
  if (!appStore.activeTeamRunId && runs.length > 0) {
    const first = runs.find((r) => r.status === 'waiting_user') || runs[0];
    selectTeamRun(first.id);
    return;
  }
  if (appStore.activeTeamRunId && !getActiveDetail()) {
    selectTeamRun(appStore.activeTeamRunId);
    return;
  }
  renderTeamBoard();
}

function trackTeamTurn(runId, role, turnId) {
  if (!runId || !role || !turnId) return;
  if (!appStore.teamRunTurns) appStore.teamRunTurns = {};
  if (!appStore.teamRunTurns[runId]) appStore.teamRunTurns[runId] = {};
  appStore.teamRunTurns[runId][role] = turnId;
}

function updateActiveSession(session) {
  const detail = getActiveDetail();
  if (!detail || !session) return false;
  const entry = (detail.sessions || []).find((s) => (s.session_id || (s.session && s.session.id)) === session.id);
  if (!entry) return false;
  entry.session = { ...(entry.session || {}), ...session };
  return true;
}

/**
 * Handles SSE events relevant to Team Mode (§7.4 + session/turn events for team sessions).
 * @param {string} eventName
 * @param {object} payload
 */
export function handleTeamSseEvent(eventName, payload) {
  if (!appStore || !payload) return;

  if (eventName === 'team_run_updated' || eventName === 'team_inbox_updated') {
    const run = payload.run;
    const runId = (run && run.id) || payload.run_id;
    if (!runId) return;
    const prev = (appStore.teamRuns || []).find((r) => r.id === runId);
    if (run) upsertRunInList(run);
    updateTeamInboxBadge();

    if (run && run.status === 'waiting_user' && (!prev || prev.stage_state !== run.stage_state || prev.status !== run.status) && (appStore.activeView !== 'team' || appStore.activeTeamRunId !== runId)) {
      showToast(`Team run needs your input: ${formatStageLabel(run.stage_state)}`, 'info');
    }

    if (appStore.activeTeamRunId === runId) {
      const detail = getActiveDetail();
      if (!detail || !run) {
        selectTeamRun(runId);
      } else {
        const stageChanged = detail.run.stage_state !== run.stage_state || detail.run.status !== run.status;
        detail.run = run;
        if (stageChanged) {
          // Refresh sessions/messages too (new sessions on start, user_action messages, etc.)
          selectTeamRun(runId);
        } else {
          scheduleRender('header', 'inbox', 'roster', 'stream');
        }
      }
    }
    scheduleRender('list');
    return;
  }

  if (eventName === 'team_message_created') {
    const msg = payload.message;
    const runId = payload.run_id || (msg && msg.team_run_id);
    const detail = getActiveDetail();
    if (!msg || !detail || detail.run.id !== runId) return;
    if (!Array.isArray(detail.messages)) detail.messages = [];
    if (!detail.messages.some((m) => m.id === msg.id)) {
      detail.messages.push(msg);
      detail.messages.sort((a, b) => Number(a.seq || 0) - Number(b.seq || 0));
    }
    scheduleRender('messages', 'stream');
    return;
  }

  if (eventName === 'session_updated') {
    const session = payload.session;
    if (session && session.team_run_id && session.team_run_id === appStore.activeTeamRunId) {
      if (!updateActiveSession(session)) {
        selectTeamRun(appStore.activeTeamRunId);
        return;
      }
      scheduleRender('roster', 'stream');
    }
    return;
  }

  if (eventName === 'turn_started') {
    const turn = payload.turn;
    const detail = getActiveDetail();
    if (!turn || !detail) return;
    const entry = (detail.sessions || []).find((s) => (s.session_id || (s.session && s.session.id)) === turn.session_id);
    if (!entry) return;
    trackTeamTurn(detail.run.id, entry.role, turn.id);
    entry.session = { ...(entry.session || {}), status: 'running' };
    scheduleRender('roster', 'stream');
    return;
  }

  if (eventName === 'turn_event') {
    const { team_run_id: runId, turn_id: turnId, role } = payload;
    if (!runId || !turnId || !role) return;
    trackTeamTurn(runId, role, turnId);
    if (runId === appStore.activeTeamRunId) scheduleRender('stream');
    return;
  }

  if (eventName === 'turn_completed') {
    const turn = payload.turn;
    if (!turn || !turn.id) return;
    completedTurnIds.add(turn.id);
    const detail = getActiveDetail();
    if (detail && (detail.sessions || []).some((s) => (s.session_id || (s.session && s.session.id)) === turn.session_id)) {
      scheduleRender('roster', 'stream');
    }
  }
}

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

async function startTeamRunFromForm() {
  const repoEl = document.getElementById('team-target-repo-input');
  const taskEl = document.getElementById('team-task-prompt-input');
  const autoEl = document.getElementById('team-auto-approve-mockup');
  const targetRepo = repoEl ? repoEl.value.trim() : '';
  const taskPrompt = taskEl ? taskEl.value.trim() : '';

  if (!targetRepo) {
    showToast('Enter the target repository folder (a git repo with at least one commit).', 'error');
    if (repoEl) repoEl.focus();
    return;
  }
  if (!taskPrompt) {
    showToast('Describe the task for the team.', 'error');
    if (taskEl) taskEl.focus();
    return;
  }

  const body = {
    task_prompt: taskPrompt,
    target_repo: targetRepo,
    auto_approve_mockup: Boolean(autoEl && autoEl.checked),
  };
  const detail = appStore.activeTeamId
    ? await api.startTeamRun(appStore.activeTeamId, body)
    : await api.startTeamRun(body);

  writeLocal(LAST_REPO_KEY, targetRepo);
  if (taskEl) taskEl.value = '';
  showToast('Team run started in an isolated git worktree', 'success');

  const run = detail && detail.run;
  if (run && run.id) {
    upsertRunInList(run);
    appStore.activeTeamRunId = run.id;
    appStore.activeTeamRunDetail = detail;
    streamRoleTab = null;
    openMessageIds.clear();
    lastInboxKey = null;
    updateTeamInboxBadge();
    renderTeamRunsList();
    renderTeamBoard();
    showTeamSubtab('board');
  } else if (appCallbacks.refreshTeamRuns) {
    await appCallbacks.refreshTeamRuns();
  }
  if (appCallbacks.refreshSessions) appCallbacks.refreshSessions();
}

/**
 * Initializes Team View event listeners.
 * @param {object} store
 * @param {object} callbacks
 */
export function initTeamView(store, callbacks) {
  appStore = store;
  appCallbacks = callbacks || {};
  if (isInitialized) return;
  isInitialized = true;

  showTeamSubtab('launch');

  const presetSelect = document.getElementById('team-preset-select');
  if (presetSelect) {
    presetSelect.addEventListener('change', () => {
      appStore.activeTeamId = presetSelect.value || null;
      const team = (appStore.teams || []).find((t) => t.id === appStore.activeTeamId);
      const autoCheck = document.getElementById('team-auto-approve-mockup');
      if (autoCheck) autoCheck.checked = Boolean(team && team.auto_approve_mockup);
    });
  }

  const configureBtn = document.getElementById('btn-configure-team');
  if (configureBtn) configureBtn.addEventListener('click', () => openTeamSetupModal());

  const startBtn = document.getElementById('btn-start-team-run');
  if (startBtn) {
    startBtn.addEventListener('click', async () => {
      const original = startBtn.textContent;
      startBtn.disabled = true;
      startBtn.textContent = 'Starting...';
      try {
        await startTeamRunFromForm();
      } catch (err) {
        showToast(`Failed to start team run: ${err.message}`, 'error');
      } finally {
        startBtn.disabled = false;
        startBtn.textContent = original;
      }
    });
  }

  const stopRunBtn = document.getElementById('btn-stop-team-run');
  if (stopRunBtn) {
    stopRunBtn.addEventListener('click', async () => {
      const runId = appStore.activeTeamRunId;
      if (!runId) return;
      if (!window.confirm('Stop this team run? Active agent turns are cancelled; the worktree is kept so you can keep or discard the branch.')) return;
      stopRunBtn.disabled = true;
      try {
        const detail = await api.stopTeamRun(runId);
        showToast('Team run stopped', 'info');
        await applyDetailResult(runId, detail);
      } catch (err) {
        showToast(`Failed to stop team run: ${err.message}`, 'error');
      } finally {
        stopRunBtn.disabled = false;
      }
    });
  }

  // Team setup modal
  const modal = document.getElementById('team-setup-modal');
  const closeBtn = document.getElementById('btn-close-team-modal');
  const cancelBtn = document.getElementById('btn-cancel-team-modal');
  if (closeBtn) closeBtn.addEventListener('click', closeTeamSetupModal);
  if (cancelBtn) cancelBtn.addEventListener('click', closeTeamSetupModal);
  if (modal) {
    modal.addEventListener('click', (e) => {
      if (e.target === modal) closeTeamSetupModal();
    });
  }
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && modal && !modal.classList.contains('hidden')) closeTeamSetupModal();
  });

  const setupSelect = document.getElementById('team-setup-select');
  if (setupSelect) {
    setupSelect.addEventListener('change', () => {
      const team = (appStore.teams || []).find((t) => t.id === setupSelect.value) || null;
      populateTeamSetupForm(team);
    });
  }

  const newPresetBtn = document.getElementById('btn-new-team-preset');
  if (newPresetBtn) newPresetBtn.addEventListener('click', () => populateTeamSetupForm(null));

  const delPresetBtn = document.getElementById('btn-delete-team-preset');
  if (delPresetBtn) delPresetBtn.addEventListener('click', () => deleteTeamFromModal());

  const resetBtn = document.getElementById('btn-reset-team-defaults');
  if (resetBtn) {
    resetBtn.addEventListener('click', () => {
      modalMembers = getDefaultMembersClone();
      renderModalMembers();
      showToast('Roles reset to recommended defaults (not saved yet)', 'info');
    });
  }

  const setupForm = document.getElementById('team-setup-form');
  if (setupForm) {
    setupForm.addEventListener('submit', (e) => {
      e.preventDefault();
      saveTeamFromModal();
    });
  }

  const btnSubLaunch = document.getElementById('team-subtab-launch');
  const btnSubBoard = document.getElementById('team-subtab-board');
  if (btnSubLaunch) btnSubLaunch.addEventListener('click', () => showTeamSubtab('launch'));
  if (btnSubBoard) btnSubBoard.addEventListener('click', () => showTeamSubtab('board'));
}
