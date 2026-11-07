const $ = s => document.querySelector(s);
let currentThread = null, currentRun = null, streaming = false;
let memoryEdit = null;
let token = sessionStorage.getItem('agent-token') || '';
const terminal = new Set(['completed', 'partial', 'failed', 'cancelled']);

function el(tag, text, cls) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (cls) node.className = cls;
  return node;
}
function notice(text) { $('#notice').textContent = text; $('#notice').hidden = !text; }
function resetRunView() {
  currentRun = null;
  $('#run-status').textContent = 'Готов к работе';
  $('#usage').textContent = '';
  $('#cancel').hidden = true;
  $('#send').disabled = false;
  $('#approval').hidden = true;
  $('#support-result').hidden = true;
  for (const selector of ['#approval', '#sources', '#artifacts', '#events', '#support-result']) $(selector).replaceChildren();
}
async function api(path, options = {}) {
  const headers = {...(token ? {'Authorization': `Bearer ${token}`} : {}), ...options.headers};
  if (options.body && !(options.body instanceof FormData)) headers['Content-Type'] = 'application/json';
  const response = await fetch('/api' + path, {...options, headers});
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    if (response.status === 401) { if (!$('#access-dialog').open) $('#access-dialog').showModal(); }
    throw new Error(typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail || response.statusText));
  }
  return options.raw ? response : response.json();
}
function guarded(fn) { return async (...args) => { try { await fn(...args); } catch (e) { notice(e.message); } }; }
function message(role, content) {
  $('#welcome')?.remove();
  const box = el('article', undefined, 'message ' + role);
  box.append(el('div', role === 'user' ? 'ВЫ' : 'AGENT NETOLOGY', 'role'), el('div', content, 'content'));
  $('#chat').append(box); $('#chat').scrollTop = $('#chat').scrollHeight;
}
async function refreshThreads() {
  const rows = await api('/threads'); $('#threads').replaceChildren();
  for (const t of rows) {
    const button = el('button', t.title, 'thread' + (t.id === currentThread ? ' active' : ''));
    button.onclick = guarded(() => loadThread(t.id)); $('#threads').append(button);
  }
}
async function loadThread(id, follow = true) {
  if (streaming && id !== currentThread) { notice('Дождитесь завершения текущего потока или остановите задачу.'); return; }
  currentThread = id; const thread = await api('/threads/' + id);
  $('#thread-title').textContent = thread.title; $('#chat').replaceChildren();
  thread.messages.forEach(m => message(m.role, m.content));
  await refreshThreads();
  const last = thread.runs.at(-1);
  if (last) { currentRun = last.id; const run = await refreshRun(last.id); if (follow && !streaming && ['queued','running'].includes(run.status)) { watchRun(last.id).catch(e => notice(e.message)); } }
  else { resetRunView(); }
}
async function newThread(title = 'Новое обращение') {
  if (streaming) return;
  const t = await api('/threads', {method: 'POST', body: JSON.stringify({title})});
  currentRun = null; $('#events').replaceChildren(); await loadThread(t.id);
}
async function refreshDocuments() {
  const rows = await api('/documents'); $('#documents').replaceChildren();
  if (!rows.length) $('#documents').append(el('p', 'Добавьте документы для поиска с источниками.', 'hint'));
  for (const d of rows) {
    const item = el('div', undefined, 'item'); const actions = el('span', undefined, 'item-actions');
    const remove = el('button', '×'); remove.title = 'Удалить из базы знаний';
    remove.onclick = guarded(async () => { if (!confirm('Удалить документ из базы знаний и сбросить контекст? История ответов остаётся.')) return; await api('/documents/' + d.id, {method:'DELETE'}); await refreshDocuments(); });
    actions.append(remove); item.append(actions, el('strong', d.name), el('p', `${d.chunks} фрагм. · ${d.state}`)); $('#documents').append(item);
  }
}
async function refreshMemory() {
  const rows = await api('/memory'); $('#memories').replaceChildren();
  if (!rows.length) $('#memories').append(el('p', 'Подтверждённые предпочтения появятся здесь.', 'hint'));
  for (const m of rows) {
    const item = el('div', undefined, 'item'), actions = el('span', undefined, 'item-actions');
    const edit = el('button', '✎'); edit.title = 'Изменить'; edit.onclick = () => { memoryEdit=m; $('#memory-key').readOnly=true; $('#memory-key').value=m.key; $('#memory-value').value=m.value; $('#memory-dialog').showModal(); };
    const history = el('button', 'История');
    history.onclick = guarded(async () => {
      const revisions = await api('/memory/'+m.id+'/history');
      item.querySelector('details')?.remove();
      const details = el('details'); details.open=true; details.append(el('summary','Версии записи'));
      for (const r of revisions) details.append(el('p', `v${r.version} · ${new Date(r.updated*1000).toLocaleString()} · ${r.value}`));
      item.append(details);
    });
    const remove = el('button', '×'); remove.title = 'Забыть';
    remove.onclick = guarded(async () => { await api('/memory/'+m.id, {method:'DELETE'}); await refreshMemory(); notice('Память удалена, контекст сброшен. Для удаления упоминаний из истории удалите соответствующую беседу.'); });
    actions.append(edit,history,remove); item.append(actions, el('strong',m.key),el('p',m.value)); $('#memories').append(item);
  }
}
async function showResult(result) {
  $('#sources').replaceChildren(); $('#artifacts').replaceChildren();
  const panel = $('#support-result'); panel.replaceChildren(); panel.hidden = !result.support_case;
  if (result.support_case) {
    const support = result.support_case;
    panel.append(el('h3', support.category), el('p', support.reason, 'hint'));
    const draft = el('textarea'); draft.value = support.draft;
    draft.setAttribute('aria-label', 'Черновик ответа покупателю'); draft.rows = 9;
    panel.append(draft, el('p', 'Можно отредактировать и скопировать. Автоматической отправки нет.', 'hint'));
    const copy = el('button', 'Скопировать ответ', 'quiet');
    copy.onclick = guarded(async () => { await navigator.clipboard.writeText(draft.value); notice('Черновик скопирован. Отправьте его покупателю после проверки.'); });
    const ticket = el('button', 'Подготовить заявку', 'primary');
    ticket.onclick = guarded(async () => {
      if (streaming || $('#send').disabled) { notice('Дождитесь завершения текущей задачи.'); return; }
      ticket.disabled = true;
      try {
        const body = support.ticket.body + '\n\nЧерновик после проверки менеджером:\n' + draft.value;
        await submitMessage('/ticket ' + support.ticket.title + '\n' + body.slice(0, 4700));
      } finally { ticket.disabled = false; }
    });
    panel.append(copy, ticket);
  }
  for (const s of result.citations || []) {
    const details = el('details', undefined, 'item'), summary=el('summary', s.title + ' · ' + s.locator);
    details.append(summary,el('p',s.excerpt),el('small',s.source_id));
    if (s.url && /^https?:\/\//.test(s.url)) { const a=el('a','Открыть источник ↗','text-button'); a.href=s.url; a.target='_blank'; a.rel='noopener noreferrer'; details.append(a); }
    $('#sources').append(details);
  }
  for (const a of result.artifacts || []) {
    const button=el('button','↓ '+a.filename,'text-button');
    button.onclick=guarded(async()=>{ const response=await api('/artifacts/'+a.artifact_id,{raw:true});const url=URL.createObjectURL(await response.blob());const link=el('a');link.href=url;link.download=a.filename;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000); });
    $('#artifacts').append(button);
  }
}
async function refreshRun(id) {
  const run=await api('/runs/'+id);
  $('#run-status').textContent = ({queued:'В очереди',running:'Агент работает…',waiting_approval:'Нужно ваше подтверждение',completed:'Готово',partial:'Завершено с ограничениями',failed:'Ошибка',cancelled:'Отменено'})[run.status] || run.status;
  $('#usage').textContent=`Модель: ${run.model_calls} выз. · Tools: ${run.tool_calls} · Токены: ${run.tokens}`;
  $('#cancel').hidden=terminal.has(run.status); $('#send').disabled=!terminal.has(run.status);
  $('#approval').replaceChildren(); $('#approval').hidden=!run.approvals.length || run.status!=='waiting_approval';
  for (const approval of run.approvals) {
    $('#approval').append(el('h3','Подтвердите действие: '+approval.tool),el('p','Проверьте аргументы. Вы можете изменить их или отклонить действие.','hint'));
    const args=el('textarea');args.value=JSON.stringify(approval.args,null,2);args.setAttribute('aria-label','Аргументы действия');$('#approval').append(args);
    const buttons=el('div',undefined,'buttons');
    for (const [decision,label,cls] of [['approve','Подтвердить','primary'],['edit','Изменить и выполнить','quiet'],['reject','Отклонить','danger']]) {
      const button=el('button',label,cls);button.onclick=guarded(async()=>{ const body={decision,action_hash:approval.hash};if(decision==='edit')body.edited_args=JSON.parse(args.value);button.disabled=true;try{await api('/approvals/'+approval.id,{method:'POST',body:JSON.stringify(body)});$('#approval').hidden=true;await watchRun(id);}finally{button.disabled=false;}});buttons.append(button);
    }
    $('#approval').append(buttons);
  }
  await showResult(run.result || {});
  return run;
}
async function watchRun(id) {
  streaming=true;$('#send').disabled=true;$('#cancel').hidden=false;
  try {
    const response=await api('/runs/'+id+'/events',{raw:true});
    const reader=response.body.getReader(), decoder=new TextDecoder();let buffer='';
    while(true){const {value,done}=await reader.read();if(done)break;buffer+=decoder.decode(value,{stream:true});let boundary;while((boundary=buffer.indexOf('\n\n'))>=0){const block=buffer.slice(0,boundary);buffer=buffer.slice(boundary+2);const event=block.split('\n').find(x=>x.startsWith('event: '))?.slice(7);const data=block.split('\n').find(x=>x.startsWith('data: '))?.slice(6);if(!event)continue;let payload={};try{payload=JSON.parse(data);}catch{}const labels={'run.started':'Задача запущена','tool.started':'Инструмент','tool.completed':'Готово','model.started':'Модель','model.completed':'Ответ модели','task.started':'Подагент','plan.updated':'План','approval.required':'Ожидает подтверждения','context.compacted':'Контекст сжат','run.completed':'Задача завершена','sandbox.created':'Sandbox создан'};const detail=payload.name||payload.model||payload.role||(payload.tasks?payload.tasks.join(' → '):'');$('#events').append(el('li',(labels[event]||event)+(detail?': '+detail:'')));$('#events').scrollTop=$('#events').scrollHeight;$('#run-status').textContent=labels[event]||event;}}
  } finally {
    streaming=false;await loadThread(currentThread, false);await refreshMemory();await refreshTickets();
  }
}
async function submitMessage(text) {
  if (!text || streaming) return;
  notice('');
  if (!currentThread) await newThread(text.slice(0,50));
  const run=await api('/threads/'+currentThread+'/runs',{method:'POST',headers:{'Idempotency-Key':crypto.randomUUID()},body:JSON.stringify({message:text})});
  currentRun=run.id;message('user',text);$('#message').value='';$('#events').replaceChildren();await watchRun(run.id);
}
$('#composer').onsubmit=guarded(async e=>{e.preventDefault();await submitMessage($('#message').value.trim());});
$('#message').onkeydown=e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();if(!$('#send').disabled)$('#composer').requestSubmit();}};
$('#new-thread').onclick=guarded(()=>newThread());
$('#delete-thread').onclick=guarded(async()=>{if(!currentThread||!confirm('Удалить беседу, checkpoints и её результаты? Локальные заявки сохранятся.'))return;await api('/threads/'+currentThread,{method:'DELETE'});currentThread=null;resetRunView();$('#thread-title').textContent='Новое обращение';$('#chat').replaceChildren();await refreshThreads();});
$('#cancel').onclick=guarded(async()=>{if(currentRun){await api('/runs/'+currentRun+'/cancel',{method:'POST'});await refreshRun(currentRun);}});
$('#upload').onchange=guarded(async e=>{const file=e.target.files[0];if(!file)return;notice('Индексируем '+file.name+'…');const body=new FormData();body.append('file',file);try{await api('/documents',{method:'POST',body});await refreshDocuments();notice('Документ добавлен в Qdrant. Можно задавать вопросы.');}finally{e.target.value='';}});
$('#add-memory').onclick=()=>{memoryEdit=null;$('#memory-key').readOnly=false;$('#memory-key').value='';$('#memory-value').value='';$('#memory-dialog').showModal();};
$('#memory-close').onclick=()=>$('#memory-dialog').close();
$('#memory-form').onsubmit=guarded(async e=>{e.preventDefault();await api('/memory',{method:'POST',body:JSON.stringify({key:$('#memory-key').value,value:$('#memory-value').value,expected_version:memoryEdit?.version || 0})});$('#memory-dialog').close();await refreshMemory();});
$('#access-button').onclick=()=>$('#access-dialog').showModal();
$('#access-form').onsubmit=guarded(async e=>{e.preventDefault();token=$('#token').value;sessionStorage.setItem('agent-token',token);$('#access-dialog').close();await bootstrap();});
document.querySelectorAll('[data-prompt]').forEach(b=>b.onclick=()=>{$('#message').value=b.dataset.prompt;$('#message').focus();});
async function refreshTickets() {
  const rows = await api('/tickets'); $('#tickets').replaceChildren();
  if (!rows.length) $('#tickets').append(el('p', 'После разбора нажмите «Подготовить заявку» и подтвердите действие.', 'hint'));
  for (const row of rows) {
    const item = el('details', undefined, 'item');
    item.append(el('summary', row.title), el('small', row.id), el('p', row.body));
    $('#tickets').append(item);
  }
}
$('#load-sample').onclick = guarded(async () => {
  const button = $('#load-sample'); button.disabled = true;
  try {
    notice('Загружаем регламенты и каталог учебного магазина…');
    await api('/support/sample', {method:'POST'}); await refreshDocuments();
    notice('Магазин «Лист» загружен. Выберите обращение и нажмите стрелку отправки. Повторная загрузка не создаёт дубликаты.');
  } finally { button.disabled = false; }
});
async function bootstrap(){const config=await api('/config');$('#mode').textContent='LIVE · '+config.model;$('#mode').classList.add('live');$('#connections').replaceChildren();for(const [name,value,on] of [['LangGraph','Persistent',true],['Qdrant',config.qdrant,true],['Embeddings',config.embedding_model,true],['LLM',config.model,true],['Web search',config.web_search?'Connected':'Нужен ключ',config.web_search],['Sandbox E2B',config.sandbox?'Connected':'Нужен ключ',config.sandbox]]){const row=el('div',undefined,'connection');row.append(el('span',name),el('b',value,on?'':'off'));$('#connections').append(row);}await Promise.all([refreshThreads(),refreshDocuments(),refreshMemory(),refreshTickets()]);}
guarded(bootstrap)();
