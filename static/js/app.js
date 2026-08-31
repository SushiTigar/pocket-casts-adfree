let podcasts = [];
    let upNextEpisodes = [];
    let pcEpisodeStatus = {};
    let selectedEpisodes = {};
    let expandedPodcasts = new Set();
    let podcastEpisodes = {};
    let loadingEpisodes = new Set();
    let loadEpisodesErrors = new Set();
    const loadEpisodesErrorMsg = new Map();
    let lastMinusPodHealthy = null;
    let activeJobId = null;
    let pollTimer = null;
    let lastLogCursor = 0;
    let statFilter = null;
    let processedPodcastUuids = new Set();
    let uploadedFiles = [];
    let serviceHealth = { minuspod: false, pocketcasts: false };
    let currentView = 'dashboard';
    let historyEntries = null;
    let logForceOpen = false;
    let logUnreadCount = 0;

    function el(id) { return document.getElementById(id); }

    function toast(message, type = 'info', duration = 4000) {
      const container = el('toast-container');
      if (!container) return;
      const node = document.createElement('div');
      node.className = 'toast ' + type;
      node.textContent = message;
      container.appendChild(node);
      setTimeout(() => {
        node.style.opacity = '0';
        node.style.transition = 'opacity 0.2s';
        setTimeout(() => node.remove(), 200);
      }, duration);
    }

    function confirmDialog(opts) {
      return new Promise(resolve => {
        const dlg = document.createElement('dialog');
        dlg.className = 'dialog';
        const fieldsHtml = (opts.fields || []).map(f => {
          if (f.type === 'checkbox') {
            return `<label class="tunable-row"><input type="checkbox" id="dlg-${f.id}" ${f.checked ? 'checked' : ''}><span>${esc(f.label)}</span></label>`;
          }
          return '';
        }).join('');
        dlg.innerHTML = `<div class="dialog-card">
          <div class="dialog-header">
            <div><div class="dialog-title">${esc(opts.title || 'Confirm')}</div>
            ${opts.message ? `<div class="dialog-subtitle">${opts.message}</div>` : ''}</div>
            <div class="dialog-actions">
              <button class="btn sm" type="button" data-action="cancel">${esc(opts.cancelLabel || 'Cancel')}</button>
              <button class="btn sm ${opts.danger ? 'danger' : 'primary'}" type="button" data-action="confirm">${esc(opts.confirmLabel || 'Confirm')}</button>
            </div>
          </div>
          ${fieldsHtml ? `<div class="dialog-body">${fieldsHtml}</div>` : ''}
        </div>`;
        const close = (confirmed) => {
          const values = {};
          (opts.fields || []).forEach(f => {
            const input = dlg.querySelector('#dlg-' + f.id);
            if (input) values[f.id] = input.type === 'checkbox' ? input.checked : input.value;
          });
          dlg.close();
          dlg.remove();
          resolve({ confirmed, values });
        };
        dlg.querySelector('[data-action="cancel"]').onclick = () => close(false);
        dlg.querySelector('[data-action="confirm"]').onclick = () => close(true);
        dlg.addEventListener('cancel', e => { e.preventDefault(); close(false); });
        document.body.appendChild(dlg);
        dlg.showModal();
      });
    }

    function showShutdownScreen() {
      document.body.innerHTML = `<div class="shutdown-screen"><h1>UI server shut down</h1><p>All background services have been stopped. You can close this tab now.</p></div>`;
    }

    function toggleSettingsMenu(e) {
      e.stopPropagation();
      const menu = el('settings-menu');
      const trigger = el('menu-trigger');
      const isHidden = menu.classList.toggle('hidden');
      trigger.setAttribute('aria-expanded', isHidden ? 'false' : 'true');
    }

    function closeSettingsMenu() {
      const menu = el('settings-menu');
      const trigger = el('menu-trigger');
      if (menu) menu.classList.add('hidden');
      if (trigger) trigger.setAttribute('aria-expanded', 'false');
    }

    document.addEventListener('click', () => closeSettingsMenu());

    function clearSelection() {
      selectedEpisodes = {};
      const selectAll = el('select-all');
      if (selectAll) selectAll.checked = false;
      renderPodcasts();
    }

    function updateSegmentCounts(d) {
      const set = (id, val) => { const n = el(id); if (n) n.textContent = val ?? '-'; };
      set('count-all', d.total);
      set('count-eligible', d.eligible);
      set('count-patreon', d.patreon);
      set('count-processed', d.processed_count);
      document.querySelectorAll('.segment').forEach(s => s.classList.remove('active'));
      const activeId = statFilter ? 'filter-' + statFilter : 'filter-all';
      const active = el(activeId);
      if (active) active.classList.add('active');
    }

    function updateStatusChip(services, memory, llmProvider) {
      const chip = el('status-chip');
      const text = el('status-chip-text');
      const dot = el('status-dot');
      if (!chip || !text) return;
      const relevant = (services || []).filter(s => s.id !== 'ui' && !(s.id === 'ollama' && llmProvider !== 'ollama'));
      const running = relevant.filter(s => s.healthy).length;
      const anyRunning = relevant.some(s => s.running);
      chip.classList.remove('all-up', 'partial', 'down');
      if (running === relevant.length && relevant.length > 0) chip.classList.add('all-up');
      else if (anyRunning) chip.classList.add('partial');
      else chip.classList.add('down');
      let ramPart = '';
      if (memory && typeof memory.available_gb !== 'undefined') {
        ramPart = ` · ${memory.available_gb.toFixed(1)} GB free`;
      }
      text.textContent = `${running}/${relevant.length} services${ramPart}`;
    }

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
        logUnreadCount = 0;
        el('log-body').scrollTop = el('log-body').scrollHeight;
      }
    }

    function toggleServicesBar() {
      const bar = el('services-quick-bar');
      const collapsed = bar.classList.toggle('collapsed');
      localStorage.setItem('sq-collapsed', collapsed ? '1' : '0');
    }
    (function initServicesBar() {
      if (localStorage.getItem('sq-collapsed') === '1') {
        const bar = el('services-quick-bar');
        if (bar) bar.classList.add('collapsed');
      }
    })();

    // Custom error type so callers can branch on Pocket Casts auth failures
    // and render the human-readable hint we attach server-side, instead of
    // the old generic "JSON.parse: unexpected character" surfaced when the
    // server returned an HTML 500 page.
    class ApiError extends Error {
      constructor(status, body) {
        super(body && body.message ? body.message : ('HTTP ' + status));
        this.status = status;
        this.body = body || {};
      }
    }

    async function api(path, opts = {}) {
      const resp = await fetch('/api' + path, {
        headers: { 'Content-Type': 'application/json' }, ...opts
      });
      const ctype = (resp.headers.get('content-type') || '').toLowerCase();
      // Be defensive: even on non-200, try to parse JSON so we surface the
      // structured `{error, message, hint}` shape the backend now returns
      // for known failure modes (Pocket Casts auth, MinusPod down, etc.).
      let body = null;
      if (ctype.includes('application/json')) {
        try { body = await resp.json(); } catch (_) { body = null; }
      } else {
        // Non-JSON response (Werkzeug HTML 500, e.g.). Read a snippet so
        // the error message has *something* useful in it.
        try {
          const text = await resp.text();
          body = { message: 'Server returned ' + (ctype || 'unknown') + ': ' + text.slice(0, 200) };
        } catch (_) { body = null; }
      }
      if (!resp.ok) {
        throw new ApiError(resp.status, body);
      }
      return body || {};
    }

    function renderAuthBanner(err) {
      let banner = document.getElementById('pc-auth-banner');
      if (!banner) {
        banner = document.createElement('div');
        banner.id = 'pc-auth-banner';
        banner.className = 'banner error';
        const controls = document.querySelector('.dashboard-controls');
        if (controls && controls.parentNode) {
          controls.parentNode.insertBefore(banner, controls);
        }
      }
      const hint = (err.body && err.body.hint) || '';
      const msg = (err.body && err.body.message) || err.message || 'Pocket Casts auth failed.';
      banner.innerHTML =
        '<strong>Pocket Casts authentication failed</strong>' +
        '<div>' + escapeHtml(msg) + '</div>' +
        (hint ? ('<div><em>' + escapeHtml(hint) + '</em></div>') : '');
    }

    function clearAuthBanner() {
      const banner = document.getElementById('pc-auth-banner');
      if (banner && banner.parentNode) banner.parentNode.removeChild(banner);
    }

    function escapeHtml(s) {
      return String(s).replace(/[&<>"']/g, c => ({
        '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
      }[c]));
    }

    async function checkStatus() {
      try {
        const d = await api('/status');
        const minusPodUp = !!d.minuspod;
        if (lastMinusPodHealthy === false && minusPodUp && loadEpisodesErrors.size > 0) {
          for (const uuid of [...loadEpisodesErrors]) {
            if (expandedPodcasts.has(uuid) || expandedPodcasts.has(`upnext-${uuid}`)) {
              loadEpisodesErrors.delete(uuid);
              loadEpisodesErrorMsg.delete(uuid);
              loadEpisodes(uuid);
            }
          }
        }
        lastMinusPodHealthy = minusPodUp;
        serviceHealth = { minuspod: minusPodUp, pocketcasts: !!d.pocketcasts };
        if (d.pocketcasts_error) {
          renderAuthBanner({ body: d.pocketcasts_error, message: d.pocketcasts_error.message });
        } else if (d.pocketcasts) {
          clearAuthBanner();
        }
      } catch { }

      // Update status chip in app bar
      try {
        const svcData = await api('/services');
        const services = svcData.services || [];
        const memory = svcData.memory || {};
        const llmProvider = svcData.llm_provider || 'ollama';
        servicesState.services = services;
        servicesState.memory = memory;
        servicesState.llm_provider = llmProvider;
        updateStatusChip(services, memory, llmProvider);
      } catch (e) {
        console.error('Failed to update status chip:', e);
      }
    }

    // Set up warning when leaving page if services are running
    window.addEventListener('beforeunload', (event) => {
      const anyRunning = servicesState.services.some(s => s.running && s.id !== 'ui');
      if (anyRunning) {
        const message = 'Reminder: Background services (Ollama/Whisper/MinusPod) are still running. Please stop them to free system RAM.';
        event.returnValue = message;
        return message;
      }
    });

    async function startAllServices() {
      const btn = el('btn-start-all');
      if (btn) { btn.disabled = true; btn.textContent = 'Starting...'; }
      addLog('info', 'Starting background services in the background...');
      try {
        const res = await api('/services/all/start', { method: 'POST' });
        if (res.ok) {
          addLog('info', 'Startup initiated. Services are starting (this may take ~60s for MinusPod)...');
          let pings = 0;
          const pingInterval = setInterval(async () => {
            await checkStatus();
            pings++;
            if (pings >= 20) clearInterval(pingInterval);
          }, 5000);
        } else {
          addLog('error', 'Failed to start: ' + (res.error || 'unknown error'));
        }
      } catch (e) {
        addLog('error', 'Start request failed: ' + e.message);
      } finally {
        if (btn) { btn.disabled = false; btn.textContent = 'Start All'; }
        await checkStatus();
      }
    }

    async function stopAllServices() {
      const btn = el('btn-stop-all');
      if (btn) { btn.disabled = true; btn.textContent = 'Stopping...'; }
      addLog('info', 'Stopping background services...');
      try {
        const res = await api('/services/all/stop', { method: 'POST' });
        if (res.ok) {
          addLog('success', 'Shutdown initiated.');
          let pings = 0;
          const pingInterval = setInterval(async () => {
            await checkStatus();
            pings++;
            if (pings >= 10) clearInterval(pingInterval);
          }, 3000);
        } else {
          addLog('error', 'Failed to stop: ' + (res.error || 'unknown error'));
        }
      } catch (e) {
        addLog('error', 'Stop request failed: ' + e.message);
      } finally {
        if (btn) { btn.disabled = false; btn.textContent = 'Stop All'; }
        await checkStatus();
      }
    }

    async function shutdownUI() {
      const { confirmed } = await confirmDialog({
        title: 'Shut down UI server',
        message: 'Stop all backend services and shut down this web UI? You will need to restart it from the terminal to access it again.',
        confirmLabel: 'Shut down',
        danger: true,
      });
      if (!confirmed) return;

      const btn = el('btn-shutdown-ui');
      if (btn) { btn.disabled = true; btn.textContent = 'Shutting down...'; }
      addLog('warn', 'Shutting down UI and all backend services...', { forceOpen: true });
      try {
        const res = await api('/shutdown', { method: 'POST' });
        if (res.ok) {
          addLog('success', 'UI and all services are shutting down. You can close this tab now.');
          toast('Shutting down. You can close this tab.', 'info', 8000);
          showShutdownScreen();
        } else {
          addLog('error', 'Failed to shut down UI: ' + (res.error || 'unknown error'));
        }
      } catch (e) {
        addLog('success', 'Shutdown request sent. Web server is stopping.');
        showShutdownScreen();
      }
    }

    window.startAllServices = startAllServices;
    window.stopAllServices = stopAllServices;
    window.shutdownUI = shutdownUI;
    window.clearSelection = clearSelection;
    window.toggleSettingsMenu = toggleSettingsMenu;
    window.closeSettingsMenu = closeSettingsMenu;




    function thumbInitial(p) {
      return (p.title || '?').charAt(0).toUpperCase();
    }

    function thumbHTML(p) {
      const initial = thumbInitial(p);
      if (!p.thumbnail) {
        return `<span class="podcast-thumb-initial">${esc(initial)}</span>`;
      }
      // Inline onerror restores the letter on load failure (iTunes 404,
      // geo-block, CORS, etc.) so the 48x48 slot never ends up blank.
      return `<img src="${esc(p.thumbnail)}" alt="" loading="lazy" data-initial="${esc(initial)}"
        onerror="this.parentNode.classList.add('thumb-missing');this.outerHTML='<span class=\\'podcast-thumb-initial\\'>'+this.dataset.initial+'</span>';">`;
    }

    async function fetchArtworkFor(p) {
      try {
        const url = '/podcast_artwork/' + encodeURIComponent(p.uuid);
        const d = await api(url);
        if (d && d.url && !p.thumbnail) {
          p.thumbnail = d.url;
          swapPodcastThumb(p);
        } else if (d && d.url && p.thumbnail === d.url) {
          // already loaded — nothing to do
        } else if (d && !d.url) {
          console.log(`[artwork] no iTunes result for "${p.title}" (${p.uuid})`);
        } else if (!d) {
          console.log(`[artwork] endpoint returned empty for ${p.uuid}`);
        }
      } catch (e) {
        // A 404 here is the strongest signal that the user is running an
        // old UI server (predates the /api/podcast_artwork endpoint). Log
        // it loudly so it's easy to spot in DevTools.
        if (e && e.status === 404) {
          console.error(`[artwork] /api/podcast_artwork/${p.uuid} returned 404 — ` +
            'your UI server is older than commit 3724bf9. Restart it (Ctrl+C, ' +
            'then `python -m pocketcasts_adfree` again) to pick up the endpoint.');
        } else {
          console.log(`[artwork] lookup failed for "${p.title}": ${e && e.message || e}`);
        }
      }
    }

    function fetchMissingArtworks() {
      if (!Array.isArray(podcasts)) return;
      // Fire in parallel; each swap is idempotent and tiny. Sequential would
      // add ~200ms per podcast of perceived delay; this finishes in one
      // round-trip to iTunes for all of them.
      podcasts.filter(p => !p.thumbnail).forEach(fetchArtworkFor);
    }

    function swapPodcastThumb(p) {
      const card = document.querySelector(`.podcast-card[data-uuid="${esc(p.uuid)}"] .podcast-thumb`);
      if (!card) return;
      card.classList.remove('thumb-missing');
      card.innerHTML = thumbHTML(p);
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
        updateSegmentCounts(d);
        clearAuthBanner();
        renderPodcasts();
        fetchMissingArtworks();
      } catch(e) {
        if (e instanceof ApiError && e.body && e.body.error === 'pocketcasts_auth_failed') {
          renderAuthBanner(e);
          el('podcast-list').innerHTML =
            '<div class="empty-state"><h3>Pocket Casts auth failed</h3>' +
            '<p>Fix the credentials in <code>.env</code> and restart the UI.</p></div>';
          return;
        }
        el('podcast-list').innerHTML = '<div class="empty-state"><h3>Error loading</h3><p>' + escapeHtml(e.message) + '</p></div>';
      }
    }

    async function loadUploadedFiles() {
      try {
        const d = await api('/files');
        uploadedFiles = d.files || [];
      } catch(e) { uploadedFiles = []; }
    }

    function setStatFilter(filter) {
      if (statFilter === filter || (filter === 'all' && !statFilter)) {
        statFilter = null;
      } else {
        statFilter = filter === 'all' ? null : filter;
      }
      document.querySelectorAll('.segment').forEach(s => s.classList.remove('active'));
      const activeId = statFilter ? 'filter-' + statFilter : 'filter-all';
      const active = el(activeId);
      if (active) active.classList.add('active');
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
            const podData = podcasts.find(x => x.uuid === puuid);
            const podTitle = getPodcastTitle(puuid);
            const isExp = expandedPodcasts.has(`upnext-${puuid}`);
            const hasSel = isPodcastSelected(puuid);
            const isPat = podData?.is_patreon;
            const allAdFree = eps.every(e => e.title.includes('(Ad-Free)'));
            const cls = ['podcast-card',
              hasSel ? 'selected' : '',
              isExp ? 'expanded' : '',
              isPat ? 'patreon' : '',
              allAdFree ? 'processed' : ''
            ].filter(Boolean).join(' ');
            // For Up Next, podData may be undefined if the user has an episode
            // queued for a podcast they've since unsubscribed from. Fall back
            // to a synthesized object so thumbHTML() still renders the title
            // initial.
            const thumbObj = podData || { uuid: puuid, title: podTitle, thumbnail: '' };
            html += `<div class="${cls} compact" data-uuid="${esc(puuid)}">
              <div class="podcast-header" onclick="togglePodcast('upnext-${puuid}', '${puuid}')" aria-expanded="${isExp}">
                <span class="badge up-next-badge">Up next</span>
                <div class="podcast-thumb" aria-hidden="true">${thumbHTML(thumbObj)}</div>
                <div class="podcast-info">
                  <div class="podcast-title">${esc(podTitle)}</div>
                  <div class="podcast-author">${eps.length} episode${eps.length > 1 ? 's' : ''} in queue</div>
                </div>
                <span class="section-count">${eps.length}</span>
                ${!allAdFree ? '<span class="expand-icon">&#9654;</span>' : ''}
              </div>`;
            if (isExp && !allAdFree) {
              html += `<div class="episode-list compact">`;
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
            html += `<div class="podcast-card compact ${isExp ? 'expanded' : ''}">
              <div class="podcast-header" onclick="toggleCustomFiles()" aria-expanded="${isExp}">
                <span class="badge custom-files-badge">Custom files</span>
                <div class="podcast-info">
                  <div class="podcast-title">Uploaded files</div>
                  <div class="podcast-author">${filesFiltered.length} file${filesFiltered.length === 1 ? '' : 's'}${adFreeCount ? ` · ${adFreeCount} ad-free` : ''}${playedCount ? ` · ${playedCount} played` : ''}</div>
                </div>
                <span class="section-count">${filesFiltered.length}</span>
                <span class="expand-icon">&#9654;</span>
              </div>`;
            if (isExp) {
              html += `<div class="episode-list compact">`;
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
        html += `<div class="section-header section-header-spaced">
          <span>All podcasts</span>
          <span class="section-count">${filtered.length}</span>
        </div>`;
        html += renderPodcastGroup(filtered);
      }

      el('podcast-list').innerHTML = html;
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
          <button class="btn ghost sm" type="button" onclick="event.stopPropagation(); renameFile('${f.uuid}')" title="Rename">Rename</button>
          <button class="btn ghost sm" type="button" onclick="event.stopPropagation(); toggleFilePlayed('${f.uuid}', ${f.playing_status !== 3})" title="${markLabel}">${markLabel}</button>
          <button class="btn ghost sm" type="button" onclick="event.stopPropagation(); removeFileFromUpNext('${f.uuid}')" title="Remove from Up Next">Un-queue</button>
          <button class="btn ghost sm danger" type="button" onclick="event.stopPropagation(); deleteFile('${f.uuid}', '${esc(f.title).replace(/'/g, '&#39;')}')" title="Delete">Delete</button>
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
      const dur = (ep.duration && ep.duration > 0) ? formatDur(ep.duration) : '';
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
        <div class="ep-check"></div>
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
          <button class="btn ghost sm" type="button" onclick="event.stopPropagation(); toggleEpisodePlayed('${podcastUuid}','${esc(ep.uuid)}',${markPlayed})" title="${markLabel}">${markLabel}</button>
          <button class="btn ghost sm" type="button" onclick="event.stopPropagation(); removeEpisodeFromUpNext('${esc(ep.uuid)}')" title="Remove from Up Next">Un-queue</button>
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
      const row = document.querySelector(`.file-row[data-uuid="${uuid}"] .file-title`);
      if (!row) return;
      const input = document.createElement('input');
      input.className = 'inline-rename-input';
      input.value = f.title;
      row.replaceWith(input);
      input.focus();
      input.select();
      const finish = async (save) => {
        if (save) {
          const newTitle = input.value.trim();
          if (newTitle && newTitle !== f.title) {
            addLog('info', `Renaming to: ${newTitle}`);
            const r = await api(`/files/${uuid}`, {
              method: 'PATCH',
              body: JSON.stringify({ title: newTitle }),
            });
            if (r.ok) { addLog('success', 'Renamed'); await loadUploadedFiles(); }
            else addLog('error', 'Rename failed');
          }
        }
        renderPodcasts();
      };
      input.addEventListener('keydown', e => {
        if (e.key === 'Enter') { e.preventDefault(); finish(true); }
        if (e.key === 'Escape') { e.preventDefault(); finish(false); }
      });
      input.addEventListener('blur', () => finish(true));
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
      const { confirmed } = await confirmDialog({
        title: 'Delete uploaded file',
        message: `Delete "${title}" from Pocket Casts cloud? This permanently removes the file and clears its processed marker.`,
        confirmLabel: 'Delete',
        danger: true,
      });
      if (!confirmed) return;
      addLog('warn', `Deleting: ${title}`);
      const r = await api(`/files/${uuid}`, { method: 'DELETE' });
      if (r.ok) { addLog('success', 'Deleted'); await loadSubscriptions(); }
      else addLog('error', 'Delete failed');
    }

    async function cleanupPlayedFiles() {
      const { confirmed, values } = await confirmDialog({
        title: 'Clean up played files',
        message: 'Remove played ad-free uploaded files from Pocket Casts cloud?',
        confirmLabel: 'Clean up',
        danger: true,
        fields: [{ type: 'checkbox', id: 'include90', label: 'Also include in-progress files that are 90%+ complete' }],
      });
      if (!confirmed) return;
      const also90 = values.include90;
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
    let servicesState = { services: [], expandedLogs: new Set(), llm_provider: 'ollama' };
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
      if (m) { m.close(); m.remove(); }
      if (servicesPollTimer) { clearInterval(servicesPollTimer); servicesPollTimer = null; }
    }

    async function refreshServices() {
      try {
        const d = await api('/services');
        servicesState.services = d.services || [];
        servicesState.memory = d.memory || null;
        servicesState.llm_provider = d.llm_provider || 'ollama';
        if (document.getElementById('services-modal')) renderServicesModal();
        updateStatusChip(servicesState.services, servicesState.memory, servicesState.llm_provider);
      } catch (e) {
        addLog('error', 'Failed to refresh services: ' + e.message);
      }
    }

    function renderServicesModal() {
      let modal = document.getElementById('services-modal');
      const wasOpenLogs = new Set(servicesState.expandedLogs);
      if (!modal) {
        modal = document.createElement('dialog');
        modal.id = 'services-modal';
        modal.className = 'dialog';
        document.body.appendChild(modal);
      }

      let body = '<div class="services-panel-actions">';
      body += '<span class="svc-reminder">Stop services before leaving to free system RAM.</span>';
      body += '<button class="btn sm primary" type="button" onclick="startAllServices()">Start all</button>';
      body += '<button class="btn sm" type="button" onclick="stopAllServices()">Stop all</button>';
      body += '<button class="btn sm danger" type="button" onclick="shutdownUI()">Shut down UI</button>';
      body += '</div>';
      body += '<div class="dialog-body dialog-body-flush">';
      for (const s of servicesState.services) {
        if (s.id === 'ollama' && servicesState.llm_provider !== 'ollama') continue;
        body += renderServiceRow(s, wasOpenLogs.has(s.id));
      }
      body += '</div>';
      body += renderServicesFooter();

      modal.innerHTML = `<div class="dialog-card">
        <div class="dialog-header">
          <div>
            <div class="dialog-title">Services</div>
            <div class="dialog-subtitle">Status auto-refreshes every 5 seconds.</div>
          </div>
          <div class="dialog-actions">
            <button class="btn sm" type="button" onclick="refreshServices()">Refresh</button>
            <button class="btn sm" type="button" onclick="closeServicesPanel()">Close</button>
          </div>
        </div>
        ${body}
      </div>`;

      if (!modal.open) {
        modal.addEventListener('cancel', e => { e.preventDefault(); closeServicesPanel(); });
        modal.showModal();
      }
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

    let modelPullingState = {};

    function renderServicesFooter() {
      const ollama = servicesState.services.find(s => s.id === 'ollama');
      const models = (ollama && ollama.extra && ollama.extra.models) || [];
      const current = ollamaModelsCache && ollamaModelsCache.current;

      // Get recommended model from the latest memory API data if available
      const recommendedModel = (servicesState.memory && servicesState.memory.recommended_model) || 'qwen3:14b';

      const availableOptions = [
        { value: 'qwen3.5-addetect', label: `qwen3.5-addetect (default, lightest)${recommendedModel === 'qwen3.5-addetect' ? ' (recommended)' : ''}` },
        { value: 'qwen3:14b', label: `qwen3:14b (balanced)${recommendedModel === 'qwen3:14b' ? ' (recommended)' : ''}` },
        { value: 'qwen3.5:35b-a3b', label: `qwen3.5:35b-a3b (high accuracy)${recommendedModel === 'qwen3.5:35b-a3b' ? ' (recommended)' : ''}` }
      ];

      // Build options, labeling which ones are installed
      const opts = availableOptions.map(opt => {
        const isInstalled = models.some(m => m === opt.value || m.split(':')[0] === opt.value.split(':')[0]);
        const suffix = isInstalled ? ' (Installed)' : ' (Will download automatically)';
        const selectedAttr = opt.value === current ? 'selected' : '';
        return `<option value="${esc(opt.value)}" ${selectedAttr}>${esc(opt.label)}${suffix}</option>`;
      }).join('');
      
      let pullStatusHtml = '';
      for (const [model, state] of Object.entries(modelPullingState)) {
        if (state.status === 'downloading') {
          pullStatusHtml += `<div class="svc-pull-status info">Downloading ${esc(model)}: ${state.pct}%</div>`;
        } else if (state.status === 'success') {
          pullStatusHtml += `<div class="svc-pull-status success">Downloaded ${esc(model)}</div>`;
        } else if (state.status === 'error') {
          pullStatusHtml += `<div class="svc-pull-status error">Failed ${esc(model)}: ${esc(state.error)}</div>`;
        }
      }

      return `<div class="dialog-footer">
        ${servicesState.llm_provider !== 'ollama' ? `
        <div class="svc-footer-note">
          LLM provider: <strong>${esc(servicesState.llm_provider)}</strong>. Ollama is not needed and has been hidden.
        </div>
        ` : `
        <div class="svc-footer-row">
          <span class="svc-footer-note">Model</span>
          <select id="ollama-model-sel">${opts}</select>
          <button class="btn sm primary" type="button" onclick="applyModelSelection()">Apply</button>
        </div>
        <div class="svc-footer-note">Current: <strong>${esc(current || 'unknown')}</strong> · Recommended: <code>${esc(recommendedModel)}</code></div>
        ${pullStatusHtml}
        `}
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

    async function applyModelSelection() {
      const sel = document.getElementById('ollama-model-sel');
      if (!sel || !sel.value) return;
      const model = sel.value;
      const ollama = servicesState.services.find(s => s.id === 'ollama');
      const models = (ollama && ollama.extra && ollama.extra.models) || [];
      const isInstalled = models.some(m => m === model || m.split(':')[0] === model.split(':')[0]);

      if (!isInstalled) {
        addLog('info', `Model ${model} is not installed locally. Starting download...`);
        try {
          const r = await fetch('/api/services/ollama/pull', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ model }),
          });
          const j = await r.json();
          if (j.ok) {
            modelPullingState[model] = { status: 'downloading', pct: 0 };
            renderServicesModal();
            pollModelPullStatus(model, true); // true to apply model after download
          } else {
            addLog('error', `Download failed to start: ${j.error || 'unknown'}`);
          }
        } catch (e) {
          addLog('error', 'Download failed: ' + e.message);
        }
      } else {
        await executeSetModel(model);
      }
    }

    async function executeSetModel(model) {
      try {
        const r = await fetch('/api/services/ollama/model', {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ model }),
        });
        const j = await r.json();
        if (j.ok) {
          addLog('success', `MinusPod model set to ${model}`);
          ollamaModelsCache = await api('/services/ollama/model');
        } else {
          addLog('error', `Failed to set model: ${j.error || 'unknown'}`);
        }
      } catch (e) {
        addLog('error', 'Set model failed: ' + e.message);
      }
      renderServicesModal();
    }

    async function pollModelPullStatus(model, shouldApply = false) {
      const interval = setInterval(async () => {
        try {
          const r = await fetch(`/api/services/ollama/pull-status?model=${encodeURIComponent(model)}`);
          const j = await r.json();
          if (j.status === 'success') {
            modelPullingState[model] = { status: 'success' };
            addLog('success', `Model ${model} downloaded successfully!`);
            clearInterval(interval);
            try { ollamaModelsCache = await api('/services/ollama/model'); } catch {}
            await refreshServices();
            if (shouldApply) {
              await executeSetModel(model);
            }
          } else if (j.status === 'error') {
            modelPullingState[model] = { status: 'error', error: j.error };
            addLog('error', `Model ${model} download failed: ${j.error}`);
            clearInterval(interval);
          } else if (j.completed && j.total) {
            const pct = Math.round((j.completed / j.total) * 100);
            modelPullingState[model] = { status: 'downloading', pct };
          }
          if (document.getElementById('services-modal')) renderServicesModal();
        } catch (e) {
          clearInterval(interval);
        }
      }, 2000);
    }

    // ───── Ad detection tunables panel (LLM cost optimisations) ─────
    // The four cost levers live in MinusPod's stage-tunables system. The
    // React UI is not built for this fork, so this dashboard owns the
    // settings surface. We only manage the three keys the dashboard
    // documents (largeWindowSeconds, skipVerificationUnderSeconds,
    // enablePromptCaching); the other ~20 stage tunables are editable
    // via MinusPod's own UI at http://localhost:8000/ui/.
    let adTunablesState = null;
    let adTunablesSaving = false;

    const LARGE_CONTEXT_PATTERNS = [
      'deepseek-v4', 'gemini-2.5-flash', 'gemini-3-flash',
      'gemini-flash', 'qwen-long', 'llama-4-', 'llama-3.1-405b',
    ];

    function _unpackTunable(t) {
      // MinusPod returns the wrapped shape `{value, isDefault, envOverride}`.
      // For the form we want the bare value; the default/override flags are
      // surfaced as a small badge next to each field.
      if (t === null || t === undefined) return { value: null, isDefault: true, envOverride: false };
      if (typeof t === 'object' && 'value' in t) return t;
      return { value: t, isDefault: false, envOverride: false };
    }

    async function openAdTunablesPanel() {
      let modal = document.getElementById('ad-tunables-modal');
      if (!modal) {
        modal = document.createElement('dialog');
        modal.id = 'ad-tunables-modal';
        modal.className = 'dialog';
        modal.innerHTML = `<div class="dialog-card">
          <div class="dialog-header">
            <div>
              <div class="dialog-title">Ad detection settings</div>
              <div class="dialog-subtitle">Stored in MinusPod settings. Takes effect on the next episode.</div>
            </div>
            <div class="dialog-actions">
              <button class="btn sm" type="button" id="ad-tunables-refresh">Refresh</button>
              <button class="btn sm" type="button" id="ad-tunables-close">Close</button>
            </div>
          </div>
          <div class="dialog-body" id="ad-tunables-body"></div>
        </div>`;
        modal.addEventListener('cancel', e => { e.preventDefault(); closeAdTunablesPanel(); });
        document.body.appendChild(modal);
        modal.querySelector('#ad-tunables-close').onclick = closeAdTunablesPanel;
        modal.querySelector('#ad-tunables-refresh').onclick = () => loadAdTunables();
      }
      modal.showModal();
      await loadAdTunables();
    }

    function closeAdTunablesPanel() {
      const m = document.getElementById('ad-tunables-modal');
      if (m) { m.close(); m.remove(); }
    }

    async function loadAdTunables() {
      const body = document.getElementById('ad-tunables-body');
      if (!body) return;
      body.innerHTML = '<div class="episodes-loading">Loading...</div>';
      try {
        const data = await api('/minuspod/settings');
        adTunablesState = data;
        renderAdTunablesBody(data);
      } catch (e) {
        body.innerHTML = `<div class="svc-warn">Could not reach MinusPod: ${esc(e.message)}<br><br>
          Start it from the <strong>Services</strong> panel, then click <em>Refresh</em>.</div>`;
      }
    }

    function renderAdTunablesBody(data) {
      const body = document.getElementById('ad-tunables-body');
      if (!body) return;
      const t = (data && data.stageTunables) || {};
      const def = (data && data.stageTunableDefaults) || {};
      const model = ((data && data.claudeModel) || {}).value
                 || ((data && data.claudeModel) || '')
                 || (typeof (data && data.claudeModel) === 'string' ? data.claudeModel : '');
      const modelText = (typeof model === 'string' && model) ? model : 'unknown';
      const triggersLongContext = LARGE_CONTEXT_PATTERNS.some(p => modelText.includes(p));
      const large = _unpackTunable(t.largeWindowSeconds);
      const skip = _unpackTunable(t.skipVerificationUnderSeconds);
      const cache = _unpackTunable(t.enablePromptCaching);

      const fmtSec = (v) => {
        if (v === null || v === undefined) return '—';
        const n = Number(v);
        if (!Number.isFinite(n)) return String(v);
        if (n === 0) return '0 (off)';
        if (n < 60) return `${n}s`;
        const m = Math.floor(n / 60);
        const s = n % 60;
        return s ? `${m}m ${s}s` : `${m} min`;
      };

      const badge = (t) => {
        if (t.envOverride) return '<span class="tunable-badge env" title="Overridden by environment variable">env</span>';
        if (t.isDefault) return '<span class="tunable-badge def" title="Using built-in default">default</span>';
        return '<span class="tunable-badge set" title="User-set value">custom</span>';
      };

      const longContextHint = triggersLongContext
        ? `<div class="tunable-hint ok">Current model <code>${esc(modelText)}</code> matches a 1M-context pattern. The large-window override is active for long episodes.</div>`
        : `<div class="tunable-hint muted">Current model <code>${esc(modelText)}</code> does not match a 1M-context pattern. The large-window override will be a no-op until you switch models.</div>`;

      body.innerHTML = `
        <div class="tunable-section">
          <div class="tunable-title">Large context window</div>
          <div class="tunable-desc">When the model ID matches a 1M-context pattern <em>and</em> the episode is more than 2× the base window, the detector uses this larger window. Default range 300–36000 (5 min – 10 hr); widen via <code>LARGE_WINDOW_MAX_SECONDS</code> in <code>.env</code> for larger-context models.</div>
          ${longContextHint}
          <label class="tunable-row">
            <span>Large window (seconds)</span>
            <input type="number" id="t-large" min="300" max="36000" step="60"
              value="${esc(large.value ?? '')}" placeholder="${esc(def.largeWindowSeconds ?? 10800)}">
            ${badge(large)}
          </label>
          <div class="tunable-hint muted">Default: ${esc(fmtSec(def.largeWindowSeconds))} (10800s = 3 hr). Must be ≥ the base <code>windowSizeSeconds</code>. API rejects values outside the configured range.</div>
        </div>

        <div class="tunable-section">
          <div class="tunable-title">Skip verification pass on short episodes</div>
          <div class="tunable-desc">Pass 2 doubles LLM cost per episode for near-zero yield on short ones. Set to 0 to disable the skip.</div>
          <label class="tunable-row">
            <span>Skip pass 2 under (seconds)</span>
            <input type="number" id="t-skip" min="0" max="86400" step="60"
              value="${esc(skip.value ?? '')}" placeholder="${esc(def.skipVerificationUnderSeconds ?? 1200)}">
            ${badge(skip)}
          </label>
          <div class="tunable-hint muted">Default: ${esc(fmtSec(def.skipVerificationUnderSeconds))} (20 min). Set to 0 to always run pass 2.</div>
        </div>

        <div class="tunable-section">
          <div class="tunable-title">System-prompt caching</div>
          <div class="tunable-desc">Annotate the system prompt with OpenRouter's <code>cache_control: ephemeral</code> marker so the provider can cache it across the ~22 windows of a long episode. Saves ~1.2K tokens per window at the cache-read rate (1/4.5× input). The <code>cached</code> count is logged per request for visibility.</div>
          <label class="tunable-row">
            <span>Enable prompt caching</span>
            <input type="checkbox" id="t-cache" ${cache.value ? 'checked' : ''}>
            ${badge(cache)}
          </label>
          <div class="tunable-hint muted">Default: enabled. Disable if your provider rejects the <code>cache_control</code> field.</div>
        </div>

        <div id="ad-tunables-status" class="tunable-hint muted tunable-status"></div>
        <div class="tunable-footer">
          <button class="btn sm" type="button" onclick="closeAdTunablesPanel()">Cancel</button>
          <button class="btn sm primary" type="button" id="ad-tunables-save" onclick="saveAdTunables()">Save</button>
        </div>
      `;
    }

    async function saveAdTunables() {
      if (adTunablesSaving) return;
      adTunablesSaving = true;
      const status = document.getElementById('ad-tunables-status');
      const saveBtn = document.getElementById('ad-tunables-save');
      const large = document.getElementById('t-large').value;
      const skip = document.getElementById('t-skip').value;
      const cache = document.getElementById('t-cache').checked;
      const body = {
        largeWindowSeconds: Number(large),
        skipVerificationUnderSeconds: Number(skip),
        enablePromptCaching: cache,
      };
      saveBtn.disabled = true;
      status.textContent = 'Saving...';
      status.className = 'tunable-hint muted tunable-status';
      try {
        const r = await fetch('/api/minuspod/stage-tunables', {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
        const j = await r.json();
        if (r.ok && j.ok) {
          status.textContent = `Saved (${j.updated.join(', ')}). Next episode will use the new values.`;
          status.className = 'tunable-hint ok tunable-status';
          addLog('success', 'Ad detection tunables saved');
        } else {
          status.textContent = `Save failed: ${j.error || ('HTTP ' + r.status_code)}`;
          status.className = 'tunable-hint danger tunable-status';
          addLog('error', 'Ad detection tunables save failed: ' + (j.error || ('HTTP ' + r.status_code)));
        }
      } catch (e) {
        status.textContent = 'Save failed: ' + e.message;
        status.className = 'tunable-hint danger tunable-status';
        addLog('error', 'Ad detection tunables save failed: ' + e.message);
      } finally {
        saveBtn.disabled = false;
        adTunablesSaving = false;
      }
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

        const cbCls = ['podcast-check',
          checkState === 'all' ? 'checked' : '',
          checkState === 'some' ? 'indeterminate' : '',
        ].filter(Boolean).join(' ');
        const ariaChecked = checkState === 'all' ? 'true' : (checkState === 'some' ? 'mixed' : 'false');
        const willSelectAll = checkState !== 'all';

        let html = `<div class="${cls}" data-uuid="${p.uuid}">
          <div class="podcast-header" onclick="togglePodcast('${p.uuid}')" aria-expanded="${isExp}">
            <div class="${cbCls}" role="checkbox" aria-checked="${ariaChecked}"
                 aria-label="Select all eligible episodes" tabindex="0"
                 onclick="event.stopPropagation(); selectAllEpsInPodcast('${p.uuid}', ${willSelectAll})"
                 onkeydown="if(event.key===' '||event.key==='Enter'){event.preventDefault();event.stopPropagation();selectAllEpsInPodcast('${p.uuid}',${willSelectAll});}"></div>
            <div class="podcast-thumb" aria-hidden="true">${thumbHTML(p)}</div>
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
      if (loadEpisodesErrors.has(uuid)) {
        const errMsg = loadEpisodesErrorMsg.get(uuid) || 'Failed to load episodes.';
        return `<div class="episode-list"><div class="episodes-loading error">${escapeHtml(errMsg)} <button class="btn sm" type="button" onclick="event.stopPropagation(); loadEpisodes('${uuid}')">Retry</button></div></div>`;
      }
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
      const { confirmed } = await confirmDialog({
        title: 'Reset processed markers',
        message: 'Episodes will become eligible for processing again. Files already uploaded to Pocket Casts are not deleted.',
        confirmLabel: 'Reset',
      });
      if (!confirmed) return;
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
        loadEpisodesErrors.delete(uuid);
        loadEpisodesErrorMsg.delete(uuid);
        // Up Next rows may have been selected using Pocket Casts UUIDs (ep.uuid)
        // before episodes loaded. After loading, the rows use MinusPod episode IDs
        // (match.id). Remap selected IDs by title so selection state survives.
        const existingSel = selectedEpisodes[uuid];
        if (existingSel && existingSel.size > 0) {
          const remapped = new Set();
          for (const oldId of existingSel) {
            const ep = podcastEpisodes[uuid].find(e => e.id === oldId);
            if (ep) {
              remapped.add(ep.id);
              continue;
            }
            // Old ID was a PC UUID; find the matching MinusPod episode by title.
            const titlesToMatch = (upNextEpisodes || [])
              .filter(u => u.podcast_uuid === uuid)
              .map(u => u.title);
            const match = podcastEpisodes[uuid].find(e => titlesToMatch.includes(e.title) || titlesToMatch.some(t => t + ' (Ad-Free)' === e.title));
            if (match) remapped.add(match.id);
          }
          selectedEpisodes[uuid] = remapped;
        }
        renderPodcasts();
      } catch(e) {
        podcastEpisodes[uuid] = null;
        loadEpisodesErrors.add(uuid);
        let msg = 'Failed to load episodes.';
        if (e instanceof ApiError && e.body) {
          msg = e.body.error || e.body.message || msg;
        } else if (e && e.message) {
          msg = e.message;
        }
        loadEpisodesErrorMsg.set(uuid, msg);
        renderPodcasts();
      } finally {
        loadingEpisodes.delete(uuid);
      }
    }

    function togglePodcast(uuid, realUuid) {
      const lookupUuid = realUuid || uuid;
      const p = podcasts.find(x => x.uuid === lookupUuid);

      const wasExpanded = expandedPodcasts.has(uuid);
      if (wasExpanded) {
        expandedPodcasts.delete(uuid);
      } else {
        expandedPodcasts.add(uuid);
      }
      // For Up Next items, we use 'upnext-{uuid}' as the expand key
      // but load episodes using the real podcast UUID
      const episodeUuid = realUuid || uuid;
      if (!wasExpanded && !podcastEpisodes[episodeUuid] && !loadingEpisodes.has(episodeUuid)) {
        loadEpisodes(episodeUuid);
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
      updateProcessBtn();
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
      const bar = el('selection-bar');
      const text = el('selection-text');
      const btn = el('btn-process');
      if (bar) bar.classList.toggle('visible', count > 0);
      if (text) {
        text.textContent = count === 1 ? '1 episode selected' : `${count} episodes selected`;
      }
      if (btn) {
        btn.disabled = count === 0;
        if (activeJobId) {
          btn.textContent = count ? `Queue ${count} more` : 'Queue more';
        } else {
          btn.textContent = count === 1 ? 'Process 1 episode' : `Process ${count} episodes`;
        }
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
      el('btn-pause').classList.toggle('hidden', !show);
      el('btn-stop').classList.toggle('hidden', !show);
      if (!show) {
        // Reset pause button to default state when controls are hidden
        el('btn-pause').textContent = 'Pause';
        el('btn-pause').classList.remove('active');
      }
    }

    let _jobIsPaused = false;

    async function togglePause() {
      if (!activeJobId) return;
      try {
        const endpoint = _jobIsPaused ? 'resume' : 'pause';
        await api('/job/' + activeJobId + '/' + endpoint, { method: 'POST' });
        _jobIsPaused = !_jobIsPaused;
        const btn = el('btn-pause');
        btn.textContent = _jobIsPaused ? 'Resume' : 'Pause';
        btn.classList.toggle('active', _jobIsPaused);
        addLog(_jobIsPaused ? 'warn' : 'info', _jobIsPaused ? 'Job paused.' : 'Job resumed.');
      } catch(e) { addLog('error', 'Pause failed: ' + e.message); }
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
      // Also clear paused state so button resets on next job
      _jobIsPaused = false;
      try {
        await api('/job/' + activeJobId + '/stop', { method: 'POST' });
        addLog('warn', 'Stopping job...');
      } catch(e) { addLog('error', 'Stop failed: ' + e.message); }
    }

    function startPolling() {
      if (pollTimer) clearInterval(pollTimer);
      pollTimer = setInterval(pollJobs, 3000);
    }

    function applyActiveJobProgress(active, queuedEps) {
      const jp = el('job-progress');
      const jpf = el('job-progress-fill');
      const stageEl = el('log-stage-text');
      if (!active) {
        if (jp) jp.classList.remove('active');
        if (stageEl) stageEl.textContent = '';
        return;
      }
      if (jp) jp.classList.add('active');
      const epName = active.current_episode || 'Processing...';
      const epPaused = active.paused ? ' (paused)' : '';
      const stageText = (active.current_episode_completed ? 'Done: ' : '') + epName + epPaused;
      if (stageEl) stageEl.textContent = stageText;
      const totalAll = active.total_episodes + (queuedEps || 0);
      const pct = totalAll > 0 ? Math.round((active.processed / totalAll) * 100) : 0;
      if (jpf) jpf.style.width = pct + '%';
      if (jp) jp.setAttribute('aria-valuenow', String(pct));
    }

    /** Re-attach to a server-side job after reload or iOS tab sleep. */
    async function recoverActiveJob(replayLogs) {
      try {
        const d = await api('/queue/status');
        const active = d.active_job;
        if (!active) return false;

        const queuedEps = d.queued_episodes || 0;
        const isNewJob = activeJobId !== active.job_id;
        activeJobId = active.job_id;
        _jobIsPaused = !!active.paused;
        showJobControls(true);
        const pauseBtn = el('btn-pause');
        if (pauseBtn) {
          pauseBtn.textContent = _jobIsPaused ? 'Resume' : 'Pause';
          pauseBtn.classList.toggle('active', _jobIsPaused);
        }
        applyActiveJobProgress(active, queuedEps);

        const qb = el('queue-badge');
        if (queuedEps > 0) {
          qb.classList.remove('hidden');
          qb.textContent = `+${queuedEps} queued`;
        } else {
          qb.classList.add('hidden');
        }

        if (replayLogs || isNewJob) {
          if (replayLogs) clearLog();
          lastLogCursor = 0;
          const jd = await api('/job/' + activeJobId + '?cursor=0');
          if (jd.new_logs && jd.new_logs.length > 0) {
            jd.new_logs.forEach(l => addLog(l.level, l.msg));
            lastLogCursor = jd.log_count != null ? jd.log_count : jd.new_logs.length;
          }
        } else {
          // Tab woke — sync cursor in case logs arrived while timers were frozen
          const jd = await api('/job/' + activeJobId + '?cursor=' + lastLogCursor);
          if (jd.log_count != null && jd.log_count < lastLogCursor) {
            lastLogCursor = jd.log_count;
          }
          if (jd.new_logs && jd.new_logs.length > 0) {
            jd.new_logs.forEach(l => addLog(l.level, l.msg));
            lastLogCursor = jd.log_count != null ? jd.log_count : lastLogCursor + jd.new_logs.length;
          }
        }

        startPolling();
        updateProcessBtn();
        return true;
      } catch (e) {
        console.warn('recoverActiveJob failed:', e);
        return false;
      }
    }

    async function pollJobs() {
      try {
        const d = await api('/queue/status');
        const active = d.active_job;
        const queuedEps = d.queued_episodes || 0;

        if (active) {
          // Pick up an in-flight job if we lost client state (reload / iOS sleep)
          if (!activeJobId) {
            activeJobId = active.job_id;
            showJobControls(true);
          }
          applyActiveJobProgress(active, queuedEps);

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
            lastLogCursor = jd.log_count != null ? jd.log_count : lastLogCursor + jd.new_logs.length;
          }
          if (jd.status === 'completed' || jd.status === 'failed' || jd.status === 'stopped') {
            _jobIsPaused = false;
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
              el('job-progress').classList.remove('active');
              const stageEl = el('log-stage-text');
              if (stageEl) stageEl.textContent = '';
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
      const jp = el('job-progress');
      const jpf = el('job-progress-fill');
      if (jp) jp.classList.add('active');
      if (jpf) jpf.style.width = '0%';
      const stageEl = el('log-stage-text');
      if (stageEl) stageEl.textContent = 'Starting...';
      logForceOpen = true;
      const panel = el('log-panel');
      if (panel) panel.classList.remove('collapsed');
    }

    function addLog(level, msg, opts = {}) {
      const body = el('log-body');
      const ts = new Date().toLocaleTimeString();
      let cls = level;
      const m = esc(msg);
      if (m.includes('Downloading') || m.includes('Downloaded')) cls = 'download';
      else if (m.includes('Upload') || m.includes('Syncing')) cls = 'upload';
      else if (m.includes('Processing:') || m.includes('Starting:')) cls = 'stage';

      const line = document.createElement('div');
      line.className = 'log-line ' + cls;
      line.innerHTML = `<span class="log-ts">${ts}</span>${m}`;
      body.appendChild(line);
      body.scrollTop = body.scrollHeight;

      const panel = el('log-panel');
      const isCollapsed = panel.classList.contains('collapsed');
      const shouldOpen = opts.forceOpen || level === 'error' || level === 'warn' || logForceOpen;

      if (shouldOpen && isCollapsed) {
        panel.classList.remove('collapsed');
        el('log-unread').classList.remove('has-new');
        logUnreadCount = 0;
        if (level !== 'error' && level !== 'warn') logForceOpen = false;
      } else if (isCollapsed) {
        logUnreadCount++;
        el('log-unread').classList.add('has-new');
      }
    }
    function clearLog() { el('log-body').innerHTML = ''; }

    function formatDur(s) {
      const m = Math.floor(s / 60);
      return m >= 60 ? `${Math.floor(m/60)}h${m%60}m` : `${m}m`;
    }

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
        el('history-rows').innerHTML = `<tr><td colspan="6" class="empty-state-cell">Failed to load history: ${esc(e.message)}</td></tr>`;
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
        el('history-rows').innerHTML = `<tr><td colspan="6" class="empty-state-cell">No matching entries.</td></tr>`;
        return;
      }
      el('history-rows').innerHTML = rows.map(r => {
        const dt = r.processed_at ? new Date(r.processed_at) : null;
        const dateStr = dt ? dt.toLocaleString('en-US', { month:'short', day:'numeric', year:'numeric', hour:'numeric', minute:'2-digit' }) : '—';
        // Highlight rows that look suspicious so the user knows the reset
        // button is the right next step. "deleted" means the ad-free file
        // vanished from Pocket Casts; "no-effect" means processing recorded
        // a marker but the cut produced 0 ads removed (often a sign the
        // ad-detection pipeline silently failed and MinusPod is now stuck).
        const suspect = r.deleted || (r.ads_removed == null && r.time_saved_secs == null);
        const actionCls = suspect ? 'btn small warning' : 'btn small';
        const actionTitle = suspect
          ? 'MinusPod state may be stuck. Reset so the episode can be reprocessed.'
          : 'Reset MinusPod state and re-queue for processing.';
        return `<tr class="${suspect ? 'history-row-suspect' : ''}">
          <td>${dateStr}</td>
          <td title="${esc(r.title || '')}">${esc(r.title || '')}</td>
          <td>${esc(r.podcast_title || '')}</td>
          <td class="num">${r.ads_removed != null ? r.ads_removed : '—'}</td>
          <td class="num">${r.time_saved_secs != null ? formatDuration(r.time_saved_secs) : '—'}</td>
          <td class="actions-col">
            <button class="${actionCls}" title="${actionTitle}"
              onclick="resetMinusPodState('${esc(r.slug || '')}', '${esc(r.episode_id || '')}', '${esc(r.title || '').replace(/'/g, '&#39;')}')">Reset</button>
          </td>
        </tr>`;
      }).join('');
    }

    async function resetMinusPodState(slug, episodeId, title) {
      if (!slug || !episodeId) {
        addLog('error', 'Reset failed: missing slug or episode id.');
        return;
      }
      const ok = await confirmDialog({
        title: 'Reset MinusPod state',
        message: `Reset state for "${title || episodeId}"? This clears stuck processing status and asks MinusPod to reprocess. Your local processed marker is not touched.`,
        confirmLabel: 'Reset',
      });
      if (!ok.confirmed) return;
      addLog('info', `Resetting MinusPod state: ${slug}/${episodeId}…`);
      try {
        const r = await api(`/episodes/${encodeURIComponent(slug)}/${encodeURIComponent(episodeId)}/reset`, {
          method: 'POST',
        });
        if (r.db_reset) {
          let msg = `Reset ${slug}/${episodeId} (was '${r.previous_status}').`;
          if (r.reprocess_triggered) msg += ' Reprocess queued. Watch the log.';
          else if (r.already_processing) msg += ' MinusPod is already reprocessing it.';
          else if (r.reprocess_error) msg += ` Reprocess request failed: ${r.reprocess_error}`;
          addLog('success', msg);
        } else if (r.previous_status === 'not_stuck') {
          addLog('info', `No stuck state to clear for ${slug}/${episodeId} (already healthy).`);
        } else {
          addLog('error', `Reset failed for ${slug}/${episodeId}: ${r.message || r.previous_status}`);
        }
      } catch (e) {
        addLog('error', `Reset request failed: ${e.message}`);
      }
      await loadHistory();
      await loadSubscriptions();
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
    recoverActiveJob(true);
    setInterval(checkStatus, 15000);

    // iOS Safari suspends timers when the tab is backgrounded — resume polling
    // when the user returns without a full reload.
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden) {
        if (activeJobId) {
          pollJobs();
          startPolling();
        } else {
          recoverActiveJob(false);
        }
      }
    });
    window.addEventListener('pageshow', (e) => {
      if (e.persisted) recoverActiveJob(false);
    });

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
