/**
 * Baton — Reusable UI Components & DOM Helpers
 */

/**
 * Escapes raw HTML entities to prevent XSS.
 * @param {any} str
 * @returns {string}
 */
export function escapeHtml(str) {
  if (str == null) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

/**
 * Safely escapes HTML first, then formats fenced code blocks, inline code,
 * headings, bold/italics, links, bullet lists, and Reviewer verdict banners.
 * @param {string} text
 * @returns {string}
 */
export function renderMarkdown(text) {
  if (!text) return '';

  // 1. Always escape raw HTML entities FIRST
  let safe = escapeHtml(text);

  // 2. Extract and preserve fenced code blocks before inline markdown processing
  const codeBlocks = [];
  safe = safe.replace(/```([a-zA-Z0-9_\-]+)?\r?\n([\s\S]*?)```/g, (match, lang, code) => {
    const placeholder = `__CODE_BLOCK_${codeBlocks.length}__`;
    const languageClass = lang ? ` class="language-${lang.trim()}"` : '';
    const langBadge = lang ? `<span class="code-lang-tag">${lang.trim()}</span>` : '';
    const html = `<div class="code-block-wrapper">${langBadge}<pre><code${languageClass}>${code.trimEnd()}</code></pre></div>`;
    codeBlocks.push(html);
    return placeholder;
  });

  // Extract and preserve inline code spans before inline markdown processing
  const inlineCodeBlocks = [];
  safe = safe.replace(/`([^`\r\n]+)`/g, (match, code) => {
    const placeholder = `__INLINE_CODE_${inlineCodeBlocks.length}__`;
    inlineCodeBlocks.push(`<code class="inline-code">${code}</code>`);
    return placeholder;
  });

  // 3. Verdict banners (strict Reviewer machine-readable output)
  safe = safe.replace(
    /(?:^|\n)\s*[*`#_~>\-\s]*VERDICT\s*:\s*[*`_\s]*APPROVED[*`_\s]*(?:\n|$)/gi,
    '\n<div class="verdict-banner approved"><span class="verdict-icon">✓</span> <span class="verdict-title">VERDICT: APPROVED</span></div>\n'
  );
  safe = safe.replace(
    /(?:^|\n)\s*[*`#_~>\-\s]*VERDICT\s*:\s*[*`_\s]*CHANGES_REQUESTED[*`_\s]*(?:\n|$)/gi,
    '\n<div class="verdict-banner changes-requested"><span class="verdict-icon">⚠</span> <span class="verdict-title">VERDICT: CHANGES_REQUESTED</span></div>\n'
  );

  // Findings header
  safe = safe.replace(/(?:^|\n)\s*(?:[*#_]*\s*)?FINDINGS\s*:\s*(?:\n|$)/gi, '\n<div class="findings-header"><strong>FINDINGS:</strong></div>\n');

  // 4. Headings (#, ##, ###)
  safe = safe.replace(/^###\s+(.+)$/gm, '<h3>$1</h3>');
  safe = safe.replace(/^##\s+(.+)$/gm, '<h2>$1</h2>');
  safe = safe.replace(/^#\s+(.+)$/gm, '<h1>$1</h1>');

  // 5. Inline formatting (bold, italic)
  safe = safe.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  safe = safe.replace(/(^|[^*])\*([^*]+)\*/g, '$1<em>$2</em>');

  // 6. Links: [text](url)
  safe = safe.replace(/\[([^\]]+)\]\(([^)]+)\)/g, (match, text, rawUrl) => {
    const url = rawUrl.trim();
    if (/^https?:\/\//i.test(url)) {
      const safeUrl = url.replace(/"/g, '&quot;');
      return `<a href="${safeUrl}" target="_blank" rel="noopener noreferrer">${text}</a>`;
    }
    if (/^file:\/\//i.test(url)) {
      let path = url.replace(/^file:\/\//i, '').replace(/#.*$/, '');
      const unescaped = path
        .replace(/&amp;/g, '&')
        .replace(/&lt;/g, '<')
        .replace(/&gt;/g, '>')
        .replace(/&quot;/g, '"')
        .replace(/&#39;/g, "'");
      try {
        path = decodeURIComponent(unescaped);
      } catch (_) {
        path = unescaped;
      }
      const safePath = escapeHtml(path);
      return `<span class="file-link" title="${safePath}">${text}</span>`;
    }
    return text;
  });

  // 7. Bullet lists
  // Group consecutive lines starting with '- ' or '* '
  safe = safe.replace(/(?:^[ \t]*[-*]\s+.+(?:\r?\n|$))+/gm, (listBlock) => {
    const items = listBlock
      .trim()
      .split(/\r?\n/)
      .map(item => item.replace(/^[ \t]*[-*]\s+/, '').trim())
      .filter(Boolean)
      .map(item => `<li>${item}</li>`)
      .join('');
    return `<ul>${items}</ul>\n`;
  });

  // 8. Paragraphs & line breaks (outside of code blocks and headers)
  // Split by double newline into paragraphs
  const paragraphs = safe.split(/\n{2,}/);
  safe = paragraphs
    .map(p => {
      const trimmed = p.trim();
      if (!trimmed) return '';
      // Don't wrap if already starting with block tags
      if (/^<(div|ul|ol|h1|h2|h3|pre|blockquote)/i.test(trimmed) || trimmed.startsWith('__CODE_BLOCK_')) {
        return trimmed;
      }
      return `<p>${trimmed.replace(/\n/g, '<br>')}</p>`;
    })
    .filter(Boolean)
    .join('\n');

  // 9. Restore code blocks
  safe = safe.replace(/__INLINE_CODE_(\d+)__/g, (match, index) => {
    return inlineCodeBlocks[Number(index)] || '';
  });
  safe = safe.replace(/__CODE_BLOCK_(\d+)__/g, (match, index) => {
    return codeBlocks[Number(index)] || '';
  });

  return safe;
}

/**
 * Returns HTML string for .badge-provider ('Claude' or 'AGY').
 * @param {string} providerId
 * @returns {string}
 */
export function renderProviderBadge(providerId) {
  const p = (providerId || '').toLowerCase();
  if (p === 'claude') {
    return `<span class="badge-provider badge-claude">Claude</span>`;
  }
  if (p === 'antigravity' || p === 'agy') {
    return `<span class="badge-provider badge-agy">AGY</span>`;
  }
  return `<span class="badge-provider">${escapeHtml(providerId || 'Unknown')}</span>`;
}

/**
 * Returns HTML string for .badge-role.
 * @param {string} roleId
 * @returns {string}
 */
export function renderRoleBadge(roleId) {
  const r = (roleId || '').toLowerCase();
  const labelMap = {
    designer: 'Designer',
    planner: 'Planner',
    implementer: 'Implementer',
    reviewer: 'Code Reviewer',
    custom: 'Custom',
  };
  const label = labelMap[r] || escapeHtml(roleId || 'Unknown');
  return `<span class="badge-role badge-role-${escapeHtml(r)}">${label}</span>`;
}

/**
 * Returns HTML string for .badge-permission.
 * @param {string} permissionLevel
 * @returns {string}
 */
export function renderPermissionBadge(permissionLevel) {
  const perm = (permissionLevel || '').toLowerCase();
  const permMap = {
    readonly: { label: 'Read-Only', icon: '🔒' },
    edit: { label: 'Edit Files', icon: '✏️' },
    full: { label: 'Full Access', icon: '⚡' },
  };
  const item = permMap[perm] || { label: escapeHtml(permissionLevel || 'Unknown'), icon: '🛡️' };
  return `<span class="badge-permission badge-perm-${escapeHtml(perm)}"><span class="badge-icon">${item.icon}</span> ${item.label}</span>`;
}

/**
 * Returns HTML string for .status-dot.
 * @param {string} status
 * @returns {string}
 */
export function renderStatusDot(status) {
  const s = (status || 'idle').toLowerCase();
  return `<span class="status-dot ${escapeHtml(s)}" title="${escapeHtml(status || 'idle')}"></span>`;
}

/**
 * Returns HTML string for status pill badge.
 * @param {string} status
 * @returns {string}
 */
export function renderStatusBadge(status) {
  const s = (status || 'idle').toLowerCase();
  const labelMap = {
    idle: 'Idle',
    running: 'Running',
    waiting_approval: 'Waiting Approval',
    completed: 'Completed',
    changes_requested: 'Changes Requested',
    error: 'Error',
    failed: 'Failed',
    timeout: 'Timed Out',
    stopped: 'Stopped',
    pending: 'Pending',
  };
  const label = labelMap[s] || escapeHtml(status || 'Unknown');
  return `<span class="status-badge status-${escapeHtml(s)}">${renderStatusDot(s)} <span class="status-text">${label}</span></span>`;
}

/**
 * Extract a concise one-line primary parameter summary from tool input.
 * @param {object|string} input
 * @returns {string}
 */
function extractToolParamSummary(input) {
  if (!input) return '';
  if (typeof input === 'string') {
    return input.length > 50 ? input.slice(0, 47) + '...' : input;
  }
  if (typeof input !== 'object') return String(input);

  const priorityKeys = [
    'Path', 'path', 'file_path', 'filePath', 'file',
    'CommandLine', 'command', 'cmd',
    'query', 'prompt', 'url', 'pattern', 'target'
  ];

  for (const key of priorityKeys) {
    if (input[key] !== undefined && input[key] !== null) {
      const val = String(input[key]);
      const shortVal = val.length > 45 ? val.slice(0, 42) + '...' : val;
      return `${key}: ${shortVal}`;
    }
  }

  // Fallback to first key
  const keys = Object.keys(input);
  if (keys.length > 0) {
    const firstKey = keys[0];
    const val = String(input[firstKey]);
    const shortVal = val.length > 35 ? val.slice(0, 32) + '...' : val;
    return `${firstKey}: ${shortVal}`;
  }

  return '';
}

/**
 * Returns HTML string for .tool-card (collapsible <details> with tool name,
 * primary param summary, and pretty-printed JSON input/output).
 * @param {object} eventOrToolState
 * @returns {string}
 */
export function renderToolCard(eventOrToolState) {
  if (!eventOrToolState) return '';

  const toolName = eventOrToolState.tool_name || eventOrToolState.name || 'tool';
  const toolInput = eventOrToolState.tool_input !== undefined
    ? eventOrToolState.tool_input
    : (eventOrToolState.input || eventOrToolState.params || null);
  const toolOutput = eventOrToolState.tool_output !== undefined
    ? eventOrToolState.tool_output
    : (eventOrToolState.output !== undefined ? eventOrToolState.output : null);

  const isError = Boolean(
    eventOrToolState.is_error ||
    eventOrToolState.status === 'error' ||
    eventOrToolState.tool_state === 'ERROR'
  );

  const isDone = Boolean(
    toolOutput !== null ||
    eventOrToolState.status === 'completed' ||
    eventOrToolState.tool_state === 'DONE' ||
    eventOrToolState.type === 'tool_result'
  );

  let statusIconHtml;
  if (isError) {
    statusIconHtml = '<span class="tool-status-icon error" title="Error">✗</span>';
  } else if (isDone) {
    statusIconHtml = '<span class="tool-status-icon success" title="Completed">✓</span>';
  } else {
    statusIconHtml = '<span class="tool-status-icon running" title="Running">⏳</span>';
  }

  const paramSummary = extractToolParamSummary(toolInput);

  let inputBody = '';
  if (toolInput !== null && toolInput !== undefined) {
    const formatted = typeof toolInput === 'string'
      ? toolInput
      : JSON.stringify(toolInput, null, 2);
    inputBody = `
      <div class="tool-section">
        <div class="tool-section-label">Input</div>
        <pre class="tool-json"><code>${escapeHtml(formatted)}</code></pre>
      </div>`;
  }

  let outputBody = '';
  if (toolOutput !== null && toolOutput !== undefined) {
    const formatted = typeof toolOutput === 'string'
      ? toolOutput
      : JSON.stringify(toolOutput, null, 2);
    outputBody = `
      <div class="tool-section">
        <div class="tool-section-label">Output${isError ? ' (Error)' : ''}</div>
        <pre class="tool-json${isError ? ' tool-error-json' : ''}"><code>${escapeHtml(formatted)}</code></pre>
      </div>`;
  }

  return `
<details class="tool-card${isError ? ' is-error' : ''}">
  <summary class="tool-summary">
    ${statusIconHtml}
    <span class="tool-name">${escapeHtml(toolName)}</span>
    ${paramSummary ? `<span class="tool-param-summary">${escapeHtml(paramSummary)}</span>` : ''}
    <span class="tool-expand-chevron">▾</span>
  </summary>
  <div class="tool-details-body">
    ${inputBody}
    ${outputBody}
  </div>
</details>`.trim();
}

/**
 * Formats duration in milliseconds into "14.2s" or "1m 05s".
 * @param {number|null} durationMs
 * @returns {string}
 */
export function formatDuration(durationMs) {
  if (durationMs == null || isNaN(durationMs)) return '—';
  const ms = Number(durationMs);
  if (ms < 0) return '—';
  if (ms < 1000) return `${ms}ms`;

  const totalSec = ms / 1000;
  if (totalSec < 60) {
    return `${totalSec.toFixed(1)}s`;
  }

  const roundedSec = Math.floor(totalSec);
  const minutes = Math.floor(roundedSec / 60);
  const seconds = roundedSec % 60;
  const secStr = seconds < 10 ? `0${seconds}` : `${seconds}`;
  return `${minutes}m ${secStr}s`;
}

/**
 * Formats an ISO-8601 UTC timestamp string into readable local time.
 * @param {string|null} isoStr
 * @returns {string}
 */
export function formatTimestamp(isoStr) {
  if (!isoStr) return '—';
  try {
    const d = new Date(isoStr);
    if (isNaN(d.getTime())) return String(isoStr);
    return d.toLocaleTimeString([], {
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      hour12: false,
    });
  } catch (_) {
    return String(isoStr);
  }
}

/**
 * Displays a toast notification in #toast-container.
 * @param {string} message
 * @param {"info"|"success"|"warning"|"error"} type
 */
export function showToast(message, type = 'info') {
  let container = document.getElementById('toast-container');
  if (!container) {
    container = document.createElement('div');
    container.id = 'toast-container';
    document.body.appendChild(container);
  }

  const toast = document.createElement('div');
  toast.className = `toast toast-${escapeHtml(type)}`;

  const iconMap = {
    info: 'ℹ️',
    success: '✓',
    warning: '⚠️',
    error: '✗',
  };
  const icon = iconMap[type] || 'ℹ️';

  toast.innerHTML = `
    <span class="toast-icon">${icon}</span>
    <span class="toast-message">${escapeHtml(message)}</span>
    <button type="button" class="toast-close" aria-label="Close">×</button>
  `;

  const dismiss = () => {
    toast.classList.add('toast-fade-out');
    setTimeout(() => {
      if (toast.parentNode) {
        toast.parentNode.removeChild(toast);
      }
    }, 200);
  };

  const closeBtn = toast.querySelector('.toast-close');
  if (closeBtn) {
    closeBtn.addEventListener('click', dismiss);
  }

  container.appendChild(toast);

  // Auto-remove after 4 seconds
  setTimeout(dismiss, 4000);
}

export default {
  escapeHtml,
  renderMarkdown,
  renderProviderBadge,
  renderRoleBadge,
  renderPermissionBadge,
  renderStatusDot,
  renderStatusBadge,
  renderToolCard,
  formatDuration,
  formatTimestamp,
  showToast,
};
