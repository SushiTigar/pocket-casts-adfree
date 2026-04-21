let podcasts = [];
    let upNextEpisodes = [];
    let pcEpisodeStatus = {};
    let selectedEpisodes = {};
    let expandedPodcasts = new Set();
    let podcastEpisodes = {};
    let loadingEpisodes = new Set();
    let activeJobId = null;
    let pollTimer = null;
    let lastLogCursor = 0;
    let statFilter = null;
    let processedPodcastUuids = new Set();
    let uploadedFiles = [];
    let serviceHealth = { minuspod: false, pocketcasts: false };
    let currentView = 'dashboard';
    let historyEntries = null;

    function toggleTheme() {
      const isLight = document.documentElement.classList.toggle('light');
      localStorage.setItem('theme', isLight ? 'light' : 'dark');
      el('theme-btn').innerHTML = isLight ? '&#9728;' : '&#9790;';
    }
    (function initTheme() {
      const saved = localStorage.getItem('theme');
      if (saved === 'light') {
        document.documentElement.classList.add('light');
        document.addEventListener('DOMContentLoaded', () => {
          const b = document.getElementById('theme-btn');
          if (b) b.innerHTML = '&#9728;';
        });
      }
    })();

    function toggleLogPanel() {
      const panel = el('log-panel');
      panel.classList.toggle('collapsed');
      if (!panel.classList.contains('collapsed')) {
        el('log-unread').classList.remove('has-new');
        el('log-body').scrollTop = el('log-body').scrollHeight;
      }
    }

    async function api(path, opts = {}) {
      const resp = await fetch('/api' + path, {
        headers: { 'Content-Type': 'application/json' }, ...opts
      });
      return resp.json();
    }

    async function checkStatus() {
      try {
        const d = await api('/status');
        serviceHealth = { minuspod: !!d.minuspod, pocketcasts: !!d.pocketcasts };
      } catch { }
    }

    async function loadSubscriptions() {
      try {
        const [d, filesResp] = await Promise.all([
          api('/subscriptions'),
          api('/files').catch(() => ({ files: [] })),
        ]);
        podcasts = d.podcasts || [];
        upNextEpisodes = d.up_next_episodes || [];
        pcEpisodeStatus = d.episode_status || {};
        processedPodcastUuids = new Set(d.processed_podcast_uuids || []);
        uploadedFiles = filesResp.files || [];
        el('stat-total').textContent = d.total || 0;
        el('stat-eligible').textContent = d.eligible || 0;
        el('stat-patreon').textContent = d.patreon || 0;
        el('stat-processed').textContent = d.processed_count || 0;
        renderPodcasts();
      } catch(e) {
        el('podcast-list').innerHTML = '<div class="empty-state"><h3>Error loading</h3><p>' + e.message + '</p></div>';
      }
    }

    async function loadUploadedFiles() {
      try {
        const d = await api('/files');
        uploadedFiles = d.files || [];
      } catch(e) { uploadedFiles = []; }
    }

    function setStatFilter(filter) {
      if (statFilter === filter) {
        statFilter = null;
      } else {
        statFilter = filter;
      }
      document.querySelectorAll('.stat-card').forEach(c => c.classList.remove('active'));
      if (statFilter) {
        el('stat-card-' + statFilter).classList.add('active');
      }
      renderPodcasts();
    }

    function getFilteredPodcasts() {
      let list = podcasts;
      const q = el('search').value.toLowerCase();
      if (q) {
        list = list.filter(p =>
          p.title.toLowerCase().includes(q) || (p.author||'').toLowerCase().includes(q)
        );
      }
      if (statFilter === 'eligible') {
        list = list.filter(p => !p.is_patreon);
      } else if (statFilter === 'patreon') {
        list = list.filter(p => p.is_patreon);
      } else if (statFilter === 'processed') {
        list = list.filter(p => processedPodcastUuids.has(p.uuid));
      }
      return list;
    }

    function getSelectedCount() {
      let n = 0;
      for (const uuid in selectedEpisodes) {
        if (uuid === '_files') {
          n += selectedEpisodes['_files'].size;
          continue;
        }
        for (const epId of selectedEpisodes[uuid]) {
          const eps = podcastEpisodes[uuid] || [];
          const ep = eps.find(e => e.id === epId);
          // Count as selectable if:
          //  - matched in MinusPod and not already processed, OR
          //  - not matched in MinusPod at all (Up Next / custom path — backend
          //    will resolve via source URL / title)
          if (!ep || !ep.already_processed) n++;
        }
      }
      return n;
    }

    function isPodcastSelected(uuid) {
      return selectedEpisodes[uuid] && selectedEpisodes[uuid].size > 0;
    }

    function getPodcastTitle(puuid) {
      if (puuid === '_files' || puuid === 'da7aba5e-f11e-f11e-f11e-da7aba5ef11e') return 'CUSTOM FILES';
      const p = podcasts.find(x => x.uuid === puuid);
      return p ? p.title : puuid;
    }

    function renderPodcasts() {
      const filtered = getFilteredPodcasts();
      const q = el('search').value.toLowerCase();
      let html = '';

      // --- In Up Next: show individual episodes ---
      if (!statFilter || statFilter === 'all') {
        const upNextRegular = (upNextEpisodes || []).filter(e =>
          e.podcast_uuid && e.podcast_uuid !== '_files' && e.podcast_uuid !== 'da7aba5e-f11e-f11e-f11e-da7aba5ef11e'
        );
        const filteredRegular = q
          ? upNextRegular.filter(e =>
              e.title.toLowerCase().includes(q) || getPodcastTitle(e.podcast_uuid).toLowerCase().includes(q)
            )
          : upNextRegular;

        const filesFiltered = q
          ? uploadedFiles.filter(f => (f.title || '').toLowerCase().includes(q))
          : uploadedFiles;

        if (filteredRegular.length > 0 || filesFiltered.length > 0) {
          const totalCount = filteredRegular.length + filesFiltered.length;
          html += `<div class="section-header up-next">
            <span>In Up Next</span>
            <span class="section-count">${filteredRegular.length} episode${filteredRegular.length === 1 ? '' : 's'}${filesFiltered.length ? ` &middot; ${filesFiltered.length} custom file${filesFiltered.length === 1 ? '' : 's'}` : ''}</span>
          </div>`;

          // Group regular Up Next episodes by podcast
          const upNextByPodcast = {};
          for (const ep of filteredRegular) {
            (upNextByPodcast[ep.podcast_uuid] ||= []).push(ep);
          }
          for (const [puuid, eps] of Object.entries(upNextByPodcast)) {
            const podTitle = getPodcastTitle(puuid);
            const isExp = expandedPodcasts.has(`upnext-${puuid}`);
            const hasSel = isPodcastSelected(puuid);
            const isPat = podcasts.find(x => x.uuid === puuid)?.is_patreon;
            const allAdFree = eps.every(e => e.title.includes('(Ad-Free)'));
            const cls = ['podcast-card',
              hasSel ? 'selected' : '',
              isExp ? 'expanded' : '',
              isPat ? 'patreon' : '',
              allAdFree ? 'processed' : ''
            ].filter(Boolean).join(' ');
            html += `<div class="${cls}" style="margin-bottom:4px;">
              <div class="podcast-header" onclick="togglePodcast('upnext-${puuid}', '${puuid}')" style="padding:10px 14px;">
                <span class="up-next-badge">Up Next</span>
                <div class="podcast-info" style="flex:1;min-width:0">
                  <div class="podcast-title">${esc(podTitle)}</div>
                  <div class="podcast-author" style="font-size:11px;color:var(--text-muted)">${eps.length} episode${eps.length > 1 ? 's' : ''} in queue</div>
                </div>
                <span class="section-count">${eps.length}</span>
                ${!allAdFree ? '<span class="expand-icon">&#9654;</span>' : ''}
              </div>`;
            if (isExp && !allAdFree) {
              html += `<div class="episode-list" style="padding:4px 10px;">`;
              // Kick off MinusPod episode lookup in the background so that if
              // the user later selects a row it can be matched to its MinusPod
              // episode id (needed to queue ad-detection). Not required for
              // rendering — all metadata below comes from /api/subscriptions.
              if (!podcastEpisodes[puuid] && !loadingEpisodes.has(puuid)) {
                loadEpisodes(puuid);
              }
              for (const ep of eps) {
                let isDone = ep.title.includes('(Ad-Free)');
                let epId = ep.uuid;
                if (podcastEpisodes[puuid]) {
                  const match = podcastEpisodes[puuid].find(pe => pe.title === ep.title || pe.title === ep.title + ' (Ad-Free)');
                  if (match) {
                    epId = match.id;
                    if (match.already_processed) isDone = true;
                  }
                }
                html += renderUpNextRow(ep, puuid, epId, isDone);
              }
              html += `</div>`;
            }
            html += `</div>`;
          }

          // CUSTOM FILES card lives inside Up Next now (single source of truth).
          if (filesFiltered.length > 0) {
            const isExp = expandedPodcasts.has('custom-files-all');
            const adFreeCount = filesFiltered.filter(f => f.ad_free).length;
            const playedCount = filesFiltered.filter(f => f.playing_status === 3).length;
            html += `<div class="podcast-card ${isExp ? 'expanded' : ''}" style="margin-bottom:4px;">
              <div class="podcast-header" onclick="toggleCustomFiles()" style="padding:10px 14px;">
                <span class="up-next-badge files-badge">Custom Files</span>
                <div class="podcast-info" style="flex:1;min-width:0">
                  <div class="podcast-title">Uploaded files</div>
                  <div class="podcast-author" style="font-size:11px;color:var(--text-muted)">${filesFiltered.length} file${filesFiltered.length === 1 ? '' : 's'}${adFreeCount ? ` &middot; ${adFreeCount} ad-free` : ''}${playedCount ? ` &middot; ${playedCount} played` : ''}</div>
                </div>
                <span class="section-count">${filesFiltered.length}</span>
                <span class="expand-icon">&#9654;</span>
              </div>`;
            if (isExp) {
              html += `<div class="episode-list" style="padding:4px 10px;">`;
              for (const f of filesFiltered) html += renderFileRow(f);
              html += `</div>`;
            }
            html += `</div>`;
          }
        }
      }

      // --- All Podcasts ---
      if (!filtered.length && !html) {
        el('podcast-list').innerHTML = '<div class="empty-state"><h3>No podcasts found</h3></div>';
        updateProcessBtn();
        return;
      }

      if (filtered.length > 0) {
        html += `<div class="section-header" style="margin-top:16px">
          <span>All Podcasts</span>
          <span class="section-count">${filtered.length}</span>
        </div>`;
        html += renderPodcastGroup(filtered);
      }

      el('podcast-list').innerHTML = html;
      document.querySelectorAll('.podcast-check[data-indeterminate="1"]').forEach(c => { c.indeterminate = true; });
      updateProcessBtn();
    }

    function toggleCustomFiles() {
      if (expandedPodcasts.has('custom-files-all')) {
        expandedPodcasts.delete('custom-files-all');
      } else {
        expandedPodcasts.add('custom-files-all');
      }
      renderPodcasts();
    }

    function renderFileRow(f) {
      const statusLabel = f.playing_status === 3 ? 'played'
                        : f.playing_status === 2 ? 'in-progress'
                        : 'unplayed';
      const dur = formatDur(f.duration);
      // Pocket Casts returns `1970-01-01` for some freshly-uploaded files
      // before their publish metadata settles. Treat any pre-2000 date as
      // "no date available" rather than rendering "Dec 31, 1969".
      const pub = f.published ? formatDate(f.published) : '';
      const progPct = f.duration > 0 ? Math.round((f.played_up_to / f.duration) * 100) : 0;
      const progress = f.playing_status === 2 ? ` · ${progPct}%` : '';
      const thumb = f.image_url && f.image_status === 2
        ? `<img class="file-thumb" src="${esc(f.image_url)}" alt="" onerror="this.style.display='none'">`
        : `<div class="file-thumb"></div>`;
      const markLabel = f.playing_status === 3 ? 'Mark unplayed' : 'Mark played';
      return `<div class="file-row" data-uuid="${f.uuid}">
        ${thumb}
        <div class="file-info">
          <div class="file-title" title="${esc(f.title)}">${esc(f.title)}</div>
          <div class="file-meta">
            <span class="file-pill ${statusLabel}">${statusLabel}${progress}</span>
            ${pub ? `<span>${pub}</span>` : ''}
            <span>${dur}</span>
          </div>
        </div>
        <div class="file-actions">
          <button onclick="event.stopPropagation(); renameFile('${f.uuid}')" title="Rename">Rename</button>
          <button onclick="event.stopPropagation(); toggleFilePlayed('${f.uuid}', ${f.playing_status !== 3})" title="${markLabel}">${markLabel}</button>
          <button onclick="event.stopPropagation(); removeFileFromUpNext('${f.uuid}')" title="Remove from Up Next">Un-queue</button>
          <button class="danger" onclick="event.stopPropagation(); deleteFile('${f.uuid}', '${esc(f.title).replace(/'/g, '&#39;')}')" title="Delete">Delete</button>
        </div>
      </div>`;
    }

    /**
     * Render a row for a regular (non-custom-file) Up Next episode. Mirrors
     * renderFileRow so the IN UP NEXT section has a consistent look whether
     * the item is an uploaded Ad-Free file or an original podcast episode.
     *
     * @param {object} ep - Up Next episode (from /api/subscriptions).
     * @param {string} podcastUuid - Owning podcast UUID.
     * @param {string} minusPodEpId - Best-effort MinusPod episode id (may be
     *   the same as ep.uuid until /api/episodes resolves a match).
     * @param {boolean} isDone - True if this episode has an Ad-Free twin.
     */
    function renderUpNextRow(ep, podcastUuid, minusPodEpId, isDone) {
      const status = ep.playing_status === 3 ? 'played'
                   : ep.playing_status === 2 ? 'in-progress'
                   : 'unplayed';
      const dur = formatDur(ep.duration || 0);
      const pub = ep.published ? formatDate(ep.published) : '';
      const progPct = ep.duration > 0 ? Math.round(((ep.played_up_to || 0) / ep.duration) * 100) : 0;
      const progress = ep.playing_status === 2 ? ` · ${progPct}%` : '';
      const selSet = selectedEpisodes[podcastUuid] || new Set();
      const isSel = !isDone && selSet.has(minusPodEpId);
      const rowCls = [
        'file-row',
        'up-next-row',
        isDone ? 'done' : 'selectable',
        isSel ? 'selected' : '',
      ].filter(Boolean).join(' ');
      const rowClick = isDone
        ? ''
        : `onclick="toggleEp('${podcastUuid}','${minusPodEpId}')"`;
      const markLabel = ep.playing_status === 3 ? 'Mark unplayed' : 'Mark played';
      const markPlayed = ep.playing_status !== 3;
      return `<div class="${rowCls}" data-ep-uuid="${esc(ep.uuid)}" ${rowClick}>
        <div class="file-thumb"></div>
        <div class="file-info">
          <div class="file-title" title="${esc(ep.title)}">${esc(ep.title)}</div>
          <div class="file-meta">
            <span class="file-pill ${status}">${status}${progress}</span>
            ${isDone ? '<span class="file-pill played">processed</span>' : ''}
            ${pub ? `<span>${pub}</span>` : ''}
            <span>${dur}</span>
          </div>
        </div>
        <div class="file-actions">
          <button onclick="event.stopPropagation(); toggleEpisodePlayed('${podcastUuid}','${esc(ep.uuid)}',${markPlayed})" title="${markLabel}">${markLabel}</button>
          <button onclick="event.stopPropagation(); removeEpisodeFromUpNext('${esc(ep.uuid)}')" title="Remove from Up Next">Un-queue</button>
        </div>
      </div>`;
    }

    async function removeEpisodeFromUpNext(episodeUuid) {
      const r = await api(`/pc_episode/${episodeUuid}/up_next`, { method: 'DELETE' });
      if (r.ok) { addLog('info', 'Removed from Up Next'); await loadSubscriptions(); }
      else addLog('error', 'Remove failed: ' + (r.error || ''));
    }

    async function renameFile(uuid) {
      const f = uploadedFiles.find(x => x.uuid === uuid);
      if (!f) return;
      const newTitle = prompt('New title:', f.title);
      if (newTitle === null || newTitle.trim() === f.title) return;
      addLog('info', `Renaming to: ${newTitle}`);
      const r = await api(`/files/${uuid}`, {
        method: 'PATCH',
        body: JSON.stringify({ title: newTitle.trim() }),
      });
      if (r.ok) { addLog('success', 'Renamed'); await loadUploadedFiles(); renderPodcasts(); }
      else addLog('error', 'Rename failed');
    }

    async function toggleFilePlayed(uuid, played) {
      const r = await api(`/files/${uuid}`, {
        method: 'PATCH',
        body: JSON.stringify({ playing_status: played ? 3 : 0, played_up_to: 0 }),
      });
      if (r.ok) { addLog('info', `Marked ${played ? 'played' : 'unplayed'}`); await loadUploadedFiles(); renderPodcasts(); }
      else addLog('error', 'Status update failed');
    }

    async function removeFileFromUpNext(uuid) {
      const r = await api(`/files/${uuid}/up_next`, { method: 'DELETE' });
      if (r.ok) { addLog('info', 'Removed from Up Next'); await loadSubscriptions(); }
      else addLog('error', 'Remove failed: ' + (r.error || ''));
    }

    async function deleteFile(uuid, title) {
      if (!confirm(`Delete from Pocket Casts cloud?\n\n${title}\n\nThis permanently removes the uploaded file and clears its processed marker so it can be re-processed.`)) return;
      addLog('warn', `Deleting: ${title}`);
      const r = await api(`/files/${uuid}`, { method: 'DELETE' });
      if (r.ok) { addLog('success', 'Deleted'); await loadSubscriptions(); }
      else addLog('error', 'Delete failed');
    }

    async function cleanupPlayedFiles() {
      const includeInProgress = confirm(
        'Clean up PLAYED (Ad-Free) uploaded files?\n\n' +
        'OK  = only fully played files\n' +
        'Cancel = abort\n\n' +
        '(After confirming, you can also include files that are 90%+ complete.)'
      );
      if (!includeInProgress) return;
      const also90 = confirm('Also delete in-progress files that are 90%+ complete?');
      addLog('warn', 'Cleaning up played Ad-Free files...');
      const r = await api('/files/cleanup_played', {
        method: 'POST',
        body: JSON.stringify({ include_in_progress: also90 }),
      });
      if (r.error) { addLog('error', 'Cleanup failed: ' + r.error); return; }
      addLog('success', `Deleted ${r.deleted.length}, kept ${r.kept.length}`);
      await loadSubscriptions();
    }

    // ───── Services panel ─────
    let servicesPollTimer = null;
    let servicesState = { services: [], expandedLogs: new Set() };
    let ollamaModelsCache = null;

    async function openServicesPanel() {
      await refreshServices();
      try { ollamaModelsCache = await api('/services/ollama/model'); } catch { ollamaModelsCache = null; }
      renderServicesModal();
      if (servicesPollTimer) clearInterval(servicesPollTimer);
      servicesPollTimer = setInterval(refreshServices, 5000);
    }

    function closeServicesPanel() {
      const m = document.getElementById('services-modal');
      if (m) m.remove();
      if (servicesPollTimer) { clearInterval(servicesPollTimer); servicesPollTimer = null; }
    }

    async function refreshServices() {
      try {
        const d = await api('/services');
        servicesState.services = d.services || [];
        if (document.getElementById('services-modal')) renderServicesModal();
      } catch (e) {
        addLog('error', 'Failed to refresh services: ' + e.message);
      }
    }

    function renderServicesModal() {
      const existing = document.getElementById('services-modal');
      const wasOpenLogs = new Set(servicesState.expandedLogs);
      if (existing) existing.remove();
      const modal = document.createElement('div');
      modal.id = 'services-modal';
      modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.6);z-index:500;display:flex;align-items:center;justify-content:center;padding:20px;';
      const card = document.createElement('div');
      card.style.cssText = 'background:var(--card);border:1px solid var(--border);border-radius:10px;max-width:920px;width:100%;max-height:88vh;overflow:hidden;display:flex;flex-direction:column;';

      const header = `<div style="display:flex;align-items:center;justify-content:space-between;padding:14px 16px;border-bottom:1px solid var(--border)">
        <div>
          <div style="font-weight:600">Services</div>
          <div style="font-size:11px;color:var(--text-muted)">Status auto-refreshes every 5 seconds.</div>
        </div>
        <div style="display:flex;gap:8px">
          <button class="btn small" onclick="refreshServices()">Refresh</button>
          <button class="btn small" onclick="closeServicesPanel()">Close</button>
        </div>
      </div>`;

      let body = '<div style="overflow:auto;flex:1">';
      for (const s of servicesState.services) {
        body += renderServiceRow(s, wasOpenLogs.has(s.id));
      }
      body += '</div>';
      // Whisper backend toggle / Ollama model picker live in a footer bar
      body += renderServicesFooter();

      card.innerHTML = header + body;
      modal.appendChild(card);
      modal.addEventListener('click', e => { if (e.target === modal) closeServicesPanel(); });
      document.body.appendChild(modal);
    }

    const SERVICE_HELP = {
      ollama: {
        purpose: 'Local LLM that classifies transcript windows as ad / non-ad.',
        configures: 'Set OPENAI_MODEL or change at runtime via the picker below.',
        readme: '#ollama--llm-provider',
      },
      whisper: {
        purpose: 'Audio→text transcription. Local Metal binary on Apple Silicon, Docker fallback elsewhere.',
        configures: 'Toggle backend, manage models in whisper.cpp/models/.',
        readme: '#whispercpp--transcription',
      },
      minuspod: {
        purpose: 'Pulls RSS feeds, transcribes, runs ad detection, cuts audio with FFmpeg.',
        configures: 'See start_services.sh for env vars (WINDOW_SIZE_SECONDS, OLLAMA_NUM_PARALLEL, …).',
        readme: '#minuspod-patches',
      },
      ui: {
        purpose: 'This dashboard. Stays online to host the Services panel.',
        configures: 'Restart by relaunching pocketcasts_adfree.py ui from the shell.',
        readme: '#web-ui',
      },
    };

    function renderServiceRow(s, logShown) {
      const dot = s.healthy ? 'up' : (s.running ? 'warn' : 'down');
      const dotTitle = s.healthy ? 'Healthy' : (s.running ? 'Running but unhealthy' : 'Not running');
      const pill = s.backend ? `<span class="svc-pill ${s.backend}">${esc(s.backend)}</span>` : '';
      const port = s.port ? `<span class="svc-port">:${s.port}</span>` : '';
      const meta = renderServiceMeta(s);
      const help = SERVICE_HELP[s.id];
      const purpose = help ? `<div class="svc-purpose">${esc(help.purpose)}</div>` : '';
      const docsLink = help ? `<a class="svc-docs" href="/readme${help.readme}" target="_blank" rel="noopener" title="Open README section">docs</a>` : '';

      const warningBlocks = [];
      if (s.extra && s.extra.warning) warningBlocks.push(`<div class="svc-warn">${esc(s.extra.warning)}</div>`);
      if (!s.healthy && s.id === 'whisper' && s.extra && !s.extra.native_binary_exists) {
        warningBlocks.push(`<div class="svc-warn">Native binary missing. Run <code>scripts/setup_whisper.sh</code>.</div>`);
      }
      if (!s.healthy && s.id === 'minuspod') {
        warningBlocks.push(`<div class="svc-hint">MinusPod is down. Try <strong>Start</strong>; if that fails, check the log for missing models or DB locks.</div>`);
      }
      if (!s.can_start && !s.can_stop && !s.can_restart) {
        warningBlocks.push(`<div class="svc-hint">${esc((s.extra && s.extra.note) || 'No actions available for this service.')}</div>`);
      }

      const rowCls = (s.extra && s.extra.warning) ? 'svc-row has-warn' : 'svc-row';

      let whisperBackendSel = '';
      if (s.id === 'whisper' && (s.can_start || s.can_restart)) {
        whisperBackendSel = `<select id="whisper-backend-sel" title="Backend to use when starting Whisper">
          <option value="native" ${s.backend === 'native' ? 'selected' : ''}>Native (Metal)</option>
          <option value="docker" ${s.backend === 'docker' ? 'selected' : ''}>Docker</option>
        </select>`;
      }

      const startBtn = s.can_start
        ? `<button class="btn small" onclick="serviceAction('${s.id}','start')">Start</button>` : '';
      const stopBtn = s.can_stop
        ? `<button class="btn small danger" onclick="serviceAction('${s.id}','stop')">Stop</button>` : '';
      const restartBtn = s.can_restart
        ? `<button class="btn small" onclick="serviceAction('${s.id}','restart')">Restart</button>` : '';
      const logBtn = s.log_path
        ? `<button class="btn small" onclick="toggleServiceLog('${s.id}')">${logShown ? 'Hide log' : 'Log'}</button>` : '';

      return `<div class="${rowCls}" id="svc-row-${s.id}">
        <div class="svc-name">
          <span class="svc-dot ${dot}" title="${dotTitle}"></span>
          <span class="svc-title">${esc(s.name)}</span>
          ${port}
          ${pill}
          ${docsLink}
        </div>
        <div class="svc-meta">${purpose}${meta}${warningBlocks.join('')}</div>
        <div class="svc-actions">${whisperBackendSel}${startBtn}${restartBtn}${stopBtn}${logBtn}</div>
      </div>
      <pre class="svc-log ${logShown ? 'shown' : ''}" id="svc-log-${s.id}"></pre>`;
    }

    function renderServiceMeta(s) {
      const parts = [];
      if (s.pid) parts.push(`pid ${s.pid}`);
      if (s.id === 'ollama' && s.extra && s.extra.models) {
        parts.push(`${s.extra.models.length} model(s)`);
      }
      if (s.id === 'whisper' && s.extra && s.extra.available_models) {
        parts.push(`${s.extra.available_models.length} model(s)`);
      }
      if (s.id === 'minuspod' && s.extra && s.extra.currentJob) {
        const j = s.extra.currentJob;
        parts.push(`processing: ${esc((j.title || '').slice(0, 50))} (${j.stage || '?'} ${j.progress || 0}%)`);
      }
      if (s.log_path) parts.push(esc(s.log_path));
      return parts.join(' · ');
    }

    function renderServicesFooter() {
      const ollama = servicesState.services.find(s => s.id === 'ollama');
      const models = (ollama && ollama.extra && ollama.extra.models) || [];
      const current = ollamaModelsCache && ollamaModelsCache.current;
      let opts = models.map(m => `<option value="${esc(m)}" ${m === current ? 'selected' : ''}>${esc(m)}</option>`).join('');
      if (!opts) opts = '<option value="">(no models loaded)</option>';
      return `<div style="padding:14px 16px;border-top:1px solid var(--border);display:flex;gap:14px;align-items:center;flex-wrap:wrap">
        <div style="font-weight:600;font-size:12px">MinusPod ad-detection model:</div>
        <select id="ollama-model-sel" style="font-size:12px;padding:4px 10px">${opts}</select>
        <button class="btn small primary" onclick="setOllamaModel()">Apply</button>
        <div style="flex:1"></div>
        <div style="font-size:11px;color:var(--text-muted)">Currently: ${esc(current || 'unknown')}</div>
      </div>`;
    }

    async function serviceAction(serviceId, action) {
      const body = {};
      if (serviceId === 'whisper' && (action === 'start' || action === 'restart')) {
        const sel = document.getElementById('whisper-backend-sel');
        if (sel) body.backend = sel.value;
      }
      addLog('info', `Service: ${action} ${serviceId}${body.backend ? ' (' + body.backend + ')' : ''}…`);
      try {
        const r = await fetch(`/api/services/${serviceId}/${action}`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
        const j = await r.json();
        if (j.ok === false || (!r.ok)) {
          addLog('error', `${serviceId} ${action} failed: ${j.error || 'unknown'}`);
        } else {
          addLog('success', `${serviceId} ${action} ok${j.note ? ' (' + j.note + ')' : ''}`);
        }
      } catch (e) {
        addLog('error', `${serviceId} ${action} failed: ${e.message}`);
      }
      await refreshServices();
    }

    async function toggleServiceLog(serviceId) {
      if (servicesState.expandedLogs.has(serviceId)) {
        servicesState.expandedLogs.delete(serviceId);
      } else {
        servicesState.expandedLogs.add(serviceId);
        try {
          const r = await fetch(`/api/services/${serviceId}/log?lines=200`);
          const j = await r.json();
          const el = document.getElementById('svc-log-' + serviceId);
          if (el) el.textContent = j.exists ? (j.text || '(log file is empty)') : `(log file not found: ${j.log_path || 'n/a'})`;
        } catch (e) {
          const el = document.getElementById('svc-log-' + serviceId);
          if (el) el.textContent = 'Failed to load log: ' + e.message;
        }
      }
      renderServicesModal();
    }

    async function setOllamaModel() {
      const sel = document.getElementById('ollama-model-sel');
      if (!sel || !sel.value) return;
      try {
        const r = await fetch('/api/services/ollama/model', {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ model: sel.value }),
        });
        const j = await r.json();
        if (j.ok) {
          addLog('success', `MinusPod model set to ${sel.value}`);
          ollamaModelsCache = await api('/services/ollama/model');
        } else {
          addLog('error', `Failed to set model: ${j.error || 'unknown'}`);
        }
      } catch (e) { addLog('error', 'Set model failed: ' + e.message); }
      renderServicesModal();
    }

    function renderPodcastGroup(list) {
      return list.map(p => {
        const isPat = p.is_patreon;
        const hasSel = isPodcastSelected(p.uuid);
        const isExp = expandedPodcasts.has(p.uuid);
        const cls = ['podcast-card',
          isPat ? 'patreon' : '',
          hasSel ? 'selected' : '',
          isExp ? 'expanded' : ''
        ].filter(Boolean).join(' ');

        const checkState = headerCheckState(p.uuid);

        let html = `<div class="${cls}" data-uuid="${p.uuid}">
          <div class="podcast-header" onclick="togglePodcast('${p.uuid}')">
            <input type="checkbox" class="podcast-check" aria-label="Select all eligible episodes"
              ${checkState === 'all' ? 'checked' : ''}
              ${checkState === 'some' ? 'data-indeterminate="1"' : ''}
              onclick="event.stopPropagation(); selectAllEpsInPodcast('${p.uuid}', this.checked)">
            <div class="podcast-info">
              <div class="podcast-title">${esc(p.title)}</div>
              <div class="podcast-author">${esc(p.author || '')}</div>
            </div>
            <span class="expand-icon">&#9654;</span>
          </div>`;
        if (isExp) {
          html += renderEpisodeList(p.uuid);
        }
        html += `</div>`;
        return html;
      }).join('');
    }

    function headerCheckState(uuid) {
      const eps = (podcastEpisodes[uuid] || []).filter(e => !e.already_processed);
      if (!eps.length) return 'none';
      const sel = selectedEpisodes[uuid] || new Set();
      const selectable = eps.filter(e => sel.has(e.id));
      if (selectable.length === 0) return 'none';
      if (selectable.length === eps.length) return 'all';
      return 'some';
    }

    async function selectAllEpsInPodcast(uuid, checked) {
      if (!checked) {
        delete selectedEpisodes[uuid];
        renderPodcasts();
        return;
      }

      // Episodes are only fetched when a podcast is expanded. If the user
      // toggled the header checkbox without ever opening the row, our local
      // cache is empty and selecting an empty Set leaves the checkbox stuck
      // unchecked. Expand the row, await the lazy load, then select.
      let eps = (podcastEpisodes[uuid] || []).filter(e => !e.already_processed);
      if (!podcastEpisodes[uuid]) {
        expandedPodcasts.add(uuid);
        // Mark a placeholder so the indeterminate state shows during load.
        selectedEpisodes[uuid] = new Set();
        renderPodcasts();
        try {
          await loadEpisodes(uuid);
        } catch (_) { /* loadEpisodes already resets state on error */ }
        eps = (podcastEpisodes[uuid] || []).filter(e => !e.already_processed);
      }

      if (!eps.length) {
        delete selectedEpisodes[uuid];
      } else {
        selectedEpisodes[uuid] = new Set(eps.map(e => e.id));
      }
      renderPodcasts();
    }

    function renderEpisodeList(uuid) {
      const eps = podcastEpisodes[uuid];
      if (!eps) {
        if (!loadingEpisodes.has(uuid)) loadEpisodes(uuid);
        return `<div class="episode-list"><div class="episodes-loading">Loading episodes...</div></div>`;
      }

      const selSet = selectedEpisodes[uuid] || new Set();
      const processedCount = eps.filter(e => e.already_processed).length;

      let html = `<div class="episode-list">`;
      html += `<div class="ep-toolbar">
        <span class="ep-toolbar-summary">${eps.length} episode${eps.length === 1 ? '' : 's'}${
          processedCount ? ` &middot; <span class="ep-pill processed">${processedCount} processed</span>` : ''
        }</span>
        ${processedCount ? `<button class="btn small" onclick="event.stopPropagation(); resetProcessedForPodcast('${uuid}')" title="Mark these episodes as not yet processed (allows re-processing).">Reset processed</button>` : ''}
      </div>`;

      for (const ep of eps) {
        const status = episodeStatus(uuid, ep);
        const isDone = ep.already_processed;
        const isSel = !isDone && selSet.has(ep.id);
        const dur = ep.duration ? formatDur(ep.duration) : '';
        const date = ep.published ? formatDate(ep.published) : '';
        const itemCls = ['episode-item', 'with-actions',
          isDone ? 'done' : 'selectable',
          isSel ? 'selected' : '',
          status,
        ].filter(Boolean).join(' ');
        const onclick = isDone ? '' : `onclick="event.stopPropagation(); toggleEp('${uuid}','${ep.id}')"`;
        const badge = statusBadge(status, isDone);
        const pcUuid = ep.pc_episode_uuid || '';
        const inQueue = !!ep.in_up_next;
        const isPlayed = status === 'played';
        const actions = pcUuid ? `
          <div class="ep-actions" onclick="event.stopPropagation()">
            <button class="btn small" onclick="toggleEpisodeQueue('${uuid}', '${pcUuid}', ${inQueue}, ${JSON.stringify(ep.title).replace(/"/g,'&quot;')})" title="${inQueue ? 'Remove from Up Next' : 'Add to Up Next'}">${inQueue ? 'Un-queue' : 'Queue'}</button>
            <button class="btn small" onclick="toggleEpisodePlayed('${uuid}', '${pcUuid}', ${!isPlayed})" title="${isPlayed ? 'Mark unplayed' : 'Mark played'}">${isPlayed ? 'Mark unplayed' : 'Mark played'}</button>
          </div>` : '';
        html += `<div class="${itemCls}" ${onclick}>
          <div class="ep-check"></div>
          <div class="ep-title" title="${esc(ep.title)}">${esc(ep.title)}</div>
          ${badge}
          <div class="ep-meta">${dur}</div>
          <div class="ep-meta">${date}</div>
          ${actions}
        </div>`;
      }
      html += `</div>`;
      return html;
    }

    async function toggleEpisodeQueue(podcastUuid, pcEpisodeUuid, currentlyQueued, title) {
      const path = `/pc_episode/${pcEpisodeUuid}/up_next`;
      const r = currentlyQueued
        ? await api(path, { method: 'DELETE' })
        : await api(path, { method: 'POST', body: JSON.stringify({ podcast_uuid: podcastUuid, title }) });
      if (r.ok) {
        addLog('info', currentlyQueued ? 'Removed from Up Next' : 'Added to Up Next');
        podcastEpisodes[podcastUuid] = null;
        await loadEpisodes(podcastUuid);
        await loadSubscriptions();
      } else {
        addLog('error', 'Queue action failed: ' + (r.error || ''));
      }
    }

    async function toggleEpisodePlayed(podcastUuid, pcEpisodeUuid, played) {
      const r = await api(`/pc_episode/${pcEpisodeUuid}/played`, {
        method: 'POST',
        body: JSON.stringify({ podcast_uuid: podcastUuid, played }),
      });
      if (r.ok) {
        addLog('info', played ? 'Marked played' : 'Marked unplayed');
        podcastEpisodes[podcastUuid] = null;
        // Refresh both the cached per-podcast episode list AND the Up Next
        // summary, since marking played can also evict the episode from the
        // queue.
        loadEpisodes(podcastUuid);
        await loadSubscriptions();
      } else {
        addLog('error', 'Status update failed: ' + (r.error || ''));
      }
    }

    function episodeStatus(podUuid, ep) {
      if (ep.already_processed) return 'processed';
      // Prefer per-episode status from /api/episodes (comes from PC's
      // get_podcast_episodes), fall back to the lighter new-releases feed
      // which only covers the last ~2 weeks.
      if (ep.pc_playing_status === 3) return 'played';
      if (ep.pc_playing_status === 2) return 'in-progress';
      const status = (pcEpisodeStatus[podUuid] || {})[ep.title];
      if (status === 3) return 'played';
      if (status === 2) return 'in-progress';
      if (ep.pc_archived || ep.archived) return 'archived';
      return 'unplayed';
    }

    function statusBadge(status, isDone) {
      if (isDone) return `<span class="ep-badge processed">processed</span>`;
      if (status === 'in-progress') return `<span class="ep-badge in-progress">in progress</span>`;
      if (status === 'played') return `<span class="ep-badge played">played</span>`;
      if (status === 'archived') return `<span class="ep-badge archived">archived</span>`;
      return `<span class="ep-badge unplayed">unplayed</span>`;
    }

    async function resetProcessedForPodcast(uuid) {
      if (!confirm('Reset processed markers for this podcast?\n\nEpisodes will become eligible for processing again. Files already uploaded to Pocket Casts are not deleted.')) return;
      const r = await api('/processed/podcast/' + encodeURIComponent(uuid), { method: 'DELETE' });
      if (r.error) { addLog('error', 'Reset failed: ' + r.error); return; }
      addLog('info', `Reset ${r.cleared} processed marker${r.cleared === 1 ? '' : 's'} for this podcast`);
      podcastEpisodes[uuid] = null;
      await loadEpisodes(uuid);
      await loadSubscriptions();
    }

    function formatDate(dateStr) {
      if (!dateStr) return '';
      try {
        const d = new Date(dateStr);
        if (isNaN(d.getTime()) || d.getFullYear() < 2000) return '';
        return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
      } catch { return dateStr.slice(0, 10); }
    }

    async function loadEpisodes(uuid) {
      loadingEpisodes.add(uuid);
      try {
        const d = await api('/episodes/' + uuid);
        podcastEpisodes[uuid] = d.episodes || [];
        renderPodcasts();
      } catch(e) {
        podcastEpisodes[uuid] = [];
        renderPodcasts();
      } finally {
        loadingEpisodes.delete(uuid);
      }
    }

    function togglePodcast(uuid, realUuid) {
      const lookupUuid = realUuid || uuid;
      const p = podcasts.find(x => x.uuid === lookupUuid);

      if (expandedPodcasts.has(uuid)) {
        expandedPodcasts.delete(uuid);
      } else {
        expandedPodcasts.add(uuid);
      }
      // For Up Next items, we use 'upnext-{uuid}' as the expand key
      // but load episodes using the real podcast UUID
      if (realUuid && !podcastEpisodes[realUuid] && !loadingEpisodes.has(realUuid)) {
        loadEpisodes(realUuid);
      }
      renderPodcasts();
    }

    function toggleEp(uuid, epId) {
      if (uuid === '_files') {
        if (!selectedEpisodes['_files']) selectedEpisodes['_files'] = new Set();
        const s = selectedEpisodes['_files'];
        s.has(epId) ? s.delete(epId) : s.add(epId);
        renderPodcasts();
        return;
      }
      const eps = podcastEpisodes[uuid] || [];
      const ep = eps.find(e => e.id === epId);
      if (ep && ep.already_processed) return;
      if (!selectedEpisodes[uuid]) selectedEpisodes[uuid] = new Set();
      const s = selectedEpisodes[uuid];
      s.has(epId) ? s.delete(epId) : s.add(epId);
      renderPodcasts();
    }

    function toggleSelectAll() {
      const all = el('select-all').checked;
      selectedEpisodes = {};
      if (all) {
        for (const p of podcasts) {

          expandedPodcasts.add(p.uuid);
        }
        renderPodcasts();
        addLog('info', 'Expand each podcast and select episodes to process.');
      } else {
        renderPodcasts();
      }
    }

    function filterPodcasts() { renderPodcasts(); }

    function updateProcessBtn() {
      const count = getSelectedCount();
      const btn = el('btn-process');
      btn.disabled = count === 0;
      if (activeJobId) {
        btn.textContent = count ? `Queue ${count} More` : 'Queue More';
      } else {
        btn.textContent = count ? `Process ${count} Episode${count > 1 ? 's' : ''}` : 'Process Selected';
      }
    }

    async function processSelected() {
      const count = getSelectedCount();
      if (!count) return;
      const btn = el('btn-process');
      btn.disabled = true; btn.textContent = 'Starting...';

      const selections = {};
      for (const uuid in selectedEpisodes) {
        if (selectedEpisodes[uuid].size <= 0) continue;
        if (uuid === '_files') {
          selections['_files'] = [...selectedEpisodes['_files']];
          continue;
        }
        const eps = podcastEpisodes[uuid] || [];
        const keep = [...selectedEpisodes[uuid]].filter(epId => {
          const ep = eps.find(e => e.id === epId);
          // Keep entries that matched a non-processed MinusPod episode,
          // AND entries that did not match at all (Up Next items that
          // MinusPod doesn't know about yet — backend resolves via
          // podcast RSS / title / source URL).
          return !ep || !ep.already_processed;
        });
        if (keep.length > 0) selections[uuid] = keep;
      }

      if (!Object.keys(selections).length) {
        addLog('warn', 'All selected episodes are already processed.');
        updateProcessBtn();
        return;
      }

      try {
        const d = await api('/process', { method: 'POST', body: JSON.stringify({ selections }) });
        const jobId = d.job_id;
        if (activeJobId) {
          addLog('info', `Queued ${count} more episodes (job: ${jobId.slice(0,8)})`);
        } else {
          addLog('info', `Started processing ${count} episodes (job: ${jobId.slice(0,8)})`);
          activeJobId = jobId;
          lastLogCursor = 0;
          showJobControls(true);
          startPolling();
        }
        selectedEpisodes = {};
        renderPodcasts();
        updateGlobalProgress();
        el('log-panel').classList.remove('collapsed');
      } catch(e) {
        addLog('error', 'Failed to start: ' + e.message);
        updateProcessBtn();
      }
    }

    function showJobControls(show) {
      el('btn-skip').classList.toggle('hidden', !show);
      el('btn-stop').classList.toggle('hidden', !show);
    }

    async function skipEpisode() {
      if (!activeJobId) return;
      try {
        await api('/job/' + activeJobId + '/skip', { method: 'POST' });
        addLog('warn', 'Skipping current episode...');
      } catch(e) { addLog('error', 'Skip failed: ' + e.message); }
    }

    async function stopJob() {
      if (!activeJobId) return;
      try {
        await api('/job/' + activeJobId + '/stop', { method: 'POST' });
        addLog('warn', 'Stopping job...');
      } catch(e) { addLog('error', 'Stop failed: ' + e.message); }
    }

    function startPolling() {
      if (pollTimer) clearInterval(pollTimer);
      pollTimer = setInterval(pollJobs, 3000);
    }

    async function pollJobs() {
      try {
        const d = await api('/queue/status');
        const active = d.active_job;
        const queuedEps = d.queued_episodes || 0;
        const gp = el('global-progress');

        if (active) {
          gp.classList.add('active');
          el('progress-stage').textContent = active.current_episode || 'Processing...';
          const totalAll = active.total_episodes + queuedEps;
          const pct = totalAll > 0
            ? Math.round((active.processed / totalAll) * 100) : 0;
          el('progress-bar').style.width = pct + '%';
          let label = `${active.processed}/${totalAll} episodes`;
          el('progress-label').textContent = label;

          // Switch to new active job — reset log cursor
          if (activeJobId !== active.job_id) {
            activeJobId = active.job_id;
            lastLogCursor = 0;
          }
        }

        const qb = el('queue-badge');
        if (queuedEps > 0) { qb.classList.remove('hidden'); qb.textContent = `+${queuedEps} queued`; }
        else { qb.classList.add('hidden'); }

        if (activeJobId) {
          const jd = await api('/job/' + activeJobId + '?cursor=' + lastLogCursor);
          if (jd.new_logs && jd.new_logs.length > 0) {
            jd.new_logs.forEach(l => addLog(l.level, l.msg));
            lastLogCursor += jd.new_logs.length;
          }
          if (jd.status === 'completed' || jd.status === 'failed' || jd.status === 'stopped') {
            const label = jd.status === 'stopped' ? 'stopped by user' : jd.status;
            addLog(jd.status === 'completed' ? 'success' : 'warn',
              `Job ${label}. Processed: ${jd.processed || 0}, Uploaded: ${jd.uploaded || 0}`);

            // Check if there's a new active job
            if (d.active_job && d.active_job.job_id !== activeJobId) {
              activeJobId = d.active_job.job_id;
              lastLogCursor = 0;
              addLog('info', `Starting next queued job (${activeJobId.slice(0,8)})`);
            } else if (!d.active_job) {
              clearInterval(pollTimer); pollTimer = null;
              gp.classList.remove('active');
              loadSubscriptions();
              showJobControls(false);
              activeJobId = null;
            }
          }
        }
        updateProcessBtn();
      } catch {}
    }

    function updateGlobalProgress() {
      el('global-progress').classList.add('active');
      el('progress-stage').textContent = 'Starting...';
      el('progress-bar').style.width = '0%';
    }

    function addLog(level, msg) {
      const body = el('log-body');
      const ts = new Date().toLocaleTimeString();
      const icons = {
        success: '\u2705', error: '\u274c', warn: '\u26a0\ufe0f', info: '\u2139\ufe0f',
        stage: '\u2699\ufe0f', download: '\u2b07\ufe0f', upload: '\u2b06\ufe0f'
      };
      let cls = level;
      let icon = icons[level] || '';
      const m = esc(msg);
      if (m.includes('Downloading') || m.includes('Downloaded')) { cls = 'download'; icon = icons.download; }
      else if (m.includes('Upload') || m.includes('Syncing')) { cls = 'upload'; icon = icons.upload; }
      else if (m.includes('Processing:') || m.includes('Starting:')) { cls = 'stage'; icon = icons.stage; }
      else if (m.includes('transcript') || m.includes('Whisper')) { cls = 'info'; icon = '\ud83d\udcdd'; }
      else if (m.includes('artwork') || m.includes('image')) { cls = 'info'; icon = '\ud83c\udfa8'; }
      else if (m.includes('Unloading') || m.includes('memory')) { cls = 'info'; icon = '\ud83e\uddf9'; }
      else if (m.includes('RSS')) { cls = 'info'; icon = '\ud83d\udce1'; }
      body.innerHTML += `<div class="log-line ${cls}"><span class="log-ts">${ts}</span><span class="log-icon">${icon}</span>${m}</div>`;
      body.scrollTop = body.scrollHeight;
      // New log entries always open the panel so the user sees progress,
      // errors, and Whisper/LLM messages without hunting for the toggle.
      const panel = el('log-panel');
      if (panel.classList.contains('collapsed')) {
        panel.classList.remove('collapsed');
        el('log-unread').classList.remove('has-new');
      }
    }
    function clearLog() { el('log-body').innerHTML = ''; }

    function formatDur(s) {
      const m = Math.floor(s / 60);
      return m >= 60 ? `${Math.floor(m/60)}h${m%60}m` : `${m}m`;
    }

    function el(id) { return document.getElementById(id); }
    function esc(s) { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }

    function showView(name) {
      currentView = name;
      el('view-dashboard').classList.toggle('hidden', name !== 'dashboard');
      el('view-history').classList.toggle('hidden', name !== 'history');
      const navD = el('nav-dashboard'); if (navD) navD.classList.toggle('active', name === 'dashboard');
      const navH = el('nav-history'); if (navH) navH.classList.toggle('active', name === 'history');
      if (name === 'history' && historyEntries === null) loadHistory();
    }

    async function loadHistory() {
      try {
        const d = await api('/history');
        historyEntries = d.entries || [];
        renderHistory();
      } catch (e) {
        el('history-rows').innerHTML = `<tr><td colspan="5" class="empty-state-cell">Failed to load history: ${esc(e.message)}</td></tr>`;
      }
    }

    function renderHistory() {
      if (!historyEntries) return;
      const q = (el('history-search').value || '').toLowerCase();
      const sort = el('history-sort').value;
      let rows = historyEntries.filter(e =>
        !q || (e.title || '').toLowerCase().includes(q) || (e.podcast_title || '').toLowerCase().includes(q)
      );
      const cmp = {
        processed_desc: (a,b) => (b.processed_at || '').localeCompare(a.processed_at || ''),
        processed_asc:  (a,b) => (a.processed_at || '').localeCompare(b.processed_at || ''),
        time_saved_desc: (a,b) => (b.time_saved_secs || 0) - (a.time_saved_secs || 0),
        ads_desc: (a,b) => (b.ads_removed || 0) - (a.ads_removed || 0),
      }[sort] || (() => 0);
      rows.sort(cmp);

      const totalSaved = historyEntries.reduce((s, e) => s + (e.time_saved_secs || 0), 0);
      const totalAds = historyEntries.reduce((s, e) => s + (e.ads_removed || 0), 0);
      el('history-summary').innerHTML = `
        <div class="history-stat"><div class="history-stat-label">Episodes processed</div><div class="history-stat-value">${historyEntries.length}</div></div>
        <div class="history-stat"><div class="history-stat-label">Ads removed</div><div class="history-stat-value">${totalAds}</div></div>
        <div class="history-stat"><div class="history-stat-label">Time saved</div><div class="history-stat-value">${formatDuration(totalSaved)}</div></div>
      `;

      if (!rows.length) {
        el('history-rows').innerHTML = `<tr><td colspan="5" class="empty-state-cell">No matching entries.</td></tr>`;
        return;
      }
      el('history-rows').innerHTML = rows.map(r => {
        const dt = r.processed_at ? new Date(r.processed_at) : null;
        const dateStr = dt ? dt.toLocaleString('en-US', { month:'short', day:'numeric', year:'numeric', hour:'numeric', minute:'2-digit' }) : '—';
        return `<tr>
          <td>${dateStr}</td>
          <td title="${esc(r.title || '')}">${esc(r.title || '')}</td>
          <td>${esc(r.podcast_title || '')}</td>
          <td class="num">${r.ads_removed != null ? r.ads_removed : '—'}</td>
          <td class="num">${r.time_saved_secs != null ? formatDuration(r.time_saved_secs) : '—'}</td>
        </tr>`;
      }).join('');
    }

    function downloadHistoryCsv() {
      if (!historyEntries || !historyEntries.length) { addLog('warn', 'No history to export.'); return; }
      const headers = ['processed_at','title','podcast_title','ads_removed','time_saved_secs'];
      const escCsv = v => `"${String(v == null ? '' : v).replace(/"/g, '""')}"`;
      const lines = [headers.join(',')].concat(
        historyEntries.map(r => headers.map(h => escCsv(r[h])).join(','))
      );
      const blob = new Blob([lines.join('\n')], { type: 'text/csv' });
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = 'adfree-history.csv';
      a.click();
      URL.revokeObjectURL(a.href);
    }

    function formatBytes(n) {
      if (n == null || isNaN(n)) return '—';
      if (n < 1024) return n + ' B';
      if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
      if (n < 1024 * 1024 * 1024) return (n / (1024 * 1024)).toFixed(1) + ' MB';
      return (n / (1024 * 1024 * 1024)).toFixed(2) + ' GB';
    }
    function formatDuration(secs) {
      if (secs == null || isNaN(secs)) return '—';
      secs = Math.round(secs);
      const h = Math.floor(secs / 3600);
      const m = Math.floor((secs % 3600) / 60);
      const s = secs % 60;
      if (h) return `${h}h ${m}m`;
      if (m) return `${m}m ${s}s`;
      return `${s}s`;
    }

    const _initialView = new URLSearchParams(location.search).get('view');
    if (_initialView === 'history') showView('history');

    checkStatus();
    loadSubscriptions();
    setInterval(checkStatus, 15000);

    // Auto-refresh the dashboard so processed uploads, newly queued episodes,
    // and reconciled-out originals show up without the user clicking anything.
    // Pause while the user is mid-edit (has selected episodes or just ran an
    // action) to avoid clobbering their work.
    setInterval(() => {
      if (document.hidden) return;
      if (currentView !== 'dashboard') return;
      if (getSelectedCount() > 0) return;
      loadSubscriptions();
    }, 20000);
