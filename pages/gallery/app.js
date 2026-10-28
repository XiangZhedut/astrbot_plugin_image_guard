const $ = (id) => document.getElementById(id);
const bridge = window.AstrBotPluginPage || window.AstrBotPluginView;
const state = {mode: 'all', page: 1, size: 60, selected: new Set(), items: [], total: 0, generation: 0, busy: false, similarTo: '', groups: []};
const titles = {all: '全部图片', context: '上下文图片', similar: '相似图片整理', memes: '表情包', kept: '已保留', blocked: '已禁发', trash: '回收站'};
const labels = {ban: '禁发', unban: '解除独立禁发', unban_rule: '解除来源规则', mark_meme: '标为表情包', mark_photo: '当作普通图片', meme_auto: '恢复自动判定', keep: '保留', unkeep: '取消保留', detach: '移除上下文', trash: '移入回收站', restore: '恢复', inspect: '测试禁发规则', purge: '彻底删除'};
const thumbCache = new Map();
let currentImage = null, pending = null, poll = null, toastTimer, searchTimer;

function el(tag, cls, text) {const node = document.createElement(tag); if (cls) node.className = cls; if (text !== undefined) node.textContent = text; return node;}
function toast(text) {$('toast').textContent = text; $('toast').hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => {$('toast').hidden = true;}, 6500);}
async function api(method, endpoint, data) {
  const result = await (method === 'GET' ? bridge.apiGet(endpoint, data) : bridge.apiPost(endpoint, data));
  const value = result?.data && result.success === undefined ? result.data : result;
  if (value?.success === false || !value) throw new Error(value?.error || value?.message || '面板请求失败');
  return value;
}
function updateSelection() {
  $('selection').hidden = state.selected.size === 0;
  $('selected-count').textContent = `已选 ${state.selected.size} 张`;
  document.querySelectorAll('.photo').forEach(card => {const checked = state.selected.has(card.dataset.id); card.classList.toggle('selected', checked); card.querySelector('input').checked = checked;});
  document.querySelectorAll('.selection [data-action]').forEach(button => {button.hidden = ['restore', 'purge'].includes(button.dataset.action) ? state.mode !== 'trash' : state.mode === 'trash';});
}
function scanStatus(info) {
  $('scan').disabled = Boolean(info?.running);
  $('scan').textContent = info?.running ? '正在扫描…' : '↻ 扫描历史';
  $('scan-status').textContent = info?.error || (info?.running ? '正在关联聊天历史与本地图片，首次扫描可能需要一点时间。' : info?.last_scan ? `上次扫描 ${new Date(info.last_scan * 1000).toLocaleString('zh-CN')} · 临时图片 ${info.indexed || 0} 张${info.errors ? ` · ${info.errors} 项无法读取` : ''}` : '点击“扫描历史”导入已有图片。');
}
async function load() {
  const generation = ++state.generation;
  try {
    const data = await api('GET', 'images', {mode: state.mode, page: state.page, size: state.size, search: $('search').value, day: $('day').value, group: $('group').value, similar_to: state.similarTo});
    if (generation !== state.generation) return;
    state.items = data.items; state.total = data.total;
    state.groups = data.groups || []; updateGroups(); $('collect-memes').checked = Boolean(data.collect_memes);
    $('heading').textContent = state.similarTo ? '相似图片候选' : titles[state.mode]; $('summary').textContent = `${data.total} 张图片${state.mode === 'similar' ? ` · ${data.similar_group_count} 组相似图片` : ' · 按日期排列'}`;
    $('view-notice').hidden = !state.similarTo && !['similar', 'blocked'].includes(state.mode);
    $('view-notice').textContent = state.similarTo || state.mode === 'similar' ? '按整张图片的构图、颜色与边缘寻找候选，不识别人脸。可逐张核对后批量禁发、保留或移入回收站；不会自动删除。' : '同时展示禁发原图、已被规则覆盖的转码图和待核对的相似候选。“待核对”不代表已经禁发，选中后可单独禁发。';
    $('clear-similar').hidden = !state.similarTo;
    for (const [key, field] of Object.entries({all: 'total', kept: 'kept', blocked: 'blocked', trash: 'trash', memes: 'memes'})) $(`count-${key}`).textContent = data.counts[field];
    scanStatus(data.scan); render();
    $('page-label').textContent = `第 ${state.page} / ${Math.max(1, Math.ceil(state.total / state.size))} 页`;
    $('prev').disabled = state.page <= 1; $('next').disabled = state.page * state.size >= state.total;
  } catch (error) {toast(error.message); $('summary').textContent = '读取失败，可点击“扫描历史”重试';}
}
function updateGroups() {
  const selected = $('group').value; $('group').replaceChildren();
  for (const [value, label] of [['', '全部群聊与未关联图片'], ['unknown', '未关联群聊（含私聊、临时文件）'], ...state.groups.map(g => [g.key, `${g.name} · ${g.platform}（${g.count}）`])]) {const option = el('option', '', label); option.value = value; $('group').append(option);}
  $('group').value = selected; $('rename-group').disabled = !selected || selected === 'unknown';
}
function render() {
  const gallery = $('gallery'); gallery.replaceChildren();
  if (!state.items.length) {const empty = el('div', 'empty'); empty.append(el('div', '', '▧'), el('b', '', '这里还没有图片'), el('p', '', '试试扫描历史，或切换分类与日期。')); gallery.append(empty); return;}
  const groups = new Map();
  for (const item of state.items) {const key = state.mode === 'similar' ? item.similar_group : item.day; if (!groups.has(key)) groups.set(key, []); groups.get(key).push(item);}
  const jobs = [];
  for (const [day, items] of groups) {
    const head = el('div', 'date-head'); head.append(el('h2', '', state.mode === 'similar' ? `相似组 ${day.slice(0, 8)}` : day.replaceAll('-', ' / ')), el('span', '', `${items.length} 张 · 当前页${items[0].similar_count ? ` / 本组 ${items[0].similar_count} 张` : ''}`));
    if (state.mode === 'similar') {const choose = el('button', '', '选择本组当前页'); choose.addEventListener('click', () => {for (const item of items) state.selected.add(item.id); updateSelection();}); head.append(choose);} gallery.append(head);
    const grid = el('div', 'photo-grid'); gallery.append(grid);
    for (const item of items) {
      const card = el('article', 'photo'); card.dataset.id = item.id;
      const check = el('input', 'check'); check.type = 'checkbox'; check.setAttribute('aria-label', `选择图片 ${item.name}`);
      check.addEventListener('change', () => {check.checked ? state.selected.add(item.id) : state.selected.delete(item.id); updateSelection();});
      const open = el('button', 'photo-open'); open.setAttribute('aria-label', `查看图片 ${item.name}`);
      open.append(el('div', 'placeholder', item.removed ? '已移入回收站' : '正在加载预览…')); open.addEventListener('click', () => openDetail(item));
      const badges = el('div', 'badges'); if (item.is_meme) badges.append(el('span', 'badge', '表情包')); if (item.kept) badges.append(el('span', 'badge', '♡ 保留')); if (item.blocked) badges.append(el('span', 'badge banned', item.direct_ban ? '禁发原图' : '规则覆盖')); else if (item.related_blocked) badges.append(el('span', 'badge candidate', '待核对'));
      const meta = el('div', 'photo-meta'); const name = el('p', 'photo-name', item.name); name.title = item.name;
      const sub = el('div', 'photo-sub'); sub.append(el('span', '', item.width ? `${item.width} × ${item.height}` : '图片引用'), el('span', '', item.context_count ? `${item.context_count} 处上下文` : item.detached ? '已移除引用' : '本地图片'));
      meta.append(name, sub); const origin = el('p', 'photo-origin', (item.groups || []).map(key => state.groups.find(g => g.key === key)?.name || key).join(' · ') || '未关联群聊'); origin.title = origin.textContent; meta.append(origin); card.append(open, check, badges, meta); grid.append(card);
      if (!item.removed) jobs.push({item, open});
    }
  }
  updateSelection();
  // Bounded concurrency prevents dozens of remote downloads and image decodes at once.
  const generation = state.generation;
  for (let worker = 0; worker < 4; worker++) (async () => {
    while (jobs.length && generation === state.generation) {
      const {item, open} = jobs.shift();
      try {
        let url = thumbCache.get(item.id);
        if (!url) {const result = await api('GET', 'preview', {id: item.id}); url = result.url; if (thumbCache.size >= 180) thumbCache.delete(thumbCache.keys().next().value); thumbCache.set(item.id, url);}
        if (generation !== state.generation) return;
        const img = el('img'); img.alt = item.name; img.src = url; img.loading = 'lazy'; open.replaceChildren(img);
      } catch {if (generation === state.generation) open.replaceChildren(el('div', 'placeholder', '原图已清理或地址过期'));}
    }
  })();
}
async function openDetail(item) {
  currentImage = item; $('viewer-title').textContent = item.name;
  $('large-image').removeAttribute('src'); $('large-image').hidden = true; $('preview-error').hidden = false;
  $('preview-error').textContent = item.removed ? '图片位于回收站，恢复后可预览。' : '正在加载…';
  const info = $('detail-info'); info.replaceChildren();
  const actions = $('detail-actions'); actions.replaceChildren(); $('context-links').textContent = '正在读取…';
  for (const [key, value] of [['收录日期', item.day], ['尺寸', item.width ? `${item.width} × ${item.height}` : '待读取'], ['实际格式', item.format || '待读取'], ['收纳类型', `${item.is_meme ? '表情包' : '普通图片'}${item.meme_override >= 0 ? '（手动）' : ''}${item.meme_reason ? ` · ${item.meme_reason}` : ''}`], ['文件大小', `${(item.size / 1024).toFixed(1)} KiB`], ['图片编号', item.id.slice(0, 12)], ['来源群聊', (item.groups || []).map(key => state.groups.find(g => g.key === key)?.name || key).join(' · ') || '未关联'], ['状态', [item.kept ? '已保留' : '', item.blocked ? '已禁发' : '', item.related_blocked && !item.blocked ? '相似候选，待核对' : '', item.detached ? '已移除上下文' : ''].filter(Boolean).join(' · ') || '普通图片']]) info.append(el('p', '', `${key}　${value}`));
  for (const action of item.removed ? ['restore', 'purge'] : [item.kept ? 'unkeep' : 'keep', item.direct_ban ? 'unban' : 'ban', 'inspect', item.is_meme ? 'mark_photo' : 'mark_meme', ...(item.meme_override >= 0 ? ['meme_auto'] : []), 'detach', 'trash']) {
    const button = el('button', ['trash', 'detach'].includes(action) ? 'danger' : '', labels[action]);
    button.addEventListener('click', () => requestAction(action, [item.id])); actions.append(button);
  }
  if (!item.removed && item.detached) {const restore = el('button', '', '恢复上下文引用'); restore.addEventListener('click', () => requestAction('restore', [item.id])); actions.append(restore);}
  if (!item.removed) {const similar = el('button', '', '查看相似图片'); similar.addEventListener('click', () => {state.similarTo = item.id; state.mode = 'all'; state.page = 1; state.selected.clear(); $('viewer').close(); currentImage = null; syncNav(); load();}); actions.append(similar);}
  if (!$('viewer').open) $('viewer').showModal();
  api('GET', 'detail', {id: item.id}).then(data => {if (currentImage?.id !== item.id) return; const target = $('context-links'); target.replaceChildren(); if (!data.contexts.length) target.textContent = '没有关联的上下文记录；可能来自临时目录扫描，或引用已经移除。'; for (const ctx of data.contexts) target.append(el('p', '', `${ctx.table_name === 'conversations' ? 'AI 对话' : '平台消息'} · ${ctx.session}`)); info.append(el('p', 'hint', `已确认接收会话：${(data.confirmed_scopes || []).join(' · ') || '无，开启隔离时拒发'}`)); for (const rule of data.rules || []) {const block = el('div', 'rule-source'); block.append(el('p', '', `来源规则 ${rule.sha256.slice(0, 12)} · ${rule.reason}${rule.external ? '（表情包小偷，需在原插件解除）' : ''}`)); if (!rule.external) {const button = el('button', 'danger', '解除此来源规则'); button.addEventListener('click', () => requestAction('unban_rule', [item.id], {rule_sha: rule.sha256})); block.append(button);} info.append(block);}}).catch(error => {$('context-links').textContent = error.message;});
  if (!item.removed) try {const data = await api('GET', 'preview', {id: item.id, large: '1'}); if (currentImage?.id === item.id && $('viewer').open) {$('large-image').src = data.url; $('large-image').hidden = false; $('preview-error').hidden = true;}} catch (error) {if (currentImage?.id === item.id) $('preview-error').textContent = error.message;}
}
function requestAction(action, ids, extra = {}) {
  if (state.busy) return;
  if (['detach', 'trash', 'purge', 'unban_rule'].includes(action)) {
    pending = {action, ids, ...extra}; $('confirm-title').textContent = `${labels[action]} · ${ids.length} 张`;
    $('confirm-description').textContent = action === 'unban_rule' ? '解除这条禁发原图的来源规则后，受其保护的所有转码、压缩及缩放图片也会解除该规则。其他独立禁发规则和表情包黑名单仍有效。' : action === 'purge' ? '永久删除回收站中的这些图片及本插件的上下文恢复记录，此操作不能撤销。禁发指纹仍保留。其他插件、外部目录、QQ 与模型服务端的记录不会一并删除。' : action === 'detach' ? '移除这些图片在 AI 对话和平台消息历史中的引用。其他文字与工具调用结构会保留。已发送到 QQ 的消息和模型服务端记录不会因此撤回。' : '将这些图片的本地文件移入回收站。你可以选择同时移除上下文引用并禁止机器人重发。';
    $('purge-label').hidden = action !== 'purge'; $('purge-confirm').value = '';
    $('delete-options').hidden = action !== 'trash'; $('detach-option').checked = true; $('ban-option').checked = true;
    $('confirm').showModal();
  } else runAction({action, ids, destination: !['', 'unknown'].includes($('group').value) ? $('group').value : '', ...extra});
}
async function runAction(payload) {
  state.busy = true;
  const notice = $('toast'); notice.textContent = '正在处理…'; notice.hidden = false;
  try {
    const data = await api('POST', 'action', payload); let success = 0; const notes = [];
    for (const result of data.results) {if (result.success) {success++; if (result.verdict) notes.push(`规则测试：${result.verdict}${result.guard_enabled ? '' : '（禁发总开关当前关闭）'}`); if (result.scope_verdict) notes.push(`会话隔离：${result.scope_verdict}`); if (result.notice) notes.push(result.notice); if (result.changed_records !== undefined) notes.push(`已处理 ${result.changed_records} 条上下文记录`); if (result.moved_files !== undefined) notes.push(`已移动 ${result.moved_files} 个文件${result.external_files_untouched ? `，${result.external_files_untouched} 个外部文件保持原位` : ''}`); if (result.conflicts) notes.push(`${result.conflicts} 条记录已发生变化，未覆盖。`);} else notes.push(result.error); thumbCache.delete(result.id);}
    toast(`${labels[payload.action]}：${success}/${data.results.length} 张成功${notes.length ? '\n' + [...new Set(notes)].join('\n') : ''}`);
    state.selected.clear(); $('viewer').close(); currentImage = null; await load();
  } catch (error) {toast(error.message);} finally {state.busy = false;}
}
function syncNav() {document.querySelectorAll('[data-mode]').forEach(b => b.classList.toggle('active', b.dataset.mode === state.mode));}
document.querySelectorAll('[data-mode]').forEach(button => button.addEventListener('click', () => {state.mode = button.dataset.mode; state.similarTo = ''; state.page = 1; state.selected.clear(); syncNav(); load();}));
document.querySelectorAll('[data-action]').forEach(button => button.addEventListener('click', () => requestAction(button.dataset.action, [...state.selected])));
$('search').addEventListener('input', () => {clearTimeout(searchTimer); searchTimer = setTimeout(() => {state.page = 1; state.selected.clear(); load();}, 300);});
$('day').addEventListener('change', () => {state.page = 1; state.selected.clear(); load();});
$('reset').addEventListener('click', () => {$('search').value = ''; $('day').value = ''; $('group').value = ''; state.similarTo = ''; state.page = 1; state.selected.clear(); load();});
$('collect-memes').addEventListener('change', async () => {const value = $('collect-memes').checked; $('collect-memes').disabled = true; try {await api('POST', 'collection', {collect_memes: value}); state.page = 1; state.selected.clear(); await load(); startPolling(); toast(value ? '已开启表情包收纳，后续收集会保存副本。' : '已关闭表情包收纳；已有图片不删除，禁发与跨群隔离保持生效。');} catch (error) {$('collect-memes').checked = !value; toast(error.message);} finally {$('collect-memes').disabled = false;}});
$('group').addEventListener('change', () => {state.page = 1; state.selected.clear(); load();});
$('clear-similar').addEventListener('click', () => {state.similarTo = ''; state.page = 1; state.selected.clear(); load();});
$('rename-group').addEventListener('click', () => {$('group-name').value = state.groups.find(g => g.key === $('group').value)?.name || ''; $('group-dialog').showModal();});
$('cancel-group').addEventListener('click', () => $('group-dialog').close());
$('save-group').addEventListener('click', async () => {try {await api('POST', 'group', {key: $('group').value, name: $('group-name').value}); $('group-dialog').close(); await load(); toast('已保存本地群聊备注');} catch (error) {toast(error.message);}});
$('select-page').addEventListener('click', () => {for (const item of state.items) state.selected.add(item.id); updateSelection();});
$('clear-selection').addEventListener('click', () => {state.selected.clear(); updateSelection();});
$('prev').addEventListener('click', () => {state.page--; state.selected.clear(); load(); window.scrollTo(0, 0);});
$('next').addEventListener('click', () => {state.page++; state.selected.clear(); load(); window.scrollTo(0, 0);});
$('close-viewer').addEventListener('click', () => {$('viewer').close(); currentImage = null;});
$('cancel-action').addEventListener('click', () => {$('confirm').close(); pending = null;});
$('confirm-action').addEventListener('click', () => {if (!pending) return; if (pending.action === 'purge' && $('purge-confirm').value !== '彻底删除') {toast('请输入“彻底删除”确认永久清理。'); return;} const payload = {...pending, confirm: true, confirmation: $('purge-confirm').value, detach: $('detach-option').checked, ban: $('ban-option').checked}; pending = null; $('confirm').close(); runAction(payload);});
$('scan').addEventListener('click', async () => {try {await api('POST', 'scan', {}); scanStatus({running: true}); startPolling();} catch (error) {toast(error.message);}});
function startPolling() {if (poll) return; poll = setInterval(async () => {try {const data = await api('GET', 'status'); scanStatus(data.scan); if (!data.scan.running) {clearInterval(poll); poll = null; await load();}} catch (error) {clearInterval(poll); poll = null; toast(error.message);}}, 1500);}
async function init() {
  if (!bridge) {toast('请从 AstrBot 的扩展功能入口打开此页面。'); return;}
  await bridge.ready(); const status = await api('GET', 'status');
  $('guard-status').textContent = status.guard_enabled ? '禁发保护已启用' : '禁发保护已关闭'; $('guard-dot').style.background = status.guard_enabled ? '#36a881' : '#d19b39';
  $('scope-status').textContent = status.guard_enabled && status.group_isolation ? '跨群隔离已启用 · 未知来源拒发' : '跨群隔离未启用';
  if (status.legacy_rules_without_visual) toast(`${status.legacy_rules_without_visual} 条旧规则缺少原图，暂不能补全转码保护；请找到原图后重新禁发。`);
  if (status.storage) $('storage-status').textContent = `图片副本 ${(status.storage.library_bytes / 1048576).toFixed(1)} / ${status.storage.limit_mb} MiB · 回收站 ${(status.storage.trash_bytes / 1048576).toFixed(1)} MiB`;
  await load(); if (status.scan.running) startPolling();
}
init().catch(error => toast(error.message));
